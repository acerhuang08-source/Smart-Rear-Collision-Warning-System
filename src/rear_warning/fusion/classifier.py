"""I/O-free relative-motion classification from fusion evidence."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real

from .models import (
    DistanceTrend,
    DistanceTrendReason,
    OwnMotionEvidence,
    OwnMotionReason,
    RelativeMotionAssessment,
    RelativeMotionClass,
    RelativeMotionReason,
    RelativeMotionThresholds,
)


class InconsistentClassifierInputError(ValueError):
    """A repeated assessment timestamp carried different validated inputs."""


@dataclass(frozen=True, slots=True)
class _ClassifierInput:
    timestamp_seconds: float
    distance_trend: DistanceTrend
    own_motion_evidence: OwnMotionEvidence
    sync_healthy: bool
    motion_age_seconds: float | None


@dataclass(frozen=True, slots=True)
class _Decision:
    classification: RelativeMotionClass
    reason: RelativeMotionReason


def _finite_non_negative(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def _duration_reached(elapsed: float, required: float) -> bool:
    return elapsed >= required or math.isclose(
        elapsed,
        required,
        abs_tol=1e-12,
    )


class RelativeMotionClassifier:
    """Classify relative radial motion without reading clocks or doing I/O.

    ``TARGET_APPROACH`` is permitted only after a recent, sustained stationary
    baseline. Closing range with no own-motion evidence but without that
    history remains ``UNCERTAIN`` because constant-velocity self motion is not
    observable from short-window acceleration and orientation alone.

    Classification persistence is elapsed-time based. A committed class may be
    retained while a different healthy candidate matures; ``is_ready`` and
    ``candidate_classification`` make that transition explicit.
    """

    def __init__(
        self,
        thresholds: RelativeMotionThresholds | None = None,
    ) -> None:
        self.thresholds = thresholds or RelativeMotionThresholds()
        self._last_timestamp: float | None = None
        self._last_input: _ClassifierInput | None = None
        self._current_assessment: RelativeMotionAssessment | None = None
        self._committed_classification = RelativeMotionClass.UNCERTAIN
        self._candidate_classification: RelativeMotionClass | None = None
        self._candidate_since: float | None = None
        self._stationary_candidate_since: float | None = None
        self._stationary_baseline_last_confirmed: float | None = None

    def reset(self) -> None:
        """Discard classifier, candidate, and stationary-baseline state."""
        self._last_timestamp = None
        self._last_input = None
        self._current_assessment = None
        self._reset_temporal_state()

    def update(
        self,
        timestamp_seconds: object,
        *,
        distance_trend: DistanceTrend,
        own_motion_evidence: OwnMotionEvidence,
        sync_healthy: bool,
        motion_age_seconds: object | None,
    ) -> RelativeMotionAssessment:
        """Validate inputs and update time-based classification state.

        An identical repeated timestamp and payload is a no-op. Conflicting
        duplicates and backwards timestamps raise before any state changes.
        A large timestamp gap invalidates all temporal evidence and starts a
        new segment at the supplied timestamp.
        """
        timestamp = _finite_non_negative("timestamp_seconds", timestamp_seconds)
        if not isinstance(distance_trend, DistanceTrend):
            raise TypeError("distance_trend must be a DistanceTrend")
        if not isinstance(own_motion_evidence, OwnMotionEvidence):
            raise TypeError("own_motion_evidence must be an OwnMotionEvidence")
        if not isinstance(sync_healthy, bool):
            raise TypeError("sync_healthy must be a bool")
        motion_age = (
            None
            if motion_age_seconds is None
            else _finite_non_negative("motion_age_seconds", motion_age_seconds)
        )
        current_input = _ClassifierInput(
            timestamp,
            distance_trend,
            own_motion_evidence,
            sync_healthy,
            motion_age,
        )

        if self._last_timestamp is not None:
            if timestamp == self._last_timestamp:
                if current_input != self._last_input:
                    raise InconsistentClassifierInputError(
                        "repeated timestamp_seconds has conflicting classifier inputs"
                    )
                assert self._current_assessment is not None
                return self._current_assessment
            if timestamp < self._last_timestamp:
                raise ValueError("timestamp_seconds must be strictly increasing")
            if (
                timestamp - self._last_timestamp
                > self.thresholds.maximum_update_gap_seconds
            ):
                self._reset_temporal_state()
                assessment = self._invalid_assessment(
                    current_input,
                    RelativeMotionReason.TIMESTAMP_DISCONTINUITY,
                )
                self._remember(current_input, assessment)
                return assessment

        health_reason = self._health_reason(current_input)
        if health_reason is not None:
            self._reset_temporal_state()
            assessment = self._invalid_assessment(current_input, health_reason)
            self._remember(current_input, assessment)
            return assessment

        decision = self._classify_healthy(timestamp, current_input)
        baseline_ready = self._stationary_baseline_ready(timestamp)

        target_candidate_lost_baseline = (
            self._candidate_classification
            is RelativeMotionClass.TARGET_APPROACH
            and decision.reason
            is RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED
            and not baseline_ready
        )
        # A committed TARGET must not outlive its baseline. Likewise, when a
        # pending TARGET loses the baseline on which it was founded, retaining
        # the older STABLE classification would misdescribe clear closing.
        if target_candidate_lost_baseline or (
            self._committed_classification is RelativeMotionClass.TARGET_APPROACH
            and not baseline_ready
        ):
            self._committed_classification = RelativeMotionClass.UNCERTAIN
            self._clear_candidate()

        assessment = self._advance_candidate(
            timestamp,
            current_input,
            decision,
            baseline_ready,
        )
        self._remember(current_input, assessment)
        return assessment

    def _health_reason(
        self,
        current_input: _ClassifierInput,
    ) -> RelativeMotionReason | None:
        trend = current_input.distance_trend
        motion = current_input.own_motion_evidence
        if trend.reason is DistanceTrendReason.LARGE_TIME_GAP:
            return RelativeMotionReason.TIMESTAMP_DISCONTINUITY
        if not trend.is_valid or not trend.is_ready:
            return RelativeMotionReason.DISTANCE_TREND_NOT_READY
        if motion.reason is OwnMotionReason.LARGE_TIME_GAP:
            return RelativeMotionReason.TIMESTAMP_DISCONTINUITY
        if not motion.is_valid or not motion.is_ready:
            return RelativeMotionReason.OWN_MOTION_NOT_READY
        if not current_input.sync_healthy:
            return RelativeMotionReason.SYNC_UNHEALTHY
        if (
            current_input.motion_age_seconds is None
            or current_input.motion_age_seconds
            > self.thresholds.maximum_motion_age_seconds
        ):
            return RelativeMotionReason.MOTION_STALE
        return None

    def _classify_healthy(
        self,
        timestamp: float,
        current_input: _ClassifierInput,
    ) -> _Decision:
        trend = current_input.distance_trend
        motion = current_input.own_motion_evidence
        slope = trend.range_slope_mps
        closing_rate = trend.closing_rate_mps
        assert slope is not None and closing_rate is not None

        has_translation = motion.translational_motion_detected
        has_rotation = motion.rotational_motion_detected
        has_own_motion = has_translation or has_rotation
        if has_own_motion:
            self._invalidate_stationary_baseline()

        if abs(slope) <= self.thresholds.stable_range_rate_deadband_mps:
            if not has_own_motion:
                self._observe_stationary_baseline(timestamp)
            return _Decision(
                RelativeMotionClass.STABLE_OR_RECEDING,
                RelativeMotionReason.STABLE_OR_RECEDING_RANGE,
            )

        self._stationary_candidate_since = None
        if slope > self.thresholds.stable_range_rate_deadband_mps:
            return _Decision(
                RelativeMotionClass.STABLE_OR_RECEDING,
                RelativeMotionReason.STABLE_OR_RECEDING_RANGE,
            )

        if closing_rate < self.thresholds.minimum_closing_rate_mps:
            return _Decision(
                RelativeMotionClass.UNCERTAIN,
                RelativeMotionReason.AMBIGUOUS_CLOSING,
            )

        if has_translation and has_rotation:
            return _Decision(
                RelativeMotionClass.STATIC_OR_SELF_APPROACH,
                RelativeMotionReason.SELF_TRANSLATIONAL_AND_ROTATIONAL_EVIDENCE,
            )
        if has_translation:
            return _Decision(
                RelativeMotionClass.STATIC_OR_SELF_APPROACH,
                RelativeMotionReason.SELF_TRANSLATIONAL_EVIDENCE,
            )
        if has_rotation:
            return _Decision(
                RelativeMotionClass.STATIC_OR_SELF_APPROACH,
                RelativeMotionReason.SELF_ROTATIONAL_EVIDENCE,
            )
        if self._stationary_baseline_ready(timestamp):
            return _Decision(
                RelativeMotionClass.TARGET_APPROACH,
                RelativeMotionReason.TARGET_APPROACH_EVIDENCE,
            )
        return _Decision(
            RelativeMotionClass.UNCERTAIN,
            RelativeMotionReason.STATIONARY_BASELINE_NOT_ESTABLISHED,
        )

    def _observe_stationary_baseline(self, timestamp: float) -> None:
        if self._stationary_candidate_since is None:
            self._stationary_candidate_since = timestamp
        elapsed = timestamp - self._stationary_candidate_since
        if _duration_reached(
            elapsed,
            self.thresholds.stationary_baseline_duration_seconds,
        ):
            self._stationary_baseline_last_confirmed = timestamp

    def _stationary_baseline_ready(self, timestamp: float) -> bool:
        last_confirmed = self._stationary_baseline_last_confirmed
        if last_confirmed is None:
            return False
        if (
            timestamp - last_confirmed
            > self.thresholds.stationary_baseline_max_age_seconds
        ):
            self._stationary_baseline_last_confirmed = None
            return False
        return True

    def _invalidate_stationary_baseline(self) -> None:
        self._stationary_candidate_since = None
        self._stationary_baseline_last_confirmed = None

    def _advance_candidate(
        self,
        timestamp: float,
        current_input: _ClassifierInput,
        decision: _Decision,
        baseline_ready: bool,
    ) -> RelativeMotionAssessment:
        if decision.classification is self._committed_classification:
            self._clear_candidate()
            return self._assessment(
                current_input,
                baseline_ready=baseline_ready,
                reason=decision.reason,
                is_ready=True,
            )

        if decision.classification is not self._candidate_classification:
            self._candidate_classification = decision.classification
            self._candidate_since = timestamp
        assert self._candidate_since is not None
        candidate_duration = timestamp - self._candidate_since
        required = self._persistence_for(decision.classification)
        if _duration_reached(candidate_duration, required):
            self._committed_classification = decision.classification
            self._clear_candidate()
            return self._assessment(
                current_input,
                baseline_ready=baseline_ready,
                reason=decision.reason,
                is_ready=True,
            )
        return self._assessment(
            current_input,
            baseline_ready=baseline_ready,
            reason=RelativeMotionReason.CANDIDATE_PERSISTENCE_NOT_SATISFIED,
            is_ready=False,
            candidate_classification=decision.classification,
            candidate_duration_seconds=candidate_duration,
        )

    def _persistence_for(self, classification: RelativeMotionClass) -> float:
        if classification is RelativeMotionClass.STABLE_OR_RECEDING:
            return self.thresholds.stable_persistence_seconds
        if classification is RelativeMotionClass.STATIC_OR_SELF_APPROACH:
            return self.thresholds.self_approach_persistence_seconds
        if classification is RelativeMotionClass.TARGET_APPROACH:
            return self.thresholds.target_approach_persistence_seconds
        return self.thresholds.uncertain_persistence_seconds

    def _invalid_assessment(
        self,
        current_input: _ClassifierInput,
        reason: RelativeMotionReason,
    ) -> RelativeMotionAssessment:
        return RelativeMotionAssessment(
            classification=RelativeMotionClass.UNCERTAIN,
            candidate_classification=None,
            timestamp_seconds=current_input.timestamp_seconds,
            distance_trend=current_input.distance_trend,
            own_motion_evidence=current_input.own_motion_evidence,
            stationary_baseline_ready=False,
            candidate_duration_seconds=0.0,
            is_valid=False,
            is_ready=False,
            reason=reason,
        )

    def _assessment(
        self,
        current_input: _ClassifierInput,
        *,
        baseline_ready: bool,
        reason: RelativeMotionReason,
        is_ready: bool,
        candidate_classification: RelativeMotionClass | None = None,
        candidate_duration_seconds: float = 0.0,
    ) -> RelativeMotionAssessment:
        return RelativeMotionAssessment(
            classification=self._committed_classification,
            candidate_classification=candidate_classification,
            timestamp_seconds=current_input.timestamp_seconds,
            distance_trend=current_input.distance_trend,
            own_motion_evidence=current_input.own_motion_evidence,
            stationary_baseline_ready=baseline_ready,
            candidate_duration_seconds=candidate_duration_seconds,
            is_valid=True,
            is_ready=is_ready,
            reason=reason,
        )

    def _clear_candidate(self) -> None:
        self._candidate_classification = None
        self._candidate_since = None

    def _reset_temporal_state(self) -> None:
        self._committed_classification = RelativeMotionClass.UNCERTAIN
        self._clear_candidate()
        self._invalidate_stationary_baseline()

    def _remember(
        self,
        current_input: _ClassifierInput,
        assessment: RelativeMotionAssessment,
    ) -> None:
        self._last_timestamp = current_input.timestamp_seconds
        self._last_input = current_input
        self._current_assessment = assessment
