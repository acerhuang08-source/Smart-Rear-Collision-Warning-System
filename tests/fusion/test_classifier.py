"""Synthetic tests for I/O-free relative-motion classification."""

from __future__ import annotations

import math

import pytest

from rear_warning.fusion import (
    DistanceTrend,
    DistanceTrendReason,
    InconsistentClassifierInputError,
    OwnMotionEvidence,
    OwnMotionReason,
    RelativeMotionAssessment,
    RelativeMotionClass,
    RelativeMotionClassifier,
    RelativeMotionReason,
    RelativeMotionThresholds,
)


def ready_trend(slope_mps: float = 0.0) -> DistanceTrend:
    return DistanceTrend(
        sample_count=10,
        inlier_count=10,
        window_span_seconds=0.5,
        range_slope_mps=slope_mps,
        closing_rate_mps=max(0.0, -slope_mps),
        residual_rms_m=0.01,
        is_valid=True,
        is_ready=True,
        reason=None,
    )


def not_ready_trend(
    reason: DistanceTrendReason = DistanceTrendReason.INSUFFICIENT_SAMPLES,
) -> DistanceTrend:
    return DistanceTrend(
        sample_count=1,
        inlier_count=1,
        window_span_seconds=0.0,
        range_slope_mps=None,
        closing_rate_mps=None,
        residual_rms_m=None,
        is_valid=reason is not DistanceTrendReason.LARGE_TIME_GAP,
        is_ready=False,
        reason=reason,
    )


def ready_motion(
    *,
    translation: bool = False,
    rotation: bool = False,
) -> OwnMotionEvidence:
    return OwnMotionEvidence(
        unique_sample_count=5,
        window_span_seconds=0.5,
        linear_accel_rms_mps2=0.5 if translation else 0.0,
        linear_accel_peak_mps2=1.2 if translation else 0.0,
        delta_v_evidence_mps=0.25 if translation else 0.0,
        net_orientation_change_radians=0.2 if rotation else 0.0,
        cumulative_orientation_change_radians=0.3 if rotation else 0.0,
        translational_motion_detected=translation,
        rotational_motion_detected=rotation,
        is_valid=True,
        is_ready=True,
        reason=None,
    )


def not_ready_motion(
    reason: OwnMotionReason = OwnMotionReason.INSUFFICIENT_SAMPLES,
) -> OwnMotionEvidence:
    return OwnMotionEvidence(
        unique_sample_count=1,
        window_span_seconds=0.0,
        linear_accel_rms_mps2=0.0,
        linear_accel_peak_mps2=0.0,
        delta_v_evidence_mps=0.0,
        net_orientation_change_radians=0.0,
        cumulative_orientation_change_radians=0.0,
        translational_motion_detected=False,
        rotational_motion_detected=False,
        is_valid=reason is not OwnMotionReason.LARGE_TIME_GAP,
        is_ready=False,
        reason=reason,
    )


def update(
    classifier: RelativeMotionClassifier,
    timestamp: object,
    *,
    slope_mps: float = 0.0,
    translation: bool = False,
    rotation: bool = False,
    sync_healthy: bool = True,
    motion_age_seconds: object | None = 0.05,
    distance_trend: DistanceTrend | None = None,
    own_motion_evidence: OwnMotionEvidence | None = None,
) -> RelativeMotionAssessment:
    return classifier.update(
        timestamp,
        distance_trend=distance_trend or ready_trend(slope_mps),
        own_motion_evidence=own_motion_evidence
        or ready_motion(translation=translation, rotation=rotation),
        sync_healthy=sync_healthy,
        motion_age_seconds=motion_age_seconds,
    )


def establish_stationary_baseline(
    classifier: RelativeMotionClassifier,
) -> RelativeMotionAssessment:
    return [update(classifier, index / 10) for index in range(11)][-1]


def test_stable_range_commits_then_establishes_stationary_baseline() -> None:
    classifier = RelativeMotionClassifier()
    assessments = [update(classifier, index / 10) for index in range(11)]

    assert assessments[2].classification is RelativeMotionClass.UNCERTAIN
    assert assessments[2].candidate_classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert not assessments[2].is_ready
    assert assessments[3].classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert assessments[3].is_ready
    assert not assessments[7].stationary_baseline_ready
    assert assessments[8].stationary_baseline_ready
    assert assessments[-1].stationary_baseline_ready


