"""Bounded, hardware-free projection of growing SHADOW JSONL captures."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from rear_warning.voice.autoconnect import MOTOAutoConnector
from rear_warning.voice.config import load_voice_settings


class _Tail:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.inode: int | None = None
        self.offset = 0
        self.remainder = ""

    def reset(self, path: Path) -> None:
        self.path = path
        self.inode = None
        self.offset = 0
        self.remainder = ""

    def read(self) -> list[dict[str, object]]:
        if self.path is None or not self.path.is_file():
            return []
        stat = self.path.stat()
        if self.inode != stat.st_ino or stat.st_size < self.offset:
            self.inode = stat.st_ino
            self.offset = 0
            self.remainder = ""
        with self.path.open("r", encoding="utf-8") as stream:
            stream.seek(self.offset)
            chunk = stream.read()
            self.offset = stream.tell()
        text = self.remainder + chunk
        lines = text.splitlines(keepends=True)
        self.remainder = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self.remainder = lines.pop()
        records: list[dict[str, object]] = []
        for line in lines:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        return records


class CaptureDashboardSource:
    """One bounded shared source; HTTP clients only read immutable snapshots."""

    def __init__(
        self,
        capture_root: Path,
        *,
        mode: str = "LIVE",
        history_limit: int = 120,
        voice_config: Path | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if history_limit <= 0:
            raise ValueError("history_limit must be greater than zero")
        if mode not in {"LIVE", "REPLAY", "ANY"}:
            raise ValueError("mode must be LIVE, REPLAY, or ANY")
        self.capture_root = capture_root
        self.mode = mode
        self.history = deque(maxlen=history_limit)
        self._clock = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_id: str | None = None
        self._shadow_tail = _Tail()
        self._motion_tail = _Tail()
        self._shadow: dict[str, object] | None = None
        self._motion: dict[str, object] | None = None
        self._metadata: dict[str, object] = {}
        self._observed_growth = False
        self._initialized_run = False
        self._error: str | None = None
        self._headset: dict[str, object] = {
            "connected": None, "audio_ready": None, "reason": "not_configured"
        }
        self._headset_checked_at = -math.inf
        self._connector: MOTOAutoConnector | None = None
        if voice_config is not None:
            settings = load_voice_settings(voice_config)
            self._connector = MOTOAutoConnector(settings)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="dashboard-source", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.wait(0.25):
            self.poll_once()

    def poll_once(self) -> None:
        try:
            directory = self._latest_directory()
            with self._lock:
                self._poll_locked(directory)
                self._poll_headset_locked()
                self._error = None
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return self._snapshot_locked()

    def _latest_directory(self) -> Path | None:
        if (self.capture_root / "shadow.jsonl").is_file():
            return self.capture_root
        prefixes = {"LIVE": ("live-",), "REPLAY": ("replay-",), "ANY": ("live-", "replay-")}
        candidates = [
            item for item in self.capture_root.iterdir()
            if item.is_dir() and item.name.startswith(prefixes[self.mode])
        ] if self.capture_root.is_dir() else []
        return max(candidates, key=lambda item: item.name) if candidates else None

    def _poll_locked(self, directory: Path | None) -> None:
        run_id = None if directory is None else directory.name
        if run_id != self._run_id:
            self._run_id = run_id
            self._shadow = None
            self._motion = None
            self._metadata = {}
            self.history.clear()
            self._observed_growth = self._initialized_run
            self._initialized_run = True
            if directory is not None:
                self._shadow_tail.reset(directory / "shadow.jsonl")
                self._motion_tail.reset(directory / "bno055.jsonl")
        if directory is None:
            return
        previous_timestamp = _number(self._shadow, "timestamp_seconds")
        shadow_records = self._shadow_tail.read()
        motion_records = self._motion_tail.read()
        if shadow_records:
            self._shadow = shadow_records[-1]
            current_timestamp = _number(self._shadow, "timestamp_seconds")
            if previous_timestamp is not None and current_timestamp != previous_timestamp:
                self._observed_growth = True
            for record in shadow_records:
                self._append_history(record)
        if motion_records:
            self._motion = motion_records[-1]
        metadata = directory / "metadata.json"
        if metadata.is_file():
            value = json.loads(metadata.read_text(encoding="utf-8"))
            self._metadata = value if isinstance(value, dict) else {}

    def _append_history(self, record: dict[str, object]) -> None:
        selection = record.get("selection")
        selected = selection.get("selected_target") if isinstance(selection, dict) else None
        self.history.append({
            "timestamp_seconds": record.get("timestamp_seconds"),
            "distance_m": selected.get("distance_m") if isinstance(selected, dict) else None,
        })

    def _poll_headset_locked(self) -> None:
        now = self._clock()
        if self._connector is None or now - self._headset_checked_at < 2.0:
            return
        status = self._connector.status()
        self._headset = {
            "connected": status.connected,
            "audio_ready": status.ready,
            "reason": status.reason,
        }
        self._headset_checked_at = now

    def _snapshot_locked(self) -> dict[str, object]:
        now = self._clock()
        shadow = self._shadow or {}
        motion = self._motion or {}
        timestamp = _number(shadow, "timestamp_seconds")
        motion_timestamp = _number(motion, "canonical_captured_at_monotonic_seconds")
        complete = bool(self._metadata.get("recording_complete"))
        stale_after = float(self._metadata.get("stale_after_seconds", 1.0))
        if self._run_id is None:
            source_state, source_reason = "not_started", "尚未找到指定來源"
        elif self._error:
            source_state, source_reason = "error", self._error
        elif complete:
            source_state, source_reason = "stopped", "來源採集已停止"
        elif not self._observed_growth:
            source_state, source_reason = "historical", "僅載入啟動前既有資料"
        elif timestamp is None:
            source_state, source_reason = "not_started", "來源尚無 SHADOW 資料"
        elif now - timestamp > stale_after:
            source_state, source_reason = "stale", "來源資料已過期"
        else:
            source_state, source_reason = "live", "來源持續更新"
        live_age = source_state in {"live", "stale"}
        selection = shadow.get("selection")
        selection = selection if isinstance(selection, dict) else {}
        selected = selection.get("selected_target")
        selected = selected if isinstance(selected, dict) else None
        candidates = selection.get("candidates")
        candidates = candidates if isinstance(candidates, list) else ([] if selected is None else [selected])
        calibration = motion.get("calibration")
        calibration = calibration if isinstance(calibration, dict) else {}
        quality = _motion_quality(motion)
        fusion = shadow.get("fusion")
        fusion = fusion if isinstance(fusion, dict) else None
        profile = self._metadata.get("bno055_calibration_profile")
        profile = profile if isinstance(profile, dict) else {}
        return {
            "generated_monotonic_seconds": now,
            "run_id": self._run_id,
            "source_mode": self._metadata.get("mode") or self.mode,
            "source_state": source_state,
            "source_reason": source_reason,
            "experimental_warning": "實驗資料顯示／未啟用實體警示",
            "radar": {
                "timestamp_seconds": timestamp,
                "age_seconds": max(0.0, now - timestamp) if live_age and timestamp is not None else None,
                "availability": shadow.get("availability"),
                "reason": shadow.get("reason"),
                "target_count": len(selection.get("candidate_ids", [])) if isinstance(selection.get("candidate_ids"), list) else len(candidates),
                "selected_target": selected,
                "candidates": candidates,
                "selection_reason": selection.get("reason"),
                "axis_note": "X／Y／Vx 車體軸向與正負尚未確認；Vx 不是接近速度或車速",
                "warning_candidate": shadow.get("distance_warning_candidate"),
                "warning_stabilized": shadow.get("distance_warning_stabilized"),
            },
            "bno055": {
                "timestamp_seconds": motion_timestamp,
                "age_seconds": max(0.0, now - motion_timestamp) if live_age and motion_timestamp is not None else None,
                "euler": motion.get("euler"),
                "quaternion": motion.get("quaternion"),
                "linear_acceleration_mps2": motion.get("linear_acceleration_mps2"),
                "gravity_mps2": motion.get("gravity_mps2"),
                "angular_velocity": None,
                "angular_velocity_reason": "現行 SHADOW 未保存角速度",
                "calibration": calibration,
                "quality_valid": quality[0],
                "quality_reason": quality[1],
                "fully_calibrated": bool(calibration) and all(calibration.get(key) == 3 for key in ("system", "gyroscope", "accelerometer", "magnetometer")),
                "profile_loaded": profile.get("loaded", False),
            },
            "fusion": {
                "ready": fusion.get("is_ready") if fusion else False,
                "valid": fusion.get("is_valid") if fusion else False,
                "classification": fusion.get("classification") if fusion else None,
                "reason": fusion.get("reason") if fusion else shadow.get("reason"),
                "sync": shadow.get("sync"),
            },
            "headset": dict(self._headset),
            "distance_history": list(self.history),
        }


def _number(value: dict[str, object] | None, key: str) -> float | None:
    if not isinstance(value, dict):
        return None
    item = value.get(key)
    return float(item) if isinstance(item, (int, float)) and not isinstance(item, bool) else None


def _motion_quality(record: dict[str, object]) -> tuple[bool, str]:
    if not record:
        return False, "尚無 BNO055 資料"
    issues: list[str] = []
    if record.get("operation_mode") != 0x0C:
        issues.append("operation_mode 非 NDOF")
    if record.get("system_status") != 0x05:
        issues.append("system_status 非 fusion running")
    if record.get("system_error") != 0:
        issues.append("system_error 非 0")
    for name, low, high in (("quaternion", 0.5, 1.5), ("gravity_mps2", 5.0, 15.0)):
        vector = record.get(name)
        if not isinstance(vector, dict):
            issues.append(f"缺少 {name}")
            continue
        keys = ("w", "x", "y", "z") if name == "quaternion" else ("x", "y", "z")
        values = [vector.get(key) for key in keys]
        if not all(isinstance(item, (int, float)) and math.isfinite(float(item)) for item in values):
            issues.append(f"{name} 含無效值")
            continue
        norm = math.sqrt(sum(float(item) ** 2 for item in values))
        if not low <= norm <= high:
            issues.append(f"{name} norm={norm:.3f} 超出 {low}–{high}")
    return not issues, "資料完整性通過（不等於完整校正）" if not issues else "；".join(issues)
