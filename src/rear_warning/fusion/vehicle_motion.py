"""I/O-free stationary/moving estimate for the host vehicle from BNO055 data.

The estimate uses sustained vibration (the per-axis standard deviation of the
BNO055 linear acceleration over a short window). Accelerations alone cannot
observe constant-velocity travel, so a smooth ride at constant speed may look
calm. The defaults are therefore deliberately conservative and untuned on a
real vehicle: ``STATIONARY`` needs a long calm period, any sustained vibration
leaves it quickly, and every missing, stale or invalid input yields
``UNKNOWN``. Callers must treat anything other than ``STATIONARY`` as "not
known to be stationary".

Default thresholds come from the still model on a table on 2026-11-25, where
the 2 s vibration was about 0.05 m/s^2 (p99 0.06-0.11, max 0.26 when bumped).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import Enum

from rear_warning.sensors.bno055.models import Vector3


class VehicleMotionState(str, Enum):
    UNKNOWN = "unknown"
    STATIONARY = "stationary"
    MOVING = "moving"


@dataclass(frozen=True, slots=True)
class VehicleMotionThresholds:
    window_seconds: float = 2.0
    minimum_samples: int = 10
    maximum_sample_gap_seconds: float = 0.5
    stale_after_seconds: float = 0.5
    stationary_vibration_mps2: float = 0.15
    stationary_confirm_seconds: float = 10.0
    moving_vibration_mps2: float = 0.30
    leave_stationary_confirm_seconds: float = 0.3

    def __post_init__(self) -> None:
        for name in (
            "window_seconds",
            "maximum_sample_gap_seconds",
            "stale_after_seconds",
            "stationary_vibration_mps2",
            "stationary_confirm_seconds",
            "moving_vibration_mps2",
            "leave_stationary_confirm_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
            object.__setattr__(self, name, float(value))
        if (
            isinstance(self.minimum_samples, bool)
            or not isinstance(self.minimum_samples, int)
            or self.minimum_samples < 2
        ):
            raise ValueError("minimum_samples must be an integer >= 2")
        if self.stationary_vibration_mps2 >= self.moving_vibration_mps2:
            raise ValueError(
                "stationary_vibration_mps2 must be below moving_vibration_mps2"
            )


@dataclass(frozen=True, slots=True)
class VehicleMotionSnapshot:
    state: VehicleMotionState
    vibration_mps2: float | None
    sample_count: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "vibration_mps2": self.vibration_mps2,
            "sample_count": self.sample_count,
            "reason": self.reason,
        }


def _finite(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


class VehicleMotionEstimator:
    """Track a hysteretic UNKNOWN/STATIONARY/MOVING state from timed samples."""

    def __init__(self, thresholds: VehicleMotionThresholds | None = None) -> None:
        self.thresholds = thresholds or VehicleMotionThresholds()
        self._samples: deque[tuple[float, float, float, float]] = deque()
        self._state = VehicleMotionState.UNKNOWN
        self._reason = "no_samples"
        self._vibration: float | None = None
        self._last_timestamp: float | None = None
        self._calm_since: float | None = None
        self._active_since: float | None = None

    def reset(self, reason: str) -> None:
        self._samples.clear()
        self._state = VehicleMotionState.UNKNOWN
        self._reason = reason
        self._vibration = None
        self._calm_since = None
        self._active_since = None

    def note_invalid(self, timestamp: float) -> None:
        """An invalid BNO055 sample discards all evidence."""
        stamp = _finite("timestamp", timestamp)
        self.reset("invalid_motion_sample")
        if self._last_timestamp is None or stamp > self._last_timestamp:
            self._last_timestamp = stamp

    def update(self, timestamp: float, linear_acceleration: Vector3) -> VehicleMotionSnapshot:
        stamp = _finite("timestamp", timestamp)
        if not isinstance(linear_acceleration, Vector3):
            raise TypeError("linear_acceleration must be a Vector3")
        axes = tuple(
            _finite(f"linear_acceleration.{axis}", getattr(linear_acceleration, axis))
            for axis in ("x", "y", "z")
        )
        if self._last_timestamp is not None and stamp <= self._last_timestamp:
            return self.snapshot(self._last_timestamp)
        if (
            self._last_timestamp is not None
            and stamp - self._last_timestamp > self.thresholds.maximum_sample_gap_seconds
        ):
            self.reset("sample_gap")
        self._last_timestamp = stamp
        self._samples.append((stamp, *axes))
        horizon = stamp - self.thresholds.window_seconds
        while self._samples and self._samples[0][0] < horizon:
            self._samples.popleft()

        if len(self._samples) < self.thresholds.minimum_samples:
            self._vibration = None
            self._calm_since = None
            self._active_since = None
            if self._state is VehicleMotionState.UNKNOWN:
                self._reason = "warming_up"
            return self.snapshot(stamp)

        vibration = self._window_vibration()
        self._vibration = vibration
        limits = self.thresholds
        if vibration < limits.stationary_vibration_mps2:
            self._active_since = None
            if self._calm_since is None:
                self._calm_since = stamp
            if stamp - self._calm_since >= limits.stationary_confirm_seconds:
                self._state = VehicleMotionState.STATIONARY
                self._reason = "sustained_low_vibration"
            elif self._state is not VehicleMotionState.STATIONARY:
                self._reason = "confirming_stationary"
        else:
            self._calm_since = None
            if self._active_since is None:
                self._active_since = stamp
            if stamp - self._active_since >= limits.leave_stationary_confirm_seconds:
                if vibration >= limits.moving_vibration_mps2:
                    self._state = VehicleMotionState.MOVING
                    self._reason = "sustained_vibration"
                elif self._state is VehicleMotionState.STATIONARY:
                    self._state = VehicleMotionState.UNKNOWN
                    self._reason = "vibration_above_stationary"
        return self.snapshot(stamp)

    def snapshot(self, now: float) -> VehicleMotionSnapshot:
        """Return the state as seen at ``now``; stale input reads as UNKNOWN."""
        current = _finite("now", now)
        if self._last_timestamp is None:
            return VehicleMotionSnapshot(VehicleMotionState.UNKNOWN, None, 0, "no_samples")
        if current - self._last_timestamp > self.thresholds.stale_after_seconds:
            return VehicleMotionSnapshot(
                VehicleMotionState.UNKNOWN, None, len(self._samples), "motion_stale"
            )
        return VehicleMotionSnapshot(
            self._state,
            None if self._vibration is None else round(self._vibration, 4),
            len(self._samples),
            self._reason,
        )

    def _window_vibration(self) -> float:
        count = len(self._samples)
        total = 0.0
        for axis in (1, 2, 3):
            values = [sample[axis] for sample in self._samples]
            mean = sum(values) / count
            total += sum((value - mean) ** 2 for value in values) / count
        return math.sqrt(total)
