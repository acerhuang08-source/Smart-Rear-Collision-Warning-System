from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rear_warning.cli import ioradar701_shadow
from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)


class Stream:
    def __init__(self, *, tty: bool, line: str = "") -> None:
        self.tty = tty
        self.line = line
        self.text = ""

    def isatty(self) -> bool:
        return self.tty

    def readline(self) -> str:
        return self.line

    def write(self, value: str) -> int:
        self.text += value
        return len(value)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return 1


@pytest.mark.parametrize(
    "argv",
    [
        ["live"],
        ["live", "--confirm-hardware"],
    ],
)
def test_live_gate_performs_zero_hardware_io(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    stdin = Stream(tty=False)
    stdout = Stream(tty=False)
    calls: list[str] = []
    monkeypatch.setattr(ioradar701_shadow.sys, "stdin", stdin)
    monkeypatch.setattr(ioradar701_shadow.sys, "stdout", stdout)
    monkeypatch.setattr(
        ioradar701_shadow, "_PREFLIGHT", lambda *_args: calls.append("preflight")
    )
    assert ioradar701_shadow.main(argv) == 2
    assert calls == []


def test_empty_enter_cancels_without_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    stdin = Stream(tty=True, line="")
    stdout = Stream(tty=True)
    calls: list[str] = []
    monkeypatch.setattr(ioradar701_shadow.sys, "stdin", stdin)
    monkeypatch.setattr(ioradar701_shadow.sys, "stdout", stdout)
    monkeypatch.setattr(
        ioradar701_shadow, "_PREFLIGHT", lambda *_args: calls.append("preflight")
    )
    assert ioradar701_shadow.main(["live", "--confirm-hardware"]) == 2
    assert calls == []


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("static_start", "static_start"),
        ("motion_start", "motion_start"),
        ("motion_end", "motion_end"),
        (":event person_move_start", "person_move_start"),
        (":event   person_move_stop  ", "person_move_stop"),
        (":event", None),
        (":event   ", None),
        ("person_move_start", None),
        (":stop", None),
    ],
)
def test_operator_event_command_is_explicit_and_non_empty(
    line: str, expected: str | None
) -> None:
    assert ioradar701_shadow._operator_event_detail(line) == expected


def test_phase_prompts_are_not_operator_events_and_emit_only_once() -> None:
    guide = ioradar701_shadow._OperatorPhaseGuide()

    assert guide.prompts(elapsed=1.0, real_data_seen=False) == ()
    first = guide.prompts(elapsed=1.1, real_data_seen=True)
    assert len(first) == 1 and "static_start" in first[0] and "尚未保存" in first[0]
    assert guide.prompts(elapsed=79.9, real_data_seen=True) == ()
    motion = guide.prompts(elapsed=80.0, real_data_seen=True)
    assert len(motion) == 1 and "motion_start" in motion[0] and "尚未保存" in motion[0]
    stopped = guide.prompts(elapsed=100.0, real_data_seen=True)
    assert len(stopped) == 1 and "motion_end" in stopped[0] and "尚未保存" in stopped[0]
    assert guide.prompts(elapsed=120.0, real_data_seen=True) == ()


def test_operator_event_is_durable_and_records_source_and_elapsed(
    tmp_path: Path,
) -> None:
    capture = ioradar701_shadow._Capture(tmp_path, raw=False)
    capture.operator_event("static_start", 12.5, 2.5)

    row = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip()
    )
    assert row == {
        "detail": "static_start",
        "elapsed_seconds": 2.5,
        "kind": "operator",
        "source": "operator_stdin_complete_line",
        "timestamp_seconds": 12.5,
    }
    assert capture.close() == []


def test_replay_operator_event_accepts_standalone_elapsed_timing(
    tmp_path: Path,
) -> None:
    event = {
        "kind": "operator",
        "detail": "person_move_start",
        "elapsed_seconds": 2.5,
    }
    (tmp_path / "events.jsonl").write_text(
        json.dumps(event) + "\n", encoding="utf-8"
    )
    records, timing_source = ioradar701_shadow._source_operator_events(tmp_path)
    assert timing_source == "saved_host_elapsed"
    assert len(records) == 1
    assert records[0].record == event
    assert records[0].timestamp == 2.5


