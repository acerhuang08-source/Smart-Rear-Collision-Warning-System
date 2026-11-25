"""Deterministic, I/O-free target selection for completed radar cycles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .models import TargetFrame
from .state import RadarCycle


class SelectionAvailability(str, Enum):
    SELECTED = "selected"
    NO_TARGETS = "no_targets"
    INCOMPLETE_CYCLE = "incomplete_cycle"


@dataclass(frozen=True, slots=True)
class RadarSelection:
    cycle_sequence: int
    availability: SelectionAvailability
    selected_target: TargetFrame | None
    candidates: tuple[TargetFrame, ...]
    reason: str
    target_changed: bool
    previous_target_id: int | None

    @property
    def target_id(self) -> int | None:
        return None if self.selected_target is None else self.selected_target.target_id


class RadarTargetSelector:
    """Choose the minimum hypot distance, using target ID as the tie-break.

    This is an intentionally simple development-time policy. It is neither a
    threat ranking nor proof of persistent physical identity.
    """

    RULE = "minimum-hypot-then-id"

    def __init__(self) -> None:
        self._selected_target_id: int | None = None

    @property
    def selected_target_id(self) -> int | None:
        return self._selected_target_id

    def reset(self) -> int | None:
        previous = self._selected_target_id
        self._selected_target_id = None
        return previous

    def select(self, cycle: RadarCycle) -> RadarSelection:
        if not isinstance(cycle, RadarCycle):
            raise TypeError("cycle must be a RadarCycle")
        previous = self._selected_target_id
        candidates = tuple(sorted(cycle.targets, key=lambda item: item.target_id))
        if cycle.kind == "incomplete":
            self._selected_target_id = None
            return RadarSelection(
                cycle.sequence,
                SelectionAvailability.INCOMPLETE_CYCLE,
                None,
                candidates,
                "cycle closed before all declared target IDs arrived",
                previous is not None,
                previous,
            )
        if cycle.kind == "no_targets":
            self._selected_target_id = None
            return RadarSelection(
                cycle.sequence,
                SelectionAvailability.NO_TARGETS,
                None,
                (),
                "status declared zero targets",
                previous is not None,
                previous,
            )
        selected = min(candidates, key=lambda item: (item.distance_m, item.target_id))
        candidate_ids = {target.target_id for target in candidates}
        if previous is None:
            reason = (
                f"selected minimum hypot distance; tie-break target ID "
                f"{selected.target_id}"
            )
        elif previous == selected.target_id:
            reason = (
                f"retained target ID {selected.target_id}; it remains minimum "
                "hypot distance"
            )
        elif previous in candidate_ids:
            reason = (
                f"nearer target ID {selected.target_id} replaced previous "
                f"target ID {previous}"
            )
        else:
            reason = (
                f"target ID {previous} disappeared; selected minimum hypot "
                f"distance target ID {selected.target_id}"
            )
        self._selected_target_id = selected.target_id
        return RadarSelection(
            cycle.sequence,
            SelectionAvailability.SELECTED,
            selected,
            candidates,
            reason,
            previous != selected.target_id,
            previous,
        )
