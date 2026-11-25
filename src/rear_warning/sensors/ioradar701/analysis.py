"""Incremental protocol metrics shared by offline and live workflows."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import RadarFrame, StatusFrame, TargetFrame
from .parser import ParserStatistics
from .state import RadarStateTracker


@dataclass(slots=True)
class _Range:
    minimum: float
    maximum: float

    def include(self, value: float) -> None:
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)

    def as_dict(self) -> dict[str, float]:
        return {"min": self.minimum, "max": self.maximum}


@dataclass(slots=True)
class _TargetMetrics:
    samples: int = 0
    x_raw: _Range | None = None
    y_raw: _Range | None = None
    vx_raw: _Range | None = None
    x_m: _Range | None = None
    y_m: _Range | None = None
    vx_mps: _Range | None = None
    distance_m: _Range | None = None
    energy: _Range | None = None

    def observe(self, frame: TargetFrame) -> None:
        self.samples += 1
        for name, value in (
            ("x_raw", frame.x_raw),
            ("y_raw", frame.y_raw),
            ("vx_raw", frame.vx_raw),
            ("x_m", frame.x_m),
            ("y_m", frame.y_m),
            ("vx_mps", frame.vx_mps),
            ("distance_m", frame.distance_m),
            ("energy", frame.energy),
        ):
            current = getattr(self, name)
            if current is None:
                setattr(self, name, _Range(float(value), float(value)))
            else:
                current.include(float(value))

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"samples": self.samples}
        for name in (
            "x_raw", "y_raw", "vx_raw", "x_m", "y_m", "vx_mps",
            "distance_m", "energy",
        ):
            value = getattr(self, name)
            result[name] = None if value is None else value.as_dict()
        return result


@dataclass(slots=True)
class RadarAnalysis:
    """Accumulate decoded ranges, status values, and receipt-gap evidence."""

    tracker: RadarStateTracker = field(default_factory=RadarStateTracker)
    status_values: dict[str, set[int]] = field(
        default_factory=lambda: {
            "check": set(),
            "active": set(),
            "error": set(),
            "target_count": set(),
            "roll_count": set(),
        }
    )
    targets: dict[int, _TargetMetrics] = field(default_factory=dict)
    last_valid_at: float | None = None
    max_valid_gap_seconds: float | None = None
    frame_sequence: int = 0

    def observe(self, frame: RadarFrame, received_at: float | None = None) -> None:
        """Add one decoded frame; timing is optional for offline byte files."""
        if isinstance(frame, StatusFrame):
            self.status_values["check"].add(frame.check)
            self.status_values["active"].add(frame.active)
            self.status_values["error"].add(frame.error)
            self.status_values["target_count"].add(frame.target_count)
            self.status_values["roll_count"].add(frame.roll_count)
        else:
            self.targets.setdefault(frame.target_id, _TargetMetrics()).observe(frame)
        self.frame_sequence += 1
        tracker_time = float(self.frame_sequence) if received_at is None else received_at
        self.tracker.observe(frame, tracker_time)
        if received_at is not None:
            if self.last_valid_at is not None:
                gap = max(0.0, received_at - self.last_valid_at)
                self.max_valid_gap_seconds = (
                    gap if self.max_valid_gap_seconds is None
                    else max(self.max_valid_gap_seconds, gap)
                )
            self.last_valid_at = received_at

    def as_dict(
        self,
        parser: ParserStatistics,
        *,
        elapsed_seconds: float | None = None,
    ) -> dict[str, object]:
        """Return a JSON-safe report with explicit rate denominators."""
        status_rate = None
        target_rate = None
        if elapsed_seconds is not None and elapsed_seconds > 0:
            status_rate = parser.status_frames / elapsed_seconds
            target_rate = parser.target_frames / elapsed_seconds
        return {
            "bytes": parser.input_bytes,
            "valid_frames": parser.valid_frames,
            "status_frames": parser.status_frames,
            "target_frames": parser.target_frames,
            "checksum_failures": parser.checksum_failures,
            "sync_discarded_bytes": parser.sync_discarded_bytes,
            "trailing_bytes": parser.trailing_bytes,
            "rate_denominator_seconds": elapsed_seconds,
            "status_frames_per_second": status_rate,
            "target_frames_per_second": target_rate,
            "max_valid_receipt_gap_seconds": self.max_valid_gap_seconds,
            "status_values": {
                key: sorted(values) for key, values in self.status_values.items()
            },
            "target_ids": {
                str(target_id): metrics.as_dict()
                for target_id, metrics in sorted(self.targets.items())
            },
            "cycle_quality": {
                "incomplete_cycles_before_next_status": self.tracker.incomplete_cycles,
                "final_cycle_incomplete": self.tracker.current_cycle_incomplete,
                "incomplete_cycles_total": (
                    self.tracker.incomplete_cycles
                    + int(self.tracker.current_cycle_incomplete)
                ),
                "duplicate_target_frames": self.tracker.duplicate_target_frames,
                "extra_target_frames": self.tracker.extra_target_frames,
                "orphan_target_frames": self.tracker.orphan_target_frames,
            },
        }
