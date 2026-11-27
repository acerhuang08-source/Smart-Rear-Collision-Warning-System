"""ioRadar701+BNO055 shadow replay and manually gated live capture."""

from __future__ import annotations

import argparse
import json
import math
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, Protocol, TextIO

from rear_warning.live.hardware import check_devices_available
from rear_warning.live.ioradar701_shadow import IoRadar701ShadowCore, ShadowAssessment
from rear_warning.live.terminal import BoundedTerminal, PanelSnapshot
from rear_warning.sensors.bno055.device import BNO055Device, RegisterIO
from rear_warning.sensors.bno055.models import (
    BNO055Calibration,
    BNO055Measurement,
    EulerAngles,
    Quaternion,
    Vector3,
)
from rear_warning.sensors.ioradar701 import RadarFrame, RadarTargetSelector
from rear_warning.synchronization import InvalidMotionMeasurement
from rear_warning.warning import HysteresisThresholds

_CLOCK: Callable[[], float] = time.monotonic
_WALL_CLOCK: Callable[[], datetime] = lambda: datetime.now().astimezone()
_SLEEP: Callable[[float], None] = time.sleep
_PREFLIGHT: Callable[[str, int], None] = check_devices_available
_SHORT_OPERATOR_EVENTS = frozenset(
    {
        "static_start",
        "led_test_start",
        "led_test_end",
        "motion_start",
        "motion_end",
        "far",
        "warning",
        "danger",
        "release",
        "no_targets",
    }
)


def _poll_operator_line(input_stream: TextIO) -> str | None:
    """Return one complete operator line without blocking sensor sampling."""

    try:
        descriptor = input_stream.fileno()
    except (AttributeError, OSError):
        return None
    try:
        ready, _, _ = select.select([descriptor], [], [], 0)
    except (OSError, TypeError, ValueError):
        return None
    if not ready:
        return None
    line = input_stream.readline()
    return None if line == "" else line.strip()


def _operator_event_detail(line: str) -> str | None:
    """Parse a documented short command or ``:event <detail>``."""

    if not isinstance(line, str):
        raise TypeError("operator line must be a string")
    normalized_line = line.strip()
    if normalized_line in _SHORT_OPERATOR_EVENTS:
        return normalized_line
    command, separator, detail = normalized_line.partition(" ")
    if command != ":event" or not separator:
        return None
    normalized = detail.strip()
    return normalized or None


_OPERATOR_LINE_POLLER: Callable[[TextIO], str | None] = _poll_operator_line


class RadarSerial(Protocol):
    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


class MotionDevice(Protocol):
    def read_identity(self) -> object: ...
    def initialize_ndof(self) -> tuple[int, int, int]: ...
    def read_measurement(self) -> BNO055Measurement: ...
    def close(self) -> None: ...


def _positive(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("必須是有限正數")
    return parsed


def _address(value: str) -> int:
    parsed = int(value, 0)
    if parsed not in (0x28, 0x29):
        raise argparse.ArgumentTypeError("位址必須是 0x28 或 0x29")
    return parsed


def _add_live_arguments(live: argparse.ArgumentParser) -> None:
    """Add the shared, hardware-live UART and BNO055 options."""
    live.add_argument("--confirm-hardware", action="store_true")
    live.add_argument("--port", default="/dev/ttyAMA0")
    live.add_argument("--baudrate", type=int, default=230400)
    live.add_argument("--i2c-bus", type=int, default=1)
    live.add_argument("--bno055-address", type=_address, default=0x29)
    live.add_argument("--uart-timeout", type=_positive, default=0.02)
    live.add_argument("--motion-period", type=_positive, default=0.1)
    live.add_argument("--maximum-motion-age", type=_positive, default=0.2)
    live.add_argument("--stale-after", type=_positive, default=1.0)
    live.add_argument("--refresh-hz", type=_positive, default=5.0)
    live.add_argument("--duration", type=_positive)
    live.add_argument("--output-root", type=Path, default=Path("shadow-results"))
    live.add_argument("--plain", action="store_true")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="ioradar701-shadow",
        description="ioRadar701＋BNO055 SHADOW：只顯示／記錄，不控制 GPIO／LED",
    )
    commands = result.add_subparsers(dest="command", required=True)
    replay = commands.add_parser("replay", help="重播既有 ioRadar701 capture；不碰硬體")
    replay.add_argument("capture", type=Path)
    replay.add_argument("--output-root", type=Path, default=Path("shadow-results"))
    replay.add_argument("--stale-after", type=_positive, default=1.0)
    replay.add_argument("--plain", action="store_true")

    live = commands.add_parser("live", help="Enter 後開 UART／I²C 的真實 shadow 入口")
    _add_live_arguments(live)
    return result


def _default_serial_factory(port: str, baudrate: int, timeout: float) -> RadarSerial:
    import fcntl
    import termios
    import serial

    connection = serial.Serial(
        port=None,
        baudrate=baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=timeout,
        write_timeout=0,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
        exclusive=True,
    )
    try:
        connection.port = port
        connection.dtr = False
        connection.rts = False
        connection.open()
        fcntl.ioctl(connection.fileno(), termios.TIOCEXCL)
        return connection
    except BaseException:
        connection.close()
        raise


def _default_register_factory(bus: int) -> RegisterIO:
    from rear_warning.sensors.bno055.hardware import load_smbus_register_io
    factory = load_smbus_register_io()
    return factory(bus)  # type: ignore[no-any-return]


_SERIAL_FACTORY: Callable[[str, int, float], RadarSerial] = _default_serial_factory
_REGISTER_FACTORY: Callable[[int], RegisterIO] = _default_register_factory
_MOTION_FACTORY: Callable[..., MotionDevice] = BNO055Device
_TERMINAL_FACTORY: Callable[..., BoundedTerminal] = BoundedTerminal


class _LiveObserver(Protocol):
    """Optional physical-output observer owned by the live capture loop."""

    def start(self, capture: _Capture, timestamp: float) -> None: ...

    def observe(self, assessment: ShadowAssessment, capture: _Capture) -> None: ...

    def finish(self, capture: _Capture, timestamp: float) -> None: ...

    def cleanup_status(self) -> dict[str, bool]: ...


