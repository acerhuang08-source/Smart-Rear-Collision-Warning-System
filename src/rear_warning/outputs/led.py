"""Hardware-library-independent three-colour LED driver."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from rear_warning.warning import WarningState


class DigitalOutput(Protocol):
    """Minimal GPIO adapter required by :class:`ThreeColorLedDriver`."""

    def write(self, pin: int, active: bool) -> None:
        """Set one configured output pin active or inactive."""
        ...

class LedDriver(Protocol):
    """Output boundary consumed by the warning controller."""

    def display(self, state: WarningState) -> None:
        """Display one warning state."""
        ...


@dataclass(frozen=True, slots=True)
class LedPins:
    """Explicit GPIO pin assignment supplied by deployment configuration."""

    red: int
    yellow: int
    green: int

    @classmethod
    def raspberry_pi_default(cls) -> LedPins:
        """Return the documented BCM pin assignment for this project."""
        return cls(red=17, yellow=27, green=22)

    def __post_init__(self) -> None:
        pins = (self.red, self.yellow, self.green)
        if any(isinstance(pin, bool) or not isinstance(pin, int) for pin in pins):
            raise TypeError("LED pins must be integers")
        if any(pin < 0 for pin in pins):
            raise ValueError("LED pins must be non-negative")
        if len(set(pins)) != len(pins):
            raise ValueError("LED pins must be distinct")


class ThreeColorLedDriver:
    """Map warning states to red, yellow, and green digital outputs.

    ``SENSOR_FAULT`` lights red and yellow together so invalid sensor data is
    visibly distinct from every normal distance classification.
    """

    _ACTIVE_COLOURS = {
        WarningState.SAFE: (False, False, True),
        WarningState.WARNING: (False, True, False),
        WarningState.DANGER: (True, False, False),
        WarningState.SENSOR_FAULT: (True, True, False),
    }

    def __init__(self, output: DigitalOutput, pins: LedPins) -> None:
        self._output = output
        self._pins = pins

    def display(self, state: WarningState) -> None:
        """Write a complete LED pattern for ``state``."""
        if not isinstance(state, WarningState):
            raise TypeError("state must be a WarningState")
        red, yellow, green = self._ACTIVE_COLOURS[state]
        self._output.write(self._pins.red, red)
        self._output.write(self._pins.yellow, yellow)
        self._output.write(self._pins.green, green)

    def all_off(self) -> None:
        """Deactivate every LED."""
        self._output.write(self._pins.red, False)
        self._output.write(self._pins.yellow, False)
        self._output.write(self._pins.green, False)
