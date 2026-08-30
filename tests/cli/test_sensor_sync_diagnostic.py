from __future__ import annotations

from pathlib import Path

import pytest

from rear_warning.cli import sensor_sync_diagnostic
from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Identity, BNO055Measurement, EulerAngles,
    Quaternion, Vector3,
)
from rear_warning.sensors.bno055.device import BNO055Device
from rear_warning.sensors.bno055.registers import (
    ACC_ID, BL_REV_ID, CHIP_ID, CONFIG_MODE, EXPECTED_ACC_ID,
    EXPECTED_CHIP_ID, EXPECTED_GYR_ID, EXPECTED_MAG_ID, GYR_ID, MAG_ID,
    NDOF_MODE, OPR_MODE, SW_REV_ID_LSB, SW_REV_ID_MSB, SYS_ERR, SYS_STATUS,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization.csv_sink import CsvSinkError


def motion() -> BNO055Measurement:
    return BNO055Measurement(
        1.0, EulerAngles(0, 0, 0), Quaternion(1, 0, 0, 0),
        Vector3(0, 0, 0), Vector3(0, 0, 9.8), 25,
        BNO055Calibration(0, 0, 0, 0), 0x0C, 0x05, 0,
    )


class FakeDistance:
    def __init__(
        self,
        *,
        read_error: BaseException | None = None,
        close_error: BaseException | None = None,
        measurements: list[TFMiniPlusMeasurement] | None = None,
    ) -> None:
        self.read_error = read_error
        self.close_error = close_error
        self.measurements = measurements or [TFMiniPlusMeasurement(100, 500, 25)]
        self.open_calls = 0
        self.close_calls = 0

    def open(self) -> None:
        self.open_calls += 1

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        if self.read_error:
            raise self.read_error
        return self.measurements

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


class FakeMotion:
    def __init__(
        self,
        *,
        read_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.read_error = read_error
        self.close_error = close_error
        self.close_calls = 0

    def read_identity(self) -> BNO055Identity:
        return BNO055Identity(0x29, 0xA0, 0xFB, 0x32, 0x0F, 0x0308, 0x15)

    def initialize_ndof(self) -> tuple[int, int, int]:
        return 0x0C, 0x05, 0

    def read_measurement(self) -> BNO055Measurement:
        if self.read_error:
            raise self.read_error
        return motion()

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


class FakeRegisterIO:
    def __init__(self) -> None:
        self.close_calls = 0
    def close(self) -> None:
        self.close_calls += 1


def install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    distance: FakeDistance,
    motion_device: FakeMotion,
) -> None:
    register_io = FakeRegisterIO()
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_DISTANCE_FACTORY",
        lambda **_kwargs: distance,
    )
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_MOTION_ADAPTER_FACTORY",
        lambda _bus: register_io,
    )
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_MOTION_DEVICE_FACTORY",
        lambda _io, **_kwargs: motion_device,
    )
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", lambda: 1.0)
    monkeypatch.setattr(sensor_sync_diagnostic, "_SLEEP", lambda _seconds: None)


def test_unconfirmed_is_zero_io_import_and_csv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "must-not-exist.csv"
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_load_hardware_factories",
        lambda: (_ for _ in ()).throw(AssertionError("hardware import")),
    )
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_CSV_FACTORY",
        lambda _path: (_ for _ in ()).throw(AssertionError("CSV created")),
    )
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--csv-output", str(path)]
    ) == 2
    captured = capsys.readouterr()
    assert "opens_UART_and_writes_BNO055_mode_registers" in captured.out
    assert "causal_latest" in captured.out
    assert "--confirm-hardware is required" in captured.err
    assert not path.exists()


@pytest.mark.parametrize(
    "argv",
    [[], ["--duration", "0"], ["--max-samples", "0"],
     ["--bno055-address", "0x27", "--max-samples", "1"],
     ["--maximum-bno055-age", "nan", "--max-samples", "1"]],
)
def test_invalid_arguments_return_usage(argv: list[str]) -> None:
    assert sensor_sync_diagnostic.main(argv) == 2


