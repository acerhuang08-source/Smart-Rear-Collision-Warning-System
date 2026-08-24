"""Hardware-free tests for the three-colour LED driver."""

from __future__ import annotations

import pytest

from rear_warning.outputs import LedPins, ThreeColorLedDriver
from rear_warning.warning import WarningState


class _FakeDigitalOutput:
    def __init__(self) -> None:
        self.states: dict[int, bool] = {}

    def write(self, pin: int, active: bool) -> None:
        self.states[pin] = active


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (WarningState.SAFE, {17: False, 27: False, 22: True}),
        (WarningState.WARNING, {17: False, 27: True, 22: False}),
        (WarningState.DANGER, {17: True, 27: False, 22: False}),
        (WarningState.SENSOR_FAULT, {17: True, 27: True, 22: False}),
    ],
)
def test_state_is_mapped_to_expected_outputs(
    state: WarningState, expected: dict[int, bool]
) -> None:
    output = _FakeDigitalOutput()
    driver = ThreeColorLedDriver(output, LedPins(red=17, yellow=27, green=22))

    driver.display(state)

    assert output.states == expected


def test_duplicate_pins_are_rejected() -> None:
    with pytest.raises(ValueError, match="distinct"):
        LedPins(red=17, yellow=17, green=22)


def test_project_default_pins_are_centralized() -> None:
    assert LedPins.raspberry_pi_default() == LedPins(
        red=17, yellow=27, green=22
    )