class _Capture:
    def __init__(
        self,
        directory: Path,
        *,
        raw: bool,
        gpio_enabled: bool = False,
    ) -> None:
        self.directory = directory
        self._gpio_enabled = gpio_enabled
        opened: list[BinaryIO | TextIO] = []
        try:
            self.raw_file: BinaryIO | None = (
                (directory / "raw.bin").open("xb") if raw else None
            )
            if self.raw_file is not None:
                opened.append(self.raw_file)
            self.decoded = (directory / "decoded.jsonl").open("x", encoding="utf-8")
            opened.append(self.decoded)
            self.motion = (directory / "bno055.jsonl").open(
                "x", encoding="utf-8", buffering=1
            )
            opened.append(self.motion)
            self.shadow = (directory / "shadow.jsonl").open(
                "x", encoding="utf-8", buffering=1
            )
            opened.append(self.shadow)
            self.events = (directory / "events.jsonl").open("x", encoding="utf-8")
            opened.append(self.events)
            self.led = (
                (directory / "led.jsonl").open("x", encoding="utf-8", buffering=1)
                if gpio_enabled else None
            )
            if self.led is not None:
                opened.append(self.led)
        except BaseException:
            for output in opened:
                try:
                    output.close()
                except OSError:
                    pass
            raise
        self.raw_bytes = 0
        self.frames = 0
        self.motion_records = 0
        self.shadow_records = 0
        self.fusion_updates_applied = 0
        self.fusion_updates_coalesced = 0
        self.time_discontinuities = 0
        self.led_records = 0

    def write_raw(self, data: bytes) -> None:
        if self.raw_file is None:
            return
        written = self.raw_file.write(data)
        if written != len(data):
            raise OSError(f"raw.bin short write: {written}/{len(data)}")
        self.raw_bytes += written

    @staticmethod
    def _line(output: TextIO, value: dict[str, object]) -> None:
        output.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")

    def write_frame(
        self,
        frame: RadarFrame,
        timestamp: float,
        receipt_batch_sequence: int,
    ) -> None:
        value = frame.as_dict()
        value["received_at_monotonic_seconds"] = timestamp
        value["receipt_batch_sequence"] = receipt_batch_sequence
        self._line(self.decoded, value)
        self.frames += 1

    def write_motion(self, measurement: BNO055Measurement,
                     canonical_timestamp: float) -> None:
        record = _motion_record(measurement)
        record["canonical_captured_at_monotonic_seconds"] = canonical_timestamp
        self._line(self.motion, record)
        self.motion_records += 1

    def write_assessment(self, assessment: ShadowAssessment) -> None:
        record = assessment.as_dict()
        record["gpio_enabled"] = self._gpio_enabled
        record["shadow"] = not self._gpio_enabled
        self._line(self.shadow, record)
        self.shadow_records += 1
        if assessment.fusion_update == "applied":
            self.fusion_updates_applied += 1
        elif assessment.fusion_update == "coalesced":
            self.fusion_updates_coalesced += 1
        if assessment.availability.value == "radar_time_discontinuity":
            self.time_discontinuities += 1

    def event(self, kind: str, detail: str, timestamp: float) -> None:
        self.write_event_record(
            {"kind": kind, "detail": detail, "timestamp_seconds": timestamp}
        )

    def operator_event(self, detail: str, timestamp: float, elapsed: float) -> None:
        """Durably save one complete operator line before acknowledging it."""
        self.write_event_record(
            {
                "kind": "operator",
                "detail": detail,
                "source": "operator_stdin_complete_line",
                "timestamp_seconds": timestamp,
                "elapsed_seconds": elapsed,
            },
            durable=True,
        )

    def write_event_record(
        self, record: dict[str, object], *, durable: bool = False
    ) -> None:
        self._line(self.events, record)
        self.events.flush()
        if durable:
            os.fsync(self.events.fileno())

    def write_led(self, record: dict[str, object]) -> None:
        if self.led is None:
            raise RuntimeError("LED recording was not enabled for this capture")
        self._line(self.led, record)
        self.led_records += 1

    def close(self) -> list[str]:
        errors: list[str] = []
        files = [
            self.raw_file,
            self.decoded,
            self.motion,
            self.shadow,
            self.events,
            self.led,
        ]
        for output in files:
            if output is None:
                continue
            try:
                output.flush()
                os.fsync(output.fileno())
            except OSError as exc:
                errors.append(f"{Path(output.name).name} flush: {exc}")
            try:
                output.close()
            except OSError as exc:
                errors.append(f"{Path(output.name).name} close: {exc}")
        return errors


class _OperatorPhaseGuide:
    """Emit automatic T4 prompts without creating operator-event records."""

    def __init__(self) -> None:
        self._data_prompted = False
        self._motion_prompted = False
        self._motion_end_prompted = False

    def prompts(self, *, elapsed: float, real_data_seen: bool) -> tuple[str, ...]:
        messages: list[str] = []
        if real_data_seen and not self._data_prompted:
            self._data_prompted = True
            messages.append(
                "請逐行輸入 static_start；若開始 LED 診斷，再輸入 "
                "led_test_start。這是自動提示，尚未保存任何 operator event。"
            )
        if elapsed >= 80.0 and not self._motion_prompted:
            self._motion_prompted = True
            messages.append(
                "請輸入 motion_start 並等待『已保存』後才開始移動。"
                "這是自動提示，尚未保存 operator event。"
            )
        if elapsed >= 100.0 and not self._motion_end_prompted:
            self._motion_end_prompted = True
            messages.append(
                "請先站定，再輸入 motion_end 並確認『已保存』。"
                "這是自動提示，尚未保存 operator event。"
            )
        return tuple(messages)


def _allocate(root: Path, prefix: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    stamp = _WALL_CLOCK().strftime("%Y%m%dT%H%M%S.%f%z")
    for number in range(1000):
        path = root / (f"{prefix}-{stamp}" if number == 0 else f"{prefix}-{stamp}-{number:03d}")
        try:
            path.mkdir(exist_ok=False)
            return path
        except FileExistsError:
            pass
    raise FileExistsError(f"無法在 {root} 建立唯一輸出目錄")


def _git() -> dict[str, object]:
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=False, timeout=2)
        dirty = subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                               text=True, check=False, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None}
    return {"head": head.stdout.strip() if head.returncode == 0 else None,
            "dirty": bool(dirty.stdout) if dirty.returncode == 0 else None}