def test_fake_complete_dual_sensor_flow_and_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    distance, motion_device = FakeDistance(), FakeMotion()
    install_fakes(monkeypatch, distance, motion_device)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "2", "--confirm-hardware"]
    ) == 0
    captured = capsys.readouterr()
    assert "bno055_readiness=0x0c/0x05/0x00" in captured.out
    assert "matched_samples=2" in captured.out
    assert "cleanup_completed=true" in captured.out
    assert distance.open_calls == 1 and distance.close_calls == 1
    assert motion_device.close_calls == 1


def test_cli_injects_identical_clock_object_into_bno_and_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Clock:
        def __call__(self) -> float:
            return 1.0

    clock = Clock()
    distance = FakeDistance()
    motion_device = FakeMotion()
    register_io = FakeRegisterIO()
    received: dict[str, object] = {}
    real_coordinator = sensor_sync_diagnostic.SensorSamplingCoordinator

    def motion_factory(_io: object, **kwargs: object) -> FakeMotion:
        received["bno_clock"] = kwargs["monotonic"]
        return motion_device

    def coordinator_factory(*args: object, **kwargs: object) -> object:
        received["coordinator_clock"] = kwargs["monotonic"]
        return real_coordinator(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", clock)
    monkeypatch.setattr(sensor_sync_diagnostic, "_SLEEP", lambda _seconds: None)
    monkeypatch.setattr(sensor_sync_diagnostic, "_DISTANCE_FACTORY", lambda **_kwargs: distance)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY", lambda _bus: register_io)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", motion_factory)
    monkeypatch.setattr(sensor_sync_diagnostic, "SensorSamplingCoordinator", coordinator_factory)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 0
    assert received["bno_clock"] is clock
    assert received["coordinator_clock"] is clock


def test_csv_existing_stops_before_hardware(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "existing.csv"
    path.write_text("keep\n")
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_load_hardware_factories",
        lambda: (_ for _ in ()).throw(AssertionError("hardware touched")),
    )
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--csv-output", str(path), "--confirm-hardware"]
    ) == 1
    assert path.read_text() == "keep\n"
    assert "CSV error" in capsys.readouterr().err


def test_confirmed_csv_is_written_and_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fakes(monkeypatch, FakeDistance(), FakeMotion())
    path = tmp_path / "samples.csv"
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--csv-output", str(path), "--confirm-hardware"]
    ) == 0
    content = path.read_text()
    assert "sequence,distance_timestamp" in content
    assert ",matched," in content


def test_missing_hardware_extra_is_reported_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sensor_sync_diagnostic, "_DISTANCE_FACTORY", None)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY", None)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", None)
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_load_hardware_factories",
        lambda: (_ for _ in ()).throw(RuntimeError('Install with: pip install -e ".[hardware]"')),
    )
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert ".[hardware]" in captured.err and "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("distance_error", "motion_error", "label"),
    [(OSError("serial"), None, "serial error"),
     (None, OSError("i2c"), "BNO055 error")],
)
def test_device_errors_are_nonzero_and_cleanup(
    distance_error: BaseException | None,
    motion_error: BaseException | None,
    label: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    distance = FakeDistance(read_error=distance_error)
    motion_device = FakeMotion(read_error=motion_error)
    install_fakes(monkeypatch, distance, motion_device)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert label in captured.err
    assert distance.close_calls == 1 and motion_device.close_calls == 1


def test_ctrl_c_returns_130_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    distance = FakeDistance(read_error=KeyboardInterrupt())
    motion_device = FakeMotion()
    install_fakes(monkeypatch, distance, motion_device)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 130
    output = capsys.readouterr().out
    assert "interrupted=true" in output and "cleanup_completed=true" in output


def test_primary_error_and_multiple_cleanup_errors_are_all_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    distance = FakeDistance(
        read_error=OSError("primary serial"),
        close_error=OSError("UART close"),
    )
    motion_device = FakeMotion(close_error=OSError("BNO close"))
    install_fakes(monkeypatch, distance, motion_device)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "serial error" in captured.err
    assert "UART close" in captured.err and "BNO close" in captured.err
    assert "cleanup_completed=false" in captured.out


def test_csv_factory_failure_is_counted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_CSV_FACTORY",
        lambda _path: (_ for _ in ()).throw(CsvSinkError("open failed")),
    )
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--csv-output", str(tmp_path / "x.csv"),
         "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "csv_errors=1" in captured.out


