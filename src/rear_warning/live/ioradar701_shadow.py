"""Hardware-free core shared by ioRadar701+BNO055 shadow replay and live."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from rear_warning.fusion import (
    RadarFusionInput,
    RadarRelativeMotionAdapter,
    RelativeMotionAssessment,
    VehicleMotionEstimator,
    VehicleMotionSnapshot,
)
from rear_warning.sensors.bno055.models import BNO055Measurement
from rear_warning.sensors.ioradar701 import (
    IoRadar701StreamParser,
    RadarCycle,
    RadarFrame,
    RadarSelection,
    RadarStateTracker,
    RadarTargetSelector,
    SelectionAvailability,
)
from rear_warning.synchronization import (
    InvalidMotionMeasurement,
    RadarMotionMatch,
    RadarMotionSynchronizer,
    SensorSyncStatus,
    TimedMotionMeasurement,
)
from rear_warning.warning import (
    HysteresisThresholds,
    WarningPolicy,
    WarningState,
    WarningStateStabilizer,
)


class IntegratedRadarAvailability(str, Enum):
    LIVE_SELECTED = "live_selected"
    NO_TARGETS = "no_targets"
    INCOMPLETE_CYCLE = "incomplete_cycle"
    RADAR_STALE = "radar_stale"
    RADAR_ERROR = "radar_error"
    RADAR_TIME_DISCONTINUITY = "radar_time_discontinuity"
    FUSION_UPDATE_COALESCED = "fusion_update_coalesced"
    BNO_NOT_READY = "bno_not_ready"
    SYNC_UNMATCHED = "sync_unmatched"
    FUSION_WARMUP = "fusion_warmup"


@dataclass(frozen=True, slots=True)
class ShadowAssessment:
    timestamp_seconds: float
    availability: IntegratedRadarAvailability
    reason: str
    selection: RadarSelection | None
    motion_match: RadarMotionMatch | None
    fusion: RelativeMotionAssessment | None
    warning_candidate: WarningState | None
    warning_stabilized: WarningState | None
    reset_reason: str | None
    receipt_batch_sequence: int | None = None
    fusion_update: str = "not_applicable"
    coalesced_into_cycle_sequence: int | None = None
    time_relation: str | None = None
    shadow: bool = True
    gpio_enabled: bool = False
    vehicle_motion: VehicleMotionSnapshot | None = None

    def as_dict(self) -> dict[str, object]:
        selection = self.selection
        target = None if selection is None else selection.selected_target
        match = self.motion_match
        fusion = self.fusion
        return {
            "schema_version": 2,
            "timestamp_seconds": self.timestamp_seconds,
            "availability": self.availability.value,
            "reason": self.reason,
            "shadow": self.shadow,
            "gpio_enabled": self.gpio_enabled,
            "reset_reason": self.reset_reason,
            "receipt_batch_sequence": self.receipt_batch_sequence,
            "fusion_update": self.fusion_update,
            "coalesced_into_cycle_sequence": self.coalesced_into_cycle_sequence,
            "time_relation": self.time_relation,
            "selection": None if selection is None else {
                "cycle_sequence": selection.cycle_sequence,
                "availability": selection.availability.value,
                "reason": selection.reason,
                "target_changed": selection.target_changed,
                "previous_target_id": selection.previous_target_id,
                "candidate_ids": [item.target_id for item in selection.candidates],
                "candidates": [item.as_dict() for item in selection.candidates],
                "selected_target": None if target is None else target.as_dict(),
            },
            "sync": None if match is None else {
                "status": match.status.value,
                "range_timestamp": match.range_timestamp,
                "motion_timestamp": match.motion_timestamp,
                "motion_age_seconds": match.motion_age_seconds,
            },
            "fusion": None if fusion is None else {
                "classification": fusion.classification.value,
                "candidate_classification": None if fusion.candidate_classification is None
                else fusion.candidate_classification.value,
                "is_valid": fusion.is_valid,
                "is_ready": fusion.is_ready,
                "reason": fusion.reason.value,
                "stationary_baseline_ready": fusion.stationary_baseline_ready,
                "candidate_duration_seconds": fusion.candidate_duration_seconds,
                "distance_trend": {
                    "sample_count": fusion.distance_trend.sample_count,
                    "inlier_count": fusion.distance_trend.inlier_count,
                    "window_span_seconds": fusion.distance_trend.window_span_seconds,
                    "range_slope_mps": fusion.distance_trend.range_slope_mps,
                    "closing_rate_mps": fusion.distance_trend.closing_rate_mps,
                    "is_valid": fusion.distance_trend.is_valid,
                    "is_ready": fusion.distance_trend.is_ready,
                    "reason": None if fusion.distance_trend.reason is None
                    else fusion.distance_trend.reason.value,
                },
                "own_motion": {
                    "unique_sample_count": fusion.own_motion_evidence.unique_sample_count,
                    "is_valid": fusion.own_motion_evidence.is_valid,
                    "is_ready": fusion.own_motion_evidence.is_ready,
                    "reason": None if fusion.own_motion_evidence.reason is None
                    else fusion.own_motion_evidence.reason.value,
                    "translational_motion_detected":
                    fusion.own_motion_evidence.translational_motion_detected,
                    "rotational_motion_detected":
                    fusion.own_motion_evidence.rotational_motion_detected,
                },
            },
            "distance_warning_candidate": None if self.warning_candidate is None
            else self.warning_candidate.value,
            "distance_warning_stabilized": None if self.warning_stabilized is None
            else self.warning_stabilized.value,
            "vehicle_motion": None if self.vehicle_motion is None
            else self.vehicle_motion.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ShadowBatch:
    frames: tuple[RadarFrame, ...]
    assessments: tuple[ShadowAssessment, ...]


class IoRadar701ShadowCore:
    """Single-state-machine shadow inference with no external I/O."""

    def __init__(self, *, stale_after_seconds: float = 1.0,
                 maximum_motion_age_seconds: float = 0.2) -> None:
        self.stale_after_seconds = stale_after_seconds
        self.parser = IoRadar701StreamParser()
        self.tracker = RadarStateTracker()
        self.selector = RadarTargetSelector()
        self.synchronizer = RadarMotionSynchronizer(
            maximum_motion_age_seconds=maximum_motion_age_seconds
        )
        self.adapter = RadarRelativeMotionAdapter()
        self.vehicle_motion = VehicleMotionEstimator()
        self.policy = WarningPolicy()
        self._stabilizer = self._new_stabilizer()
        self.latest: ShadowAssessment | None = None
        self._invalidated_key: str | None = None
        self._last_fusion_timestamp: float | None = None
        self._last_closed_cycle_timestamp: float | None = None
        self._last_receipt_batch_sequence = 0

    def update_motion(
        self,
        measurement: BNO055Measurement,
        *,
        captured_at_monotonic_seconds: float | None = None,
    ) -> bool:
        """Commit a quality-valid motion sample using the caller's canonical time.

        Live acquisition passes the post-read host-monotonic timestamp used by
        the existing synchronization layer. The model's read-start timestamp
        remains preserved separately in the measurement payload.
        """
        canonical = (
            measurement.monotonic_timestamp
            if captured_at_monotonic_seconds is None
            else captured_at_monotonic_seconds
        )
        timed = TimedMotionMeasurement(measurement, canonical)
        try:
            accepted = self.synchronizer.update_motion(timed)
        except InvalidMotionMeasurement:
            self.synchronizer.note_invalid_motion(timed)
            self.adapter.reset_all()
            self.vehicle_motion.note_invalid(canonical)
            raise
        if accepted:
            self.vehicle_motion.update(canonical, measurement.linear_acceleration)
        return accepted

    def feed_bytes(
        self,
        data: bytes,
        received_at: float,
        *,
        receipt_batch_sequence: int | None = None,
    ) -> ShadowBatch:
        batch_sequence = self._accept_batch_sequence(receipt_batch_sequence)
        self.tracker.note_bytes(received_at)
        frames = tuple(self.parser.feed(data))
        cycles: list[RadarCycle] = []
        for frame in frames:
            self.tracker.observe(frame, received_at)
            cycles.extend(self.tracker.drain_cycle_events())
        return ShadowBatch(
            frames,
            self._process_receipt_batch(tuple(cycles), batch_sequence),
        )

    def _accept_batch_sequence(self, supplied: int | None) -> int:
        sequence = self._last_receipt_batch_sequence + 1 if supplied is None else supplied
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise TypeError("receipt_batch_sequence must be an integer")
        if sequence <= self._last_receipt_batch_sequence:
            raise ValueError("receipt_batch_sequence must be strictly increasing")
        self._last_receipt_batch_sequence = sequence
        return sequence

    def tick(self, now: float) -> ShadowAssessment | None:
        snapshot = self.tracker.snapshot(now, stale_after=self.stale_after_seconds)
        if snapshot.state in ("bytes_stale", "status_stale", "targets_stale"):
            key = f"{IntegratedRadarAvailability.RADAR_STALE.value}:radar freshness expired: {snapshot.state}"
            if key == self._invalidated_key:
                return None
            return self._invalidate(
                now,
                IntegratedRadarAvailability.RADAR_STALE,
                f"radar freshness expired: {snapshot.state}",
            )
        if snapshot.state == "serial_error":
            detail = snapshot.error_detail or "radar serial error"
            if f"{IntegratedRadarAvailability.RADAR_ERROR.value}:{detail}" == self._invalidated_key:
                return None
            return self._invalidate(
                now,
                IntegratedRadarAvailability.RADAR_ERROR,
                detail,
            )
        return None

    def set_radar_error(self, detail: str, now: float) -> ShadowAssessment:
        self.tracker.set_serial_error(detail)
        return self._invalidate(
            now, IntegratedRadarAvailability.RADAR_ERROR, detail
        )

    def _process_receipt_batch(
        self,
        cycles: tuple[RadarCycle, ...],
        batch_sequence: int,
    ) -> tuple[ShadowAssessment, ...]:
        results: list[ShadowAssessment] = []
        final_index = len(cycles) - 1
        for index, cycle in enumerate(cycles):
            selection = self.selector.select(cycle)
            timestamp = cycle.completed_at_monotonic_seconds
            if selection.availability is SelectionAvailability.NO_TARGETS:
                results.append(self._invalidate(
                    timestamp,
                    IntegratedRadarAvailability.NO_TARGETS,
                    selection.reason,
                    selection=selection,
                    receipt_batch_sequence=batch_sequence,
                ))
                continue
            if selection.availability is SelectionAvailability.INCOMPLETE_CYCLE:
                results.append(self._invalidate(
                    timestamp,
                    IntegratedRadarAvailability.INCOMPLETE_CYCLE,
                    selection.reason,
                    selection=selection,
                    receipt_batch_sequence=batch_sequence,
                ))
                continue
            if index != final_index:
                results.append(self._coalesce_selected(
                    selection,
                    timestamp,
                    batch_sequence=batch_sequence,
                    final_cycle_sequence=cycles[-1].sequence,
                ))
                continue
            results.append(self._process_selected(
                selection,
                timestamp,
                batch_sequence=batch_sequence,
            ))
        if cycles:
            final_timestamp = cycles[-1].completed_at_monotonic_seconds
            self._last_closed_cycle_timestamp = (
                final_timestamp
                if self._last_closed_cycle_timestamp is None
                else max(self._last_closed_cycle_timestamp, final_timestamp)
            )
        return tuple(results)

    def _process_selected(
        self,
        selection: RadarSelection,
        timestamp: float,
        *,
        batch_sequence: int,
    ) -> ShadowAssessment:
        target = selection.selected_target
        assert target is not None
        if (
            self._last_closed_cycle_timestamp is not None
            and timestamp <= self._last_closed_cycle_timestamp
        ):
            relation = (
                "equal_across_receipt_batches"
                if timestamp == self._last_closed_cycle_timestamp
                else "backwards"
            )
            return self._reject_time(
                selection,
                timestamp,
                batch_sequence=batch_sequence,
                relation=relation,
            )
        reset_reason = self._apply_target_change(selection)
        match = self.synchronizer.associate(timestamp)
        fusion = self.adapter.update(RadarFusionInput(target, timestamp, match))
        warning_candidate = self.policy.classify_distance_cm(target.distance_m * 100.0)
        warning_stabilized = self._stabilizer.stabilize(
            warning_candidate, target.distance_m * 100.0
        )
        if match.status is SensorSyncStatus.NO_MOTION_SAMPLE:
            availability = IntegratedRadarAvailability.BNO_NOT_READY
            reason = "no causal BNO055 sample; absence of motion is not proof of stillness"
        elif match.status is not SensorSyncStatus.MATCHED:
            availability = IntegratedRadarAvailability.SYNC_UNMATCHED
            reason = f"causal motion match unavailable: {match.status.value}"
        elif not fusion.is_ready:
            availability = IntegratedRadarAvailability.FUSION_WARMUP
            reason = fusion.reason.value
        else:
            availability = IntegratedRadarAvailability.LIVE_SELECTED
            reason = fusion.reason.value
        result = ShadowAssessment(
            timestamp,
            availability,
            reason,
            selection,
            match,
            fusion,
            warning_candidate,
            warning_stabilized,
            reset_reason,
            receipt_batch_sequence=batch_sequence,
            fusion_update="applied",
            time_relation=("initial" if self._last_fusion_timestamp is None else "increasing"),
            vehicle_motion=self.vehicle_motion.snapshot(timestamp),
        )
        self.latest = result
        self._last_fusion_timestamp = timestamp
        self._invalidated_key = None
        return result

    def _coalesce_selected(
        self,
        selection: RadarSelection,
        timestamp: float,
        *,
        batch_sequence: int,
        final_cycle_sequence: int,
    ) -> ShadowAssessment:
        target = selection.selected_target
        assert target is not None
        reset_reason = self._apply_target_change(selection)
        result = ShadowAssessment(
            timestamp,
            IntegratedRadarAvailability.FUSION_UPDATE_COALESCED,
            "same receipt batch has a later closed cycle; distance/fusion update coalesced",
            selection,
            None,
            None,
            self.policy.classify_distance_cm(target.distance_m * 100.0),
            None,
            reset_reason,
            receipt_batch_sequence=batch_sequence,
            fusion_update="coalesced",
            coalesced_into_cycle_sequence=final_cycle_sequence,
            time_relation="same_receipt_batch_coalesced",
            vehicle_motion=self.vehicle_motion.snapshot(timestamp),
        )
        self.latest = result
        self._invalidated_key = None
        return result

    def _apply_target_change(self, selection: RadarSelection) -> str | None:
        if not selection.target_changed:
            return None
        target = selection.selected_target
        assert target is not None
        reason = (
            "initial target acquisition" if selection.previous_target_id is None
            else f"target switch {selection.previous_target_id}->{target.target_id}"
        )
        self.adapter.reset_target()
        self._stabilizer = self._new_stabilizer()
        return reason

    def _reject_time(
        self,
        selection: RadarSelection,
        timestamp: float,
        *,
        batch_sequence: int,
        relation: str,
    ) -> ShadowAssessment:
        target = selection.selected_target
        assert target is not None
        reason = (
            "completed cycle timestamp equals the prior closed cycle in a "
            "different receipt batch"
            if relation == "equal_across_receipt_batches"
            else "completed cycle timestamp moved backwards"
        )
        warning_candidate = self.policy.classify_distance_cm(target.distance_m * 100.0)
        self.selector.reset()
        self.adapter.reset_target()
        self._stabilizer = self._new_stabilizer()
        warning_stabilized = self._stabilizer.stabilize(
            warning_candidate, target.distance_m * 100.0
        )
        result = ShadowAssessment(
            timestamp,
            IntegratedRadarAvailability.RADAR_TIME_DISCONTINUITY,
            reason,
            selection,
            None,
            None,
            warning_candidate,
            warning_stabilized,
            reason,
            receipt_batch_sequence=batch_sequence,
            fusion_update="rejected_time",
            time_relation=relation,
            vehicle_motion=self.vehicle_motion.snapshot(timestamp),
        )
        self.latest = result
        self._invalidated_key = None
        return result

    def _invalidate(
        self,
        timestamp: float,
        availability: IntegratedRadarAvailability,
        reason: str,
        *,
        selection: RadarSelection | None = None,
        receipt_batch_sequence: int | None = None,
    ) -> ShadowAssessment:
        key = f"{availability.value}:{reason}"
        self.selector.reset()
        self.adapter.reset_target()
        self._stabilizer = self._new_stabilizer()
        result = ShadowAssessment(
            timestamp,
            availability,
            reason,
            selection,
            None,
            None,
            None,
            None,
            reason,
            receipt_batch_sequence=receipt_batch_sequence,
            vehicle_motion=self.vehicle_motion.snapshot(timestamp),
        )
        self.latest = result
        self._invalidated_key = key
        return result

    def _new_stabilizer(self) -> WarningStateStabilizer:
        return WarningStateStabilizer(
            self.policy.thresholds,
            HysteresisThresholds(),
            initial_state=WarningState.SENSOR_FAULT,
        )