def _motion_record(value: BNO055Measurement) -> dict[str, object]:
    return {
        "monotonic_timestamp": value.monotonic_timestamp,
        "euler": {"heading": value.euler.heading, "roll": value.euler.roll,
                  "pitch": value.euler.pitch},
        "quaternion": {"w": value.quaternion.w, "x": value.quaternion.x,
                       "y": value.quaternion.y, "z": value.quaternion.z},
        "linear_acceleration_mps2": {"x": value.linear_acceleration.x,
                                     "y": value.linear_acceleration.y,
                                     "z": value.linear_acceleration.z},
        "gravity_mps2": {"x": value.gravity.x, "y": value.gravity.y,
                         "z": value.gravity.z},
        "temperature_c": value.temperature_c,
        "calibration": {"system": value.calibration.system,
                        "gyroscope": value.calibration.gyroscope,
                        "accelerometer": value.calibration.accelerometer,
                        "magnetometer": value.calibration.magnetometer},
        "operation_mode": value.operation_mode,
        "system_status": value.system_status,
        "system_error": value.system_error,
    }


def _save_json(path: Path, value: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def _format(
    assessment: ShadowAssessment | None,
    *,
    elapsed: float,
    gpio_enabled: bool = False,
) -> str:
    mode = "RADAR_LED" if gpio_enabled else "SHADOW"
    gpio = "enabled" if gpio_enabled else "disabled"
    if assessment is None:
        return f"[{mode} t={elapsed:.2f}s GPIO={gpio}] 等待完整雷達週期"
    target = None if assessment.selection is None else assessment.selection.selected_target
    distance = "unavailable" if target is None else f"{target.distance_m:.2f}m(ID={target.target_id})"
    sync = "unavailable" if assessment.motion_match is None else assessment.motion_match.status.value
    fusion = "unavailable" if assessment.fusion is None else assessment.fusion.classification.value
    warning = "unavailable" if assessment.warning_candidate is None else assessment.warning_candidate.value
    stabilized = (
        "unavailable"
        if assessment.warning_stabilized is None
        else assessment.warning_stabilized.value
    )
    return (f"[{mode} t={elapsed:.2f}s GPIO={gpio}] availability={assessment.availability.value} "
            f"distance={distance} sync={sync} fusion={fusion} "
            f"distance_warning_candidate={warning} "
            f"distance_warning_stabilized={stabilized} reason={assessment.reason}")


def _panel(assessment: ShadowAssessment | None, *, now: float,
           started: float, terminal: BoundedTerminal,
           gpio_enabled: bool = False) -> PanelSnapshot:
    common = {"elapsed": max(0.0, now - started),
              "dropped_status": terminal.dropped_status,
              "dropped_events": terminal.dropped_events}
    if assessment is None:
        state = "RADAR LED 等待資料" if gpio_enabled else "SHADOW 等待資料"
        reason = (
            "GPIO 已啟用但保持全關；等待完整雷達週期"
            if gpio_enabled
            else "不控制實體警示；等待完整雷達週期"
        )
        return PanelSnapshot(**common, state=state, reason=reason)
    target = None if assessment.selection is None else assessment.selection.selected_target
    fusion = assessment.fusion
    match = assessment.motion_match
    distance = "unavailable" if target is None else f"{target.distance_m:.2f} 公尺 / ID {target.target_id}"
    rate = "資料不足"
    classification = "不可用"
    candidate = "無"
    translation = rotation = baseline = "資料不足"
    if fusion is not None:
        if fusion.distance_trend.closing_rate_mps is not None:
            rate = f"{fusion.distance_trend.closing_rate_mps:.2f} 公尺/秒"
        classification = fusion.classification.value
        candidate = ("無" if fusion.candidate_classification is None
                     else fusion.candidate_classification.value)
        translation = str(fusion.own_motion_evidence.translational_motion_detected)
        rotation = str(fusion.own_motion_evidence.rotational_motion_detected)
        baseline = str(fusion.stationary_baseline_ready)
    warning = ("unavailable" if assessment.warning_candidate is None
               else assessment.warning_candidate.value)
    stabilized = (
        "unavailable"
        if assessment.warning_stabilized is None
        else assessment.warning_stabilized.value
    )
    mode = "RADAR LED／GPIO enabled" if gpio_enabled else "SHADOW／GPIO disabled"
    reason = (f"{mode}；{assessment.reason}；距離警示候選={warning}；"
              f"穩定結果={stabilized}")
    return PanelSnapshot(
        **common,
        distance=distance,
        rate=rate,
        state=assessment.availability.value,
        classification=classification,
        candidate=candidate,
        reason=reason,
        translation=translation,
        rotation=rotation,
        baseline=baseline,
        health="unavailable" if match is None else match.status.value,
        distance_age=f"{max(0.0, now - assessment.timestamp_seconds):.3f} 秒",
        motion_age=("尚無" if match is None or match.motion_timestamp is None
                    else f"{max(0.0, now - match.motion_timestamp):.3f} 秒"),
        fault=assessment.availability.value not in ("live_selected", "fusion_warmup"),
    )


@dataclass(frozen=True, slots=True)
class _ReplayBatch:
    frame_bytes: tuple[bytes, ...]
    timestamp: float
    sequence: int


@dataclass(frozen=True, slots=True)
class _ReplayMotion:
    measurement: BNO055Measurement
    canonical_timestamp: float


@dataclass(frozen=True, slots=True)
class _ReplayOperatorEvent:
    record: dict[str, object]
    timestamp: float


def _finite_number(value: object, *, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{location} 必須是有限數值")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{location} 必須是有限數值")
    return parsed


def _source_records(
    capture: Path,
) -> tuple[bytes, list[_ReplayBatch], str, str]:
    raw_path = capture / "raw.bin" if capture.is_dir() else capture
    raw = raw_path.read_bytes()
    decoded_path = raw_path.with_name("decoded.jsonl")
    records: list[tuple[bytes, float, int | None]] = []
    timing_source = "saved_host_receipt_elapsed"
    if decoded_path.is_file():
        for line_number, line in enumerate(
            decoded_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"decoded.jsonl:{line_number} 必須是 JSON 物件")
            raw_hex = value.get("raw_hex")
            timestamp_value = value.get("received_at_monotonic_seconds")
            if timestamp_value is not None:
                timing_source = "saved_host_receipt_monotonic"
            else:
                timestamp_value = value.get("elapsed_seconds")
            if not isinstance(raw_hex, str) or timestamp_value is None:
                raise ValueError(
                    f"decoded.jsonl:{line_number} 缺少 raw_hex/接收時間"
                )
            try:
                frame_bytes = bytes.fromhex(raw_hex)
            except ValueError as exc:
                raise ValueError(
                    f"decoded.jsonl:{line_number} raw_hex 無效"
                ) from exc
            timestamp = _finite_number(
                timestamp_value,
                location=f"decoded.jsonl:{line_number} 接收時間",
            )
            sequence = value.get("receipt_batch_sequence")
            if sequence is not None and (
                isinstance(sequence, bool) or not isinstance(sequence, int)
            ):
                raise ValueError(
                    f"decoded.jsonl:{line_number} receipt_batch_sequence 必須是整數"
                )
            records.append((frame_bytes, timestamp, sequence))

        if not records:
            return raw, [], timing_source, "no_decoded_frames"
        has_sequences = [record[2] is not None for record in records]
        if any(has_sequences) and not all(has_sequences):
            raise ValueError("decoded.jsonl 的 receipt_batch_sequence 不可部分缺漏")
        batches: list[_ReplayBatch] = []
        if all(has_sequences):
            batching_source = "saved_receipt_batch_sequence"
            for frame_bytes, timestamp, sequence_value in records:
                assert sequence_value is not None
                if sequence_value <= 0:
                    raise ValueError("receipt_batch_sequence 必須是正整數")
                if batches and sequence_value < batches[-1].sequence:
                    raise ValueError("receipt_batch_sequence 不可倒退")
                if batches and sequence_value == batches[-1].sequence:
                    if timestamp != batches[-1].timestamp:
                        raise ValueError("同一 receipt batch 的接收時間不一致")
                    previous = batches[-1]
                    batches[-1] = _ReplayBatch(
                        previous.frame_bytes + (frame_bytes,),
                        timestamp,
                        sequence_value,
                    )
                else:
                    batches.append(
                        _ReplayBatch((frame_bytes,), timestamp, sequence_value)
                    )
        else:
            batching_source = "inferred_from_consecutive_equal_receipt_timestamp"
            for frame_bytes, timestamp, _sequence in records:
                if batches and timestamp == batches[-1].timestamp:
                    previous = batches[-1]
                    batches[-1] = _ReplayBatch(
                        previous.frame_bytes + (frame_bytes,),
                        timestamp,
                        previous.sequence,
                    )
                else:
                    batches.append(
                        _ReplayBatch((frame_bytes,), timestamp, len(batches) + 1)
                    )
        return raw, batches, timing_source, batching_source

    # Byte position cannot recover measurement time. This deterministic clock
    # is explicitly labelled synthetic in metadata and terminal output.
    parser_core = IoRadar701ShadowCore()
    frames = parser_core.parser.feed(raw)
    batches = [
        _ReplayBatch((frame.raw,), index * 0.05, index + 1)
        for index, frame in enumerate(frames)
    ]
    return (
        raw,
        batches,
        "synthetic_0.05_seconds_per_valid_frame",
        "synthetic_one_valid_frame_per_batch",
    )


def _mapping(value: object, *, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} 必須是物件")
    return value


def _integer(value: object, *, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{location} 必須是整數")
    return value


def _measurement_from_record(
    value: dict[str, object], *, line_number: int
) -> _ReplayMotion:
    prefix = f"bno055.jsonl:{line_number}"
    euler = _mapping(value.get("euler"), location=f"{prefix}.euler")
    quaternion = _mapping(
        value.get("quaternion"), location=f"{prefix}.quaternion"
    )
    acceleration = _mapping(
        value.get("linear_acceleration_mps2"),
        location=f"{prefix}.linear_acceleration_mps2",
    )
    gravity = _mapping(
        value.get("gravity_mps2"), location=f"{prefix}.gravity_mps2"
    )
    calibration = _mapping(
        value.get("calibration"), location=f"{prefix}.calibration"
    )
    measurement = BNO055Measurement(
        _finite_number(
            value.get("monotonic_timestamp"),
            location=f"{prefix}.monotonic_timestamp",
        ),
        EulerAngles(
            _finite_number(euler.get("heading"), location=f"{prefix}.euler.heading"),
            _finite_number(euler.get("roll"), location=f"{prefix}.euler.roll"),
            _finite_number(euler.get("pitch"), location=f"{prefix}.euler.pitch"),
        ),
        Quaternion(
            _finite_number(quaternion.get("w"), location=f"{prefix}.quaternion.w"),
            _finite_number(quaternion.get("x"), location=f"{prefix}.quaternion.x"),
            _finite_number(quaternion.get("y"), location=f"{prefix}.quaternion.y"),
            _finite_number(quaternion.get("z"), location=f"{prefix}.quaternion.z"),
        ),
        Vector3(
            _finite_number(
                acceleration.get("x"), location=f"{prefix}.linear_acceleration_mps2.x"
            ),
            _finite_number(
                acceleration.get("y"), location=f"{prefix}.linear_acceleration_mps2.y"
            ),
            _finite_number(
                acceleration.get("z"), location=f"{prefix}.linear_acceleration_mps2.z"
            ),
        ),
        Vector3(
            _finite_number(gravity.get("x"), location=f"{prefix}.gravity_mps2.x"),
            _finite_number(gravity.get("y"), location=f"{prefix}.gravity_mps2.y"),
            _finite_number(gravity.get("z"), location=f"{prefix}.gravity_mps2.z"),
        ),
        _integer(value.get("temperature_c"), location=f"{prefix}.temperature_c"),
        BNO055Calibration(
            _integer(
                calibration.get("system"), location=f"{prefix}.calibration.system"
            ),
            _integer(
                calibration.get("gyroscope"),
                location=f"{prefix}.calibration.gyroscope",
            ),
            _integer(
                calibration.get("accelerometer"),
                location=f"{prefix}.calibration.accelerometer",
            ),
            _integer(
                calibration.get("magnetometer"),
                location=f"{prefix}.calibration.magnetometer",
            ),
        ),
        _integer(value.get("operation_mode"), location=f"{prefix}.operation_mode"),
        _integer(value.get("system_status"), location=f"{prefix}.system_status"),
        _integer(value.get("system_error"), location=f"{prefix}.system_error"),
    )
    canonical = _finite_number(
        value.get("canonical_captured_at_monotonic_seconds"),
        location=f"{prefix}.canonical_captured_at_monotonic_seconds",
    )
    return _ReplayMotion(measurement, canonical)


def _source_motion(capture: Path) -> list[_ReplayMotion]:
    base = capture if capture.is_dir() else capture.parent
    path = base / "bno055.jsonl"
    if not path.is_file():
        return []
    records: list[_ReplayMotion] = []
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"bno055.jsonl:{line_number} 必須是 JSON 物件")
        record = _measurement_from_record(value, line_number=line_number)
        if records and record.canonical_timestamp < records[-1].canonical_timestamp:
            raise ValueError("bno055.jsonl canonical timestamp 不可倒退")
        records.append(record)
    return records


def _source_operator_events(
    capture: Path,
) -> tuple[list[_ReplayOperatorEvent], str]:
    base = capture if capture.is_dir() else capture.parent
    path = base / "events.jsonl"
    if not path.is_file():
        return [], "unavailable"
    records: list[_ReplayOperatorEvent] = []
    timing_sources: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"events.jsonl:{line_number} 必須是 JSON 物件")
        if value.get("kind") != "operator":
            continue
        detail = value.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            raise ValueError(f"events.jsonl:{line_number} operator detail 必須是非空字串")
        if "timestamp_seconds" in value:
            timestamp_key = "timestamp_seconds"
            timing_sources.add("saved_host_monotonic")
        else:
            timestamp_key = "elapsed_seconds"
            timing_sources.add("saved_host_elapsed")
        timestamp = _finite_number(
            value.get(timestamp_key),
            location=f"events.jsonl:{line_number}.{timestamp_key}",
        )
        if timestamp < 0:
            raise ValueError(f"events.jsonl:{line_number}.{timestamp_key} 不可為負數")
        if records and timestamp < records[-1].timestamp:
            raise ValueError("events.jsonl operator timestamp 不可倒退")
        records.append(_ReplayOperatorEvent(dict(value), timestamp))
    if not records:
        return [], "unavailable"
    return records, (
        next(iter(timing_sources))
        if len(timing_sources) == 1
        else "mixed_saved_host_monotonic_and_elapsed"
    )


