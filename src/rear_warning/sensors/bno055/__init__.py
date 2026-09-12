"""Software-only BNO055 models, conversions, and injectable device layer."""

from .calibration_profile import (
    BNO055CalibrationData, BNO055CalibrationProfile,
    BNO055CalibrationProfileError, decode_calibration_profile_bytes,
    deserialize_calibration_profile, encode_calibration_profile_bytes,
    serialize_calibration_profile, utc_timestamp, validate_created_at_utc,
    validate_sensor_label,
)
from .device import (
    BNO055CalibrationTimeoutError, BNO055CleanupError, BNO055Device,
    BNO055Error, BNO055IOError, BNO055IdentityError,
    BNO055MeasurementQualityTimeoutError, BNO055ModeError,
    BNO055ProfileCompatibilityError, BNO055ProfileReadbackError,
    BNO055ReadinessTimeoutError, BNO055RuntimeStateError, BNO055SystemError,
    RegisterIO,
)
from .models import (
    BNO055Calibration, BNO055Identity, BNO055Measurement, EulerAngles,
    Quaternion, Vector3,
)
from .quality import (
    MeasurementQualityAssessment, MeasurementQualityThresholds,
    assess_measurement_quality,
)
from .profile_storage import (
    BNO055ProfileStorageError, BNO055ProfileStore, describe_exception,
)

__all__ = [
    "BNO055Calibration", "BNO055CalibrationData", "BNO055CalibrationProfile",
    "BNO055CalibrationProfileError", "BNO055CalibrationTimeoutError",
    "BNO055CleanupError", "BNO055Device", "BNO055Error", "BNO055IOError",
    "BNO055Identity", "BNO055IdentityError", "BNO055Measurement",
    "BNO055MeasurementQualityTimeoutError", "BNO055ModeError",
    "BNO055ProfileCompatibilityError", "BNO055ProfileReadbackError",
    "BNO055ProfileStorageError", "BNO055ProfileStore",
    "BNO055ReadinessTimeoutError", "BNO055RuntimeStateError",
    "BNO055SystemError", "EulerAngles", "MeasurementQualityAssessment",
    "MeasurementQualityThresholds", "Quaternion", "RegisterIO", "Vector3",
    "assess_measurement_quality", "decode_calibration_profile_bytes",
    "describe_exception", "deserialize_calibration_profile",
    "encode_calibration_profile_bytes",
    "serialize_calibration_profile", "utc_timestamp", "validate_sensor_label",
    "validate_created_at_utc",
]
