"""UART device boundary for the Benewake TFMini Plus.

pySerial API reference:
    https://pyserial.readthedocs.io/en/latest/pyserial_api.html

This module owns serial-port lifecycle and byte reads only. It deliberately
delegates all framing, checksum validation, and field decoding to
``TFMiniPlusParser``. Constructing or importing the class never opens a port.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

import serial

from .models import TFMiniPlusMeasurement
from .parser import TFMiniPlusParser

_DEFAULT_BAUDRATE = 115200
_DEFAULT_TIMEOUT_SECONDS = 0.1
_READ_SIZE = 256


class TFMiniPlusSerialError(Exception):
    """Base exception for TFMini Plus serial-device failures."""


class TFMiniPlusSerialNotOpenError(TFMiniPlusSerialError):
    """Raised when a read is requested before the serial device is open."""


class TFMiniPlusSerialOpenError(TFMiniPlusSerialError):
    """Raised when pySerial cannot open the configured serial device."""


class TFMiniPlusSerialReadError(TFMiniPlusSerialError):
    """Raised when pySerial reports a failure while reading bytes."""


class TFMiniPlusSerialCloseError(TFMiniPlusSerialError):
    """Raised when pySerial reports a failure while closing the device."""


class _SerialConnection(Protocol):
    """Minimum serial connection surface used by the device layer."""

    @property
    def is_open(self) -> bool:
        """Return whether the connection is open."""
        ...

    def open(self) -> None:
        """Open the configured serial connection."""
        ...

    def close(self) -> None:
        """Close the serial connection."""
        ...

    def read(self, size: int = 1) -> bytes:
        """Read up to ``size`` bytes."""
        ...


class _SerialFactory(Protocol):
    """Factory contract used to create real or fake serial connections."""

    def __call__(
        self,
        *,
        port: str,
        baudrate: int,
        timeout: float | None,
    ) -> _SerialConnection:
        """Create a serial connection with the requested settings."""
        ...


def _default_serial_factory(
    *, port: str, baudrate: int, timeout: float | None
) -> _SerialConnection:
    """Create pySerial's native connection; a supplied port opens immediately."""
    return serial.Serial(port=port, baudrate=baudrate, timeout=timeout)


class TFMiniPlusSerialDevice:
    """Read TFMini Plus bytes from one serial port and delegate parsing.

    Args:
        port: Platform-specific serial device name or URL.
        baudrate: Serial baud rate. TFMini Plus defaults to 115200.
        timeout: pySerial read timeout in seconds. ``None`` enables blocking
            reads, following pySerial semantics.
        serial_factory: Optional injectable factory for hardware-free tests.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = _DEFAULT_BAUDRATE,
        timeout: float | None = _DEFAULT_TIMEOUT_SECONDS,
        *,
        serial_factory: _SerialFactory | None = None,
    ) -> None:
        """Configure a device without opening the serial port."""
        if not port:
            raise ValueError("port must not be empty")
        if baudrate <= 0:
            raise ValueError("baudrate must be greater than zero")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout must be non-negative or None")

        self._port = port
        self._baudrate = baudrate
        self._timeout = timeout
        self._serial_factory = serial_factory or _default_serial_factory
        self._connection: _SerialConnection | None = None
        self._parser = TFMiniPlusParser()

    @property
    def is_open(self) -> bool:
        """Return whether this device currently owns an open connection."""
        return self._connection is not None and self._connection.is_open

    def open(self) -> None:
        """Open the configured serial device.

        Repeated calls while the device is already open are harmless.

        Raises:
            TFMiniPlusSerialOpenError: If pySerial reports an open failure.
        """
        if self.is_open:
            return

        if self._connection is not None:
            self._connection = None
            self._parser.reset()

        connection: _SerialConnection | None = None
        try:
            connection = self._serial_factory(
                port=self._port,
                baudrate=self._baudrate,
                timeout=self._timeout,
            )
            if not connection.is_open:
                connection.open()
        except serial.SerialException as exc:
            if connection is not None:
                try:
                    connection.close()
                except serial.SerialException:
                    pass
            raise TFMiniPlusSerialOpenError(
                f"failed to open TFMini Plus serial device {self._port!r}"
            ) from exc

        self._connection = connection

    def close(self) -> None:
        """Close the serial device and discard any partial packet.

        Calling this method repeatedly is safe.

        Raises:
            TFMiniPlusSerialCloseError: If pySerial reports a close failure.
        """
        connection = self._connection
        if connection is None:
            self._parser.reset()
            return

        if not connection.is_open:
            self._connection = None
            self._parser.reset()
            return

        try:
            connection.close()
        except serial.SerialException as exc:
            raise TFMiniPlusSerialCloseError(
                f"failed to close TFMini Plus serial device {self._port!r}"
            ) from exc

        self._connection = None
        self._parser.reset()

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        """Perform one serial read and return all completed measurements.

        A timeout or empty read returns an empty list. Partial packet bytes stay
        in the parser until a later call completes the frame.

        Raises:
            TFMiniPlusSerialNotOpenError: If the device is not open.
            TFMiniPlusSerialReadError: If pySerial reports a read failure.
        """
        connection = self._connection
        if connection is None or not connection.is_open:
            raise TFMiniPlusSerialNotOpenError(
                "TFMini Plus serial device must be opened before reading"
            )

        try:
            data = connection.read(_READ_SIZE)
        except serial.SerialException as exc:
            raise TFMiniPlusSerialReadError(
                f"failed to read TFMini Plus serial device {self._port!r}"
            ) from exc

        if not data:
            return []
        return self._parser.feed(data)

    def __enter__(self) -> Self:
        """Open and return this device for use in a context manager."""
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the device without suppressing exceptions."""
        self.close()