def replay(options: argparse.Namespace) -> int:
    capture = Path(options.capture)
    try:
        raw, batches, timing_source, batching_source = _source_records(capture)
        motion_records = _source_motion(capture)
        operator_events, operator_event_source = _source_operator_events(capture)
        directory = _allocate(Path(options.output_root), "replay")
        output = _Capture(directory, raw=False)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"replay_error: {exc}", file=sys.stderr)
        return 1
    core = IoRadar701ShadowCore(stale_after_seconds=options.stale_after)
    cursor = 0
    motion_index = 0
    operator_event_index = 0
    error: str | None = None

    def apply_motion(record: _ReplayMotion) -> None:
        output.write_motion(record.measurement, record.canonical_timestamp)
        try:
            core.update_motion(
                record.measurement,
                captured_at_monotonic_seconds=record.canonical_timestamp,
            )
        except InvalidMotionMeasurement as exc:
            output.event("invalid_motion", str(exc), record.canonical_timestamp)

    def apply_timeline(until: float) -> None:
        nonlocal motion_index, operator_event_index
        while True:
            motion_timestamp = (
                motion_records[motion_index].canonical_timestamp
                if motion_index < len(motion_records)
                else math.inf
            )
            event_timestamp = (
                operator_events[operator_event_index].timestamp
                if operator_event_index < len(operator_events)
                else math.inf
            )
            next_timestamp = min(motion_timestamp, event_timestamp)
            if next_timestamp == math.inf:
                return
            if next_timestamp > until:
                return
            if motion_timestamp <= event_timestamp:
                apply_motion(motion_records[motion_index])
                motion_index += 1
            else:
                output.write_event_record(
                    operator_events[operator_event_index].record
                )
                operator_event_index += 1

    try:
        for source_batch in batches:
            apply_timeline(source_batch.timestamp)
            batch_start = cursor
            for frame_bytes in source_batch.frame_bytes:
                offset = raw.find(frame_bytes, cursor)
                if offset < 0:
                    raise ValueError("decoded frame sequence does not match raw.bin")
                cursor = offset + len(frame_bytes)
            result = core.feed_bytes(
                raw[batch_start:cursor],
                source_batch.timestamp,
                receipt_batch_sequence=source_batch.sequence,
            )
            for frame in result.frames:
                output.write_frame(
                    frame, source_batch.timestamp, source_batch.sequence
                )
            for assessment in result.assessments:
                output.write_assessment(assessment)
                if options.plain:
                    print(_format(assessment, elapsed=source_batch.timestamp))
        if cursor < len(raw):
            last_timestamp = batches[-1].timestamp if batches else 0.0
            last_sequence = batches[-1].sequence if batches else 0
            core.feed_bytes(
                raw[cursor:],
                last_timestamp,
                receipt_batch_sequence=last_sequence + 1,
            )
        apply_timeline(math.inf)
    except (OSError, ValueError) as exc:
        error = str(exc)
    close_errors = output.close()
    complete = error is None and not close_errors
    metadata = {
        "schema_version": 2,
        "mode": "REPLAY",
        "label": (
            "REPLAY／真實雷達＋保存 BNO055"
            if motion_records
            else "REPLAY／真實雷達＋無 BNO055 記錄"
            if timing_source.startswith("saved")
            else "REPLAY／真實雷達＋合成時間＋無 BNO055 記錄"
        ),
        "source": str(capture),
        "timing_source": timing_source,
        "batching_source": batching_source,
        "batching_note": (
            "consecutive equal saved timestamps are an inference, not proof of the "
            "original transport callback boundary"
            if batching_source == "inferred_from_consecutive_equal_receipt_timestamp"
            else "receipt batch identity was preserved by the source capture"
        ),
        "motion_source": (
            "saved_bno055_canonical_host_monotonic"
            if motion_records else "unavailable"
        ),
        "operator_event_source": operator_event_source,
        "selector_rule": RadarTargetSelector.RULE,
        "shadow": True,
        "gpio_enabled": False,
        "stale_after_seconds": options.stale_after,
        "git": _git(),
        "recording_complete": complete,
        "errors": [item for item in ([error] if error else []) + close_errors],
        "counts": {
            "raw_bytes": len(raw),
            "receipt_batches": len(batches),
            "decoded_frames": output.frames,
            "bno055_records": output.motion_records,
            "shadow_records": output.shadow_records,
            "fusion_updates_applied": output.fusion_updates_applied,
            "fusion_updates_coalesced": output.fusion_updates_coalesced,
            "radar_time_discontinuities": output.time_discontinuities,
        },
    }
    try:
        _save_json(directory / "metadata.json", metadata)
        _save_json(directory / "summary.json", metadata["counts"] | {
            "recording_complete": complete,
            "parser": asdict(core.parser.statistics),
        })
    except OSError as exc:
        print(f"replay_error: metadata save failed: {exc}", file=sys.stderr)
        return 1
    print(metadata["label"])
    print(f"output_directory={directory}")
    print(
        f"frames={output.frames} shadow_records={output.shadow_records} "
        f"fusion_applied={output.fusion_updates_applied} "
        f"fusion_coalesced={output.fusion_updates_coalesced} "
        f"recording_complete={str(complete).lower()}"
    )
    if error:
        print(f"replay_error: {error}", file=sys.stderr)
    return 0 if complete else 1


