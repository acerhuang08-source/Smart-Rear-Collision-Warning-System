"""Software-only BNO055 models, conversions, and injectable device layer."""

from .device import (
    BNO055CleanupError, BNO055Device, BNO055Error, BNO055IOError,
    BNO055IdentityError, BNO055ModeError, BNO055ReadinessTimeoutError,
    BNO055RuntimeStateError, BNO055SystemError, RegisterIO,
)
from .models import (
    BNO055Calibration, BNO055Identity, BNO055Measurement, EulerAngles,
    Quaternion, Vector3,
)
from .quality import (
    MeasurementQualityAssessment, MeasurementQualityThresholds,
    assess_measurement_quality,
)

__all__ = [
    "BNO055Calibration", "BNO055CleanupError", "BNO055Device", "BNO055Error",
    "BNO055IOError", "BNO055Identity", "BNO055IdentityError",
    "BNO055Measurement", "BNO055ModeError", "BNO055ReadinessTimeoutError",
    "BNO055RuntimeStateError", "BNO055SystemError",
    "EulerAngles", "MeasurementQualityAssessment", "MeasurementQualityThresholds",
    "Quaternion", "RegisterIO", "Vector3", "assess_measurement_quality",
]
