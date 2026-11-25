"""Cycle and freshness tests for ioRadar701 live state."""

from __future__ import annotations

from rear_warning.sensors.ioradar701.models import StatusFrame, TargetFrame
from rear_warning.sensors.ioradar701.parser import IoRadar701StreamParser
from rear_warning.sensors.ioradar701.state import RadarStateTracker


def _frame(kind: int, *, identifier: int = 0, count: int = 0) -> bytes:
    body = bytearray(11)
    body[0] = 0xA5
    body[1] = kind
    if kind == 1:
        body[3] = 0x05
        body[4] = 0x08
        body[6] = count
    else:
        body[3] = identifier
        body[4:6] = (200 + identifier).to_bytes(2, "big")
        body[6:8] = (1200 + identifier).to_bytes(2, "big")
        body[8:10] = (6500 + identifier).to_bytes(2, "big")
        body[10] = 80 + identifier
    return bytes(body) + bytes((sum(body) & 0xFF,))


def _decode(payload: bytes) -> StatusFrame | TargetFrame:
    frames = IoRadar701StreamParser().feed(payload)
    assert len(frames) == 1
    return frames[0]


def test_multi_target_duplicate_zero_and_next_cycle_do_not_mix() -> None:
    tracker = RadarStateTracker()
    tracker.note_bytes(1.0)
    tracker.observe(_decode(_frame(1, count=2)), 1.0)
    tracker.observe(_decode(_frame(2, identifier=4)), 1.1)
    assert tracker.snapshot(1.1, stale_after=1).state == "waiting_targets"

    tracker.observe(_decode(_frame(2, identifier=4)), 1.2)
    tracker.observe(_decode(_frame(2, identifier=7)), 1.3)
    complete = tracker.snapshot(1.3, stale_after=1)
    assert complete.state == "live"
    assert [target.target_id for target in complete.targets] == [4, 7]
    assert tracker.duplicate_target_frames == 1

    tracker.observe(_decode(_frame(1, count=0)), 1.4)
    zero = tracker.snapshot(1.4, stale_after=1)
    assert zero.state == "no_targets"
    assert zero.targets == ()

    tracker.observe(_decode(_frame(2, identifier=9)), 1.5)
    assert tracker.orphan_target_frames == 1
    tracker.observe(_decode(_frame(1, count=1)), 1.6)
    waiting = tracker.snapshot(1.6, stale_after=1)
    assert waiting.state == "waiting_targets"
    assert waiting.targets == ()


def test_incomplete_cycle_is_counted_when_next_status_arrives() -> None:
    tracker = RadarStateTracker()
    tracker.note_bytes(0.0)
    tracker.observe(_decode(_frame(1, count=2)), 0.0)
    tracker.observe(_decode(_frame(2, identifier=1)), 0.1)
    assert tracker.current_cycle_incomplete is True
    tracker.observe(_decode(_frame(1, count=1)), 0.2)
    assert tracker.incomplete_cycles == 1
    assert tracker.current_cycle_incomplete is True
    assert tracker.snapshot(0.2, stale_after=1).targets == ()


def test_bytes_status_and_target_freshness_are_independent_and_recover() -> None:
    tracker = RadarStateTracker()
    assert tracker.snapshot(0.0, stale_after=1).state == "waiting_bytes"
    tracker.note_bytes(0.0)
    assert tracker.snapshot(0.0, stale_after=1).state == "waiting_status"
    tracker.observe(_decode(_frame(1, count=1)), 0.0)
    tracker.observe(_decode(_frame(2, identifier=1)), 0.1)
    assert tracker.snapshot(0.2, stale_after=1).state == "live"

    tracker.note_bytes(1.5)  # Bad bytes can be recent without refreshing status/target.
    assert tracker.snapshot(1.5, stale_after=1).state == "status_stale"
    assert tracker.snapshot(2.6, stale_after=1).state == "bytes_stale"

    tracker.note_bytes(2.7)
    tracker.observe(_decode(_frame(1, count=1)), 2.7)
    assert tracker.snapshot(2.7, stale_after=1).state == "waiting_targets"
    tracker.observe(_decode(_frame(2, identifier=2)), 2.8)
    recovered = tracker.snapshot(2.8, stale_after=1)
    assert recovered.state == "live"
    assert [target.target_id for target in recovered.targets] == [2]


def test_serial_error_is_distinct() -> None:
    tracker = RadarStateTracker()
    tracker.note_bytes(1.0)
    tracker.set_serial_error("disconnected")
    snapshot = tracker.snapshot(1.1, stale_after=1)
    assert snapshot.state == "serial_error"
    assert snapshot.error_detail == "disconnected"


def test_cycle_events_emit_once_and_incomplete_closes_on_next_status() -> None:
    tracker = RadarStateTracker()
    tracker.note_bytes(1.0)
    tracker.observe(_decode(_frame(1, count=2)), 1.0)
    assert tracker.drain_cycle_events() == ()
    tracker.observe(_decode(_frame(2, identifier=8)), 1.1)
    assert tracker.drain_cycle_events() == ()
    tracker.observe(_decode(_frame(1, count=0)), 1.2)
    events = tracker.drain_cycle_events()
    assert [(event.sequence, event.kind) for event in events] == [
        (1, "incomplete"),
        (2, "no_targets"),
    ]
    assert [target.target_id for target in events[0].targets] == [8]
    assert tracker.drain_cycle_events() == ()


def test_complete_cycle_event_is_not_reemitted_by_duplicate_or_snapshot() -> None:
    tracker = RadarStateTracker()
    tracker.note_bytes(2.0)
    tracker.observe(_decode(_frame(1, count=1)), 2.0)
    tracker.observe(_decode(_frame(2, identifier=3)), 2.1)
    event, = tracker.drain_cycle_events()
    assert event.kind == "complete"
    assert event.completed_at_monotonic_seconds == 2.1
    tracker.snapshot(2.2, stale_after=1.0)
    tracker.observe(_decode(_frame(2, identifier=3)), 2.3)
    assert tracker.drain_cycle_events() == ()
