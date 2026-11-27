from __future__ import annotations

import csv
import io

import pytest

from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import (
    SensorSyncStatus, StrictSynchronizedSampleCsvReader,
    SynchronizedSampleCsvRowError, SynchronizedSampleCsvSchemaError,
    SynchronizedSensorSample,
)
from rear_warning.synchronization.csv_schema import (
    SYNCHRONIZED_SAMPLE_CSV_SCHEMA_VERSION, SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS,
)
from rear_warning.synchronization.csv_sink import (
    CsvSinkError, SynchronizedSampleCsvSink,
)


RAW_MEASUREMENT_TIMESTAMP = 40.125
CANONICAL_MOTION_TIMESTAMP = 40.25
DISTANCE_TIMESTAMP = 40.30


def motion(
    *,
    raw_timestamp: float = RAW_MEASUREMENT_TIMESTAMP,
    quality_valid: bool = True,
) -> BNO055Measurement:
    return BNO055Measurement(
        monotonic_timestamp=raw_timestamp,
        euler=EulerAngles(heading=10.5, roll=-2.25, pitch=3.75),
        quaternion=(
            Quaternion(w=1.0, x=0.01, y=-0.02, z=0.03)
            if quality_valid
            else Quaternion(w=0.0, x=0.0, y=0.0, z=0.0)
        ),
        linear_acceleration=Vector3(x=0.1, y=-0.2, z=0.3),
        gravity=Vector3(x=0.0, y=0.0, z=9.8),
        temperature_c=26,
        calibration=BNO055Calibration(
            system=1, gyroscope=2, accelerometer=3, magnetometer=0
        ),
        operation_mode=0x0C,
        system_status=0x05,
        system_error=0x00,
    )


def sample_for_status(
    status: SensorSyncStatus,
    *,
    raw_timestamp: float | None = None,
) -> SynchronizedSensorSample:
    distance = TFMiniPlusMeasurement(
        distance_cm=321,
        strength=(-1 if status is SensorSyncStatus.INVALID_DISTANCE_SAMPLE else 654),
        chip_temperature_c=27.5,
    )
    if status in {
        SensorSyncStatus.NO_MOTION_SAMPLE,
        SensorSyncStatus.INVALID_DISTANCE_SAMPLE,
    }:
        return SynchronizedSensorSample(
            distance_measurement=distance,
            motion_measurement=None,
            distance_timestamp=DISTANCE_TIMESTAMP,
            motion_timestamp=None,
            motion_age_seconds=None,
            status=status,
        )

    canonical_timestamp = {
        SensorSyncStatus.MATCHED: CANONICAL_MOTION_TIMESTAMP,
        SensorSyncStatus.STALE_MOTION_SAMPLE: 40.0,
        SensorSyncStatus.FUTURE_MOTION_SAMPLE: 40.4,
        SensorSyncStatus.INVALID_MOTION_SAMPLE: CANONICAL_MOTION_TIMESTAMP,
    }[status]
    effective_raw_timestamp = (
        raw_timestamp
        if raw_timestamp is not None
        else min(RAW_MEASUREMENT_TIMESTAMP, canonical_timestamp)
    )
    return SynchronizedSensorSample(
        distance_measurement=distance,
        motion_measurement=motion(
            raw_timestamp=effective_raw_timestamp,
            quality_valid=status is not SensorSyncStatus.INVALID_MOTION_SAMPLE,
        ),
        distance_timestamp=DISTANCE_TIMESTAMP,
        motion_timestamp=canonical_timestamp,
        motion_age_seconds=(
            DISTANCE_TIMESTAMP - canonical_timestamp
            if status is SensorSyncStatus.MATCHED
            else None
        ),
        status=status,
    )


def complete_sample() -> SynchronizedSensorSample:
    return sample_for_status(SensorSyncStatus.MATCHED)


def serialized_v2_sample(
    sample: SynchronizedSensorSample | None = None,
) -> str:
    stream = io.StringIO()
    sink = SynchronizedSampleCsvSink(
        "unused.csv", opener=lambda *_args, **_kwargs: stream
    )
    sink.write(1, complete_sample() if sample is None else sample)
    return stream.getvalue()


def replace_field(csv_text: str, field: str, value: str) -> str:
    source = io.StringIO(csv_text)
    reader = csv.DictReader(source)
    row = next(reader)
    row[field] = value
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS)
    writer.writeheader()
    writer.writerow(row)
    return output.getvalue()


@pytest.mark.parametrize("status", list(SensorSyncStatus))
def test_every_supported_status_round_trips_through_sink_and_reader(
    status: SensorSyncStatus,
) -> None:
    expected = sample_for_status(status)
    reader = StrictSynchronizedSampleCsvReader(
        io.StringIO(serialized_v2_sample(expected))
    )
    assert reader.schema_version == SYNCHRONIZED_SAMPLE_CSV_SCHEMA_VERSION == 2
    records = list(reader)
    assert len(records) == 1
    assert records[0].sequence == 1
    assert records[0].sample == expected
    assert isinstance(
        records[0].sample.distance_measurement, TFMiniPlusMeasurement
    )
    if expected.motion_measurement is None:
        assert records[0].sample.motion_measurement is None
    else:
        assert isinstance(records[0].sample.motion_measurement, BNO055Measurement)


