"""Hardware-free tests for the TFMini Plus serial device boundary."""

from __future__ import annotations

from collections import deque

import pytest
import serial

from rear_warning.sensors.tfmini_plus import (
    TFMiniPlusMeasurement,
    TFMiniPlusSerialCloseError,
    TFMiniPlusSerialDevice,
    TFMiniPlusSerialNotOpenError,
    TFMiniPlusSerialOpenError,
    TFMiniPlusSerialReadError,
)


def _make_frame(
    *,
    distance_cm: int = 300,
    strength: int = 1200,
    raw_temperature: int = 2248,
) -> bytes:
    """Build a standard TFMini Plus packet with a valid checksum."""
    frame_without_checksum = (
        b"\x59\x59"
        + distance_cm.to_bytes(2, byteorder="little", signed=False)
        + strength.to_bytes(2, byteorder="little", signed=False)
        + raw_temperature.to_bytes(2, byteorder="little", signed=False)
    )
    return frame_without_checksum + bytes((sum(frame_without_checksum) & 0xFF,))


class _FakeSerialConnection:
    """Small pySerial-compatible fake with queued read chunks."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        *,
        initially_open: bool = True,
        open_error: serial.SerialException | None = None,
        read_error: serial.SerialException | None = None,
        close_error: serial.SerialException | None = None,
    ) -> None:
        self._chunks = deque(chunks or [])
        self._is_open = initially_open
        self._open_error = open_error
        self._read_error = read_error
        self._close_error = close_error
        self.open_calls = 0
        self.close_calls = 0
        self.read_sizes: list[int] = []

    @property
    def is_open(self) -> bool:
        """Return the fake connection state."""
        return self._is_open

    def open(self) -> None:
        """Open the fake connection or raise its configured error."""
        self.open_calls += 1
        if self._open_error is not None:
            raise self._open_error
        self._is_open = True

    def close(self) -> None:
        """Close the fake connection or raise its configured error."""
        self.close_calls += 1
        if self._close_error is not None:
            error = self._close_error
            self._close_error = None
            raise error
        self._is_open = False

    def read(self, size: int = 1) -> bytes:
        """Return the next queued chunk or simulate a timeout."""
        self.read_sizes.append(size)
        if self._read_error is not None:
            raise self._read_error
        if not self._chunks:
            return b""
        return self._chunks.popleft()


class _FakeSerialFactory:
    """Record serial settings and return one configured fake connection."""

    def __init__(
        self,
        connection: _FakeSerialConnection | None = None,
        *,
        open_error: serial.SerialException | None = None,
    ) -> None:
        self.connection = connection or _FakeSerialConnection()
        self.open_error = open_error
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        port: str,
        baudrate: int,
        timeout: float | None,
    ) -> _FakeSerialConnection:
        """Return the fake connection after recording constructor settings."""
        self.calls.append(
            {"port": port, "baudrate": baudrate, "timeout": timeout}
        )
        if self.open_error is not None:
            raise self.open_error
        return self.connection


def test_constructor_does_not_open_serial_device() -> None:
    factory = _FakeSerialFactory()

    device = TFMiniPlusSerialDevice("fake-port", serial_factory=factory)

    assert device.is_open is False
    assert factory.calls == []


def test_open_and_close_serial_device() -> None:
    connection = _FakeSerialConnection(initially_open=False)
    factory = _FakeSerialFactory(connection)
    device = TFMiniPlusSerialDevice("fake-port", serial_factory=factory)

    device.open()

    assert device.is_open is True
    assert connection.open_calls == 1

    device.close()

    assert device.is_open is False
    assert connection.close_calls == 1


def test_repeated_open_does_not_create_another_connection() -> None:
    factory = _FakeSerialFactory()
    device = TFMiniPlusSerialDevice("fake-port", serial_factory=factory)

    device.open()
    device.open()

    assert len(factory.calls) == 1


def test_context_manager_opens_and_closes_device() -> None:
    connection = _FakeSerialConnection()
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )

    with device as opened_device:
        assert opened_device is device
        assert opened_device.is_open is True

    assert device.is_open is False
    assert connection.close_calls == 1


def test_repeated_close_is_safe() -> None:
    connection = _FakeSerialConnection()
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    device.close()
    device.close()

    assert connection.close_calls == 1


def test_read_before_open_raises_project_exception() -> None:
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory()
    )

    with pytest.raises(
        TFMiniPlusSerialNotOpenError,
        match="must be opened before reading",
    ):
        device.read_measurements()


def test_timeout_or_empty_read_returns_empty_list() -> None:
    connection = _FakeSerialConnection([b""])
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    assert device.read_measurements() == []


def test_complete_packet_is_delegated_to_parser() -> None:
    connection = _FakeSerialConnection([_make_frame()])
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    measurements = device.read_measurements()

    assert measurements == [
        TFMiniPlusMeasurement(
            distance_cm=300,
            strength=1200,
            chip_temperature_c=25.0,
        )
    ]
    assert connection.read_sizes == [256]


def test_packet_split_across_reads_is_completed_later() -> None:
    packet = _make_frame(distance_cm=432)
    connection = _FakeSerialConnection([packet[:4], packet[4:]])
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    assert device.read_measurements() == []
    measurements = device.read_measurements()

    assert [measurement.distance_cm for measurement in measurements] == [432]


def test_multiple_packets_in_one_read_return_multiple_measurements() -> None:
    first = _make_frame(distance_cm=100)
    second = _make_frame(distance_cm=200)
    connection = _FakeSerialConnection([first + second])
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    measurements = device.read_measurements()

    assert [measurement.distance_cm for measurement in measurements] == [100, 200]


def test_serial_factory_open_error_is_converted() -> None:
    cause = serial.SerialException("port unavailable")
    factory = _FakeSerialFactory(open_error=cause)
    device = TFMiniPlusSerialDevice("fake-port", serial_factory=factory)

    with pytest.raises(TFMiniPlusSerialOpenError) as error:
        device.open()

    assert error.value.__cause__ is cause
    assert device.is_open is False


def test_closed_factory_connection_open_error_is_converted() -> None:
    cause = serial.SerialException("open failed")
    connection = _FakeSerialConnection(
        initially_open=False,
        open_error=cause,
    )
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )

    with pytest.raises(TFMiniPlusSerialOpenError) as error:
        device.open()

    assert error.value.__cause__ is cause
    assert device.is_open is False


def test_serial_read_error_is_converted() -> None:
    cause = serial.SerialException("read failed")
    connection = _FakeSerialConnection(read_error=cause)
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    with pytest.raises(TFMiniPlusSerialReadError) as error:
        device.read_measurements()

    assert error.value.__cause__ is cause


def test_serial_close_error_is_converted_and_close_can_be_retried() -> None:
    cause = serial.SerialException("close failed")
    connection = _FakeSerialConnection(close_error=cause)
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    with pytest.raises(TFMiniPlusSerialCloseError) as error:
        device.close()

    assert error.value.__cause__ is cause
    assert device.is_open is True
    device.close()
    assert device.is_open is False
    device.close()


def test_parser_buffer_is_retained_across_reads() -> None:
    first = _make_frame(distance_cm=111)
    second = _make_frame(distance_cm=222)
    connection = _FakeSerialConnection([first + second[:3], second[3:]])
    device = TFMiniPlusSerialDevice(
        "fake-port", serial_factory=_FakeSerialFactory(connection)
    )
    device.open()

    first_measurements = device.read_measurements()
    second_measurements = device.read_measurements()

    assert [item.distance_cm for item in first_measurements] == [111]
    assert [item.distance_cm for item in second_measurements] == [222]


def test_custom_serial_settings_are_passed_to_factory() -> None:
    factory = _FakeSerialFactory()
    device = TFMiniPlusSerialDevice(
        "custom-port",
        baudrate=230400,
        timeout=0.25,
        serial_factory=factory,
    )

    device.open()

    assert factory.calls == [
        {"port": "custom-port", "baudrate": 230400, "timeout": 0.25}
    ]


def test_default_baudrate_is_115200() -> None:
    factory = _FakeSerialFactory()
    device = TFMiniPlusSerialDevice("fake-port", serial_factory=factory)

    device.open()

    assert factory.calls[0]["baudrate"] == 115200


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"port": ""}, "port must not be empty"),
        ({"port": "fake-port", "baudrate": 0}, "baudrate"),
        ({"port": "fake-port", "timeout": -0.1}, "timeout"),
    ],
)
def test_invalid_serial_settings_are_rejected(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        TFMiniPlusSerialDevice(**kwargs)  # type: ignore[arg-type]
