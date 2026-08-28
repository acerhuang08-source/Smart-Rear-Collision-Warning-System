from __future__ import annotations

import math
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from rear_warning.cli import bno055_diagnostic
from rear_warning.sensors.bno055 import (
    BNO055Calibration, BNO055RuntimeStateError,
)
from rear_warning.sensors.bno055.device import BNO055IOError
from rear_warning.sensors.bno055.hardware import BNO055HardwareDependenciesNotInstalled
from rear_warning.sensors.bno055.models import (
    BNO055Identity, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)


class FakeAdapterFactory:
    def __call__(self, bus: int) -> FakeRegisterIO:
        self.calls.append(bus)
        return self.register_io

    def __init__(self) -> None:
        self.calls: list[int] = []
        self.register_io = FakeRegisterIO()


class FakeRegisterIO:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class MutableClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class NonAdvancingClock:
    def __init__(self, *, backwards: bool = False) -> None:
        self.calls = 0
        self.backwards = backwards

    def monotonic(self) -> float:
        self.calls += 1
        return float(-self.calls if self.backwards else 0.0)


class FakeDevice:
    def __init__(self, *, read_error: Exception | None = None, close_error: Exception | None = None, init_error: Exception | None = None, interrupt_on_read: int | None = None, runtime_states: list[tuple[int, int]] | None = None, measurements: list[BNO055Measurement] | None = None, on_read: Callable[[], None] | None = None, on_close: Callable[[], None] | None = None) -> None:
        self.address = 0x29
        self.read_error = read_error; self.close_error = close_error
        self.init_error = init_error; self.interrupt_on_read = interrupt_on_read
        self.runtime_states = list(runtime_states or [])
        self.measurements = list(measurements or [])
        self.on_read = on_read; self.on_close = on_close
        self.close_calls = 0; self.read_calls = 0
    def read_identity(self) -> BNO055Identity:
        return BNO055Identity(self.address, 0xA0, 0xFB, 0x32, 0x0F, 0x0308, 0x15)
    def initialize_ndof(self) -> tuple[int, int, int]:
        if self.init_error: raise self.init_error
        return (0x0C, 5, 0)
    def read_calibration(self) -> BNO055Calibration: return BNO055Calibration(3, 2, 1, 0)
    def read_measurement(self) -> BNO055Measurement:
        self.read_calls += 1
        if self.on_read is not None: self.on_read()
        if self.interrupt_on_read == self.read_calls: raise KeyboardInterrupt
        if self.read_error: raise self.read_error
        status, error = self.runtime_states.pop(0) if self.runtime_states else (5, 0)
        if status != 5 or error != 0:
            raise BNO055RuntimeStateError(
                operation_mode=0x0C, system_status=status, system_error=error
            )
        if self.measurements:
            return (
                self.measurements.pop(0)
                if len(self.measurements) > 1
                else self.measurements[0]
            )
        return BNO055Measurement(
            1.0, EulerAngles(1, 2, 3), Quaternion(1, 0, 0, 0),
            Vector3(0.1, 0.2, 0.3), Vector3(0, 0, 9.8), 24,
            BNO055Calibration(3, 2, 1, 0), 0x0C, status, error,
        )
    def close(self) -> None:
        self.close_calls += 1
        if self.on_close is not None: self.on_close()
        if self.close_error: raise self.close_error


def fake_measurement(
    *,
    quaternion: Quaternion = Quaternion(1, 0, 0, 0),
    gravity: Vector3 = Vector3(0, 0, 9.8),
    euler: EulerAngles = EulerAngles(1, 2, 3),
    linear: Vector3 = Vector3(0.1, 0.2, 0.3),
    calibration: BNO055Calibration = BNO055Calibration(3, 2, 1, 0),
) -> BNO055Measurement:
    return BNO055Measurement(
        1.0, euler, quaternion, linear, gravity, 24, calibration,
        0x0C, 0x05, 0x00,
    )


def install_fakes(monkeypatch: pytest.MonkeyPatch, device: FakeDevice) -> FakeAdapterFactory:
    adapter = FakeAdapterFactory()
    monkeypatch.setattr(bno055_diagnostic, "_ADAPTER_FACTORY", adapter)
    monkeypatch.setattr(bno055_diagnostic, "_DEVICE_FACTORY", lambda _io, address: setattr(device, "address", address) or device)
    monkeypatch.setattr(bno055_diagnostic, "_SLEEP", lambda _seconds: None)
    monkeypatch.setattr(bno055_diagnostic, "_MONOTONIC", lambda: 1.0)
    return adapter


