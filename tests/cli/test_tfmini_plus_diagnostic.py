"""Hardware-free tests for the TFMini Plus diagnostic command."""

from __future__ import annotations

import tomllib
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import pytest

from rear_warning.cli import tfmini_plus_diagnostic as diagnostic
from rear_warning.sensors.tfmini_plus import (
    TFMiniPlusMeasurement,
    TFMiniPlusSerialCloseError,
    TFMiniPlusSerialOpenError,
    TFMiniPlusSerialReadError,
)


_MEASUREMENT = TFMiniPlusMeasurement(
    distance_cm=321,
    strength=987,
    chip_temperature_c=24.5,
)
_MEASUREMENT_TIME = datetime(2026, 8, 1, 12, 34, 56, tzinfo=timezone.utc)


class _FakeMonotonic:
    """Return deterministic monotonic timestamps for loop and rate tests."""

    def __init__(self, values: list[float]) -> None:
        if not values:
            raise ValueError("at least one clock value is required")
        self._values = deque(values)
        self._last_value = values[-1]

    def __call__(self) -> float:
        """Return the next timestamp, then retain the final timestamp."""
        if self._values:
            self._last_value = self._values.popleft()
        return self._last_value


class _FakeDevice:
    """Small serial-device fake with queued measurements or failures."""

    def __init__(
        self,
        events: list[list[TFMiniPlusMeasurement] | BaseException] | None = None,
        *,
        open_error: TFMiniPlusSerialOpenError | None = None,
        close_error: TFMiniPlusSerialCloseError | None = None,
    ) -> None:
        self._events = deque(events or [])
        self._open_error = open_error
        self._close_error = close_error
        self.open_calls = 0
        self.close_calls = 0
        self.read_calls = 0
        self.is_open = False

    def open(self) -> None:
        """Open the fake device or raise its configured failure."""
        self.open_calls += 1
        if self._open_error is not None:
            raise self._open_error
        self.is_open = True

    def close(self) -> None:
        """Close the fake device or raise its configured failure."""
        self.close_calls += 1
        if self._close_error is not None:
            raise self._close_error
        self.is_open = False

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        """Return the next event, with an empty list representing timeout."""
        self.read_calls += 1
        if not self._events:
            return []
        event = self._events.popleft()
        if isinstance(event, BaseException):
            raise event
        return event


class _FakeDeviceFactory:
    """Record requested UART settings and return one fake device."""

    def __init__(self, device: _FakeDevice) -> None:
        self.device = device
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        port: str,
        baudrate: int,
        timeout: float | None,
    ) -> _FakeDevice:
        """Return the device after recording the requested settings."""
        self.calls.append(
            {"port": port, "baudrate": baudrate, "timeout": timeout}
        )
        return self.device


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    device: _FakeDevice,
    *,
    monotonic_values: list[float] | None = None,
) -> _FakeDeviceFactory:
    """Install a fake device factory and deterministic clocks."""
    factory = _FakeDeviceFactory(device)
    monkeypatch.setattr(diagnostic, "_DEVICE_FACTORY", factory)
    monkeypatch.setattr(
        diagnostic,
        "_MONOTONIC",
        _FakeMonotonic(monotonic_values or [0.0, 1.0]),
    )
    monkeypatch.setattr(
        diagnostic,
        "_WALL_CLOCK",
        lambda: _MEASUREMENT_TIME,
    )
    return factory


def test_custom_command_line_arguments_are_passed_to_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _FakeDevice([[_MEASUREMENT]])
    factory = _install_fakes(
        monkeypatch,
        device,
        monotonic_values=[0.0, 0.25, 1.0],
    )

    result = diagnostic.main(
        [
            "--port",
            "loop://diagnostic",
            "--baudrate",
            "230400",
            "--timeout",
            "0.25",
            "--duration",
            "5",
            "--max-samples",
            "1",
        ]
    )

    assert result == 0
    assert factory.calls == [
        {
            "port": "loop://diagnostic",
            "baudrate": 230400,
            "timeout": 0.25,
        }
    ]


