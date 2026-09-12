"""Confirmation-gated BNO055 calibration profile export and restore CLI."""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn, Protocol, TextIO

from rear_warning.sensors.bno055 import (
    BNO055CalibrationProfile,
    BNO055CalibrationProfileError,
    BNO055Device,
    BNO055Error,
    BNO055Measurement,
    BNO055ProfileStorageError,
    BNO055ProfileStore,
    describe_exception,
    utc_timestamp,
    validate_sensor_label,
)
from rear_warning.sensors.bno055.hardware import (
    BNO055HardwareDependenciesNotInstalled,
    load_smbus_register_io,
)

_SUCCESS = 0
_DEVICE_ERROR = 1
_USAGE_ERROR = 2
_DATA_READY_POLL_INTERVAL_SECONDS = 0.02


class _RegisterIO(Protocol):
    def close(self) -> None: ...


class _AdapterFactory(Protocol):
    def __call__(self, bus: int) -> _RegisterIO: ...


class _Device(Protocol):
    def capture_calibration_profile(
        self,
        *,
        sensor_label: str,
        created_at_utc: str,
        calibration_timeout_seconds: float,
        calibration_poll_interval_seconds: float,
    ) -> BNO055CalibrationProfile: ...

    def restore_calibration_profile(
        self,
        profile: BNO055CalibrationProfile,
        *,
        data_ready_timeout_seconds: float,
        data_ready_poll_interval_seconds: float,
    ) -> BNO055Measurement: ...

    def close(self) -> None: ...


class _DeviceFactory(Protocol):
    def __call__(
        self,
        register_io: _RegisterIO,
        *,
        address: int,
        readiness_timeout_seconds: float,
    ) -> _Device: ...


@dataclass(frozen=True, slots=True)
class _CommonOptions:
    bus: int
    address: int
    sensor_label: str
    confirm_hardware: bool


@dataclass(frozen=True, slots=True)
class _ExportOptions(_CommonOptions):
    profile_output: Path
    calibration_wait_timeout: float
    polling_interval: float


@dataclass(frozen=True, slots=True)
class _RestoreOptions(_CommonOptions):
    profile_input: Path
    data_ready_timeout: float


class _UsageError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)


_ADAPTER_FACTORY: _AdapterFactory | None = None
_DEVICE_FACTORY: _DeviceFactory = BNO055Device
_STORE_FACTORY: Callable[[], BNO055ProfileStore] = BNO055ProfileStore
_UTC_NOW: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _exception_text(error: BaseException) -> str:
    details = [describe_exception(error)]
    for note in getattr(error, "__notes__", ()):
        if note not in details:
            details.append(note)
    return "; ".join(details)


def _profile_cleanup_failed(error: BaseException) -> bool:
    if isinstance(error, BNO055ProfileStorageError) and error.cleanup_errors:
        return True
    return any(
        note.startswith("profile cleanup failed:")
        for note in getattr(error, "__notes__", ())
    )


def _contains_keyboard_interrupt(
    error: BaseException, seen: set[int] | None = None
) -> bool:
    visited = seen if seen is not None else set()
    if id(error) in visited:
        return False
    visited.add(id(error))
    if isinstance(error, KeyboardInterrupt):
        return True
    nested: list[BaseException] = []
    if isinstance(error, BNO055ProfileStorageError):
        if error.primary is not None:
            nested.append(error.primary)
        nested.extend(error.cleanup_errors)
    if error.__cause__ is not None:
        nested.append(error.__cause__)
    if error.__context__ is not None:
        nested.append(error.__context__)
    return any(_contains_keyboard_interrupt(item, visited) for item in nested)


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


def _address(value: str) -> int:
    try:
        result = int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be 0x28 or 0x29") from exc
    if result not in (0x28, 0x29):
        raise argparse.ArgumentTypeError("must be 0x28 or 0x29")
    return result


def _sensor_label(value: str) -> str:
    try:
        return validate_sensor_label(value)
    except BNO055CalibrationProfileError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _profile_path(value: str) -> Path:
    result = Path(value)
    if not result.name or result.name in (".", ".."):
        raise argparse.ArgumentTypeError("must name an explicit profile file")
    return result


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bus", type=_non_negative_int, default=1)
    parser.add_argument("--address", type=_address, default=0x29)
    parser.add_argument("--sensor-label", type=_sensor_label, required=True)
    parser.add_argument("--confirm-hardware", action="store_true")


