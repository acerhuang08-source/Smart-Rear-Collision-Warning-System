"""Bounded nonblocking output and I/O-free live-state formatting."""

from __future__ import annotations

import math
import os
import re
import select
import time
import unicodedata
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from rear_warning.fusion import RelativeMotionAssessment, RelativeMotionFusionAdapter
from rear_warning.synchronization.models import (
    SensorSyncStatus, SynchronizedSensorSample, is_valid_distance_measurement,
)

CLASS_NAMES = {
    "uncertain": "資料不足或情況不確定",
    "stable_or_receding": "距離穩定或遠離",
    "static_or_self_approach": "接近可能由自身動作造成",
    "target_approach": "符合目標接近判定條件",
}
REASONS = {
    "distance_trend_not_ready": "距離趨勢暖機中",
    "own_motion_not_ready": "動作證據暖機中",
    "sync_unhealthy": "時間關聯或輸入品質異常",
    "motion_stale": "動作資料過期",
    "ambiguous_closing": "接近速率未達明確條件",
    "stationary_baseline_not_established": "缺少有效近期靜止基準",
    "self_translational_evidence": "平移證據達條件",
    "self_rotational_evidence": "旋轉證據達條件",
    "self_translational_and_rotational_evidence": "平移及旋轉證據達條件",
    "stable_or_receding_range": "距離穩定或增加",
    "target_approach_evidence": "基準及持續接近條件成立",
    "candidate_persistence_not_satisfied": "候選持續時間未滿",
    "timestamp_discontinuity": "樣本時間不連續",
}

HEALTH_NAMES = {
    "matched": "正常", "no_motion_sample": "尚無動作資料",
    "stale_motion_sample": "動作資料過期", "future_motion_sample": "動作時間超前",
    "invalid_distance_sample": "距離資料無效", "invalid_motion_sample": "動作資料無效",
    "motion_stale": "動作資料過期",
}
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_RESTORE = "\x1b[0m\x1b[?25h\x1b[?1049l"


def plain_text(text: str) -> str:
    """Remove terminal controls from externally supplied messages."""
    return "".join(c for c in _ANSI.sub("", text)
                   if not unicodedata.category(c).startswith("C"))


def cell_width(text: str) -> int:
    """Width of this UI's CJK/ASCII text, excluding ANSI and combining marks."""
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
               for c in plain_text(text))


def wrap_cells(text: str, width: int) -> list[str]:
    """Wrap without dropping long reasons, using terminal cells rather than len."""
    if width < 2:
        raise ValueError("text width must be at least two cells")
    lines: list[str] = []
    line = ""
    used = 0
    for char in plain_text(text):
        size = cell_width(char)
        if used + size > width:
            lines.append(line)
            line, used = "", 0
        line += char
        used += size
    return [*lines, line]


@dataclass(frozen=True)
class PanelSnapshot:
    elapsed: float
    distance: str = "資料不足"
    rate: str = "資料不足"
    state: str = "等待首筆資料"
    classification: str = "尚無"
    candidate: str = "無"
    reason: str = "等待首筆資料"
    translation: str = "資料不足"
    rotation: str = "資料不足"
    baseline: str = "不可用"
    health: str = "等待首筆資料"
    distance_age: str = "尚無"
    motion_age: str = "尚無"
    dropped_status: int = 0
    dropped_events: int = 0
    fault: bool = False


@dataclass(frozen=True)
class PanelEvent:
    elapsed: float
    text: str