@pytest.mark.parametrize("bound", [["--duration", "1"], ["--max-samples", "1"]])
def test_unconfirmed_is_zero_io_and_prints_plan(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bound: list[str]) -> None:
    monkeypatch.setattr(bno055_diagnostic, "_ADAPTER_FACTORY", None)
    monkeypatch.setattr(bno055_diagnostic, "load_smbus_register_io", lambda: (_ for _ in ()).throw(AssertionError("hardware import")))
    assert bno055_diagnostic.main(bound) == 2
    captured = capsys.readouterr()
    assert "bus=1" in captured.out and "address=0x29" in captured.out
    assert "planned_writes=" in captured.out and "--confirm-hardware is required" in captured.err


@pytest.mark.parametrize("argv", [[], ["--duration", "0"], ["--max-samples", "0"], ["--duration", "nan"], ["--sample-interval", "-1"], ["--data-ready-timeout", "0"], ["--address", "0x27"], ["--bus", "-1"]])
def test_argument_boundaries_return_usage(argv: list[str]) -> None:
    assert bno055_diagnostic.main(argv) == 2


@pytest.mark.parametrize(("address", "expected"), [("0x28", 0x28), ("0x29", 0x29)])
def test_confirmed_bounded_run(address: str, expected: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(); adapter = install_fakes(monkeypatch, device)
    result = bno055_diagnostic.main(["--address", address, "--max-samples", "2", "--sample-interval", "0", "--confirm-hardware"])
    assert result == 0 and adapter.calls == [1] and device.address == expected
    assert device.read_calls == 2 and device.close_calls == 1
    output = capsys.readouterr().out
    assert "operation_mode=0x0c" in output and "valid_samples=2" in output
    assert "cleanup_completed=true" in output and "euler=" in output


def test_read_error_counts_and_closes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(read_error=BNO055IOError("read failed")); install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(["--max-samples", "1", "--confirm-hardware"]) == 1
    captured = capsys.readouterr()
    assert "read_errors=1" in captured.out and "read failed" in captured.err
    assert device.close_calls == 1


def test_initialization_failure_still_cleans_up(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(init_error=BNO055IOError("initialization failed")); install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(["--max-samples", "1", "--confirm-hardware"]) == 1
    assert device.close_calls == 1
    captured = capsys.readouterr()
    assert "initialization failed" in captured.err
    assert "cleanup_completed=true" in captured.out


def test_ctrl_c_cleans_up_without_false_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(interrupt_on_read=1); install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(["--max-samples", "1", "--confirm-hardware"]) == 130
    output = capsys.readouterr().out
    assert "interrupted=true" in output and "cleanup_completed=true" in output
    assert "valid_samples=0" in output


def test_ctrl_c_after_samples_reports_count_and_130(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(interrupt_on_read=3); install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(["--max-samples", "5", "--sample-interval", "0", "--confirm-hardware"]) == 130
    output = capsys.readouterr().out
    assert "valid_samples=2" in output and "interrupted=true" in output


def test_cleanup_failure_is_command_failure(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(close_error=RuntimeError("close failed")); install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(["--max-samples", "1", "--sample-interval", "0", "--confirm-hardware"]) == 1
    captured = capsys.readouterr()
    assert "cleanup_completed=false" in captured.out and "close failed" in captured.err


def test_missing_hardware_extra_has_hint_without_traceback(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(bno055_diagnostic, "_ADAPTER_FACTORY", None)
    monkeypatch.setattr(bno055_diagnostic, "load_smbus_register_io", lambda: (_ for _ in ()).throw(BNO055HardwareDependenciesNotInstalled('Install with: pip install -e ".[hardware]"')))
    assert bno055_diagnostic.main(["--max-samples", "1", "--confirm-hardware"]) == 1
    captured = capsys.readouterr()
    assert ".[hardware]" in captured.err and "Traceback" not in captured.err


def test_short_duration_still_attempts_first_read(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(); install_fakes(monkeypatch, device)
    times = iter([0.0, 10.0, 10.001, 10.002, 10.003, 10.004])
    monkeypatch.setattr(bno055_diagnostic, "_MONOTONIC", lambda: next(times))
    assert bno055_diagnostic.main(["--duration", "0.0005", "--confirm-hardware"]) == 0
    assert device.read_calls == 1
    output = capsys.readouterr().out
    assert "valid_samples=1" in output
    assert "sampling_elapsed_seconds=0.002" in output
    assert "total_elapsed_seconds=10.004" in output


@pytest.mark.parametrize(
    ("outcome", "expected_exit"),
    [("normal", 0), ("interrupt", 130), ("runtime_error", 1)],
)
def test_sampling_elapsed_excludes_one_second_cleanup_delay(
    outcome: str,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = MutableClock()
    device = FakeDevice(
        interrupt_on_read=2 if outcome == "interrupt" else None,
        runtime_states=[(5, 0), (5, 7)] if outcome == "runtime_error" else None,
        on_read=lambda: clock.advance(0.25),
        on_close=lambda: clock.advance(1.0),
    )
    install_fakes(monkeypatch, device)
    monkeypatch.setattr(bno055_diagnostic, "_MONOTONIC", clock.monotonic)
    result = bno055_diagnostic.main(
        ["--max-samples", "2", "--sample-interval", "0", "--confirm-hardware"]
    )
    assert result == expected_exit
    output = capsys.readouterr().out
    assert "sampling_elapsed_seconds=0.250" in output
    assert "total_elapsed_seconds=1.500" in output


@pytest.mark.parametrize(
    ("states", "expected_valid", "status", "error"),
    [([(5, 0), (5, 7)], 1, 5, 7), ([(5, 0), (4, 0)], 1, 4, 0),
     ([(5, 9)], 0, 5, 9)],
)
def test_runtime_state_error_is_not_counted_as_valid(
    states: list[tuple[int, int]], expected_valid: int, status: int, error: int,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    device = FakeDevice(runtime_states=states); install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(["--max-samples", "3", "--sample-interval", "0", "--confirm-hardware"]) == 1
    captured = capsys.readouterr()
    assert f"valid_samples={expected_valid}" in captured.out
    assert "runtime_state_errors=1" in captured.out
    assert f"sample_index={expected_valid + 1}" in captured.err
    assert f"status=0x{status:02x}" in captured.err
    assert f"error=0x{error:02x}" in captured.err
    assert device.close_calls == 1


def test_runtime_error_and_cleanup_failure_are_both_reported(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    device = FakeDevice(runtime_states=[(5, 7)], close_error=RuntimeError("cleanup failed")); install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(["--max-samples", "2", "--confirm-hardware"]) == 1
    captured = capsys.readouterr()
    assert "runtime state error" in captured.err
    assert "cleanup failed" in captured.err
    assert "cleanup_completed=false" in captured.out


def test_device_construction_failure_closes_adapter(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    adapter = FakeAdapterFactory()
    monkeypatch.setattr(bno055_diagnostic, "_ADAPTER_FACTORY", adapter)
    monkeypatch.setattr(bno055_diagnostic, "_DEVICE_FACTORY", lambda _io, address: (_ for _ in ()).throw(RuntimeError("construction failed")))
    monkeypatch.setattr(bno055_diagnostic, "_MONOTONIC", lambda: 0.0)
    assert bno055_diagnostic.main(["--max-samples", "1", "--confirm-hardware"]) == 1
    assert adapter.register_io.close_calls == 1
    assert "construction failed" in capsys.readouterr().err


def test_hardware_extra_and_entry_point_are_declared() -> None:
    data = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    assert "smbus2>=0.6.1,<1" in data["project"]["optional-dependencies"]["hardware"]
    assert data["project"]["scripts"]["bno055-diagnostic"].endswith(":main")


def test_zero_startup_sample_is_discarded_and_second_is_sample_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    zero = fake_measurement(
        quaternion=Quaternion(0, 0, 0, 0), gravity=Vector3(0, 0, 0)
    )
    device = FakeDevice(measurements=[zero, fake_measurement()])
    install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(
        ["--max-samples", "1", "--sample-interval", "0", "--confirm-hardware"]
    ) == 0
    output = capsys.readouterr().out
    assert device.read_calls == 2
    assert "discarded_startup_samples=1" in output
    assert "valid_samples=1" in output
    assert output.count("measurement timestamp=") == 1


def test_multiple_zero_startup_samples_recover(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    zero = fake_measurement(
        quaternion=Quaternion(0, 0, 0, 0), gravity=Vector3(0, 0, 0)
    )
    device = FakeDevice(measurements=[zero, zero, zero, fake_measurement()])
    install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(
        ["--max-samples", "1", "--sample-interval", "0", "--confirm-hardware"]
    ) == 0
    output = capsys.readouterr().out
    assert "discarded_startup_samples=3" in output
    assert "valid_samples=1" in output


@pytest.mark.parametrize("backwards", [False, True])
def test_data_ready_poll_count_bounds_constant_or_backward_clock(
    backwards: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    zero = fake_measurement(
        quaternion=Quaternion(0, 0, 0, 0), gravity=Vector3(0, 0, 0)
    )
    device = FakeDevice(measurements=[zero])
    install_fakes(monkeypatch, device)
    clock = NonAdvancingClock(backwards=backwards)
    monkeypatch.setattr(bno055_diagnostic, "_MONOTONIC", clock.monotonic)
    timeout = 0.05
    expected_attempts = math.ceil(
        timeout / bno055_diagnostic._DATA_READY_POLL_INTERVAL_SECONDS
    ) + 1
    assert bno055_diagnostic.main(
        ["--max-samples", "1", "--data-ready-timeout", str(timeout),
         "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert device.read_calls == expected_attempts
    assert f"discarded_startup_samples={expected_attempts}" in captured.out
    assert "data readiness timed out" in captured.err
    assert "valid_samples=0" in captured.out
    assert "cleanup_completed=true" in captured.out


def test_sampling_duration_starts_with_first_quality_sample(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = MutableClock()
    zero = fake_measurement(
        quaternion=Quaternion(0, 0, 0, 0), gravity=Vector3(0, 0, 0)
    )
    device = FakeDevice(
        measurements=[zero, fake_measurement(), fake_measurement()],
        on_read=lambda: clock.advance(0.5),
    )
    install_fakes(monkeypatch, device)
    monkeypatch.setattr(bno055_diagnostic, "_MONOTONIC", clock.monotonic)
    monkeypatch.setattr(bno055_diagnostic, "_SLEEP", clock.advance)
    assert bno055_diagnostic.main(
        ["--duration", "0.25", "--sample-interval", "0.25",
         "--confirm-hardware"]
    ) == 0
    output = capsys.readouterr().out
    assert device.read_calls == 2
    assert "first_valid_sample_wait_seconds=1.020" in output
    assert "valid_samples=1" in output
    assert "sampling_elapsed_seconds=0.250" in output
    assert "average_rate_hz=4.000" in output


@pytest.mark.parametrize(
    "bad",
    [
        fake_measurement(quaternion=Quaternion(0, 0, 0, 0)),
        fake_measurement(quaternion=Quaternion(0.49, 0, 0, 0)),
        fake_measurement(quaternion=Quaternion(1.51, 0, 0, 0)),
        fake_measurement(gravity=Vector3(0, 0, 4.9)),
        fake_measurement(gravity=Vector3(0, 0, 15.1)),
        fake_measurement(euler=EulerAngles(math.nan, 0, 0)),
        fake_measurement(linear=Vector3(math.inf, 0, 0)),
    ],
)
def test_runtime_data_error_stops_and_does_not_count_sample(
    bad: BNO055Measurement,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    device = FakeDevice(measurements=[fake_measurement(), bad])
    install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(
        ["--max-samples", "3", "--sample-interval", "0", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "valid_samples=1" in captured.out
    assert "runtime_data_errors=1" in captured.out
    assert "runtime data error: sample_index=2" in captured.err
    assert device.close_calls == 1


def test_runtime_data_error_and_cleanup_failure_are_both_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = fake_measurement(quaternion=Quaternion(0, 0, 0, 0))
    device = FakeDevice(
        measurements=[fake_measurement(), bad],
        close_error=RuntimeError("cleanup failed"),
    )
    install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(
        ["--max-samples", "3", "--sample-interval", "0", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "runtime data error" in captured.err
    assert "cleanup failed" in captured.err
    assert "runtime_data_errors=1" in captured.out
    assert "cleanup_completed=false" in captured.out


def test_zero_euler_linear_and_calibration_remain_valid_in_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    device = FakeDevice(
        measurements=[
            fake_measurement(
                euler=EulerAngles(0, 0, 0),
                linear=Vector3(0, 0, 0),
                calibration=BNO055Calibration(0, 0, 0, 0),
            )
        ]
    )
    install_fakes(monkeypatch, device)
    assert bno055_diagnostic.main(
        ["--max-samples", "1", "--sample-interval", "0", "--confirm-hardware"]
    ) == 0
    output = capsys.readouterr().out
    assert "valid_samples=1" in output
    assert "runtime_data_errors=0" in output
