"""Tests for immutable distance-trend models and thresholds."""

from dataclasses import FrozenInstanceError

import pytest

from rear_warning.fusion import (
    DistanceTrend,
    DistanceTrendReason,
    DistanceTrendThresholds,
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
