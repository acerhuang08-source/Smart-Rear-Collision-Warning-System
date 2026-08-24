"""Application service connecting measurements to warning outputs."""

from __future__ import annotations

from rear_warning.outputs.led import LedDriver
from rear_warning.sensors.tfmini_plus import TFMiniPlusMeasurement

from .models import WarningState
from .policy import WarningPolicy


class WarningController:
    """Apply the policy to one measurement and update an injected driver."""

    def __init__(self, policy: WarningPolicy, led_driver: LedDriver) -> None:
        self._policy = policy
        self._led_driver = led_driver

    def process(
        self, measurement: TFMiniPlusMeasurement | None
    ) -> WarningState:
        """Display and return the classification for one measurement."""
        state = self._policy.classify(measurement)
        self._led_driver.display(state)
        return state
