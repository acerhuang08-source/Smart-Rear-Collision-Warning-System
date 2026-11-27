"""Hardware-free tests for the interactive post-wiring quick test."""

from __future__ import annotations

import importlib.util
import io
import sys
from collections.abc import Iterator
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "tools" / "wiring_quick_test.py"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("wiring_quick_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool() -> Iterator[ModuleType]:
    module = _load_tool()
    yield module


def _frame(frame_type: int, marker: int = 0) -> bytes:
    raw = bytearray(12)
    raw[0] = 0xA5
    raw[1] = frame_type
    raw[3] = marker
    raw[11] = sum(raw[:11]) & 0xFF
    return bytes(raw)


def test_parser_handles_split_reads_checksum_failure_and_resync(tool: ModuleType) -> None:
    parser = tool.IoRadar701StreamParser()
    damaged = bytearray(_frame(0x01))
    damaged[11] ^= 0xFF
    stream = b"\x99" + bytes(damaged) + _frame(0x01) + _frame(0x02, marker=7)

    decoded = []
    for chunk in (stream[:2], stream[2:9], stream[9:23], stream[23:31], stream[31:]):
        decoded.extend(parser.feed(chunk))

    assert [frame.kind for frame in decoded] == ["status", "target"]
    assert parser.statistics.status_frames == 1
    assert parser.statistics.target_frames == 1
    assert parser.statistics.checksum_failures == 1
    assert parser.statistics.sync_discarded_bytes == 13
    assert parser.statistics.trailing_bytes == 0


def test_gpio_chip_detection_prefers_rp1(tmp_path: Path, tool: ModuleType) -> None:
    for number, label, ngpio in ((0, "pinctrl-bcm2712", 32), (4, "pinctrl-rp1", 54)):
        chip = tmp_path / f"gpiochip{number}"
        chip.mkdir()
        (chip / "label").write_text(label, encoding="utf-8")
        (chip / "ngpio").write_text(str(ngpio), encoding="utf-8")

    assert tool._detect_gpio_chip(tmp_path) == (4, "auto:pinctrl-rp1,ngpio=54")


def test_led_refuses_to_guess_gpio_chip(tool: ModuleType) -> None:
    tool._GPIO_FACTORY_LOADER = lambda: (_ for _ in ()).throw(AssertionError("GPIO loaded"))
    result = tool._run_led(
        tool._Options("led", None, "unavailable"),
        output=io.StringIO(),
        error_output=io.StringIO(),
    )

    assert result.status == "FAIL"
    assert "--gpio-chip" in result.detail


class _FakeGpio:
    def __init__(self) -> None:
        self.writes: list[tuple[int, bool]] = []
        self.closed = False

    def write(self, pin: int, active: bool) -> None:
        self.writes.append((pin, active))

    def close(self) -> None:
        self.closed = True


def test_led_ctrl_c_turns_everything_off_and_closes(tool: ModuleType) -> None:
    gpio = _FakeGpio()
    tool._GPIO_FACTORY_LOADER = lambda: lambda _pins, chip: gpio
    tool._SLEEP = lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt())
    options = tool._Options("led", 4, "test")

    with pytest.raises(KeyboardInterrupt):
        tool._run_led(options, output=io.StringIO(), error_output=io.StringIO())

    assert gpio.closed is True
    assert gpio.writes[-3:] == [(17, False), (27, False), (22, False)]


class _FakeBus:
    def __init__(self, values: dict[int, object]) -> None:
        self.values = values
        self.reads: list[tuple[int, int]] = []
        self.closed = False

    def read_byte_data(self, address: int, register: int) -> int:
        self.reads.append((address, register))
        value = self.values[address]
        if isinstance(value, BaseException):
            raise value
        return int(value)

    def close(self) -> None:
        self.closed = True


def test_bno_falls_back_to_0x28_and_closes(tool: ModuleType) -> None:
    bus = _FakeBus({0x29: OSError("no response"), 0x28: 0xA0})
    tool._REGISTER_IO_LOADER = lambda: lambda _number: bus

    result = tool._run_bno(output=io.StringIO(), error_output=io.StringIO())

    assert result.status == "PASS"
    assert bus.reads == [(0x29, 0x00), (0x28, 0x00)]
    assert bus.closed is True


class _FakeSerial:
    def __init__(self, chunks: list[object]) -> None:
        self.chunks = chunks
        self.closed = False

    @property
    def is_open(self) -> bool:
        return not self.closed

    def read(self, _size: int = 1) -> bytes:
        value = self.chunks.pop(0)
        if isinstance(value, BaseException):
            raise value
        return bytes(value)

    def fileno(self) -> int:
        return 1

    def close(self) -> None:
        self.closed = True


def _clock(step: float = 1.0) -> Callable[[], float]:
    value = -step

    def now() -> float:
        nonlocal value
        value += step
        return value

    return now


def test_radar_reports_split_valid_frames_and_closes(tool: ModuleType) -> None:
    raw = _frame(0x01) + _frame(0x02)
    serial = _FakeSerial([raw[:5], raw[5:13], raw[13:], b""])
    tool._SERIAL_FACTORY = lambda: serial
    tool._MONOTONIC = _clock()
    output = io.StringIO()

    result = tool._run_radar(output=output, error_output=io.StringIO())

    assert result.status == "PASS"
    assert serial.closed is True
    assert "valid_status=1" in output.getvalue()
    assert "valid_target=1" in output.getvalue()


def test_radar_read_failure_still_closes(tool: ModuleType) -> None:
    serial = _FakeSerial([OSError("read failed")])
    tool._SERIAL_FACTORY = lambda: serial
    tool._MONOTONIC = _clock()

    result = tool._run_radar(output=io.StringIO(), error_output=io.StringIO())

    assert result.status == "FAIL"
    assert serial.closed is True


def test_unknown_owner_is_not_stopped(tool: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    tool._OWNER_PROBE = lambda _path: (123,)
    monkeypatch.setattr(tool, "_service_state", lambda: None)
    actions: list[str] = []
    monkeypatch.setattr(tool, "_systemctl", actions.append)

    with pytest.raises(OSError, match="nothing was stopped"):
        tool._prepare_device("/dev/ttyAMA0", output=io.StringIO())

    assert actions == []


def test_confirmed_tfmini_owner_is_paused(tool: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    owner_results = iter(((456,), ()))
    tool._OWNER_PROBE = lambda _path: next(owner_results)
    monkeypatch.setattr(tool, "_service_state", lambda: tool._ServiceState("active", 456))
    actions: list[str] = []
    monkeypatch.setattr(tool, "_systemctl", actions.append)

    paused = tool._prepare_device("/dev/ttyAMA0", output=io.StringIO())

    assert paused is True
    assert actions == ["stop"]


def test_eof_before_confirmation_has_zero_hardware_activity(tool: ModuleType) -> None:
    touched: list[str] = []
    tool._INPUT = lambda _prompt: (_ for _ in ()).throw(EOFError())
    tool._OWNER_PROBE = lambda path: (touched.append(path), ())[1]
    tool._GPIO_FACTORY_LOADER = lambda: (_ for _ in ()).throw(AssertionError("GPIO loaded"))
    tool._REGISTER_IO_LOADER = lambda: (_ for _ in ()).throw(AssertionError("SMBus loaded"))
    tool._SERIAL_FACTORY = lambda: (_ for _ in ()).throw(AssertionError("UART opened"))

    assert tool.main(["all"]) == 2
    assert touched == []
