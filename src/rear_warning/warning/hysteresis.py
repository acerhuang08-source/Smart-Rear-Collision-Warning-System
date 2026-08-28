"""Stateful, I/O-free hysteresis for distance warning states."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real

from .models import WarningState
from .policy import DistanceThresholds


@dataclass(frozen=True, slots=True)
class HysteresisThresholds:
    """Configurable release thresholds, expressed in metres."""

    danger_release_m: float = 1.7
    safe_release_m: float = 3.2

    def __post_init__(self) -> None:
        """Reject intrinsically invalid release-threshold values."""
        values = (self.danger_release_m, self.safe_release_m)
        if any(
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not math.isfinite(float(value))
            for value in values
        ):
            raise ValueError("hysteresis thresholds must be finite numbers")
        if any(value < 0 for value in values):
            raise ValueError("hysteresis thresholds must be non-negative")
        if self.danger_release_m > self.safe_release_m:
            raise ValueError(
                "danger_release_m must not exceed safe_release_m"
            )

    def validate_for(self, entry: DistanceThresholds) -> None:
        """Validate release thresholds against policy entry thresholds."""
        if self.danger_release_m < entry.danger_below_m:
            raise ValueError(
                "danger_release_m must not be below danger entry threshold"
            )
        if self.safe_release_m < entry.safe_above_m:
            raise ValueError(
                "safe_release_m must not be below the safe entry boundary"
            )
        if self.safe_release_m >= entry.maximum_valid_m:
            raise ValueError(
                "safe_release_m must be below the maximum valid distance"
            )


class WarningStateStabilizer:
    """Apply hysteresis to policy states without performing any I/O."""

    def __init__(
        self,
        entry_thresholds: DistanceThresholds,
        release_thresholds: HysteresisThresholds | None = None,
        *,
        initial_state: WarningState = WarningState.SENSOR_FAULT,
    ) -> None:
        if not isinstance(initial_state, WarningState):
            raise TypeError("initial_state must be a WarningState")
        self._entry_thresholds = entry_thresholds
        self._release_thresholds = (
            release_thresholds or HysteresisThresholds()
        )
        self._release_thresholds.validate_for(entry_thresholds)
        self._state = initial_state

    @property
    def state(self) -> WarningState:
        """Return the current stabilized state."""
        return self._state

    @property
    def entry_thresholds(self) -> DistanceThresholds:
        """Return the immutable policy thresholds used for validation."""
        return self._entry_thresholds

    @property
    def release_thresholds(self) -> HysteresisThresholds:
        """Return the immutable release-threshold configuration."""
        return self._release_thresholds

    def stabilize(
        self, candidate: WarningState, distance_cm: object
    ) -> WarningState:
        """Return and retain the stabilized state for one policy result."""
        if not isinstance(candidate, WarningState):
            raise TypeError("candidate must be a WarningState")
        if candidate is WarningState.SENSOR_FAULT:
            self._state = WarningState.SENSOR_FAULT
            return self._state

        distance_m = _valid_distance_metres(distance_cm)
        current = self._state

        if current is WarningState.SENSOR_FAULT:
            self._state = candidate
        elif candidate is WarningState.DANGER:
            self._state = WarningState.DANGER
        elif current is WarningState.DANGER:
            if distance_m < self._release_thresholds.danger_release_m:
                self._state = WarningState.DANGER
            elif distance_m <= self._release_thresholds.safe_release_m:
                self._state = WarningState.WARNING
            else:
                self._state = WarningState.SAFE
        elif current is WarningState.WARNING:
            if (
                candidate is not WarningState.SAFE
                or distance_m > self._release_thresholds.safe_release_m
            ):
                self._state = candidate
        else:
            self._state = candidate

        return self._state


def _valid_distance_metres(distance_cm: object) -> float:
    """Validate a distance paired with a non-fault policy classification."""
    if (
        isinstance(distance_cm, bool)
        or not isinstance(distance_cm, Real)
        or not math.isfinite(float(distance_cm))
        or float(distance_cm) <= 0
    ):
        raise ValueError(
            "a non-fault candidate requires a positive finite distance"
        )
    return float(distance_cm) / 100.0
