"""Offline and fake-UART lifecycle tests for the ioRadar701 CLI."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import serial

from rear_warning.cli import ioradar701
from rear_warning.sensors.ioradar701.parser import IoRadar701StreamParser
from rear_warning.sensors.ioradar701.state import RadarStateTracker

STATUS = bytes.fromhex("a5 01 00 05 0b 00 01 00 00 00 00 b7")
TARGET = bytes.fromhex("a5 02 00 01 00 e9 04 c6 19 64 66 3e")


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeSerial:
    def __init__(
        self,
        clock: _Clock,
        events: list[bytes | BaseException],
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.clock = clock
        self.events = iter(events)
        self.close_error = close_error
        self.is_open = True
        self.closed = False

    def read(self, size: int = 1) -> bytes:
        assert size == 4096
        self.clock.advance(0.1)
        try:
            event = next(self.events)
        except StopIteration:
            return b""
        if isinstance(event, BaseException):
            raise event
        return event

    def close(self) -> None:
        self.closed = True
        self.is_open = False
        if self.close_error is not None:
            raise self.close_error


def _options(tmp_path: Path, *, duration: float | None = 0.3) -> ioradar701._LiveOptions:
    return ioradar701._LiveOptions(
        port="/dev/fake",
        baudrate=230400,
        stale_after=1.0,
        refresh_hz=5.0,
        duration=duration,
        output_root=tmp_path,
        plain=True,
        scenario="fake lifecycle",
        initial_events=("test marker",),
    )


def _install_live(
    monkeypatch: pytest.MonkeyPatch,
    clock: _Clock,
    device: _FakeSerial,
) -> None:
    monkeypatch.setattr(ioradar701, "_MONOTONIC", clock)
    monkeypatch.setattr(
        ioradar701,
        "_WALL_CLOCK",
        lambda: datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(ioradar701, "_INPUT", lambda prompt: "")
    monkeypatch.setattr(ioradar701, "_SERIAL_FACTORY", lambda options: device)
    monkeypatch.setattr(ioradar701, "_poll_operator_line", lambda stream: None)
    monkeypatch.setattr(
        ioradar701, "_git_identity", lambda: {"head": "abc", "dirty": True}
    )


def test_offline_writes_no_files_by_default_and_reports_real_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = tmp_path / "raw.bin"
    raw.write_bytes(STATUS + TARGET + STATUS[:5])

    assert ioradar701.main(["offline", str(raw)]) == 0

    output = capsys.readouterr().out
    assert f"sha256={hashlib.sha256(raw.read_bytes()).hexdigest()}" in output
    assert "bytes=29" in output
    assert "status_frames=1" in output
    assert "target_frames=1" in output
    assert "trailing_bytes=5" in output
    assert sorted(path.name for path in tmp_path.iterdir()) == ["raw.bin"]


def test_offline_decoded_output_never_overwrites(tmp_path: Path) -> None:
    raw = tmp_path / "raw.bin"
    output = tmp_path / "decoded.jsonl"
    raw.write_bytes(STATUS + TARGET)

    assert ioradar701.main(
        ["offline", str(raw), "--decoded-output", str(output)]
    ) == 0
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [record["kind"] for record in records] == ["status", "target"]
    assert ioradar701.main(
        ["offline", str(raw), "--decoded-output", str(output)]
    ) == 1


def test_enter_gate_cancels_before_files_or_uart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ioradar701,
        "_INPUT",
        lambda prompt: (_ for _ in ()).throw(EOFError()),
    )
    monkeypatch.setattr(
        ioradar701,
        "_SERIAL_FACTORY",
        lambda options: pytest.fail("UART must not open before Enter"),
    )

    result = ioradar701.main(
        [
            "live", "--confirm-hardware", "--plain", "--duration", "1",
            "--output-root", str(tmp_path),
        ]
    )
    assert result == 2
    assert list(tmp_path.iterdir()) == []


def test_fake_live_capture_saves_all_five_artifacts_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    device = _FakeSerial(clock, [STATUS + TARGET])
    _install_live(monkeypatch, clock, device)

    assert ioradar701._live(_options(tmp_path)) == 0

    capture = next(tmp_path.iterdir())
    assert sorted(path.name for path in capture.iterdir()) == [
        "decoded.jsonl", "events.jsonl", "metadata.json", "raw.bin", "summary.json",
    ]
    assert (capture / "raw.bin").read_bytes() == STATUS + TARGET
    decoded = (capture / "decoded.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(decoded) == 2
    summary = json.loads((capture / "summary.json").read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "completed"
    assert summary["status_frames"] == 1
    assert summary["target_frames"] == 1
    assert summary["persisted_bytes"] == 24
    assert summary["capture_files_close_succeeded"] is True
    assert device.closed is True


def test_fake_live_read_error_is_distinct_and_resources_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    device = _FakeSerial(clock, [STATUS, serial.SerialException("disconnected")])
    _install_live(monkeypatch, clock, device)

    assert ioradar701._live(_options(tmp_path, duration=None)) == 1

    capture = next(tmp_path.iterdir())
    summary = json.loads((capture / "summary.json").read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "read_error"
    assert "disconnected" in summary["detail"]
    assert (capture / "raw.bin").read_bytes() == STATUS
    assert device.closed is True


def test_fake_live_ctrl_c_preserves_prior_bytes_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    device = _FakeSerial(clock, [STATUS, KeyboardInterrupt()])
    _install_live(monkeypatch, clock, device)

    assert ioradar701._live(_options(tmp_path, duration=None)) == 130

    capture = next(tmp_path.iterdir())
    summary = json.loads((capture / "summary.json").read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "interrupted"
    assert summary["full_capture_saved"] is True
    assert (capture / "raw.bin").read_bytes() == STATUS
    assert device.closed is True


def test_fake_live_close_error_is_nonzero_and_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    device = _FakeSerial(
        clock,
        [STATUS + TARGET],
        close_error=serial.SerialException("close failed"),
    )
    _install_live(monkeypatch, clock, device)

    assert ioradar701._live(_options(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    summary = json.loads((capture / "summary.json").read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "close_error"
    assert summary["serial_close_succeeded"] is False
    assert any("close failed" in error for error in summary["storage_errors"])


def test_fake_live_raw_write_error_is_reported_and_resources_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    device = _FakeSerial(clock, [STATUS + TARGET])
    _install_live(monkeypatch, clock, device)
    original_open = Path.open

    class _FailingRaw:
        def __init__(self, wrapped: object) -> None:
            self.wrapped = wrapped

        def write(self, data: bytes) -> int:
            raise OSError("disk full")

        def flush(self) -> None:
            self.wrapped.flush()  # type: ignore[attr-defined]

        def fileno(self) -> int:
            return self.wrapped.fileno()  # type: ignore[attr-defined,no-any-return]

        def close(self) -> None:
            self.wrapped.close()  # type: ignore[attr-defined]

    def failing_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        opened = original_open(path, mode, *args, **kwargs)
        return _FailingRaw(opened) if path.name == "raw.bin" and mode == "xb" else opened

    monkeypatch.setattr(Path, "open", failing_open)

    assert ioradar701._live(_options(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    summary = json.loads((capture / "summary.json").read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "storage_error"
    assert summary["received_bytes"] == 24
    assert summary["persisted_bytes"] == 0
    assert "disk full" in summary["detail"]
    assert device.closed is True


def test_plain_stale_status_does_not_fabricate_zero_measurement() -> None:
    parser = IoRadar701StreamParser()
    tracker = RadarStateTracker()
    tracker.note_bytes(0.0)
    snapshot = tracker.snapshot(0.1, stale_after=1.0)

    line = ioradar701._format_plain(snapshot, parser, elapsed=0.1)

    assert "等待有效狀態" in line
    assert "無即時目標值" in line
    assert "D=0" not in line
    assert "Vx=0" not in line
