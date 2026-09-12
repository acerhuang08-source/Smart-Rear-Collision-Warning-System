"""Streaming, I/O-free short-term own-motion evidence estimation."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real

from .models import OwnMotionEvidence, OwnMotionReason, OwnMotionThresholds


class InconsistentMotionSampleError(ValueError):
    """A repeated motion timestamp carried a different validated payload."""


@dataclass(frozen=True, slots=True)
class _MotionSample:
    timestamp_seconds: float
    linear_acceleration_mps2: tuple[float, float, float]
    quaternion: tuple[float, float, float, float]


def _finite_non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def _finite_vector(
    name: str,
    values: Sequence[object],
    *,
    length: int,
) -> tuple[float, ...]:
    if isinstance(values, (str, bytes, bytearray)) or not isinstance(
        values, Sequence
    ):
        raise TypeError(f"{name} must be a sequence")
    if len(values) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    result: list[float] = []
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, Real):
            raise TypeError(f"{name}[{index}] must be a number")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError(f"{name}[{index}] must be finite")
        result.append(converted)
    return tuple(result)


def _normalized_quaternion(
    values: Sequence[object],
) -> tuple[float, float, float, float]:
    quaternion = _finite_vector("quaternion", values, length=4)
    norm = math.hypot(*quaternion)
    if norm == 0.0:
        raise ValueError("quaternion must have a non-zero norm")
    normalized = tuple(value / norm for value in quaternion)

    # q and -q encode the same orientation. Canonicalizing their sign also
    # makes equivalent repeated samples compare equal during timestamp dedup.
    for component in normalized:
        if component == 0.0:
            continue
        if component < 0.0:
            normalized = tuple(-value for value in normalized)
        break
    return (
        normalized[0],
        normalized[1],
        normalized[2],
        normalized[3],
    )


def _quaternion_angle(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> float:
    dot = abs(math.fsum(a * b for a, b in zip(left, right, strict=True)))
    return 2.0 * math.acos(min(max(dot, 0.0), 1.0))


class OwnMotionEstimator:
    """Measure recent acceleration and orientation-change evidence.

    The estimator never reads a clock or performs I/O. It cannot detect every
    self-motion case: constant-velocity straight-line motion may produce almost
    no linear acceleration or orientation change. Its output is evidence for a
    later classifier, not velocity, direction, target identity, or warning state.
    """

    def __init__(self, thresholds: OwnMotionThresholds | None = None) -> None:
        self.thresholds = thresholds or OwnMotionThresholds()
        self._samples: deque[_MotionSample] = deque()
        self._current_evidence: OwnMotionEvidence | None = None

    def reset(self) -> None:
        """Discard all history without performing an external operation."""
        self._samples.clear()
        self._current_evidence = None

    def update(
        self,
        timestamp_seconds: object,
        *,
        linear_acceleration_mps2: Sequence[object],
        quaternion: Sequence[object],
    ) -> OwnMotionEvidence:
        """Validate one unique motion sample and update short-window evidence.

        An identical payload at the latest timestamp is a synchronization
        duplicate and returns the exact current evidence without adding weight.
        A conflicting duplicate or backwards timestamp raises before history is
        modified. A large forward gap starts a new, not-ready segment.
        """
        timestamp = _finite_non_negative("timestamp_seconds", timestamp_seconds)
        acceleration_values = _finite_vector(
            "linear_acceleration_mps2",
            linear_acceleration_mps2,
            length=3,
        )
        acceleration = (
            acceleration_values[0],
            acceleration_values[1],
            acceleration_values[2],
        )
        normalized_quaternion = _normalized_quaternion(quaternion)
        candidate = _MotionSample(timestamp, acceleration, normalized_quaternion)

        if self._samples:
            previous = self._samples[-1]
            if timestamp == previous.timestamp_seconds:
                if candidate != previous:
                    raise InconsistentMotionSampleError(
                        "repeated timestamp_seconds has a conflicting motion payload"
                    )
                assert self._current_evidence is not None
                return self._current_evidence
            if timestamp < previous.timestamp_seconds:
                raise ValueError("timestamp_seconds must be strictly increasing")
            if timestamp - previous.timestamp_seconds > self.thresholds.maximum_gap_seconds:
                self._samples.clear()
                self._samples.append(candidate)
                self._current_evidence = self._evidence(
                    forced_reason=OwnMotionReason.LARGE_TIME_GAP,
                    is_valid=False,
                )
                return self._current_evidence

        self._samples.append(candidate)
        self._trim_window(timestamp)
        self._current_evidence = self._evidence()
        return self._current_evidence

    def _trim_window(self, newest_timestamp: float) -> None:
        oldest_allowed = newest_timestamp - self.thresholds.window_duration_seconds
        while (
            len(self._samples) > 1
            and self._samples[0].timestamp_seconds < oldest_allowed
        ):
            self._samples.popleft()
        while len(self._samples) > self.thresholds.maximum_samples:
            self._samples.popleft()

    def _evidence(
        self,
        *,
        forced_reason: OwnMotionReason | None = None,
        is_valid: bool = True,
    ) -> OwnMotionEvidence:
        samples = tuple(self._samples)
        sample_count = len(samples)
        span = (
            0.0
            if sample_count < 2
            else samples[-1].timestamp_seconds - samples[0].timestamp_seconds
        )
        acceleration_magnitudes = tuple(
            math.hypot(*sample.linear_acceleration_mps2) for sample in samples
        )
        acceleration_rms = math.sqrt(
            math.fsum(value * value for value in acceleration_magnitudes)
            / sample_count
        )
        acceleration_peak = max(acceleration_magnitudes)
        delta_velocity = [0.0, 0.0, 0.0]
        cumulative_orientation = 0.0
        for left, right in zip(samples, samples[1:], strict=False):
            elapsed = right.timestamp_seconds - left.timestamp_seconds
            for axis in range(3):
                delta_velocity[axis] += (
                    0.5
                    * (
                        left.linear_acceleration_mps2[axis]
                        + right.linear_acceleration_mps2[axis]
                    )
                    * elapsed
                )
            cumulative_orientation += _quaternion_angle(
                left.quaternion, right.quaternion
            )
        delta_v_evidence = math.hypot(*delta_velocity)
        net_orientation = (
            0.0
            if sample_count < 2
            else _quaternion_angle(samples[0].quaternion, samples[-1].quaternion)
        )

        reason = forced_reason
        if reason is None and sample_count < self.thresholds.minimum_unique_samples:
            reason = OwnMotionReason.INSUFFICIENT_SAMPLES
        if reason is None and span < self.thresholds.minimum_span_seconds:
            reason = OwnMotionReason.INSUFFICIENT_SPAN
        ready = reason is None
        translational = ready and (
            acceleration_rms >= self.thresholds.linear_accel_rms_threshold_mps2
            or acceleration_peak >= self.thresholds.linear_accel_peak_threshold_mps2
            or delta_v_evidence >= self.thresholds.delta_v_evidence_threshold_mps
        )
        rotational = ready and (
            cumulative_orientation
            >= self.thresholds.cumulative_orientation_threshold_radians
        )
        return OwnMotionEvidence(
            unique_sample_count=sample_count,
            window_span_seconds=span,
            linear_accel_rms_mps2=acceleration_rms,
            linear_accel_peak_mps2=acceleration_peak,
            delta_v_evidence_mps=delta_v_evidence,
            net_orientation_change_radians=net_orientation,
            cumulative_orientation_change_radians=cumulative_orientation,
            translational_motion_detected=translational,
            rotational_motion_detected=rotational,
            is_valid=is_valid,
            is_ready=ready,
            reason=reason,
        )
