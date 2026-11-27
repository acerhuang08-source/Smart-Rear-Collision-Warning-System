"""Tests for immutable distance-trend models and thresholds."""

from dataclasses import FrozenInstanceError

import pytest

from rear_warning.fusion import (
    DistanceTrend,
    DistanceTrendReason,
    DistanceTrendThresholds,
    OwnMotionEvidence,
    OwnMotionReason,
    OwnMotionThresholds,
    RelativeMotionAssessment,
    RelativeMotionClass,
    RelativeMotionReason,
    RelativeMotionThresholds,
)


def test_threshold_defaults_are_immutable_and_bounded() -> None:
    thresholds = DistanceTrendThresholds()

    assert thresholds.window_duration_seconds == 1.0
    assert thresholds.maximum_samples == 128
    with pytest.raises(FrozenInstanceError):
        thresholds.minimum_samples = 10  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("window_duration_seconds", 0, ValueError),
        ("minimum_span_seconds", float("nan"), ValueError),
        ("maximum_gap_seconds", float("inf"), ValueError),
        ("outlier_mad_scale", True, TypeError),
        ("outlier_residual_floor_m", "0.1", TypeError),
        ("minimum_samples", 1, ValueError),
        ("minimum_samples", True, TypeError),
        ("maximum_samples", 1, ValueError),
    ],
)
def test_thresholds_reject_invalid_scalars(
    field: str, value: object, error: type[Exception]
) -> None:
    values: dict[str, object] = {field: value}
    with pytest.raises(error):
        DistanceTrendThresholds(**values)  # type: ignore[arg-type]


def test_thresholds_reject_inconsistent_bounds() -> None:
    with pytest.raises(ValueError, match="minimum_span_seconds"):
        DistanceTrendThresholds(
            window_duration_seconds=0.5,
            minimum_span_seconds=0.6,
        )
    with pytest.raises(ValueError, match="maximum_gap_seconds"):
        DistanceTrendThresholds(
            window_duration_seconds=0.5,
            maximum_gap_seconds=0.6,
        )
    with pytest.raises(ValueError, match="maximum_samples"):
        DistanceTrendThresholds(minimum_samples=6, maximum_samples=5)


def test_ready_trend_enforces_units_and_closing_rate_invariant() -> None:
    trend = DistanceTrend(
        sample_count=5,
        inlier_count=5,
        window_span_seconds=0.5,
        range_slope_mps=-1.25,
        closing_rate_mps=1.25,
        residual_rms_m=0.01,
        is_valid=True,
        is_ready=True,
        reason=None,
    )

    assert trend.rejected_sample_count == 0
    with pytest.raises(FrozenInstanceError):
        trend.sample_count = 6  # type: ignore[misc]
    with pytest.raises(ValueError, match="closing_rate_mps"):
        DistanceTrend(
            sample_count=5,
            inlier_count=5,
            window_span_seconds=0.5,
            range_slope_mps=-1.25,
            closing_rate_mps=0.0,
            residual_rms_m=0.01,
            is_valid=True,
            is_ready=True,
            reason=None,
        )


def test_not_ready_trend_requires_reason_and_no_estimates() -> None:
    trend = DistanceTrend(
        sample_count=1,
        inlier_count=0,
        window_span_seconds=0.0,
        range_slope_mps=None,
        closing_rate_mps=None,
        residual_rms_m=None,
        is_valid=True,
        is_ready=False,
        reason=DistanceTrendReason.INSUFFICIENT_SAMPLES,
    )

    assert trend.reason is DistanceTrendReason.INSUFFICIENT_SAMPLES
    with pytest.raises(ValueError, match="requires a reason"):
        DistanceTrend(
            sample_count=1,
            inlier_count=0,
            window_span_seconds=0.0,
            range_slope_mps=None,
            closing_rate_mps=None,
            residual_rms_m=None,
            is_valid=True,
            is_ready=False,
            reason=None,
        )


