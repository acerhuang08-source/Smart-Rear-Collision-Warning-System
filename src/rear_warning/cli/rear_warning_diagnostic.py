"""Bounded physical TFMini Plus to LED warning-chain diagnostic."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import NoReturn, Protocol, TextIO

from rear_warning.outputs import LedPins, ThreeColorLedDriver
from rear_warning.outputs.hardware import load_gpiozero_output_factory
from rear_warning.sensors.tfmini_plus import (
    TFMiniPlusMeasurement,
    TFMiniPlusSerialDevice,
    TFMiniPlusSerialError,
)
from rear_warning.warning import DistanceThresholds, WarningPolicy
from rear_warning.warning.controller import WarningController
from rear_warning.warning.models import WarningState

_SUCCESS = 0
_HARDWARE_ERROR = 1
_USAGE_ERROR = 2
_INTERRUPTED = 130
_DEFAULT_PORT = "/dev/ttyAMA0"
_DEFAULT_BAUDRATE = 115200
_DEFAULT_TIMEOUT_SECONDS = 0.1


class _DiagnosticDevice(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def read_measurements(self) -> list[TFMiniPlusMeasurement]: ...


class _DeviceFactory(Protocol):
    def __call__(
        self, *, port: str, baudrate: int, timeout: float | None
    ) -> _DiagnosticDevice: ...


class _ClosableOutput(Protocol):
    def write(self, pin: int, active: bool) -> None: ...

    def close(self) -> None: ...


class _OutputFactory(Protocol):
    def __call__(self, pins: LedPins, *, chip: int) -> _ClosableOutput: ...


@dataclass(frozen=True, slots=True)
class _Options:
    port: str
    baudrate: int
    timeout: float
    duration: float | None
    max_samples: int | None
    pins: LedPins
    gpio_chip: int
    confirm_hardware: bool


@dataclass(slots=True)
class _Stats:
    valid_measurements: int = 0
    empty_reads: int = 0
    parser_errors: int = 0
    sensor_faults: int = 0
    state_transitions: int = 0
    state_counts: dict[WarningState, int] = field(
        default_factory=lambda: {state: 0 for state in WarningState}
    )
    last_valid_measurement_at: datetime | None = None
    final_state: WarningState = WarningState.SENSOR_FAULT


class _UsageError(ValueError):
    """Argument error converted to a return code by :func:`main`."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


def _local_now() -> datetime:
    return datetime.now().astimezone()


_DEVICE_FACTORY: _DeviceFactory = TFMiniPlusSerialDevice
_OUTPUT_FACTORY: _OutputFactory | None = None
_MONOTONIC: Callable[[], float] = time.monotonic
_WALL_CLOCK: Callable[[], datetime] = _local_now


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


def _finite_float(value: str, *, positive: bool) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or (parsed <= 0 if positive else parsed < 0):
        qualifier = "positive " if positive else "non-negative "
        raise argparse.ArgumentTypeError(f"must be a {qualifier}finite number")
    return parsed


def _build_parser() -> _Parser:
    parser = _Parser(
        prog="rear-warning-diagnostic",
        description="Run the bounded physical TFMini Plus to LED warning chain.",
    )
    parser.add_argument("--port", default=_DEFAULT_PORT)
    parser.add_argument("--baudrate", type=_positive_int, default=_DEFAULT_BAUDRATE)
    parser.add_argument(
        "--timeout",
        type=lambda value: _finite_float(value, positive=False),
        default=_DEFAULT_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--duration", type=lambda value: _finite_float(value, positive=True)
    )
    parser.add_argument("--max-samples", type=_positive_int)
    parser.add_argument("--red-pin", type=_non_negative_int, default=17)
    parser.add_argument("--yellow-pin", type=_non_negative_int, default=27)
    parser.add_argument("--green-pin", type=_non_negative_int, default=22)
    parser.add_argument("--gpio-chip", type=_non_negative_int, default=0)
    parser.add_argument("--confirm-hardware", action="store_true")
    return parser