def test_receding_range_commits_stable_or_receding() -> None:
    classifier = RelativeMotionClassifier()
    assessment = [update(classifier, index / 10, slope_mps=0.4) for index in range(4)][-1]

    assert assessment.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert assessment.reason is RelativeMotionReason.STABLE_OR_RECEDING_RANGE
    assert not assessment.stationary_baseline_ready


def test_startup_closing_without_stationary_baseline_remains_uncertain() -> None:
    classifier = RelativeMotionClassifier()
    assessments = [
        update(classifier, index / 10, slope_mps=-0.5)
        for index in range(11)
    ]

    assert all(
        item.classification is RelativeMotionClass.UNCERTAIN
        for item in assessments
    )
    assert all(not item.stationary_baseline_ready for item in assessments)
    assert assessments[-1].reason is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED


def test_stationary_baseline_allows_later_target_approach() -> None:
    classifier = RelativeMotionClassifier()
    establish_stationary_baseline(classifier)

    assessment = [
        update(classifier, timestamp, slope_mps=-0.5)
        for timestamp in (1.1, 1.2, 1.3, 1.4, 1.5)
    ][-1]

    assert assessment.classification is RelativeMotionClass.TARGET_APPROACH
    assert assessment.stationary_baseline_ready
    assert assessment.reason is RelativeMotionReason.TARGET_APPROACH_EVIDENCE


@pytest.mark.parametrize(
    ("translation", "rotation", "reason"),
    [
        (True, False, RelativeMotionReason.SELF_TRANSLATIONAL_EVIDENCE),
        (False, True, RelativeMotionReason.SELF_ROTATIONAL_EVIDENCE),
        (
            True,
            True,
            RelativeMotionReason.SELF_TRANSLATIONAL_AND_ROTATIONAL_EVIDENCE,
        ),
    ],
)
def test_closing_with_own_motion_commits_self_approach(
    translation: bool,
    rotation: bool,
    reason: RelativeMotionReason,
) -> None:
    classifier = RelativeMotionClassifier()
    assessment = [
        update(
            classifier,
            index / 10,
            slope_mps=-0.5,
            translation=translation,
            rotation=rotation,
        )
        for index in range(4)
    ][-1]

    assert assessment.classification is RelativeMotionClass.STATIC_OR_SELF_APPROACH
    assert assessment.reason is reason
    assert not assessment.stationary_baseline_ready


def test_self_approach_release_never_becomes_target_without_new_baseline() -> None:
    classifier = RelativeMotionClassifier()
    for index in range(4):
        assessment = update(
            classifier,
            index / 10,
            slope_mps=-0.5,
            translation=True,
        )
    assert assessment.classification is RelativeMotionClass.STATIC_OR_SELF_APPROACH

    releases = [
        update(classifier, index / 10, slope_mps=-0.5)
        for index in range(4, 9)
    ]

    assert all(
        item.classification is not RelativeMotionClass.TARGET_APPROACH
        for item in releases
    )
    assert releases[-1].classification is RelativeMotionClass.UNCERTAIN
    assert not releases[-1].stationary_baseline_ready


def test_baseline_must_be_rebuilt_after_self_approach() -> None:
    classifier = RelativeMotionClassifier()
    for index in range(4):
        update(
            classifier,
            index / 10,
            slope_mps=-0.5,
            translation=True,
        )
    for index in range(4, 9):
        update(classifier, index / 10, slope_mps=-0.5)
    stable = [update(classifier, index / 10) for index in range(9, 19)][-1]
    assert stable.stationary_baseline_ready

    target = [
        update(classifier, index / 10, slope_mps=-0.5)
        for index in range(19, 24)
    ][-1]
    assert target.classification is RelativeMotionClass.TARGET_APPROACH