def _build_parser() -> _Parser:
    parser = _Parser(
        prog="bno055-calibration-profile",
        description="Safely export or restore a versioned BNO055 calibration profile.",
    )
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser
    )
    export = subparsers.add_parser(
        "export", help="wait for full calibration and publish a new profile"
    )
    _add_common_arguments(export)
    export.add_argument("--profile-output", type=_profile_path, required=True)
    export.add_argument(
        "--calibration-wait-timeout", type=_positive_float, default=120.0
    )
    export.add_argument("--polling-interval", type=_positive_float, default=0.1)

    restore = subparsers.add_parser(
        "restore", help="validate, write, read back, and activate a profile"
    )
    _add_common_arguments(restore)
    restore.add_argument("--profile-input", type=_profile_path, required=True)
    restore.add_argument("--data-ready-timeout", type=_positive_float, default=2.0)
    return parser


def _parse(parser: _Parser, argv: Sequence[str] | None) -> _ExportOptions | _RestoreOptions:
    values = parser.parse_args(None if argv is None else list(argv))
    common = {
        "bus": int(values.bus),
        "address": int(values.address),
        "sensor_label": str(values.sensor_label),
        "confirm_hardware": bool(values.confirm_hardware),
    }
    if values.command == "export":
        return _ExportOptions(
            **common,
            profile_output=Path(values.profile_output),
            calibration_wait_timeout=float(values.calibration_wait_timeout),
            polling_interval=float(values.polling_interval),
        )
    return _RestoreOptions(
        **common,
        profile_input=Path(values.profile_input),
        data_ready_timeout=float(values.data_ready_timeout),
    )


def _print_plan(
    options: _ExportOptions | _RestoreOptions, *, output: TextIO
) -> None:
    print(f"command={'export' if isinstance(options, _ExportOptions) else 'restore'}", file=output)
    print(f"bus={options.bus}", file=output)
    print(f"address=0x{options.address:02x}", file=output)
    print(f"sensor_label={options.sensor_label}", file=output)
    print("profile_registers=page_0:0x55-0x6a (22 bytes)", file=output)
    print("excluded_registers=SIC_0x43-0x54,axis_map_writes", file=output)
    if isinstance(options, _ExportOptions):
        print(f"profile_output={options.profile_output}", file=output)
        print(
            "mode_sequence=identity,NDOF,full_calibration,CONFIGMODE,page_0,"
            "read_0x55-0x6a,cleanup_CONFIGMODE",
            file=output,
        )
        print(
            f"calibration_wait_timeout_seconds={options.calibration_wait_timeout:.3f}",
            file=output,
        )
        print(f"polling_interval_seconds={options.polling_interval:.3f}", file=output)
        print(
            "calibration_maximum_attempts="
            f"{math.ceil(options.calibration_wait_timeout / options.polling_interval) + 1}",
            file=output,
        )
    else:
        print(f"profile_input={options.profile_input}", file=output)
        print(
            "mode_sequence=offline_profile_validation,identity,CONFIGMODE,page_0,"
            "normal_power,default_units,write_0x55-0x6a,readback,NDOF,"
            "readiness,quality_measurement,cleanup_CONFIGMODE",
            file=output,
        )
        print(f"data_ready_timeout_seconds={options.data_ready_timeout:.3f}", file=output)
        print(
            "data_ready_maximum_attempts="
            f"{math.ceil(options.data_ready_timeout / _DATA_READY_POLL_INTERVAL_SECONDS) + 1}",
            file=output,
        )


