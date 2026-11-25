from __future__ import annotations

from dataclasses import replace

import pytest

from rear_warning.live.ioradar701_shadow import (
    IntegratedRadarAvailability,
    IoRadar701ShadowCore,
)
from rear_warning.sensors.bno055.models import (
    BNO055Calibration,
    BNO055Measurement,
    EulerAngles,
    Quaternion,
    Vector3,
)
from rear_warning.synchronization import InvalidMotionMeasurement


def frame(kind: int, *, identifier: int = 0, count: int = 0,
          x_raw: int = 100, y_raw: int = 1200) -> bytes:
    body = bytearray(11)
    body[0] = 0xA5
    body[1] = kind
    if kind == 1:
        body[3] = 0x05
        body[6] = count
    else:
        body[3] = identifier
        body[4:6] = x_raw.to_bytes(2, "big")
        body[6:8] = y_raw.to_bytes(2, "big")
        body[8:10] = (6500).to_bytes(2, "big")
        body[10] = 80
    return bytes(body) + bytes((sum(body) & 0xFF,))


def motion(timestamp: float) -> BNO055Measurement:
    return BNO055Measurement(
        timestamp,
        EulerAngles(0, 0, 0),
        Quaternion(1, 0, 0, 0),
        Vector3(0, 0, 0),
        Vector3(0, 0, 9.8),
        25,
        BNO055Calibration(0, 0, 0, 0),
        0x0C,
        0x05,
        0,
    )


def test_selector_is_order_independent_and_switches_to_nearest_id() -> None:
    core = IoRadar701ShadowCore()
    payload = (
        frame(1, count=2)
        + frame(2, identifier=9, x_raw=400)
        + frame(2, identifier=3, x_raw=200)
    )
    first, = core.feed_bytes(payload, 1.0).assessments
    assert first.selection is not None
    assert first.selection.target_id == 3
    assert first.selection.target_changed

    reordered = (
        frame(1, count=2)
        + frame(2, identifier=3, x_raw=500)
        + frame(2, identifier=9, x_raw=100)
    )
    second, = core.feed_bytes(reordered, 1.1).assessments
    assert second.selection is not None
    assert second.selection.target_id == 9
    assert second.selection.target_changed
    assert second.selection.previous_target_id == 3
    assert second.selection.reason == (
        "nearer target ID 9 replaced previous target ID 3"
    )
    assert second.reset_reason == "target switch 3->9"
    assert second.warning_candidate is not None
    assert second.warning_candidate.value == "danger"
    assert second.warning_stabilized is not None
    assert second.warning_stabilized.value == "danger"


@pytest.mark.parametrize(
    "target_frames",
    [
        (frame(2, identifier=9, x_raw=200), frame(2, identifier=3, x_raw=200)),
        (frame(2, identifier=3, x_raw=200), frame(2, identifier=9, x_raw=200)),
    ],
)
def test_selector_ties_use_lowest_id_regardless_of_input_order(
    target_frames: tuple[bytes, bytes],
) -> None:
    core = IoRadar701ShadowCore()

    selected, = core.feed_bytes(
        frame(1, count=2) + b"".join(target_frames), 1.0
    ).assessments

    assert selected.selection is not None
    assert selected.selection.target_id == 3
    assert [
        candidate.target_id for candidate in selected.selection.candidates
    ] == [3, 9]


def test_invalid_checksum_target_is_not_a_selector_candidate() -> None:
    core = IoRadar701ShadowCore()
    invalid_near = bytearray(frame(2, identifier=2, x_raw=100))
    invalid_near[-1] ^= 0x01

    selected, = core.feed_bytes(
        frame(1, count=1)
        + bytes(invalid_near)
        + frame(2, identifier=1, x_raw=400),
        1.0,
    ).assessments

    assert selected.selection is not None
    assert selected.selection.target_id == 1
    assert [
        candidate.target_id for candidate in selected.selection.candidates
    ] == [1]
    assert core.parser.statistics.checksum_failures == 1


def test_target_switch_resets_distance_history_without_reusing_motion() -> None:
    core = IoRadar701ShadowCore()
    core.update_motion(motion(1.0))
    first, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=1, x_raw=500), 1.0
    ).assessments
    assert first.fusion is not None
    assert first.fusion.distance_trend.sample_count == 1
    assert first.fusion.own_motion_evidence.unique_sample_count == 1

    switched, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=2, x_raw=100), 1.1
    ).assessments
    assert switched.reset_reason == "target switch 1->2"
    assert switched.fusion is not None
    assert switched.fusion.distance_trend.sample_count == 1
    assert switched.fusion.own_motion_evidence.unique_sample_count == 1


