"""Integration tests for synchronized samples and fusion feature extraction."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import replace

import pytest

from rear_warning.fusion import (
    DistanceTrendEstimator,
    DistanceTrendThresholds,
    OwnMotionEstimator,
    OwnMotionReason,
    OwnMotionThresholds,
    RelativeMotionAssessment,
    RelativeMotionClass,
    RelativeMotionClassifier,
    RelativeMotionFusionAdapter,
    RelativeMotionReason,
    RelativeMotionThresholds,
)
from rear_warning.sensors.bno055.models import (
    BNO055Calibration,
    BNO055Measurement,
    EulerAngles,
    Quaternion,
    Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import (
    SensorSynchronizer,
    SensorSyncStatus,
    SynchronizedSensorSample,
    TimedDistanceMeasurement,
    TimedMotionMeasurement,
)


def motion(timestamp: float) -> BNO055Measurement:
    return BNO055Measurement(
        monotonic_timestamp=timestamp,
        euler=EulerAngles(0.0, 0.0, 0.0),
        quaternion=Quaternion(1.0, 0.0, 0.0, 0.0),
        linear_acceleration=Vector3(0.0, 0.0, 0.0),
        gravity=Vector3(0.0, 0.0, 9.8),
        temperature_c=25,
        calibration=BNO055Calibration(0, 3, 0, 0),
        operation_mode=0x0C,
        system_status=0x05,
        system_error=0x00,
    )


def distance(distance_cm: int) -> TFMiniPlusMeasurement:
    return TFMiniPlusMeasurement(distance_cm, 500, 24.5)


def run_100_hz_distance_10_hz_motion(
    adapter: RelativeMotionFusionAdapter,
    synchronizer: SensorSynchronizer,
    *,
    start_index: int,
    end_index: int,
    distance_for_index: Callable[[int], int],
) -> list[RelativeMotionAssessment]:
    assessments: list[RelativeMotionAssessment] = []
    for index in range(start_index, end_index + 1):
        timestamp = index / 100.0
        if index % 10 == 0:
            synchronizer.update_motion(
                TimedMotionMeasurement(motion(timestamp), timestamp)
            )
        synchronized = synchronizer.associate(
            TimedDistanceMeasurement(
                distance(distance_for_index(index)),
                timestamp,
            )
        )
        assessments.append(adapter.update(synchronized))
    return assessments


def matched_sample(
    synchronizer: SensorSynchronizer,
    *,
    timestamp: float,
    distance_cm: int,
) -> SynchronizedSensorSample:
    synchronizer.update_motion(
        TimedMotionMeasurement(motion(timestamp), timestamp)
    )
    sample = synchronizer.associate(
        TimedDistanceMeasurement(distance(distance_cm), timestamp)
    )
    assert sample.status is SensorSyncStatus.MATCHED
    return sample


def test_100_hz_distance_reuses_each_10_hz_motion_only_once() -> None:
    adapter = RelativeMotionFusionAdapter()
    assessments = run_100_hz_distance_10_hz_motion(
        adapter,
        SensorSynchronizer(),
        start_index=0,
        end_index=39,
        distance_for_index=lambda _index: 500,
    )

    final = assessments[-1]
    assert final.distance_trend.sample_count == 40
    assert final.own_motion_evidence.unique_sample_count == 4
    assert final.own_motion_evidence.window_span_seconds == pytest.approx(0.3)


def test_closing_startup_without_baseline_remains_uncertain() -> None:
    adapter = RelativeMotionFusionAdapter()
    assessments = run_100_hz_distance_10_hz_motion(
        adapter,
        SensorSynchronizer(),
        start_index=0,
        end_index=120,
        distance_for_index=lambda index: 500 - index,
    )

    assert all(
        assessment.classification is RelativeMotionClass.UNCERTAIN
        for assessment in assessments
    )
    assert any(
        assessment.is_valid
        and assessment.reason
        is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED
        for assessment in assessments
    )
    assert all(not assessment.stationary_baseline_ready for assessment in assessments)


def test_stationary_baseline_then_closing_obeys_target_persistence() -> None:
    adapter = RelativeMotionFusionAdapter()
    synchronizer = SensorSynchronizer()
    stable = run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=0,
        end_index=150,
        distance_for_index=lambda _index: 500,
    )
    assert stable[-1].stationary_baseline_ready

    closing = run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=151,
        end_index=260,
        distance_for_index=lambda index: 500 - (index - 150),
    )
    candidates = [
        assessment
        for assessment in closing
        if assessment.candidate_classification
        is RelativeMotionClass.TARGET_APPROACH
    ]
    targets = [
        assessment
        for assessment in closing
        if assessment.classification is RelativeMotionClass.TARGET_APPROACH
    ]

    assert candidates
    assert all(
        assessment.classification is RelativeMotionClass.STABLE_OR_RECEDING
        and not assessment.is_ready
        and assessment.candidate_duration_seconds < 0.4
        for assessment in candidates
    )
    assert targets
    assert targets[0].timestamp_seconds - candidates[0].timestamp_seconds >= 0.4
    assert targets[0].reason is RelativeMotionReason.TARGET_APPROACH_EVIDENCE


@pytest.mark.parametrize(
    "status",
    [
        SensorSyncStatus.NO_MOTION_SAMPLE,
        SensorSyncStatus.STALE_MOTION_SAMPLE,
        SensorSyncStatus.FUTURE_MOTION_SAMPLE,
    ],
)
def test_unmatched_status_immediately_clears_baseline_and_target_candidate(
    status: SensorSyncStatus,
) -> None:
    adapter = RelativeMotionFusionAdapter()
    synchronizer = SensorSynchronizer()
    run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=0,
        end_index=150,
        distance_for_index=lambda _index: 500,
    )
    closing = run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=151,
        end_index=190,
        distance_for_index=lambda index: 500 - (index - 150),
    )
    assert any(
        assessment.candidate_classification
        is RelativeMotionClass.TARGET_APPROACH
        for assessment in closing
    )

    timestamp = 1.91
    if status is SensorSyncStatus.NO_MOTION_SAMPLE:
        associated_motion = None
        motion_timestamp = None
    elif status is SensorSyncStatus.STALE_MOTION_SAMPLE:
        associated_motion = motion(1.6)
        motion_timestamp = 1.6
    else:
        associated_motion = motion(1.92)
        motion_timestamp = 1.92
    unhealthy = SynchronizedSensorSample(
        distance_measurement=distance(459),
        motion_measurement=associated_motion,
        distance_timestamp=timestamp,
        motion_timestamp=motion_timestamp,
        motion_age_seconds=None,
        status=status,
    )

    assessment = adapter.update(unhealthy)

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.candidate_classification is None
    assert not assessment.stationary_baseline_ready
    assert not assessment.is_valid
    assert not assessment.is_ready
    assert assessment.reason is RelativeMotionReason.SYNC_UNHEALTHY

    recovery_motion = motion(1.92)
    continued = adapter.update(
        SynchronizedSensorSample(
            distance_measurement=distance(458),
            motion_measurement=recovery_motion,
            distance_timestamp=1.92,
            motion_timestamp=1.92,
            motion_age_seconds=0.0,
            status=SensorSyncStatus.MATCHED,
        )
    )
    assert continued.classification is RelativeMotionClass.UNCERTAIN
    assert continued.candidate_classification is None
    assert not continued.stationary_baseline_ready
    assert (
        continued.reason
        is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED
    )


def test_invalid_distance_does_not_update_existing_trend_and_fails_safe() -> None:
    adapter = RelativeMotionFusionAdapter()
    assessments = run_100_hz_distance_10_hz_motion(
        adapter,
        SensorSynchronizer(),
        start_index=0,
        end_index=150,
        distance_for_index=lambda _index: 500,
    )
    existing_trend = assessments[-1].distance_trend
    assert existing_trend.is_ready
    assert assessments[-1].stationary_baseline_ready

    assessment = adapter.update(
        SynchronizedSensorSample(
            distance_measurement=distance(0),
            motion_measurement=None,
            distance_timestamp=1.51,
            motion_timestamp=None,
            motion_age_seconds=None,
            status=SensorSyncStatus.INVALID_DISTANCE_SAMPLE,
        )
    )

    assert assessment.distance_trend is existing_trend
    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.candidate_classification is None
    assert not assessment.stationary_baseline_ready
    assert not assessment.is_valid
    assert not assessment.is_ready
    assert assessment.reason is RelativeMotionReason.SYNC_UNHEALTHY


def test_invalid_motion_clears_target_candidate_and_baseline() -> None:
    adapter = RelativeMotionFusionAdapter()
    synchronizer = SensorSynchronizer()
    run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=0,
        end_index=150,
        distance_for_index=lambda _index: 500,
    )
    closing = run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=151,
        end_index=190,
        distance_for_index=lambda index: 500 - (index - 150),
    )
    assert any(
        assessment.candidate_classification
        is RelativeMotionClass.TARGET_APPROACH
        for assessment in closing
    )

    invalid_motion = replace(
        motion(1.91),
        quaternion=Quaternion(0.0, 0.0, 0.0, 0.0),
    )
    assessment = adapter.update(
        SynchronizedSensorSample(
            distance_measurement=distance(459),
            motion_measurement=invalid_motion,
            distance_timestamp=1.91,
            motion_timestamp=1.91,
            motion_age_seconds=None,
            status=SensorSyncStatus.INVALID_MOTION_SAMPLE,
        )
    )

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.candidate_classification is None
    assert not assessment.stationary_baseline_ready
    assert not assessment.is_valid
    assert not assessment.is_ready
    assert assessment.reason is RelativeMotionReason.SYNC_UNHEALTHY

    recovery_motion = motion(1.92)
    continued = adapter.update(
        SynchronizedSensorSample(
            distance_measurement=distance(458),
            motion_measurement=recovery_motion,
            distance_timestamp=1.92,
            motion_timestamp=1.92,
            motion_age_seconds=0.0,
            status=SensorSyncStatus.MATCHED,
        )
    )
    assert continued.classification is RelativeMotionClass.UNCERTAIN
    assert continued.candidate_classification is None
    assert not continued.stationary_baseline_ready
    assert (
        continued.reason
        is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED
    )


def test_own_motion_large_gap_clears_temporal_state_through_adapter() -> None:
    adapter = RelativeMotionFusionAdapter(
        own_motion_estimator=OwnMotionEstimator(
            OwnMotionThresholds(maximum_gap_seconds=0.15)
        )
    )
    synchronizer = SensorSynchronizer()
    run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=0,
        end_index=150,
        distance_for_index=lambda _index: 500,
    )
    closing = run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=151,
        end_index=190,
        distance_for_index=lambda index: 500 - (index - 150),
    )
    assert any(
        assessment.candidate_classification
        is RelativeMotionClass.TARGET_APPROACH
        for assessment in closing
    )

    interrupted = adapter.update(
        matched_sample(
            synchronizer,
            timestamp=2.06,
            distance_cm=444,
        )
    )

    assert interrupted.own_motion_evidence.reason is OwnMotionReason.LARGE_TIME_GAP
    assert interrupted.own_motion_evidence.unique_sample_count == 1
    assert not interrupted.own_motion_evidence.is_valid
    assert not interrupted.own_motion_evidence.is_ready
    assert interrupted.classification is RelativeMotionClass.UNCERTAIN
    assert interrupted.candidate_classification is None
    assert not interrupted.stationary_baseline_ready
    assert not interrupted.is_valid
    assert not interrupted.is_ready
    assert interrupted.reason is RelativeMotionReason.TIMESTAMP_DISCONTINUITY

    recovered = [
        adapter.update(
            matched_sample(
                synchronizer,
                timestamp=timestamp,
                distance_cm=distance_cm,
            )
        )
        for timestamp, distance_cm in (
            (2.16, 434),
            (2.26, 424),
            (2.36, 414),
            (2.46, 404),
        )
    ]
    assert all(
        assessment.classification is RelativeMotionClass.UNCERTAIN
        for assessment in recovered
    )
    assert all(not assessment.stationary_baseline_ready for assessment in recovered)
    assert recovered[-1].reason is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED

    rebuilt = [
        adapter.update(
            matched_sample(
                synchronizer,
                timestamp=round(2.46 + index / 10.0, 2),
                distance_cm=404,
            )
        )
        for index in range(1, 22)
    ]
    assert not rebuilt[0].stationary_baseline_ready
    assert rebuilt[-1].stationary_baseline_ready


@pytest.mark.parametrize(
    ("next_timestamp", "expect_discontinuity"),
    [
        (1.75, False),
        (math.nextafter(1.75, math.inf), True),
    ],
    ids=["exact_maximum", "one_ulp_over_maximum"],
)
def test_classifier_maximum_update_gap_boundary_through_adapter(
    next_timestamp: float,
    expect_discontinuity: bool,
) -> None:
    adapter = RelativeMotionFusionAdapter(
        distance_trend_estimator=DistanceTrendEstimator(
            DistanceTrendThresholds(maximum_gap_seconds=0.5)
        ),
        own_motion_estimator=OwnMotionEstimator(
            OwnMotionThresholds(maximum_gap_seconds=0.5)
        ),
        classifier=RelativeMotionClassifier(
            RelativeMotionThresholds(maximum_update_gap_seconds=0.25)
        ),
    )
    synchronizer = SensorSynchronizer()
    stable = run_100_hz_distance_10_hz_motion(
        adapter,
        synchronizer,
        start_index=0,
        end_index=150,
        distance_for_index=lambda _index: 500,
    )
    assert stable[-1].stationary_baseline_ready

    assessment = adapter.update(
        matched_sample(
            synchronizer,
            timestamp=next_timestamp,
            distance_cm=500,
        )
    )

    if not expect_discontinuity:
        assert assessment.classification is RelativeMotionClass.STABLE_OR_RECEDING
        assert assessment.stationary_baseline_ready
        assert assessment.is_valid
        assert assessment.is_ready
        assert assessment.reason is RelativeMotionReason.STABLE_OR_RECEDING_RANGE
        return

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.candidate_classification is None
    assert not assessment.stationary_baseline_ready
    assert not assessment.is_valid
    assert not assessment.is_ready
    assert assessment.reason is RelativeMotionReason.TIMESTAMP_DISCONTINUITY

    continued = adapter.update(
        matched_sample(
            synchronizer,
            timestamp=1.85,
            distance_cm=490,
        )
    )
    assert continued.classification is not RelativeMotionClass.TARGET_APPROACH
    assert not continued.stationary_baseline_ready
