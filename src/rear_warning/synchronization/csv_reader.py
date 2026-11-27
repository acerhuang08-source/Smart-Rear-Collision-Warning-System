"""Strict, hardware-free replay reader for synchronized CSV schema v2."""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement

from .csv_contract import (
    SynchronizedSampleCsvContractError,
    validate_synchronized_sample_csv_v2_contract,
)
from .csv_schema import (
    BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD, BNO055_CSV_V2_FIELDS,
    BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD,
    SYNCHRONIZED_SAMPLE_CSV_SCHEMA_VERSION, SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS,
)
from .models import SensorSyncStatus, SynchronizedSensorSample


class SynchronizedSampleCsvReadError(ValueError):
    """A synchronized CSV cannot be replayed without guessing values."""


class SynchronizedSampleCsvSchemaError(SynchronizedSampleCsvReadError):
    """The CSV header is not the exact named schema v2 header."""


class SynchronizedSampleCsvRowError(SynchronizedSampleCsvReadError):
    """A CSV row violates schema v2 syntax or sample semantics."""


@dataclass(frozen=True, slots=True)
class SynchronizedSampleCsvRecord:
    """One validated sequence number and its reconstructed sensor sample."""

    sequence: int
    sample: SynchronizedSensorSample


class StrictSynchronizedSampleCsvReader(
    Iterator[SynchronizedSampleCsvRecord]
):
    """Read exact schema v2 rows without hardware, clocks, or fallback values."""

    schema_version = SYNCHRONIZED_SAMPLE_CSV_SCHEMA_VERSION
    fieldnames = SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS

    def __init__(self, lines: Iterable[str]) -> None:
        self._reader = csv.reader(lines, strict=True)
        self._row_number = 1
        self._expected_sequence = 1
        try:
            header = tuple(next(self._reader))
        except StopIteration as exc:
            raise SynchronizedSampleCsvSchemaError(
                "missing synchronized CSV schema v2 header"
            ) from exc
        except csv.Error as exc:
            raise SynchronizedSampleCsvSchemaError(
                f"malformed synchronized CSV header: {exc}"
            ) from exc
        self._validate_header(header)

    @staticmethod
    def _validate_header(header: tuple[str, ...]) -> None:
        if BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD not in header:
            raise SynchronizedSampleCsvSchemaError(
                "legacy synchronized CSV schema is not replayable: missing "
                f"required v2 field {BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD!r}"
            )
        if header != SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS:
            raise SynchronizedSampleCsvSchemaError(
                "synchronized CSV header does not exactly match schema v2 "
                "field names and order"
            )

    def __iter__(self) -> StrictSynchronizedSampleCsvReader:
        return self

    def __next__(self) -> SynchronizedSampleCsvRecord:
        try:
            values = next(self._reader)
        except csv.Error as exc:
            raise SynchronizedSampleCsvRowError(
                f"row {self._row_number + 1}: malformed CSV: {exc}"
            ) from exc
        self._row_number += 1
        if len(values) != len(SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS):
            raise SynchronizedSampleCsvRowError(
                f"row {self._row_number}: expected "
                f"{len(SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS)} fields, got {len(values)}"
            )
        row = dict(zip(SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS, values, strict=True))
        record = self._parse_row(row, self._row_number)
        if record.sequence != self._expected_sequence:
            raise SynchronizedSampleCsvRowError(
                f"row {self._row_number}: sequence must be "
                f"{self._expected_sequence}, got {record.sequence}"
            )
        self._expected_sequence += 1
        return record

    @staticmethod
    def _parse_integer(
        row: dict[str, str],
        field: str,
        row_number: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        text = row[field]
        if not text or text != text.strip():
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be an integer"
            )
        try:
            value = int(text, 10)
        except ValueError as exc:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be an integer"
            ) from exc
        if minimum is not None and value < minimum:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be at least {minimum}"
            )
        if maximum is not None and value > maximum:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be at most {maximum}"
            )
        return value

    @staticmethod
    def _parse_finite(
        row: dict[str, str],
        field: str,
        row_number: int,
        *,
        non_negative: bool = False,
    ) -> float:
        text = row[field]
        if not text or text != text.strip():
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be a finite number"
            )
        try:
            value = float(text)
        except ValueError as exc:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be a finite number"
            ) from exc
        if not math.isfinite(value):
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be finite"
            )
        if non_negative and value < 0:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be non-negative"
            )
        return value

    @staticmethod
    def _parse_byte(
        row: dict[str, str], field: str, row_number: int
    ) -> int:
        text = row[field]
        if (
            len(text) != 4
            or not text.startswith("0x")
            or any(character not in "0123456789abcdefABCDEF" for character in text[2:])
        ):
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: {field} must be a two-digit 0x-prefixed byte"
            )
        return int(text, 16)

    @staticmethod
    def _parse_status(
        row: dict[str, str], row_number: int
    ) -> SensorSyncStatus:
        try:
            return SensorSyncStatus(row["sync_status"])
        except ValueError as exc:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: invalid SensorSyncStatus "
                f"{row['sync_status']!r}"
            ) from exc

    def _parse_row(
        self, row: dict[str, str], row_number: int
    ) -> SynchronizedSampleCsvRecord:
        sequence = self._parse_integer(
            row, "sequence", row_number, minimum=1, maximum=2**63 - 1
        )
        status = self._parse_status(row, row_number)
        distance_timestamp = self._parse_finite(
            row, "distance_timestamp", row_number, non_negative=True
        )
        distance_cm = self._parse_integer(row, "distance_cm", row_number)
        strength = self._parse_integer(row, "signal_strength", row_number)
        tfmini_temperature = self._parse_finite(
            row, "tfmini_temperature_c", row_number
        )
        distance = TFMiniPlusMeasurement(
            distance_cm=distance_cm,
            strength=strength,
            chip_temperature_c=tfmini_temperature,
        )

        motion_presence = {
            field: row[field] != "" for field in BNO055_CSV_V2_FIELDS
        }
        has_any_motion = any(motion_presence.values())
        has_complete_motion = all(motion_presence.values())
        if has_any_motion and not has_complete_motion:
            missing = [field for field, present in motion_presence.items() if not present]
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: incomplete BNO055 measurement fields: "
                + ", ".join(missing)
            )

        motion: BNO055Measurement | None = None
        canonical_motion_timestamp: float | None = None
        if has_complete_motion:
            canonical_motion_timestamp = self._parse_finite(
                row,
                BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD,
                row_number,
                non_negative=True,
            )
            raw_measurement_timestamp = self._parse_finite(
                row,
                BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD,
                row_number,
                non_negative=True,
            )
            motion = BNO055Measurement(
                monotonic_timestamp=raw_measurement_timestamp,
                euler=EulerAngles(
                    heading=self._parse_finite(row, "euler_heading", row_number),
                    roll=self._parse_finite(row, "euler_roll", row_number),
                    pitch=self._parse_finite(row, "euler_pitch", row_number),
                ),
                quaternion=Quaternion(
                    w=self._parse_finite(row, "quaternion_w", row_number),
                    x=self._parse_finite(row, "quaternion_x", row_number),
                    y=self._parse_finite(row, "quaternion_y", row_number),
                    z=self._parse_finite(row, "quaternion_z", row_number),
                ),
                linear_acceleration=Vector3(
                    x=self._parse_finite(
                        row, "linear_acceleration_x", row_number
                    ),
                    y=self._parse_finite(
                        row, "linear_acceleration_y", row_number
                    ),
                    z=self._parse_finite(
                        row, "linear_acceleration_z", row_number
                    ),
                ),
                gravity=Vector3(
                    x=self._parse_finite(row, "gravity_x", row_number),
                    y=self._parse_finite(row, "gravity_y", row_number),
                    z=self._parse_finite(row, "gravity_z", row_number),
                ),
                temperature_c=self._parse_integer(
                    row,
                    "bno055_temperature_c",
                    row_number,
                    minimum=-128,
                    maximum=127,
                ),
                calibration=BNO055Calibration(
                    system=self._parse_integer(
                        row, "calibration_system", row_number, minimum=0, maximum=3
                    ),
                    gyroscope=self._parse_integer(
                        row,
                        "calibration_gyroscope",
                        row_number,
                        minimum=0,
                        maximum=3,
                    ),
                    accelerometer=self._parse_integer(
                        row,
                        "calibration_accelerometer",
                        row_number,
                        minimum=0,
                        maximum=3,
                    ),
                    magnetometer=self._parse_integer(
                        row,
                        "calibration_magnetometer",
                        row_number,
                        minimum=0,
                        maximum=3,
                    ),
                ),
                operation_mode=self._parse_byte(row, "operation_mode", row_number),
                system_status=self._parse_byte(row, "system_status", row_number),
                system_error=self._parse_byte(row, "system_error", row_number),
            )
        age_text = row["motion_age_seconds"]
        motion_age = (
            None
            if age_text == ""
            else self._parse_finite(
                row, "motion_age_seconds", row_number, non_negative=True
            )
        )

        try:
            sample = SynchronizedSensorSample(
                distance_measurement=distance,
                motion_measurement=motion,
                distance_timestamp=distance_timestamp,
                motion_timestamp=canonical_motion_timestamp,
                motion_age_seconds=motion_age,
                status=status,
            )
        except (TypeError, ValueError) as exc:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: invalid synchronized sample: {exc}"
            ) from exc
        try:
            validate_synchronized_sample_csv_v2_contract(sample)
        except SynchronizedSampleCsvContractError as exc:
            raise SynchronizedSampleCsvRowError(
                f"row {row_number}: violates CSV schema v2 replay contract: {exc}"
            ) from exc
        return SynchronizedSampleCsvRecord(sequence=sequence, sample=sample)


__all__ = [
    "StrictSynchronizedSampleCsvReader",
    "SynchronizedSampleCsvReadError",
    "SynchronizedSampleCsvRecord",
    "SynchronizedSampleCsvRowError",
    "SynchronizedSampleCsvSchemaError",
]
