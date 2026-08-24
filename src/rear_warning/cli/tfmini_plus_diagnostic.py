"""Command-line diagnostic monitor for the Benewake TFMini Plus."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn, Protocol, TextIO

from rear_warning.sensors.tfmini_plus import (
    TFMiniPlusMeasurement,
    TFMiniPlusSerialDevice,
    TFMiniPlusSerialError,
    TFMiniPlusSerialOpenError,
    TFMiniPlusSerialReadError,
)

_SUCCESS = 0
_DEVICE_ERROR = 1
_USAGE_ERROR = 2
_DEFAULT_BAUDRATE = 115200
_DEFAULT_TIMEOUT_SECONDS = 0.1


class _DiagnosticDevice(Protocol):
    """Device surface required by the diagnostic command."""

    def open(self) -> None:
        """Open the diagnostic device."""
        ...

    def close(self) -> None:
        """Close the diagnostic device."""
        ...

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        """Read every valid measurement completed by one device read."""
        ...


class _DeviceFactory(Protocol):
    """Factory contract used by production and hardware-free tests."""

    def __call__(
        self,
        *,
        port: str,
        baudrate: int,
        timeout: float | None,
    ) -> _DiagnosticDevice:
        """Create a configured diagnostic device without opening it."""
        ...


@dataclass(frozen=True, slots=True)
class _DiagnosticOptions:
    """Validated command-line options."""

    port: str
    baudrate: int
    timeout: float
    duration: float | None
    max_samples: int | None


@dataclass(slots=True)
class _DiagnosticStats:
    """Mutable counters collected during one diagnostic run."""

    valid_measurements: int = 0
    empty_reads: int = 0
    last_valid_measurement_at: datetime | None = None


class _UsageError(ValueError):
    """Raised instead of terminating the process during argument parsing."""


class _NonExitingArgumentParser(argparse.ArgumentParser):
    """Argument parser that reports invalid input through ``main``'s return code."""

    def error(self, message: str) -> NoReturn:
        """Convert argparse errors into a locally handled exception."""
        raise _UsageError(message)


def _local_now() -> datetime:
    """Return an aware local timestamp for the latest valid measurement."""
    return datetime.now().astimezone()


_DEVICE_FACTORY: _DeviceFactory = TFMiniPlusSerialDevice
_MONOTONIC: Callable[[], float] = time.monotonic
_WALL_CLOCK: Callable[[], datetime] = _local_now


def _positive_int(value: str) -> int:
    """Parse a strictly positive integer for argparse."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _positive_float(value: str) -> float:
    """Parse a strictly positive float for argparse."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _non_negative_float(value: str) -> float:
    """Parse a non-negative float for argparse."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _port_name(value: str) -> str:
    """Reject an empty serial port while allowing platform-specific names."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _build_argument_parser() -> _NonExitingArgumentParser:
    """Build the command-line parser without reading process arguments."""
    parser = _NonExitingArgumentParser(
        prog="tfmini-plus-diagnostic",
        description="Monitor TFMini Plus measurements and diagnostic statistics.",
    )
    parser.add_argument(
        "--port",
        required=True,
        type=_port_name,
        help="serial device name or pySerial URL",
    )
    parser.add_argument(
        "--baudrate",
        type=_positive_int,
        default=_DEFAULT_BAUDRATE,
        help=f"serial baud rate (default: {_DEFAULT_BAUDRATE})",
    )
    parser.add_argument(
        "--timeout",
        type=_non_negative_float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"read timeout in seconds (default: {_DEFAULT_TIMEOUT_SECONDS})",
    )
    parser.add_argument(
        "--duration",
        type=_positive_float,
        help="maximum diagnostic duration in seconds",
    )
    parser.add_argument(
        "--max-samples",
        type=_positive_int,
        help="maximum number of valid measurements to display",
    )
    return parser


