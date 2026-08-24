"""Distance-only warning policy for rear-collision warnings."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real

from rear_warning.sensors.tfmini_plus import TFMiniPlusMeasurement

from .models import WarningState


@dataclass(frozen=True, slots=True)
class DistanceThresholds:
    """Configurable distance thresholds, expressed in metres."""

    danger_below_m: float = 1.5
    safe_above_m: float = 3.0
    maximum_valid_m: float = 12.0

    def __post_init__(self) -> None:
        """Reject non-finite, non-positive, or unordered thresholds."""
        values = (
            self.danger_below_m,
            self.safe_above_m,
            self.maximum_valid_m,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("distance thresholds must be finite")
        if not 0 < self.danger_below_m <= self.safe_above_m:
            raise ValueError(
                "danger_below_m must be positive and no greater than "
                "safe_above_m"
            )
        if self.maximum_valid_m <= self.safe_above_m:
            raise ValueError("maximum_valid_m must be greater than safe_above_m")


class WarningPolicy:
    """Classify a TFMini Plus measurement without performing any I/O."""

    def __init__(self, thresholds: DistanceThresholds | None = None) -> None:
        self._thresholds = thresholds or DistanceThresholds()

    @property
    def thresholds(self) -> DistanceThresholds:
        """Return the immutable thresholds used by this policy."""
        return self._thresholds

    def classify(
        self, measurement: TFMiniPlusMeasurement | None
    ) -> WarningState:
        """Return a warning state; missing or invalid data is a sensor fault."""
        if measurement is None:
            return WarningState.SENSOR_FAULT
        return self.classify_distance_cm(measurement.distance_cm)

    def classify_distance_cm(self, distance_cm: object) -> WarningState:
        """Classify a raw distance value while defensively validating it."""
        if isinstance(distance_cm, bool) or not isinstance(distance_cm, Real):
            return WarningState.SENSOR_FAULT

        distance_m = float(distance_cm) / 100.0
        if (
            not math.isfinite(distance_m)
            or distance_m <= 0
            or distance_m > self._thresholds.maximum_valid_m
        ):
            return WarningState.SENSOR_FAULT
        if distance_m < self._thresholds.danger_below_m:
            return WarningState.DANGER
        if distance_m <= self._thresholds.safe_above_m:
            return WarningState.WARNING
        return WarningState.SAFE
