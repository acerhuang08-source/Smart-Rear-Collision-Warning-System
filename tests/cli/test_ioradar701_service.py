"""Hardware-free tests for the unattended ioRadar701 LED service entry."""

from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rear_warning.cli import ioradar701_service, ioradar701_shadow
from rear_warning.sensors.bno055.models import (
    BNO055Calibration,
    BNO055Measurement,
    EulerAngles,
    Quaternion,
    Vector3,
)

SERVICE_FLAGS = ["--confirm-hardware", "--enable-led"]


class _Stream:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty
        self.text = ""

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        raise AssertionError("service must not read stdin")

    def write(self, value: str) -> int:
        self.text += value
        return len(value)

    def flush(self) -> None:
        pass

    def fileno(self) -> int:
        return 1


class _FakeGpio:
    def __init__(self) -> None:
        self.closed = False
        self.states: dict[int, bool] = {}

    def write(self, pin: int, active: bool) -> None:
        self.states[pin] = active

    def close(self) -> None:
        self.closed = True


class _FakeRegister:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeMotion:
    def __init__(self, register: _FakeRegister, **_kwargs: object) -> None:
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


class _BrokenMotion(_FakeMotion):
    def read_identity(self) -> object:
        raise OSError("BNO055 not responding")


class _FakeTerminal:
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


CYCLE = _radar_frame(1, count=1) + _radar_frame(2, identifier=1)