def test_real_capture_replay_uses_saved_timing_and_no_hardware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = Path("captures/ioradar701/capture-20261027T015513.515409+0800")
    monkeypatch.setattr(
        ioradar701_shadow,
        "_PREFLIGHT",
        lambda *_args: pytest.fail("replay touched hardware preflight"),
    )
    assert ioradar701_shadow.main(
        ["replay", str(capture), "--output-root", str(tmp_path)]
    ) == 0
    directory, = tmp_path.iterdir()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    assert metadata["label"] == "REPLAY／真實雷達＋無 BNO055 記錄"
    assert metadata["gpio_enabled"] is False
    assert metadata["motion_source"] == "unavailable"
    assert summary["parser"]["sync_discarded_bytes"] == 1
    assert summary["decoded_frames"] == 1381
    assert summary["recording_complete"] is True
    rows = [json.loads(line) for line in
            (directory / "shadow.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows
    assert all(row["gpio_enabled"] is False for row in rows)
    assert any(row["availability"] == "bno_not_ready" for row in rows)


def _radar_frame(kind: int, *, identifier: int = 0, count: int = 0) -> bytes:
    body = bytearray(11)
    body[0] = 0xA5
    body[1] = kind
    if kind == 1:
        body[3] = 0x05
        body[6] = count
    else:
        body[3] = identifier
        body[4:6] = (100).to_bytes(2, "big")
        body[6:8] = (1200).to_bytes(2, "big")
        body[8:10] = (6500).to_bytes(2, "big")
        body[10] = 80
    return bytes(body) + bytes((sum(body) & 0xFF,))


def test_replay_restores_same_timestamp_batch_and_saved_motion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "capture"
    output_root = tmp_path / "output"
    capture.mkdir()
    frames = (
        _radar_frame(1, count=1),
        _radar_frame(2, identifier=1),
        _radar_frame(1, count=1),
        _radar_frame(2, identifier=1),
    )
    (capture / "raw.bin").write_bytes(b"".join(frames))
    decoded = [
        json.dumps({
            "raw_hex": value.hex(),
            "received_at_monotonic_seconds": 1.0,
        })
        for value in frames
    ]
    (capture / "decoded.jsonl").write_text(
        "\n".join(decoded) + "\n", encoding="utf-8"
    )
    measurement = FakeMotion(FakeRegister()).read_measurement()
    motion_record = ioradar701_shadow._motion_record(measurement)
    motion_record["canonical_captured_at_monotonic_seconds"] = 0.9
    (capture / "bno055.jsonl").write_text(
        json.dumps(motion_record) + "\n", encoding="utf-8"
    )
    operator_event = {
        "kind": "operator",
        "detail": "person_move_start",
        "timestamp_seconds": 1.05,
    }
    (capture / "events.jsonl").write_text(
        json.dumps(operator_event, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        ioradar701_shadow,
        "_PREFLIGHT",
        lambda *_args: pytest.fail("replay touched hardware preflight"),
    )

    assert ioradar701_shadow.main(
        ["replay", str(capture), "--output-root", str(output_root)]
    ) == 0

    directory, = output_root.iterdir()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (directory / "shadow.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    decoded_rows = [
        json.loads(line)
        for line in (directory / "decoded.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    event_rows = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert metadata["label"] == "REPLAY／真實雷達＋保存 BNO055"
    assert metadata["batching_source"] == (
        "inferred_from_consecutive_equal_receipt_timestamp"
    )
    assert metadata["motion_source"] == "saved_bno055_canonical_host_monotonic"
    assert metadata["operator_event_source"] == "saved_host_monotonic"
    assert summary["bno055_records"] == 1
    assert summary["fusion_updates_applied"] == 1
    assert summary["fusion_updates_coalesced"] == 1
    assert summary["radar_time_discontinuities"] == 0
    assert [row["fusion_update"] for row in rows] == ["coalesced", "applied"]
    assert {row["receipt_batch_sequence"] for row in decoded_rows} == {1}
    assert event_rows == [operator_event]


class FakeSerial:
    def __init__(self) -> None:
        self.closed = False
        self.reads = 0

    def read(self, _size: int = 1) -> bytes:
        self.reads += 1
        raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


class ChunkThenInterruptSerial(FakeSerial):
    def __init__(self, chunk: bytes) -> None:
        super().__init__()
        self.chunk = chunk

    def read(self, _size: int = 1) -> bytes:
        self.reads += 1
        if self.reads == 1:
            return self.chunk
        raise KeyboardInterrupt


class ReadErrorSerial(FakeSerial):
    def read(self, _size: int = 1) -> bytes:
        raise OSError("read failed")


class CleanupErrorSerial(FakeSerial):
    def close(self) -> None:
        self.closed = True
        raise OSError("close failed")


class FakeRegister:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeMotion:
    def __init__(self, register: FakeRegister, **_kwargs: object) -> None:
        self.register = register
        self.closed = False

    def read_identity(self) -> object:
        return object()

    def initialize_ndof(self) -> tuple[int, int, int]:
        return 0x0C, 0x05, 0

    def read_measurement(self) -> BNO055Measurement:
        return BNO055Measurement(
            1.0, EulerAngles(0, 0, 0), Quaternion(1, 0, 0, 0),
            Vector3(0, 0, 0), Vector3(0, 0, 9.8), 25,
            BNO055Calibration(0, 0, 0, 0), 0x0C, 0x05, 0,
        )

    def close(self) -> None:
        self.closed = True
        self.register.close()


class FakeTerminal:
    dropped_status = 0
    dropped_events = 0

    def display(self, *_args: object, **_kwargs: object) -> None:
        pass

    def flush_once(self, _now: float) -> None:
        pass

    def publish(self, _line: str, _now: float, *, event: bool = False) -> None:
        pass

    def finish(self, _line: str) -> None:
        pass

    def close(self) -> None:
        pass


def test_live_interrupt_closes_single_uart_and_i2c_and_saves_complete_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serial = FakeSerial()
    register = FakeRegister()
    motions: list[FakeMotion] = []
    serial_calls: list[tuple[str, int, float]] = []
    value = 0.0

    def clock() -> float:
        nonlocal value
        value += 0.01
        return value

    def serial_factory(port: str, baud: int, timeout: float) -> FakeSerial:
        serial_calls.append((port, baud, timeout))
        return serial

    def motion_factory(reg: FakeRegister, **kwargs: object) -> FakeMotion:
        device = FakeMotion(reg, **kwargs)
        motions.append(device)
        return device

    monkeypatch.setattr(ioradar701_shadow, "_CLOCK", clock)
    monkeypatch.setattr(ioradar701_shadow.sys, "stdout", Stream(tty=True))
    monkeypatch.setattr(ioradar701_shadow, "_WALL_CLOCK",
                        lambda: datetime(2026, 10, 27, tzinfo=timezone.utc))
    monkeypatch.setattr(ioradar701_shadow, "_PREFLIGHT", lambda *_args: None)
    monkeypatch.setattr(ioradar701_shadow, "_SERIAL_FACTORY", serial_factory)
    monkeypatch.setattr(ioradar701_shadow, "_REGISTER_FACTORY", lambda _bus: register)
    monkeypatch.setattr(ioradar701_shadow, "_MOTION_FACTORY", motion_factory)
    monkeypatch.setattr(ioradar701_shadow, "_TERMINAL_FACTORY",
                        lambda *_args, **_kwargs: FakeTerminal())
    options = ioradar701_shadow.parser().parse_args(
        ["live", "--confirm-hardware", "--output-root", str(tmp_path)]
    )
    assert ioradar701_shadow._run_live(options) == 130
    assert len(serial_calls) == 1
    assert serial.closed and register.closed and motions[0].closed
    directory, = tmp_path.iterdir()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["interrupted"] is True
    assert metadata["recording_complete"] is True
    assert metadata["gpio_enabled"] is False


def test_live_one_uart_read_persists_one_batch_and_one_fusion_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frames = (
        _radar_frame(1, count=1),
        _radar_frame(2, identifier=1),
        _radar_frame(1, count=1),
        _radar_frame(2, identifier=1),
    )
    serial = ChunkThenInterruptSerial(b"".join(frames))
    register = FakeRegister()
    value = 0.0

    def clock() -> float:
        nonlocal value
        value += 0.01
        return value

    monkeypatch.setattr(ioradar701_shadow, "_CLOCK", clock)
    monkeypatch.setattr(ioradar701_shadow.sys, "stdout", Stream(tty=True))
    monkeypatch.setattr(
        ioradar701_shadow,
        "_WALL_CLOCK",
        lambda: datetime(2026, 10, 27, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(ioradar701_shadow, "_PREFLIGHT", lambda *_args: None)
    monkeypatch.setattr(
        ioradar701_shadow, "_SERIAL_FACTORY", lambda *_args: serial
    )
    monkeypatch.setattr(
        ioradar701_shadow, "_REGISTER_FACTORY", lambda _bus: register
    )
    monkeypatch.setattr(
        ioradar701_shadow,
        "_MOTION_FACTORY",
        lambda reg, **kwargs: FakeMotion(reg, **kwargs),
    )
    monkeypatch.setattr(
        ioradar701_shadow,
        "_TERMINAL_FACTORY",
        lambda *_args, **_kwargs: FakeTerminal(),
    )
    operator_lines = iter([":event person_move_start"])
    monkeypatch.setattr(
        ioradar701_shadow,
        "_OPERATOR_LINE_POLLER",
        lambda _stream: next(operator_lines, None),
    )
    options = ioradar701_shadow.parser().parse_args(
        ["live", "--confirm-hardware", "--output-root", str(tmp_path)]
    )

    assert ioradar701_shadow._run_live(options) == 130
    assert serial.reads == 2
    assert serial.closed and register.closed

    directory, = tmp_path.iterdir()
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    decoded_rows = [
        json.loads(line)
        for line in (directory / "decoded.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    shadow_rows = [
        json.loads(line)
        for line in (directory / "shadow.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    event_rows = [
        json.loads(line)
        for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert {row["receipt_batch_sequence"] for row in decoded_rows} == {1}
    assert [row["fusion_update"] for row in shadow_rows] == [
        "coalesced",
        "applied",
    ]
    assert summary["receipt_batches"] == 1
    assert summary["fusion_updates_applied"] == 1
    assert summary["fusion_updates_coalesced"] == 1
    assert summary["radar_time_discontinuities"] == 0
    assert len(event_rows) == 1
    assert event_rows[0]["kind"] == "operator"
    assert event_rows[0]["detail"] == "person_move_start"
    assert event_rows[0]["source"] == "operator_stdin_complete_line"
    assert isinstance(event_rows[0]["elapsed_seconds"], float)
    assert isinstance(event_rows[0]["timestamp_seconds"], float)
    assert metadata["operator_event_command"] == ":event <detail>"
    assert metadata["bno055_calibration_profile"] == {
        "loaded": False,
        "source": None,
        "reason": "shadow live initializes NDOF without profile restore",
    }


def test_partial_initialization_closes_owned_uart_and_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    serial = FakeSerial()
    register = FakeRegister()
    monkeypatch.setattr(ioradar701_shadow.sys, "stdout", Stream(tty=True))
    monkeypatch.setattr(ioradar701_shadow, "_PREFLIGHT", lambda *_args: None)
    monkeypatch.setattr(ioradar701_shadow, "_SERIAL_FACTORY",
                        lambda *_args: serial)
    monkeypatch.setattr(ioradar701_shadow, "_REGISTER_FACTORY", lambda _bus: register)
    monkeypatch.setattr(
        ioradar701_shadow,
        "_MOTION_FACTORY",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("init failed")),
    )
    monkeypatch.setattr(ioradar701_shadow, "_TERMINAL_FACTORY",
                        lambda *_args, **_kwargs: FakeTerminal())
    options = ioradar701_shadow.parser().parse_args(
        ["live", "--confirm-hardware", "--output-root", str(tmp_path)]
    )
    assert ioradar701_shadow._run_live(options) == 1
    assert serial.closed and register.closed


@pytest.mark.parametrize(
    ("serial", "expected_shadow_availability"),
    [(ReadErrorSerial(), "radar_error"), (CleanupErrorSerial(), None)],
)
def test_read_and_cleanup_failures_attempt_all_cleanup_and_mark_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    serial: FakeSerial,
    expected_shadow_availability: str | None,
) -> None:
    register = FakeRegister()
    motion = FakeMotion(register)
    monkeypatch.setattr(ioradar701_shadow.sys, "stdout", Stream(tty=True))
    monkeypatch.setattr(ioradar701_shadow, "_PREFLIGHT", lambda *_args: None)
    monkeypatch.setattr(ioradar701_shadow, "_SERIAL_FACTORY", lambda *_args: serial)
    monkeypatch.setattr(ioradar701_shadow, "_REGISTER_FACTORY", lambda _bus: register)
    monkeypatch.setattr(ioradar701_shadow, "_MOTION_FACTORY",
                        lambda *_args, **_kwargs: motion)
    monkeypatch.setattr(ioradar701_shadow, "_TERMINAL_FACTORY",
                        lambda *_args, **_kwargs: FakeTerminal())
    options = ioradar701_shadow.parser().parse_args(
        ["live", "--confirm-hardware", "--output-root", str(tmp_path)]
    )
    assert ioradar701_shadow._run_live(options) == 1
    assert serial.closed and motion.closed and register.closed
    directory, = tmp_path.iterdir()
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["recording_complete"] is False
    if expected_shadow_availability is not None:
        rows = [json.loads(line) for line in
                (directory / "shadow.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[-1]["availability"] == expected_shadow_availability
