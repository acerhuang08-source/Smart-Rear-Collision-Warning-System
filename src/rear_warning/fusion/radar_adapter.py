"""I/O-free ioRadar701/BNO055 fusion using the existing estimators."""

from __future__ import annotations

from dataclasses import dataclass

from rear_warning.sensors.ioradar701.models import TargetFrame
from rear_warning.synchronization.models import SensorSyncStatus
from rear_warning.synchronization.radar_motion import RadarMotionMatch

from .classifier import RelativeMotionClassifier
from .distance_trend import DistanceTrendEstimator
from .models import (
    DistanceTrend,
    DistanceTrendReason,
    OwnMotionEvidence,
    OwnMotionReason,
    RelativeMotionAssessment,
)
from .own_motion import OwnMotionEstimator


@dataclass(frozen=True, slots=True)
class RadarFusionInput:
    target: TargetFrame
    captured_at_monotonic_seconds: float
    motion_match: RadarMotionMatch


def _empty_trend() -> DistanceTrend:
    return DistanceTrend(0, 0, 0.0, None, None, None, True, False,
                         DistanceTrendReason.INSUFFICIENT_SAMPLES)


def _empty_motion() -> OwnMotionEvidence:
    return OwnMotionEvidence(
        0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        False, False, True, False, OwnMotionReason.INSUFFICIENT_SAMPLES,
    )


class RadarRelativeMotionAdapter:
    """Fuse selected geometric range while preserving target boundaries."""

    def __init__(
        self,
        *,
        distance_trend_estimator: DistanceTrendEstimator | None = None,
        own_motion_estimator: OwnMotionEstimator | None = None,
        classifier: RelativeMotionClassifier | None = None,
    ) -> None:
        self._distance = distance_trend_estimator or DistanceTrendEstimator()
        self._motion = own_motion_estimator or OwnMotionEstimator()
        self._classifier = classifier or RelativeMotionClassifier()
        self._trend = _empty_trend()
        self._motion_evidence = _empty_motion()
        self._target_id: int | None = None
        self._last_range_timestamp: float | None = None
        self._last_motion_timestamp: float | None = None
        self._last_motion_payload: object | None = None

    @property
    def target_id(self) -> int | None:
        return self._target_id

    def reset_target(self) -> None:
        """Clear only target-bound trend/classifier state.

        The healthy BNO window and its deduplication marker are deliberately
        retained, so switching targets cannot count one motion sample twice.
        """
        self._distance.reset()
        self._classifier.reset()
        self._trend = _empty_trend()
        self._target_id = None
        self._last_range_timestamp = None

    def reset_all(self) -> None:
        """Clear target and BNO history after a motion discontinuity."""
        self.reset_target()
        self._motion.reset()
        self._motion_evidence = _empty_motion()
        self._last_motion_timestamp = None
        self._last_motion_payload = None

    def update(self, sample: RadarFusionInput) -> RelativeMotionAssessment:
        if not isinstance(sample, RadarFusionInput):
            raise TypeError("sample must be a RadarFusionInput")
        timestamp = sample.captured_at_monotonic_seconds
        if self._target_id is not None and sample.target.target_id != self._target_id:
            self.reset_target()
        if self._last_range_timestamp is not None and timestamp <= self._last_range_timestamp:
            raise ValueError("radar range timestamps must be strictly increasing")
        self._target_id = sample.target.target_id
        self._trend = self._distance.update(timestamp, sample.target.distance_m * 100.0)

        match = sample.motion_match
        if match.status is SensorSyncStatus.MATCHED:
            motion = match.motion_measurement
            motion_timestamp = match.motion_timestamp
            assert motion is not None and motion_timestamp is not None
            if motion_timestamp != self._last_motion_timestamp:
                self._motion_evidence = self._motion.update(
                    motion_timestamp,
                    linear_acceleration_mps2=(
                        motion.linear_acceleration.x,
                        motion.linear_acceleration.y,
                        motion.linear_acceleration.z,
                    ),
                    quaternion=(
                        motion.quaternion.w,
                        motion.quaternion.x,
                        motion.quaternion.y,
                        motion.quaternion.z,
                    ),
                )
                self._last_motion_timestamp = motion_timestamp
                self._last_motion_payload = motion
            elif motion != self._last_motion_payload:
                raise ValueError("repeated motion timestamp has a conflicting payload")

        assessment = self._classifier.update(
            timestamp,
            distance_trend=self._trend,
            own_motion_evidence=self._motion_evidence,
            sync_healthy=match.status is SensorSyncStatus.MATCHED,
            motion_age_seconds=match.motion_age_seconds,
        )
        self._last_range_timestamp = timestamp
        return assessment