def test_collecting_cycle_does_not_reset_but_closed_incomplete_does() -> None:
    core = IoRadar701ShadowCore()
    first, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=1), 1.0
    ).assessments
    assert first.selection is not None and first.selection.target_id == 1
    assert core.feed_bytes(frame(1, count=2), 1.1).assessments == ()
    assert core.selector.selected_target_id == 1
    results = core.feed_bytes(frame(1, count=0), 1.2).assessments
    assert [item.availability for item in results] == [
        IntegratedRadarAvailability.INCOMPLETE_CYCLE,
        IntegratedRadarAvailability.NO_TARGETS,
    ]
    assert core.selector.selected_target_id is None


def test_no_motion_is_not_ready_but_distance_warning_is_independent() -> None:
    core = IoRadar701ShadowCore()
    result, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=1, x_raw=100), 1.0
    ).assessments
    assert result.availability is IntegratedRadarAvailability.BNO_NOT_READY
    assert result.warning_candidate is not None
    assert result.gpio_enabled is False


def test_tick_expires_without_new_callback_and_bad_bytes_do_not_extend_target() -> None:
    core = IoRadar701ShadowCore(stale_after_seconds=0.5)
    core.feed_bytes(frame(1, count=1) + frame(2, identifier=1), 1.0)
    core.feed_bytes(b"garbage", 1.4)
    expired = core.tick(1.6)
    assert expired is not None
    assert expired.availability is IntegratedRadarAvailability.RADAR_STALE
    assert core.selector.selected_target_id is None
    assert core.tick(1.7) is None


def test_t4_leading_ff_recovers_through_shared_parser() -> None:
    core = IoRadar701ShadowCore()
    batch = core.feed_bytes(b"\xff" + frame(1, count=1) + frame(2, identifier=4), 1.0)
    assert core.parser.statistics.sync_discarded_bytes == 1
    assert batch.assessments[0].selection is not None
    assert batch.assessments[0].selection.target_id == 4


def test_invalid_motion_is_not_replaced_by_old_valid_motion() -> None:
    core = IoRadar701ShadowCore()
    core.update_motion(motion(1.0), captured_at_monotonic_seconds=1.0)
    invalid = replace(motion(1.1), quaternion=Quaternion(0, 0, 0, 0))
    with pytest.raises(InvalidMotionMeasurement, match="invalid motion measurement"):
        core.update_motion(invalid, captured_at_monotonic_seconds=1.1)
    result, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=1), 1.1
    ).assessments
    assert result.availability is IntegratedRadarAvailability.SYNC_UNMATCHED
    assert result.motion_match is not None
    assert result.motion_match.status.value == "invalid_motion_sample"


def test_two_cycles_in_one_receipt_batch_apply_only_the_final_cycle() -> None:
    core = IoRadar701ShadowCore()
    core.update_motion(motion(1.0))
    payload = (
        frame(1, count=1)
        + frame(2, identifier=1, x_raw=500)
        + frame(1, count=1)
        + frame(2, identifier=1, x_raw=450)
    )

    first, final = core.feed_bytes(
        payload, 1.0, receipt_batch_sequence=1
    ).assessments

    assert first.availability is IntegratedRadarAvailability.FUSION_UPDATE_COALESCED
    assert first.fusion is None
    assert first.fusion_update == "coalesced"
    assert first.coalesced_into_cycle_sequence == 2
    assert first.receipt_batch_sequence == 1
    assert final.availability is not IntegratedRadarAvailability.RADAR_TIME_DISCONTINUITY
    assert final.fusion_update == "applied"
    assert final.receipt_batch_sequence == 1
    assert final.fusion is not None
    assert final.fusion.distance_trend.sample_count == 1
    assert final.fusion.own_motion_evidence.unique_sample_count == 1

    follow_up, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=1, x_raw=400),
        1.1,
        receipt_batch_sequence=2,
    ).assessments
    assert follow_up.fusion is not None
    assert follow_up.fusion.distance_trend.sample_count == 2
    assert follow_up.fusion.own_motion_evidence.unique_sample_count == 1


def test_equal_timestamp_across_receipt_batches_is_still_rejected() -> None:
    core = IoRadar701ShadowCore()
    payload = frame(1, count=1) + frame(2, identifier=1)
    core.feed_bytes(payload, 1.0, receipt_batch_sequence=1)

    repeated, = core.feed_bytes(
        payload, 1.0, receipt_batch_sequence=2
    ).assessments

    assert repeated.availability is IntegratedRadarAvailability.RADAR_TIME_DISCONTINUITY
    assert repeated.time_relation == "equal_across_receipt_batches"
    assert repeated.fusion_update == "rejected_time"


