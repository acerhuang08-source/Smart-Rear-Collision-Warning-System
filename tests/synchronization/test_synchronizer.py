from __future__ import annotations

import math

import pytest

from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import (
    InvalidMotionMeasurement, SensorSyncStatus, SensorSynchronizer,
    SynchronizedSensorSample,
    TimedDistanceMeasurement, TimedMotionMeasurement,
)


def motion(*, quaternion: Quaternion = Quaternion(1, 0, 0, 0)) -> BNO055Measurement:
    return BNO055Measurement(
        1.0, EulerAngles(0, 0, 0), quaternion, Vector3(0, 0, 0),
        Vector3(0, 0, 9.8), 25, BNO055Calibration(0, 0, 0, 0),
        0x0C, 0x05, 0x00,
    )


def distance(value: int = 100) -> TFMiniPlusMeasurement:
    return TFMiniPlusMeasurement(value, 500, 24.5)


@pytest.mark.parametrize("value", [True, "1", math.nan, math.inf, -math.inf, -1.0])
@pytest.mark.parametrize("model", [TimedDistanceMeasurement, TimedMotionMeasurement])
def test_timed_models_reject_invalid_timestamp(value: object, model: type[object]) -> None:
    measurement = distance() if model is TimedDistanceMeasurement else motion()
    with pytest.raises((TypeError, ValueError)):
        model(measurement, value)  # type: ignore[call-arg]


def test_no_motion_sample() -> None:
    result = SensorSynchronizer().associate(TimedDistanceMeasurement(distance(), 1.0))
    assert result.status is SensorSyncStatus.NO_MOTION_SAMPLE
    assert result.motion_measurement is None and result.motion_age_seconds is None


@pytest.mark.parametrize("age", [0.0, 0.2])
def test_age_zero_and_maximum_are_matched(age: float) -> None:
    sync = SensorSynchronizer(maximum_motion_age_seconds=0.2)
    sync.update_motion(TimedMotionMeasurement(motion(), 1.0))
    result = sync.associate(TimedDistanceMeasurement(distance(), 1.0 + age))
    assert result.status is SensorSyncStatus.MATCHED
    assert result.motion_age_seconds == pytest.approx(age)


def test_age_over_maximum_is_stale_without_fake_age() -> None:
    sync = SensorSynchronizer(maximum_motion_age_seconds=0.2)
    sync.update_motion(TimedMotionMeasurement(motion(), 1.0))
    result = sync.associate(TimedDistanceMeasurement(distance(), 1.200001))
    assert result.status is SensorSyncStatus.STALE_MOTION_SAMPLE
    assert result.motion_timestamp == 1.0 and result.motion_age_seconds is None


def test_future_motion_is_not_matched() -> None:
    sync = SensorSynchronizer()
    sync.update_motion(TimedMotionMeasurement(motion(), 2.0))
    result = sync.associate(TimedDistanceMeasurement(distance(), 1.0))
    assert result.status is SensorSyncStatus.FUTURE_MOTION_SAMPLE
    assert result.motion_age_seconds is None


def test_new_motion_replaces_old_and_old_cannot_replace_new() -> None:
    sync = SensorSynchronizer()
    newest = TimedMotionMeasurement(motion(), 2.0)
    assert sync.update_motion(TimedMotionMeasurement(motion(), 1.0))
    assert sync.update_motion(newest)
    assert not sync.update_motion(TimedMotionMeasurement(motion(), 1.5))
    result = sync.associate(TimedDistanceMeasurement(distance(), 2.1))
    assert result.motion_timestamp == 2.0


def test_same_motion_matches_multiple_distances() -> None:
    sync = SensorSynchronizer(maximum_motion_age_seconds=0.2)
    sync.update_motion(TimedMotionMeasurement(motion(), 1.0))
    results = [
        sync.associate(TimedDistanceMeasurement(distance(), timestamp))
        for timestamp in (1.01, 1.02, 1.03)
    ]
    assert [item.status for item in results] == [SensorSyncStatus.MATCHED] * 3


def test_synchronizer_instances_do_not_share_motion() -> None:
    first, second = SensorSynchronizer(), SensorSynchronizer()
    first.update_motion(TimedMotionMeasurement(motion(), 1.0))
    assert first.associate(TimedDistanceMeasurement(distance(), 1.0)).status is SensorSyncStatus.MATCHED
    assert second.associate(TimedDistanceMeasurement(distance(), 1.0)).status is SensorSyncStatus.NO_MOTION_SAMPLE


def test_invalid_distance_and_motion_statuses() -> None:
    sync = SensorSynchronizer()
    invalid_distance = sync.associate(TimedDistanceMeasurement(distance(0), 1.0))
    assert invalid_distance.status is SensorSyncStatus.INVALID_DISTANCE_SAMPLE
    with pytest.raises(InvalidMotionMeasurement):
        sync.update_motion(
            TimedMotionMeasurement(
                motion(quaternion=Quaternion(0, 0, 0, 0)), 1.0
            )
        )


def test_invalid_new_motion_does_not_replace_latest_valid_motion() -> None:
    sync = SensorSynchronizer()
    sync.update_motion(TimedMotionMeasurement(motion(), 1.0))
    with pytest.raises(InvalidMotionMeasurement):
        sync.update_motion(
            TimedMotionMeasurement(
                motion(quaternion=Quaternion(0, 0, 0, 0)), 1.1
            )
        )
    result = sync.associate(TimedDistanceMeasurement(distance(), 1.15))
    assert result.status is SensorSyncStatus.MATCHED
    assert result.motion_timestamp == 1.0


@pytest.mark.parametrize(
    ("motion_time", "distance_time", "expected"),
    [
        (1.0, 1.2, SensorSyncStatus.MATCHED),
        (0.6, 0.8, SensorSyncStatus.MATCHED),
        (100_000.0, 100_000.2, SensorSyncStatus.MATCHED),
        (1.0, 1.199, SensorSyncStatus.MATCHED),
        (1.0, 1.200001, SensorSyncStatus.STALE_MOTION_SAMPLE),
        (1.1, 1.0, SensorSyncStatus.FUTURE_MOTION_SAMPLE),
    ],
)
def test_maximum_age_float_boundaries(
    motion_time: float,
    distance_time: float,
    expected: SensorSyncStatus,
) -> None:
    sync = SensorSynchronizer(maximum_motion_age_seconds=0.2)
    sync.update_motion(TimedMotionMeasurement(motion(), motion_time))
    result = sync.associate(TimedDistanceMeasurement(distance(), distance_time))
    assert result.status is expected


@pytest.mark.parametrize("value", [True, "0.2", 0, -1, math.nan, math.inf])
def test_maximum_age_intrinsic_validation(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        SensorSynchronizer(maximum_motion_age_seconds=value)  # type: ignore[arg-type]


def test_synchronized_model_invariants() -> None:
    with pytest.raises(ValueError, match="MATCHED requires"):
        SynchronizedSensorSample(distance(), None, 1.0, None, None, SensorSyncStatus.MATCHED)
    with pytest.raises(ValueError, match="unmatched"):
        SynchronizedSensorSample(distance(), None, 1.0, None, 0.0, SensorSyncStatus.NO_MOTION_SAMPLE)
    with pytest.raises(TypeError, match="SensorSyncStatus"):
        SynchronizedSensorSample(distance(), None, 1.0, None, None, "matched")  # type: ignore[arg-type]