def test_weak_closing_is_uncertain() -> None:
    classifier = RelativeMotionClassifier()
    assessments = [
        update(classifier, index / 10, slope_mps=-0.1)
        for index in range(6)
    ]

    assert assessments[-1].classification is RelativeMotionClass.UNCERTAIN
    assert assessments[-1].reason is RelativeMotionReason.AMBIGUOUS_CLOSING


def test_single_noisy_candidate_does_not_change_committed_state() -> None:
    classifier = RelativeMotionClassifier()
    for index in range(4):
        update(classifier, index / 10)

    noisy = update(
        classifier,
        0.4,
        slope_mps=-0.5,
        translation=True,
    )
    recovered = update(classifier, 0.5)

    assert noisy.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert noisy.candidate_classification is RelativeMotionClass.STATIC_OR_SELF_APPROACH
    assert noisy.reason is RelativeMotionReason.CANDIDATE_PERSISTENCE_NOT_SATISFIED
    assert not noisy.is_ready
    assert recovered.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert recovered.candidate_classification is None


def test_target_candidate_does_not_commit_before_persistence() -> None:
    classifier = RelativeMotionClassifier()
    establish_stationary_baseline(classifier)

    pending = [
        update(classifier, timestamp, slope_mps=-0.5)
        for timestamp in (1.1, 1.2, 1.3)
    ][-1]

    assert pending.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert pending.candidate_classification is RelativeMotionClass.TARGET_APPROACH
    assert pending.candidate_duration_seconds == pytest.approx(0.2)
    assert not pending.is_ready


def test_reset_clears_all_temporal_classification_state() -> None:
    classifier = RelativeMotionClassifier()
    baseline = establish_stationary_baseline(classifier)
    assert baseline.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert baseline.stationary_baseline_ready

    pending = [
        update(classifier, timestamp, slope_mps=-0.5)
        for timestamp in (1.1, 1.2, 1.3)
    ][-1]
    assert pending.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert pending.candidate_classification is RelativeMotionClass.TARGET_APPROACH
    assert pending.candidate_duration_seconds == pytest.approx(0.2)
    assert pending.stationary_baseline_ready
    assert not pending.is_ready

    assert classifier.reset() is None
    assert classifier.reset() is None

    after_reset = update(classifier, 0.0, slope_mps=-0.5)
    assert after_reset.classification is RelativeMotionClass.UNCERTAIN
    assert after_reset.candidate_classification is None
    assert after_reset.candidate_duration_seconds == 0.0
    assert not after_reset.stationary_baseline_ready
    assert after_reset.is_valid
    assert after_reset.is_ready
    assert (
        after_reset.reason
        is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED
    )

    rebuilt = [update(classifier, index / 10) for index in range(1, 10)]
    assert all(
        not assessment.stationary_baseline_ready
        for assessment in rebuilt[:-1]
    )
    assert rebuilt[-1].stationary_baseline_ready


def test_baseline_expiry_before_target_matures_is_immediately_uncertain() -> None:
    classifier = RelativeMotionClassifier(
        RelativeMotionThresholds(
            stationary_baseline_duration_seconds=0.3,
            stationary_baseline_max_age_seconds=0.25,
            target_approach_persistence_seconds=0.2,
        )
    )
    for timestamp in (0.0, 0.1, 0.2, 0.3):
        update(classifier, timestamp)
    for timestamp in (0.4, 0.5):
        pending = update(classifier, timestamp, slope_mps=-0.5)
    assert pending.candidate_classification is RelativeMotionClass.TARGET_APPROACH

    expired = update(classifier, 0.6, slope_mps=-0.5)
    continued = update(classifier, 0.7, slope_mps=-0.5)

    assert expired.classification is RelativeMotionClass.UNCERTAIN
    assert expired.candidate_classification is None
    assert expired.candidate_duration_seconds == 0.0
    assert not expired.stationary_baseline_ready
    assert expired.reason is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED
    assert continued.classification is RelativeMotionClass.UNCERTAIN
    assert continued.candidate_classification is None


