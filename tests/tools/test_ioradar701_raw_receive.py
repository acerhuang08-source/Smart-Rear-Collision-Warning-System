"""Hardware-free tests for the standalone ioRadar701 raw UART receiver."""

from __future__ import annotations

import json
import importlib.util
import sys
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import pytest
import serial


_TOOL_PATH = Path(__file__).parents[2] / "tools" / "ioradar701_raw_receive.py"
_SPEC = importlib.util.spec_from_file_location("ioradar701_raw_receive", _TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
receiver = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = receiver
_SPEC.loader.exec_module(receiver)


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
        events: Iterable[bytes | BaseException],
        clock: _Clock,
        *,
        close_error: BaseException | None = None,
    ) -> None:
        self.events = iter(events)
        self.clock = clock
        self.close_error = close_error
        self.is_open = True
        self.read_calls = 0
        self.close_calls = 0

    def read(self, size: int = 1) -> bytes:
        assert size == 4096
        self.read_calls += 1
        self.clock.advance(0.25)
        try:
            event = next(self.events)
        except StopIteration:
            return b""
        if isinstance(event, BaseException):
            raise event
        return event

    def close(self) -> None:
        self.close_calls += 1
        self.is_open = False
        if self.close_error is not None:
            raise self.close_error


def _install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    device: _FakeSerial,
    clock: _Clock,
) -> list[receiver._Options]:
    calls: list[receiver._Options] = []

    def factory(options: receiver._Options) -> _FakeSerial:
        calls.append(options)
        return device

    monkeypatch.setattr(receiver, "_SERIAL_FACTORY", factory)
    monkeypatch.setattr(receiver, "_MONOTONIC", clock)
    monkeypatch.setattr(
        receiver,
        "_WALL_CLOCK",
        lambda: datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(receiver, "_INPUT", lambda prompt: "")
    return calls


def _argv(tmp_path: Path, *, duration: str = "0.5") -> list[str]:
    return ["--duration", duration, "--output-root", str(tmp_path)]


def test_help_does_not_construct_serial(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        receiver,
        "_SERIAL_FACTORY",
        lambda options: pytest.fail("UART must not be opened for --help"),
    )
    with pytest.raises(SystemExit) as raised:
        receiver.main(["--help"])
    assert raised.value.code == 0
    assert "--port" in capsys.readouterr().out


def test_real_factory_configures_receive_only_8n1_before_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    created: list[object] = []

    class _ConfiguredSerial:
        def __init__(self, **settings: object) -> None:
            self.settings = settings
            self.port: str | None = None
            self.exclusive = False
            self.dtr = True
            self.rts = True
            self.is_open = False
            created.append(self)

        def open(self) -> None:
            assert self.port == "/dev/ttyAMA0"
            assert self.exclusive is True
            assert self.dtr is False
            assert self.rts is False
            self.is_open = True

        def close(self) -> None:
            self.is_open = False

    monkeypatch.setattr(receiver.serial, "Serial", _ConfiguredSerial)
    options = receiver._Options(
        port="/dev/ttyAMA0",
        baudrate=256000,
        duration=5.0,
        output_root=tmp_path,
    )

    connection = receiver._open_serial(options)

    configured = created[0]
    assert configured.settings == {
        "port": None,
        "baudrate": 256000,
        "bytesize": serial.EIGHTBITS,
        "parity": serial.PARITY_NONE,
        "stopbits": serial.STOPBITS_ONE,
        "timeout": 0.1,
        "write_timeout": 0,
        "xonxoff": False,
        "rtscts": False,
        "dsrdtr": False,
    }
    assert connection.is_open is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--baudrate", "0"],
        ["--duration", "0"],
        ["--duration", "5.001"],
        ["--duration", "nan"],
        ["--port", "   "],
    ],
)
def test_invalid_arguments_do_not_construct_serial(
    argv: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        receiver,
        "_SERIAL_FACTORY",
        lambda options: pytest.fail("UART must not be opened for invalid input"),
    )
    assert receiver.main(argv) == 2


def test_eof_cancels_before_files_or_uart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(receiver, "_INPUT", lambda prompt: (_ for _ in ()).throw(EOFError()))
    monkeypatch.setattr(
        receiver,
        "_SERIAL_FACTORY",
        lambda options: pytest.fail("UART must not be opened after EOF"),
    )
    assert receiver.main(_argv(tmp_path)) == 2
    assert list(tmp_path.iterdir()) == []
    assert "未開啟 UART" in capsys.readouterr().err


def test_ctrl_c_at_prompt_cancels_before_uart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        receiver,
        "_INPUT",
        lambda prompt: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        receiver,
        "_SERIAL_FACTORY",
        lambda options: pytest.fail("UART must not be opened after Ctrl+C"),
    )
    assert receiver.main(_argv(tmp_path)) == 130
    assert list(tmp_path.iterdir()) == []


