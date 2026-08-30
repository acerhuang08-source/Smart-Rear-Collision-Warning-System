"""Explicit, no-overwrite CSV output for synchronized samples."""

from __future__ import annotations

import csv
import math
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from .models import SynchronizedSensorSample


CSV_FIELDS = (
    "sequence", "distance_timestamp", "bno055_timestamp", "motion_age_seconds",
    "sync_status", "distance_cm", "signal_strength", "tfmini_temperature_c",
    "euler_heading", "euler_roll", "euler_pitch", "quaternion_w",
    "quaternion_x", "quaternion_y", "quaternion_z", "linear_acceleration_x",
    "linear_acceleration_y", "linear_acceleration_z", "gravity_x", "gravity_y",
    "gravity_z", "bno055_temperature_c", "calibration_system",
    "calibration_gyroscope", "calibration_accelerometer",
    "calibration_magnetometer", "operation_mode", "system_status", "system_error",
)


class CsvSinkError(Exception):
    """CSV creation, write, flush, or close failed."""


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
        try:
            self._file = opener(self.path, "x", encoding="utf-8", newline="")
            self._writer = csv.DictWriter(self._file, fieldnames=CSV_FIELDS)
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
        distance = sample.distance_measurement
        motion = sample.motion_measurement
        def finite_or_blank(value: object) -> object:
            return (
                value
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                else ""
            )

        row: dict[str, object] = {
            "sequence": sequence,
            "distance_timestamp": sample.distance_timestamp,
            "bno055_timestamp": "" if sample.motion_timestamp is None else sample.motion_timestamp,
            "motion_age_seconds": "" if sample.motion_age_seconds is None else sample.motion_age_seconds,
            "sync_status": sample.status.value,
            "distance_cm": distance.distance_cm,
            "signal_strength": distance.strength,
            "tfmini_temperature_c": finite_or_blank(distance.chip_temperature_c),
        }
        for field in CSV_FIELDS[8:]:
            row[field] = ""
        if motion is not None:
            row.update({
                "euler_heading": finite_or_blank(motion.euler.heading),
                "euler_roll": finite_or_blank(motion.euler.roll),
                "euler_pitch": finite_or_blank(motion.euler.pitch),
                "quaternion_w": finite_or_blank(motion.quaternion.w),
                "quaternion_x": finite_or_blank(motion.quaternion.x),
                "quaternion_y": finite_or_blank(motion.quaternion.y),
                "quaternion_z": finite_or_blank(motion.quaternion.z),
                "linear_acceleration_x": finite_or_blank(motion.linear_acceleration.x),
                "linear_acceleration_y": finite_or_blank(motion.linear_acceleration.y),
                "linear_acceleration_z": finite_or_blank(motion.linear_acceleration.z),
                "gravity_x": finite_or_blank(motion.gravity.x),
                "gravity_y": finite_or_blank(motion.gravity.y),
                "gravity_z": finite_or_blank(motion.gravity.z),
                "bno055_temperature_c": motion.temperature_c,
                "calibration_system": motion.calibration.system,
                "calibration_gyroscope": motion.calibration.gyroscope,
                "calibration_accelerometer": motion.calibration.accelerometer,
                "calibration_magnetometer": motion.calibration.magnetometer,
                "operation_mode": f"0x{motion.operation_mode:02x}",
                "system_status": f"0x{motion.system_status:02x}",
                "system_error": f"0x{motion.system_error:02x}",
            })
        try:
            self._writer.writerow(row)
            self._file.flush()
        except Exception as exc:
            raise CsvSinkError(f"failed to write CSV {self.path}") from exc

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
