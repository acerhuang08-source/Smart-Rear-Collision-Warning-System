"""I/O-free causal association for radar ranges and BNO055 measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass

from rear_warning.sensors.bno055.models import BNO055Measurement
from rear_warning.sensors.bno055.quality import assess_measurement_quality

from .models import SensorSyncStatus, TimedMotionMeasurement
from .synchronizer import InvalidMotionMeasurement


@dataclass(frozen=True, slots=True)
class RadarMotionMatch:
    range_timestamp: float
    motion_measurement: BNO055Measurement | None
    motion_timestamp: float | None
    motion_age_seconds: float | None
    status: SensorSyncStatus


class RadarMotionSynchronizer:
    """Match a completed radar cycle to the latest causal valid motion."""

    def __init__(self, *, maximum_motion_age_seconds: float = 0.2) -> None:
        if isinstance(maximum_motion_age_seconds, bool) or not isinstance(
            maximum_motion_age_seconds, (int, float)
        ):
            raise TypeError("maximum_motion_age_seconds must be a number")
        if not math.isfinite(maximum_motion_age_seconds) or maximum_motion_age_seconds <= 0:
            raise ValueError("maximum_motion_age_seconds must be positive and finite")
        self.maximum_motion_age_seconds = float(maximum_motion_age_seconds)
        self._latest_motion: TimedMotionMeasurement | None = None
        self._latest_invalid_motion: TimedMotionMeasurement | None = None

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
        if (
            self._latest_invalid_motion is not None
            and motion.captured_at_monotonic_seconds
            >= self._latest_invalid_motion.captured_at_monotonic_seconds
        ):
            self._latest_invalid_motion = None
        return True

    def note_invalid_motion(self, motion: TimedMotionMeasurement) -> bool:
        """Record a rejected latest sample so it cannot masquerade as old valid data."""
        assessment = assess_measurement_quality(motion.measurement)
        if assessment.is_valid:
            raise ValueError("motion passed quality validation")
        if (
            self._latest_invalid_motion is not None
            and motion.captured_at_monotonic_seconds
            < self._latest_invalid_motion.captured_at_monotonic_seconds
        ):
            return False
        self._latest_invalid_motion = motion
        return True

    def associate(self, range_timestamp: float) -> RadarMotionMatch:
        if isinstance(range_timestamp, bool) or not isinstance(range_timestamp, (int, float)):
            raise TypeError("range_timestamp must be a number")
        timestamp = float(range_timestamp)
        if not math.isfinite(timestamp) or timestamp < 0:
            raise ValueError("range_timestamp must be finite and non-negative")
        invalid = self._latest_invalid_motion
        motion = self._latest_motion
        if invalid is not None and (
            motion is None
            or invalid.captured_at_monotonic_seconds
            >= motion.captured_at_monotonic_seconds
        ):
            invalid_timestamp = invalid.captured_at_monotonic_seconds
            status = (
                SensorSyncStatus.FUTURE_MOTION_SAMPLE
                if invalid_timestamp > timestamp
                else SensorSyncStatus.INVALID_MOTION_SAMPLE
            )
            return RadarMotionMatch(
                timestamp,
                invalid.measurement,
                invalid_timestamp,
                None,
                status,
            )
        if motion is None:
            return RadarMotionMatch(
                timestamp, None, None, None, SensorSyncStatus.NO_MOTION_SAMPLE
            )
        motion_timestamp = motion.captured_at_monotonic_seconds
        if not assess_measurement_quality(motion.measurement).is_valid:
            return RadarMotionMatch(
                timestamp,
                motion.measurement,
                motion_timestamp,
                None,
                SensorSyncStatus.INVALID_MOTION_SAMPLE,
            )
        age = timestamp - motion_timestamp
        if age < 0:
            return RadarMotionMatch(
                timestamp,
                motion.measurement,
                motion_timestamp,
                None,
                SensorSyncStatus.FUTURE_MOTION_SAMPLE,
            )
        tolerance = max(1e-12, 4 * math.ulp(self.maximum_motion_age_seconds))
        if age - self.maximum_motion_age_seconds > tolerance:
            return RadarMotionMatch(
                timestamp,
                motion.measurement,
                motion_timestamp,
                None,
                SensorSyncStatus.STALE_MOTION_SAMPLE,
            )
        return RadarMotionMatch(
            timestamp,
            motion.measurement,
            motion_timestamp,
            age,
            SensorSyncStatus.MATCHED,
        )
