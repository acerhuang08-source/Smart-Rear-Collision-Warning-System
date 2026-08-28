"""Application service connecting measurements to warning outputs."""

from __future__ import annotations

from rear_warning.outputs.led import LedDriver
from rear_warning.sensors.tfmini_plus import TFMiniPlusMeasurement

from .hysteresis import WarningStateStabilizer
from .models import WarningState
from .policy import WarningPolicy


class WarningController:
    """Apply the policy to one measurement and update an injected driver."""

    def __init__(
        self,
        policy: WarningPolicy,
        led_driver: LedDriver,
        stabilizer: WarningStateStabilizer | None = None,
    ) -> None:
        if (
            stabilizer is not None
            and stabilizer.entry_thresholds != policy.thresholds
        ):
            raise ValueError(
                "stabilizer entry thresholds must match policy thresholds"
            )
        self._policy = policy
        self._led_driver = led_driver
        self._stabilizer = (
            stabilizer or WarningStateStabilizer(policy.thresholds)
        )

    def process(
        self, measurement: TFMiniPlusMeasurement | None
    ) -> WarningState:
        """Display and return the classification for one measurement."""
        candidate = self._policy.classify(measurement)
        distance_cm = None if measurement is None else measurement.distance_cm
        state = self._stabilizer.stabilize(candidate, distance_cm)
        self._led_driver.display(state)
        return state
