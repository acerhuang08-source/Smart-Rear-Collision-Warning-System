"""BNO055 register constants and pure byte-to-model conversions."""

from __future__ import annotations

from collections.abc import Sequence

from .models import BNO055Calibration, EulerAngles, Quaternion, Vector3

CHIP_ID = 0x00
ACC_ID = 0x01
MAG_ID = 0x02
GYR_ID = 0x03
SW_REV_ID_LSB = 0x04
SW_REV_ID_MSB = 0x05
BL_REV_ID = 0x06
PAGE_ID = 0x07
EULER_H_LSB = 0x1A
MEASUREMENT_DATA_START = EULER_H_LSB
MEASUREMENT_DATA_LENGTH = 26
EULER_OFFSET = 0
EULER_LENGTH = 6
QUATERNION_OFFSET = 6
QUATERNION_LENGTH = 8
LINEAR_ACCELERATION_OFFSET = 14
LINEAR_ACCELERATION_LENGTH = 6
GRAVITY_OFFSET = 20
GRAVITY_LENGTH = 6
QUATERNION_W_LSB = 0x20
LINEAR_ACCEL_X_LSB = 0x28
GRAVITY_X_LSB = 0x2E
TEMPERATURE = 0x34
CALIB_STAT = 0x35
SYS_STATUS = 0x39
SYS_ERR = 0x3A
UNIT_SEL = 0x3B
OPR_MODE = 0x3D
PWR_MODE = 0x3E
AXIS_MAP_CONFIG = 0x41
AXIS_MAP_SIGN = 0x42
CALIBRATION_PROFILE_START = 0x55
CALIBRATION_PROFILE_END = 0x6A
CALIBRATION_PROFILE_LENGTH = CALIBRATION_PROFILE_END - CALIBRATION_PROFILE_START + 1

EXPECTED_CHIP_ID = 0xA0
EXPECTED_ACC_ID = 0xFB
EXPECTED_MAG_ID = 0x32
EXPECTED_GYR_ID = 0x0F
CONFIG_MODE = 0x00
NDOF_MODE = 0x0C
NORMAL_POWER_MODE = 0x00
DEFAULT_UNITS = 0x00


def signed_int16_le(lsb: int, msb: int) -> int:
    """Decode one little-endian signed 16-bit value."""
    for value in (lsb, msb):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("bytes must be integers")
        if not 0 <= value <= 0xFF:
            raise ValueError("bytes must be between 0 and 255")
    unsigned = lsb | (msb << 8)
    return unsigned - 0x10000 if unsigned & 0x8000 else unsigned


def signed_int8(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("byte must be an integer")
    if not 0 <= value <= 0xFF:
        raise ValueError("byte must be between 0 and 255")
    return value - 0x100 if value & 0x80 else value


def _scaled_values(data: Sequence[int], count: int, scale: float) -> list[float]:
    expected = count * 2
    if len(data) != expected:
        raise ValueError(f"expected {expected} bytes, got {len(data)}")
    return [signed_int16_le(data[index], data[index + 1]) / scale for index in range(0, expected, 2)]


def decode_euler(data: Sequence[int]) -> EulerAngles:
    heading, roll, pitch = _scaled_values(data, 3, 16.0)
    return EulerAngles(heading=heading, roll=roll, pitch=pitch)


def decode_quaternion(data: Sequence[int]) -> Quaternion:
    w, x, y, z = _scaled_values(data, 4, 16384.0)
    return Quaternion(w=w, x=x, y=y, z=z)


def decode_acceleration(data: Sequence[int]) -> Vector3:
    x, y, z = _scaled_values(data, 3, 100.0)
    return Vector3(x=x, y=y, z=z)


def decode_calibration(value: int) -> BNO055Calibration:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("calibration byte must be an integer")
    if not 0 <= value <= 0xFF:
        raise ValueError("calibration byte must be between 0 and 255")
    return BNO055Calibration(
        system=(value >> 6) & 0x03,
        gyroscope=(value >> 4) & 0x03,
        accelerometer=(value >> 2) & 0x03,
        magnetometer=value & 0x03,
    )
