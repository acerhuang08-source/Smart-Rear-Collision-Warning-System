"""Hardware-free tests for the opt-in BNO055 vehicle-motion LED policy."""

from __future__ import annotations

import dataclasses

import pytest

from rear_warning.cli import ioradar701_led, ioradar701_service
from rear_warning.fusion import VehicleMotionSnapshot, VehicleMotionState
from rear_warning.live.ioradar701_shadow import IoRadar701ShadowCore, ShadowAssessment
from rear_warning.live.radar_led import (
    RadarLedAction,
    RadarLedObserver,
    VehicleMotionPolicy,
    decide_radar_led,
)
from rear_warning.outputs import LedPins
from rear_warning.sensors.bno055.models import (
    BNO055Calibration,
    BNO055Measurement,
    EulerAngles,
    Quaternion,
    Vector3,
)
from rear_warning.warning import WarningState

RELAX = VehicleMotionPolicy.RELAX_WHEN_STATIONARY


def _frame(kind: int, *, identifier: int = 0, count: int = 0, x_raw: int = 100) -> bytes:
    body = bytearray(11)
    body[0] = 0xA5
    body[1] = kind
    if kind == 1:
        body[3] = 0x05
        body[6] = count
    else:
        body[3] = identifier
        body[4:6] = x_raw.to_bytes(2, "big")
        body[6:8] = (1200).to_bytes(2, "big")
        body[8:10] = (6500).to_bytes(2, "big")
        body[10] = 80
    return bytes(body) + bytes((sum(body) & 0xFF,))


def _selected(core: IoRadar701ShadowCore, distance_cm: int, timestamp: float) -> ShadowAssessment:
    result, = core.feed_bytes(
        _frame(1, count=1) + _frame(2, identifier=1, x_raw=distance_cm),
        timestamp,
    ).assessments
    return result


def _with_state(assessment: ShadowAssessment, state: VehicleMotionState | None) -> ShadowAssessment:
    snapshot = None if state is None else VehicleMotionSnapshot(state, 0.05, 18, "test")
    return dataclasses.replace(assessment, vehicle_motion=snapshot)


def _measurement(stamp: float, x: float) -> BNO055Measurement:
    return BNO055Measurement(
        stamp, EulerAngles(0, 0, 0), Quaternion(1, 0, 0, 0),
        Vector3(x, 0.05, 0.08), Vector3(0, 0, 9.8), 25,
        BNO055Calibration(0, 0, 0, 0), 0x0C, 0x05, 0,
    )


class _Output:
    def __init__(self) -> None:
        self.states: dict[int, bool] = {}
        self.closed = False

    def write(self, pin: int, active: bool) -> None:
        self.states[pin] = active

    def close(self) -> None:
        self.closed = True


class _Writer:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def write_led(self, record: dict[str, object]) -> None:
        self.rows.append(record)


def test_default_policy_ignores_stationary_vehicle() -> None:
    core = IoRadar701ShadowCore()
    _selected(core, 400, 1.0)
    warning = _with_state(_selected(core, 300, 1.1), VehicleMotionState.STATIONARY)

    decision = decide_radar_led(warning)

    assert decision.state is WarningState.WARNING
    assert decision.vehicle_motion_relaxed is False


def test_relax_policy_shows_warning_as_safe_only_when_stationary() -> None:
    core = IoRadar701ShadowCore()
    _selected(core, 400, 1.0)
    warning = _selected(core, 300, 1.1)
    assert warning.warning_stabilized is WarningState.WARNING

    relaxed = decide_radar_led(_with_state(warning, VehicleMotionState.STATIONARY), RELAX)
    assert (relaxed.action, relaxed.state) == (RadarLedAction.DISPLAY, WarningState.SAFE)
    assert relaxed.vehicle_motion_relaxed is True
    assert "stationary" in relaxed.reason

    for state in (VehicleMotionState.MOVING, VehicleMotionState.UNKNOWN, None):
        kept = decide_radar_led(_with_state(warning, state), RELAX)
        assert kept.state is WarningState.WARNING
        assert kept.vehicle_motion_relaxed is False


def test_relax_policy_never_changes_danger_fault_or_no_targets() -> None:
    core = IoRadar701ShadowCore()
    _selected(core, 400, 1.0)
    danger = _with_state(_selected(core, 140, 1.1), VehicleMotionState.STATIONARY)
    assert decide_radar_led(danger, RELAX).state is WarningState.DANGER

    no_targets, = core.feed_bytes(_frame(1, count=0), 1.2).assessments
    no_targets = _with_state(no_targets, VehicleMotionState.STATIONARY)
    assert decide_radar_led(no_targets, RELAX).action is RadarLedAction.ALL_OFF

    fault = core.set_radar_error("simulated", 1.3)
    fault = _with_state(fault, VehicleMotionState.STATIONARY)
    assert decide_radar_led(fault, RELAX).state is WarningState.SENSOR_FAULT


def test_invalid_policy_type_is_rejected() -> None:
    core = IoRadar701ShadowCore()
    assessment = _selected(core, 300, 1.0)
    with pytest.raises(TypeError):
        decide_radar_led(assessment, "relax-when-stationary")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        RadarLedObserver(_Output(), LedPins.raspberry_pi_default(), "off")  # type: ignore[arg-type]


def test_observer_records_policy_state_and_relaxation() -> None:
    core = IoRadar701ShadowCore()
    output = _Output()
    writer = _Writer()
    observer = RadarLedObserver(output, LedPins.raspberry_pi_default(), RELAX)
    observer.start(writer, 0.0)
    _selected(core, 400, 1.0)
    observer.observe(
        _with_state(_selected(core, 300, 1.1), VehicleMotionState.STATIONARY), writer
    )

    row = writer.rows[-1]
    assert row["vehicle_motion_policy"] == "relax-when-stationary"
    assert row["vehicle_motion_state"] == "stationary"
    assert row["vehicle_motion_relaxed"] is True
    assert row["warning_stabilized"] == "warning"
    assert row["gpio_pattern"] == {
        "red_bcm17": False, "yellow_bcm27": False, "green_bcm22": True,
    }


def test_core_attaches_vehicle_motion_from_bno055_samples() -> None:
    core = IoRadar701ShadowCore()
    stamp = 0.0
    for index in range(120):
        stamp = index * 0.11
        core.update_motion(_measurement(stamp, 0.10 + 0.005 * (index % 2)),
                           captured_at_monotonic_seconds=stamp)

    assessment = _selected(core, 300, stamp + 0.05)

    assert assessment.vehicle_motion is not None
    assert assessment.vehicle_motion.state is VehicleMotionState.STATIONARY
    assert assessment.as_dict()["vehicle_motion"]["state"] == "stationary"  # type: ignore[index]


def test_core_without_motion_reports_unknown() -> None:
    core = IoRadar701ShadowCore()
    assessment = _selected(core, 300, 1.0)
    assert assessment.vehicle_motion is not None
    assert assessment.vehicle_motion.state is VehicleMotionState.UNKNOWN


@pytest.mark.parametrize("module", [ioradar701_led, ioradar701_service])
def test_cli_policy_defaults_off_and_accepts_relax(module: object) -> None:
    build = getattr(module, "_build_parser")
    assert build().parse_args([]).vehicle_motion_policy == "off"
    parsed = build().parse_args(["--vehicle-motion-policy", "relax-when-stationary"])
    assert parsed.vehicle_motion_policy == "relax-when-stationary"
    with pytest.raises(SystemExit):
        build().parse_args(["--vehicle-motion-policy", "always"])
