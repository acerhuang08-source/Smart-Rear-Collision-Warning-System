"""Named schema definitions for replayable synchronized-sample CSV files."""

from __future__ import annotations


SYNCHRONIZED_SAMPLE_CSV_SCHEMA_VERSION = 2

# This is the coordinator's read-completed timestamp used for association.
BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD = "bno055_timestamp"

# This is captured by BNO055Device immediately before its measurement block read.
BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD = "bno055_measurement_timestamp"

SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS = (
    "sequence",
    "distance_timestamp",
    BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD,
    BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD,
    "motion_age_seconds",
    "sync_status",
    "distance_cm",
    "signal_strength",
    "tfmini_temperature_c",
    "euler_heading",
    "euler_roll",
    "euler_pitch",
    "quaternion_w",
    "quaternion_x",
    "quaternion_y",
    "quaternion_z",
    "linear_acceleration_x",
    "linear_acceleration_y",
    "linear_acceleration_z",
    "gravity_x",
    "gravity_y",
    "gravity_z",
    "bno055_temperature_c",
    "calibration_system",
    "calibration_gyroscope",
    "calibration_accelerometer",
    "calibration_magnetometer",
    "operation_mode",
    "system_status",
    "system_error",
)

BNO055_CSV_V2_FIELDS = (
    BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD,
    BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD,
    "euler_heading",
    "euler_roll",
    "euler_pitch",
    "quaternion_w",
    "quaternion_x",
    "quaternion_y",
    "quaternion_z",
    "linear_acceleration_x",
    "linear_acceleration_y",
    "linear_acceleration_z",
    "gravity_x",
    "gravity_y",
    "gravity_z",
    "bno055_temperature_c",
    "calibration_system",
    "calibration_gyroscope",
    "calibration_accelerometer",
    "calibration_magnetometer",
    "operation_mode",
    "system_status",
    "system_error",
)

__all__ = [
    "BNO055_CANONICAL_MOTION_TIMESTAMP_FIELD",
    "BNO055_CSV_V2_FIELDS",
    "BNO055_RAW_MEASUREMENT_TIMESTAMP_FIELD",
    "SYNCHRONIZED_SAMPLE_CSV_SCHEMA_VERSION",
    "SYNCHRONIZED_SAMPLE_CSV_V2_FIELDS",
]
