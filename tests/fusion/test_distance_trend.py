"""Synthetic tests for robust timestamp-aware distance trend estimation."""

from collections.abc import Iterable

import pytest

from rear_warning.fusion import (
    DistanceTrend,
    DistanceTrendEstimator,
    DistanceTrendReason,
    DistanceTrendThresholds,
)


def estimate(
    samples: Iterable[tuple[float, float]],
    *,
    thresholds: DistanceTrendThresholds | None = None,
) -> DistanceTrend:
    estimator = DistanceTrendEstimator(thresholds)
    result: DistanceTrend | None = None
    for timestamp, distance_cm in samples:
        result = estimator.update(timestamp, distance_cm)
    assert result is not None
    return result


def linear_samples(
    timestamps: Iterable[float],
    *,
    initial_distance_m: float,
    slope_mps: float,
) -> list[tuple[float, float]]:
    values = list(timestamps)
    origin = values[0]
    return [
        (
            timestamp,
            (initial_distance_m + slope_mps * (timestamp - origin)) * 100.0,
        )
        for timestamp in values
    ]


def test_constant_two_metres_has_zero_slope() -> None:
    trend = estimate((index / 10, 200.0) for index in range(11))

    assert trend.is_ready and trend.is_valid
    assert trend.range_slope_mps == pytest.approx(0.0, abs=1e-12)
    assert trend.closing_rate_mps == 0.0
    assert trend.residual_rms_m == pytest.approx(0.0, abs=1e-12)


def test_linear_approach_has_negative_slope_and_positive_closing_rate() -> None:
    samples = linear_samples(
        (index / 10 for index in range(11)),
        initial_distance_m=5.0,
        slope_mps=-2.0,
    )

    trend = estimate(samples)

    assert trend.range_slope_mps == pytest.approx(-2.0)
    assert trend.closing_rate_mps == pytest.approx(2.0)


def test_linear_receding_has_positive_slope_and_zero_closing_rate() -> None:
    samples = linear_samples(
        (index / 10 for index in range(11)),
        initial_distance_m=2.0,
        slope_mps=0.75,
    )

    trend = estimate(samples)

    assert trend.range_slope_mps == pytest.approx(0.75)
    assert trend.closing_rate_mps == 0.0


def test_small_deterministic_noise_preserves_trend() -> None:
    timestamps = [index / 20 for index in range(21)]
    samples = linear_samples(
        timestamps,
        initial_distance_m=4.0,
        slope_mps=-0.8,
    )
    noisy = [
        (timestamp, distance_cm + (-1.0, 0.5, 1.0, -0.5)[index % 4])
        for index, (timestamp, distance_cm) in enumerate(samples)
    ]

    trend = estimate(noisy)

    assert trend.range_slope_mps == pytest.approx(-0.8, abs=0.02)
    assert trend.closing_rate_mps == pytest.approx(0.8, abs=0.02)
    assert trend.residual_rms_m is not None
    assert trend.residual_rms_m < 0.02


@pytest.mark.parametrize("spike_index", [0, 10, 20])
def test_single_extreme_spike_is_rejected_before_final_regression(
    spike_index: int,
) -> None:
    timestamps = [index / 20 for index in range(21)]
    samples = linear_samples(
        timestamps,
        initial_distance_m=4.0,
        slope_mps=-1.0,
    )
    samples[spike_index] = (samples[spike_index][0], 5000.0)

    trend = estimate(samples)

    assert trend.range_slope_mps == pytest.approx(-1.0, abs=1e-12)
    assert trend.closing_rate_mps == pytest.approx(1.0, abs=1e-12)
    assert trend.sample_count > trend.inlier_count
    assert trend.rejected_sample_count == 1


def test_irregular_timestamps_use_actual_elapsed_time() -> None:
    timestamps = [0.0, 0.03, 0.11, 0.22, 0.38, 0.57, 0.79, 1.0]

    trend = estimate(
        linear_samples(
            timestamps,
            initial_distance_m=3.0,
            slope_mps=-0.6,
        )
    )

    assert trend.window_span_seconds == pytest.approx(1.0)
    assert trend.range_slope_mps == pytest.approx(-0.6)