class _ScriptedSerial:
    """Return one radar cycle per read, then run ``finish`` on the last read."""

    def __init__(self, reads_before_finish: int, finish: str) -> None:
        self.remaining = reads_before_finish
        self.finish = finish
        self.closed = False

    def read(self, _size: int = 1) -> bytes:
        if self.remaining > 0:
            self.remaining -= 1
            return CYCLE
        if self.finish == "sigterm":
            os.kill(os.getpid(), signal.SIGTERM)
            return b""
        raise KeyboardInterrupt

    def close(self) -> None:
        self.closed = True


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    serial: _ScriptedSerial,
    gpio: _FakeGpio,
    motion_class: type[_FakeMotion] = _FakeMotion,
    step: float = 0.05,
) -> _FakeRegister:
    register = _FakeRegister()
    value = 0.0
    stamps = iter(range(10_000))

    def clock() -> float:
        nonlocal value
        value += step
        return value

    def wall_clock() -> datetime:
        return datetime(2026, 11, 25, 0, 0, next(stamps) % 60, tzinfo=timezone.utc)

    monkeypatch.setattr(ioradar701_service.sys, "stdin", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_service.sys, "stdout", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_service.sys, "stderr", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_service, "_OUTPUT_FACTORY",
                        lambda *_args, **_kwargs: gpio)
    monkeypatch.setattr(ioradar701_shadow, "_CLOCK", clock)
    monkeypatch.setattr(ioradar701_shadow, "_WALL_CLOCK", wall_clock)
    monkeypatch.setattr(ioradar701_shadow, "_PREFLIGHT", lambda *_args: None)
    monkeypatch.setattr(ioradar701_shadow, "_SERIAL_FACTORY", lambda *_args: serial)
    monkeypatch.setattr(ioradar701_shadow, "_REGISTER_FACTORY", lambda _bus: register)
    monkeypatch.setattr(ioradar701_shadow, "_MOTION_FACTORY",
                        lambda reg, **kwargs: motion_class(reg, **kwargs))
    monkeypatch.setattr(ioradar701_shadow, "_TERMINAL_FACTORY",
                        lambda *_args, **_kwargs: _FakeTerminal())
    monkeypatch.setattr(
        ioradar701_shadow,
        "_OPERATOR_LINE_POLLER",
        lambda _stream: pytest.fail("service must not poll operator stdin"),
    )
    return register


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("argv", [[], ["--confirm-hardware"], ["--enable-led"]])
def test_both_opt_in_flags_are_required_before_any_hardware_io(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(ioradar701_service.sys, "stderr", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_service, "_OUTPUT_FACTORY",
                        lambda *_args, **_kwargs: calls.append("gpio"))
    monkeypatch.setattr(ioradar701_shadow, "_run_live",
                        lambda *_args, **_kwargs: calls.append("sensors"))

    assert ioradar701_service.main(argv + ["--output-root", str(tmp_path)]) == 2
    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_defaults_match_led_command_and_need_no_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    options = ioradar701_service._build_parser().parse_args([])
    assert options.port == "/dev/ttyAMA0"
    assert options.baudrate == 230400
    assert options.i2c_bus == 1
    assert options.bno055_address == 0x29
    assert options.stale_after == 1.0
    assert options.duration is None
    assert options.segment_seconds == 600.0
    assert options.max_total_mb == 2048
    assert options.output_root == Path("local-state/autostart-ioradar701")

    gpio = _FakeGpio()
    received: dict[str, object] = {}

    def fake_run(run_options: object, **kwargs: object) -> int:
        received.update(kwargs)
        received["plain"] = getattr(run_options, "plain")
        return 0

    monkeypatch.setattr(ioradar701_service.sys, "stdin", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_service.sys, "stdout", _Stream(tty=False))
    monkeypatch.setattr(ioradar701_service, "_OUTPUT_FACTORY",
                        lambda *_args, **_kwargs: gpio)
    monkeypatch.setattr(ioradar701_shadow, "_run_live", fake_run)

    assert ioradar701_service.main(
        SERVICE_FLAGS + ["--output-root", str(tmp_path)]
    ) == 0
    assert received["gpio_enabled"] is True
    assert received["operator_input"] is False
    assert received["t4_phase_prompts"] is False
    assert received["capture_prefix"] == "service"
    assert received["segment_seconds"] == 600.0
    assert received["mode"] == "SERVICE_RADAR_DISTANCE_LED"
    assert received["plain"] is True
    # The fake run never started the observer, so the entry must release GPIO.
    assert gpio.closed


def test_sigterm_turns_leds_off_closes_everything_and_finalizes_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gpio = _FakeGpio()
    serial = _ScriptedSerial(reads_before_finish=3, finish="sigterm")
    register = _install_fakes(monkeypatch, serial=serial, gpio=gpio)
    before = signal.getsignal(signal.SIGTERM)

    assert ioradar701_service.main(
        SERVICE_FLAGS + ["--output-root", str(tmp_path)]
    ) == 130

    assert signal.getsignal(signal.SIGTERM) == before
    assert serial.closed and register.closed and gpio.closed
    assert set(gpio.states.values()) == {False}
    directory, = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert directory.name.startswith("service-")
    assert (directory / ioradar701_service.MARKER_NAME).is_file()
    metadata = _json(directory / "metadata.json")
    assert metadata["interrupted"] is True
    assert metadata["recording_complete"] is True
    assert metadata["mode"] == "SERVICE_RADAR_DISTANCE_LED"
    assert metadata["led_cleanup"] == {
        "finish_attempted": True,
        "all_off_succeeded": True,
        "gpio_close_succeeded": True,
    }
    assert metadata["segment"]["final"] is True  # type: ignore[index]
    led_kinds = [row["kind"] for row in _jsonl(directory / "led.jsonl")]
    assert led_kinds[0] == "startup" and led_kinds[-1] == "cleanup"


def test_segments_rotate_without_reopening_sensors_or_gpio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gpio = _FakeGpio()
    serial = _ScriptedSerial(reads_before_finish=12, finish="interrupt")
    openings: list[str] = []
    register = _install_fakes(monkeypatch, serial=serial, gpio=gpio, step=0.05)
    monkeypatch.setattr(ioradar701_shadow, "_SERIAL_FACTORY",
                        lambda *_args: openings.append("uart") or serial)
    monkeypatch.setattr(ioradar701_service, "_OUTPUT_FACTORY",
                        lambda *_args, **_kwargs: openings.append("gpio") or gpio)

    assert ioradar701_service.main(
        SERVICE_FLAGS + ["--output-root", str(tmp_path), "--segment-seconds", "0.5"]
    ) == 130

    assert openings == ["gpio", "uart"]
    assert serial.closed and register.closed and gpio.closed
    segments = sorted(path for path in tmp_path.iterdir() if path.is_dir())
    assert len(segments) >= 2
    finals = []
    for index, directory in enumerate(segments):
        assert (directory / ioradar701_service.MARKER_NAME).is_file()
        metadata = _json(directory / "metadata.json")
        summary = _json(directory / "summary.json")
        segment = metadata["segment"]
        assert isinstance(segment, dict)
        assert segment["index"] == index
        finals.append(segment["final"])
        assert summary["recording_complete"] is True
        assert metadata["interrupted"] is (index == len(segments) - 1)
    assert finals == [False] * (len(segments) - 1) + [True]
    first_led = [row["kind"] for row in _jsonl(segments[0] / "led.jsonl")]
    last_led = [row["kind"] for row in _jsonl(segments[-1] / "led.jsonl")]
    assert first_led[0] == "startup"
    assert "startup" not in [
        row["kind"] for path in segments[1:] for row in _jsonl(path / "led.jsonl")
    ]
    assert last_led[-1] == "cleanup"


def test_bno055_initialization_failure_exits_non_zero_and_releases_gpio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gpio = _FakeGpio()
    serial = _ScriptedSerial(reads_before_finish=0, finish="interrupt")
    register = _install_fakes(
        monkeypatch, serial=serial, gpio=gpio, motion_class=_BrokenMotion
    )

    assert ioradar701_service.main(
        SERVICE_FLAGS + ["--output-root", str(tmp_path)]
    ) == 1
    assert serial.closed and register.closed and gpio.closed
    assert set(gpio.states.values()) == {False}
    directory, = [path for path in tmp_path.iterdir() if path.is_dir()]
    metadata = _json(directory / "metadata.json")
    assert metadata["recording_complete"] is False
    assert any("BNO055 not responding" in item for item in metadata["errors"])  # type: ignore[union-attr]


def test_busy_uart_preflight_exits_non_zero_without_capture_and_releases_gpio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gpio = _FakeGpio()
    serial = _ScriptedSerial(reads_before_finish=0, finish="interrupt")
    _install_fakes(monkeypatch, serial=serial, gpio=gpio)

    def busy(*_args: object) -> None:
        raise RuntimeError("/dev/ttyAMA0 busy: pid 3286 python3")

    monkeypatch.setattr(ioradar701_shadow, "_PREFLIGHT", busy)
    monkeypatch.setattr(ioradar701_shadow, "_SERIAL_FACTORY",
                        lambda *_args: pytest.fail("opened a busy UART"))

    assert ioradar701_service.main(
        SERVICE_FLAGS + ["--output-root", str(tmp_path)]
    ) == 1
    assert gpio.closed
    assert set(gpio.states.values()) <= {False}
    assert [path for path in tmp_path.iterdir() if path.is_dir()] == []


def _make_capture(root: Path, name: str, size: int, *, marker: bool = True) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "raw.bin").write_bytes(b"\0" * size)
    if marker:
        (path / ioradar701_service.MARKER_NAME).write_text("x", encoding="utf-8")
    return path


