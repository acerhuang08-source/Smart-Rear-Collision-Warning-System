"""Shared semantic validation for replayable synchronized CSV schema v2."""

from __future__ import annotations

import math

from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.sensors.bno055.quality import assess_measurement_quality
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement

from .models import (
    SensorSyncStatus, SynchronizedSensorSample, is_valid_distance_measurement,
)


class SynchronizedSampleCsvContractError(ValueError):
    """A synchronized sample cannot be represented by replayable schema v2."""


def _require_integer(
    name: str,
    value: object,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SynchronizedSampleCsvContractError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise SynchronizedSampleCsvContractError(
            f"{name} must be at least {minimum}"
        )
    if maximum is not None and value > maximum:
        raise SynchronizedSampleCsvContractError(
            f"{name} must be at most {maximum}"
        )
    return value


def _require_finite(
    name: str, value: object, *, non_negative: bool = False
) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise SynchronizedSampleCsvContractError(
            f"{name} must be a finite number"
        )
    result = float(value)
    if non_negative and result < 0:
        raise SynchronizedSampleCsvContractError(
            f"{name} must be non-negative"
        )
    return result


def _validate_distance(measurement: object) -> TFMiniPlusMeasurement:
    if not isinstance(measurement, TFMiniPlusMeasurement):
        raise SynchronizedSampleCsvContractError(
            "distance_measurement must be a TFMiniPlusMeasurement"
        )
    _require_integer("distance_cm", measurement.distance_cm)
    _require_integer("signal_strength", measurement.strength)
    _require_finite("tfmini_temperature_c", measurement.chip_temperature_c)
    return measurement


def _validate_motion(measurement: object) -> BNO055Measurement:
    if not isinstance(measurement, BNO055Measurement):
        raise SynchronizedSampleCsvContractError(
            "motion_measurement must be a BNO055Measurement"
        )
    if not isinstance(measurement.euler, EulerAngles):
        raise SynchronizedSampleCsvContractError("euler must be EulerAngles")
    if not isinstance(measurement.quaternion, Quaternion):
        raise SynchronizedSampleCsvContractError("quaternion must be Quaternion")
    if not isinstance(measurement.linear_acceleration, Vector3):
        raise SynchronizedSampleCsvContractError(
            "linear_acceleration must be Vector3"
        )
    if not isinstance(measurement.gravity, Vector3):
        raise SynchronizedSampleCsvContractError("gravity must be Vector3")
    if not isinstance(measurement.calibration, BNO055Calibration):
        raise SynchronizedSampleCsvContractError(
            "calibration must be BNO055Calibration"
        )

    _require_finite(
        "bno055_measurement_timestamp",
        measurement.monotonic_timestamp,
        non_negative=True,
    )
    for name, value in (
        ("euler_heading", measurement.euler.heading),
        ("euler_roll", measurement.euler.roll),
        ("euler_pitch", measurement.euler.pitch),
        ("quaternion_w", measurement.quaternion.w),
        ("quaternion_x", measurement.quaternion.x),
        ("quaternion_y", measurement.quaternion.y),
        ("quaternion_z", measurement.quaternion.z),
        ("linear_acceleration_x", measurement.linear_acceleration.x),
        ("linear_acceleration_y", measurement.linear_acceleration.y),
        ("linear_acceleration_z", measurement.linear_acceleration.z),
        ("gravity_x", measurement.gravity.x),
        ("gravity_y", measurement.gravity.y),
        ("gravity_z", measurement.gravity.z),
    ):
        _require_finite(name, value)
    _require_integer(
        "bno055_temperature_c", measurement.temperature_c,
        minimum=-128, maximum=127,
    )
    for name, value in (
        ("calibration_system", measurement.calibration.system),
        ("calibration_gyroscope", measurement.calibration.gyroscope),
        ("calibration_accelerometer", measurement.calibration.accelerometer),
        ("calibration_magnetometer", measurement.calibration.magnetometer),
    ):
        _require_integer(name, value, minimum=0, maximum=3)
    _require_integer(
        "operation_mode", measurement.operation_mode, minimum=0, maximum=0xFF
    )
    _require_integer(
        "system_status", measurement.system_status, minimum=0, maximum=0xFF
    )
    _require_integer(
        "system_error", measurement.system_error, minimum=0, maximum=0xFF
    )
    return measurement


def validate_synchronized_sample_csv_v2_contract(
    sample: object,
) -> SynchronizedSensorSample:
    """Validate the single writer/reader semantic contract for one v2 row."""

    if not isinstance(sample, SynchronizedSensorSample):
        raise SynchronizedSampleCsvContractError(
            "sample must be a SynchronizedSensorSample"
        )
    if not isinstance(sample.status, SensorSyncStatus):
        raise SynchronizedSampleCsvContractError(
            "status must be a SensorSyncStatus"
        )

    distance = _validate_distance(sample.distance_measurement)
    _require_finite(
        "distance_timestamp", sample.distance_timestamp, non_negative=True
    )
    distance_is_valid = is_valid_distance_measurement(distance)
    if sample.status is SensorSyncStatus.INVALID_DISTANCE_SAMPLE:
        if distance_is_valid:
            raise SynchronizedSampleCsvContractError(
                "invalid_distance_sample requires a distance measurement rejected "
                "by the synchronization validity predicate"
            )
    elif not distance_is_valid:
        raise SynchronizedSampleCsvContractError(
            f"{sample.status.value} requires a valid distance measurement"
        )

    statuses_requiring_motion = {
        SensorSyncStatus.MATCHED,
        SensorSyncStatus.STALE_MOTION_SAMPLE,
        SensorSyncStatus.FUTURE_MOTION_SAMPLE,
        SensorSyncStatus.INVALID_MOTION_SAMPLE,
    }
    motion_is_required = sample.status in statuses_requiring_motion
    motion_is_present = sample.motion_measurement is not None
    timestamp_is_present = sample.motion_timestamp is not None
    if motion_is_present != timestamp_is_present:
        raise SynchronizedSampleCsvContractError(
            "motion measurement and canonical motion timestamp must both be "
            "present or both be absent"
        )
    if motion_is_required and not motion_is_present:
        raise SynchronizedSampleCsvContractError(
            f"{sample.status.value} requires a complete motion payload"
        )
    if not motion_is_required and motion_is_present:
        raise SynchronizedSampleCsvContractError(
            f"{sample.status.value} must not contain a motion payload"
        )

    if sample.status is SensorSyncStatus.MATCHED:
        if sample.motion_age_seconds is None:
            raise SynchronizedSampleCsvContractError(
                "matched requires motion_age_seconds"
            )
        assert sample.motion_timestamp is not None
        expected_age = sample.distance_timestamp - sample.motion_timestamp
        if expected_age < 0 or not math.isclose(
            sample.motion_age_seconds,
            expected_age,
            rel_tol=0.0,
            abs_tol=1e-12,
        ):
            raise SynchronizedSampleCsvContractError(
                "matched motion_age_seconds must equal distance_timestamp "
                "minus canonical bno055_timestamp"
            )
    elif sample.motion_age_seconds is not None:
        raise SynchronizedSampleCsvContractError(
            "unmatched status must not contain motion_age_seconds"
        )

    if sample.motion_measurement is None:
        return sample

    motion = _validate_motion(sample.motion_measurement)
    canonical_timestamp = _require_finite(
        "canonical bno055_timestamp", sample.motion_timestamp, non_negative=True
    )
    raw_timestamp = float(motion.monotonic_timestamp)
    if raw_timestamp > canonical_timestamp:
        raise SynchronizedSampleCsvContractError(
            "bno055_measurement_timestamp must be less than or equal to "
            "canonical bno055_timestamp"
        )

    quality = assess_measurement_quality(motion)
    if sample.status is SensorSyncStatus.INVALID_MOTION_SAMPLE:
        if quality.is_valid:
            raise SynchronizedSampleCsvContractError(
                "invalid_motion_sample requires a quality-invalid motion measurement"
            )
    elif not quality.is_valid:
        raise SynchronizedSampleCsvContractError(
            f"{sample.status.value} requires a quality-valid motion measurement: "
            + "; ".join(quality.state_issues + quality.data_issues)
        )

    if (
        sample.status is SensorSyncStatus.FUTURE_MOTION_SAMPLE
        and canonical_timestamp <= sample.distance_timestamp
    ):
        raise SynchronizedSampleCsvContractError(
            "future_motion_sample requires canonical bno055_timestamp later "
            "than distance_timestamp"
        )
    if (
        sample.status is SensorSyncStatus.STALE_MOTION_SAMPLE
        and canonical_timestamp > sample.distance_timestamp
    ):
        raise SynchronizedSampleCsvContractError(
            "stale_motion_sample cannot contain a future canonical "
            "bno055_timestamp"
        )
    return sample


__all__ = [
    "SynchronizedSampleCsvContractError",
    "validate_synchronized_sample_csv_v2_contract",
]
