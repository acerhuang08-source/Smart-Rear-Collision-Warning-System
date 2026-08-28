"""I/O-free quality checks for decoded BNO055 measurements."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .models import BNO055Measurement
from .registers import NDOF_MODE


@dataclass(frozen=True, slots=True)
class MeasurementQualityThresholds:
    """Broad startup-integrity limits, not calibration requirements."""

    quaternion_norm_min: float = 0.5
    quaternion_norm_max: float = 1.5
    gravity_norm_min_m_s2: float = 5.0
    gravity_norm_max_m_s2: float = 15.0

    def __post_init__(self) -> None:
        names = (
            "quaternion_norm_min",
            "quaternion_norm_max",
            "gravity_norm_min_m_s2",
            "gravity_norm_max_m_s2",
        )
        for name in names:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
        if self.quaternion_norm_min >= self.quaternion_norm_max:
            raise ValueError("quaternion_norm_min must be less than quaternion_norm_max")
        if self.gravity_norm_min_m_s2 >= self.gravity_norm_max_m_s2:
            raise ValueError(
                "gravity_norm_min_m_s2 must be less than gravity_norm_max_m_s2"
            )


@dataclass(frozen=True, slots=True)
class MeasurementQualityAssessment:
    """Separate fusion-state faults from decoded-data integrity faults."""

    state_issues: tuple[str, ...]
    data_issues: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        return not self.state_issues and not self.data_issues


def assess_measurement_quality(
    measurement: BNO055Measurement,
    thresholds: MeasurementQualityThresholds = MeasurementQualityThresholds(),
) -> MeasurementQualityAssessment:
    """Assess one decoded measurement without performing any I/O."""

    state_issues: list[str] = []
    if measurement.operation_mode != NDOF_MODE:
        state_issues.append(
            f"operation_mode=0x{measurement.operation_mode:02x} expected=0x{NDOF_MODE:02x}"
        )
    if measurement.system_status != 0x05:
        state_issues.append(
            f"system_status=0x{measurement.system_status:02x} expected=0x05"
        )
    if measurement.system_error != 0x00:
        state_issues.append(
            f"system_error=0x{measurement.system_error:02x} expected=0x00"
        )

    values = {
        "monotonic_timestamp": measurement.monotonic_timestamp,
        "euler.heading": measurement.euler.heading,
        "euler.roll": measurement.euler.roll,
        "euler.pitch": measurement.euler.pitch,
        "quaternion.w": measurement.quaternion.w,
        "quaternion.x": measurement.quaternion.x,
        "quaternion.y": measurement.quaternion.y,
        "quaternion.z": measurement.quaternion.z,
        "linear_acceleration.x": measurement.linear_acceleration.x,
        "linear_acceleration.y": measurement.linear_acceleration.y,
        "linear_acceleration.z": measurement.linear_acceleration.z,
        "gravity.x": measurement.gravity.x,
        "gravity.y": measurement.gravity.y,
        "gravity.z": measurement.gravity.z,
    }
    data_issues = [
        f"{name} is not finite"
        for name, value in values.items()
        if not math.isfinite(value)
    ]

    quaternion_values = (
        measurement.quaternion.w,
        measurement.quaternion.x,
        measurement.quaternion.y,
        measurement.quaternion.z,
    )
    if all(math.isfinite(value) for value in quaternion_values):
        quaternion_norm = math.sqrt(sum(value * value for value in quaternion_values))
        if not thresholds.quaternion_norm_min <= quaternion_norm <= thresholds.quaternion_norm_max:
            data_issues.append(
                f"quaternion_norm={quaternion_norm:.6f} outside "
                f"[{thresholds.quaternion_norm_min:.6f},"
                f"{thresholds.quaternion_norm_max:.6f}]"
            )

    gravity_values = (
        measurement.gravity.x,
        measurement.gravity.y,
        measurement.gravity.z,
    )
    if all(math.isfinite(value) for value in gravity_values):
        gravity_norm = math.sqrt(sum(value * value for value in gravity_values))
        if not thresholds.gravity_norm_min_m_s2 <= gravity_norm <= thresholds.gravity_norm_max_m_s2:
            data_issues.append(
                f"gravity_norm={gravity_norm:.6f} outside "
                f"[{thresholds.gravity_norm_min_m_s2:.6f},"
                f"{thresholds.gravity_norm_max_m_s2:.6f}]"
            )

    return MeasurementQualityAssessment(tuple(state_issues), tuple(data_issues))