def test_prune_removes_only_oldest_marked_service_captures(tmp_path: Path) -> None:
    oldest = _make_capture(tmp_path, "service-20261125T000001", 400)
    middle = _make_capture(tmp_path, "service-20261125T000002", 400)
    current = _make_capture(tmp_path, "service-20261125T000003", 400)
    unmarked = _make_capture(tmp_path, "service-20261125T000000", 400, marker=False)
    foreign = _make_capture(tmp_path, "live-20261125T000000", 400)
    marker_size = len("x")

    removed, errors = ioradar701_service.prune_service_captures(
        tmp_path, 2 * (400 + marker_size), keep=frozenset({current})
    )

    assert errors == []
    assert removed == [oldest]
    assert not oldest.exists()
    assert middle.exists() and current.exists()
    assert unmarked.exists() and foreign.exists()


def test_prune_never_removes_kept_capture_even_when_over_limit(tmp_path: Path) -> None:
    current = _make_capture(tmp_path, "service-20261125T000001", 4000)

    removed, errors = ioradar701_service.prune_service_captures(
        tmp_path, 1, keep=frozenset({current})
    )

    assert removed == [] and errors == []
    assert current.exists()


def test_unsegmented_live_metadata_has_no_segment_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gpio = _FakeGpio()
    serial = _ScriptedSerial(reads_before_finish=1, finish="interrupt")
    _install_fakes(monkeypatch, serial=serial, gpio=gpio)
    options = ioradar701_shadow.parser().parse_args(
        ["live", "--confirm-hardware", "--output-root", str(tmp_path)]
    )

    assert ioradar701_shadow._run_live(options, operator_input=False) == 130
    directory, = tmp_path.iterdir()
    assert directory.name.startswith("live-")
    assert "segment" not in _json(directory / "metadata.json")
