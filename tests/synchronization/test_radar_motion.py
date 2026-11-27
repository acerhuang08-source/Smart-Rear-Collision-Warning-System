from __future__ import annotations

from dataclasses import replace

import pytest

from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.synchronization import (
    InvalidMotionMeasurement, RadarMotionSynchronizer, SensorSyncStatus,
    TimedMotionMeasurement,
)


def motion(timestamp: float) -> BNO055Measurement:
    return BNO055Measurement(
        timestamp, EulerAngles(0, 0, 0), Quaternion(1, 0, 0, 0),
        Vector3(0, 0, 0), Vector3(0, 0, 9.8), 25,
        BNO055Calibration(0, 0, 0, 0), 0x0C, 0x05, 0,
    )


@pytest.mark.parametrize(
    ("range_time", "status"),
    [(1.0, SensorSyncStatus.MATCHED), (1.2, SensorSyncStatus.MATCHED),
     (1.200001, SensorSyncStatus.STALE_MOTION_SAMPLE),
     (0.9, SensorSyncStatus.FUTURE_MOTION_SAMPLE)],
)
def test_causal_boundaries(range_time: float, status: SensorSyncStatus) -> None:
    sync = RadarMotionSynchronizer(maximum_motion_age_seconds=0.2)
    sync.update_motion(TimedMotionMeasurement(motion(1.0), 1.0))
    assert sync.associate(range_time).status is status


def test_invalid_motion_is_rejected_without_replacing_latest() -> None:
    sync = RadarMotionSynchronizer()
    sync.update_motion(TimedMotionMeasurement(motion(1.0), 1.0))
    invalid = replace(motion(1.1), quaternion=Quaternion(0, 0, 0, 0))
    with pytest.raises(InvalidMotionMeasurement):
        sync.update_motion(TimedMotionMeasurement(invalid, 1.1))
    assert sync.associate(1.1).motion_timestamp == 1.0


def test_explicit_invalid_motion_blocks_old_valid_until_recovery() -> None:
    sync = RadarMotionSynchronizer()
    sync.update_motion(TimedMotionMeasurement(motion(1.0), 1.0))
    invalid = replace(motion(1.1), quaternion=Quaternion(0, 0, 0, 0))
    rejected = TimedMotionMeasurement(invalid, 1.1)
    with pytest.raises(InvalidMotionMeasurement):
        sync.update_motion(rejected)
    assert sync.note_invalid_motion(rejected)
    assert sync.associate(1.1).status is SensorSyncStatus.INVALID_MOTION_SAMPLE
    sync.update_motion(TimedMotionMeasurement(motion(1.2), 1.2))
    assert sync.associate(1.2).status is SensorSyncStatus.MATCHED