def _run_live(
    options: argparse.Namespace,
    *,
    observer: _LiveObserver | None = None,
    mode: str = "LIVE",
    label: str = "SHADOW／不控制實體警示",
    gpio_enabled: bool = False,
    finish_message: str = "[SHADOW 結束] 已停止；從未控制 GPIO／LED",
    t4_phase_prompts: bool = True,
    capture_prefix: str = "live",
    segment_seconds: float | None = None,
    on_capture_created: Callable[[Path], None] | None = None,
    on_capture_closed: Callable[[Path], None] | None = None,
    operator_input: bool = True,
) -> int:
    """Run the single-owner UART/I2C loop.

    ``segment_seconds`` (opt-in, used by the unattended service) rotates the
    capture directory without reopening sensors or GPIO: the previous segment
    is closed and gets its own metadata/summary, and ``on_capture_closed`` is
    called so the caller can bound total disk usage. ``None`` keeps the
    original single-directory behaviour unchanged.
    """
    if segment_seconds is not None and (
        not math.isfinite(segment_seconds) or segment_seconds <= 0
    ):
        raise ValueError("segment_seconds must be a finite positive number")
    started_wall = _WALL_CLOCK()
    started = _CLOCK()
    segment_started_wall = started_wall
    segment_started = started
    segment_index = 0
    directory: Path | None = None
    output: _Capture | None = None
    serial_device: RadarSerial | None = None
    register_io: RegisterIO | None = None
    motion_device: MotionDevice | None = None
    terminal: BoundedTerminal | None = None
    errors: list[str] = []
    cleanup_errors: list[str] = []
    interrupted = False
    radar_batch_sequence = 0
    bno_initialization: tuple[int, int, int] | None = None
    last_motion: BNO055Measurement | None = None
    real_radar_data_seen = False
    phase_guide = _OperatorPhaseGuide() if t4_phase_prompts else None
    core = IoRadar701ShadowCore(
        stale_after_seconds=options.stale_after,
        maximum_motion_age_seconds=options.maximum_motion_age,
    )

    def _finalize_capture(
        target: Path,
        capture: _Capture | None,
        *,
        started_wall: datetime,
        elapsed: float,
        interrupted: bool,
        errors: list[str],
        cleanup_errors: list[str],
        segment: dict[str, object] | None,
    ) -> list[str]:
        """Write metadata/summary for one capture directory; return save errors."""
        complete = capture is not None and not errors and not cleanup_errors
        metadata: dict[str, object] = {
            "schema_version": 2, "mode": mode, "label": label,
            "started_at": started_wall.isoformat(timespec="microseconds"),
            "elapsed_seconds": elapsed, "port": options.port,
            "baudrate": options.baudrate, "serial_format": "8N1",
            "flow_control": False, "receive_only": True,
            "i2c_bus": options.i2c_bus, "bno055_address": options.bno055_address,
            "bno055_initialization": None if bno_initialization is None else {
                "operation_mode": bno_initialization[0],
                "system_status": bno_initialization[1],
                "system_error": bno_initialization[2],
            },
            "bno055_last_measurement": None if last_motion is None else
            _motion_record(last_motion),
            "bno055_canonical_timestamp": "host monotonic immediately after successful read",
            "bno055_raw_measurement_timestamp":
            "BNO055Measurement.monotonic_timestamp before measurement block read",
            "bno055_calibration_profile": {
                "loaded": False,
                "source": None,
                "reason": "shadow live initializes NDOF without profile restore",
            },
            "radar_timestamp": "host monotonic time immediately after one UART read",
            "receipt_batch_definition": "one non-empty UART read callback",
            "operator_event_command": ":event <detail>",
            "operator_event_short_commands": sorted(_SHORT_OPERATOR_EVENTS),
            "operator_event_timestamp":
            "host monotonic immediately after the complete stdin line is received",
            "operator_event_acknowledgement":
            "shown only after events.jsonl flush and fsync succeed",
            "selector_rule": RadarTargetSelector.RULE,
            "maximum_motion_age_seconds": options.maximum_motion_age,
            "stale_after_seconds": options.stale_after,
            "refresh_hz": options.refresh_hz, "requested_duration_seconds": options.duration,
            "distance_thresholds_m": asdict(core.policy.thresholds),
            "hysteresis_thresholds_m": asdict(HysteresisThresholds()),
            "radar_beyond_maximum_valid_distance": "safe (no target within warning range)",
            "voice_enabled": None if observer is None else getattr(observer, "voice_enabled", None),
            "vehicle_motion_thresholds": asdict(core.vehicle_motion.thresholds),
            "vehicle_motion_policy": (
                None if observer is None
                else getattr(getattr(observer, "vehicle_motion_policy", None), "value", None)
            ),
            "shadow": not gpio_enabled, "gpio_enabled": gpio_enabled, "git": _git(),
            "interrupted": interrupted, "recording_complete": complete,
            "errors": errors, "cleanup_errors": cleanup_errors,
            "led_cleanup": (
                None if observer is None else observer.cleanup_status()
            ),
            "counts": {} if capture is None else {
                "raw_bytes": capture.raw_bytes, "decoded_frames": capture.frames,
                "receipt_batches": radar_batch_sequence,
                "bno055_records": capture.motion_records,
                "shadow_records": capture.shadow_records,
                "fusion_updates_applied": capture.fusion_updates_applied,
                "fusion_updates_coalesced": capture.fusion_updates_coalesced,
                "radar_time_discontinuities": capture.time_discontinuities,
                "led_records": capture.led_records,
            },
            "parser": asdict(core.parser.statistics),
        }
        if segment is not None:
            metadata["segment"] = segment | {
                "run_started_at": started_wall_of_run.isoformat(
                    timespec="microseconds"
                ),
                "cumulative_since_run_start": [
                    "parser", "counts.receipt_batches"
                ],
            }
        save_errors: list[str] = []
        try:
            _save_json(target / "metadata.json", metadata)
            counts = metadata["counts"]
            assert isinstance(counts, dict)
            _save_json(target / "summary.json", counts | {
                "recording_complete": complete,
                "interrupted": interrupted,
                "errors": errors,
                "cleanup_errors": cleanup_errors,
                "led_cleanup": (
                    None if observer is None else observer.cleanup_status()
                ),
                "parser": asdict(core.parser.statistics),
            })
        except OSError as exc:
            save_errors.append(f"metadata save: {exc}")
        print(f"capture_directory={target}", flush=True)
        return save_errors

    started_wall_of_run = started_wall
    try:
        _PREFLIGHT(options.port, options.i2c_bus)
        directory = _allocate(Path(options.output_root), capture_prefix)
        output = _Capture(directory, raw=True, gpio_enabled=gpio_enabled)
        if on_capture_created is not None:
            on_capture_created(directory)
        terminal = _TERMINAL_FACTORY(sys.stdout.fileno(), monotonic=_CLOCK,
                                     plain=options.plain,
                                     label=label)
        if observer is not None:
            observer.start(output, _CLOCK())
        serial_device = _SERIAL_FACTORY(options.port, options.baudrate, options.uart_timeout)
        register_io = _REGISTER_FACTORY(options.i2c_bus)
        motion_device = _MOTION_FACTORY(register_io, address=options.bno055_address,
                                        monotonic=_CLOCK, sleep=_SLEEP)
        motion_device.read_identity()
        bno_initialization = motion_device.initialize_ndof()
        next_motion = _CLOCK()
        next_refresh = next_motion
        deadline = None if options.duration is None else started + options.duration
        while deadline is None or _CLOCK() < deadline:
            now = _CLOCK()
            if now >= next_motion:
                measurement = motion_device.read_measurement()
                canonical_motion_timestamp = _CLOCK()
                output.write_motion(measurement, canonical_motion_timestamp)
                last_motion = measurement
                try:
                    core.update_motion(
                        measurement,
                        captured_at_monotonic_seconds=canonical_motion_timestamp,
                    )
                except InvalidMotionMeasurement as exc:
                    output.event("invalid_motion", str(exc), canonical_motion_timestamp)
                next_motion = now + options.motion_period
            try:
                chunk = serial_device.read(4096)
            except Exception as exc:
                failed_at = _CLOCK()
                failed = core.set_radar_error(
                    f"UART read failed: {type(exc).__name__}: {exc}", failed_at
                )
                output.write_assessment(failed)
                if observer is not None:
                    observer.observe(failed, output)
                raise
            now = _CLOCK()
            if chunk:
                radar_batch_sequence += 1
                output.write_raw(chunk)
                batch = core.feed_bytes(
                    chunk,
                    now,
                    receipt_batch_sequence=radar_batch_sequence,
                )
                for frame in batch.frames:
                    output.write_frame(frame, now, radar_batch_sequence)
                real_radar_data_seen = real_radar_data_seen or bool(batch.frames)
                for assessment in batch.assessments:
                    output.write_assessment(assessment)
                    if observer is not None:
                        observer.observe(assessment, output)
            operator_line = (
                _OPERATOR_LINE_POLLER(sys.stdin) if operator_input else None
            )
            if operator_line is not None:
                operator_detail = _operator_event_detail(operator_line)
                received_at = _CLOCK()
                event_elapsed = max(0.0, received_at - started)
                if operator_detail is not None:
                    output.operator_event(
                        operator_detail,
                        received_at,
                        event_elapsed,
                    )
                    terminal.publish(
                        f"[操作員事件 t={event_elapsed:.3f}s] 已接收並保存："
                        f"{operator_detail}",
                        received_at,
                        event=True,
                    )
                else:
                    terminal.publish(
                        f"[操作員輸入 t={event_elapsed:.3f}s] 未保存："
                        "請輸入已列出的短指令，或 :event <名稱>",
                        received_at,
                        event=True,
                    )
            expired = core.tick(now)
            if expired is not None:
                output.write_assessment(expired)
                if observer is not None:
                    observer.observe(expired, output)
            elapsed_now = max(0.0, now - started)
            if phase_guide is not None:
                for prompt in phase_guide.prompts(
                    elapsed=elapsed_now,
                    real_data_seen=real_radar_data_seen,
                ):
                    terminal.publish(
                        f"[自動提示 t={elapsed_now:.3f}s] {prompt}",
                        now,
                        event=True,
                    )
            if now >= next_refresh:
                terminal.display(
                    _panel(
                        core.latest,
                        now=now,
                        started=started,
                        terminal=terminal,
                        gpio_enabled=gpio_enabled,
                    ),
                    _format(
                        core.latest,
                        elapsed=now - started,
                        gpio_enabled=gpio_enabled,
                    ),
                    now,
                )
                next_refresh = now + 1.0 / options.refresh_hz
            terminal.flush_once(now)
            if (
                segment_seconds is not None
                and now - segment_started >= segment_seconds
            ):
                # Open the next segment first so a failure leaves the current
                # capture as the one finalized by the outer cleanup path.
                next_directory = _allocate(Path(options.output_root), capture_prefix)
                next_output = _Capture(
                    next_directory, raw=True, gpio_enabled=gpio_enabled
                )
                if on_capture_created is not None:
                    on_capture_created(next_directory)
                closed_directory, closed_output = directory, output
                closed_started_wall = segment_started_wall
                closed_elapsed = max(0.0, now - segment_started)
                closed_index = segment_index
                directory, output = next_directory, next_output
                segment_index += 1
                segment_started = now
                segment_started_wall = _WALL_CLOCK()
                close_errors = closed_output.close()
                save_errors = _finalize_capture(
                    closed_directory,
                    closed_output,
                    started_wall=closed_started_wall,
                    elapsed=closed_elapsed,
                    interrupted=False,
                    errors=[],
                    cleanup_errors=close_errors,
                    segment={
                        "index": closed_index,
                        "final": False,
                        "segment_seconds": segment_seconds,
                    },
                )
                if close_errors or save_errors:
                    errors.extend(
                        f"segment {closed_directory.name}: {item}"
                        for item in close_errors + save_errors
                    )
                if on_capture_closed is not None:
                    on_capture_closed(closed_directory)
    except KeyboardInterrupt:
        interrupted = True
    except BaseException as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    finally:
        for name, resource in (
            ("UART", serial_device),
            ("BNO055", motion_device if motion_device is not None else register_io),
        ):
            if resource is not None:
                try:
                    resource.close()
                except BaseException as exc:
                    cleanup_errors.append(f"{name}: {type(exc).__name__}: {exc}")
        if observer is not None and output is not None:
            try:
                observer.finish(output, _CLOCK())
            except BaseException as exc:
                cleanup_errors.append(f"GPIO: {type(exc).__name__}: {exc}")
        if output is not None:
            cleanup_errors.extend(output.close())
        if terminal is not None:
            try:
                terminal.finish(finish_message)
            except (OSError, ValueError) as exc:
                cleanup_errors.append(f"terminal finish: {exc}")
            try:
                terminal.close()
            except OSError as exc:
                cleanup_errors.append(f"terminal close: {exc}")
    if segment_seconds is None:
        final_started_wall, elapsed = started_wall, max(0.0, _CLOCK() - started)
        final_segment: dict[str, object] | None = None
    else:
        final_started_wall = segment_started_wall
        elapsed = max(0.0, _CLOCK() - segment_started)
        final_segment = {
            "index": segment_index,
            "final": True,
            "segment_seconds": segment_seconds,
        }
    complete = output is not None and not errors and not cleanup_errors
    if directory is not None:
        save_errors = _finalize_capture(
            directory,
            output,
            started_wall=final_started_wall,
            elapsed=elapsed,
            interrupted=interrupted,
            errors=errors,
            cleanup_errors=cleanup_errors,
            segment=final_segment,
        )
        if save_errors:
            errors.extend(save_errors)
            complete = False
        if on_capture_closed is not None and segment_seconds is not None:
            on_capture_closed(directory)
    for item in errors + cleanup_errors:
        print(f"shadow_error: {item}", file=sys.stderr)
    return 130 if interrupted and complete else 0 if complete else 1


