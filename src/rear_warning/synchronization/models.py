"""I/O-free models for causal TFMini Plus/BNO055 time association."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rear_warning.sensors.bno055.models import BNO055Measurement
    from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement


def _timestamp(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return float(value)


@dataclass(frozen=True, slots=True)
class TimedDistanceMeasurement:
    measurement: TFMiniPlusMeasurement
    captured_at_monotonic_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "captured_at_monotonic_seconds",
            _timestamp(
                "captured_at_monotonic_seconds",
                self.captured_at_monotonic_seconds,
            ),
        )


@dataclass(frozen=True, slots=True)
class TimedMotionMeasurement:
    measurement: BNO055Measurement
    captured_at_monotonic_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "captured_at_monotonic_seconds",
            _timestamp(
                "captured_at_monotonic_seconds",
                self.captured_at_monotonic_seconds,
            ),
        )


class SensorSyncStatus(str, Enum):
    MATCHED = "matched"
    NO_MOTION_SAMPLE = "no_motion_sample"
    STALE_MOTION_SAMPLE = "stale_motion_sample"
    FUTURE_MOTION_SAMPLE = "future_motion_sample"
    INVALID_DISTANCE_SAMPLE = "invalid_distance_sample"
    INVALID_MOTION_SAMPLE = "invalid_motion_sample"


@dataclass(frozen=True, slots=True)
class SynchronizedSensorSample:
    distance_measurement: TFMiniPlusMeasurement
    motion_measurement: BNO055Measurement | None
    distance_timestamp: float
    motion_timestamp: float | None
    motion_age_seconds: float | None
    status: SensorSyncStatus

    def __post_init__(self) -> None:
        if not isinstance(self.status, SensorSyncStatus):
            raise TypeError("status must be a SensorSyncStatus")
        object.__setattr__(
            self,
            "distance_timestamp",
            _timestamp("distance_timestamp", self.distance_timestamp),
        )
        if self.motion_timestamp is not None:
            object.__setattr__(
                self,
                "motion_timestamp",
                _timestamp("motion_timestamp", self.motion_timestamp),
            )
        if self.motion_age_seconds is not None:
            object.__setattr__(
                self,
                "motion_age_seconds",
                _timestamp("motion_age_seconds", self.motion_age_seconds),
            )

        if self.status is SensorSyncStatus.MATCHED:
            if (
                self.motion_measurement is None
                or self.motion_timestamp is None
                or self.motion_age_seconds is None
            ):
                raise ValueError("MATCHED requires motion measurement, timestamp, and age")
            expected_age = self.distance_timestamp - self.motion_timestamp
            if expected_age < 0 or not math.isclose(
                self.motion_age_seconds, expected_age, abs_tol=1e-12
            ):
                raise ValueError("MATCHED age must equal the causal timestamp difference")
        elif self.motion_age_seconds is not None:
            raise ValueError("unmatched statuses must not contain motion age")
