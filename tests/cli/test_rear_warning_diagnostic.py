"""Hardware-free tests for the integrated rear-warning diagnostic CLI."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import pytest

from rear_warning.cli import rear_warning_diagnostic as diagnostic
from rear_warning.outputs import LedPins
from rear_warning.sensors.tfmini_plus import (
    TFMiniPlusMeasurement,
    TFMiniPlusSerialReadError,
)


class _Clock:
    def __init__(self, values: list[float]) -> None:
        self.values = deque(values)
        self.last = values[-1]

    def __call__(self) -> float:
        if self.values:
            self.last = self.values.popleft()
        return self.last


class _FakeOutput:
    def __init__(
        self,
        *,
        write_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.writes: list[tuple[int, bool]] = []
        self.close_calls = 0
        self.write_error = write_error
        self.close_error = close_error

    def write(self, pin: int, active: bool) -> None:
        if self.write_error is not None:
            raise self.write_error
        self.writes.append((pin, active))

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _OutputFactory:
    def __init__(self, output: _FakeOutput) -> None:
        self.output = output
        self.calls: list[tuple[LedPins, int]] = []

    def __call__(self, pins: LedPins, *, chip: int) -> _FakeOutput:
        self.calls.append((pins, chip))
        return self.output


class _FakeDevice:
    def __init__(
        self,
        events: list[object],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.events = deque(events)
        self.open_calls = 0
        self.close_calls = 0
        self.close_error = close_error

    def open(self) -> None:
        self.open_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        event = self.events.popleft() if self.events else []
        if isinstance(event, BaseException):
            raise event
        assert isinstance(event, list)
        return event


class _DeviceFactory:
    def __init__(self, device: _FakeDevice) -> None:
        self.device = device
        self.calls: list[dict[str, object]] = []

    def __call__(
        self, *, port: str, baudrate: int, timeout: float | None
    ) -> _FakeDevice:
        self.calls.append(
            {"port": port, "baudrate": baudrate, "timeout": timeout}
        )
        return self.device


def _install(
    monkeypatch: pytest.MonkeyPatch,
    events: list[object],
    *,
    clock: list[float] | None = None,
    output: _FakeOutput | None = None,
    device_close_error: Exception | None = None,
) -> tuple[_FakeOutput, _OutputFactory, _FakeDevice, _DeviceFactory]:
    fake_output = output or _FakeOutput()
    output_factory = _OutputFactory(fake_output)
    device = _FakeDevice(events, close_error=device_close_error)
    device_factory = _DeviceFactory(device)
    monkeypatch.setattr(diagnostic, "_OUTPUT_FACTORY", output_factory)
    monkeypatch.setattr(diagnostic, "_DEVICE_FACTORY", device_factory)
    monkeypatch.setattr(
        diagnostic, "_MONOTONIC", _Clock(clock or [0.0, 1.0])
    )
    monkeypatch.setattr(
        diagnostic,
        "_WALL_CLOCK",
        lambda: datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc),
    )
    return fake_output, output_factory, device, device_factory


def _confirmed(*extra: str) -> list[str]:
    return ["--max-samples", "1", "--confirm-hardware", *extra]


def test_unconfirmed_prints_configuration_without_creating_hardware(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, output_factory, device, device_factory = _install(monkeypatch, [])

    result = diagnostic.main(["--duration", "10"])

    captured = capsys.readouterr()
    assert result == 2
    assert output_factory.calls == []
    assert device_factory.calls == []
    assert output.writes == []
    assert device.open_calls == 0
    assert "uart_port=/dev/ttyAMA0" in captured.out
    assert "led_pins_bcm=red:17,yellow:27,green:22" in captured.out
    assert "thresholds_m=" in captured.out
    assert "hysteresis_enabled=true" in captured.out
    assert "release_thresholds_m=DANGER:1.7,SAFE:3.2" in captured.out
    assert "hysteresis_margin_m=danger:0.2,safe:0.2" in captured.out
    assert "distance_filtering=disabled" in captured.out
    assert "add --confirm-hardware" in captured.err


def test_configuration_uses_actual_thresholds_for_hysteresis_margins(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        diagnostic,
        "_DISTANCE_THRESHOLDS",
        diagnostic.DistanceThresholds(2.0, 4.0, 15.0),
    )
    monkeypatch.setattr(
        diagnostic,
        "_HYSTERESIS_THRESHOLDS",
        diagnostic.HysteresisThresholds(2.25, 4.3),
    )

    result = diagnostic.main(["--duration", "1"])

    captured = capsys.readouterr().out
    assert result == 2
    assert "DANGER:(0,2.0)" in captured
    assert "WARNING:[2.0,4.0]" in captured
    assert "release_thresholds_m=DANGER:2.25,SAFE:4.3" in captured
    assert "hysteresis_margin_m=danger:0.25,safe:0.3" in captured


def test_custom_hardware_settings_are_displayed_and_passed_to_factories(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measurement = TFMiniPlusMeasurement(400, 100, 20.0)
    _, output_factory, _, device_factory = _install(monkeypatch, [[measurement]])

    result = diagnostic.main(
        _confirmed(
            "--port", "/dev/fake", "--baudrate", "230400", "--timeout", "0.2",
            "--red-pin", "5", "--yellow-pin", "6", "--green-pin", "13",
            "--gpio-chip", "4",
        )
    )

    assert result == 0
    assert output_factory.calls == [(LedPins(5, 6, 13), 4)]
    assert device_factory.calls == [
        {"port": "/dev/fake", "baudrate": 230400, "timeout": 0.2}
    ]
    assert "gpio_chip=4" in capsys.readouterr().out


def test_state_changes_use_controller_patterns_and_turn_old_leds_off(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measurements = [
        TFMiniPlusMeasurement(400, 1, 20.0),
        TFMiniPlusMeasurement(240, 1, 20.0),
        TFMiniPlusMeasurement(120, 1, 20.0),
        TFMiniPlusMeasurement(1300, 1, 20.0),
    ]
    output, _, _, _ = _install(monkeypatch, [measurements])

    result = diagnostic.main(
        ["--max-samples", "4", "--confirm-hardware"]
    )

    assert result == 0
    assert output.writes[3:] == [
        (17, False), (27, False), (22, True),
        (17, False), (27, True), (22, False),
        (17, True), (27, False), (22, False),
        (17, True), (27, True), (22, False),
    ]
    captured = capsys.readouterr().out
    assert "SAFE -> WARNING" in captured
    assert "WARNING -> DANGER" in captured
    assert "DANGER -> SENSOR_FAULT" in captured
    assert "safe_count=1" in captured
    assert "warning_count=1" in captured
    assert "danger_count=1" in captured
    assert "sensor_faults=1" in captured
    assert "state_transitions=4" in captured


def test_no_measurement_keeps_fault_logic_and_leds_off(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, _, _, _ = _install(monkeypatch, [], clock=[0.0, 1.0, 1.0])

    result = diagnostic.main(
        ["--duration", "1", "--confirm-hardware"]
    )

    captured = capsys.readouterr().out
    assert result == 0
    assert output.writes == [(17, False), (27, False), (22, False)]
    assert "initial_state=SENSOR_FAULT (LEDs remain off)" in captured
    assert "valid_measurements=0" in captured
    assert "empty_reads=0" in captured
    assert "state_transitions=0" in captured
    assert "final_state=SENSOR_FAULT" in captured


def test_first_empty_read_displays_fault_without_false_transition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output, _, _, _ = _install(
        monkeypatch, [[]], clock=[0.0, 0.25, 1.0, 1.0]
    )

    result = diagnostic.main(
        ["--duration", "1", "--confirm-hardware"]
    )

    captured = capsys.readouterr().out
    assert result == 0
    assert output.writes == [
        (17, False), (27, False), (22, False),
        (17, True), (27, True), (22, False),
    ]
    assert "empty_reads=1" in captured
    assert "sensor_faults=1" in captured
    assert "state_transitions=0" in captured
    assert "final_state=SENSOR_FAULT" in captured


@pytest.mark.parametrize(
    ("distance_cm", "state", "pattern"),
    [
        (400, "SAFE", [(17, False), (27, False), (22, True)]),
        (240, "WARNING", [(17, False), (27, True), (22, False)]),
        (120, "DANGER", [(17, True), (27, False), (22, False)]),
    ],
)
def test_first_valid_measurement_transitions_from_fault_and_drives_leds(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    distance_cm: int,
    state: str,
    pattern: list[tuple[int, bool]],
) -> None:
    measurement = TFMiniPlusMeasurement(distance_cm, 1, 20.0)
    output, _, _, _ = _install(monkeypatch, [[measurement]])

    result = diagnostic.main(_confirmed())

    captured = capsys.readouterr().out
    assert result == 0
    assert output.writes[:3] == [
        (17, False), (27, False), (22, False)
    ]
    assert output.writes[3:] == pattern
    assert f"SENSOR_FAULT -> {state}" in captured
    assert "state_transitions=1" in captured
    assert f"final_state={state}" in captured


@pytest.mark.parametrize(
    ("argv", "events", "clock"),
    [
        (
            ["--max-samples", "1", "--confirm-hardware"],
            [[TFMiniPlusMeasurement(400, 1, 20.0)]],
            [0.0, 1.0],
        ),
        (
            ["--duration", "1", "--confirm-hardware"],
            [[]],
            [0.0, 0.25, 1.0, 1.0],
        ),
    ],
)
def test_bounded_completion_closes_uart_and_gpio(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    events: list[object],
    clock: list[float],
) -> None:
    output, _, device, _ = _install(monkeypatch, events, clock=clock)

    result = diagnostic.main(argv)

    assert result == 0
    assert device.close_calls == 1
    assert output.close_calls == 1
    assert "cleanup_completed=true" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_text"),
    [
        (KeyboardInterrupt(), 130, "interrupted"),
        (TFMiniPlusSerialReadError("read failed"), 1, "UART error"),
        (ValueError("parser failed"), 1, "parser error"),
    ],
)
def test_read_failures_close_resources_and_report_faults(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
    expected_code: int,
    expected_text: str,
) -> None:
    output, _, device, _ = _install(monkeypatch, [failure])

    result = diagnostic.main(_confirmed())

    captured = capsys.readouterr()
    assert result == expected_code
    assert device.close_calls == 1
    assert output.close_calls == 1
    assert expected_text in captured.out + captured.err
    assert "cleanup_completed=true" in captured.out
    if not isinstance(failure, KeyboardInterrupt):
        assert "final_state=SENSOR_FAULT" in captured.out


def test_gpio_write_failure_prevents_uart_open_and_still_closes_gpio(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake_output = _FakeOutput(write_error=RuntimeError("GPIO write failed"))
    output, _, device, device_factory = _install(
        monkeypatch, [], output=fake_output
    )

    result = diagnostic.main(_confirmed())

    assert result == 1
    assert device_factory.calls == []
    assert device.open_calls == 0
    assert output.close_calls == 1
    assert "GPIO write failed" in capsys.readouterr().err


def test_gpio_write_and_cleanup_errors_are_both_reported(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output = _FakeOutput(
        write_error=RuntimeError("GPIO write failed"),
        close_error=RuntimeError("GPIO close failed"),
    )
    _install(monkeypatch, [], output=output)

    result = diagnostic.main(_confirmed())

    captured = capsys.readouterr()
    assert result == 1
    assert "warning-chain hardware error: GPIO write failed" in captured.err
    assert "failed to close LED GPIO outputs: GPIO close failed" in captured.err
    assert "cleanup_completed=false" in captured.out


def test_summary_counts_empty_read_and_valid_measurement(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measurement = TFMiniPlusMeasurement(240, 1, 20.0)
    _install(monkeypatch, [[], [measurement]], clock=[0.0, 0.1, 0.2, 1.0])

    result = diagnostic.main(_confirmed())

    captured = capsys.readouterr().out
    assert result == 0
    assert "valid_measurements=1" in captured
    assert "empty_reads=1" in captured
    assert "parser_errors=0" in captured
    assert "last_valid_measurement_at=2026-08-24T12:00:00.000+00:00" in captured
    assert "final_state=WARNING" in captured


def test_summary_counts_only_stabilized_state_transitions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    measurements = [
        TFMiniPlusMeasurement(distance, 1, 20.0)
        for distance in (301, 300, 301, 320, 321)
    ]
    _install(monkeypatch, [measurements])

    result = diagnostic.main(
        ["--max-samples", "5", "--confirm-hardware"]
    )

    captured = capsys.readouterr().out
    assert result == 0
    assert "state_transitions=3" in captured
    assert "safe_count=2" in captured
    assert "warning_count=3" in captured
    assert "danger_count=0" in captured
    assert "sensor_faults=0" in captured
    assert "final_state=SAFE" in captured


@pytest.mark.parametrize(
    ("uart_fails", "gpio_fails"),
    [(True, False), (False, True), (True, True)],
)
def test_cleanup_failures_are_all_reported_and_return_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    uart_fails: bool,
    gpio_fails: bool,
) -> None:
    measurement = TFMiniPlusMeasurement(400, 1, 20.0)
    output = _FakeOutput(
        close_error=RuntimeError("GPIO close failed") if gpio_fails else None
    )
    _install(
        monkeypatch,
        [[measurement]],
        output=output,
        device_close_error=(
            RuntimeError("UART close failed") if uart_fails else None
        ),
    )

    result = diagnostic.main(_confirmed())

    captured = capsys.readouterr()
    assert result == 1
    assert "cleanup_completed=false" in captured.out
    if uart_fails:
        assert "failed to close TFMini Plus UART: UART close failed" in captured.err
    if gpio_fails:
        assert "failed to close LED GPIO outputs: GPIO close failed" in captured.err


@pytest.mark.parametrize(
    ("failure", "primary_message"),
    [
        (ValueError("parser failed"), "parser error: parser failed"),
        (TFMiniPlusSerialReadError("serial failed"), "UART error: serial failed"),
    ],
)
def test_primary_read_error_and_cleanup_errors_remain_diagnosable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    primary_message: str,
) -> None:
    output = _FakeOutput(close_error=RuntimeError("GPIO close failed"))
    _install(
        monkeypatch,
        [failure],
        output=output,
        device_close_error=RuntimeError("UART close failed"),
    )

    result = diagnostic.main(_confirmed())

    captured = capsys.readouterr()
    assert result == 1
    assert primary_message in captured.err
    assert "UART close failed" in captured.err
    assert "GPIO close failed" in captured.err
    assert "cleanup_completed=false" in captured.out
