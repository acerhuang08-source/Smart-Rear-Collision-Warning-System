"""Synthetic tests for short-window own-motion evidence estimation."""

import math

import pytest

from rear_warning.fusion import (
    InconsistentMotionSampleError,
    OwnMotionEvidence,
    OwnMotionEstimator,
    OwnMotionReason,
    OwnMotionThresholds,
)

_IDENTITY = (1.0, 0.0, 0.0, 0.0)


def rotation_about_z(angle_radians: float) -> tuple[float, float, float, float]:
    return (
        math.cos(angle_radians / 2.0),
        0.0,
        0.0,
        math.sin(angle_radians / 2.0),
    )


def update(
    estimator: OwnMotionEstimator,
    timestamp: object,
    acceleration: object = (0.0, 0.0, 0.0),
    quaternion: object = _IDENTITY,
) -> OwnMotionEvidence:
    return estimator.update(
        timestamp,
        linear_acceleration_mps2=acceleration,  # type: ignore[arg-type]
        quaternion=quaternion,  # type: ignore[arg-type]
    )


def test_stationary_window_has_no_motion_evidence() -> None:
    estimator = OwnMotionEstimator()
    evidence = [update(estimator, index / 10) for index in range(11)][-1]

    assert evidence.is_ready and evidence.is_valid
    assert evidence.unique_sample_count == 11
    assert evidence.linear_accel_rms_mps2 == 0.0
    assert evidence.linear_accel_peak_mps2 == 0.0
    assert evidence.delta_v_evidence_mps == 0.0
    assert evidence.net_orientation_change_radians == 0.0
    assert evidence.cumulative_orientation_change_radians == 0.0
    assert not evidence.translational_motion_detected
    assert not evidence.rotational_motion_detected


def test_small_deterministic_noise_does_not_trigger_motion() -> None:
    estimator = OwnMotionEstimator()
    evidence = None
    for index in range(11):
        acceleration = (
            (-0.04, 0.04)[index % 2],
            (0.03, -0.03)[index % 2],
            0.02,
        )
        evidence = update(
            estimator,
            index / 10,
            acceleration,
            rotation_about_z(math.radians(index * 0.1)),
        )

    assert evidence is not None
    assert evidence.linear_accel_peak_mps2 < 0.1
    assert evidence.cumulative_orientation_change_radians < math.radians(2.0)
    assert not evidence.translational_motion_detected
    assert not evidence.rotational_motion_detected


def test_sustained_acceleration_produces_translation_and_delta_v_evidence() -> None:
    estimator = OwnMotionEstimator()
    evidence = [
        update(estimator, index / 10, (0.5, 0.0, 0.0))
        for index in range(11)
    ][-1]

    assert evidence.linear_accel_rms_mps2 == pytest.approx(0.5)
    assert evidence.linear_accel_peak_mps2 == pytest.approx(0.5)
    assert evidence.delta_v_evidence_mps == pytest.approx(0.5)
    assert evidence.translational_motion_detected


def test_single_acceleration_spike_can_trigger_peak_without_persistence() -> None:
    estimator = OwnMotionEstimator()
    evidence = None
    for index in range(11):
        acceleration = (1.1, 0.0, 0.0) if index == 5 else (0.0, 0.0, 0.0)
        evidence = update(estimator, index / 10, acceleration)

    assert evidence is not None
    assert evidence.linear_accel_rms_mps2 < estimator.thresholds.linear_accel_rms_threshold_mps2
    assert evidence.linear_accel_peak_mps2 > estimator.thresholds.linear_accel_peak_threshold_mps2
    assert evidence.delta_v_evidence_mps < estimator.thresholds.delta_v_evidence_threshold_mps
    assert evidence.translational_motion_detected


def test_opposing_acceleration_keeps_rms_but_vector_delta_v_cancels() -> None:
    estimator = OwnMotionEstimator()
    evidence = [
        update(estimator, index / 10, ((-1.0, 1.0)[index % 2], 0.0, 0.0))
        for index in range(11)
    ][-1]

    assert evidence.linear_accel_rms_mps2 == pytest.approx(1.0)
    assert evidence.delta_v_evidence_mps == pytest.approx(0.0, abs=1e-12)
    assert evidence.translational_motion_detected


def test_fixed_quaternion_has_zero_orientation_change() -> None:
    estimator = OwnMotionEstimator()
    evidence = [update(estimator, index / 10, quaternion=_IDENTITY) for index in range(4)][-1]

    assert evidence.net_orientation_change_radians == 0.0
    assert evidence.cumulative_orientation_change_radians == 0.0
    assert not evidence.rotational_motion_detected


def test_ninety_degree_rotation_has_correct_orientation_evidence() -> None:
    estimator = OwnMotionEstimator()
    angles = (0.0, math.pi / 6, math.pi / 3, math.pi / 2)
    evidence = [
        update(estimator, index / 10, quaternion=rotation_about_z(angle))
        for index, angle in enumerate(angles)
    ][-1]

    assert evidence.net_orientation_change_radians == pytest.approx(math.pi / 2)
    assert evidence.cumulative_orientation_change_radians == pytest.approx(math.pi / 2)
    assert evidence.rotational_motion_detected


