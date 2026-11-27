#!/usr/bin/env python3
"""Interactive, bounded post-wiring checks for LEDs, BNO055, and ioRadar701.

Hardware libraries are imported only after the operator has reviewed the plan
and pressed Enter.  The ioRadar parser remains independent of UART I/O.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import shutil
import stat
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO

from rear_warning.outputs import LedPins
from rear_warning.sensors.ioradar701 import IoRadar701StreamParser


_SUCCESS = 0
_TEST_FAILED = 1
_USAGE_OR_CANCELLED = 2
_INTERRUPTED = 130

_MODES = ("all", "led", "bno", "radar")
_LED_SECONDS = 1.0
_BNO_BUS = 1
_BNO_ADDRESSES = (0x29, 0x28)
_BNO_CHIP_ID_REGISTER = 0x00
_BNO_EXPECTED_CHIP_ID = 0xA0
_RADAR_PORT = "/dev/ttyAMA0"
_RADAR_BAUDRATE = 230400
_RADAR_SECONDS = 5.0
_RADAR_READ_SIZE = 4096
_RADAR_TIMEOUT = 0.1
_TFMINI_SERVICE = "tfmini.service"


class _DigitalOutput(Protocol):
    def write(self, pin: int, active: bool) -> None: ...

    def close(self) -> None: ...


class _RegisterIO(Protocol):
    def read_byte_data(self, address: int, register: int) -> int: ...

    def close(self) -> None: ...


class _SerialConnection(Protocol):
    @property
    def is_open(self) -> bool: ...

    def read(self, size: int = 1) -> bytes: ...

    def fileno(self) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Options:
    mode: str
    gpio_chip: int | None
    gpio_chip_source: str


@dataclass(frozen=True, slots=True)
class _TestResult:
    item: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True, slots=True)
class _ServiceState:
    active_state: str
    main_pid: int


_INPUT: Callable[[str], str] = input
_SLEEP: Callable[[float], None] = time.sleep
_MONOTONIC: Callable[[], float] = time.monotonic
_GPIO_FACTORY_LOADER: Callable[[], Callable[..., _DigitalOutput]] | None = None
_REGISTER_IO_LOADER: Callable[[], Callable[[int], _RegisterIO]] | None = None
_SERIAL_FACTORY: Callable[[], _SerialConnection] | None = None
_OWNER_PROBE: Callable[[str], tuple[int, ...]] | None = None
_COMMAND_RUNNER: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run


def _detect_gpio_chip(
    root: Path = Path("/sys/bus/gpio/devices"),
) -> tuple[int | None, str]:
    """Choose the RP1/pinctrl GPIO chip exposed by the running kernel."""
    candidates: list[tuple[int, str, int]] = []
    try:
        paths = sorted(root.glob("gpiochip*"))
    except OSError:
        paths = []
    for path in paths:
        suffix = path.name.removeprefix("gpiochip")
        if not suffix.isdigit():
            continue
        try:
            label = (path / "label").read_text(encoding="utf-8").strip()
            ngpio_text = (path / "ngpio").read_text(encoding="utf-8").strip()
            ngpio = int(ngpio_text)
        except (OSError, ValueError):
            continue
        candidates.append((int(suffix), label, ngpio))

    def priority(candidate: tuple[int, str, int]) -> tuple[int, int]:
        number, label, ngpio = candidate
        normalized = label.lower()
        if "pinctrl-rp1" in normalized:
            rank = 0
        elif "rp1" in normalized:
            rank = 1
        elif "pinctrl-bcm" in normalized:
            rank = 2
        elif ngpio >= 28:
            rank = 3
        else:
            rank = 4
        return rank, number

    if candidates:
        number, label, ngpio = min(candidates, key=priority)
        return number, f"auto:{label or 'unlabelled'},ngpio={ngpio}"
    return None, "unavailable: no usable gpiochip sysfs metadata"


def _gpio_chip(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wiring_quick_test.py",
        description=(
            "Interactive post-wiring quick test. Hardware opens only after Enter; "
            "Ctrl+C releases owned resources."
        ),
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=_MODES,
        default="all",
        help="test mode (default: all; order: led, bno, radar)",
    )
    parser.add_argument(
        "--gpio-chip",
        type=_gpio_chip,
        default=None,
        help="lgpio chip number (default: detect RP1/pinctrl chip from sysfs)",
    )
    return parser


def _options(namespace: argparse.Namespace) -> _Options:
    detected_chip, detected_source = _detect_gpio_chip()
    if namespace.gpio_chip is None:
        return _Options(str(namespace.mode), detected_chip, detected_source)
    return _Options(
        str(namespace.mode),
        int(namespace.gpio_chip),
        "operator override; auto detection was "
        + (
            f"gpiochip{detected_chip} ({detected_source})"
            if detected_chip is not None
            else detected_source
        ),
    )


def _selected_items(mode: str) -> tuple[str, ...]:
    return ("led", "bno", "radar") if mode == "all" else (mode,)


def _print_plan(options: _Options, *, output: TextIO) -> None:
    items = _selected_items(options.mode)
    gpio_chip = (
        f"gpiochip{options.gpio_chip}"
        if options.gpio_chip is not None
        else "UNAVAILABLE (explicit --gpio-chip N required)"
    )
    print("重新接線後功能快測（尚未開啟硬體）", file=output)
    print(f"mode={options.mode}; order={','.join(items)}", file=output)
    if "led" in items:
        print(
            "LED=active-high, BCM17/27/22 (physical 11/13/15), "
            "each with 220 ohm resistor and common ground, "
            f"1.0 s each, lgpio chip={gpio_chip} ({options.gpio_chip_source})",
            file=output,
        )
    if "bno" in items:
        print(
            "BNO055=/dev/i2c-1, SDA=physical Pin 3, SCL=physical Pin 5, "
            "read-only CHIP_ID register 0x00, try 0x29 then 0x28",
            file=output,
        )
    if "radar" in items:
        print("ioRadar701=/dev/ttyAMA0, 230400 8N1, flow-control off, receive-only 5 s", file=output)
        print(
            "radar wiring=blue-board original ESP32 socket IO16 -> Pi physical "
            "Pin 10/GPIO15 RX; independent power/common ground retained; Pi Pin 8 not connected",
            file=output,
        )
    print(
        "Before UART/I2C access the tool checks visible owners. It may stop and later "
        "restart tfmini.service only when its MainPID is the sole confirmed owner; "
        "unknown owners are never terminated.",
        file=output,
    )
    print("No wiring, EEPROM, thresholds, boot settings, or radar commands are changed.", file=output)


def _probe_owners(path: str) -> tuple[int, ...]:
    """Return visible fuser PIDs after validating the device path."""
    if _OWNER_PROBE is not None:
        return _OWNER_PROBE(path)
    mode = os.stat(path).st_mode
    if not stat.S_ISCHR(mode):
        raise OSError(f"device path is not a character device: {path}")
    if not os.access(path, os.R_OK | os.W_OK):
        raise PermissionError(f"device permission denied: {path}")
    executable = shutil.which("fuser")
    if executable is None:
        raise OSError("fuser is unavailable; ownership cannot be checked safely")
    result = _COMMAND_RUNNER(
        [executable, path],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    if result.returncode == 1 and not result.stderr.strip():
        return ()
    if result.returncode != 0:
        raise OSError(f"fuser failed for {path}: {result.stderr.strip() or result.returncode}")
    return tuple(int(value) for value in re.findall(r"\b\d+\b", result.stdout))


def _service_state() -> _ServiceState | None:
    executable = shutil.which("systemctl")
    if executable is None:
        return None
    result = _COMMAND_RUNNER(
        [
            executable,
            "show",
            _TFMINI_SERVICE,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=MainPID",
        ],
        capture_output=True,
        text=True,
        timeout=3.0,
        check=False,
    )
    if result.returncode != 0:
        return None
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    if values.get("LoadState") != "loaded":
        return None
    try:
        main_pid = int(values.get("MainPID", "0"))
    except ValueError:
        main_pid = 0
    return _ServiceState(values.get("ActiveState", "unknown"), main_pid)


def _pid_label(pid: int) -> str:
    try:
        name = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        name = "unknown"
    return f"PID {pid} ({name})"


def _systemctl(action: str) -> None:
    executable = shutil.which("systemctl")
    if executable is None:
        raise OSError("systemctl is unavailable")
    result = _COMMAND_RUNNER(
        [executable, action, _TFMINI_SERVICE],
        capture_output=True,
        text=True,
        timeout=10.0,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or str(result.returncode)
        raise OSError(f"systemctl {action} {_TFMINI_SERVICE} failed: {detail}")


def _prepare_device(path: str, *, output: TextIO) -> bool:
    """Check owners and pause the known service; return whether it was paused."""
    owners = _probe_owners(path)
    if not owners:
        print(f"owner check: {path}=free", file=output)
        return False

    state = _service_state()
    if (
        state is None
        or state.active_state != "active"
        or state.main_pid <= 0
        or set(owners) != {state.main_pid}
    ):
        labels = ", ".join(_pid_label(pid) for pid in owners)
        raise OSError(f"unknown or mixed owner on {path}: {labels}; nothing was stopped")

    print(
        f"owner check: {path} is held only by {_TFMINI_SERVICE} "
        f"(MainPID={state.main_pid}); pausing it",
        file=output,
    )
    _systemctl("stop")
    try:
        remaining = _probe_owners(path)
        if remaining:
            labels = ", ".join(_pid_label(pid) for pid in remaining)
            raise OSError(f"{path} remained busy after service stop: {labels}")
    except BaseException as exc:
        try:
            _systemctl("start")
        except OSError as restart_error:
            exc.add_note(f"service restart also failed: {restart_error}")
        else:
            exc.add_note(f"{_TFMINI_SERVICE} was restarted after preflight failed")
        raise
    return True


def _restore_service(*, output: TextIO, error_output: TextIO) -> bool:
    try:
        _systemctl("start")
    except OSError as exc:
        print(f"cleanup error: could not restart {_TFMINI_SERVICE}: {exc}", file=error_output)
        return False
    print(f"cleanup: restarted {_TFMINI_SERVICE}", file=output)
    return True


def _load_gpio_factory() -> Callable[..., _DigitalOutput]:
    if _GPIO_FACTORY_LOADER is not None:
        return _GPIO_FACTORY_LOADER()
    from rear_warning.outputs.hardware import load_gpiozero_output_factory

    return load_gpiozero_output_factory()  # type: ignore[return-value]


def _run_led(options: _Options, *, output: TextIO, error_output: TextIO) -> _TestResult:
    pins = LedPins.raspberry_pi_default()
    if options.gpio_chip is None:
        detail = (
            "GPIO chip auto-detection unavailable; inspect the Pi gpiochip labels "
            "and rerun with an explicit --gpio-chip N"
        )
        print(f"LED failed: {detail}", file=error_output)
        return _TestResult("LED", "FAIL", detail)
    gpio: _DigitalOutput | None = None
    run_error: BaseException | None = None
    try:
        gpio = _load_gpio_factory()(pins, chip=options.gpio_chip)
        for pin in (pins.red, pins.yellow, pins.green):
            gpio.write(pin, False)
        for label, pin in (("red", pins.red), ("yellow", pins.yellow), ("green", pins.green)):
            print(f"LED: {label} ON for {_LED_SECONDS:g} second", file=output)
            gpio.write(pin, True)
            _SLEEP(_LED_SECONDS)
            gpio.write(pin, False)
    except BaseException as exc:
        run_error = exc
    finally:
        if gpio is not None:
            for pin in (pins.red, pins.yellow, pins.green):
                try:
                    gpio.write(pin, False)
                except BaseException as exc:
                    run_error = run_error or exc
            try:
                gpio.close()
            except BaseException as exc:
                run_error = run_error or exc

    if isinstance(run_error, KeyboardInterrupt):
        raise run_error
    if run_error is not None:
        print(f"LED failed: {run_error}", file=error_output)
        return _TestResult("LED", "FAIL", str(run_error))

    try:
        observed = _INPUT("目視是否依序只有紅、黃、綠各亮約 1 秒，最後全關？[y/N] ")
    except EOFError:
        observed = ""
    if observed.strip().lower() in {"y", "yes"}:
        return _TestResult("LED", "PASS", "operator confirmed sequence; outputs closed/off")
    return _TestResult("LED", "INCONCLUSIVE", "software sequence completed; visual confirmation not received")


def _load_register_io() -> Callable[[int], _RegisterIO]:
    if _REGISTER_IO_LOADER is not None:
        return _REGISTER_IO_LOADER()
    from rear_warning.sensors.bno055.hardware import load_smbus_register_io

    return load_smbus_register_io()  # type: ignore[return-value]


def _run_bno(*, output: TextIO, error_output: TextIO) -> _TestResult:
    bus: _RegisterIO | None = None
    errors: list[str] = []
    matched_address: int | None = None
    cleanup_error: BaseException | None = None
    try:
        bus = _load_register_io()(_BNO_BUS)
        for address in _BNO_ADDRESSES:
            try:
                chip_id = bus.read_byte_data(address, _BNO_CHIP_ID_REGISTER)
            except (OSError, RuntimeError) as exc:
                errors.append(f"0x{address:02X}: {exc}")
                continue
            print(f"BNO055: address=0x{address:02X}, CHIP_ID=0x{chip_id:02X}", file=output)
            if chip_id == _BNO_EXPECTED_CHIP_ID:
                matched_address = address
                break
            errors.append(
                f"0x{address:02X}: CHIP_ID 0x{chip_id:02X}, expected 0x{_BNO_EXPECTED_CHIP_ID:02X}"
            )
    except (OSError, RuntimeError, ValueError) as exc:
        errors.append(str(exc))
    finally:
        if bus is not None:
            try:
                bus.close()
            except BaseException as exc:
                cleanup_error = exc

    if cleanup_error is not None:
        errors.append(f"SMBus cleanup: {cleanup_error}")
    if matched_address is not None and cleanup_error is None:
        return _TestResult(
            "BNO055",
            "PASS",
            f"CHIP_ID 0xA0 at 0x{matched_address:02X}; identity/basic I2C only",
        )
    detail = "; ".join(errors) or "BNO055 identity was not read"
    print(f"BNO055 failed: {detail}", file=error_output)
    return _TestResult("BNO055", "FAIL", detail)


def _open_serial() -> _SerialConnection:
    if _SERIAL_FACTORY is not None:
        return _SERIAL_FACTORY()
    import serial

    connection = serial.Serial(
        port=None,
        baudrate=_RADAR_BAUDRATE,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=_RADAR_TIMEOUT,
        write_timeout=0,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
        exclusive=True,
    )
    connection.port = _RADAR_PORT
    connection.dtr = False
    connection.rts = False
    try:
        connection.open()
        fcntl.ioctl(connection.fileno(), termios.TIOCEXCL)
    except BaseException:
        if connection.is_open:
            connection.close()
        raise
    return connection  # type: ignore[return-value]


def _run_radar(*, output: TextIO, error_output: TextIO) -> _TestResult:
    connection: _SerialConnection | None = None
    parser = IoRadar701StreamParser()
    total_bytes = 0
    run_error: BaseException | None = None
    cleanup_error: BaseException | None = None
    try:
        connection = _open_serial()
        deadline = _MONOTONIC() + _RADAR_SECONDS
        while _MONOTONIC() < deadline:
            chunk = connection.read(_RADAR_READ_SIZE)
            if not chunk:
                continue
            total_bytes += len(chunk)
            parser.feed(chunk)
    except BaseException as exc:
        run_error = exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except BaseException as exc:
                cleanup_error = exc

    if isinstance(run_error, KeyboardInterrupt):
        raise run_error
    statistics = parser.statistics
    print(
        "ioRadar701: "
        f"bytes={total_bytes}, valid_status={statistics.status_frames}, "
        f"valid_target={statistics.target_frames}, checksum_failures={statistics.checksum_failures}, "
        f"sync_discarded={statistics.sync_discarded_bytes}, trailing={statistics.trailing_bytes}",
        file=output,
    )
    if run_error is not None or cleanup_error is not None:
        details = []
        if run_error is not None:
            details.append(str(run_error))
        if cleanup_error is not None:
            details.append(f"UART cleanup: {cleanup_error}")
        detail = "; ".join(details)
        print(f"ioRadar701 failed: {detail}", file=error_output)
        return _TestResult("ioRadar701", "FAIL", detail)
    if statistics.valid_frames == 0:
        return _TestResult(
            "ioRadar701",
            "INCONCLUSIVE",
            "receive window completed but no checksum-valid A5 01/A5 02 frame was found",
        )
    detail = (
        f"{statistics.status_frames} status and {statistics.target_frames} target frames; "
        "valid framing only, not distance accuracy"
    )
    if statistics.target_frames == 0:
        detail += "; zero target frames alone does not prove wiring failure"
    return _TestResult("ioRadar701", "PASS", detail)


def _print_results(results: Sequence[_TestResult], *, output: TextIO) -> None:
    print("quick_test_summary", file=output)
    for result in results:
        print(f"{result.item}: {result.status} - {result.detail}", file=output)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    namespace = parser.parse_args(None if argv is None else list(argv))
    options = _options(namespace)
    _print_plan(options, output=sys.stdout)
    try:
        _INPUT("確認以上為現地接法後按 Enter 開始；Ctrl+C 取消：")
    except EOFError:
        print("cancelled: EOF received; no hardware was opened", file=sys.stderr)
        return _USAGE_OR_CANCELLED
    except KeyboardInterrupt:
        print("\ncancelled: no hardware was opened", file=sys.stderr)
        return _INTERRUPTED

    items = _selected_items(options.mode)
    results: list[_TestResult] = []
    preflight_errors: dict[str, str] = {}
    service_paused = False
    interrupted = False
    service_restored = True
    try:
        if "radar" in items:
            try:
                service_paused = _prepare_device(_RADAR_PORT, output=sys.stdout)
            except (OSError, PermissionError, subprocess.SubprocessError) as exc:
                preflight_errors["radar"] = str(exc)
        if "bno" in items:
            try:
                service_paused = (
                    _prepare_device(f"/dev/i2c-{_BNO_BUS}", output=sys.stdout)
                    or service_paused
                )
            except (OSError, PermissionError, subprocess.SubprocessError) as exc:
                preflight_errors["bno"] = str(exc)

        for item in items:
            if item in preflight_errors:
                results.append(_TestResult(item.upper(), "FAIL", preflight_errors[item]))
                continue
            if item == "led":
                results.append(_run_led(options, output=sys.stdout, error_output=sys.stderr))
            elif item == "bno":
                results.append(_run_bno(output=sys.stdout, error_output=sys.stderr))
            else:
                results.append(_run_radar(output=sys.stdout, error_output=sys.stderr))
    except KeyboardInterrupt:
        interrupted = True
        print("\ninterrupted: cleaning up opened resources", file=sys.stderr)
    finally:
        if service_paused:
            service_restored = _restore_service(output=sys.stdout, error_output=sys.stderr)

    _print_results(results, output=sys.stdout)
    if interrupted:
        return _INTERRUPTED
    if not service_restored or not results or any(not result.passed for result in results):
        return _TEST_FAILED
    return _SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())
