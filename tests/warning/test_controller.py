"""Integration tests from measurement model to LED driver boundary."""

from __future__ import annotations

import math

import pytest

from rear_warning.sensors.tfmini_plus import TFMiniPlusMeasurement
from rear_warning.warning import (
    DistanceThresholds,
    HysteresisThresholds,
    WarningPolicy,
    WarningState,
    WarningStateStabilizer,
)
from rear_warning.warning.controller import WarningController


class _FakeLedDriver:
    def __init__(self) -> None:
        self.states: list[WarningState] = []

    def display(self, state: WarningState) -> None:
        self.states.append(state)


def test_measurement_is_classified_and_sent_to_led_driver() -> None:
    driver = _FakeLedDriver()
    controller = WarningController(WarningPolicy(), driver)
    measurement = TFMiniPlusMeasurement(149, 100, 25.0)

    result = controller.process(measurement)

    assert result is WarningState.DANGER
    assert driver.states == [WarningState.DANGER]


def test_missing_measurement_displays_sensor_fault() -> None:
    driver = _FakeLedDriver()
    controller = WarningController(WarningPolicy(), driver)

    assert controller.process(None) is WarningState.SENSOR_FAULT
    assert driver.states == [WarningState.SENSOR_FAULT]


def test_controller_displays_stabilized_states() -> None:
    driver = _FakeLedDriver()
    controller = WarningController(WarningPolicy(), driver)

    states = [
        controller.process(TFMiniPlusMeasurement(distance, 100, 25.0))
        for distance in (301, 300, 301, 320, 321)
    ]

    assert states == [
        WarningState.SAFE,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.SAFE,
    ]
    assert driver.states == states


def test_controller_uses_compatible_custom_thresholds() -> None:
    entry = DistanceThresholds(2.0, 4.0, 15.0)
    policy = WarningPolicy(entry)
    stabilizer = WarningStateStabilizer(
        entry, HysteresisThresholds(2.2, 4.2)
    )
    driver = _FakeLedDriver()
    controller = WarningController(policy, driver, stabilizer)

    states = [
        controller.process(TFMiniPlusMeasurement(distance, 100, 25.0))
        for distance in (401, 400, 410, 421, 199, 219, 220)
    ]

    assert states == [
        WarningState.SAFE,
        WarningState.WARNING,
        WarningState.WARNING,
        WarningState.SAFE,
        WarningState.DANGER,
        WarningState.DANGER,
        WarningState.WARNING,
    ]
    assert driver.states == states


def test_controller_rejects_stabilizer_with_different_entry_thresholds() -> None:
    driver = _FakeLedDriver()
    policy = WarningPolicy(DistanceThresholds(2.0, 4.0, 15.0))
    stabilizer = WarningStateStabilizer(DistanceThresholds())

    with pytest.raises(ValueError, match="must match policy thresholds"):
        WarningController(policy, driver, stabilizer)

    assert driver.states == []


def test_controllers_do_not_share_stabilizer_state() -> None:
    thresholds = DistanceThresholds()
    first_driver = _FakeLedDriver()
    second_driver = _FakeLedDriver()
    first = WarningController(WarningPolicy(thresholds), first_driver)
    second = WarningController(WarningPolicy(thresholds), second_driver)

    assert (
        first.process(TFMiniPlusMeasurement(100, 100, 25.0))
        is WarningState.DANGER
    )
    assert (
        second.process(TFMiniPlusMeasurement(301, 100, 25.0))
        is WarningState.SAFE
    )
    assert first_driver.states == [WarningState.DANGER]
    assert second_driver.states == [WarningState.SAFE]


@pytest.mark.parametrize("initial_distance_cm", [400, 240, 120])
@pytest.mark.parametrize(
    "invalid_distance_cm",
    [None, math.nan, math.inf, 0, -1, 1201],
)
def test_invalid_measurement_enters_sensor_fault_through_controller(
    initial_distance_cm: int,
    invalid_distance_cm: object,
) -> None:
    driver = _FakeLedDriver()
    controller = WarningController(WarningPolicy(), driver)
    controller.process(TFMiniPlusMeasurement(initial_distance_cm, 100, 25.0))
    measurement = (
        None
        if invalid_distance_cm is None
        else TFMiniPlusMeasurement(invalid_distance_cm, 100, 25.0)
    )

    state = controller.process(measurement)

    assert state is WarningState.SENSOR_FAULT
    assert driver.states[-1] is WarningState.SENSOR_FAULT


@pytest.mark.parametrize(
    ("recovery_distance_cm", "expected"),
    [
        (400, WarningState.SAFE),
        (240, WarningState.WARNING),
        (120, WarningState.DANGER),
    ],
)
def test_controller_fault_recovery_uses_first_valid_policy_state(
    recovery_distance_cm: int,
    expected: WarningState,
) -> None:
    driver = _FakeLedDriver()
    controller = WarningController(WarningPolicy(), driver)
    controller.process(TFMiniPlusMeasurement(120, 100, 25.0))
    controller.process(None)

    state = controller.process(
        TFMiniPlusMeasurement(recovery_distance_cm, 100, 25.0)
    )

    assert state is expected
    assert driver.states[-1] is expected
