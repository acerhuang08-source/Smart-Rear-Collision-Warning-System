"""Distance-warning to three-colour LED integration without sensor I/O."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from rear_warning.live.ioradar701_shadow import (
    IntegratedRadarAvailability,
    ShadowAssessment,
)
from rear_warning.outputs import LedPins, ThreeColorLedDriver
from rear_warning.warning import WarningState


class ClosableDigitalOutput(Protocol):
    def write(self, pin: int, active: bool) -> None: ...

    def close(self) -> None: ...


class LedRecordWriter(Protocol):
    def write_led(self, record: dict[str, object]) -> None: ...


class RadarLedAction(str, Enum):
    HOLD = "hold"
    ALL_OFF = "all_off"
    DISPLAY = "display"


@dataclass(frozen=True, slots=True)
class RadarLedDecision:
    action: RadarLedAction
    state: WarningState | None
    reason: str


_FAULT_AVAILABILITIES = frozenset(
    {
        IntegratedRadarAvailability.INCOMPLETE_CYCLE,
        IntegratedRadarAvailability.RADAR_STALE,
        IntegratedRadarAvailability.RADAR_ERROR,
        IntegratedRadarAvailability.RADAR_TIME_DISCONTINUITY,
    }
)


def decide_radar_led(assessment: ShadowAssessment) -> RadarLedDecision:
    """Map one completed core assessment to a physical-output intent."""
    if not isinstance(assessment, ShadowAssessment):
        raise TypeError("assessment must be a ShadowAssessment")
    if assessment.availability is IntegratedRadarAvailability.NO_TARGETS:
        return RadarLedDecision(
            RadarLedAction.ALL_OFF,
            None,
            "normal completed cycle declared zero targets",
        )
    if assessment.availability in _FAULT_AVAILABILITIES:
        return RadarLedDecision(
            RadarLedAction.DISPLAY,
            WarningState.SENSOR_FAULT,
            f"radar input invalid: {assessment.availability.value}",
        )
    if assessment.fusion_update == "coalesced":
        return RadarLedDecision(
            RadarLedAction.HOLD,
            None,
            "same UART receipt batch has a later completed cycle",
        )
    selection = assessment.selection
    if (
        selection is not None
        and selection.selected_target is not None
        and assessment.warning_stabilized is not None
        and assessment.fusion_update == "applied"
    ):
        return RadarLedDecision(
            RadarLedAction.DISPLAY,
            assessment.warning_stabilized,
            "fresh complete selected-target distance warning",
        )
    return RadarLedDecision(
        RadarLedAction.HOLD,
        None,
        "assessment has no new complete selected-target LED result",
    )


def _pattern(state: WarningState | None) -> tuple[bool, bool, bool]:
    if state is None:
        return False, False, False
    return {
        WarningState.SAFE: (False, False, True),
        WarningState.WARNING: (False, True, False),
        WarningState.DANGER: (True, False, False),
        WarningState.SENSOR_FAULT: (True, True, False),
    }[state]


class RadarLedObserver:
    """Own the GPIO adapter and record every LED decision alongside capture data."""

    def __init__(
        self,
        output: ClosableDigitalOutput,
        pins: LedPins,
    ) -> None:
        self._output = output
        self._driver = ThreeColorLedDriver(output, pins)
        self._current_pattern: tuple[bool, bool, bool] | None = None
        self._closed = False
        self._finish_attempted = False
        self._all_off_succeeded = False
        self._gpio_close_succeeded = False

    @property
    def current_pattern(self) -> tuple[bool, bool, bool] | None:
        return self._current_pattern

    @property
    def closed(self) -> bool:
        return self._closed

    def cleanup_status(self) -> dict[str, bool]:
        return {
            "finish_attempted": self._finish_attempted,
            "all_off_succeeded": self._all_off_succeeded,
            "gpio_close_succeeded": self._gpio_close_succeeded,
        }

    def start(self, capture: LedRecordWriter, timestamp: float) -> None:
        """Force all outputs off before any sensor is opened."""
        self._driver.all_off()
        self._current_pattern = _pattern(None)
        capture.write_led(
            {
                "timestamp_seconds": timestamp,
                "kind": "startup",
                "action": RadarLedAction.ALL_OFF.value,
                "reason": "integration startup; no selected target yet",
                "gpio_pattern": self._pattern_dict(self._current_pattern),
                "gpio_write_applied": True,
            }
        )

    def observe(
        self,
        assessment: ShadowAssessment,
        capture: LedRecordWriter,
    ) -> None:
        decision = decide_radar_led(assessment)
        target = (
            None
            if assessment.selection is None
            else assessment.selection.selected_target
        )
        write_applied = False
        if decision.action is RadarLedAction.ALL_OFF:
            next_pattern = _pattern(None)
            if next_pattern != self._current_pattern:
                self._driver.all_off()
                self._current_pattern = next_pattern
                write_applied = True
        elif decision.action is RadarLedAction.DISPLAY:
            assert decision.state is not None
            next_pattern = _pattern(decision.state)
            if next_pattern != self._current_pattern:
                self._driver.display(decision.state)
                self._current_pattern = next_pattern
                write_applied = True
        capture.write_led(
            {
                "timestamp_seconds": assessment.timestamp_seconds,
                "kind": "assessment",
                "availability": assessment.availability.value,
                "action": decision.action.value,
                "decision_reason": decision.reason,
                "selected_target_id": None if target is None else target.target_id,
                "distance_m": None if target is None else target.distance_m,
                "target_changed": (
                    None
                    if assessment.selection is None
                    else assessment.selection.target_changed
                ),
                "warning_candidate": (
                    None
                    if assessment.warning_candidate is None
                    else assessment.warning_candidate.value
                ),
                "warning_stabilized": (
                    None
                    if assessment.warning_stabilized is None
                    else assessment.warning_stabilized.value
                ),
                "gpio_pattern": self._pattern_dict(self._current_pattern),
                "gpio_write_applied": write_applied,
            }
        )

    def finish(self, capture: LedRecordWriter, timestamp: float) -> None:
        """Best-effort all-off, record it, and close the one GPIO owner."""
        if self._closed:
            return
        self._closed = True
        self._finish_attempted = True
        errors: list[str] = []
        try:
            self._driver.all_off()
            self._current_pattern = _pattern(None)
            self._all_off_succeeded = True
        except BaseException as exc:
            errors.append(f"all_off: {type(exc).__name__}: {exc}")
        try:
            capture.write_led(
                {
                    "timestamp_seconds": timestamp,
                    "kind": "cleanup",
                    "action": RadarLedAction.ALL_OFF.value,
                    "reason": "normal, interrupted, or error cleanup",
                    "gpio_pattern": self._pattern_dict(self._current_pattern),
                    "gpio_write_applied": not errors,
                }
            )
        except BaseException as exc:
            errors.append(f"record: {type(exc).__name__}: {exc}")
        try:
            self._output.close()
            self._gpio_close_succeeded = True
        except BaseException as exc:
            errors.append(f"close: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def abort_cleanup(self) -> None:
        """Release GPIO if capture allocation or sensor preflight never completed."""
        if self._closed:
            return
        self._closed = True
        self._finish_attempted = True
        errors: list[str] = []
        try:
            self._driver.all_off()
            self._current_pattern = _pattern(None)
            self._all_off_succeeded = True
        except BaseException as exc:
            errors.append(f"all_off: {type(exc).__name__}: {exc}")
        try:
            self._output.close()
            self._gpio_close_succeeded = True
        except BaseException as exc:
            errors.append(f"close: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    @staticmethod
    def _pattern_dict(
        pattern: tuple[bool, bool, bool] | None,
    ) -> dict[str, bool] | None:
        if pattern is None:
            return None
        return {
            "red_bcm17": pattern[0],
            "yellow_bcm27": pattern[1],
            "green_bcm22": pattern[2],
        }