def test_csv_write_failure_stops_and_still_closes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FailingCsv:
        def __init__(self) -> None:
            self.close_calls = 0
        def write(self, _sequence: int, _sample: object) -> None:
            raise CsvSinkError("write failed")
        def close(self) -> None:
            self.close_calls += 1
    csv_sink = FailingCsv()
    install_fakes(monkeypatch, FakeDistance(), FakeMotion())
    monkeypatch.setattr(sensor_sync_diagnostic, "_CSV_FACTORY", lambda _path: csv_sink)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--csv-output", str(tmp_path / "x.csv"),
         "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "CSV error" in captured.err and "csv_errors=1" in captured.out
    assert csv_sink.close_calls == 1


def test_zero_matched_samples_is_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    distance, motion_device = FakeDistance(), FakeMotion()
    install_fakes(monkeypatch, distance, motion_device)
    last = [-1.0]
    def clock() -> float:
        last[0] += 1.0
        return last[0]
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", clock)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "zero matched samples" in captured.err


def test_uart_timeout_over_motion_interval_is_rejected_before_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "not-created.csv"
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_CSV_FACTORY",
        lambda _path: (_ for _ in ()).throw(AssertionError("CSV created")),
    )
    monkeypatch.setattr(
        sensor_sync_diagnostic,
        "_load_hardware_factories",
        lambda: (_ for _ in ()).throw(AssertionError("hardware created")),
    )
    assert sensor_sync_diagnostic.main([
        "--duration", "1", "--uart-timeout", "0.11",
        "--bno055-interval", "0.1", "--csv-output", str(target),
        "--confirm-hardware",
    ]) == 2
    assert not target.exists()


