#!/usr/bin/env python3
"""Bounded, receive-only UART capture tool for the ioRadar701.

This utility deliberately stores opaque bytes.  It does not send commands,
reset the input buffer, infer packet boundaries, or decode measurements.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol, TextIO

import serial

_SUCCESS = 0
_CAPTURE_ERROR = 1
_USAGE_OR_CANCELLED = 2
_INTERRUPTED = 130

_DEFAULT_PORT = "/dev/ttyAMA0"
_DEFAULT_BAUDRATE = 230400
_DEFAULT_DURATION_SECONDS = 5.0
_MAX_DURATION_SECONDS = 5.0
_DEFAULT_OUTPUT_ROOT = Path("captures/ioradar701")
_READ_TIMEOUT_SECONDS = 0.1
_READ_SIZE = 4096
_SAMPLE_LIMIT = 512


class _SerialConnection(Protocol):
    """The receive-only part of a pySerial connection used by this tool."""

    @property
    def is_open(self) -> bool:
        """Return whether the port is open."""
        ...

    def read(self, size: int = 1) -> bytes:
        """Read at most ``size`` bytes."""
        ...

    def close(self) -> None:
        """Close the connection."""
        ...


class _SerialFactory(Protocol):
    """Factory surface used to keep all automated tests hardware-free."""

    def __call__(self, options: "_Options") -> _SerialConnection:
        """Create and open one configured serial connection."""
        ...


@dataclass(frozen=True, slots=True)
class _Options:
    """Validated command-line configuration."""

    port: str
    baudrate: int
    duration: float
    output_root: Path


@dataclass(slots=True)
class _CaptureResult:
    """Outcome and preservation state for one requested capture."""

    capture_directory: Path
    raw_path: Path
    metadata_path: Path
    started_at: datetime
    ended_at: datetime
    elapsed_seconds: float = 0.0
    total_bytes: int = 0
    persisted_bytes: int = 0
    sample: bytes = b""
    acquisition_started: bool = False
    serial_close_succeeded: bool | None = None
    raw_file_close_succeeded: bool = False
    metadata_saved: bool = False
    exit_reason: str = "not_started"
    detail: str | None = None

    @property
    def full_capture_saved(self) -> bool:
        """Return whether every received byte was durably closed to the raw file."""
        return (
            self.raw_file_close_succeeded
            and self.persisted_bytes == self.total_bytes
        )


class _UsageError(ValueError):
    """Raised instead of exiting for invalid command-line input."""


class _ArgumentParser(argparse.ArgumentParser):
    """Argument parser whose errors can be returned from ``main``."""

    def error(self, message: str) -> NoReturn:
        """Turn argparse errors into a locally handled exception."""
        raise _UsageError(message)


def _local_now() -> datetime:
    """Return an aware local wall-clock timestamp."""
    return datetime.now().astimezone()


_MONOTONIC: Callable[[], float] = time.monotonic
_WALL_CLOCK: Callable[[], datetime] = _local_now
_INPUT: Callable[[str], str] = input


def _positive_int(value: str) -> int:
    """Parse a strictly positive integer."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _capture_duration(value: str) -> float:
    """Parse a positive capture duration no longer than the authorized limit."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be finite")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    if parsed > _MAX_DURATION_SECONDS:
        raise argparse.ArgumentTypeError(
            f"must not exceed {_MAX_DURATION_SECONDS:g} seconds"
        )
    return parsed


def _port_name(value: str) -> str:
    """Reject an empty UART path."""
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be empty")
    return value


def _build_parser() -> _ArgumentParser:
    """Build the CLI parser without touching UART or capture files."""
    parser = _ArgumentParser(
        prog="ioradar701_raw_receive.py",
        description=(
            "Receive and preserve opaque ioRadar701 UART bytes without "
            "sending data or interpreting packets."
        ),
    )
    parser.add_argument(
        "--port",
        type=_port_name,
        default=_DEFAULT_PORT,
        help=f"UART device (default: {_DEFAULT_PORT})",
    )
    parser.add_argument(
        "--baudrate",
        type=_positive_int,
        default=_DEFAULT_BAUDRATE,
        help=f"requested baud rate (default: {_DEFAULT_BAUDRATE})",
    )
    parser.add_argument(
        "--duration",
        type=_capture_duration,
        default=_DEFAULT_DURATION_SECONDS,
        help=(
            "capture duration in seconds "
            f"(default/max: {_DEFAULT_DURATION_SECONDS:g})"
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=_DEFAULT_OUTPUT_ROOT,
        help=f"parent directory for new captures (default: {_DEFAULT_OUTPUT_ROOT})",
    )
    return parser


def _parse_options(parser: _ArgumentParser, argv: Sequence[str] | None) -> _Options:
    """Parse validated options without opening a device."""
    namespace = parser.parse_args(None if argv is None else list(argv))
    output_root = Path(namespace.output_root)
    if not str(output_root).strip():
        raise _UsageError("--output-root must not be empty")
    return _Options(
        port=str(namespace.port),
        baudrate=int(namespace.baudrate),
        duration=float(namespace.duration),
        output_root=output_root,
    )


def _open_serial(options: _Options) -> _SerialConnection:
    """Configure DTR/RTS low and open one 8N1 receive-only connection."""
    connection = serial.Serial(
        port=None,
        baudrate=options.baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=min(_READ_TIMEOUT_SECONDS, options.duration),
        write_timeout=0,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
    )
    connection.port = options.port
    if os.name == "posix":
        connection.exclusive = True
    connection.dtr = False
    connection.rts = False
    try:
        connection.open()
    except BaseException:
        if connection.is_open:
            connection.close()
        raise
    return connection


_SERIAL_FACTORY: _SerialFactory = _open_serial


def _allocate_capture_directory(options: _Options, timestamp: datetime) -> Path:
    """Create a timestamped directory without replacing an existing capture."""
    options.output_root.mkdir(parents=True, exist_ok=True)
    stamp = timestamp.strftime("%Y%m%dT%H%M%S.%f%z")
    base_name = f"capture-{stamp}"
    for suffix in range(1000):
        name = base_name if suffix == 0 else f"{base_name}-{suffix:03d}"
        candidate = options.output_root / name
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"could not allocate a unique capture directory under {options.output_root}")


def _close_raw_file(raw_file: BinaryIO, result: _CaptureResult) -> None:
    """Flush, sync, and close the raw file while recording any failure."""
    try:
        raw_file.flush()
        os.fsync(raw_file.fileno())
        raw_file.close()
    except OSError as exc:
        result.raw_file_close_succeeded = False
        result.exit_reason = "storage_error"
        result.detail = f"failed to finalize raw data file: {exc}"
        try:
            raw_file.close()
        except OSError:
            pass
    else:
        result.raw_file_close_succeeded = True


def _capture(options: _Options, capture_directory: Path) -> _CaptureResult:
    """Run one bounded receive-only capture and always release owned resources."""
    started_at = _WALL_CLOCK()
    result = _CaptureResult(
        capture_directory=capture_directory,
        raw_path=capture_directory / "raw.bin",
        metadata_path=capture_directory / "metadata.json",
        started_at=started_at,
        ended_at=started_at,
    )
    raw_file: BinaryIO | None = None
    connection: _SerialConnection | None = None
    capture_started_monotonic: float | None = None

    try:
        try:
            raw_file = result.raw_path.open("xb")
        except OSError as exc:
            result.exit_reason = "storage_error"
            result.detail = f"failed to create raw data file: {exc}"
            return result

        try:
            connection = _SERIAL_FACTORY(options)
        except (OSError, ValueError, serial.SerialException) as exc:
            result.exit_reason = "open_error"
            result.detail = f"failed to open UART {options.port!r}: {exc}"
            return result

        result.acquisition_started = True
        capture_started_monotonic = _MONOTONIC()
        deadline = capture_started_monotonic + options.duration
        result.exit_reason = "completed"

        while _MONOTONIC() < deadline:
            try:
                chunk = connection.read(_READ_SIZE)
            except KeyboardInterrupt:
                result.exit_reason = "interrupted"
                break
            except (OSError, serial.SerialException) as exc:
                result.exit_reason = "read_error"
                result.detail = f"failed to read UART {options.port!r}: {exc}"
                break

            if not chunk:
                continue

            result.total_bytes += len(chunk)
            if len(result.sample) < _SAMPLE_LIMIT:
                remaining = _SAMPLE_LIMIT - len(result.sample)
                result.sample += chunk[:remaining]
            try:
                written = raw_file.write(chunk)
            except OSError as exc:
                result.exit_reason = "storage_error"
                result.detail = f"failed to write raw data file: {exc}"
                break
            if written != len(chunk):
                result.persisted_bytes += max(written or 0, 0)
                result.exit_reason = "storage_error"
                result.detail = (
                    "failed to write complete UART chunk: "
                    f"expected {len(chunk)} bytes, wrote {written}"
                )
                break
            result.persisted_bytes += written

        if result.exit_reason == "completed" and result.total_bytes == 0:
            result.exit_reason = "no_data"
            result.detail = "capture completed but UART returned zero bytes"
    except KeyboardInterrupt:
        result.exit_reason = "interrupted"
    finally:
        if capture_started_monotonic is not None:
            result.elapsed_seconds = max(
                _MONOTONIC() - capture_started_monotonic,
                0.0,
            )
        result.ended_at = _WALL_CLOCK()

        if connection is not None:
            try:
                connection.close()
            except (OSError, serial.SerialException) as exc:
                result.serial_close_succeeded = False
                previous = result.exit_reason
                result.exit_reason = "close_error"
                result.detail = (
                    f"UART close failed after {previous}: {exc}"
                )
            else:
                result.serial_close_succeeded = True

        if raw_file is not None:
            _close_raw_file(raw_file, result)

    return result


def _metadata(options: _Options, result: _CaptureResult) -> dict[str, object]:
    """Build explicit metadata without claiming that bytes are measurements."""
    return {
        "format_version": 1,
        "device_family": "ioRadar701",
        "data_interpretation": "opaque_uart_bytes_not_validated_packets",
        "port": options.port,
        "requested_baudrate": options.baudrate,
        "requested_serial_format": "8N1",
        "flow_control": {
            "xonxoff": False,
            "rtscts": False,
            "dsrdtr": False,
        },
        "requested_duration_seconds": options.duration,
        "started_at": result.started_at.isoformat(timespec="microseconds"),
        "ended_at": result.ended_at.isoformat(timespec="microseconds"),
        "elapsed_seconds": result.elapsed_seconds,
        "total_bytes": result.total_bytes,
        "persisted_bytes": result.persisted_bytes,
        "exit_reason": result.exit_reason,
        "detail": result.detail,
        "acquisition_started": result.acquisition_started,
        "serial_close_succeeded": result.serial_close_succeeded,
        "raw_file_close_succeeded": result.raw_file_close_succeeded,
        "full_capture_saved": result.full_capture_saved,
        "raw_file": result.raw_path.name,
    }


def _save_metadata(options: _Options, result: _CaptureResult) -> None:
    """Create metadata once; report failures without overwriting anything."""
    try:
        with result.metadata_path.open("x", encoding="utf-8", newline="\n") as file:
            json.dump(
                _metadata(options, result),
                file,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
    except OSError as exc:
        result.metadata_saved = False
        previous = result.exit_reason
        result.exit_reason = "storage_error"
        result.detail = f"metadata save failed after {previous}: {exc}"
    else:
        result.metadata_saved = True


def _print_plan(options: _Options, *, output: TextIO) -> None:
    """Show exactly what Enter will do before any UART or file is opened."""
    print("ioRadar701 UART 原始資料接收（只讀、不解析）", file=output)
    print(f"port={options.port}", file=output)
    print(f"baudrate={options.baudrate}", file=output)
    print("format=8N1", file=output)
    print("flow_control=off", file=output)
    print(f"duration_seconds={options.duration:g}", file=output)
    print(f"output_root={options.output_root}", file=output)
    print("planned_action=open_UART_and_read_only", file=output)


def _print_summary(result: _CaptureResult, *, output: TextIO) -> None:
    """Print a bounded hexadecimal sample and preservation outcome."""
    close_status = (
        "not_opened"
        if result.serial_close_succeeded is None
        else str(result.serial_close_succeeded).lower()
    )
    sample_hex = result.sample.hex(" ") if result.sample else "(empty)"
    print("capture_summary", file=output)
    print(f"exit_reason={result.exit_reason}", file=output)
    print(f"elapsed_seconds={result.elapsed_seconds:.3f}", file=output)
    print(f"total_bytes={result.total_bytes}", file=output)
    print(f"sample_bytes={len(result.sample)}", file=output)
    print(f"sample_hex={sample_hex}", file=output)
    print(f"serial_close_succeeded={close_status}", file=output)
    print(f"full_capture_saved={str(result.full_capture_saved).lower()}", file=output)
    print(f"capture_directory={result.capture_directory}", file=output)
    print(f"raw_data_path={result.raw_path}", file=output)
    metadata = str(result.metadata_path) if result.metadata_saved else "NOT_SAVED"
    print(f"metadata_path={metadata}", file=output)


def _result_code(result: _CaptureResult) -> int:
    """Map the explicit exit reason to a process status."""
    if result.exit_reason == "completed" and result.metadata_saved:
        return _SUCCESS
    if result.exit_reason == "interrupted":
        return _INTERRUPTED
    return _CAPTURE_ERROR


def main(argv: Sequence[str] | None = None) -> int:
    """Validate, wait for operator confirmation, and run one bounded capture."""
    parser = _build_parser()
    try:
        options = _parse_options(parser, argv)
    except _UsageError as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return _USAGE_OR_CANCELLED

    _print_plan(options, output=sys.stdout)
    prompt = (
        "僅在 TX 方向、3.3V 電平相容、共地及 UART 單一占用均已確認後，"
        "按 Enter 開始；EOF 或 Ctrl+C 取消："
    )
    try:
        _INPUT(prompt)
    except EOFError:
        print("已取消：收到 EOF，未開啟 UART。", file=sys.stderr)
        return _USAGE_OR_CANCELLED
    except KeyboardInterrupt:
        print("\n已取消：收到 Ctrl+C，未開啟 UART。", file=sys.stderr)
        return _INTERRUPTED

    try:
        capture_directory = _allocate_capture_directory(options, _WALL_CLOCK())
    except OSError as exc:
        print(f"無法建立擷取目錄，未開啟 UART：{exc}", file=sys.stderr)
        return _CAPTURE_ERROR

    result = _capture(options, capture_directory)
    _save_metadata(options, result)
    _print_summary(result, output=sys.stdout)
    if result.detail is not None:
        print(f"capture_error: {result.detail}", file=sys.stderr)
    if result.exit_reason == "no_data":
        print(
            "未收到資料；不會自動改速率、交換訊號線或切換模擬模式。",
            file=sys.stderr,
        )
    return _result_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
