"""Lazy loading for optional Raspberry Pi output dependencies."""

from __future__ import annotations

from collections.abc import Callable


class HardwareDependenciesNotInstalled(RuntimeError):
    """Raised when a confirmed hardware command lacks its optional extra."""


def load_gpiozero_output_factory() -> Callable[..., object]:
    """Load the gpiozero adapter without affecting software-only imports."""
    try:
        import gpiozero  # noqa: F401
        import lgpio  # noqa: F401
        import gpiozero.pins.lgpio  # noqa: F401

        from .gpiozero import GpioZeroDigitalOutput
    except ModuleNotFoundError as exc:
        if exc.name not in {"gpiozero", "lgpio", "_lgpio"}:
            raise
        raise HardwareDependenciesNotInstalled(
            "Raspberry Pi GPIO dependencies are not installed.\n"
            'Install with: pip install -e ".[hardware]"'
        ) from exc
    return GpioZeroDigitalOutput