def test_default_baudrate_and_timeout_are_passed_to_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _FakeDevice([[_MEASUREMENT]])
    factory = _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    assert result == 0
    assert factory.calls[0]["baudrate"] == 115200
    assert factory.calls[0]["timeout"] == 0.1


def test_missing_end_condition_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice()
    factory = _install_fakes(monkeypatch, device)

    result = diagnostic.main(["--port", "fake-port"])

    captured = capsys.readouterr()
    assert result == 2
    assert "at least one of --duration or --max-samples" in captured.err
    assert factory.calls == []


def test_duration_stops_the_diagnostic_loop(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice([[]])
    _install_fakes(
        monkeypatch,
        device,
        monotonic_values=[0.0, 0.25, 1.0, 1.0],
    )

    result = diagnostic.main(
        ["--port", "fake-port", "--duration", "1"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert device.read_calls == 1
    assert "empty_reads=1" in captured.out
    assert "elapsed_seconds=1.000" in captured.out


def test_max_samples_stops_and_limits_batch_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    extra = TFMiniPlusMeasurement(999, 111, 22.0)
    device = _FakeDevice([[_MEASUREMENT, _MEASUREMENT, extra]])
    _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "2"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out.count("measurement distance_cm=") == 2
    assert "distance_cm=999" not in captured.out
    assert "valid_measurements=2" in captured.out


def test_valid_measurement_and_last_timestamp_are_printed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice([[_MEASUREMENT]])
    _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert (
        "measurement distance_cm=321 strength=987 "
        "chip_temperature_c=24.50"
    ) in captured.out
    assert "last_valid_measurement_at=2026-08-01T12:34:56.000+00:00" in (
        captured.out
    )


def test_timeout_is_counted_before_later_measurement(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice([[], [_MEASUREMENT]])
    _install_fakes(monkeypatch, device, monotonic_values=[0.0, 2.0])

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert device.read_calls == 2
    assert "empty_reads=1" in captured.out
    assert "average_valid_rate_hz=0.500" in captured.out


def test_ctrl_c_closes_device_and_returns_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice([KeyboardInterrupt()])
    _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert device.close_calls == 1
    assert device.is_open is False
    assert "interrupted by user" in captured.out
    assert "valid_measurements=0" in captured.out


def test_uart_open_error_is_reported_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice(
        open_error=TFMiniPlusSerialOpenError("port unavailable")
    )
    _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert device.close_calls == 1
    assert "failed to open TFMini Plus UART: port unavailable" in captured.err
    assert "diagnostic_statistics" in captured.out


def test_uart_read_error_is_reported_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice(
        [TFMiniPlusSerialReadError("read disconnected")]
    )
    _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert device.close_calls == 1
    assert device.is_open is False
    assert "failed to read TFMini Plus UART: read disconnected" in captured.err


def test_device_is_closed_after_successful_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = _FakeDevice([[_MEASUREMENT]])
    _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    assert result == 0
    assert device.open_calls == 1
    assert device.close_calls == 1
    assert device.is_open is False


def test_uart_close_error_changes_result_to_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice(
        [[_MEASUREMENT]],
        close_error=TFMiniPlusSerialCloseError("close failed"),
    )
    _install_fakes(monkeypatch, device)

    result = diagnostic.main(
        ["--port", "fake-port", "--max-samples", "1"]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert device.close_calls == 1
    assert "failed to close TFMini Plus UART: close failed" in captured.err


def test_invalid_numeric_argument_returns_usage_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = _FakeDevice()
    factory = _install_fakes(monkeypatch, device)

    zero_result = diagnostic.main(
        ["--port", "fake-port", "--duration", "0"]
    )
    nan_result = diagnostic.main(
        ["--port", "fake-port", "--duration", "nan"]
    )

    captured = capsys.readouterr()
    assert zero_result == 2
    assert nan_result == 2
    assert "must be greater than zero" in captured.err
    assert "must be finite" in captured.err
    assert factory.calls == []


def test_console_script_points_to_diagnostic_main() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["scripts"]["tfmini-plus-diagnostic"] == (
        "rear_warning.cli.tfmini_plus_diagnostic:main"
    )
