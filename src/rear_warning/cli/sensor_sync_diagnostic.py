"""Confirmation-gated bounded TFMini Plus/BNO055 time-association CLI."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, Protocol, TextIO

from rear_warning.synchronization import (
    DistanceSamplingError, MotionSamplingError, SamplingSchedule,
    SamplingStatistics, SensorSamplingCoordinator, SensorSamplingError,
    SynchronizedSensorSample,
)
from rear_warning.synchronization.csv_sink import CsvSinkError, SynchronizedSampleCsvSink

_SUCCESS = 0
_DEVICE_ERROR = 1
_USAGE_ERROR = 2
_SCHEDULER_POLL_SECONDS = 0.001


class _DistanceDevice(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read_measurements(self) -> list[object]: ...


class _MotionDevice(Protocol):
    def read_identity(self) -> object: ...
    def initialize_ndof(self) -> tuple[int, int, int]: ...
    def read_measurement(self) -> object: ...
    def close(self) -> None: ...


class _RegisterIO(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Options:
    port: str
    baudrate: int
    uart_timeout: float
    i2c_bus: int
    bno055_address: int
    bno055_interval: float
    maximum_bno055_age: float
    data_ready_timeout: float
    duration: float | None
    max_samples: int | None
    csv_output: Path | None
    confirm_hardware: bool


@dataclass(slots=True)
class _RunCounters:
    parser_serial_errors: int = 0
    bno055_errors: int = 0
    csv_errors: int = 0
    interrupted: bool = False


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


_DISTANCE_FACTORY: Callable[..., _DistanceDevice] | None = None
_MOTION_ADAPTER_FACTORY: Callable[[int], _RegisterIO] | None = None
_MOTION_DEVICE_FACTORY: Callable[..., _MotionDevice] | None = None
_CSV_FACTORY: Callable[[Path], SynchronizedSampleCsvSink] = SynchronizedSampleCsvSink
_MONOTONIC: Callable[[], float] = time.monotonic
_SLEEP: Callable[[float], None] = time.sleep


def _load_hardware_factories() -> tuple[
    Callable[..., _DistanceDevice],
    Callable[[int], _RegisterIO],
    Callable[..., _MotionDevice],
]:
    from rear_warning.sensors.bno055 import BNO055Device
    from rear_warning.sensors.bno055.hardware import load_smbus_register_io
    from rear_warning.sensors.tfmini_plus.serial_device import TFMiniPlusSerialDevice

    return TFMiniPlusSerialDevice, load_smbus_register_io(), BNO055Device


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def _non_negative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative and finite")
    return parsed


def _address(value: str) -> int:
    try:
        parsed = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 0x28 or 0x29") from exc
    if parsed not in (0x28, 0x29):
        raise argparse.ArgumentTypeError("must be 0x28 or 0x29")
    return parsed


def _build_parser() -> _Parser:
    parser = _Parser(
        prog="sensor-sync-diagnostic",
        description="Bounded causal TFMini Plus/BNO055 time association.",
    )
    parser.add_argument("--port", default="/dev/ttyAMA0")
    parser.add_argument("--baudrate", type=_positive_int, default=115200)
    parser.add_argument(
        "--uart-timeout", type=_positive_float, default=0.02,
        help="positive UART timeout in seconds; must not exceed --bno055-interval",
    )
    parser.add_argument("--i2c-bus", type=_non_negative_int, default=1)
    parser.add_argument("--bno055-address", type=_address, default=0x29)
    parser.add_argument("--bno055-interval", type=_positive_float, default=0.1)
    parser.add_argument("--maximum-bno055-age", type=_positive_float, default=0.2)
    parser.add_argument("--data-ready-timeout", type=_positive_float, default=2.0)
    parser.add_argument(
        "--duration", type=_positive_float,
        help="sampling seconds after BNO055 data-ready completes",
    )
    parser.add_argument(
        "--max-samples", type=_positive_int,
        help="maximum successfully committed records, matched and unmatched",
    )
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--confirm-hardware", action="store_true")
    return parser


def _parse(parser: _Parser, argv: Sequence[str] | None) -> _Options:
    values = parser.parse_args(None if argv is None else list(argv))
    if values.duration is None and values.max_samples is None:
        raise _UsageError("at least one of --duration or --max-samples is required")
    if not str(values.port).strip():
        raise _UsageError("--port must not be empty")
    if values.uart_timeout > values.bno055_interval:
        raise _UsageError("--uart-timeout must not exceed --bno055-interval")
    return _Options(
        str(values.port), int(values.baudrate), float(values.uart_timeout),
        int(values.i2c_bus), int(values.bno055_address),
        float(values.bno055_interval), float(values.maximum_bno055_age),
        float(values.data_ready_timeout),
        None if values.duration is None else float(values.duration),
        None if values.max_samples is None else int(values.max_samples),
        values.csv_output, bool(values.confirm_hardware),
    )


def _maximum_iterations(options: _Options) -> int:
    candidates: list[int] = []
    if options.duration is not None:
        candidates.append(math.ceil(options.duration / _SCHEDULER_POLL_SECONDS) + 1)
    if options.max_samples is not None:
        candidates.append(options.max_samples * 1000 + 1)
    return min(candidates)


def _print_plan(options: _Options, output: TextIO) -> None:
    print(f"uart_port={options.port}", file=output)
    print(f"uart_baudrate={options.baudrate}", file=output)
    print(f"uart_timeout_seconds={options.uart_timeout:.3f}", file=output)
    print(f"i2c_bus={options.i2c_bus}", file=output)
    print(f"bno055_address=0x{options.bno055_address:02x}", file=output)
    print(f"bno055_interval_seconds={options.bno055_interval:.3f}", file=output)
    print(f"maximum_bno055_age_seconds={options.maximum_bno055_age:.3f}", file=output)
    print(f"data_ready_timeout_seconds={options.data_ready_timeout:.3f}", file=output)
    print(f"duration_seconds={options.duration if options.duration is not None else 'disabled'}", file=output)
    print(f"maximum_committed_records={options.max_samples if options.max_samples is not None else 'disabled'}", file=output)
    print("matching=causal_latest_motion_not_newer_than_distance", file=output)
    print(f"csv_output={options.csv_output or 'disabled'}", file=output)
    print("planned_hardware=opens_UART_and_writes_BNO055_mode_registers", file=output)


def _print_sample(sequence: int, sample: SynchronizedSensorSample, output: TextIO) -> None:
    print(
        f"sample sequence={sequence} status={sample.status.value} "
        f"distance_timestamp={sample.distance_timestamp:.6f} "
        f"motion_timestamp={sample.motion_timestamp if sample.motion_timestamp is not None else 'none'} "
        f"motion_age_seconds={sample.motion_age_seconds if sample.motion_age_seconds is not None else 'none'} "
        f"distance_cm={sample.distance_measurement.distance_cm}",
        file=output,
    )


def _print_summary(
    stats: SamplingStatistics,
    counters: _RunCounters,
    sampling_elapsed: float,
    total_elapsed: float,
    cleanup_completed: bool,
    output: TextIO,
) -> None:
    def rate(count: int) -> float:
        return count / sampling_elapsed if sampling_elapsed > 0 else 0.0

    age_average = stats.motion_age_average
    print("sensor_sync_summary", file=output)
    for name in (
        "valid_distance_measurements", "bno055_measurements", "matched_samples",
        "no_motion_count", "stale_motion_count", "future_motion_count",
        "invalid_distance_count", "invalid_motion_count", "uart_empty_reads",
        "startup_motion_discards", "startup_motion_reads", "total_records",
        "missed_bno055_periods",
    ):
        print(f"{name}={getattr(stats, name)}", file=output)
    print(f"parser_serial_errors={counters.parser_serial_errors}", file=output)
    print(f"bno055_errors={counters.bno055_errors}", file=output)
    print(f"csv_errors={counters.csv_errors}", file=output)
    print(f"motion_age_minimum={stats.motion_age_minimum if stats.motion_age_minimum is not None else 'none'}", file=output)
    print(f"motion_age_maximum={stats.motion_age_maximum if stats.motion_age_maximum is not None else 'none'}", file=output)
    print(f"motion_age_average={age_average if age_average is not None else 'none'}", file=output)
    print(f"bno055_deadline_lateness_maximum={stats.bno055_deadline_lateness_maximum}", file=output)
    print(f"bno055_deadline_lateness_average={stats.bno055_deadline_lateness_average if stats.bno055_deadline_lateness_average is not None else 'none'}", file=output)
    print(f"distance_rate_hz={rate(stats.valid_distance_measurements):.3f}", file=output)
    print(f"bno055_rate_hz={rate(stats.bno055_measurements):.3f}", file=output)
    print(f"matched_rate_hz={rate(stats.matched_samples):.3f}", file=output)
    print(f"sampling_elapsed_seconds={sampling_elapsed:.3f}", file=output)
    print(f"total_elapsed_seconds={total_elapsed:.3f}", file=output)
    print(f"interrupted={str(counters.interrupted).lower()}", file=output)
    print(f"cleanup_completed={str(cleanup_completed).lower()}", file=output)


def _run(options: _Options, output: TextIO, error_output: TextIO) -> int:
    counters = _RunCounters()
    stats = SamplingStatistics()
    started = _MONOTONIC()
    sampling_elapsed = 0.0
    total_elapsed = 0.0
    exit_code = _SUCCESS
    cleanup_completed = False
    distance: _DistanceDevice | None = None
    motion: _MotionDevice | None = None
    register_io: _RegisterIO | None = None
    csv_sink: SynchronizedSampleCsvSink | None = None
    coordinator: SensorSamplingCoordinator | None = None
    try:
        if options.csv_output is not None:
            csv_sink = _CSV_FACTORY(options.csv_output)
        factories = (
            (_DISTANCE_FACTORY, _MOTION_ADAPTER_FACTORY, _MOTION_DEVICE_FACTORY)
            if all((_DISTANCE_FACTORY, _MOTION_ADAPTER_FACTORY, _MOTION_DEVICE_FACTORY))
            else _load_hardware_factories()
        )
        distance_factory, adapter_factory, motion_factory = factories
        assert distance_factory is not None and adapter_factory is not None and motion_factory is not None
        distance = distance_factory(
            port=options.port, baudrate=options.baudrate, timeout=options.uart_timeout
        )
        try:
            distance.open()
        except Exception as exc:
            raise DistanceSamplingError("TFMini serial open failed") from exc
        try:
            register_io = adapter_factory(options.i2c_bus)
            motion = motion_factory(
                register_io,
                address=options.bno055_address,
                monotonic=_MONOTONIC,
                sleep=_SLEEP,
            )
            motion.read_identity()
            mode, status, error = motion.initialize_ndof()
        except Exception as exc:
            raise MotionSamplingError(f"BNO055 setup failed: {exc}") from exc
        print(f"bno055_readiness=0x{mode:02x}/0x{status:02x}/0x{error:02x}", file=output)
        schedule = SamplingSchedule(
            bno055_interval_seconds=options.bno055_interval,
            maximum_bno055_age_seconds=options.maximum_bno055_age,
            data_ready_timeout_seconds=options.data_ready_timeout,
            maximum_iterations=_maximum_iterations(options),
        )
        coordinator = SensorSamplingCoordinator(
            distance, motion, schedule=schedule, monotonic=_MONOTONIC, sleep=_SLEEP
        )
        stats, sampling_elapsed = coordinator.run(
            duration_seconds=options.duration,
            maximum_samples=options.max_samples,
            sink=csv_sink,
            on_sample=lambda sequence, sample: _print_sample(sequence, sample, output),
        )
        if stats.matched_samples == 0:
            print("sensor sync error: zero matched samples", file=error_output)
            exit_code = _DEVICE_ERROR
    except KeyboardInterrupt:
        counters.interrupted = True
        exit_code = 130
        print("sensor sync diagnostic interrupted by user (Ctrl+C)", file=error_output)
    except DistanceSamplingError as exc:
        counters.parser_serial_errors += 1
        exit_code = _DEVICE_ERROR
        print(f"sensor sync serial error: {exc}", file=error_output)
    except MotionSamplingError as exc:
        counters.bno055_errors += 1
        exit_code = _DEVICE_ERROR
        print(f"sensor sync BNO055 error: {exc}", file=error_output)
    except CsvSinkError as exc:
        counters.csv_errors += 1
        exit_code = _DEVICE_ERROR
        print(f"sensor sync CSV error: {exc}", file=error_output)
    except SensorSamplingError as exc:
        counters.bno055_errors += 1
        exit_code = _DEVICE_ERROR
        print(f"sensor sync error: {exc}", file=error_output)
    except Exception as exc:
        exit_code = _DEVICE_ERROR
        print(f"sensor sync setup error: {exc}", file=error_output)
    finally:
        if coordinator is not None:
            stats = coordinator.statistics
            sampling_elapsed = coordinator.sampling_elapsed_seconds
        errors: list[str] = []
        if distance is not None:
            try:
                distance.close()
            except Exception as exc:
                errors.append(f"UART cleanup: {exc}")
        if motion is not None:
            try:
                motion.close()
            except Exception as exc:
                errors.append(f"BNO055 cleanup: {exc}")
        elif register_io is not None:
            try:
                register_io.close()
            except Exception as exc:
                errors.append(f"SMBus cleanup: {exc}")
        if csv_sink is not None:
            try:
                csv_sink.close()
            except Exception as exc:
                counters.csv_errors += 1
                errors.append(f"CSV cleanup: {exc}")
        cleanup_completed = not errors
        if errors:
            exit_code = _DEVICE_ERROR
            for detail in errors:
                print(f"sensor sync cleanup error: {detail}", file=error_output)
        try:
            total_elapsed = max(_MONOTONIC() - started, 0.0)
        except Exception:
            exit_code = _DEVICE_ERROR
        _print_summary(
            stats, counters, sampling_elapsed, total_elapsed,
            cleanup_completed, output,
        )
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        options = _parse(parser, argv)
    except _UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return _USAGE_ERROR
    _print_plan(options, sys.stdout)
    if not options.confirm_hardware:
        parser.print_usage(sys.stderr)
        print(
            f"{parser.prog}: error: --confirm-hardware is required before UART/I2C/CSV access",
            file=sys.stderr,
        )
        return _USAGE_ERROR
    return _run(options, sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
