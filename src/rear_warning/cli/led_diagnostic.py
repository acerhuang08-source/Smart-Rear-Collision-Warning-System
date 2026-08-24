"""Explicitly confirmed physical LED diagnostic command."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Callable, Sequence
from typing import Protocol, TextIO

from rear_warning.outputs import LedPins, ThreeColorLedDriver
from rear_warning.outputs.hardware import load_gpiozero_output_factory
from rear_warning.warning import WarningState

_SUCCESS = 0
_HARDWARE_ERROR = 1
_USAGE_ERROR = 2
_DEFAULT_STEP_SECONDS = 1.0
_SEQUENCE = (
    ("red", WarningState.DANGER),
    ("yellow", WarningState.WARNING),
    ("green", WarningState.SAFE),
    ("SENSOR_FAULT (red + yellow)", WarningState.SENSOR_FAULT),
)


class _ClosableOutput(Protocol):
    def write(self, pin: int, active: bool) -> None: ...

    def close(self) -> None: ...


class _OutputFactory(Protocol):
    def __call__(self, pins: LedPins, *, chip: int) -> _ClosableOutput: ...


_OUTPUT_FACTORY: _OutputFactory | None = None
_SLEEP: Callable[[float], None] = time.sleep


def _positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite number")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="led-diagnostic",
        description="Test red, yellow, green, and SENSOR_FAULT LED patterns.",
    )
    parser.add_argument(
        "--confirm-hardware",
        action="store_true",
        help="confirm the documented LEDs are connected before GPIO access",
    )
    parser.add_argument(
        "--step-seconds",
        type=_positive_float,
        default=_DEFAULT_STEP_SECONDS,
        help=f"seconds to show each pattern (default: {_DEFAULT_STEP_SECONDS})",
    )
    return parser


def _run(*, step_seconds: float, output: TextIO, error_output: TextIO) -> int:
    pins = LedPins.raspberry_pi_default()
    gpio: _ClosableOutput | None = None
    exit_code = _SUCCESS
    interrupted = False
    try:
        output_factory = _OUTPUT_FACTORY or load_gpiozero_output_factory()
        gpio = output_factory(pins, chip=0)
        driver = ThreeColorLedDriver(gpio, pins)
        for label, state in _SEQUENCE:
            print(f"testing {label}", file=output)
            driver.display(state)
            _SLEEP(step_seconds)
        driver.all_off()
    except KeyboardInterrupt:
        print("LED diagnostic interrupted; turning all LEDs off", file=output)
        interrupted = True
    except Exception as exc:
        print(f"LED diagnostic failed: {exc}", file=error_output)
        exit_code = _HARDWARE_ERROR
    finally:
        if gpio is not None:
            try:
                gpio.close()
            except Exception as exc:
                print(f"LED cleanup error: {exc}", file=error_output)
                exit_code = _HARDWARE_ERROR

    if exit_code == _SUCCESS and not interrupted:
        print("LED diagnostic completed; all LEDs are off", file=output)
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(None if argv is None else list(argv))
    if not options.confirm_hardware:
        parser.print_usage(sys.stderr)
        print(
            f"{parser.prog}: error: --confirm-hardware is required before GPIO access",
            file=sys.stderr,
        )
        return _USAGE_ERROR
    return _run(
        step_seconds=float(options.step_seconds),
        output=sys.stdout,
        error_output=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