def test_target_duration_restarts_only_after_baseline_is_rebuilt() -> None:
    classifier = RelativeMotionClassifier(
        RelativeMotionThresholds(
            maximum_update_gap_seconds=0.4,
            stationary_baseline_duration_seconds=0.3,
            stationary_baseline_max_age_seconds=0.6,
            target_approach_persistence_seconds=0.4,
        )
    )
    for timestamp in (0.0, 0.1, 0.2, 0.3):
        update(classifier, timestamp)
    first_candidates = [
        update(classifier, timestamp, slope_mps=-0.5)
        for timestamp in (0.6, 0.7, 0.8)
    ]
    assert first_candidates[-1].candidate_duration_seconds == pytest.approx(0.2)
    update(classifier, 0.9, slope_mps=-0.5)
    expired = update(classifier, 1.0, slope_mps=-0.5)
    continued = update(classifier, 1.1, slope_mps=-0.5)
    assert expired.classification is RelativeMotionClass.UNCERTAIN
    assert expired.candidate_classification is None
    assert continued.classification is RelativeMotionClass.UNCERTAIN

    rebuilt = [update(classifier, timestamp) for timestamp in (1.2, 1.3, 1.4, 1.5)][-1]
    assert rebuilt.stationary_baseline_ready
    second_candidates = [
        update(classifier, timestamp, slope_mps=-0.5)
        for timestamp in (1.6, 1.7, 1.8)
    ]
    assert second_candidates[-1].classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert second_candidates[-1].candidate_classification is RelativeMotionClass.TARGET_APPROACH
    assert second_candidates[-1].candidate_duration_seconds == pytest.approx(0.2)

    target = [
        update(classifier, timestamp, slope_mps=-0.5)
        for timestamp in (1.9, 2.0)
    ][-1]
    assert target.classification is RelativeMotionClass.TARGET_APPROACH


@pytest.mark.parametrize("slope", [-0.05, 0.05])
def test_stable_deadband_boundary_is_inclusive(slope: float) -> None:
    classifier = RelativeMotionClassifier()
    assessment = [
        update(classifier, index / 10, slope_mps=slope)
        for index in range(4)
    ][-1]

    assert assessment.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert assessment.reason is RelativeMotionReason.STABLE_OR_RECEDING_RANGE


def test_minimum_closing_rate_boundary_is_meaningful_closing() -> None:
    classifier = RelativeMotionClassifier()

    assessment = update(classifier, 0.0, slope_mps=-0.15)

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.reason is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED


def test_self_persistence_boundary_is_inclusive() -> None:
    classifier = RelativeMotionClassifier()
    assessment = [
        update(
            classifier,
            timestamp,
            slope_mps=-0.5,
            translation=True,
        )
        for timestamp in (0.0, 0.125, 0.25)
    ][-1]

    assert assessment.classification is RelativeMotionClass.STATIC_OR_SELF_APPROACH
    assert assessment.is_ready


def run_rate_scenario(rate_hz: int) -> tuple[float, float, float]:
    classifier = RelativeMotionClassifier()
    baseline_timestamp = None
    target_timestamp = None
    assessment = update(classifier, 0.0)
    for index in range(1, int(1.6 * rate_hz) + 1):
        timestamp = index / rate_hz
        slope = 0.0 if timestamp <= 1.0 else -0.5
        assessment = update(classifier, timestamp, slope_mps=slope)
        if baseline_timestamp is None and assessment.stationary_baseline_ready:
            baseline_timestamp = timestamp
        if (
            target_timestamp is None
            and assessment.classification is RelativeMotionClass.TARGET_APPROACH
        ):
            target_timestamp = timestamp

    self_classifier = RelativeMotionClassifier()
    self_timestamp = None
    for index in range(int(0.5 * rate_hz) + 1):
        timestamp = index / rate_hz
        assessment = update(
            self_classifier,
            timestamp,
            slope_mps=-0.5,
            translation=True,
        )
        if assessment.classification is RelativeMotionClass.STATIC_OR_SELF_APPROACH:
            self_timestamp = timestamp
            break

    assert baseline_timestamp is not None
    assert target_timestamp is not None
    assert self_timestamp is not None
    return baseline_timestamp, target_timestamp, self_timestamp