def _parse_options(parser: _Parser, argv: Sequence[str] | None) -> _Options:
    values = parser.parse_args(None if argv is None else list(argv))
    if values.duration is None and values.max_samples is None:
        raise _UsageError("at least one of --duration or --max-samples is required")
    if not str(values.port).strip():
        raise _UsageError("--port must not be empty")
    try:
        pins = LedPins(values.red_pin, values.yellow_pin, values.green_pin)
    except (TypeError, ValueError) as exc:
        raise _UsageError(str(exc)) from exc
    return _Options(
        port=str(values.port),
        baudrate=int(values.baudrate),
        timeout=float(values.timeout),
        duration=None if values.duration is None else float(values.duration),
        max_samples=(
            None if values.max_samples is None else int(values.max_samples)
        ),
        pins=pins,
        gpio_chip=int(values.gpio_chip),
        confirm_hardware=bool(values.confirm_hardware),
    )


def _print_configuration(options: _Options, *, output: TextIO) -> None:
    thresholds = DistanceThresholds()
    print("rear_warning_diagnostic_configuration", file=output)
    print(f"uart_port={options.port}", file=output)
    print(f"baudrate={options.baudrate}", file=output)
    print(f"timeout_seconds={options.timeout}", file=output)
    print(f"gpio_chip={options.gpio_chip}", file=output)
    print(
        "led_pins_bcm="
        f"red:{options.pins.red},yellow:{options.pins.yellow},"
        f"green:{options.pins.green}",
        file=output,
    )
    print(
        "led_mapping="
        "SAFE:green,WARNING:yellow,DANGER:red,SENSOR_FAULT:red+yellow",
        file=output,
    )
    print(
        "thresholds_m="
        f"DANGER:(0,{thresholds.danger_below_m}),"
        f"WARNING:[{thresholds.danger_below_m},{thresholds.safe_above_m}],"
        f"SAFE:({thresholds.safe_above_m},{thresholds.maximum_valid_m}],"
        "SENSOR_FAULT:invalid_or_out_of_range",
        file=output,
    )


def _record_state(
    state: WarningState,
    measurement: TFMiniPlusMeasurement | None,
    stats: _Stats,
    *,
    output: TextIO,
) -> None:
    previous = stats.final_state
    if state is not previous:
        stats.state_transitions += 1
        print(f"{previous.name} -> {state.name}", file=output)
    if measurement is not None and state is not previous:
        print(f"distance_m={measurement.distance_cm / 100.0:g}", file=output)
    stats.final_state = state
    stats.state_counts[state] += 1
    if state is WarningState.SENSOR_FAULT:
        stats.sensor_faults += 1


def _display_sensor_fault(
    controller: WarningController,
    stats: _Stats,
    *,
    output: TextIO,
    error_output: TextIO,
) -> None:
    try:
        state = controller.process(None)
    except Exception as exc:
        print(f"failed to display SENSOR_FAULT: {exc}", file=error_output)
        return
    _record_state(state, None, stats, output=output)


def _print_summary(
    stats: _Stats,
    *,
    elapsed_seconds: float,
    cleanup_completed: bool,
    output: TextIO,
) -> None:
    rate = stats.valid_measurements / elapsed_seconds if elapsed_seconds > 0 else 0.0
    last_valid = (
        stats.last_valid_measurement_at.isoformat(timespec="milliseconds")
        if stats.last_valid_measurement_at is not None
        else "none"
    )
    print("rear_warning_diagnostic_summary", file=output)
    print(f"valid_measurements={stats.valid_measurements}", file=output)
    print(f"empty_reads={stats.empty_reads}", file=output)
    print(f"parser_errors={stats.parser_errors}", file=output)
    print(f"sensor_faults={stats.sensor_faults}", file=output)
    print(f"state_transitions={stats.state_transitions}", file=output)
    print(f"safe_count={stats.state_counts[WarningState.SAFE]}", file=output)
    print(f"warning_count={stats.state_counts[WarningState.WARNING]}", file=output)
    print(f"danger_count={stats.state_counts[WarningState.DANGER]}", file=output)
    print(f"elapsed_seconds={elapsed_seconds:.3f}", file=output)
    print(f"average_valid_rate_hz={rate:.3f}", file=output)
    print(f"last_valid_measurement_at={last_valid}", file=output)
    print(
        f"final_state={stats.final_state.name}",
        file=output,
    )
    print(f"cleanup_completed={str(cleanup_completed).lower()}", file=output)


