"""Tests for distance warning classification and validation."""

from __future__ import annotations

import math
from typing import cast

import pytest

from rear_warning.sensors.tfmini_plus import TFMiniPlusMeasurement
from rear_warning.warning import DistanceThresholds, WarningPolicy, WarningState


def _measurement(distance_cm: object) -> TFMiniPlusMeasurement:
    return TFMiniPlusMeasurement(
        distance_cm=cast(int, distance_cm),
        strength=100,
        chip_temperature_c=25.0,
    )


@pytest.mark.parametrize(
    ("distance_cm", "expected"),
    [
        (149, WarningState.DANGER),
        (150, WarningState.WARNING),
        (225, WarningState.WARNING),
        (300, WarningState.WARNING),
        (301, WarningState.SAFE),
    ],
)
def test_distance_boundaries(
    distance_cm: int, expected: WarningState
) -> None:
    assert WarningPolicy().classify(_measurement(distance_cm)) is expected


@pytest.mark.parametrize(
    "distance_cm",
    [0, -1, None, "150", True, math.nan, math.inf, 1201],
)
def test_invalid_distance_is_sensor_fault(distance_cm: object) -> None:
    assert (
        WarningPolicy().classify(_measurement(distance_cm))
        is WarningState.SENSOR_FAULT
    )


def test_missing_measurement_is_sensor_fault() -> None:
    assert WarningPolicy().classify(None) is WarningState.SENSOR_FAULT


def test_thresholds_can_be_overridden() -> None:
    policy = WarningPolicy(DistanceThresholds(2.0, 4.0, 15.0))

    assert policy.classify(_measurement(199)) is WarningState.DANGER
    assert policy.classify(_measurement(200)) is WarningState.WARNING
    assert policy.classify(_measurement(401)) is WarningState.SAFE


@pytest.mark.parametrize(
    "thresholds",
    [
        DistanceThresholds(1.5, 3.0, 12.0),
    ],
)
def test_valid_thresholds_are_accepted(thresholds: DistanceThresholds) -> None:
    assert WarningPolicy(thresholds).thresholds is thresholds


@pytest.mark.parametrize(
    "values",
    [(0.0, 3.0, 12.0), (3.1, 3.0, 12.0), (1.5, 3.0, 3.0)],
)
def test_invalid_threshold_order_is_rejected(
    values: tuple[float, float, float]
) -> None:
    with pytest.raises(ValueError):
        DistanceThresholds(*values)
