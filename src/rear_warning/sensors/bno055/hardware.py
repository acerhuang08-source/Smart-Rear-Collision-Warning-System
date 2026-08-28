"""Lazy loader for the optional smbus2 BNO055 adapter."""

from __future__ import annotations

from collections.abc import Callable


class BNO055HardwareDependenciesNotInstalled(RuntimeError):
    """Raised when confirmed hardware access lacks smbus2."""


def load_smbus_register_io() -> Callable[..., object]:
    try:
        from .smbus import SMBusRegisterIO
    except ModuleNotFoundError as exc:
        if exc.name != "smbus2":
            raise
        raise BNO055HardwareDependenciesNotInstalled(
            "BNO055 I2C dependency is not installed.\n"
            'Install with: pip install -e ".[hardware]"'
        ) from exc
    return SMBusRegisterIO