def test_persistence_is_equivalent_at_100_hz_and_10_hz() -> None:
    slow = run_rate_scenario(10)
    fast = run_rate_scenario(100)

    assert slow == pytest.approx((0.8, 1.5, 0.3))
    assert fast == pytest.approx((0.8, 1.41, 0.25))
    assert abs(slow[0] - fast[0]) <= 0.1
    assert abs(slow[1] - fast[1]) <= 0.1
    assert abs(slow[2] - fast[2]) <= 0.1


def test_irregular_timestamps_use_elapsed_time_for_persistence() -> None:
    classifier = RelativeMotionClassifier()
    for timestamp in (0.0, 0.07, 0.19, 0.4, 0.61, 0.79, 0.95):
        update(classifier, timestamp)

    assessment = None
    for timestamp in (1.13, 1.31, 1.53):
        assessment = update(classifier, timestamp, slope_mps=-0.5)

    assert assessment is not None
    assert assessment.classification is RelativeMotionClass.TARGET_APPROACH


def test_identical_repeated_timestamp_is_no_op() -> None:
    classifier = RelativeMotionClassifier()
    first = update(classifier, 0.0)
    duplicates = [update(classifier, 0.0) for _ in range(10)]

    assert all(item is first for item in duplicates)
    next_assessment = update(classifier, 0.1)
    assert next_assessment.candidate_duration_seconds == pytest.approx(0.1)


def test_conflicting_repeated_timestamp_is_rejected_without_state_change() -> None:
    classifier = RelativeMotionClassifier()
    first = update(classifier, 0.0)

    with pytest.raises(InconsistentClassifierInputError, match="conflicting"):
        update(classifier, 0.0, slope_mps=-0.5)

    next_assessment = update(classifier, 0.1)
    assert first.candidate_duration_seconds == 0.0
    assert next_assessment.candidate_duration_seconds == pytest.approx(0.1)


def test_backwards_timestamp_is_rejected_without_state_change() -> None:
    classifier = RelativeMotionClassifier()
    update(classifier, 1.0)

    with pytest.raises(ValueError, match="strictly increasing"):
        update(classifier, 0.9)

    assessment = update(classifier, 1.1)
    assert assessment.candidate_duration_seconds == pytest.approx(0.1)


def test_large_classifier_gap_resets_candidate_and_baseline() -> None:
    classifier = RelativeMotionClassifier()
    establish_stationary_baseline(classifier)

    assessment = update(classifier, 2.0)

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.reason is RelativeMotionReason.TIMESTAMP_DISCONTINUITY
    assert not assessment.is_valid and not assessment.is_ready
    assert not assessment.stationary_baseline_ready
    assert assessment.candidate_classification is None

    closing = update(classifier, 2.1, slope_mps=-0.5)
    assert closing.classification is RelativeMotionClass.UNCERTAIN
    assert closing.reason is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED


def test_distance_trend_not_ready_is_uncertain() -> None:
    assessment = update(
        RelativeMotionClassifier(),
        0.0,
        distance_trend=not_ready_trend(),
    )

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.reason is RelativeMotionReason.DISTANCE_TREND_NOT_READY
    assert not assessment.is_valid


def test_own_motion_not_ready_is_uncertain() -> None:
    assessment = update(
        RelativeMotionClassifier(),
        0.0,
        own_motion_evidence=not_ready_motion(),
    )

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.reason is RelativeMotionReason.OWN_MOTION_NOT_READY
    assert not assessment.is_valid


def test_component_gap_reason_resets_classifier_as_timestamp_discontinuity() -> None:
    assessment = update(
        RelativeMotionClassifier(),
        0.0,
        distance_trend=not_ready_trend(DistanceTrendReason.LARGE_TIME_GAP),
    )

    assert assessment.reason is RelativeMotionReason.TIMESTAMP_DISCONTINUITY
    assert not assessment.stationary_baseline_ready


