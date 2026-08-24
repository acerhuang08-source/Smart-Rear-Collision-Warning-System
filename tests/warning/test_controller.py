"""Integration tests from measurement model to LED driver boundary."""

from __future__ import annotations

from rear_warning.sensors.tfmini_plus import TFMiniPlusMeasurement
from rear_warning.warning import WarningPolicy, WarningState
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
