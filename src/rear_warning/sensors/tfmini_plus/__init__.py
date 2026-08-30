"""TFMini Plus models/parser with lazy serial-device exports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .models import TFMiniPlusMeasurement
from .parser import TFMiniPlusParser

if TYPE_CHECKING:
    from .serial_device import (
        TFMiniPlusSerialCloseError, TFMiniPlusSerialDevice,
        TFMiniPlusSerialError, TFMiniPlusSerialNotOpenError,
        TFMiniPlusSerialOpenError, TFMiniPlusSerialReadError,
    )


_SERIAL_EXPORTS = {
    "TFMiniPlusSerialCloseError",
    "TFMiniPlusSerialDevice",
    "TFMiniPlusSerialError",
    "TFMiniPlusSerialNotOpenError",
    "TFMiniPlusSerialOpenError",
    "TFMiniPlusSerialReadError",
}


def __getattr__(name: str) -> Any:
    if name not in _SERIAL_EXPORTS:
        raise AttributeError(name)
    from . import serial_device

    value = getattr(serial_device, name)
    globals()[name] = value
    return value

__all__ = [
    "TFMiniPlusMeasurement",
    "TFMiniPlusParser",
    "TFMiniPlusSerialCloseError",
    "TFMiniPlusSerialDevice",
    "TFMiniPlusSerialError",
    "TFMiniPlusSerialNotOpenError",
    "TFMiniPlusSerialOpenError",
    "TFMiniPlusSerialReadError",
]