def render_panel(
    snapshot: PanelSnapshot, events: tuple[PanelEvent, ...], *, width: int,
    height: int, label: str = "LIVE 實際感測", color: bool = False,
) -> list[str] | None:
    """Return a fitting frame, or request plain fallback; never trim a reason."""
    if width < 38 or height < 18:
        return None
    # Leave the final column unused to avoid terminal auto-wrap on last column.
    available = min(width - 1, 100)
    wide = available >= 72
    lines: list[str] = []

    def row(left: str, right: str = "") -> None:
        if right and wide and cell_width(left) <= 37:
            lines.extend(wrap_cells(left + " " * (39 - cell_width(left)) + right, available))
        else:
            lines.extend(wrap_cells(left, available))
            if right:
                lines.extend(wrap_cells(right, available))

    row("智慧型後方警示系統", label)
    row(f"運行時間  {snapshot.elapsed:.2f} 秒", "Ctrl+C 停止")
    row("-" * available)
    row(f"距離        {snapshot.distance}", f"接近速率  {snapshot.rate}")
    row("")
    state = snapshot.state
    if color:
        style = "1;31" if snapshot.fault else "1" if state == "有效輸出" else "1;33"
        state = f"\x1b[{style}m{state}\x1b[0m"
    # Apply color only after wrapping (the wrap helper deliberately sanitizes).
    state_rows = wrap_cells(f"判斷狀態    {snapshot.state}", available)
    if color:
        state_rows = [line.replace(snapshot.state, state) for line in state_rows]
    lines.extend(state_rows)
    row(f"最近確認分類  {snapshot.classification}")
    row(f"待確認候選    {snapshot.candidate}")
    row(f"原因          {snapshot.reason}")
    row("")
    row(f"自身平移    {snapshot.translation}", f"自身旋轉  {snapshot.rotation}")
    row(f"靜止基準    {snapshot.baseline}", f"時間配對  {snapshot.health}")
    row("-" * available)
    row(f"有效資料年齡  距離 {snapshot.distance_age} / 動作 {snapshot.motion_age}")
    row(f"顯示丟棄      狀態 {snapshot.dropped_status} / 事件 {snapshot.dropped_events}")
    row("最近事件（歷史）")
    if len(lines) > height - 1:
        return None
    # Keep the newest events that fit, still ordered by their original time.
    event_lines: list[str] = []
    for event in reversed(events[-(3 if wide else 1):]):
        minutes, seconds = divmod(max(0.0, event.elapsed), 60)
        block = wrap_cells(f"  {int(minutes):02d}:{seconds:06.3f}  {event.text}", available)
        if len(lines) + len(event_lines) + len(block) > height - 1:
            break
        event_lines = block + event_lines
    return lines + event_lines


def format_summary(report: str) -> str:
    """Keep all original report fields, one short statistic per line."""
    lines: list[str] = []
    for line in report.splitlines():
        if line.startswith("[LIVE 結束] "):
            lines.append("[LIVE 結束]")
            lines.extend("  " + field for field in line.removeprefix("[LIVE 結束] ").split())
        else:
            lines.extend(wrap_cells(line, 76))
    return "\n".join(lines)


class TerminalBackpressureError(OSError):
    """The terminal has stopped accepting output within the allowed delay."""