def test_equal_timestamp_after_invalid_prior_batch_is_also_rejected() -> None:
    core = IoRadar701ShadowCore()
    no_targets, = core.feed_bytes(
        frame(1, count=0), 1.0, receipt_batch_sequence=1
    ).assessments
    assert no_targets.availability is IntegratedRadarAvailability.NO_TARGETS

    repeated, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=1),
        1.0,
        receipt_batch_sequence=2,
    ).assessments

    assert repeated.availability is IntegratedRadarAvailability.RADAR_TIME_DISCONTINUITY
    assert repeated.time_relation == "equal_across_receipt_batches"


def test_backwards_timestamp_has_a_distinct_reason() -> None:
    core = IoRadar701ShadowCore()
    payload = frame(1, count=1) + frame(2, identifier=1)
    core.feed_bytes(payload, 1.0, receipt_batch_sequence=1)

    backwards, = core.feed_bytes(
        payload, 0.9, receipt_batch_sequence=2
    ).assessments

    assert backwards.availability is IntegratedRadarAvailability.RADAR_TIME_DISCONTINUITY
    assert backwards.time_relation == "backwards"
    assert backwards.reason == "completed cycle timestamp moved backwards"


def test_invalid_final_cycle_does_not_fall_back_to_earlier_selected_cycle() -> None:
    core = IoRadar701ShadowCore()
    payload = (
        frame(1, count=1)
        + frame(2, identifier=1)
        + frame(1, count=0)
    )

    selected, empty = core.feed_bytes(
        payload, 1.0, receipt_batch_sequence=1
    ).assessments

    assert selected.availability is IntegratedRadarAvailability.FUSION_UPDATE_COALESCED
    assert selected.fusion is None
    assert empty.availability is IntegratedRadarAvailability.NO_TARGETS
    assert core.selector.selected_target_id is None

    recovered, = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=1),
        1.1,
        receipt_batch_sequence=2,
    ).assessments
    assert recovered.fusion is not None
    assert recovered.fusion.distance_trend.sample_count == 1


def test_same_batch_target_switch_resets_before_final_fusion_update() -> None:
    core = IoRadar701ShadowCore()
    payload = (
        frame(1, count=1)
        + frame(2, identifier=1, x_raw=500)
        + frame(1, count=1)
        + frame(2, identifier=2, x_raw=200)
    )

    first, switched = core.feed_bytes(
        payload, 1.0, receipt_batch_sequence=1
    ).assessments

    assert first.fusion_update == "coalesced"
    assert switched.reset_reason == "target switch 1->2"
    assert switched.selection is not None and switched.selection.target_id == 2
    assert switched.fusion is not None
    assert switched.fusion.distance_trend.sample_count == 1


def test_incomplete_cycle_before_final_valid_cycle_does_not_block_final() -> None:
    core = IoRadar701ShadowCore()
    assert core.feed_bytes(
        frame(1, count=2) + frame(2, identifier=1),
        1.0,
        receipt_batch_sequence=1,
    ).assessments == ()

    incomplete, final = core.feed_bytes(
        frame(1, count=1) + frame(2, identifier=2),
        1.1,
        receipt_batch_sequence=2,
    ).assessments

    assert incomplete.availability is IntegratedRadarAvailability.INCOMPLETE_CYCLE
    assert final.fusion_update == "applied"
    assert final.selection is not None and final.selection.target_id == 2
    assert final.fusion is not None
    assert final.fusion.distance_trend.sample_count == 1


def test_large_forward_gap_remains_an_explicit_fusion_discontinuity() -> None:
    core = IoRadar701ShadowCore()
    payload = frame(1, count=1) + frame(2, identifier=1)
    core.feed_bytes(payload, 1.0, receipt_batch_sequence=1)

    result, = core.feed_bytes(
        payload, 1.3, receipt_batch_sequence=2
    ).assessments

    assert result.fusion is not None
    assert result.fusion.distance_trend.sample_count == 1
    assert result.fusion.distance_trend.is_valid is False
    assert result.fusion.distance_trend.reason is not None
    assert result.fusion.distance_trend.reason.value == "large_time_gap"


def test_receipt_batch_sequence_must_strictly_increase() -> None:
    core = IoRadar701ShadowCore()
    core.feed_bytes(b"", 1.0, receipt_batch_sequence=2)
    with pytest.raises(ValueError, match="strictly increasing"):
        core.feed_bytes(b"", 1.1, receipt_batch_sequence=2)
