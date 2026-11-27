"""Hardware-free tests for the conservative host-vehicle motion estimate."""

from __future__ import annotations

import math

import pytest

from rear_warning.fusion import (
    VehicleMotionEstimator,
    VehicleMotionState,
    VehicleMotionThresholds,
)
from rear_warning.sensors.bno055.models import Vector3

PERIOD = 0.11  # BNO055 sampling period observed in the live captures


def _calm(index: int) -> Vector3:
    # Constant bias plus tiny noise, like the still model on a table.
    return Vector3(0.10 + 0.01 * math.sin(index), 0.05, 0.08)


def _shaking(index: int, amplitude: float = 0.6) -> Vector3:
    sign = 1.0 if index % 2 else -1.0
    return Vector3(sign * amplitude, -sign * amplitude, 0.0)


def _feed(
    estimator: VehicleMotionEstimator,
    start: float,
    count: int,
    sample: object,
) -> float:
    stamp = start
    for index in range(count):
        stamp = start + index * PERIOD
        estimator.update(stamp, sample(index))  # type: ignore[operator]
    return stamp


def test_starts_unknown_without_samples() -> None:
    snapshot = VehicleMotionEstimator().snapshot(0.0)
    assert snapshot.state is VehicleMotionState.UNKNOWN
    assert snapshot.reason == "no_samples"
    assert snapshot.vibration_mps2 is None


def test_long_calm_period_is_required_before_stationary() -> None:
    estimator = VehicleMotionEstimator()
    # About 9 s of calm data: warm-up plus calm time is still under 10 s.
    last = _feed(estimator, 0.0, 82, _calm)
    assert estimator.snapshot(last).state is VehicleMotionState.UNKNOWN

    last = _feed(estimator, last + PERIOD, 30, lambda i: _calm(i + 82))
    snapshot = estimator.snapshot(last)
    assert snapshot.state is VehicleMotionState.STATIONARY
    assert snapshot.reason == "sustained_low_vibration"
    assert snapshot.vibration_mps2 is not None
    assert snapshot.vibration_mps2 < 0.15


def test_sustained_vibration_leaves_stationary_quickly() -> None:
    estimator = VehicleMotionEstimator()
    last = _feed(estimator, 0.0, 120, _calm)
    assert estimator.snapshot(last).state is VehicleMotionState.STATIONARY

    start = last + PERIOD
    states = []
    for index in range(12):
        stamp = start + index * PERIOD
        states.append(estimator.update(stamp, _shaking(index)).state)
    assert states[-1] is VehicleMotionState.MOVING
    # Leaves STATIONARY within about one second of strong vibration.
    first_moving = states.index(VehicleMotionState.MOVING)
    assert first_moving * PERIOD <= 1.0


def test_moderate_vibration_drops_stationary_to_unknown_not_moving() -> None:
    estimator = VehicleMotionEstimator()
    last = _feed(estimator, 0.0, 120, _calm)
    assert estimator.snapshot(last).state is VehicleMotionState.STATIONARY

    last = _feed(estimator, last + PERIOD, 25, lambda i: _shaking(i, amplitude=0.13))
    snapshot = estimator.snapshot(last)
    assert snapshot.state is VehicleMotionState.UNKNOWN
    assert snapshot.reason == "vibration_above_stationary"


def test_stale_input_reads_as_unknown_without_losing_state() -> None:
    estimator = VehicleMotionEstimator()
    last = _feed(estimator, 0.0, 120, _calm)

    stale = estimator.snapshot(last + 0.6)
    assert stale.state is VehicleMotionState.UNKNOWN
    assert stale.reason == "motion_stale"
    assert estimator.snapshot(last + 0.4).state is VehicleMotionState.STATIONARY


def test_sample_gap_resets_all_evidence() -> None:
    estimator = VehicleMotionEstimator()
    last = _feed(estimator, 0.0, 120, _calm)

    snapshot = estimator.update(last + 0.8, _calm(0))
    assert snapshot.state is VehicleMotionState.UNKNOWN
    assert snapshot.sample_count == 1


def test_invalid_sample_resets_to_unknown() -> None:
    estimator = VehicleMotionEstimator()
    last = _feed(estimator, 0.0, 120, _calm)

    estimator.note_invalid(last + PERIOD)
    snapshot = estimator.snapshot(last + PERIOD)
    assert snapshot.state is VehicleMotionState.UNKNOWN
    assert snapshot.reason == "invalid_motion_sample"


def test_repeated_or_backwards_timestamps_are_ignored() -> None:
    estimator = VehicleMotionEstimator()
    last = _feed(estimator, 0.0, 20, _calm)
    before = estimator.snapshot(last)

    estimator.update(last, _shaking(1, amplitude=5.0))
    estimator.update(last - 1.0, _shaking(1, amplitude=5.0))

    assert estimator.snapshot(last) == before


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_seconds": 0.0},
        {"stationary_confirm_seconds": math.nan},
        {"moving_vibration_mps2": -1.0},
        {"minimum_samples": 1},
        {"stationary_vibration_mps2": 0.5, "moving_vibration_mps2": 0.3},
    ],
)
def test_invalid_thresholds_are_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises((ValueError, TypeError)):
        VehicleMotionThresholds(**kwargs)  # type: ignore[arg-type]


def test_non_finite_inputs_are_rejected() -> None:
    estimator = VehicleMotionEstimator()
    with pytest.raises(ValueError):
        estimator.update(math.inf, _calm(0))
    with pytest.raises(ValueError):
        estimator.update(1.0, Vector3(math.nan, 0.0, 0.0))
    with pytest.raises(TypeError):
        estimator.update(1.0, (0.0, 0.0, 0.0))  # type: ignore[arg-type]
