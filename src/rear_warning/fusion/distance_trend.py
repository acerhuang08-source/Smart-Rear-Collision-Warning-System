"""Streaming, I/O-free robust range-slope estimation."""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from numbers import Real

from .models import (
    DistanceTrend,
    DistanceTrendReason,
    DistanceTrendThresholds,
)

_MAD_TO_STANDARD_DEVIATION = 1.4826


@dataclass(frozen=True, slots=True)
class _DistanceSample:
    timestamp_seconds: float
    distance_m: float


def _finite_non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def _positive_distance_cm(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError("distance_cm must be a number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("distance_cm must be a positive finite number")
    return result


class DistanceTrendEstimator:
    """Estimate robust relative radial range change from timestamped distances.

    Input timestamps must be strictly increasing monotonic-clock values. Input
    distances are centimetres and are converted to metres before estimation.
    The class performs no I/O and never reads a clock itself.
    """

    def __init__(
        self,
        thresholds: DistanceTrendThresholds | None = None,
    ) -> None:
        self.thresholds = thresholds or DistanceTrendThresholds()
        self._samples: deque[_DistanceSample] = deque()

    def reset(self) -> None:
        """Discard all history without performing any external operation."""
        self._samples.clear()

    def update(
        self,
        timestamp_seconds: object,
        distance_cm: object,
    ) -> DistanceTrend:
        """Validate and add one sample, then return the current trend.

        Invalid scalar values and non-increasing timestamps are rejected before
        estimator state changes. A large forward gap is a valid clock value but
        a discontinuous series, so it resets history and returns an explicit
        non-ready result.
        """
        timestamp = _finite_non_negative("timestamp_seconds", timestamp_seconds)
        distance_m = _positive_distance_cm(distance_cm) / 100.0

        if self._samples:
            previous_timestamp = self._samples[-1].timestamp_seconds
            if timestamp == previous_timestamp:
                raise ValueError("timestamp_seconds must not repeat")
            if timestamp < previous_timestamp:
                raise ValueError("timestamp_seconds must be strictly increasing")
            if (
                timestamp - previous_timestamp
                > self.thresholds.maximum_gap_seconds
            ):
                self._samples.clear()
                self._samples.append(_DistanceSample(timestamp, distance_m))
                return self._not_ready(
                    DistanceTrendReason.LARGE_TIME_GAP,
                    is_valid=False,
                    inlier_count=len(self._samples),
                )

        self._samples.append(_DistanceSample(timestamp, distance_m))
        self._trim_window(timestamp)
        return self._estimate()

    def _trim_window(self, newest_timestamp: float) -> None:
        oldest_allowed = newest_timestamp - self.thresholds.window_duration_seconds
        while (
            len(self._samples) > 1
            and self._samples[0].timestamp_seconds < oldest_allowed
        ):
            self._samples.popleft()
        while len(self._samples) > self.thresholds.maximum_samples:
            self._samples.popleft()

    def _estimate(self) -> DistanceTrend:
        sample_count = len(self._samples)
        span = self._window_span()
        if sample_count < self.thresholds.minimum_samples:
            return self._not_ready(
                DistanceTrendReason.INSUFFICIENT_SAMPLES,
                inlier_count=sample_count,
            )
        if span < self.thresholds.minimum_span_seconds:
            return self._not_ready(
                DistanceTrendReason.INSUFFICIENT_SPAN,
                inlier_count=sample_count,
            )

        samples = tuple(self._samples)
        seed_slope = self._median_pairwise_slope(samples)
        seed_intercept = statistics.median(
            sample.distance_m - seed_slope * sample.timestamp_seconds
            for sample in samples
        )
        residuals = tuple(
            sample.distance_m
            - (seed_intercept + seed_slope * sample.timestamp_seconds)
            for sample in samples
        )
        residual_center = statistics.median(residuals)
        residual_mad = statistics.median(
            abs(residual - residual_center) for residual in residuals
        )
        rejection_limit = max(
            self.thresholds.outlier_residual_floor_m,
            self.thresholds.outlier_mad_scale
            * _MAD_TO_STANDARD_DEVIATION
            * residual_mad,
        )
        inliers = tuple(
            sample
            for sample, residual in zip(samples, residuals, strict=True)
            if abs(residual - residual_center) <= rejection_limit
        )
        if len(inliers) < self.thresholds.minimum_samples:
            return self._not_ready(
                DistanceTrendReason.INSUFFICIENT_INLIERS,
                is_valid=False,
                inlier_count=len(inliers),
            )

        slope, intercept = self._ordinary_least_squares(inliers)
        if slope is None or intercept is None:
            return self._not_ready(
                DistanceTrendReason.DEGENERATE_TIMESTAMPS,
                is_valid=False,
                inlier_count=len(inliers),
            )
        residual_rms = math.sqrt(
            math.fsum(
                (
                    sample.distance_m
                    - (intercept + slope * sample.timestamp_seconds)
                )
                ** 2
                for sample in inliers
            )
            / len(inliers)
        )
        closing_rate = max(0.0, -slope)
        return DistanceTrend(
            sample_count=sample_count,
            inlier_count=len(inliers),
            window_span_seconds=span,
            range_slope_mps=slope,
            closing_rate_mps=closing_rate,
            residual_rms_m=residual_rms,
            is_valid=True,
            is_ready=True,
            reason=None,
        )

    @staticmethod
    def _median_pairwise_slope(samples: tuple[_DistanceSample, ...]) -> float:
        slopes = [
            (right.distance_m - left.distance_m)
            / (right.timestamp_seconds - left.timestamp_seconds)
            for index, left in enumerate(samples[:-1])
            for right in samples[index + 1 :]
        ]
        return float(statistics.median(slopes))

    @staticmethod
    def _ordinary_least_squares(
        samples: tuple[_DistanceSample, ...],
    ) -> tuple[float | None, float | None]:
        count = len(samples)
        timestamp_mean = math.fsum(
            sample.timestamp_seconds for sample in samples
        ) / count
        distance_mean = math.fsum(sample.distance_m for sample in samples) / count
        denominator = math.fsum(
            (sample.timestamp_seconds - timestamp_mean) ** 2
            for sample in samples
        )
        if denominator <= 0 or not math.isfinite(denominator):
            return None, None
        numerator = math.fsum(
            (sample.timestamp_seconds - timestamp_mean)
            * (sample.distance_m - distance_mean)
            for sample in samples
        )
        slope = numerator / denominator
        intercept = distance_mean - slope * timestamp_mean
        if not math.isfinite(slope) or not math.isfinite(intercept):
            return None, None
        return slope, intercept

    def _window_span(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        return (
            self._samples[-1].timestamp_seconds
            - self._samples[0].timestamp_seconds
        )

    def _not_ready(
        self,
        reason: DistanceTrendReason,
        *,
        is_valid: bool = True,
        inlier_count: int,
    ) -> DistanceTrend:
        return DistanceTrend(
            sample_count=len(self._samples),
            inlier_count=inlier_count,
            window_span_seconds=self._window_span(),
            range_slope_mps=None,
            closing_rate_mps=None,
            residual_rms_m=None,
            is_valid=is_valid,
            is_ready=False,
            reason=reason,
        )