class BoundedTerminal:
    """One latest status, 16 historical events, and at most one partial line.

    flush_once performs at most one nonblocking write. No sampling callback
    waits for SSH. Superseded/expired status and event overflow are counted.
    """

    def __init__(
        self, fd: int, *, monotonic: Callable[[], float] = time.monotonic,
        plain: bool = False, label: str = "LIVE 實際感測",
    ) -> None:
        self.fd = fd
        self.clock = monotonic
        self.dropped_status = 0
        self.dropped_events = 0
        self._events: deque[bytes] = deque(maxlen=16)
        self._latest: tuple[float, bytes] | None = None
        self._partial = b""
        self._pending_since: float | None = None
        self._was_blocking = os.get_blocking(fd)
        self.panel_enabled = not plain and os.isatty(fd) and os.environ.get("TERM") != "dumb"
        self.label = label
        self._panel_active = False
        self._needs_restore = False
        self._history: deque[PanelEvent] = deque(maxlen=3)
        self._event_serial = 0
        self._accounted_serial = 0
        self._latest_event_serial: int | None = None
        self._partial_event_serial: int | None = None
        os.set_blocking(fd, False)

    def record_event(self, event: PanelEvent, plain: str, now: float) -> None:
        if not self.panel_enabled:
            self.publish(plain, now, event=True)
            return
        self._event_serial += 1
        if self._event_serial - self._accounted_serial > 3:
            self.dropped_events += 1
            self._accounted_serial += 1
        self._history.append(event)

    def display(self, snapshot: PanelSnapshot, plain: str, now: float) -> None:
        if not self.panel_enabled:
            self.publish(plain, now)
            return
        try:
            size = os.get_terminal_size(self.fd)
        except OSError:
            size = os.terminal_size((80, 24))
        lines = render_panel(snapshot, tuple(self._history), width=size.columns,
                             height=size.lines, label=self.label,
                             color="NO_COLOR" not in os.environ)
        if lines is None:
            # Permanently fall back for this session: do not repeatedly switch
            # screens or erase scrollback if an error message needs more room.
            self.panel_enabled = False
            if self._panel_active:
                self.publish(_RESTORE, now, event=True)
                self._panel_active = False
            self.publish("[版面] 視窗不足以完整顯示，改用純文字（原因保留）。", now, event=True)
            for event in self._history:
                self.publish(f"[{self.label} 歷史 t={event.elapsed:.3f}s] {event.text}", now, event=True)
            self.publish(plain, now)
            return
        if not self._panel_active:
            self.publish("\x1b[?1049h\x1b[?25l\x1b[0m", now, event=True)
            self._panel_active = self._needs_restore = True
        # Home + erase each rewritten row + erase remaining rows. No external
        # clear and no full-screen clear on every frame. One output owner only.
        frame = "\x1b[H" + "\r\n".join("\x1b[2K" + line for line in lines) + "\x1b[J"
        self.publish(frame, now)
        self._latest_event_serial = self._event_serial

    def message(self, text: str, now: float, *, started: float) -> None:
        """Initialization uses the same output owner as subsequent frames."""
        if self.panel_enabled:
            reason = text.split("] ", 1)[-1] if text.startswith("[") else text
            self.display(PanelSnapshot(now - started, state="初始化", reason=reason), text, now)
        else:
            self.publish(text, now, event=True)

    def publish(self, line: str, now: float, *, event: bool = False) -> None:
        data = (line + "\n").encode("utf-8")
        if len(data) > 4096:
            raise ValueError("terminal line exceeds 4096 bytes")
        if event:
            if len(self._events) == self._events.maxlen:
                self.dropped_events += 1
            self._events.append(data)
        else:
            if self._latest is not None:
                self.dropped_status += 1
            self._latest = (now, data)
            self._latest_event_serial = None
        if self._pending_since is None:
            self._pending_since = now

    @property
    def pending(self) -> bool:
        return bool(self._partial or self._events or self._latest)

    def flush_once(self, now: float) -> None:
        if not self.pending:
            self._pending_since = None
            return
        if self._pending_since is not None and now - self._pending_since > 2.0:
            raise TerminalBackpressureError("終端機輸出停滯超過 2 秒，停止採集")
        kind = "partial"
        data = self._partial
        if not data:
            if self._events:
                kind, data = "event", self._events[0]
            elif self._latest is not None:
                created, data = self._latest
                if now - created > 0.25:
                    self._latest = None
                    self.dropped_status += 1
                    return
                kind = "status"
        if data:
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
                return
            if written <= 0:
                raise BrokenPipeError("終端機已中斷")
            if kind == "event":
                self._events.popleft()
            elif kind == "status":
                self._latest = None
                self._partial_event_serial = self._latest_event_serial
            self._partial = data[written:]
            # Reset only after a complete line, not trickling partial bytes.
            if not self._partial:
                if self._partial_event_serial is not None:
                    self._accounted_serial = max(self._accounted_serial, self._partial_event_serial)
                    self._partial_event_serial = None
                self._pending_since = now if self.pending else None

    def discard_pending(self) -> None:
        """Discard whole queued lines; retain any already partially sent line."""
        accounted = max(self._accounted_serial, self._partial_event_serial or 0)
        self.dropped_events += self._event_serial - accounted
        self._accounted_serial = self._event_serial
        self.dropped_events += len(self._events)
        self._events.clear()
        if self._latest is not None:
            self.dropped_status += 1
            self._latest = None

    def finish(self, line: str) -> None:
        """After hardware cleanup, discard queued status and drain a summary."""
        self.discard_pending()
        now = self.clock()
        self._pending_since = now
        self.publish((_RESTORE if self._needs_restore else "") + line, now, event=True)
        deadline = now + 0.5
        while self.pending:
            now = self.clock()
            if now >= deadline:
                raise TerminalBackpressureError("摘要輸出逾時；硬體 cleanup 已完成嘗試")
            self.flush_once(now)
            if self.pending:
                select.select([], [self.fd], [], min(0.02, deadline - now))
        self._needs_restore = False
        self._panel_active = False

    def close(self) -> None:
        try:
            if self._needs_restore:
                # Best effort even after broken pipe/partial-frame failure;
                # never block cleanup if the peer has stopped reading.
                data = _RESTORE.encode("ascii")
                while data:
                    written = os.write(self.fd, data)
                    if written <= 0:
                        raise BrokenPipeError("終端機還原失敗")
                    data = data[written:]
                self._needs_restore = False
        finally:
            os.set_blocking(self.fd, self._was_blocking)