def test_first_warm_up_sample_has_not_been_rejected() -> None:
    trend = DistanceTrendEstimator().update(0.0, 200.0)

    assert trend.sample_count == 1
    assert trend.inlier_count == 1
    assert trend.rejected_sample_count == 0
    assert trend.reason is DistanceTrendReason.INSUFFICIENT_SAMPLES


def test_too_few_samples_is_not_ready_without_rejections() -> None:
    trend = estimate([(0.0, 200.0), (0.1, 199.0), (0.2, 198.0)])

    assert trend.is_valid
    assert not trend.is_ready
    assert trend.reason is DistanceTrendReason.INSUFFICIENT_SAMPLES
    assert trend.range_slope_mps is None
    assert trend.inlier_count == trend.sample_count == 3
    assert trend.rejected_sample_count == 0


def test_short_span_is_not_ready() -> None:
    trend = estimate(
        [(index / 100, 200.0 - index) for index in range(5)],
        thresholds=DistanceTrendThresholds(minimum_span_seconds=0.2),
    )

    assert trend.is_valid
    assert not trend.is_ready
    assert trend.reason is DistanceTrendReason.INSUFFICIENT_SPAN
    assert trend.inlier_count == trend.sample_count == 5
    assert trend.rejected_sample_count == 0


def test_repeated_timestamp_is_rejected_without_mutating_history() -> None:
    estimator = DistanceTrendEstimator()
    estimator.update(1.0, 200.0)

    with pytest.raises(ValueError, match="must not repeat"):
        estimator.update(1.0, 199.0)

    result = estimator.update(1.1, 198.0)
    assert result.sample_count == 2


def test_clock_backwards_is_rejected_without_mutating_history() -> None:
    estimator = DistanceTrendEstimator()
    estimator.update(1.0, 200.0)

    with pytest.raises(ValueError, match="strictly increasing"):
        estimator.update(0.9, 199.0)

    result = estimator.update(1.1, 198.0)
    assert result.sample_count == 2


def test_large_time_gap_resets_history_and_reports_discontinuity() -> None:
    estimator = DistanceTrendEstimator()
    for index in range(5):
        estimator.update(index / 10, 200.0 - index)

    result = estimator.update(1.0, 190.0)

    assert not result.is_valid
    assert not result.is_ready
    assert result.reason is DistanceTrendReason.LARGE_TIME_GAP
    assert result.sample_count == 1
    assert result.inlier_count == 1
    assert result.rejected_sample_count == 0
    assert result.window_span_seconds == 0.0


@pytest.mark.parametrize(
    ("timestamp", "distance", "error"),
    [
        (float("nan"), 100.0, ValueError),
        (float("inf"), 100.0, ValueError),
        (True, 100.0, TypeError),
        ("1.0", 100.0, TypeError),
        (0.0, float("nan"), ValueError),
        (0.0, float("inf"), ValueError),
        (0.0, True, TypeError),
        (0.0, "100", TypeError),
        (0.0, 0.0, ValueError),
        (0.0, -1.0, ValueError),
    ],
)
def test_invalid_input_is_rejected(
    timestamp: object,
    distance: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        DistanceTrendEstimator().update(timestamp, distance)


@pytest.mark.parametrize(
    "timestamps",
    [
        [index / 100 for index in range(101)],
        [index / 10 for index in range(11)],
        [0.0, 0.04, 0.13, 0.29, 0.46, 0.63, 0.81, 1.0],
    ],
    ids=["100_hz", "10_hz", "irregular"],
)
def test_ideal_slope_is_independent_of_sample_rate(
    timestamps: list[float],
) -> None:
    trend = estimate(
        linear_samples(
            timestamps,
            initial_distance_m=4.0,
            slope_mps=-0.75,
        )
    )

    assert trend.range_slope_mps == pytest.approx(-0.75, abs=1e-10)
    assert trend.closing_rate_mps == pytest.approx(0.75, abs=1e-10)