def test_stale_motion_is_uncertain_but_age_boundary_is_fresh() -> None:
    fresh_classifier = RelativeMotionClassifier()
    fresh = update(fresh_classifier, 0.0, motion_age_seconds=0.2)
    stale = update(
        RelativeMotionClassifier(),
        0.0,
        motion_age_seconds=0.200001,
    )
    missing = update(
        RelativeMotionClassifier(),
        0.0,
        motion_age_seconds=None,
    )

    assert fresh.is_valid
    assert stale.reason is RelativeMotionReason.MOTION_STALE
    assert missing.reason is RelativeMotionReason.MOTION_STALE
    assert not stale.is_valid and not missing.is_valid


def test_unhealthy_sync_is_uncertain() -> None:
    assessment = update(
        RelativeMotionClassifier(),
        0.0,
        sync_healthy=False,
        motion_age_seconds=None,
    )

    assert assessment.classification is RelativeMotionClass.UNCERTAIN
    assert assessment.reason is RelativeMotionReason.SYNC_UNHEALTHY
    assert not assessment.is_valid


def test_stationary_baseline_expiry_revokes_target_approach() -> None:
    classifier = RelativeMotionClassifier()
    establish_stationary_baseline(classifier)
    assessments = [
        update(classifier, index / 10, slope_mps=-0.5)
        for index in range(11, 22)
    ]

    assert any(
        item.classification is RelativeMotionClass.TARGET_APPROACH
        for item in assessments[:-1]
    )
    assert assessments[-1].classification is RelativeMotionClass.UNCERTAIN
    assert not assessments[-1].stationary_baseline_ready
    assert assessments[-1].reason is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED


def test_own_motion_immediately_invalidates_stationary_baseline() -> None:
    classifier = RelativeMotionClassifier()
    established = establish_stationary_baseline(classifier)
    assert established.stationary_baseline_ready

    assessment = update(classifier, 1.1, translation=True)

    assert not assessment.stationary_baseline_ready
    assert assessment.classification is RelativeMotionClass.STABLE_OR_RECEDING


def test_health_failure_clears_stationary_baseline() -> None:
    classifier = RelativeMotionClassifier()
    establish_stationary_baseline(classifier)
    failed = update(
        classifier,
        1.1,
        sync_healthy=False,
        motion_age_seconds=None,
    )
    closing = update(classifier, 1.2, slope_mps=-0.5)

    assert not failed.stationary_baseline_ready
    assert closing.classification is RelativeMotionClass.UNCERTAIN
    assert closing.reason is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED


def test_constant_velocity_self_reverse_without_prior_baseline_is_uncertain() -> None:
    classifier = RelativeMotionClassifier()
    assessments = [
        update(classifier, index / 100, slope_mps=-0.5)
        for index in range(101)
    ]

    assert all(
        item.classification is RelativeMotionClass.UNCERTAIN
        for item in assessments
    )
    assert all(not item.stationary_baseline_ready for item in assessments)


def test_stationary_target_approach_differs_by_having_baseline_history() -> None:
    classifier = RelativeMotionClassifier()
    baseline = establish_stationary_baseline(classifier)
    assert baseline.stationary_baseline_ready

    target = [
        update(classifier, index / 10, slope_mps=-0.5)
        for index in range(11, 16)
    ][-1]

    assert target.classification is RelativeMotionClass.TARGET_APPROACH


@pytest.mark.parametrize(
    ("timestamp", "sync_healthy", "motion_age", "error"),
    [
        (math.nan, True, 0.0, ValueError),
        (math.inf, True, 0.0, ValueError),
        (True, True, 0.0, TypeError),
        ("0", True, 0.0, TypeError),
        (0.0, "yes", 0.0, TypeError),
        (0.0, True, math.nan, ValueError),
        (0.0, True, math.inf, ValueError),
        (0.0, True, True, TypeError),
        (0.0, True, "0", TypeError),
        (0.0, True, -1.0, ValueError),
    ],
)
def test_invalid_scalar_inputs_are_rejected(
    timestamp: object,
    sync_healthy: object,
    motion_age: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        RelativeMotionClassifier().update(
            timestamp,
            distance_trend=ready_trend(),
            own_motion_evidence=ready_motion(),
            sync_healthy=sync_healthy,  # type: ignore[arg-type]
            motion_age_seconds=motion_age,
        )
