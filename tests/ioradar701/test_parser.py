"""Hardware-independent regression tests for ioRadar701 byte decoding."""

from __future__ import annotations

import math

import pytest

from rear_warning.sensors.ioradar701.models import StatusFrame, TargetFrame
from rear_warning.sensors.ioradar701.parser import IoRadar701StreamParser

STATUS = bytes.fromhex("a5 01 00 05 0b 00 01 00 00 00 00 b7")
TARGET = bytes.fromhex("a5 02 00 01 00 e9 04 c6 19 64 66 3e")


def test_supplied_real_frames_decode_to_expected_values() -> None:
    parser = IoRadar701StreamParser()
    frames = parser.feed(STATUS + TARGET)

    assert frames[0] == StatusFrame(
        raw=STATUS, check=1, active=1, error=0, roll_count=3, target_count=1,
    )
    target = frames[1]
    assert isinstance(target, TargetFrame)
    assert target.target_id == 1
    assert target.x_raw == 233
    assert target.y_raw == 1222
    assert target.vx_raw == 6500
    assert target.x_m == pytest.approx(2.33)
    assert target.y_m == pytest.approx(0.22)
    assert target.vx_mps == pytest.approx(0.0)
    assert target.energy == 102
    assert target.distance_m == pytest.approx(math.hypot(2.33, 0.22))
    assert parser.statistics.status_frames == 1
    assert parser.statistics.target_frames == 1


@pytest.mark.parametrize("split", range(1, 24))
def test_every_split_boundary_retains_partial_frames(split: int) -> None:
    payload = STATUS + TARGET
    parser = IoRadar701StreamParser()

    first = parser.feed(payload[:split])
    second = parser.feed(payload[split:])

    assert first + second == IoRadar701StreamParser().feed(payload)
    assert parser.statistics.trailing_bytes == 0


def test_garbage_bad_checksum_and_embedded_recovery_do_not_emit_bad_frame() -> None:
    damaged = bytearray(STATUS)
    damaged[-1] ^= 0x01
    parser = IoRadar701StreamParser()

    frames = parser.feed(b"\x00\x7f" + STATUS + damaged + TARGET)

    assert [frame.kind for frame in frames] == ["status", "target"]
    assert parser.statistics.checksum_failures == 1
    assert parser.statistics.sync_discarded_bytes == 14
    assert parser.statistics.trailing_bytes == 0


def test_multiple_frames_and_half_final_frame_are_handled_together() -> None:
    parser = IoRadar701StreamParser()
    assert len(parser.feed(STATUS + TARGET + STATUS[:7])) == 2
    assert parser.pending_bytes == STATUS[:7]
    assert parser.statistics.trailing_bytes == 7
    assert parser.feed(STATUS[7:]) == [
        StatusFrame(raw=STATUS, check=1, active=1, error=0, roll_count=3, target_count=1)
    ]


def test_invalid_input_is_rejected() -> None:
    parser = IoRadar701StreamParser()
    for invalid in ("not bytes", 12):
        with pytest.raises(TypeError):
            parser.feed(invalid)  # type: ignore[arg-type]
