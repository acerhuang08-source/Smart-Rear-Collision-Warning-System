"""Hardware-free tests for ioRadar701 distance-warning LED integration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from rear_warning.cli import ioradar701_shadow
from rear_warning.live.ioradar701_shadow import IoRadar701ShadowCore
from rear_warning.live.ioradar701_shadow import ShadowAssessment
from rear_warning.live.radar_led import (
    RadarLedAction,
    RadarLedObserver,
    decide_radar_led,
)
from rear_warning.outputs import LedPins


def _frame(
    kind: int,
    *,
    identifier: int = 0,
    count: int = 0,
    x_raw: int = 100,
) -> bytes:
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


def _selected(
    core: IoRadar701ShadowCore,
    distance_cm: int,
    timestamp: float,
    *,
    identifier: int = 1,
) -> ShadowAssessment:
    result, = core.feed_bytes(
        _frame(1, count=1)
        + _frame(2, identifier=identifier, x_raw=distance_cm),
        timestamp,
    ).assessments
    return result


@dataclass
class _Device:
    pin: int
    active: bool = False


class _Output:
    def __init__(self) -> None:
        self.devices = {pin: _Device(pin) for pin in (17, 27, 22)}
        self.closed = False
        self.writes: list[tuple[int, bool]] = []

    def write(self, pin: int, active: bool) -> None:
        if self.closed:
            raise RuntimeError("closed")
        self.devices[pin].active = active
        self.writes.append((pin, active))

    def close(self) -> None:
        for device in self.devices.values():
            device.active = False
        self.closed = True

    @property
    def pattern(self) -> tuple[bool, bool, bool]:
        return tuple(self.devices[pin].active for pin in (17, 27, 22))  # type: ignore[return-value]


class _Writer:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def write_led(self, record: dict[str, object]) -> None:
        self.rows.append(record)


def test_fresh_selected_distance_reuses_core_stabilized_result() -> None:
    core = IoRadar701ShadowCore()

    safe = decide_radar_led(_selected(core, 400, 1.0))
    warning = decide_radar_led(_selected(core, 300, 1.1))
    danger = decide_radar_led(_selected(core, 149, 1.2))

    assert (safe.action.value, safe.state.value) == ("display", "safe")
    assert (warning.action.value, warning.state.value) == ("display", "warning")
    assert (danger.action.value, danger.state.value) == ("display", "danger")


def test_hysteresis_boundaries_are_the_existing_core_boundaries() -> None:
    core = IoRadar701ShadowCore()

    states = [
        decide_radar_led(_selected(core, distance, 1.0 + index / 10)).state.value
        for index, distance in enumerate((149, 150, 169, 170, 320, 321))
    ]

    assert states == [
        "danger",
        "danger",
        "danger",
        "warning",
        "warning",
        "safe",
    ]


def test_normal_zero_targets_clears_green_without_fault_pattern() -> None:
    core = IoRadar701ShadowCore()
    output = _Output()
    writer = _Writer()
    observer = RadarLedObserver(output, LedPins.raspberry_pi_default())
    observer.start(writer, 0.0)
    observer.observe(_selected(core, 400, 1.0), writer)
    assert output.pattern == (False, False, True)

    no_targets, = core.feed_bytes(_frame(1, count=0), 1.1).assessments
    assert decide_radar_led(no_targets).action is RadarLedAction.ALL_OFF
    observer.observe(no_targets, writer)

    assert output.pattern == (False, False, False)
    assert writer.rows[-1]["availability"] == "no_targets"


@pytest.mark.parametrize("failure_kind", ["stale", "incomplete", "garbage_stale"])
def test_invalid_or_expired_radar_is_red_yellow_sensor_fault(
    failure_kind: str,
) -> None:
    core = IoRadar701ShadowCore(stale_after_seconds=0.5)
    output = _Output()
    writer = _Writer()
    observer = RadarLedObserver(output, LedPins.raspberry_pi_default())
    observer.start(writer, 0.0)
    observer.observe(_selected(core, 400, 1.0), writer)

    if failure_kind == "incomplete":
        core.feed_bytes(_frame(1, count=2), 1.1)
        failed, _empty = core.feed_bytes(_frame(1, count=0), 1.2).assessments
    else:
        if failure_kind == "garbage_stale":
            core.feed_bytes(b"garbage", 1.4)
        failed = core.tick(1.6)
        assert failed is not None
    observer.observe(failed, writer)

    assert output.pattern == (True, True, False)
    assert writer.rows[-1]["warning_stabilized"] is None


def test_target_switch_resets_old_state_and_applies_new_target() -> None:
    core = IoRadar701ShadowCore()
    output = _Output()
    writer = _Writer()
    observer = RadarLedObserver(output, LedPins.raspberry_pi_default())
    observer.start(writer, 0.0)
    observer.observe(_selected(core, 400, 1.0, identifier=1), writer)
    switched, = core.feed_bytes(
        _frame(1, count=2)
        + _frame(2, identifier=1, x_raw=400)
        + _frame(2, identifier=2, x_raw=100),
        1.1,
    ).assessments
    observer.observe(switched, writer)

    assert switched.reset_reason == "target switch 1->2"
    assert switched.selection is not None
    assert switched.selection.target_changed
    assert switched.selection.reason == (
        "nearer target ID 2 replaced previous target ID 1"
    )
    assert switched.warning_candidate is not None
    assert switched.warning_candidate.value == "danger"
    assert switched.warning_stabilized is not None
    assert switched.warning_stabilized.value == "danger"
    assert output.pattern == (True, False, False)
    assert writer.rows[-1]["selected_target_id"] == 2
    assert writer.rows[-1]["warning_stabilized"] == "danger"


def test_out_of_valid_range_uses_existing_sensor_fault_pattern() -> None:
    core = IoRadar701ShadowCore()
    output = _Output()
    writer = _Writer()
    observer = RadarLedObserver(output, LedPins.raspberry_pi_default())
    observer.start(writer, 0.0)
    observer.observe(_selected(core, 1201, 1.0), writer)

    assert output.pattern == (True, True, False)
    assert writer.rows[-1]["warning_stabilized"] == "sensor_fault"


def test_cleanup_turns_everything_off_closes_and_records() -> None:
    core = IoRadar701ShadowCore()
    output = _Output()
    writer = _Writer()
    observer = RadarLedObserver(output, LedPins.raspberry_pi_default())
    observer.start(writer, 0.0)
    observer.observe(_selected(core, 100, 1.0), writer)

    observer.finish(writer, 2.0)

    assert output.closed
    assert output.pattern == (False, False, False)
    assert observer.closed
    assert writer.rows[-1]["kind"] == "cleanup"
    assert writer.rows[-1]["gpio_pattern"] == {
        "red_bcm17": False,
        "yellow_bcm27": False,
        "green_bcm22": False,
    }
    assert observer.cleanup_status() == {
        "finish_attempted": True,
        "all_off_succeeded": True,
        "gpio_close_succeeded": True,
    }


def test_integrated_capture_truthfully_marks_gpio_and_terminal_mode(
    tmp_path: Path,
) -> None:
    assessment = _selected(IoRadar701ShadowCore(), 400, 1.0)
    capture = ioradar701_shadow._Capture(
        tmp_path,
        raw=False,
        gpio_enabled=True,
    )

    capture.write_assessment(assessment)

    row = json.loads(
        (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").strip()
    )
    assert row["gpio_enabled"] is True
    assert row["shadow"] is False
    assert (tmp_path / "led.jsonl").exists()
    line = ioradar701_shadow._format(
        assessment,
        elapsed=1.0,
        gpio_enabled=True,
    )
    assert "RADAR_LED" in line
    assert "GPIO=enabled" in line
    assert "distance_warning_stabilized=safe" in line
    assert capture.close() == []


def test_shadow_capture_defaults_remain_gpio_free(tmp_path: Path) -> None:
    assessment = _selected(IoRadar701ShadowCore(), 400, 1.0)
    capture = ioradar701_shadow._Capture(tmp_path, raw=False)

    capture.write_assessment(assessment)

    row = json.loads(
        (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").strip()
    )
    assert row["gpio_enabled"] is False
    assert row["shadow"] is True
    assert not (tmp_path / "led.jsonl").exists()
    assert capture.close() == []