def _run(
    options: _ExportOptions | _RestoreOptions, *, output: TextIO, error_output: TextIO
) -> int:
    store: BNO055ProfileStore | None = None
    profile: BNO055CalibrationProfile | None = None
    register_io: _RegisterIO | None = None
    device: _Device | None = None
    exit_code = _SUCCESS
    interrupted = False
    operation_completed = False
    cleanup_completed = False
    try:
        store = _STORE_FACTORY()
        if isinstance(options, _RestoreOptions):
            profile = store.load(
                options.profile_input,
                expected_sensor_label=options.sensor_label,
            )
            print("profile_validation_success", file=output)
        else:
            created_at = utc_timestamp(_UTC_NOW())

        adapter_factory = _ADAPTER_FACTORY or load_smbus_register_io()
        register_io = adapter_factory(options.bus)
        readiness_timeout = (
            options.data_ready_timeout
            if isinstance(options, _RestoreOptions)
            else BNO055Device.DEFAULT_READINESS_TIMEOUT_SECONDS
        )
        device = _DEVICE_FACTORY(
            register_io,
            address=options.address,
            readiness_timeout_seconds=readiness_timeout,
        )
        if isinstance(options, _ExportOptions):
            profile = device.capture_calibration_profile(
                sensor_label=options.sensor_label,
                created_at_utc=created_at,
                calibration_timeout_seconds=options.calibration_wait_timeout,
                calibration_poll_interval_seconds=options.polling_interval,
            )
            operation_completed = True
        else:
            if profile is None:
                raise BNO055CalibrationProfileError(
                    "validated restore profile is unavailable"
                )
            device.restore_calibration_profile(
                profile,
                data_ready_timeout_seconds=options.data_ready_timeout,
                data_ready_poll_interval_seconds=_DATA_READY_POLL_INTERVAL_SECONDS,
            )
            print("read_back_verified", file=output)
            print("measurement_verified", file=output)
            operation_completed = True
    except KeyboardInterrupt as exc:
        interrupted = True
        print(
            "BNO055 calibration profile operation interrupted by user (Ctrl+C): "
            f"{_exception_text(exc)}",
            file=error_output,
        )
        exit_code = 130
    except BNO055HardwareDependenciesNotInstalled as exc:
        print(_exception_text(exc), file=error_output)
        exit_code = _DEVICE_ERROR
    except (BNO055CalibrationProfileError, BNO055Error, OSError) as exc:
        print(
            f"BNO055 calibration profile error: {_exception_text(exc)}",
            file=error_output,
        )
        exit_code = _DEVICE_ERROR
    except Exception as exc:
        print(
            f"BNO055 calibration adapter error: {_exception_text(exc)}",
            file=error_output,
        )
        exit_code = _DEVICE_ERROR
    finally:
        try:
            if device is not None:
                device.close()
                cleanup_completed = True
            elif register_io is not None:
                register_io.close()
                cleanup_completed = True
        except BaseException as exc:
            print(
                f"BNO055 calibration cleanup error: {_exception_text(exc)}",
                file=error_output,
            )
            if isinstance(exc, KeyboardInterrupt):
                interrupted = True
                exit_code = 130
            elif not interrupted:
                exit_code = _DEVICE_ERROR
        print(
            f"device_cleanup_completed={str(cleanup_completed).lower()}",
            file=output,
        )

    if exit_code == _SUCCESS and operation_completed:
        if isinstance(options, _ExportOptions):
            if profile is None or store is None:
                print("BNO055 calibration profile error: no profile was captured", file=error_output)
                return _DEVICE_ERROR
            try:
                store.save(options.profile_output, profile)
            except KeyboardInterrupt as exc:
                print(
                    "BNO055 calibration profile publication interrupted by user "
                    f"(Ctrl+C): {_exception_text(exc)}",
                    file=error_output,
                )
                print(
                    "profile_cleanup_completed="
                    f"{str(not _profile_cleanup_failed(exc)).lower()}",
                    file=output,
                )
                return 130
            except Exception as exc:
                print(
                    "BNO055 calibration profile storage error: "
                    f"{_exception_text(exc)}",
                    file=error_output,
                )
                print(
                    "profile_cleanup_completed="
                    f"{str(not _profile_cleanup_failed(exc)).lower()}",
                    file=output,
                )
                return 130 if _contains_keyboard_interrupt(exc) else _DEVICE_ERROR
            print(f"export_success path={options.profile_output}", file=output)
        else:
            print("restore_success", file=output)
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
        print(
            f"{parser.prog}: error: --confirm-hardware is required before profile or I2C access",
            file=sys.stderr,
        )
        return _USAGE_ERROR
    return _run(options, output=sys.stdout, error_output=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