def _age(now: float, timestamp: float | None) -> str:
    return "尚無" if timestamp is None else f"{max(0.0, now - timestamp):.3f}s"


class LivePresentation:
    """Run every sample through fusion, independently of the display cadence."""

    def __init__(
        self, terminal: BoundedTerminal, *, started: float, refresh_hz: float = 5.0,
    ) -> None:
        if not math.isfinite(refresh_hz) or not 0 < refresh_hz <= 20:
            raise ValueError("refresh_hz must be finite and in (0, 20]")
        self.terminal = terminal
        self.started = started
        self.interval = 1.0 / refresh_hz
        self.adapter = RelativeMotionFusionAdapter()
        self.sample: SynchronizedSensorSample | None = None
        self.assessment: RelativeMotionAssessment | None = None
        self.last_valid_distance: float | None = None
        self.last_valid_motion: float | None = None
        self._next_display = started
        self._event_key: tuple[str, ...] | None = None

    def on_sample(self, sequence: int, sample: SynchronizedSensorSample) -> None:
        self.assessment = self.adapter.update(sample)
        self.sample = sample
        if is_valid_distance_measurement(sample.distance_measurement):
            self.last_valid_distance = sample.distance_timestamp
        if sample.status is SensorSyncStatus.MATCHED:
            self.last_valid_motion = sample.motion_timestamp
        self._event(sample.distance_timestamp)

    def _state(self, now: float) -> tuple[str, str]:
        if self.sample is None or self.assessment is None:
            return "uncertain", "等待首筆資料"
        if now - self.sample.distance_timestamp > 0.25:
            return "uncertain", "資料斷流／過期"
        health = self.sample.status.value
        if self.assessment.reason.value == "motion_stale":
            health = "motion_stale"
        return self.assessment.classification.value, health

    def _event(self, now: float) -> None:
        state = self._state(now)
        assessment = self.assessment
        candidate = None if assessment is None else assessment.candidate_classification
        key = (*state, "" if assessment is None else assessment.reason.value,
               "" if candidate is None else candidate.value)
        if key != self._event_key:
            self._event_key = key
            snapshot = self.snapshot(now)
            text = (f"{snapshot.state}：{snapshot.reason}" if snapshot.state != "有效輸出"
                    else f"分類更新：{CLASS_NAMES[state[0]]}")
            plain = (f"[LIVE 實際感測 EVENT 歷史轉換 t={now - self.started:.3f}s] "
                     f"{CLASS_NAMES[state[0]]} ({state[0].upper()}) 健康={state[1]} {text}")
            self.terminal.record_event(PanelEvent(now - self.started, text), plain, now)

    def snapshot(self, now: float) -> PanelSnapshot:
        """Project public fusion outputs only; stale values are not retained."""
        sample, assessment = self.sample, self.assessment
        common = dict(
            elapsed=max(0.0, now - self.started),
            distance_age=_age(now, self.last_valid_distance).replace("s", " 秒"),
            motion_age=_age(now, self.last_valid_motion).replace("s", " 秒"),
            dropped_status=self.terminal.dropped_status,
            dropped_events=self.terminal.dropped_events,
        )
        if sample is None or assessment is None:
            return PanelSnapshot(**common)
        if now - sample.distance_timestamp > 0.25:
            return PanelSnapshot(**common, state="失效／不確定", classification="不可用（已過期）",
                                 reason="資料斷流／過期；舊判斷不再有效", health="資料過期", fault=True)
        valid_distance = is_valid_distance_measurement(sample.distance_measurement)
        trend, motion = assessment.distance_trend, assessment.own_motion_evidence
        motion_usable = (sample.status is SensorSyncStatus.MATCHED and motion.is_ready
                         and assessment.reason.value != "motion_stale")
        candidate = assessment.candidate_classification
        health = self._state(now)[1]
        # The public API also marks early warmup invalid; this is missing
        # evidence, not a sensor read failure. Do not color warmup red.
        fault = (health != "matched" or assessment.reason.value == "timestamp_discontinuity")
        state = "有效輸出" if assessment.is_ready else "暖機／資料不足"
        if candidate is not None:
            state = "待確認"
        if assessment.classification.value == "uncertain" and assessment.is_ready:
            state = "不確定"
        if fault:
            state = "失效／不確定"
        reason = REASONS.get(assessment.reason.value, "未提供")
        if not assessment.is_ready and candidate is None and "暖機" not in reason and not fault:
            state = "不確定"
        return PanelSnapshot(
            **common,
            distance=f"{sample.distance_measurement.distance_cm / 100:.2f} 公尺" if valid_distance else "資料不足（無效）",
            rate=f"{trend.closing_rate_mps:.2f} 公尺/秒" if valid_distance and trend.is_ready else "資料不足",
            state=state,
            classification=(CLASS_NAMES[assessment.classification.value]
                            if not fault and assessment.classification.value != "uncertain" else "不可用／不確定"),
            candidate=CLASS_NAMES[candidate.value] if candidate is not None else "無",
            reason=reason,
            translation=("達條件" if motion.translational_motion_detected else "未達條件") if motion_usable else "資料不足",
            rotation=("達條件" if motion.rotational_motion_detected else "未達條件") if motion_usable else "資料不足",
            baseline="已成立" if assessment.stationary_baseline_ready else "未成立",
            health=HEALTH_NAMES.get(health, health), fault=fault,
        )

    def format_status(self, now: float) -> str:
        classification, health = self._state(now)
        sample, assessment = self.sample, self.assessment
        prefix = (
            f"[LIVE 實際感測 t={now - self.started:.2f}s] "
            f"樣本年齡={_age(now, None if sample is None else sample.distance_timestamp)} "
            f"有效距離年齡={_age(now, self.last_valid_distance)} "
            f"有效動作年齡={_age(now, self.last_valid_motion)}"
        )
        if sample is None or assessment is None or now - sample.distance_timestamp > 0.25:
            return (
                f"{prefix} 距離=資料不足 接近率=資料不足 平移/旋轉=資料不足 "
                f"分類={CLASS_NAMES['uncertain']} (UNCERTAIN) 基準=不可用 健康={health}"
            )
        valid_distance = is_valid_distance_measurement(sample.distance_measurement)
        distance = f"{sample.distance_measurement.distance_cm / 100:.2f}m" if valid_distance else "無效"
        trend = assessment.distance_trend
        rate = (
            f"{trend.closing_rate_mps:.2f}m/s"
            if valid_distance and trend.is_ready else "資料不足"
        )
        motion = assessment.own_motion_evidence
        motion_usable = (
            sample.status is SensorSyncStatus.MATCHED and motion.is_ready
            and assessment.reason.value != "motion_stale"
        )
        translation = "資料不足" if not motion_usable else (
            "達條件" if motion.translational_motion_detected else "未達條件"
        )
        rotation = "資料不足" if not motion_usable else (
            "達條件" if motion.rotational_motion_detected else "未達條件"
        )
        candidate = assessment.candidate_classification
        return (
            f"{prefix} 距離={distance} 接近率={rate} 平移={translation} 旋轉={rotation} "
            f"分類={CLASS_NAMES[classification]} ({classification.upper()}) "
            f"基準={'成立' if assessment.stationary_baseline_ready else '不足'} "
            f"健康={health} 判斷={'ready' if assessment.is_ready else '暖機/待定'} "
            f"原因={REASONS[assessment.reason.value]} ({assessment.reason.value}) "
            f"候選={candidate.name if candidate is not None else '無'} "
            f"丟棄顯示={self.terminal.dropped_status}/{self.terminal.dropped_events}"
        )

    def on_tick(self, now: float) -> None:
        self._event(now)
        if now >= self._next_display:
            self.terminal.display(self.snapshot(now), self.format_status(now), now)
            self._next_display = now + self.interval
        self.terminal.flush_once(now)
