"""Confirmation-gated, bounded BNO055 physical diagnostic."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import NoReturn, Protocol, TextIO

from rear_warning.sensors.bno055 import (
    BNO055Calibration, BNO055Device, BNO055Error, BNO055Identity,
    BNO055Measurement, BNO055RuntimeStateError, MeasurementQualityThresholds,
    assess_measurement_quality,
)
from rear_warning.sensors.bno055.hardware import (
    BNO055HardwareDependenciesNotInstalled, load_smbus_register_io,
)

_SUCCESS = 0
_DEVICE_ERROR = 1
_USAGE_ERROR = 2


class _Device(Protocol):
    address: int
    def read_identity(self) -> BNO055Identity: ...
    def initialize_ndof(self) -> tuple[int, int, int]: ...
    def read_calibration(self) -> BNO055Calibration: ...
    def read_measurement(self) -> BNO055Measurement: ...
    def close(self) -> None: ...


class _AdapterFactory(Protocol):
    def __call__(self, bus: int) -> _RegisterIO: ...


class _RegisterIO(Protocol):
    def close(self) -> None: ...


class _DeviceFactory(Protocol):
    def __call__(self, register_io: _RegisterIO, *, address: int) -> _Device: ...


@dataclass(frozen=True, slots=True)
class _Options:
    bus: int
    address: int
    sample_interval: float
    duration: float | None
    max_samples: int | None
    data_ready_timeout: float
    confirm_hardware: bool


@dataclass(slots=True)
class _Stats:
    valid_samples: int = 0
    read_errors: int = 0
    runtime_state_errors: int = 0
    discarded_startup_samples: int = 0
    runtime_data_errors: int = 0
    interrupted: bool = False


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


_ADAPTER_FACTORY: _AdapterFactory | None = None
_DEVICE_FACTORY: _DeviceFactory = BNO055Device
_MONOTONIC: Callable[[], float] = time.monotonic
_SLEEP: Callable[[float], None] = time.sleep
_DATA_READY_POLL_INTERVAL_SECONDS = 0.02
_QUALITY_THRESHOLDS = MeasurementQualityThresholds()


def _positive_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return result


def _non_negative_int(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if result < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return result


def _positive_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return result


def _non_negative_float(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(result) or result < 0:
        raise argparse.ArgumentTypeError("must be a non-negative finite number")
    return result


def _address(value: str) -> int:
    try:
        result = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 0x28 or 0x29") from exc
    if result not in (0x28, 0x29):
        raise argparse.ArgumentTypeError("must be 0x28 or 0x29")
    return result


def _build_parser() -> _Parser:
    parser = _Parser(
        prog="bno055-diagnostic",
        description="Run a bounded BNO055 NDOF diagnostic after explicit confirmation.",
    )
    parser.add_argument("--bus", type=_non_negative_int, default=1)
    parser.add_argument("--address", type=_address, default=0x29)
    parser.add_argument("--sample-interval", type=_non_negative_float, default=0.1)
    parser.add_argument("--duration", type=_positive_float)
    parser.add_argument("--max-samples", type=_positive_int)
    parser.add_argument("--data-ready-timeout", type=_positive_float, default=2.0)
    parser.add_argument("--confirm-hardware", action="store_true")
    return parser


def _parse(parser: _Parser, argv: Sequence[str] | None) -> _Options:
    values = parser.parse_args(None if argv is None else list(argv))
    if values.duration is None and values.max_samples is None:
        raise _UsageError("at least one of --duration or --max-samples is required")
    return _Options(
        bus=int(values.bus), address=int(values.address),
        sample_interval=float(values.sample_interval),
        duration=None if values.duration is None else float(values.duration),
        max_samples=None if values.max_samples is None else int(values.max_samples),
        data_ready_timeout=float(values.data_ready_timeout),
        confirm_hardware=bool(values.confirm_hardware),
    )


def _print_plan(options: _Options, *, output: TextIO) -> None:
    print(f"bus={options.bus}", file=output)
    print(f"address=0x{options.address:02x}", file=output)
    print("requested_mode=NDOF (0x0c)", file=output)
    print(f"data_ready_timeout_seconds={options.data_ready_timeout:.3f}", file=output)
    print(
        "data_ready_maximum_attempts="
        f"{math.ceil(options.data_ready_timeout / _DATA_READY_POLL_INTERVAL_SECONDS) + 1}",
        file=output,
    )
    print("planned_writes=CONFIGMODE,page_0,normal_power,default_units,NDOF,cleanup_CONFIGMODE", file=output)


def _print_identity(identity: BNO055Identity, *, output: TextIO) -> None:
    print(
        f"identity address=0x{identity.address:02x} chip=0x{identity.chip_id:02x} "
        f"acc=0x{identity.accelerometer_id:02x} mag=0x{identity.magnetometer_id:02x} "
        f"gyr=0x{identity.gyroscope_id:02x} software=0x{identity.software_revision:04x} "
        f"bootloader=0x{identity.bootloader_revision:02x}",
        file=output,
    )


def _print_measurement(measurement: BNO055Measurement, *, output: TextIO) -> None:
    c = measurement.calibration
    print(
        "measurement "
        f"timestamp={measurement.monotonic_timestamp:.6f} "
        f"euler=({measurement.euler.heading:.3f},{measurement.euler.roll:.3f},{measurement.euler.pitch:.3f}) "
        f"quaternion=({measurement.quaternion.w:.6f},{measurement.quaternion.x:.6f},{measurement.quaternion.y:.6f},{measurement.quaternion.z:.6f}) "
        f"linear_acceleration=({measurement.linear_acceleration.x:.3f},{measurement.linear_acceleration.y:.3f},{measurement.linear_acceleration.z:.3f}) "
        f"gravity=({measurement.gravity.x:.3f},{measurement.gravity.y:.3f},{measurement.gravity.z:.3f}) "
        f"temperature_c={measurement.temperature_c} "
        f"calibration=({c.system},{c.gyroscope},{c.accelerometer},{c.magnetometer}) "
        f"operation_mode=0x{measurement.operation_mode:02x} "
        f"system_status=0x{measurement.system_status:02x} "
        f"system_error=0x{measurement.system_error:02x}",
        file=output,
    )


def _run(options: _Options, *, output: TextIO, error_output: TextIO) -> int:
    stats = _Stats()
    total_started = _MONOTONIC()
    sampling_started: float | None = None
    sampling_finished: float | None = None
    data_ready_started: float | None = None
    first_valid_sample_wait = 0.0
    device: _Device | None = None
    register_io: _RegisterIO | None = None
    exit_code = _SUCCESS
    cleanup_completed = False
    try:
        adapter_factory = _ADAPTER_FACTORY or load_smbus_register_io()
        register_io = adapter_factory(options.bus)
        device = _DEVICE_FACTORY(register_io, address=options.address)
        identity = device.read_identity()
        _print_identity(identity, output=output)
        operation_mode, system_status, system_error = device.initialize_ndof()
        calibration = device.read_calibration()
        print(f"operation_mode=0x{operation_mode:02x}", file=output)
        print(f"system_status=0x{system_status:02x}", file=output)
        print(f"system_error=0x{system_error:02x}", file=output)
        print(f"calibration=({calibration.system},{calibration.gyroscope},{calibration.accelerometer},{calibration.magnetometer})", file=output)
        data_ready_started = _MONOTONIC()
        data_ready_deadline = data_ready_started + options.data_ready_timeout
        maximum_data_ready_attempts = (
            math.ceil(
                options.data_ready_timeout / _DATA_READY_POLL_INTERVAL_SECONDS
            )
            + 1
        )
        first_measurement: BNO055Measurement | None = None
        last_startup_issues: tuple[str, ...] = ()
        for attempt in range(1, maximum_data_ready_attempts + 1):
            try:
                candidate = device.read_measurement()
            except BNO055RuntimeStateError:
                stats.runtime_state_errors += 1
                raise
            except BNO055Error:
                stats.read_errors += 1
                raise
            assessment = assess_measurement_quality(candidate, _QUALITY_THRESHOLDS)
            if assessment.state_issues:
                stats.runtime_state_errors += 1
                raise BNO055RuntimeStateError(
                    operation_mode=candidate.operation_mode,
                    system_status=candidate.system_status,
                    system_error=candidate.system_error,
                )
            if not assessment.data_issues:
                first_measurement = candidate
                break
            stats.discarded_startup_samples += 1
            last_startup_issues = assessment.data_issues
            print(
                f"discarded_startup_sample attempt={attempt} "
                f"reasons={'; '.join(assessment.data_issues)}",
                file=output,
            )
            now = _MONOTONIC()
            if now >= data_ready_deadline or attempt >= maximum_data_ready_attempts:
                raise BNO055Error(
                    "BNO055 data readiness timed out after "
                    f"{options.data_ready_timeout:.3f}s and {attempt} attempts: "
                    f"last_reasons={'; '.join(last_startup_issues)}"
                )
            _SLEEP(min(_DATA_READY_POLL_INTERVAL_SECONDS, data_ready_deadline - now))

        if first_measurement is None:
            raise BNO055Error("BNO055 data readiness ended without a valid measurement")
        sampling_started = _MONOTONIC()
        first_valid_sample_wait = max(sampling_started - data_ready_started, 0.0)
        _print_measurement(first_measurement, output=output)
        stats.valid_samples = 1
        if options.sample_interval and (
            options.max_samples is None or stats.valid_samples < options.max_samples
        ):
            _SLEEP(options.sample_interval)

        while options.max_samples is None or stats.valid_samples < options.max_samples:
            if (
                options.duration is not None
                and _MONOTONIC() - sampling_started >= options.duration
            ):
                break
            try:
                measurement = device.read_measurement()
            except BNO055RuntimeStateError as exc:
                stats.runtime_state_errors += 1
                print(
                    "BNO055 runtime state error: "
                    f"sample_index={stats.valid_samples + 1} {exc}",
                    file=error_output,
                )
                exit_code = _DEVICE_ERROR
                break
            except BNO055Error:
                stats.read_errors += 1
                raise
            assessment = assess_measurement_quality(measurement, _QUALITY_THRESHOLDS)
            if assessment.state_issues:
                stats.runtime_state_errors += 1
                print(
                    "BNO055 runtime state error: "
                    f"sample_index={stats.valid_samples + 1} "
                    f"{'; '.join(assessment.state_issues)}",
                    file=error_output,
                )
                exit_code = _DEVICE_ERROR
                break
            if assessment.data_issues:
                stats.runtime_data_errors += 1
                print(
                    "BNO055 runtime data error: "
                    f"sample_index={stats.valid_samples + 1} "
                    f"reasons={'; '.join(assessment.data_issues)}",
                    file=error_output,
                )
                exit_code = _DEVICE_ERROR
                break
            _print_measurement(measurement, output=output)
            stats.valid_samples += 1
            if (
                options.sample_interval
                and (
                    options.max_samples is None
                    or stats.valid_samples < options.max_samples
                )
            ):
                _SLEEP(options.sample_interval)
    except KeyboardInterrupt:
        stats.interrupted = True
        print("BNO055 diagnostic interrupted by user (Ctrl+C)", file=output)
        exit_code = 130
    except BNO055HardwareDependenciesNotInstalled as exc:
        print(str(exc), file=error_output)
        exit_code = _DEVICE_ERROR
    except BNO055RuntimeStateError as exc:
        print(
            "BNO055 runtime state error: "
            f"sample_index={stats.valid_samples + 1} {exc}",
            file=error_output,
        )
        exit_code = _DEVICE_ERROR
    except BNO055Error as exc:
        print(f"BNO055 diagnostic error: {exc}", file=error_output)
        exit_code = _DEVICE_ERROR
    except Exception as exc:
        print(f"BNO055 adapter error: {exc}", file=error_output)
        exit_code = _DEVICE_ERROR
    finally:
        if sampling_started is not None:
            sampling_finished = _MONOTONIC()
        if device is not None:
            try:
                device.close()
                cleanup_completed = True
            except Exception as exc:
                print(f"BNO055 cleanup error: {exc}", file=error_output)
                exit_code = _DEVICE_ERROR
        elif register_io is not None:
            try:
                register_io.close()
                cleanup_completed = True
            except Exception as exc:
                print(f"BNO055 adapter cleanup error: {exc}", file=error_output)
                exit_code = _DEVICE_ERROR
        finished = _MONOTONIC()
        total_elapsed = max(finished - total_started, 0.0)
        sampling_elapsed = (
            0.0
            if sampling_started is None or sampling_finished is None
            else max(sampling_finished - sampling_started, 0.0)
        )
        if exit_code == _SUCCESS and stats.valid_samples == 0:
            print(
                "BNO055 diagnostic error: no valid samples were collected",
                file=error_output,
            )
            exit_code = _DEVICE_ERROR
        rate = (
            stats.valid_samples / sampling_elapsed
            if sampling_elapsed > 0
            else 0.0
        )
        print("diagnostic_summary", file=output)
        print(f"valid_samples={stats.valid_samples}", file=output)
        print(f"read_errors={stats.read_errors}", file=output)
        print(f"runtime_state_errors={stats.runtime_state_errors}", file=output)
        print(f"discarded_startup_samples={stats.discarded_startup_samples}", file=output)
        print(f"runtime_data_errors={stats.runtime_data_errors}", file=output)
        print(f"first_valid_sample_wait_seconds={first_valid_sample_wait:.3f}", file=output)
        print(f"data_ready_timeout_seconds={options.data_ready_timeout:.3f}", file=output)
        print(
            "data_ready_maximum_attempts="
            f"{math.ceil(options.data_ready_timeout / _DATA_READY_POLL_INTERVAL_SECONDS) + 1}",
            file=output,
        )
        print(
            "quality_thresholds="
            f"quaternion_norm[{_QUALITY_THRESHOLDS.quaternion_norm_min:.3f},"
            f"{_QUALITY_THRESHOLDS.quaternion_norm_max:.3f}] "
            f"gravity_norm_m_s2[{_QUALITY_THRESHOLDS.gravity_norm_min_m_s2:.3f},"
            f"{_QUALITY_THRESHOLDS.gravity_norm_max_m_s2:.3f}]",
            file=output,
        )
        print(f"interrupted={str(stats.interrupted).lower()}", file=output)
        print(f"sampling_elapsed_seconds={sampling_elapsed:.3f}", file=output)
        print(f"total_elapsed_seconds={total_elapsed:.3f}", file=output)
        print(f"average_rate_hz={rate:.3f}", file=output)
        print(f"cleanup_completed={str(cleanup_completed).lower()}", file=output)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        options = _parse(parser, argv)
    except _UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return _USAGE_ERROR
    _print_plan(options, output=sys.stdout)
    if not options.confirm_hardware:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: --confirm-hardware is required before I2C access", file=sys.stderr)
        return _USAGE_ERROR
    return _run(options, output=sys.stdout, error_output=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
