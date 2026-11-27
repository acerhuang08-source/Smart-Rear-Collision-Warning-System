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


class RelativeMotionClass(str, Enum):
    """Explainable relative-motion classes produced before warning policy."""

    STABLE_OR_RECEDING = "stable_or_receding"
    STATIC_OR_SELF_APPROACH = "static_or_self_approach"
    TARGET_APPROACH = "target_approach"
    UNCERTAIN = "uncertain"


class RelativeMotionReason(str, Enum):
    """Primary reason for a relative-motion assessment."""

    DISTANCE_TREND_NOT_READY = "distance_trend_not_ready"
    OWN_MOTION_NOT_READY = "own_motion_not_ready"
    SYNC_UNHEALTHY = "sync_unhealthy"
    MOTION_STALE = "motion_stale"
    AMBIGUOUS_CLOSING = "ambiguous_closing"
    STATIONARY_BASELINE_NOT_ESTABLISHED = (
        "stationary_baseline_not_established"
    )
    SELF_TRANSLATIONAL_EVIDENCE = "self_translational_evidence"
    SELF_ROTATIONAL_EVIDENCE = "self_rotational_evidence"
    SELF_TRANSLATIONAL_AND_ROTATIONAL_EVIDENCE = (
        "self_translational_and_rotational_evidence"
    )
    STABLE_OR_RECEDING_RANGE = "stable_or_receding_range"
    TARGET_APPROACH_EVIDENCE = "target_approach_evidence"
    CANDIDATE_PERSISTENCE_NOT_SATISFIED = (
        "candidate_persistence_not_satisfied"
    )
    TIMESTAMP_DISCONTINUITY = "timestamp_discontinuity"


@dataclass(frozen=True, slots=True)
class RelativeMotionThresholds:
    """Injectable software defaults for relative-motion classification.

    These values are deterministic feature thresholds, not road-calibrated
    collision-risk limits or statistical probabilities.
    """

    stable_range_rate_deadband_mps: float = 0.05
    minimum_closing_rate_mps: float = 0.15
    maximum_motion_age_seconds: float = 0.2
    maximum_update_gap_seconds: float = 0.25
    stable_persistence_seconds: float = 0.3
    self_approach_persistence_seconds: float = 0.25
    target_approach_persistence_seconds: float = 0.4
    uncertain_persistence_seconds: float = 0.3
    stationary_baseline_duration_seconds: float = 0.8
    stationary_baseline_max_age_seconds: float = 1.0

    def __post_init__(self) -> None:
        for name in (
            "stable_range_rate_deadband_mps",
            "minimum_closing_rate_mps",
            "maximum_motion_age_seconds",
            "maximum_update_gap_seconds",
            "stable_persistence_seconds",
            "self_approach_persistence_seconds",
            "target_approach_persistence_seconds",
            "uncertain_persistence_seconds",
            "stationary_baseline_duration_seconds",
            "stationary_baseline_max_age_seconds",
        ):
            object.__setattr__(
                self,
                name,
                _finite_number(name, getattr(self, name), positive=True),
            )
        if (
            self.stable_range_rate_deadband_mps
            >= self.minimum_closing_rate_mps
        ):
            raise ValueError(
                "stable_range_rate_deadband_mps must be less than "
                "minimum_closing_rate_mps"
            )
        if (
            self.stationary_baseline_max_age_seconds
            < self.target_approach_persistence_seconds
        ):
            raise ValueError(
                "stationary_baseline_max_age_seconds must be at least "
                "target_approach_persistence_seconds"
            )


@dataclass(frozen=True, slots=True)
class RelativeMotionAssessment:
    """One I/O-free, pre-warning relative-motion classification.

    ``TARGET_APPROACH`` requires a recent stationary baseline. Absence of
    own-motion evidence is never treated as proof that the platform is still:
    constant-velocity straight-line self motion can be unobservable to the
    short-window IMU evidence and therefore remains ``UNCERTAIN`` without that
    baseline. ``STATIC_OR_SELF_APPROACH`` means closing is explainable by
    observed self motion; it does not prove that the target is a static wall.
    """

    classification: RelativeMotionClass
    candidate_classification: RelativeMotionClass | None
    timestamp_seconds: float
    distance_trend: DistanceTrend
    own_motion_evidence: OwnMotionEvidence
    stationary_baseline_ready: bool
    candidate_duration_seconds: float
    is_valid: bool
    is_ready: bool
    reason: RelativeMotionReason

    def __post_init__(self) -> None:
        if not isinstance(self.classification, RelativeMotionClass):
            raise TypeError("classification must be a RelativeMotionClass")
        if self.candidate_classification is not None and not isinstance(
            self.candidate_classification, RelativeMotionClass
        ):
            raise TypeError(
                "candidate_classification must be a RelativeMotionClass or None"
            )
        if self.candidate_classification is self.classification:
            raise ValueError(
                "candidate_classification must differ from classification"
            )
        object.__setattr__(
            self,
            "timestamp_seconds",
            _finite_number(
                "timestamp_seconds", self.timestamp_seconds, positive=False
            ),
        )
        if not isinstance(self.distance_trend, DistanceTrend):
            raise TypeError("distance_trend must be a DistanceTrend")
        if not isinstance(self.own_motion_evidence, OwnMotionEvidence):
            raise TypeError("own_motion_evidence must be an OwnMotionEvidence")
        if not isinstance(self.stationary_baseline_ready, bool):
            raise TypeError("stationary_baseline_ready must be a bool")
        if (
            self.classification is RelativeMotionClass.TARGET_APPROACH
            and not self.stationary_baseline_ready
        ):
            raise ValueError(
                "TARGET_APPROACH classification requires a stationary baseline"
            )
        if (
            self.candidate_classification is RelativeMotionClass.TARGET_APPROACH
            and not self.stationary_baseline_ready
        ):
            raise ValueError(
                "TARGET_APPROACH candidate requires a stationary baseline"
            )
        duration = _finite_number(
            "candidate_duration_seconds",
            self.candidate_duration_seconds,
            positive=False,
        )
        object.__setattr__(self, "candidate_duration_seconds", duration)
        if self.candidate_classification is None and duration != 0.0:
            raise ValueError(
                "candidate_duration_seconds must be zero without a candidate"
            )
        for name in ("is_valid", "is_ready"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a bool")
        if not isinstance(self.reason, RelativeMotionReason):
            raise TypeError("reason must be a RelativeMotionReason")
        if self.is_ready and not self.is_valid:
            raise ValueError("a ready assessment must be valid")
        if self.is_ready and self.candidate_classification is not None:
            raise ValueError("a ready assessment cannot have a pending candidate")
        if not self.is_valid and self.classification is not RelativeMotionClass.UNCERTAIN:
            raise ValueError("an invalid assessment must be UNCERTAIN")
