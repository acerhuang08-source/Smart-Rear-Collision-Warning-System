from __future__ import annotations

import math

import pytest

from rear_warning.sensors.bno055 import (
    BNO055Calibration, BNO055Measurement, EulerAngles,
    MeasurementQualityThresholds, Quaternion, Vector3,
    assess_measurement_quality,
)


def measurement(
    *,
    quaternion: Quaternion = Quaternion(1.0, 0.0, 0.0, 0.0),
    gravity: Vector3 = Vector3(0.0, 0.0, 9.8),
    euler: EulerAngles = EulerAngles(0.0, 0.0, 0.0),
    linear: Vector3 = Vector3(0.0, 0.0, 0.0),
    calibration: BNO055Calibration = BNO055Calibration(0, 0, 0, 0),
    mode: int = 0x0C,
    status: int = 0x05,
    error: int = 0x00,
) -> BNO055Measurement:
    return BNO055Measurement(
        1.0, euler, quaternion, linear, gravity, 24, calibration,
        mode, status, error,
    )


def test_zero_euler_linear_acceleration_and_calibration_are_valid() -> None:
    assert assess_measurement_quality(measurement()).is_valid


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quaternion", Quaternion(0.0, 0.0, 0.0, 0.0)),
        ("quaternion", Quaternion(0.49, 0.0, 0.0, 0.0)),
        ("quaternion", Quaternion(1.51, 0.0, 0.0, 0.0)),
        ("gravity", Vector3(0.0, 0.0, 4.9)),
        ("gravity", Vector3(0.0, 0.0, 15.1)),
    ],
)
def test_norm_outside_threshold_is_invalid(field: str, value: object) -> None:
    assessment = assess_measurement_quality(measurement(**{field: value}))  # type: ignore[arg-type]
    assert not assessment.is_valid
    assert assessment.data_issues


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_non_finite_measurement_value_is_invalid(value: float) -> None:
    assessment = assess_measurement_quality(
        measurement(euler=EulerAngles(value, 0.0, 0.0))
    )
    assert not assessment.is_valid
    assert "euler.heading is not finite" in assessment.data_issues


@pytest.mark.parametrize(
    ("mode", "status", "error"),
    [(0x00, 0x05, 0x00), (0x0C, 0x04, 0x00), (0x0C, 0x05, 0x07)],
)
def test_fusion_state_is_part_of_quality_assessment(
    mode: int, status: int, error: int
) -> None:
    assessment = assess_measurement_quality(
        measurement(mode=mode, status=status, error=error)
    )
    assert assessment.state_issues


@pytest.mark.parametrize(
    ("name", "value", "error_type"),
    [
        ("quaternion_norm_min", True, TypeError),
        ("quaternion_norm_min", "0.5", TypeError),
        ("quaternion_norm_min", math.nan, ValueError),
        ("quaternion_norm_min", math.inf, ValueError),
        ("quaternion_norm_min", -math.inf, ValueError),
        ("quaternion_norm_min", 0.0, ValueError),
        ("quaternion_norm_min", -1.0, ValueError),
        ("gravity_norm_max_m_s2", 0.0, ValueError),
    ],
)
def test_thresholds_reject_invalid_scalars(
    name: str, value: object, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type):
        MeasurementQualityThresholds(**{name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "values",
    [
        {"quaternion_norm_min": 1.0, "quaternion_norm_max": 1.0},
        {"quaternion_norm_min": 2.0, "quaternion_norm_max": 1.0},
        {"gravity_norm_min_m_s2": 10.0, "gravity_norm_max_m_s2": 10.0},
        {"gravity_norm_min_m_s2": 11.0, "gravity_norm_max_m_s2": 10.0},
    ],
)
def test_thresholds_require_strictly_ordered_ranges(values: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        MeasurementQualityThresholds(**values)


def test_thresholds_are_configurable() -> None:
    thresholds = MeasurementQualityThresholds(
        quaternion_norm_min=0.9,
        quaternion_norm_max=1.1,
        gravity_norm_min_m_s2=9.0,
        gravity_norm_max_m_s2=10.0,
    )
    assert assess_measurement_quality(measurement(), thresholds).is_valid
