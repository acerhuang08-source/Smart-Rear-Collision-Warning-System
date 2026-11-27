"""Thin, I/O-free integration of synchronized samples and fusion features."""

from __future__ import annotations

import math
from numbers import Real

from rear_warning.sensors.bno055.models import BNO055Measurement
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization.models import (
    SensorSyncStatus,
    SynchronizedSensorSample,
)

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


class InconsistentFusionSampleError(ValueError):
    """A repeated canonical timestamp carried a different sample payload."""


def _initial_distance_trend() -> DistanceTrend:
    return DistanceTrend(
        sample_count=0,
        inlier_count=0,
        window_span_seconds=0.0,
        range_slope_mps=None,
        closing_rate_mps=None,
        residual_rms_m=None,
        is_valid=True,
        is_ready=False,
        reason=DistanceTrendReason.INSUFFICIENT_SAMPLES,
    )


def _initial_own_motion_evidence() -> OwnMotionEvidence:
    return OwnMotionEvidence(
        unique_sample_count=0,
        window_span_seconds=0.0,
        linear_accel_rms_mps2=0.0,
        linear_accel_peak_mps2=0.0,
        delta_v_evidence_mps=0.0,
        net_orientation_change_radians=0.0,
        cumulative_orientation_change_radians=0.0,
        translational_motion_detected=False,
        rotational_motion_detected=False,
        is_valid=True,
        is_ready=False,
        reason=OwnMotionReason.INSUFFICIENT_SAMPLES,
    )


class RelativeMotionFusionAdapter:
    """Feed one causally associated sample through the fusion feature chain.

    The adapter performs no sensor reads and does not own a clock. Distance and
    motion use the synchronization layer's canonical host-monotonic timestamps.
    A lower-rate matched motion sample may be reused by multiple distance rows,
    but it contributes to :class:`OwnMotionEstimator` only once.
    """

    def __init__(
        self,
        *,
        distance_trend_estimator: DistanceTrendEstimator | None = None,
        own_motion_estimator: OwnMotionEstimator | None = None,
        classifier: RelativeMotionClassifier | None = None,
    ) -> None:
        if distance_trend_estimator is not None and not isinstance(
            distance_trend_estimator, DistanceTrendEstimator
        ):
            raise TypeError(
                "distance_trend_estimator must be a DistanceTrendEstimator"
            )
        if own_motion_estimator is not None and not isinstance(
            own_motion_estimator, OwnMotionEstimator
        ):
            raise TypeError("own_motion_estimator must be an OwnMotionEstimator")
        if classifier is not None and not isinstance(
            classifier, RelativeMotionClassifier
        ):
            raise TypeError("classifier must be a RelativeMotionClassifier")

        self._distance_trend_estimator = (
            distance_trend_estimator or DistanceTrendEstimator()
        )
        self._own_motion_estimator = own_motion_estimator or OwnMotionEstimator()
        self._classifier = classifier or RelativeMotionClassifier()
        self._distance_trend = _initial_distance_trend()
        self._own_motion_evidence = _initial_own_motion_evidence()
        self._last_distance_timestamp: float | None = None
        self._last_sample: SynchronizedSensorSample | None = None
        self._last_motion_timestamp: float | None = None
        self._last_motion_measurement: BNO055Measurement | None = None
        self._current_assessment: RelativeMotionAssessment | None = None

    def update(
        self,
        sample: SynchronizedSensorSample,
    ) -> RelativeMotionAssessment:
        """Process one synchronized record without performing external I/O.

        Identical repeated distance records are no-ops. Conflicting repeated or
        backwards distance timestamps, and conflicting or backwards matched
        motion timestamps, are rejected before either estimator is updated.
        """
        self._validate_sample(sample)
        timestamp = sample.distance_timestamp
        if self._last_distance_timestamp is not None:
            if timestamp == self._last_distance_timestamp:
                if sample != self._last_sample:
                    raise InconsistentFusionSampleError(
                        "repeated distance_timestamp has a conflicting sample"
                    )
                assert self._current_assessment is not None
                return self._current_assessment
            if timestamp < self._last_distance_timestamp:
                raise ValueError("distance_timestamp must be strictly increasing")

        self._validate_motion_order(sample)

        if sample.status is not SensorSyncStatus.INVALID_DISTANCE_SAMPLE:
            self._distance_trend = self._distance_trend_estimator.update(
                timestamp,
                sample.distance_measurement.distance_cm,
            )

        if (
            sample.status is SensorSyncStatus.MATCHED
            and sample.motion_timestamp != self._last_motion_timestamp
        ):
            motion = sample.motion_measurement
            motion_timestamp = sample.motion_timestamp
            assert motion is not None and motion_timestamp is not None
            self._own_motion_evidence = self._own_motion_estimator.update(
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
            self._last_motion_measurement = motion

        sync_healthy = sample.status is SensorSyncStatus.MATCHED
        assessment = self._classifier.update(
            timestamp,
            distance_trend=self._distance_trend,
            own_motion_evidence=self._own_motion_evidence,
            sync_healthy=sync_healthy,
            motion_age_seconds=(
                sample.motion_age_seconds if sync_healthy else None
            ),
        )
        self._last_distance_timestamp = timestamp
        self._last_sample = sample
        self._current_assessment = assessment
        return assessment

    @staticmethod
    def _validate_sample(sample: object) -> None:
        if not isinstance(sample, SynchronizedSensorSample):
            raise TypeError("sample must be a SynchronizedSensorSample")
        if not isinstance(sample.distance_measurement, TFMiniPlusMeasurement):
            raise TypeError(
                "sample.distance_measurement must be a TFMiniPlusMeasurement"
            )
        if sample.motion_measurement is not None and not isinstance(
            sample.motion_measurement, BNO055Measurement
        ):
            raise TypeError(
                "sample.motion_measurement must be a BNO055Measurement or None"
            )
        distance = sample.distance_measurement.distance_cm
        if sample.status is not SensorSyncStatus.INVALID_DISTANCE_SAMPLE and (
            isinstance(distance, bool)
            or not isinstance(distance, Real)
            or not math.isfinite(float(distance))
            or distance <= 0
        ):
            raise ValueError(
                "a non-invalid distance sample requires a positive distance_cm"
            )

    def _validate_motion_order(self, sample: SynchronizedSensorSample) -> None:
        if sample.status is not SensorSyncStatus.MATCHED:
            return
        motion = sample.motion_measurement
        timestamp = sample.motion_timestamp
        assert motion is not None and timestamp is not None
        if self._last_motion_timestamp is None:
            return
        if timestamp < self._last_motion_timestamp:
            raise ValueError("motion_timestamp must not move backwards")
        if (
            timestamp == self._last_motion_timestamp
            and motion != self._last_motion_measurement
        ):
            raise InconsistentFusionSampleError(
                "repeated motion_timestamp has a conflicting measurement"
            )