def _parse_options(
    parser: _NonExitingArgumentParser,
    argv: Sequence[str] | None,
) -> _DiagnosticOptions:
    """Parse and validate command-line options."""
    namespace = parser.parse_args(None if argv is None else list(argv))
    if namespace.duration is None and namespace.max_samples is None:
        raise _UsageError(
            "at least one of --duration or --max-samples is required"
        )

    return _DiagnosticOptions(
        port=str(namespace.port),
        baudrate=int(namespace.baudrate),
        timeout=float(namespace.timeout),
        duration=(
            None if namespace.duration is None else float(namespace.duration)
        ),
        max_samples=(
            None
            if namespace.max_samples is None
            else int(namespace.max_samples)
        ),
    )


def _print_measurement(
    measurement: TFMiniPlusMeasurement,
    *,
    output: TextIO,
) -> None:
    """Print one decoded measurement in a stable key-value format."""
    print(
        "measurement "
        f"distance_cm={measurement.distance_cm} "
        f"strength={measurement.strength} "
        f"chip_temperature_c={measurement.chip_temperature_c:.2f}",
        file=output,
    )


def _print_statistics(
    stats: _DiagnosticStats,
    *,
    elapsed_seconds: float,
    output: TextIO,
) -> None:
    """Print final diagnostic counters and derived data rate."""
    rate = (
        stats.valid_measurements / elapsed_seconds
        if elapsed_seconds > 0
        else 0.0
    )
    last_valid = (
        stats.last_valid_measurement_at.isoformat(timespec="milliseconds")
        if stats.last_valid_measurement_at is not None
        else "none"
    )

    print("diagnostic_statistics", file=output)
    print(f"valid_measurements={stats.valid_measurements}", file=output)
    print(f"empty_reads={stats.empty_reads}", file=output)
    print(f"elapsed_seconds={elapsed_seconds:.3f}", file=output)
    print(f"average_valid_rate_hz={rate:.3f}", file=output)
    print(f"last_valid_measurement_at={last_valid}", file=output)


def _run_diagnostic(
    options: _DiagnosticOptions,
    *,
    output: TextIO,
    error_output: TextIO,
) -> int:
    """Run one bounded diagnostic session and always print final statistics."""
    stats = _DiagnosticStats()
    started_at = _MONOTONIC()
    device: _DiagnosticDevice | None = None
    exit_code = _SUCCESS

    try:
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

            measurements = device.read_measurements()
            if not measurements:
                stats.empty_reads += 1
                continue

            if options.max_samples is not None:
                remaining = options.max_samples - stats.valid_measurements
                measurements = measurements[:remaining]

            for measurement in measurements:
                _print_measurement(measurement, output=output)
                stats.valid_measurements += 1
                stats.last_valid_measurement_at = _WALL_CLOCK()

    except KeyboardInterrupt:
        print("diagnostic interrupted by user (Ctrl+C)", file=output)
    except TFMiniPlusSerialOpenError as exc:
        print(f"failed to open TFMini Plus UART: {exc}", file=error_output)
        exit_code = _DEVICE_ERROR
    except TFMiniPlusSerialReadError as exc:
        print(f"failed to read TFMini Plus UART: {exc}", file=error_output)
        exit_code = _DEVICE_ERROR
    except TFMiniPlusSerialError as exc:
        print(f"TFMini Plus UART error: {exc}", file=error_output)
        exit_code = _DEVICE_ERROR
    finally:
        if device is not None:
            try:
                device.close()
            except TFMiniPlusSerialError as exc:
                print(
                    f"failed to close TFMini Plus UART: {exc}",
                    file=error_output,
                )
                exit_code = _DEVICE_ERROR

        elapsed_seconds = max(_MONOTONIC() - started_at, 0.0)
        _print_statistics(
            stats,
            elapsed_seconds=elapsed_seconds,
            output=output,
        )

    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    """Run the TFMini Plus diagnostic command and return a process exit code."""
    parser = _build_argument_parser()
    try:
        options = _parse_options(parser, argv)
    except _UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return _USAGE_ERROR

    return _run_diagnostic(
        options,
        output=sys.stdout,
        error_output=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