def test_quaternion_and_its_negation_have_zero_angular_distance() -> None:
    estimator = OwnMotionEstimator()
    negative_identity = tuple(-value for value in _IDENTITY)
    quaternions = (_IDENTITY, negative_identity, _IDENTITY, negative_identity)
    evidence = [
        update(estimator, index / 10, quaternion=quaternion)
        for index, quaternion in enumerate(quaternions)
    ][-1]

    assert evidence.net_orientation_change_radians == 0.0
    assert evidence.cumulative_orientation_change_radians == 0.0
    assert not evidence.rotational_motion_detected


def test_rotation_out_and_back_retains_cumulative_evidence() -> None:
    estimator = OwnMotionEstimator()
    quaternions = (
        _IDENTITY,
        rotation_about_z(math.pi / 2),
        _IDENTITY,
        _IDENTITY,
    )
    evidence = [
        update(estimator, index / 10, quaternion=quaternion)
        for index, quaternion in enumerate(quaternions)
    ][-1]

    assert evidence.net_orientation_change_radians == 0.0
    assert evidence.cumulative_orientation_change_radians == pytest.approx(math.pi)
    assert evidence.rotational_motion_detected


def test_pruning_removes_rotation_that_has_left_the_window() -> None:
    estimator = OwnMotionEstimator()
    rotated = rotation_about_z(math.pi / 2)
    update(estimator, 0.0, quaternion=_IDENTITY)
    update(estimator, 0.1, quaternion=rotated)

    evidence = None
    for index in range(2, 13):
        evidence = update(estimator, index / 10, quaternion=rotated)

    assert evidence is not None
    assert evidence.unique_sample_count == 11
    assert evidence.window_span_seconds == pytest.approx(1.0)
    assert evidence.net_orientation_change_radians == 0.0
    assert evidence.cumulative_orientation_change_radians == 0.0
    assert not evidence.rotational_motion_detected


def test_rms_equal_to_threshold_detects_translation() -> None:
    thresholds = OwnMotionThresholds(
        linear_accel_rms_threshold_mps2=0.5,
        linear_accel_peak_threshold_mps2=2.0,
        delta_v_evidence_threshold_mps=2.0,
    )
    estimator = OwnMotionEstimator(thresholds)
    evidence = [
        update(estimator, index / 10, (0.5, 0.0, 0.0))
        for index in range(4)
    ][-1]

    assert evidence.linear_accel_rms_mps2 == thresholds.linear_accel_rms_threshold_mps2
    assert evidence.translational_motion_detected


def test_peak_equal_to_threshold_detects_translation() -> None:
    thresholds = OwnMotionThresholds(
        linear_accel_rms_threshold_mps2=2.0,
        linear_accel_peak_threshold_mps2=1.0,
        delta_v_evidence_threshold_mps=2.0,
    )
    estimator = OwnMotionEstimator(thresholds)
    evidence = None
    for index in range(4):
        acceleration = (1.0, 0.0, 0.0) if index == 1 else (0.0, 0.0, 0.0)
        evidence = update(estimator, index / 10, acceleration)

    assert evidence is not None
    assert evidence.linear_accel_peak_mps2 == thresholds.linear_accel_peak_threshold_mps2
    assert evidence.translational_motion_detected


def test_delta_v_equal_to_threshold_detects_translation() -> None:
    thresholds = OwnMotionThresholds(
        linear_accel_rms_threshold_mps2=1.0,
        linear_accel_peak_threshold_mps2=1.0,
        delta_v_evidence_threshold_mps=0.25,
    )
    estimator = OwnMotionEstimator(thresholds)
    evidence = [
        update(estimator, index / 8, (0.5, 0.0, 0.0))
        for index in range(5)
    ][-1]

    assert evidence.delta_v_evidence_mps == thresholds.delta_v_evidence_threshold_mps
    assert evidence.translational_motion_detected


def test_cumulative_rotation_equal_to_threshold_detects_rotation() -> None:
    rotated = rotation_about_z(math.pi / 2)
    norm = math.hypot(*rotated)
    boundary_angle = 2.0 * math.acos(abs(rotated[0] / norm))
    thresholds = OwnMotionThresholds(
        cumulative_orientation_threshold_radians=boundary_angle
    )
    estimator = OwnMotionEstimator(thresholds)
    quaternions = (_IDENTITY, _IDENTITY, _IDENTITY, rotated)
    evidence = [
        update(estimator, index / 10, quaternion=quaternion)
        for index, quaternion in enumerate(quaternions)
    ][-1]

    assert (
        evidence.cumulative_orientation_change_radians
        == thresholds.cumulative_orientation_threshold_radians
    )
    assert evidence.rotational_motion_detected


def test_identical_timestamp_and_payload_is_a_no_op() -> None:
    estimator = OwnMotionEstimator()
    first = update(estimator, 1.0, (0.2, 0.0, 0.0))

    duplicates = [update(estimator, 1.0, (0.2, 0.0, 0.0)) for _ in range(10)]

    assert all(evidence is first for evidence in duplicates)
    assert first.unique_sample_count == 1
    assert first.linear_accel_rms_mps2 == pytest.approx(0.2)
    assert first.linear_accel_peak_mps2 == pytest.approx(0.2)
    assert first.delta_v_evidence_mps == 0.0


