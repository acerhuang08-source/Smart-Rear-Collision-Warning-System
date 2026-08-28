"""I/O-free immutable data models for BNO055 observations."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BNO055Identity:
    address: int
    chip_id: int
    accelerometer_id: int
    magnetometer_id: int
    gyroscope_id: int
    software_revision: int
    bootloader_revision: int


@dataclass(frozen=True, slots=True)
class BNO055Calibration:
    system: int
    gyroscope: int
    accelerometer: int
    magnetometer: int

    def __post_init__(self) -> None:
        for name in ("system", "gyroscope", "accelerometer", "magnetometer"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if not 0 <= value <= 3:
                raise ValueError(f"{name} must be between 0 and 3")


@dataclass(frozen=True, slots=True)
class Vector3:
    x: float
    y: float
    z: float


@dataclass(frozen=True, slots=True)
class EulerAngles:
    heading: float
    roll: float
    pitch: float


@dataclass(frozen=True, slots=True)
class Quaternion:
    w: float
    x: float
    y: float
    z: float


@dataclass(frozen=True, slots=True)
class BNO055Measurement:
    monotonic_timestamp: float
    euler: EulerAngles
    quaternion: Quaternion
    linear_acceleration: Vector3
    gravity: Vector3
    temperature_c: int
    calibration: BNO055Calibration
    operation_mode: int
    system_status: int
    system_error: int