def test_raw_bytes_are_fully_saved_and_sample_is_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    payload = bytes(range(256)) * 3
    device = _FakeSerial([payload], clock)
    calls = _install(monkeypatch, tmp_path, device, clock)

    assert receiver.main(_argv(tmp_path)) == 0

    capture = next(tmp_path.iterdir())
    assert (capture / "raw.bin").read_bytes() == payload
    metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["total_bytes"] == len(payload)
    assert metadata["persisted_bytes"] == len(payload)
    assert metadata["full_capture_saved"] is True
    assert metadata["data_interpretation"] == "opaque_uart_bytes_not_validated_packets"
    assert metadata["requested_baudrate"] == 230400
    assert metadata["requested_serial_format"] == "8N1"
    assert calls[0].port == "/dev/ttyAMA0"
    assert device.close_calls == 1
    output = capsys.readouterr().out
    assert "sample_bytes=512" in output
    assert payload[:512].hex(" ") in output
    assert payload.hex(" ") not in output


def test_zero_data_is_reported_saved_and_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    device = _FakeSerial([], clock)
    _install(monkeypatch, tmp_path, device, clock)

    assert receiver.main(_argv(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["exit_reason"] == "no_data"
    assert metadata["total_bytes"] == 0
    assert (capture / "raw.bin").read_bytes() == b""
    assert device.close_calls == 1
    captured = capsys.readouterr()
    assert "sample_hex=(empty)" in captured.out
    assert "不會自動改速率" in captured.err


def test_ctrl_c_during_read_preserves_prior_bytes_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    device = _FakeSerial([b"abc", KeyboardInterrupt()], clock)
    _install(monkeypatch, tmp_path, device, clock)

    assert receiver.main(_argv(tmp_path, duration="2")) == 130

    capture = next(tmp_path.iterdir())
    metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["exit_reason"] == "interrupted"
    assert metadata["full_capture_saved"] is True
    assert (capture / "raw.bin").read_bytes() == b"abc"
    assert device.close_calls == 1


def test_read_error_is_reported_and_resource_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    device = _FakeSerial([serial.SerialException("disconnected")], clock)
    _install(monkeypatch, tmp_path, device, clock)

    assert receiver.main(_argv(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["exit_reason"] == "read_error"
    assert device.close_calls == 1
    assert "failed to read UART" in capsys.readouterr().err


def test_raw_write_error_is_reported_and_resource_is_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    device = _FakeSerial([b"received"], clock)
    _install(monkeypatch, tmp_path, device, clock)

    class _FailingRawFile:
        def write(self, data: bytes) -> int:
            raise OSError("disk full")

        def flush(self) -> None:
            pass

        def fileno(self) -> int:
            return -1

        def close(self) -> None:
            pass

    original_open = Path.open

    def fake_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path.name == "raw.bin":
            return _FailingRawFile()
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)
    monkeypatch.setattr(receiver.os, "fsync", lambda descriptor: None)

    assert receiver.main(_argv(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["exit_reason"] == "storage_error"
    assert metadata["total_bytes"] == len(b"received")
    assert metadata["persisted_bytes"] == 0
    assert metadata["full_capture_saved"] is False
    assert device.close_calls == 1
    assert "disk full" in capsys.readouterr().err


def test_metadata_write_error_is_reported_without_overwriting_raw_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = _Clock()
    device = _FakeSerial([b"preserved"], clock)
    _install(monkeypatch, tmp_path, device, clock)
    original_open = Path.open

    def fake_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path.name == "metadata.json" and mode == "x":
            raise OSError("metadata volume unavailable")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fake_open)

    assert receiver.main(_argv(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    assert (capture / "raw.bin").read_bytes() == b"preserved"
    assert not (capture / "metadata.json").exists()
    captured = capsys.readouterr()
    assert "metadata_path=NOT_SAVED" in captured.out
    assert "metadata volume unavailable" in captured.err


def test_open_error_still_records_metadata_and_closes_raw_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(receiver, "_INPUT", lambda prompt: "")
    monkeypatch.setattr(
        receiver,
        "_WALL_CLOCK",
        lambda: datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        receiver,
        "_SERIAL_FACTORY",
        lambda options: (_ for _ in ()).throw(serial.SerialException("busy")),
    )

    assert receiver.main(_argv(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["exit_reason"] == "open_error"
    assert metadata["acquisition_started"] is False
    assert metadata["serial_close_succeeded"] is None


def test_close_error_is_reported_in_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    device = _FakeSerial([], clock, close_error=serial.SerialException("close failed"))
    _install(monkeypatch, tmp_path, device, clock)

    assert receiver.main(_argv(tmp_path)) == 1

    capture = next(tmp_path.iterdir())
    metadata = json.loads((capture / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["exit_reason"] == "close_error"
    assert metadata["serial_close_succeeded"] is False
