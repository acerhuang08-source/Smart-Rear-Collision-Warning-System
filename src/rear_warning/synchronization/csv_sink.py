"""Explicit, no-overwrite CSV output for synchronized samples."""

from __future__ import annotations

import csv
import math
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .csv_contract import (
    SynchronizedSampleCsvContractError,
    validate_synchronized_sample_csv_v2_contract,
)
from .csv_schema import (
    BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD, BNO055_CSV_V2_FIELDS,
    BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD, SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS,
)
from .models import SynchronizedSensorSample


# Backward-compatible import name; the sink itself is explicitly pinned to v2.
CSV_FIELDS = SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS


class CsvSinkError(Exception):
    """CSV creation, write, flush, or close failed."""


def _finite_number(
    name: str, value: object, *, non_negative: bool = False
) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise CsvSinkError(f"{name} must be a finite number for CSV schema v2")
    if non_negative and value < 0:
        raise CsvSinkError(
            f"{name} must be non-negative for CSV schema v2"
        )
    return value


def _integer(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CsvSinkError(f"{name} must be an integer for CSV schema v2")
    if minimum is not None and value < minimum:
        raise CsvSinkError(
            f"{name} must be at least {minimum} for CSV schema v2"
        )
    if maximum is not None and value > maximum:
        raise CsvSinkError(
            f"{name} must be at most {maximum} for CSV schema v2"
        )
    return value


class SynchronizedSampleCsvSink:
    def __init__(
        self,
        path: str | Path,
        *,
        opener: Callable[..., TextIO] = open,
    ) -> None:
        self.path = Path(path)
        self._file: TextIO | None = None
        self._closed = False
        self._expected_sequence = 1
        try:
            self._file = opener(self.path, "x", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(
                self._file, fieldnames=SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS
            )
            self._writer.writeheader()
            self._file.flush()
        except Exception as exc:
            if self._file is not None:
                try:
                    self._file.close()
                except Exception:
                    pass
            raise CsvSinkError(f"failed to create CSV {self.path}") from exc

    def write(self, sequence: int, sample: SynchronizedSensorSample) -> None:
        if self._closed or self._file is None:
            raise CsvSinkError("CSV sink is closed")
        sequence = _integer("sequence", sequence, minimum=1, maximum=2**63 - 1)
        if sequence != self._expected_sequence:
            raise CsvSinkError(
                f"sequence must be {self._expected_sequence}, got {sequence}"
            )
        try:
            validate_synchronized_sample_csv_v2_contract(sample)
        except SynchronizedSampleCsvContractError as exc:
            raise CsvSinkError(
                f"sample violates CSV schema v2 replay contract: {exc}"
            ) from exc
        distance = sample.distance_measurement
        motion = sample.motion_measurement

        row: dict[str, object] = {
            "sequence": sequence,
            "distance_timestamp": _finite_number(
                "distance_timestamp", sample.distance_timestamp, non_negative=True
            ),
            BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD: (
                ""
                if sample.motion_timestamp is None
                else _finite_number(
                    "canonical motion timestamp",
                    sample.motion_timestamp,
                    non_negative=True,
                )
            ),
            BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD: "",
            "motion_age_seconds": (
                ""
                if sample.motion_age_seconds is None
                else _finite_number(
                    "motion_age_seconds",
                    sample.motion_age_seconds,
                    non_negative=True,
                )
            ),
            "sync_status": sample.status.value,
            "distance_cm": _integer("distance_cm", distance.distance_cm),
            "signal_strength": _integer("signal_strength", distance.strength),
            "tfmini_temperature_c": _finite_number(
                "tfmini_temperature_c", distance.chip_temperature_c
            ),
        }
        for field in BNO055_CSV_V2_FIELDS:
            if field == BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD:
                continue
            row[field] = ""
        if motion is not None:
            row.update({
                BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD: _finite_number(
                    "bno055_measurement_timestamp",
                    motion.monotonic_timestamp,
                    non_negative=True,
                ),
                "euler_heading": _finite_number(
                    "euler_heading", motion.euler.heading
                ),
                "euler_roll": _finite_number("euler_roll", motion.euler.roll),
                "euler_pitch": _finite_number("euler_pitch", motion.euler.pitch),
                "quaternion_w": _finite_number(
                    "quaternion_w", motion.quaternion.w
                ),
                "quaternion_x": _finite_number(
                    "quaternion_x", motion.quaternion.x
                ),
                "quaternion_y": _finite_number(
                    "quaternion_y", motion.quaternion.y
                ),
                "quaternion_z": _finite_number(
                    "quaternion_z", motion.quaternion.z
                ),
                "linear_acceleration_x": _finite_number(
                    "linear_acceleration_x", motion.linear_acceleration.x
                ),
                "linear_acceleration_y": _finite_number(
                    "linear_acceleration_y", motion.linear_acceleration.y
                ),
                "linear_acceleration_z": _finite_number(
                    "linear_acceleration_z", motion.linear_acceleration.z
                ),
                "gravity_x": _finite_number("gravity_x", motion.gravity.x),
                "gravity_y": _finite_number("gravity_y", motion.gravity.y),
                "gravity_z": _finite_number("gravity_z", motion.gravity.z),
                "bno055_temperature_c": _integer(
                    "bno055_temperature_c",
                    motion.temperature_c,
                    minimum=-128,
                    maximum=127,
                ),
                "calibration_system": _integer(
                    "calibration_system",
                    motion.calibration.system,
                    minimum=0,
                    maximum=3,
                ),
                "calibration_gyroscope": _integer(
                    "calibration_gyroscope",
                    motion.calibration.gyroscope,
                    minimum=0,
                    maximum=3,
                ),
                "calibration_accelerometer": _integer(
                    "calibration_accelerometer",
                    motion.calibration.accelerometer,
                    minimum=0,
                    maximum=3,
                ),
                "calibration_magnetometer": _integer(
                    "calibration_magnetometer",
                    motion.calibration.magnetometer,
                    minimum=0,
                    maximum=3,
                ),
                "operation_mode": (
                    f"0x{_integer('operation_mode', motion.operation_mode, minimum=0, maximum=0xFF):02x}"
                ),
                "system_status": (
                    f"0x{_integer('system_status', motion.system_status, minimum=0, maximum=0xFF):02x}"
                ),
                "system_error": (
                    f"0x{_integer('system_error', motion.system_error, minimum=0, maximum=0xFF):02x}"
                ),
            })
        try:
            self._writer.writerow(row)
            self._file.flush()
        except Exception as exc:
            raise CsvSinkError(f"failed to write CSV {self.path}") from exc
        self._expected_sequence += 1

    def close(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
        if self._file is not None:
            try:
                self._file.flush()
            except BaseException as exc:
                errors.append(exc)
            try:
                self._file.close()
            except BaseException as exc:
                errors.append(exc)
        self._closed = True
        if errors:
            error = CsvSinkError(
                "CSV cleanup failed: " + "; ".join(str(item) for item in errors)
            )
            for item in errors:
                error.add_note(repr(item))
            raise error from errors[0]
