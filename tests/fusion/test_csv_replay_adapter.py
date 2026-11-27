"""Synthetic software integration of CSV schema v2 and the fusion adapter."""

from __future__ import annotations

import csv
import io
from unittest.mock import Mock

import pytest

from rear_warning.fusion import (
    RelativeMotionAssessment,
    RelativeMotionClass,
    RelativeMotionFusionAdapter,
)
from rear_warning.sensors.bno055.models import (
    BNO055Calibration,
    BNO055Measurement,
    EulerAngles,
    Quaternion,
    Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import (
    BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD,
    SensorSyncStatus,
    StrictSynchronizedSampleCsvReader,
    SynchronizedSampleCsvSchemaError,
    SynchronizedSensorSample,
)
from rear_warning.synchronization.csv_schema import (
    SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS,
)
from rear_warning.synchronization.csv_sink import SynchronizedSampleCsvSink


DISTANCE_RECORD_COUNT = 115
UNIQUE_CANONICAL_MOTION_COUNT = 10
START_TIMESTAMP = 100.0
DISTANCE_INTERVAL_SECONDS = 0.01
MOTION_INTERVAL_SECONDS = 0.11
RAW_TO_CANONICAL_DELAY_SECONDS = 0.005


def _stationary_motion(canonical_timestamp: float) -> BNO055Measurement:
    return BNO055Measurement(
        monotonic_timestamp=(
            canonical_timestamp - RAW_TO_CANONICAL_DELAY_SECONDS
        ),
        euler=EulerAngles(heading=0.0, roll=0.0, pitch=0.0),
        quaternion=Quaternion(w=1.0, x=0.0, y=0.0, z=0.0),
        linear_acceleration=Vector3(x=0.0, y=0.0, z=0.0),
        gravity=Vector3(x=0.0, y=0.0, z=9.8),
        temperature_c=25,
        calibration=BNO055Calibration(
            system=0,
            gyroscope=3,
            accelerometer=0,
            magnetometer=0,
        ),
        operation_mode=0x0C,
        system_status=0x05,
        system_error=0x00,
    )


def _synthetic_matched_sample(index: int) -> SynchronizedSensorSample:
    distance_timestamp = START_TIMESTAMP + index * DISTANCE_INTERVAL_SECONDS
    motion_index = min(index // 11, UNIQUE_CANONICAL_MOTION_COUNT - 1)
    canonical_motion_timestamp = (
        START_TIMESTAMP + motion_index * MOTION_INTERVAL_SECONDS
    )
    return SynchronizedSensorSample(
        distance_measurement=TFMiniPlusMeasurement(
            distance_cm=500,
            strength=500,
            chip_temperature_c=24.5,
        ),
        motion_measurement=_stationary_motion(canonical_motion_timestamp),
        distance_timestamp=distance_timestamp,
        motion_timestamp=canonical_motion_timestamp,
        motion_age_seconds=distance_timestamp - canonical_motion_timestamp,
        status=SensorSyncStatus.MATCHED,
    )


def _serialize_synthetic_v2() -> str:
    stream = io.StringIO()
    sink = SynchronizedSampleCsvSink(
        "unused-synthetic.csv",
        opener=lambda *_args, **_kwargs: stream,
    )
    for index in range(DISTANCE_RECORD_COUNT):
        sink.write(index + 1, _synthetic_matched_sample(index))
    csv_text = stream.getvalue()
    sink.close()
    return csv_text


def test_synthetic_csv_v2_round_trip_feeds_fusion_adapter() -> None:
    reader = StrictSynchronizedSampleCsvReader(
        io.StringIO(_serialize_synthetic_v2())
    )
    adapter = RelativeMotionFusionAdapter()
    records = []
    assessments: list[RelativeMotionAssessment] = []

    for expected_sequence, record in enumerate(reader, start=1):
        assert record.sequence == expected_sequence
        assert record.sample.status is SensorSyncStatus.MATCHED
        records.append(record)
        assessments.append(adapter.update(record.sample))

    assert len(records) == DISTANCE_RECORD_COUNT
    assert records[-1].sample.distance_timestamp - records[0].sample.distance_timestamp == (
        pytest.approx(1.14)
    )

    canonical_timestamps = {
        record.sample.motion_timestamp for record in records
    }
    assert len(canonical_timestamps) == UNIQUE_CANONICAL_MOTION_COUNT
    assert len(canonical_timestamps) != len(records)
    ordered_canonical_timestamps = sorted(canonical_timestamps)
    assert all(
        right - left == pytest.approx(MOTION_INTERVAL_SECONDS)
        for left, right in zip(
            ordered_canonical_timestamps[:-1],
            ordered_canonical_timestamps[1:],
            strict=True,
        )
    )

    for record in records:
        motion = record.sample.motion_measurement
        canonical_timestamp = record.sample.motion_timestamp
        assert motion is not None
        assert canonical_timestamp is not None
        assert motion.monotonic_timestamp != canonical_timestamp
        assert motion.monotonic_timestamp <= canonical_timestamp
        assert canonical_timestamp - motion.monotonic_timestamp == pytest.approx(
            RAW_TO_CANONICAL_DELAY_SECONDS
        )

    final = assessments[-1]
    assert isinstance(final, RelativeMotionAssessment)
    assert final.is_valid
    assert final.is_ready
    assert final.stationary_baseline_ready
    assert final.classification is RelativeMotionClass.STABLE_OR_RECEDING
    assert (
        final.own_motion_evidence.unique_sample_count
        == len(canonical_timestamps)
        == UNIQUE_CANONICAL_MOTION_COUNT
    )
    assert final.own_motion_evidence.unique_sample_count != len(records)
    assert all(
        assessment.classification is not RelativeMotionClass.TARGET_APPROACH
        for assessment in assessments
    )


def test_legacy_header_stops_before_fusion_adapter() -> None:
    legacy_header = tuple(
        field
        for field in SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS
        if field != BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD
    )
    stream = io.StringIO()
    csv.writer(stream).writerow(legacy_header)
    adapter = Mock(spec_set=RelativeMotionFusionAdapter)

    with pytest.raises(
        SynchronizedSampleCsvSchemaError,
        match="legacy.*not replayable.*bno055_measurement_timestamp",
    ):
        reader = StrictSynchronizedSampleCsvReader(io.StringIO(stream.getvalue()))
        for record in reader:
            adapter.update(record.sample)

    adapter.update.assert_not_called()