def test_raw_and_canonical_motion_timestamps_remain_independent() -> None:
    record = next(
        StrictSynchronizedSampleCsvReader(io.StringIO(serialized_v2_sample()))
    )
    assert record.sample.motion_measurement is not None
    assert (
        record.sample.motion_measurement.monotonic_timestamp
        == RAW_MEASUREMENT_TIMESTAMP
    )
    assert record.sample.motion_timestamp == CANONICAL_MOTION_TIMESTAMP
    assert (
        record.sample.motion_measurement.monotonic_timestamp
        != record.sample.motion_timestamp
    )


def test_raw_timestamp_equal_to_canonical_is_a_valid_boundary() -> None:
    expected = sample_for_status(
        SensorSyncStatus.MATCHED,
        raw_timestamp=CANONICAL_MOTION_TIMESTAMP,
    )
    record = next(
        StrictSynchronizedSampleCsvReader(
            io.StringIO(serialized_v2_sample(expected))
        )
    )
    assert record.sample == expected
    assert record.sample.motion_measurement is not None
    assert (
        record.sample.motion_measurement.monotonic_timestamp
        == record.sample.motion_timestamp
    )


def test_sink_rejects_no_motion_status_with_motion_payload() -> None:
    inconsistent = SynchronizedSensorSample(
        distance_measurement=TFMiniPlusMeasurement(321, 654, 27.5),
        motion_measurement=motion(),
        distance_timestamp=DISTANCE_TIMESTAMP,
        motion_timestamp=CANONICAL_MOTION_TIMESTAMP,
        motion_age_seconds=None,
        status=SensorSyncStatus.NO_MOTION_SAMPLE,
    )
    stream = io.StringIO()
    sink = SynchronizedSampleCsvSink(
        "unused.csv", opener=lambda *_args, **_kwargs: stream
    )
    with pytest.raises(CsvSinkError, match="no_motion_sample.*must not contain"):
        sink.write(1, inconsistent)
    assert len(list(csv.reader(io.StringIO(stream.getvalue())))) == 1


def test_reader_rejects_no_motion_status_with_motion_payload() -> None:
    with_motion = serialized_v2_sample(
        sample_for_status(SensorSyncStatus.STALE_MOTION_SAMPLE)
    )
    malformed = replace_field(with_motion, "sync_status", "no_motion_sample")
    reader = StrictSynchronizedSampleCsvReader(io.StringIO(malformed))
    with pytest.raises(
        SynchronizedSampleCsvRowError,
        match="no_motion_sample.*must not contain",
    ):
        next(reader)


def test_sink_rejects_raw_timestamp_later_than_canonical() -> None:
    invalid = sample_for_status(
        SensorSyncStatus.MATCHED,
        raw_timestamp=CANONICAL_MOTION_TIMESTAMP + 0.001,
    )
    stream = io.StringIO()
    sink = SynchronizedSampleCsvSink(
        "unused.csv", opener=lambda *_args, **_kwargs: stream
    )
    with pytest.raises(CsvSinkError, match="less than or equal"):
        sink.write(1, invalid)
    assert len(list(csv.reader(io.StringIO(stream.getvalue())))) == 1


def test_reader_rejects_raw_timestamp_later_than_canonical() -> None:
    malformed = replace_field(
        serialized_v2_sample(),
        "bno055_measurement_timestamp",
        str(CANONICAL_MOTION_TIMESTAMP + 0.001),
    )
    reader = StrictSynchronizedSampleCsvReader(io.StringIO(malformed))
    with pytest.raises(SynchronizedSampleCsvRowError, match="less than or equal"):
        next(reader)


def test_legacy_header_and_row_without_raw_timestamp_are_explicitly_rejected() -> None:
    rows = list(csv.reader(io.StringIO(serialized_v2_sample())))
    raw_index = rows[0].index("bno055_measurement_timestamp")
    del rows[0][raw_index]
    del rows[1][raw_index]
    legacy = io.StringIO()
    csv.writer(legacy).writerows(rows)
    with pytest.raises(
        SynchronizedSampleCsvSchemaError,
        match="legacy.*not replayable.*bno055_measurement_timestamp",
    ):
        StrictSynchronizedSampleCsvReader(io.StringIO(legacy.getvalue()))


def test_header_field_order_must_exactly_match_named_v2_schema() -> None:
    rows = list(csv.reader(io.StringIO(serialized_v2_sample())))
    rows[0][0], rows[0][1] = rows[0][1], rows[0][0]
    malformed = io.StringIO()
    csv.writer(malformed).writerows(rows)
    with pytest.raises(SynchronizedSampleCsvSchemaError, match="order"):
        StrictSynchronizedSampleCsvReader(io.StringIO(malformed.getvalue()))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("sequence", "1.0", "sequence must be an integer"),
        ("gravity_z", "nan", "gravity_z must be finite"),
        ("sync_status", "unknown", "invalid SensorSyncStatus"),
        ("motion_age_seconds", "0.06", "age must equal"),
    ],
)
def test_malformed_row_values_are_rejected(
    field: str, value: str, message: str
) -> None:
    malformed = replace_field(serialized_v2_sample(), field, value)
    reader = StrictSynchronizedSampleCsvReader(io.StringIO(malformed))
    with pytest.raises(SynchronizedSampleCsvRowError, match=message):
        next(reader)