def live(options: argparse.Namespace) -> int:
    print(
        "ioRadar701＋BNO055 SHADOW／不控制實體警示\n"
        f"UART={options.port} {options.baudrate}/8N1 receive-only；"
        f"I²C bus={options.i2c_bus} address={options.bno055_address:#04x}\n"
        "BNO055 calibration profile=未載入；本入口直接初始化 NDOF。\n"
        "Enter 後才檢查並開啟 UART／I²C；只開一個 UART reader；不建立 GPIO／LED adapter。\n"
        "執行中每次只輸入一行並按 Enter；可直接輸入 static_start、"
        "led_test_start、led_test_end、motion_start、motion_end，"
        "或使用 `:event <名稱>`。\n"
        "只有看到『已接收並保存』才代表 operator event 已 flush＋fsync；"
        "80／100 秒提示不會自動建立事件。\n"
        "風險：會占用 UART 與 I²C，並將 BNO055 切入 NDOF；結束時嘗試回 CONFIG mode。",
        flush=True,
    )
    if not options.confirm_hardware:
        print("尚未確認硬體：缺少 --confirm-hardware；零硬體 I/O。", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("需要互動終端機；拒絕 pipe／EOF 自動啟動，零硬體 I/O。", file=sys.stderr)
        return 2
    try:
        print("按 Enter 開始 SHADOW，Ctrl+C 停止", flush=True)
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        return 130
    if answer not in ("\n", "\r\n"):
        print("已取消；零硬體 I/O。")
        return 2
    return _run_live(options)


def main(argv: Sequence[str] | None = None) -> int:
    options = parser().parse_args(None if argv is None else list(argv))
    if options.command == "replay":
        return replay(options)
    if options.baudrate <= 0 or options.i2c_bus < 0 or not options.port.strip():
        parser().error("UART／I²C 參數無效")
    if options.refresh_hz > 20 or options.uart_timeout > options.motion_period:
        parser().error("refresh-hz 不得超過 20，UART timeout 不得超過 motion-period")
    return live(options)


if __name__ == "__main__":
    raise SystemExit(main())