def _run(options: _Options, *, output: TextIO, error_output: TextIO) -> int:
    stats = _Stats()
    started_at = _MONOTONIC()
    gpio: _ClosableOutput | None = None
    device: _DiagnosticDevice | None = None
    exit_code = _SUCCESS
    cleanup_completed = False

    try:
        output_factory = _OUTPUT_FACTORY or load_gpiozero_output_factory()
        gpio = output_factory(options.pins, chip=options.gpio_chip)
        led_driver = ThreeColorLedDriver(gpio, options.pins)
        controller = WarningController(WarningPolicy(), led_driver)
        led_driver.all_off()
        print("initial_state=SENSOR_FAULT (LEDs remain off)", file=output)

        device = _DEVICE_FACTORY(
            port=options.port,
            baudrate=options.baudrate,
            timeout=options.timeout,
        )
        device.open()

        while (
            options.max_samples is None
            or stats.valid_measurements < options.max_samples
        ):
            if (
                options.duration is not None
                and _MONOTONIC() - started_at >= options.duration
            ):
                break
            try:
                measurements = device.read_measurements()
            except TFMiniPlusSerialError:
                raise
            except Exception as exc:
                stats.parser_errors += 1
                print(f"parser error: {exc}", file=error_output)
                _display_sensor_fault(
                    controller, stats, output=output, error_output=error_output
                )
                exit_code = _HARDWARE_ERROR
                break

            if not measurements:
                stats.empty_reads += 1
                state = controller.process(None)
                _record_state(state, None, stats, output=output)
                continue

            if options.max_samples is not None:
                measurements = measurements[
                    : options.max_samples - stats.valid_measurements
                ]
            for measurement in measurements:
                state = controller.process(measurement)
                stats.valid_measurements += 1
                stats.last_valid_measurement_at = _WALL_CLOCK()
                _record_state(state, measurement, stats, output=output)

    except KeyboardInterrupt:
        print("diagnostic interrupted by user (Ctrl+C)", file=output)
        exit_code = _INTERRUPTED
    except TFMiniPlusSerialError as exc:
        print(f"TFMini Plus UART error: {exc}", file=error_output)
        if gpio is not None:
            _display_sensor_fault(
                controller, stats, output=output, error_output=error_output
            )
        exit_code = _HARDWARE_ERROR
    except Exception as exc:
        print(f"warning-chain hardware error: {exc}", file=error_output)
        exit_code = _HARDWARE_ERROR
    finally:
        uart_closed = device is None
        gpio_closed = gpio is None
        if device is not None:
            try:
                device.close()
                uart_closed = True
            except Exception as exc:
                print(f"failed to close TFMini Plus UART: {exc}", file=error_output)
                exit_code = _HARDWARE_ERROR
        if gpio is not None:
            try:
                gpio.close()
                gpio_closed = True
            except Exception as exc:
                print(f"failed to close LED GPIO outputs: {exc}", file=error_output)
                exit_code = _HARDWARE_ERROR
        cleanup_completed = uart_closed and gpio_closed
        elapsed = max(_MONOTONIC() - started_at, 0.0)
        _print_summary(
            stats,
            elapsed_seconds=elapsed,
            cleanup_completed=cleanup_completed,
            output=output,
        )
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        options = _parse_options(parser, argv)
    except _UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return _USAGE_ERROR

    _print_configuration(options, output=sys.stdout)
    if not options.confirm_hardware:
        print(
            "hardware not accessed; add --confirm-hardware after verifying "
            "wiring and UART ownership",
            file=sys.stderr,
        )
        return _USAGE_ERROR
    return _run(options, output=sys.stdout, error_output=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