def test_equivalent_negated_quaternion_duplicate_is_a_no_op() -> None:
    estimator = OwnMotionEstimator()
    first = update(estimator, 1.0, quaternion=_IDENTITY)

    duplicate = update(
        estimator,
        1.0,
        quaternion=tuple(-value for value in _IDENTITY),
    )

    assert duplicate is first
    assert duplicate.unique_sample_count == 1


def test_conflicting_duplicate_is_rejected_without_mutating_history() -> None:
    estimator = OwnMotionEstimator()
    first = update(estimator, 1.0, (0.0, 0.0, 0.0))

    with pytest.raises(InconsistentMotionSampleError, match="conflicting"):
        update(estimator, 1.0, (1.0, 0.0, 0.0))

    second = update(estimator, 1.1, (0.0, 0.0, 0.0))
    assert first.unique_sample_count == 1
    assert second.unique_sample_count == 2


def test_backwards_timestamp_is_rejected_without_mutating_history() -> None:
    estimator = OwnMotionEstimator()
    update(estimator, 1.0)

    with pytest.raises(ValueError, match="strictly increasing"):
        update(estimator, 0.9)

    evidence = update(estimator, 1.1)
    assert evidence.unique_sample_count == 2


def test_large_gap_starts_a_new_not_ready_segment() -> None:
    estimator = OwnMotionEstimator()
    for index in range(4):
        update(estimator, index / 10, (0.5, 0.0, 0.0))

    evidence = update(estimator, 1.0, (2.0, 0.0, 0.0))

    assert not evidence.is_valid
    assert not evidence.is_ready
    assert evidence.reason is OwnMotionReason.LARGE_TIME_GAP
    assert evidence.unique_sample_count == 1
    assert evidence.window_span_seconds == 0.0
    assert evidence.linear_accel_peak_mps2 == 2.0
    assert evidence.delta_v_evidence_mps == 0.0
    assert not evidence.translational_motion_detected


def test_irregular_timestamps_control_trapezoidal_integration() -> None:
    estimator = OwnMotionEstimator()
    timestamps = (0.0, 0.07, 0.19, 0.41, 0.6, 0.7)
    evidence = [
        update(estimator, timestamp, (1.0, 0.0, 0.0))
        for timestamp in timestamps
    ][-1]

    assert evidence.window_span_seconds == pytest.approx(0.7)
    assert evidence.delta_v_evidence_mps == pytest.approx(0.7)


@pytest.mark.parametrize(
    ("timestamp", "acceleration", "quaternion", "error"),
    [
        (float("nan"), (0.0, 0.0, 0.0), _IDENTITY, ValueError),
        (float("inf"), (0.0, 0.0, 0.0), _IDENTITY, ValueError),
        (True, (0.0, 0.0, 0.0), _IDENTITY, TypeError),
        ("0", (0.0, 0.0, 0.0), _IDENTITY, TypeError),
        (0.0, (float("nan"), 0.0, 0.0), _IDENTITY, ValueError),
        (0.0, (float("inf"), 0.0, 0.0), _IDENTITY, ValueError),
        (0.0, (True, 0.0, 0.0), _IDENTITY, TypeError),
        (0.0, ("0", 0.0, 0.0), _IDENTITY, TypeError),
        (0.0, (0.0, 0.0), _IDENTITY, ValueError),
        (0.0, None, _IDENTITY, TypeError),
        (0.0, (0.0, 0.0, 0.0), (float("nan"), 0.0, 0.0, 0.0), ValueError),
        (0.0, (0.0, 0.0, 0.0), (float("inf"), 0.0, 0.0, 0.0), ValueError),
        (0.0, (0.0, 0.0, 0.0), (True, 0.0, 0.0, 0.0), TypeError),
        (0.0, (0.0, 0.0, 0.0), ("1", 0.0, 0.0, 0.0), TypeError),
        (0.0, (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), ValueError),
        (0.0, (0.0, 0.0, 0.0), None, TypeError),
    ],
)
def test_invalid_input_is_rejected(
    timestamp: object,
    acceleration: object,
    quaternion: object,
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        update(OwnMotionEstimator(), timestamp, acceleration, quaternion)


def test_zero_norm_quaternion_is_rejected_without_mutating_history() -> None:
    estimator = OwnMotionEstimator()
    update(estimator, 0.0)

    with pytest.raises(ValueError, match="non-zero norm"):
        update(estimator, 0.1, quaternion=(0.0, 0.0, 0.0, 0.0))

    evidence = update(estimator, 0.1)
    assert evidence.unique_sample_count == 2


def test_sample_cap_bounds_window_size() -> None:
    estimator = OwnMotionEstimator()
    evidence = [update(estimator, index / 100) for index in range(50)][-1]

    assert evidence.unique_sample_count == 32
    assert evidence.window_span_seconds == pytest.approx(0.31)
    assert evidence.is_ready
