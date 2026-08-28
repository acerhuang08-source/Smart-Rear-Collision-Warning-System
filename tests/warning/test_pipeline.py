"""End-to-end tests using fake UART and LED boundaries."""

from __future__ import annotations

from collections import deque

from rear_warning.sensors.tfmini_plus import TFMiniPlusSerialDevice
from rear_warning.warning import WarningPolicy, WarningState
from rear_warning.warning.controller import WarningController
from rear_warning.warning.pipeline import WarningPipeline


def _frame(distance_cm: int) -> bytes:
    payload = (
        b"\x59\x59"
        + distance_cm.to_bytes(2, "little")
        + (100).to_bytes(2, "little")
        + (2248).to_bytes(2, "little")
    )
    return payload + bytes((sum(payload) & 0xFF,))


class _FakeSerial:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = deque(chunks)
        self.is_open = True

    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def read(self, size: int = 1) -> bytes:
        return self._chunks.popleft() if self._chunks else b""


class _FakeLedDriver:
    def __init__(self) -> None:
        self.states: list[WarningState] = []

    def display(self, state: WarningState) -> None:
        self.states.append(state)


def test_uart_frame_reaches_warning_led_boundary() -> None:
    serial_connection = _FakeSerial([_frame(149)])
    device = TFMiniPlusSerialDevice(
        "fake-port",
        serial_factory=lambda **_settings: serial_connection,
    )
    led = _FakeLedDriver()
    pipeline = WarningPipeline(
        device, WarningController(WarningPolicy(), led)
    )
    device.open()

    states = pipeline.poll()

    assert states == [WarningState.DANGER]
    assert led.states == [WarningState.DANGER]


def test_empty_uart_read_reaches_sensor_fault_led_boundary() -> None:
    serial_connection = _FakeSerial([b""])
    device = TFMiniPlusSerialDevice(
        "fake-port",
        serial_factory=lambda **_settings: serial_connection,
    )
    led = _FakeLedDriver()
    pipeline = WarningPipeline(
        device, WarningController(WarningPolicy(), led)
    )
    device.open()

    assert pipeline.poll() == [WarningState.SENSOR_FAULT]
    assert led.states == [WarningState.SENSOR_FAULT]


def test_pipeline_uses_stabilized_state_across_polls() -> None:
    serial_connection = _FakeSerial(
        [_frame(distance) for distance in (301, 300, 301, 320, 321)]
    )
    device = TFMiniPlusSerialDevice(
        "fake-port",
        serial_factory=lambda **_settings: serial_connection,
    )
    led = _FakeLedDriver()
    pipeline = WarningPipeline(
        device, WarningController(WarningPolicy(), led)
    )
    device.open()

    states = [pipeline.poll()[0] for _ in range(5)]

    assert states == [
        WarningState.SAFE,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.SAFE,
    ]
    assert led.states == states
