from __future__ import annotations

import csv
import io
import math
from pathlib import Path

import pytest

from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import SensorSyncStatus, SynchronizedSensorSample
from rear_warning.synchronization.csv_sink import (
    CSV_FIELDS, CsvSinkError, SynchronizedSampleCsvSink,
)


def motion() -> BNO055Measurement:
    return BNO055Measurement(
        1.0, EulerAngles(1, 2, 3), Quaternion(1, 0, 0, 0),
        Vector3(0.1, 0.2, 0.3), Vector3(0, 0, 9.8), 25,
        BNO055Calibration(0, 3, 0, 0), 0x0C, 0x05, 0,
    )


def matched() -> SynchronizedSensorSample:
    return SynchronizedSensorSample(
        TFMiniPlusMeasurement(123, 456, 24.5), motion(), 1.1, 1.0, 0.1,
        SensorSyncStatus.MATCHED,
    )


def unmatched() -> SynchronizedSensorSample:
    return SynchronizedSensorSample(
        TFMiniPlusMeasurement(123, 456, 24.5), None, 1.1, None, None,
        SensorSyncStatus.NO_MOTION_SAMPLE,
    )


def test_csv_field_order_and_matched_unmatched_rows(tmp_path: Path) -> None:
    path = tmp_path / "samples.csv"
    sink = SynchronizedSampleCsvSink(path)
    sink.write(1, matched())
    sink.write(2, unmatched())
    sink.close()
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
        assert tuple(rows[0]) == CSV_FIELDS
    assert rows[0]["sequence"] == "1"
    assert rows[0]["quaternion_w"] == "1"
    assert rows[0]["operation_mode"] == "0x0c"
    assert rows[1]["sync_status"] == "no_motion_sample"
    assert rows[1]["motion_age_seconds"] == ""
    assert rows[1]["quaternion_w"] == ""


def test_existing_csv_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "samples.csv"
    path.write_text("original\n")
    with pytest.raises(CsvSinkError):
        SynchronizedSampleCsvSink(path)
    assert path.read_text() == "original\n"


class TrackingTextIO(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_calls = 0
        self.close_calls = 0

    def flush(self) -> None:
        self.flush_calls += 1
        super().flush()

    def close(self) -> None:
        self.close_calls += 1
        super().close()


def test_csv_flushes_writes_and_close_is_repeatable() -> None:
    stream = TrackingTextIO()
    sink = SynchronizedSampleCsvSink("unused.csv", opener=lambda *_args, **_kwargs: stream)
    sink.write(1, matched())
    assert stream.flush_calls >= 2
    sink.close()
    sink.close()
    assert stream.close_calls == 1


class FailingTextIO(TrackingTextIO):
    def flush(self) -> None:
        raise OSError("flush failed")


def test_csv_creation_flush_failure_is_reported() -> None:
    stream = FailingTextIO()
    with pytest.raises(CsvSinkError, match="create"):
        SynchronizedSampleCsvSink("unused.csv", opener=lambda *_args, **_kwargs: stream)


class CloseFailingTextIO(TrackingTextIO):
    def close(self) -> None:
        self.close_calls += 1
        if self.close_calls == 1:
            raise OSError("close failed")
        io.StringIO.close(self)


def test_csv_close_failure_is_reported() -> None:
    stream = CloseFailingTextIO()
    sink = SynchronizedSampleCsvSink("unused.csv", opener=lambda *_args, **_kwargs: stream)
    with pytest.raises(CsvSinkError, match="cleanup"):
        sink.close()


def test_schema_does_not_contain_algorithm_or_output_fields() -> None:
    assert "closing_speed" not in CSV_FIELDS
    assert "ttc" not in CSV_FIELDS
    assert "warning_state" not in CSV_FIELDS
    assert "led_state" not in CSV_FIELDS


def test_non_finite_unmatched_values_are_not_written(tmp_path: Path) -> None:
    path = tmp_path / "finite.csv"
    sample = SynchronizedSensorSample(
        TFMiniPlusMeasurement(100, 500, math.nan), None, 1.0, None, None,
        SensorSyncStatus.INVALID_DISTANCE_SAMPLE,
    )
    sink = SynchronizedSampleCsvSink(path)
    sink.write(1, sample)
    sink.close()
    with path.open(newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["tfmini_temperature_c"] == ""