def test_sampling_elapsed_excludes_slow_cleanup(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = [0.0]

    def now() -> float:
        return clock[0]

    class TimedDistance(FakeDistance):
        def read_measurements(self) -> list[TFMiniPlusMeasurement]:
            clock[0] += 0.25
            return super().read_measurements()

        def close(self) -> None:
            clock[0] += 1.0
            super().close()

    class TimedMotion(FakeMotion):
        def initialize_ndof(self) -> tuple[int, int, int]:
            clock[0] += 0.5
            return super().initialize_ndof()

    distance = TimedDistance()
    motion_device = TimedMotion()
    install_fakes(monkeypatch, distance, motion_device)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", now)
    assert sensor_sync_diagnostic.main(
        [
            "--max-samples", "1", "--maximum-bno055-age", "0.3",
            "--confirm-hardware",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert "sampling_elapsed_seconds=0.250" in output
    assert "total_elapsed_seconds=1.750" in output
    assert "matched_rate_hz=4.000" in output


@pytest.mark.parametrize(
    ("failure", "expected_rc", "expected_error"),
    [
        (KeyboardInterrupt(), 130, "interrupted"),
        (OSError("runtime serial"), 1, "serial error"),
    ],
)
def test_error_sampling_elapsed_excludes_slow_cleanup(
    failure: BaseException,
    expected_rc: int,
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = [0.0]

    def now() -> float:
        return clock[0]

    class TimedFailDistance(FakeDistance):
        def read_measurements(self) -> list[TFMiniPlusMeasurement]:
            clock[0] += 0.25
            raise failure

        def close(self) -> None:
            clock[0] += 1.0
            super().close()

    distance = TimedFailDistance()
    install_fakes(monkeypatch, distance, FakeMotion())
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", now)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == expected_rc
    captured = capsys.readouterr()
    assert expected_error in captured.err
    assert "sampling_elapsed_seconds=0.250" in captured.out
    assert "total_elapsed_seconds=1.250" in captured.out


def test_runtime_bno_error_elapsed_excludes_cleanup_and_preserves_rate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = [0.0]

    def now() -> float:
        return clock[0]

    class TimedDistance(FakeDistance):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def read_measurements(self) -> list[TFMiniPlusMeasurement]:
            self.reads += 1
            clock[0] += 0.05
            return self.measurements if self.reads == 1 else []

        def close(self) -> None:
            clock[0] += 1.0
            super().close()

    class TimedMotion(FakeMotion):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def initialize_ndof(self) -> tuple[int, int, int]:
            clock[0] += 0.5
            return super().initialize_ndof()

        def read_measurement(self) -> BNO055Measurement:
            self.reads += 1
            if self.reads == 1:
                clock[0] += 0.2
                return motion()
            clock[0] += 0.1
            raise OSError("runtime I2C detail")

    distance, motion_device = TimedDistance(), TimedMotion()
    install_fakes(monkeypatch, distance, motion_device)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", now)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "2", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "BNO055 measurement read failed" in captured.err
    assert "total_records=1" in captured.out
    assert "matched_samples=1" in captured.out
    assert "sampling_elapsed_seconds=0.200" in captured.out
    assert "total_elapsed_seconds=1.900" in captured.out
    assert "matched_rate_hz=5.000" in captured.out
    assert distance.close_calls == 1 and motion_device.close_calls == 1


def test_csv_error_elapsed_excludes_cleanup_and_failed_row_is_uncommitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = [0.0]

    def now() -> float:
        return clock[0]

    class TimedDistance(FakeDistance):
        def read_measurements(self) -> list[TFMiniPlusMeasurement]:
            clock[0] += 0.05
            return self.measurements

        def close(self) -> None:
            clock[0] += 1.0
            super().close()

    class TimedMotion(FakeMotion):
        def initialize_ndof(self) -> tuple[int, int, int]:
            clock[0] += 0.5
            return super().initialize_ndof()

        def read_measurement(self) -> BNO055Measurement:
            clock[0] += 0.2
            return motion()

    class SecondFlushFails:
        def __init__(self) -> None:
            self.writes = 0
            self.close_calls = 0

        def write(self, _sequence: int, _sample: object) -> None:
            self.writes += 1
            if self.writes == 2:
                raise CsvSinkError("second row flush detail")

        def close(self) -> None:
            self.close_calls += 1

    distance, motion_device, sink = TimedDistance(), TimedMotion(), SecondFlushFails()
    install_fakes(monkeypatch, distance, motion_device)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", now)
    monkeypatch.setattr(sensor_sync_diagnostic, "_CSV_FACTORY", lambda _path: sink)
    assert sensor_sync_diagnostic.main([
        "--max-samples", "3", "--bno055-interval", "1.0",
        "--csv-output", str(tmp_path / "samples.csv"), "--confirm-hardware",
    ]) == 1
    captured = capsys.readouterr()
    assert "second row flush detail" in captured.err
    assert "total_records=1" in captured.out
    assert "matched_samples=1" in captured.out
    assert "sampling_elapsed_seconds=0.100" in captured.out
    assert "total_elapsed_seconds=1.800" in captured.out
    assert "matched_rate_hz=10.000" in captured.out
    assert distance.close_calls == 1 and motion_device.close_calls == 1
    assert sink.close_calls == 1


def test_max_samples_with_permanently_empty_uart_ends_at_scheduler_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class AdvancingClock:
        def __init__(self) -> None:
            self.value = 0.0

        def __call__(self) -> float:
            result = self.value
            self.value += 0.001
            return result

    class EmptyDistance(FakeDistance):
        def read_measurements(self) -> list[TFMiniPlusMeasurement]:
            return []

    clock = AdvancingClock()
    distance, motion_device = EmptyDistance(), FakeMotion()
    install_fakes(monkeypatch, distance, motion_device)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", clock)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "scheduler iteration safety limit" in captured.err
    assert "total_records=0" in captured.out
    assert "matched_samples=0" in captured.out
    assert distance.close_calls == 1 and motion_device.close_calls == 1


def test_uart_constructor_failure_does_not_create_bno_and_closes_csv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[str] = []

    class Sink:
        def close(self) -> None:
            events.append("csv.close")

    monkeypatch.setattr(sensor_sync_diagnostic, "_CSV_FACTORY", lambda _path: Sink())
    monkeypatch.setattr(
        sensor_sync_diagnostic, "_DISTANCE_FACTORY",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("UART constructor")),
    )
    monkeypatch.setattr(
        sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY",
        lambda _bus: (_ for _ in ()).throw(AssertionError("BNO created")),
    )
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", lambda *_args, **_kwargs: None)
    assert sensor_sync_diagnostic.main([
        "--max-samples", "1", "--csv-output", str(tmp_path / "x.csv"),
        "--confirm-hardware",
    ]) == 1
    assert events == ["csv.close"]


def test_uart_open_failure_closes_uart_and_does_not_create_bno(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    distance = FakeDistance()
    distance.open = lambda: (_ for _ in ()).throw(OSError("open failed"))  # type: ignore[method-assign]
    monkeypatch.setattr(sensor_sync_diagnostic, "_DISTANCE_FACTORY", lambda **_kwargs: distance)
    monkeypatch.setattr(
        sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY",
        lambda _bus: (_ for _ in ()).throw(AssertionError("BNO created")),
    )
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", lambda *_args, **_kwargs: None)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    assert distance.close_calls == 1


@pytest.mark.parametrize("failed_stage", ["adapter", "device", "identity", "initialize"])
def test_bno_setup_stage_failures_close_acquired_resources(
    failed_stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    distance = FakeDistance()
    register_io = FakeRegisterIO()
    motion_device = FakeMotion()
    events: list[str] = []
    monkeypatch.setattr(sensor_sync_diagnostic, "_DISTANCE_FACTORY", lambda **_kwargs: distance)

    def adapter(_bus: int) -> FakeRegisterIO:
        events.append("adapter")
        if failed_stage == "adapter":
            raise OSError("adapter failed")
        return register_io

    def device(_io: object, **_kwargs: object) -> FakeMotion:
        events.append("device")
        if failed_stage == "device":
            raise OSError("device failed")
        if failed_stage == "identity":
            motion_device.read_identity = lambda: (_ for _ in ()).throw(OSError("identity failed"))  # type: ignore[method-assign]
        if failed_stage == "initialize":
            motion_device.initialize_ndof = lambda: (_ for _ in ()).throw(OSError("NDOF failed"))  # type: ignore[method-assign]
        return motion_device

    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY", adapter)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", device)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    assert distance.close_calls == 1
    if failed_stage == "adapter":
        assert events == ["adapter"] and register_io.close_calls == 0
    elif failed_stage == "device":
        assert register_io.close_calls == 1 and motion_device.close_calls == 0
    else:
        assert motion_device.close_calls == 1


def test_runtime_bno_failure_does_not_emit_another_matched_sample(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = [-1.0]

    def now() -> float:
        clock[0] += 0.1
        return clock[0]

    class RuntimeFailMotion(FakeMotion):
        def read_measurement(self) -> BNO055Measurement:
            if getattr(self, "reads", 0) == 1:
                raise OSError("runtime I2C")
            self.reads = getattr(self, "reads", 0) + 1
            return motion()

    distance = FakeDistance()
    install_fakes(monkeypatch, distance, RuntimeFailMotion())
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", now)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "2", "--confirm-hardware"]
    ) == 1
    output = capsys.readouterr().out
    assert "total_records=0" in output
    assert "matched_samples=0" in output


def test_csv_primary_and_all_cleanup_failures_are_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FailingCsv:
        def write(self, _sequence: int, _sample: object) -> None:
            raise CsvSinkError("CSV write")
        def close(self) -> None:
            raise CsvSinkError("CSV close")

    distance = FakeDistance(close_error=OSError("UART close"))
    motion_device = FakeMotion(close_error=OSError("BNO close"))
    install_fakes(monkeypatch, distance, motion_device)
    monkeypatch.setattr(sensor_sync_diagnostic, "_CSV_FACTORY", lambda _path: FailingCsv())
    assert sensor_sync_diagnostic.main([
        "--max-samples", "1", "--csv-output", str(tmp_path / "x.csv"),
        "--confirm-hardware",
    ]) == 1
    captured = capsys.readouterr()
    assert "CSV write" in captured.err
    assert "UART close" in captured.err
    assert "BNO close" in captured.err
    assert "CSV close" in captured.err
    assert "cleanup_completed=false" in captured.out


def test_real_bno_device_identity_failure_uses_safe_close_without_mode_write(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    class IdentityMismatchBus:
        def __init__(self) -> None:
            self.writes: list[tuple[int, int, int]] = []
            self.close_calls = 0

        def read_byte_data(self, _address: int, _register: int) -> int:
            return 0x00

        def read_i2c_block_data(
            self, _address: int, _register: int, _length: int
        ) -> list[int]:
            raise AssertionError("measurement read not expected")

        def write_byte_data(self, address: int, register: int, value: int) -> None:
            self.writes.append((address, register, value))

        def close(self) -> None:
            self.close_calls += 1

    bus = IdentityMismatchBus()
    distance = FakeDistance()
    monkeypatch.setattr(sensor_sync_diagnostic, "_DISTANCE_FACTORY", lambda **_kwargs: distance)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY", lambda _bus: bus)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", BNO055Device)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    assert bus.writes == []
    assert bus.close_calls == 1
    assert distance.close_calls == 1


def test_coordinator_clock_failure_is_nonzero_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    distance, motion_device = FakeDistance(), FakeMotion()
    install_fakes(monkeypatch, distance, motion_device)
    values = iter([0.0, 0.0, float("nan"), float("nan")])
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", lambda: next(values))
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "monotonic clock" in captured.err
    assert "sampling_elapsed_seconds=0.000" in captured.out
    assert distance.close_calls == 1 and motion_device.close_calls == 1


@pytest.mark.parametrize("failed_stage", ["csv", "uart", "adapter"])
def test_resource_event_order_for_early_factory_failures(
    failed_stage: str, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class EventSink:
        def close(self) -> None:
            events.extend(("csv.flush", "csv.close"))

    class EventDistance(FakeDistance):
        def open(self) -> None:
            events.append("uart.open")
        def close(self) -> None:
            events.append("uart.close")

    def csv_factory(_path: Path) -> EventSink:
        events.append("csv.create")
        if failed_stage == "csv":
            raise CsvSinkError("CSV create detail")
        return EventSink()

    def uart_factory(**_kwargs: object) -> EventDistance:
        events.append("uart.create")
        if failed_stage == "uart":
            raise OSError("UART create detail")
        return EventDistance()

    def adapter_factory(_bus: int) -> FakeRegisterIO:
        events.append("adapter.create")
        raise OSError("adapter create detail")

    monkeypatch.setattr(sensor_sync_diagnostic, "_CSV_FACTORY", csv_factory)
    monkeypatch.setattr(sensor_sync_diagnostic, "_DISTANCE_FACTORY", uart_factory)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY", adapter_factory)
    monkeypatch.setattr(
        sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("BNO created")),
    )
    assert sensor_sync_diagnostic.main([
        "--max-samples", "1", "--csv-output", str(tmp_path / "events.csv"),
        "--confirm-hardware",
    ]) == 1
    expected = {
        "csv": ["csv.create"],
        "uart": ["csv.create", "uart.create", "csv.flush", "csv.close"],
        "adapter": [
            "csv.create", "uart.create", "uart.open", "adapter.create",
            "uart.close", "csv.flush", "csv.close",
        ],
    }
    assert events == expected[failed_stage]


class EventRegisterIO:
    def __init__(
        self,
        events: list[str],
        *,
        scenario: str,
    ) -> None:
        self.events = events
        self.scenario = scenario
        self.config_writes = 0
        self.close_calls = 0

    def read_byte_data(self, _address: int, register: int) -> int:
        self.events.append(f"bus.read:0x{register:02x}")
        if self.scenario == "identity" and register == CHIP_ID:
            return 0x00
        if self.scenario in ("readiness_read", "cleanup_fail") and register == OPR_MODE:
            raise OSError("readiness read primary detail")
        identity = {
            CHIP_ID: EXPECTED_CHIP_ID,
            ACC_ID: EXPECTED_ACC_ID,
            MAG_ID: EXPECTED_MAG_ID,
            GYR_ID: EXPECTED_GYR_ID,
            SW_REV_ID_LSB: 0x08,
            SW_REV_ID_MSB: 0x03,
            BL_REV_ID: 0x15,
        }
        if register in identity:
            return identity[register]
        if register == OPR_MODE:
            return NDOF_MODE
        if register == SYS_STATUS:
            return 0x01 if self.scenario == "system_error" else 0x05
        if register == SYS_ERR:
            return 0x09 if self.scenario == "system_error" else 0x00
        return 0x00

    def read_i2c_block_data(
        self, _address: int, _register: int, _length: int
    ) -> list[int]:
        raise AssertionError("measurement read not expected")

    def write_byte_data(self, _address: int, register: int, value: int) -> None:
        self.events.append(f"bus.write:0x{register:02x}=0x{value:02x}")
        if register == OPR_MODE and value == CONFIG_MODE:
            self.config_writes += 1
            if self.scenario == "cleanup_fail" and self.config_writes == 2:
                raise OSError("cleanup CONFIGMODE detail")
        if self.scenario == "ndof_write" and register == OPR_MODE and value == NDOF_MODE:
            raise OSError("NDOF write primary detail")

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("bus.close")
        if self.scenario == "cleanup_fail":
            raise OSError("bus close cleanup detail")


@pytest.mark.parametrize(
    ("scenario", "primary_detail"),
    [
        ("identity", "chip ID mismatch: expected 0xa0, got 0x00"),
        ("ndof_write", "failed to write register 0x3d"),
        ("readiness_read", "failed to read register 0x3d"),
        ("system_error", "mode=0x0c status=0x01 error=0x09"),
    ],
)
def test_real_bno_partial_initialization_primary_details_and_event_order(
    scenario: str,
    primary_detail: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    bus = EventRegisterIO(events, scenario=scenario)

    class EventSink:
        def close(self) -> None:
            events.extend(("csv.flush", "csv.close"))

    class EventDistance(FakeDistance):
        def open(self) -> None:
            events.append("uart.open")
        def close(self) -> None:
            events.append("uart.close")

    monkeypatch.setattr(
        sensor_sync_diagnostic, "_CSV_FACTORY",
        lambda _path: events.append("csv.create") or EventSink(),
    )
    monkeypatch.setattr(
        sensor_sync_diagnostic, "_DISTANCE_FACTORY",
        lambda **_kwargs: events.append("uart.create") or EventDistance(),
    )
    monkeypatch.setattr(
        sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY",
        lambda _bus: events.append("adapter.create") or bus,
    )

    def device_factory(io: object, **kwargs: object) -> BNO055Device:
        events.append("bno.create")
        return BNO055Device(io, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", device_factory)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", lambda: 1.0)
    monkeypatch.setattr(sensor_sync_diagnostic, "_SLEEP", lambda _seconds: None)
    assert sensor_sync_diagnostic.main([
        "--max-samples", "1", "--csv-output", str(tmp_path / "partial.csv"),
        "--confirm-hardware",
    ]) == 1
    captured = capsys.readouterr()
    assert primary_detail in captured.err
    assert events[:5] == [
        "csv.create", "uart.create", "uart.open", "adapter.create", "bno.create",
    ]
    assert events[-3:] == ["bus.close", "csv.flush", "csv.close"]
    assert events.count("uart.close") == 1
    assert events.count("bus.close") == 1
    assert bus.close_calls == 1
    if scenario == "identity":
        assert "bus.write:0x3d=0x00" not in events
    else:
        assert events.count("bus.write:0x3d=0x00") == 2


def test_primary_and_configmode_and_bus_cleanup_details_are_all_preserved(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    events: list[str] = []
    bus = EventRegisterIO(events, scenario="cleanup_fail")

    class EventDistance(FakeDistance):
        def close(self) -> None:
            events.append("uart.close")

    monkeypatch.setattr(sensor_sync_diagnostic, "_DISTANCE_FACTORY", lambda **_kwargs: EventDistance())
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_ADAPTER_FACTORY", lambda _bus: bus)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MOTION_DEVICE_FACTORY", BNO055Device)
    monkeypatch.setattr(sensor_sync_diagnostic, "_MONOTONIC", lambda: 1.0)
    monkeypatch.setattr(sensor_sync_diagnostic, "_SLEEP", lambda _seconds: None)
    assert sensor_sync_diagnostic.main(
        ["--max-samples", "1", "--confirm-hardware"]
    ) == 1
    captured = capsys.readouterr()
    assert "failed to read register 0x3d" in captured.err
    assert "failed to write register 0x3d" in captured.err
    assert "bus close cleanup detail" in captured.err
    assert "cleanup_completed=false" in captured.out
    assert "completed successfully" not in captured.out
    assert events.index("uart.close") < events.index("bus.close")
    assert bus.config_writes == 2
    assert bus.close_calls == 1