def test_own_motion_thresholds_are_immutable_and_validate_bounds() -> None:
    thresholds = OwnMotionThresholds()

    assert thresholds.minimum_unique_samples == 4
    assert thresholds.maximum_samples == 32
    with pytest.raises(FrozenInstanceError):
        thresholds.maximum_gap_seconds = 1.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="minimum_span_seconds"):
        OwnMotionThresholds(
            window_duration_seconds=0.5,
            minimum_span_seconds=0.6,
        )
    with pytest.raises(ValueError, match="maximum_gap_seconds"):
        OwnMotionThresholds(
            window_duration_seconds=0.5,
            maximum_gap_seconds=0.6,
        )
    with pytest.raises(ValueError, match="maximum_samples"):
        OwnMotionThresholds(minimum_unique_samples=5, maximum_samples=4)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("window_duration_seconds", 0, ValueError),
        ("minimum_unique_samples", True, TypeError),
        ("minimum_span_seconds", float("nan"), ValueError),
        ("maximum_gap_seconds", float("inf"), ValueError),
        ("maximum_samples", "32", TypeError),
        ("linear_accel_rms_threshold_mps2", True, TypeError),
        ("linear_accel_peak_threshold_mps2", 0, ValueError),
        ("delta_v_evidence_threshold_mps", -1, ValueError),
        ("cumulative_orientation_threshold_radians", "0.1", TypeError),
    ],
)
def test_own_motion_thresholds_reject_invalid_scalars(
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        OwnMotionThresholds(**{field: value})  # type: ignore[arg-type]


def test_own_motion_evidence_is_immutable_and_validates_state() -> None:
    evidence = OwnMotionEvidence(
        unique_sample_count=4,
        window_span_seconds=0.3,
        linear_accel_rms_mps2=0.1,
        linear_accel_peak_mps2=0.2,
        delta_v_evidence_mps=0.01,
        net_orientation_change_radians=0.02,
        cumulative_orientation_change_radians=0.03,
        translational_motion_detected=False,
        rotational_motion_detected=False,
        is_valid=True,
        is_ready=True,
        reason=None,
    )

    with pytest.raises(FrozenInstanceError):
        evidence.unique_sample_count = 5  # type: ignore[misc]
    with pytest.raises(ValueError, match="requires a reason"):
        OwnMotionEvidence(
            unique_sample_count=1,
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
            reason=None,
        )
    with pytest.raises(ValueError, match="cannot detect motion"):
        OwnMotionEvidence(
            unique_sample_count=1,
            window_span_seconds=0.0,
            linear_accel_rms_mps2=2.0,
            linear_accel_peak_mps2=2.0,
            delta_v_evidence_mps=0.0,
            net_orientation_change_radians=0.0,
            cumulative_orientation_change_radians=0.0,
            translational_motion_detected=True,
            rotational_motion_detected=False,
            is_valid=True,
            is_ready=False,
            reason=OwnMotionReason.INSUFFICIENT_SAMPLES,
        )


def test_relative_motion_thresholds_are_immutable_and_validate_bounds() -> None:
    thresholds = RelativeMotionThresholds()

    assert thresholds.stable_range_rate_deadband_mps == 0.05
    assert thresholds.minimum_closing_rate_mps == 0.15
    with pytest.raises(FrozenInstanceError):
        thresholds.maximum_motion_age_seconds = 1.0  # type: ignore[misc]
    with pytest.raises(ValueError, match="deadband"):
        RelativeMotionThresholds(
            stable_range_rate_deadband_mps=0.15,
            minimum_closing_rate_mps=0.15,
        )
    with pytest.raises(ValueError, match="baseline_max_age"):
        RelativeMotionThresholds(
            target_approach_persistence_seconds=0.5,
            stationary_baseline_max_age_seconds=0.4,
        )


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("stable_range_rate_deadband_mps", 0, ValueError),
        ("minimum_closing_rate_mps", True, TypeError),
        ("maximum_motion_age_seconds", float("nan"), ValueError),
        ("maximum_update_gap_seconds", float("inf"), ValueError),
        ("stable_persistence_seconds", "0.3", TypeError),
        ("self_approach_persistence_seconds", -1, ValueError),
        ("target_approach_persistence_seconds", 0, ValueError),
        ("uncertain_persistence_seconds", True, TypeError),
        ("stationary_baseline_duration_seconds", float("nan"), ValueError),
        ("stationary_baseline_max_age_seconds", float("inf"), ValueError),
    ],
)
def test_relative_motion_thresholds_reject_invalid_scalars(
    field: str,
    value: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        RelativeMotionThresholds(**{field: value})  # type: ignore[arg-type]


def test_relative_motion_assessment_is_immutable_and_validates_state() -> None:
    trend = DistanceTrend(
        sample_count=5,
        inlier_count=5,
        window_span_seconds=0.5,
        range_slope_mps=0.0,
        closing_rate_mps=0.0,
        residual_rms_m=0.0,
        is_valid=True,
        is_ready=True,
        reason=None,
    )
    motion = OwnMotionEvidence(
        unique_sample_count=4,
        window_span_seconds=0.3,
        linear_accel_rms_mps2=0.0,
        linear_accel_peak_mps2=0.0,
        delta_v_evidence_mps=0.0,
        net_orientation_change_radians=0.0,
        cumulative_orientation_change_radians=0.0,
        translational_motion_detected=False,
        rotational_motion_detected=False,
        is_valid=True,
        is_ready=True,
        reason=None,
    )
    assessment = RelativeMotionAssessment(
        classification=RelativeMotionClass.STABLE_OR_RECEDING,
        candidate_classification=None,
        timestamp_seconds=1.0,
        distance_trend=trend,
        own_motion_evidence=motion,
        stationary_baseline_ready=True,
        candidate_duration_seconds=0.0,
        is_valid=True,
        is_ready=True,
        reason=RelativeMotionReason.STABLE_OR_RECEDING_RANGE,
    )

    with pytest.raises(FrozenInstanceError):
        assessment.is_ready = False  # type: ignore[misc]
    with pytest.raises(ValueError, match="differ"):
        RelativeMotionAssessment(
            classification=RelativeMotionClass.UNCERTAIN,
            candidate_classification=RelativeMotionClass.UNCERTAIN,
            timestamp_seconds=1.0,
            distance_trend=trend,
            own_motion_evidence=motion,
            stationary_baseline_ready=False,
            candidate_duration_seconds=0.0,
            is_valid=True,
            is_ready=False,
            reason=RelativeMotionReason.CANDIDATE_PERSISTENCE_NOT_SATISFIED,
        )
    with pytest.raises(ValueError, match="must be zero"):
        RelativeMotionAssessment(
            classification=RelativeMotionClass.UNCERTAIN,
            candidate_classification=None,
            timestamp_seconds=1.0,
            distance_trend=trend,
            own_motion_evidence=motion,
            stationary_baseline_ready=False,
            candidate_duration_seconds=0.1,
            is_valid=True,
            is_ready=False,
            reason=RelativeMotionReason.CANDIDATE_PERSISTENCE_NOT_SATISFIED,
        )


def test_target_approach_assessment_requires_stationary_baseline() -> None:
    trend = DistanceTrend(
        sample_count=5,
        inlier_count=5,
        window_span_seconds=0.5,
        range_slope_mps=-0.5,
        closing_rate_mps=0.5,
        residual_rms_m=0.01,
        is_valid=True,
        is_ready=True,
        reason=None,
    )
    motion = OwnMotionEvidence(
        unique_sample_count=4,
        window_span_seconds=0.3,
        linear_accel_rms_mps2=0.0,
        linear_accel_peak_mps2=0.0,
        delta_v_evidence_mps=0.0,
        net_orientation_change_radians=0.0,
        cumulative_orientation_change_radians=0.0,
        translational_motion_detected=False,
        rotational_motion_detected=False,
        is_valid=True,
        is_ready=True,
        reason=None,
    )

    with pytest.raises(ValueError, match="classification requires"):
        RelativeMotionAssessment(
            classification=RelativeMotionClass.TARGET_APPROACH,
            candidate_classification=None,
            timestamp_seconds=1.0,
            distance_trend=trend,
            own_motion_evidence=motion,
            stationary_baseline_ready=False,
            candidate_duration_seconds=0.0,
            is_valid=True,
            is_ready=True,
            reason=RelativeMotionReason.TARGET_APPROACH_EVIDENCE,
        )
    with pytest.raises(ValueError, match="candidate requires"):
        RelativeMotionAssessment(
            classification=RelativeMotionClass.STABLE_OR_RECEDING,
            candidate_classification=RelativeMotionClass.TARGET_APPROACH,
            timestamp_seconds=1.0,
            distance_trend=trend,
            own_motion_evidence=motion,
            stationary_baseline_ready=False,
            candidate_duration_seconds=0.2,
            is_valid=True,
            is_ready=False,
            reason=RelativeMotionReason.CANDIDATE_PERSISTENCE_NOT_SATISFIED,
        )

    committed = RelativeMotionAssessment(
        classification=RelativeMotionClass.TARGET_APPROACH,
        candidate_classification=None,
        timestamp_seconds=1.0,
        distance_trend=trend,
        own_motion_evidence=motion,
        stationary_baseline_ready=True,
        candidate_duration_seconds=0.0,
        is_valid=True,
        is_ready=True,
        reason=RelativeMotionReason.TARGET_APPROACH_EVIDENCE,
    )
    pending = RelativeMotionAssessment(
        classification=RelativeMotionClass.STABLE_OR_RECEDING,
        candidate_classification=RelativeMotionClass.TARGET_APPROACH,
        timestamp_seconds=1.0,
        distance_trend=trend,
        own_motion_evidence=motion,
        stationary_baseline_ready=True,
        candidate_duration_seconds=0.2,
        is_valid=True,
        is_ready=False,
        reason=RelativeMotionReason.CANDIDATE_PERSISTENCE_NOT_SATISFIED,
    )

    assert committed.classification is RelativeMotionClass.TARGET_APPROACH
    assert committed.stationary_baseline_ready
    assert pending.candidate_classification is RelativeMotionClass.TARGET_APPROACH
    assert pending.stationary_baseline_ready
