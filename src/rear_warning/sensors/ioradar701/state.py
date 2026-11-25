"""Target-cycle assembly and independent freshness tracking."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from .models import RadarFrame, StatusFrame, TargetFrame

SnapshotState = Literal[
    "waiting_bytes",
    "bytes_stale",
    "waiting_status",
    "status_stale",
    "no_targets",
    "waiting_targets",
    "targets_stale",
    "live",
    "serial_error",
]

CycleClosureKind = Literal["complete", "no_targets", "incomplete"]


@dataclass(frozen=True, slots=True)
class RadarCycle:
    """One status-delimited cycle emitted exactly once when it closes."""

    sequence: int
    kind: CycleClosureKind
    status: StatusFrame
    targets: tuple[TargetFrame, ...]
    completed_at_monotonic_seconds: float

    @property
    def is_complete(self) -> bool:
        return self.kind in ("complete", "no_targets")


@dataclass(frozen=True, slots=True)
class RadarSnapshot:
    """One UI-facing state snapshot without fabricated zero measurements."""

    state: SnapshotState
    status: StatusFrame | None
    targets: tuple[TargetFrame, ...]
    expected_targets: int | None
    cycle_complete: bool
    bytes_age_seconds: float | None
    status_age_seconds: float | None
    target_age_seconds: float | None
    error_detail: str | None


class RadarStateTracker:
    """Build target snapshots from status-delimited radar cycles."""

    def __init__(self) -> None:
        self.last_bytes_at: float | None = None
        self.last_status_at: float | None = None
        self.last_target_at: float | None = None
        self.status: StatusFrame | None = None
        self._targets: dict[int, TargetFrame] = {}
        self.incomplete_cycles = 0
        self.duplicate_target_frames = 0
        self.extra_target_frames = 0
        self.orphan_target_frames = 0
        self.serial_error: str | None = None
        self._cycle_sequence = 0
        self._current_cycle_sequence: int | None = None
        self._cycle_events: list[RadarCycle] = []
        self._current_cycle_emitted = False

    def note_bytes(self, received_at: float) -> None:
        """Record receipt of any bytes, including malformed data."""
        self._validate_time(received_at)
        self.last_bytes_at = received_at

    def observe(self, frame: RadarFrame, received_at: float) -> None:
        """Apply one valid frame to the current status-delimited cycle."""
        self._validate_time(received_at)
        if isinstance(frame, StatusFrame):
            if self.status is not None and not self._cycle_complete():
                self.incomplete_cycles += 1
                self._emit_cycle("incomplete", received_at)
            self.status = frame
            self.last_status_at = received_at
            self._targets.clear()
            self._cycle_sequence += 1
            self._current_cycle_sequence = self._cycle_sequence
            self._current_cycle_emitted = False
            if frame.target_count == 0:
                self._emit_cycle("no_targets", received_at)
            return

        self.last_target_at = received_at
        if self.status is None or self.status.target_count == 0:
            self.orphan_target_frames += 1
            return
        if frame.target_id in self._targets:
            self.duplicate_target_frames += 1
            self._targets[frame.target_id] = frame
            return
        if len(self._targets) >= self.status.target_count:
            self.extra_target_frames += 1
            return
        self._targets[frame.target_id] = frame
        if self._cycle_complete():
            self._emit_cycle("complete", received_at)

    def drain_cycle_events(self) -> tuple[RadarCycle, ...]:
        """Return pending closed-cycle events and consume them."""
        events = tuple(self._cycle_events)
        self._cycle_events.clear()
        return events

    def set_serial_error(self, detail: str) -> None:
        """Record a non-empty UART error separately from data staleness."""
        if not detail.strip():
            raise ValueError("serial error detail must not be empty")
        self.serial_error = detail

    @property
    def current_cycle_incomplete(self) -> bool:
        """Return whether the latest status still awaits declared targets."""
        return self.status is not None and not self._cycle_complete()

    def snapshot(self, now: float, *, stale_after: float) -> RadarSnapshot:
        """Classify current freshness without carrying old-cycle targets."""
        self._validate_time(now)
        if not math.isfinite(stale_after) or stale_after <= 0:
            raise ValueError("stale_after must be finite and greater than zero")
        bytes_age = self._age(now, self.last_bytes_at)
        status_age = self._age(now, self.last_status_at)
        target_age = self._age(now, self.last_target_at)
        expected = None if self.status is None else self.status.target_count
        targets = tuple(self._targets[key] for key in sorted(self._targets))
        complete = self._cycle_complete()

        if self.serial_error is not None:
            state: SnapshotState = "serial_error"
        elif bytes_age is None:
            state = "waiting_bytes"
        elif bytes_age > stale_after:
            state = "bytes_stale"
        elif self.status is None:
            state = "waiting_status"
        elif status_age is not None and status_age > stale_after:
            state = "status_stale"
        elif expected == 0:
            state = "no_targets"
        elif not complete:
            state = "waiting_targets"
        elif target_age is None or target_age > stale_after:
            state = "targets_stale"
        else:
            state = "live"
        return RadarSnapshot(
            state=state,
            status=self.status,
            targets=targets,
            expected_targets=expected,
            cycle_complete=complete,
            bytes_age_seconds=bytes_age,
            status_age_seconds=status_age,
            target_age_seconds=target_age,
            error_detail=self.serial_error,
        )

    def _cycle_complete(self) -> bool:
        if self.status is None:
            return False
        return len(self._targets) == self.status.target_count

    def _emit_cycle(self, kind: CycleClosureKind, completed_at: float) -> None:
        if self.status is None or self._current_cycle_sequence is None:
            return
        if self._current_cycle_emitted:
            return
        self._cycle_events.append(
            RadarCycle(
                sequence=self._current_cycle_sequence,
                kind=kind,
                status=self.status,
                targets=tuple(self._targets[key] for key in sorted(self._targets)),
                completed_at_monotonic_seconds=completed_at,
            )
        )
        self._current_cycle_emitted = True

    @staticmethod
    def _validate_time(value: float) -> None:
        if not math.isfinite(value):
            raise ValueError("timestamp must be finite")

    @staticmethod
    def _age(now: float, then: float | None) -> float | None:
        return None if then is None else max(0.0, now - then)
