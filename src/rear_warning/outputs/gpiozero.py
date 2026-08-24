"""gpiozero-backed physical digital outputs for Raspberry Pi deployments."""

from __future__ import annotations

import atexit
from collections.abc import Callable
from typing import Protocol

from .led import LedPins


class _OutputDevice(Protocol):
    """Subset of gpiozero.OutputDevice used by the adapter."""

    def on(self) -> None: ...

    def off(self) -> None: ...

    def close(self) -> None: ...


class GpioZeroDigitalOutput:
    """Own three active-high GPIO outputs using gpiozero's lgpio backend."""

    def __init__(
        self,
        pins: LedPins,
        *,
        chip: int = 0,
        pin_factory: object | None = None,
        device_factory: Callable[..., _OutputDevice] | None = None,
    ) -> None:
        if isinstance(chip, bool) or not isinstance(chip, int):
            raise TypeError("chip must be an integer")
        if chip < 0:
            raise ValueError("chip must be non-negative")

        if pin_factory is None or device_factory is None:
            from gpiozero import OutputDevice
            from gpiozero.pins.lgpio import LGPIOFactory

            if pin_factory is None:
                pin_factory = LGPIOFactory(chip=chip)
            if device_factory is None:
                device_factory = OutputDevice

        self._pins = pins
        self._pin_factory = pin_factory
        self._devices: dict[int, _OutputDevice] = {}
        self._closed = False

        try:
            for pin in (pins.red, pins.yellow, pins.green):
                self._devices[pin] = device_factory(
                    pin,
                    active_high=True,
                    initial_value=False,
                    pin_factory=self._pin_factory,
                )
        except BaseException as construction_error:
            try:
                self.close()
            except BaseException as cleanup_error:
                construction_error.add_note(
                    "GPIO cleanup also failed after adapter construction "
                    f"failed: {cleanup_error!r}"
                )
            raise

        atexit.register(self.close)

    def write(self, pin: int, active: bool) -> None:
        """Set a configured BCM pin, rejecting use after closure."""
        if self._closed:
            raise RuntimeError("digital output is closed")
        if not isinstance(active, bool):
            raise TypeError("active must be a bool")
        try:
            device = self._devices[pin]
        except KeyError as exc:
            raise ValueError(f"pin {pin} is not configured for an LED") from exc
        device.on() if active else device.off()

    def close(self) -> None:
        """Best-effort deactivate and close every device and pin factory."""
        if self._closed:
            return
        self._closed = True
        first_error: BaseException | None = None

        for device in self._devices.values():
            try:
                device.off()
            except BaseException as exc:
                first_error = first_error or exc
            try:
                device.close()
            except BaseException as exc:
                first_error = first_error or exc

        close_factory = getattr(self._pin_factory, "close", None)
        if callable(close_factory):
            try:
                close_factory()
            except BaseException as exc:
                first_error = first_error or exc

        atexit.unregister(self.close)
        if first_error is not None:
            raise first_error

    def __enter__(self) -> GpioZeroDigitalOutput:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
