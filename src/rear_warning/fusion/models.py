"""Immutable models for hardware-free sensor-fusion feature extraction."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from numbers import Real


def _finite_number(name: str, value: object, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or (result <= 0 if positive else result < 0):
        qualifier = "positive " if positive else "non-negative "
        raise ValueError(f"{name} must be a {qualifier}finite number")
    return result


def _finite_scalar(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _integer(name: str, value: object, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


class DistanceTrendReason(str, Enum):
    """Why a valid numerical trend is not currently available."""

    INSUFFICIENT_SAMPLES = "insufficient_samples"
    INSUFFICIENT_SPAN = "insufficient_span"
    LARGE_TIME_GAP = "large_time_gap"
    INSUFFICIENT_INLIERS = "insufficient_inliers"
    DEGENERATE_TIMESTAMPS = "degenerate_timestamps"


@dataclass(frozen=True, slots=True)
class DistanceTrendThresholds:
    """Injectable bounds for a short, robust distance window.

    The defaults are initial software values, not hardware- or road-tuned
    thresholds. A finite sample cap keeps robust estimation work bounded.
    """

    window_duration_seconds: float = 1.0
    minimum_samples: int = 5
    minimum_span_seconds: float = 0.25
    maximum_gap_seconds: float = 0.25
    maximum_samples: int = 128
    outlier_mad_scale: float = 3.5
    outlier_residual_floor_m: float = 0.05

    def __post_init__(self) -> None:
        for name in (
            "window_duration_seconds",
            "minimum_span_seconds",
            "maximum_gap_seconds",
            "outlier_mad_scale",
            "outlier_residual_floor_m",
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(name, getattr(self, name), positive=True),
            )
        object.__setattr__(
            self,
            "minimum_samples",
            _integer("minimum_samples", self.minimum_samples, minimum=2),
        )
        object.__setattr__(
            self,
            "maximum_samples",
            _integer("maximum_samples", self.maximum_samples, minimum=2),
        )
        if self.minimum_span_seconds > self.window_duration_seconds:
            raise ValueError(
                "minimum_span_seconds must not exceed window_duration_seconds"
            )
        if self.maximum_gap_seconds > self.window_duration_seconds:
            raise ValueError(
                "maximum_gap_seconds must not exceed window_duration_seconds"
            )
        if self.maximum_samples < self.minimum_samples:
            raise ValueError(
                "maximum_samples must be at least minimum_samples"
            )


@dataclass(frozen=True, slots=True)
class DistanceTrend:
    """One robust range-change estimate in metres and seconds.

    ``range_slope_mps`` is relative radial range change along the TFMini beam.
    It can result from target motion, self motion, rotation/beam geometry, or a
    combination. It is not, by itself, the speed of a rear target.

    A continuous but still-warming window is valid and not ready. A data gap or
    unusable inlier set makes the current result invalid and not ready.
    ``residual_rms_m`` describes the final least-squares inliers; rejected
    samples remain visible through the sample and inlier counts.
    """

    sample_count: int
    inlier_count: int
    window_span_seconds: float
    range_slope_mps: float | None
    closing_rate_mps: float | None
    residual_rms_m: float | None
    is_valid: bool
    is_ready: bool
    reason: DistanceTrendReason | None

    def __post_init__(self) -> None:
        sample_count = _integer("sample_count", self.sample_count, minimum=0)
        inlier_count = _integer("inlier_count", self.inlier_count, minimum=0)
        if inlier_count > sample_count:
            raise ValueError("inlier_count must not exceed sample_count")
        object.__setattr__(
            self,
            "window_span_seconds",
            _finite_number(
                "window_span_seconds", self.window_span_seconds, positive=False
            ),
        )
        if not isinstance(self.is_valid, bool):
            raise TypeError("is_valid must be a bool")
        if not isinstance(self.is_ready, bool):
            raise TypeError("is_ready must be a bool")
        if self.reason is not None and not isinstance(
            self.reason, DistanceTrendReason
        ):
            raise TypeError("reason must be a DistanceTrendReason or None")

        estimates = (
            self.range_slope_mps,
            self.closing_rate_mps,
            self.residual_rms_m,
        )
        if self.is_ready:
            if not self.is_valid:
                raise ValueError("a ready trend must be valid")
            if self.reason is not None:
                raise ValueError("a ready trend must not contain a reason")
            if any(value is None for value in estimates):
                raise ValueError("a ready trend requires all numerical estimates")
            slope = _finite_scalar("range_slope_mps", self.range_slope_mps)
            closing = _finite_number(
                "closing_rate_mps", self.closing_rate_mps, positive=False
            )
            residual = _finite_number(
                "residual_rms_m", self.residual_rms_m, positive=False
            )
            object.__setattr__(self, "closing_rate_mps", closing)
            object.__setattr__(self, "residual_rms_m", residual)
            object.__setattr__(self, "range_slope_mps", slope)
            expected_closing = max(0.0, -slope)
            if not math.isclose(closing, expected_closing, abs_tol=1e-12):
                raise ValueError(
                    "closing_rate_mps must equal max(0, -range_slope_mps)"
                )
            if inlier_count < 2:
                raise ValueError("a ready trend requires at least two inliers")
        else:
            if self.reason is None:
                raise ValueError("a non-ready trend requires a reason")
            if any(value is not None for value in estimates):
                raise ValueError(
                    "a non-ready trend must not contain numerical estimates"
                )

    @property
    def rejected_sample_count(self) -> int:
        """Return the number of samples excluded by robust fitting."""
        return self.sample_count - self.inlier_count


class OwnMotionReason(str, Enum):
    """Why a valid own-motion evidence window is not currently ready."""

    INSUFFICIENT_SAMPLES = "insufficient_samples"
    INSUFFICIENT_SPAN = "insufficient_span"
    LARGE_TIME_GAP = "large_time_gap"


@dataclass(frozen=True, slots=True)
class OwnMotionThresholds:
    """Injectable initial software thresholds for short-term IMU evidence.

    These defaults have not been tuned on a road or moving vehicle. They detect
    short-term acceleration and orientation-change evidence; they do not define
    speed, travel direction, target motion, or a warning decision.
    """

    window_duration_seconds: float = 1.0
    minimum_unique_samples: int = 4
    minimum_span_seconds: float = 0.3
    maximum_gap_seconds: float = 0.25
    maximum_samples: int = 32
    linear_accel_rms_threshold_mps2: float = 0.35
    linear_accel_peak_threshold_mps2: float = 1.0
    delta_v_evidence_threshold_mps: float = 0.2
    cumulative_orientation_threshold_radians: float = math.radians(10.0)

    def __post_init__(self) -> None:
        for name in (
            "window_duration_seconds",
            "minimum_span_seconds",
            "maximum_gap_seconds",
            "linear_accel_rms_threshold_mps2",
            "linear_accel_peak_threshold_mps2",
            "delta_v_evidence_threshold_mps",
            "cumulative_orientation_threshold_radians",
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(name, getattr(self, name), positive=True),
            )
        object.__setattr__(
            self,
            "minimum_unique_samples",
            _integer(
                "minimum_unique_samples",
                self.minimum_unique_samples,
                minimum=2,
            ),
        )
        object.__setattr__(
            self,
            "maximum_samples",
            _integer("maximum_samples", self.maximum_samples, minimum=2),
        )
        if self.minimum_span_seconds > self.window_duration_seconds:
            raise ValueError(
                "minimum_span_seconds must not exceed window_duration_seconds"
            )
        if self.maximum_gap_seconds > self.window_duration_seconds:
            raise ValueError(
                "maximum_gap_seconds must not exceed window_duration_seconds"
            )
        if self.maximum_samples < self.minimum_unique_samples:
            raise ValueError(
                "maximum_samples must be at least minimum_unique_samples"
            )


@dataclass(frozen=True, slots=True)
class OwnMotionEvidence:
    """Short-window acceleration and orientation-change evidence.

    The acceleration metrics do not identify travel direction.
    ``delta_v_evidence_mps`` is the norm of short-window integrated XYZ linear
    acceleration, not absolute velocity or a speedometer reading. Orientation
    values describe net and cumulative quaternion changes within the window.

    Evidence cannot prove whether the platform approaches a wall or a rear
    target approaches the platform. Constant-velocity straight-line motion can
    have near-zero acceleration and orientation change, so that unobservable
    case must remain uncertain in a later classifier.
    """

    unique_sample_count: int
    window_span_seconds: float
    linear_accel_rms_mps2: float
    linear_accel_peak_mps2: float
    delta_v_evidence_mps: float
    net_orientation_change_radians: float
    cumulative_orientation_change_radians: float
    translational_motion_detected: bool
    rotational_motion_detected: bool
    is_valid: bool
    is_ready: bool
    reason: OwnMotionReason | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "unique_sample_count",
            _integer(
                "unique_sample_count", self.unique_sample_count, minimum=0
            ),
        )
        for name in (
            "window_span_seconds",
            "linear_accel_rms_mps2",
            "linear_accel_peak_mps2",
            "delta_v_evidence_mps",
            "net_orientation_change_radians",
            "cumulative_orientation_change_radians",
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(name, getattr(self, name), positive=False),
            )
        if self.net_orientation_change_radians > math.pi + 1e-12:
            raise ValueError(
                "net_orientation_change_radians must not exceed pi"
            )
        for name in (
            "translational_motion_detected",
            "rotational_motion_detected",
            "is_valid",
            "is_ready",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if self.reason is not None and not isinstance(self.reason, OwnMotionReason):
            raise TypeError("reason must be an OwnMotionReason or None")
        if self.is_ready:
            if not self.is_valid:
                raise ValueError("ready own-motion evidence must be valid")
            if self.reason is not None:
                raise ValueError("ready own-motion evidence must not have a reason")
        elif self.reason is None:
            raise ValueError("non-ready own-motion evidence requires a reason")
        if not self.is_ready and (
            self.translational_motion_detected
            or self.rotational_motion_detected
        ):
            raise ValueError("non-ready own-motion evidence cannot detect motion")
