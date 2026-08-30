"""Stateful but I/O-free causal latest-sample matching."""

from __future__ import annotations

import math

from rear_warning.sensors.bno055.quality import assess_measurement_quality

from .models import (
    SensorSyncStatus, SynchronizedSensorSample, TimedDistanceMeasurement,
    TimedMotionMeasurement,
)


class InvalidMotionMeasurement(ValueError):
    """A motion update failed quality validation and was not committed."""


def _valid_distance(measurement: object) -> bool:
    try:
        distance = measurement.distance_cm  # type: ignore[attr-defined]
        strength = measurement.strength  # type: ignore[attr-defined]
        temperature = measurement.chip_temperature_c  # type: ignore[attr-defined]
    except AttributeError:
        return False
    return (
        not isinstance(distance, bool)
        and isinstance(distance, int)
        and distance > 0
        and not isinstance(strength, bool)
        and isinstance(strength, int)
        and strength >= 0
        and not isinstance(temperature, bool)
        and isinstance(temperature, (int, float))
        and math.isfinite(temperature)
    )


class SensorSynchronizer:
    """Associate each distance with the latest non-future motion sample."""

    def __init__(self, *, maximum_motion_age_seconds: float = 0.2) -> None:
        if (
            isinstance(maximum_motion_age_seconds, bool)
            or not isinstance(maximum_motion_age_seconds, (int, float))
        ):
            raise TypeError("maximum_motion_age_seconds must be a number")
        if not math.isfinite(maximum_motion_age_seconds) or maximum_motion_age_seconds <= 0:
            raise ValueError("maximum_motion_age_seconds must be positive and finite")
        self.maximum_motion_age_seconds = float(maximum_motion_age_seconds)
        self._latest_motion: TimedMotionMeasurement | None = None

    def update_motion(self, motion: TimedMotionMeasurement) -> bool:
        assessment = assess_measurement_quality(motion.measurement)
        if not assessment.is_valid:
            raise InvalidMotionMeasurement(
                "invalid motion measurement: "
                + "; ".join(assessment.state_issues + assessment.data_issues)
            )
        if (
            self._latest_motion is not None
            and motion.captured_at_monotonic_seconds
            < self._latest_motion.captured_at_monotonic_seconds
        ):
            return False
        self._latest_motion = motion
        return True

    def associate(
        self, distance: TimedDistanceMeasurement
    ) -> SynchronizedSensorSample:
        if not _valid_distance(distance.measurement):
            return self._unmatched(distance, SensorSyncStatus.INVALID_DISTANCE_SAMPLE)
        motion = self._latest_motion
        if motion is None:
            return self._unmatched(distance, SensorSyncStatus.NO_MOTION_SAMPLE)
        assessment = assess_measurement_quality(motion.measurement)
        if not assessment.is_valid:
            return self._unmatched(
                distance,
                SensorSyncStatus.INVALID_MOTION_SAMPLE,
                motion=motion,
            )
        age = (
            distance.captured_at_monotonic_seconds
            - motion.captured_at_monotonic_seconds
        )
        if age < 0:
            return self._unmatched(
                distance,
                SensorSyncStatus.FUTURE_MOTION_SAMPLE,
                motion=motion,
            )
        boundary_tolerance = max(
            1e-12,
            4 * math.ulp(self.maximum_motion_age_seconds),
        )
        if age - self.maximum_motion_age_seconds > boundary_tolerance:
            return self._unmatched(
                distance,
                SensorSyncStatus.STALE_MOTION_SAMPLE,
                motion=motion,
            )
        return SynchronizedSensorSample(
            distance.measurement,
            motion.measurement,
            distance.captured_at_monotonic_seconds,
            motion.captured_at_monotonic_seconds,
            age,
            SensorSyncStatus.MATCHED,
        )

    @staticmethod
    def _unmatched(
        distance: TimedDistanceMeasurement,
        status: SensorSyncStatus,
        *,
        motion: TimedMotionMeasurement | None = None,
    ) -> SynchronizedSensorSample:
        return SynchronizedSensorSample(
            distance.measurement,
            None if motion is None else motion.measurement,
            distance.captured_at_monotonic_seconds,
            None if motion is None else motion.captured_at_monotonic_seconds,
            None,
            status,
        )
