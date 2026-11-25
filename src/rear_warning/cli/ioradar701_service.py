"""Unattended ioRadar701 distance-warning LED entry for a systemd service.

This reuses the same single-owner loop, selector, thresholds and LED policy as
``ioradar701_led``; only the start-up and life-cycle handling differ:

* no interactive terminal or Enter gate (systemd has no TTY), but the explicit
  ``--confirm-hardware`` and ``--enable-led`` opt-in flags remain mandatory;
* SIGTERM/SIGHUP stop the loop through the existing interrupt path so LEDs are
  switched off, GPIO/UART/I2C are closed and the capture is finalized;
* captures rotate every ``--segment-seconds`` and the service's own output root
  is pruned to ``--max-total-mb``. Only directories this service created (named
  ``service-*`` and holding the marker file) are ever removed.

A BNO055 initialization failure ends the run with a non-zero exit so systemd
restarts it (operator decision 2026-11-25).
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Protocol, cast

from rear_warning.cli import ioradar701_shadow
from rear_warning.live.radar_led import RadarLedObserver
from rear_warning.outputs import LedPins
from rear_warning.outputs.hardware import load_gpiozero_output_factory

MARKER_NAME = ".rear-warning-autostart"
CAPTURE_PREFIX = "service"
DEFAULT_OUTPUT_ROOT = Path("local-state/autostart-ioradar701")
STOP_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGHUP)


class _ClosableOutput(Protocol):
    def write(self, pin: int, active: bool) -> None: ...

    def close(self) -> None: ...


class _OutputFactory(Protocol):
    def __call__(self, pins: LedPins, *, chip: int) -> _ClosableOutput: ...


_OUTPUT_FACTORY: _OutputFactory | None = None


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必須是正整數")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ioradar701-service",
        description=(
            "Unattended ioRadar701 distance warning to shared-three GPIO LEDs "
            "for systemd; no TTY gate, explicit opt-in flags required."
        ),
    )
    ioradar701_shadow._add_live_arguments(parser)
    parser.set_defaults(output_root=DEFAULT_OUTPUT_ROOT, refresh_hz=0.2, plain=True)
    parser.add_argument(
        "--enable-led",
        action="store_true",
        help="explicitly opt in to BCM17/27/22 physical GPIO output",
    )
    parser.add_argument("--gpio-chip", type=int, default=0)
    parser.add_argument(
        "--segment-seconds",
        type=ioradar701_shadow._positive,
        default=600.0,
        help="rotate the capture directory after this many seconds (default: 600)",
    )
    parser.add_argument(
        "--max-total-mb",
        type=_positive_int,
        default=2048,
        help="upper bound for this service's own captures in MiB (default: 2048)",
    )
    return parser


def _validate(parser: argparse.ArgumentParser, options: argparse.Namespace) -> bool:
    if options.baudrate <= 0 or options.i2c_bus < 0 or not options.port.strip():
        parser.error("UART／I²C 參數無效")
    if options.refresh_hz > 20 or options.uart_timeout > options.motion_period:
        parser.error("refresh-hz 不得超過 20，UART timeout 不得超過 motion-period")
    if options.gpio_chip < 0:
        parser.error("gpio-chip 不得為負數")
    if not math.isfinite(options.segment_seconds):
        parser.error("segment-seconds 必須是有限正數")
    if not options.confirm_hardware or not options.enable_led:
        print(
            f"{parser.prog}: error: --confirm-hardware 與 --enable-led 都是必要條件；"
            "零 UART／I²C／GPIO I/O",
            file=sys.stderr,
        )
        return False
    return True


def _directory_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


def _service_directories(root: Path) -> list[Path]:
    """Return this service's own capture directories, oldest name first."""
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return []
    owned = [
        entry
        for entry in entries
        if entry.name.startswith(f"{CAPTURE_PREFIX}-")
        and entry.is_dir()
        and not entry.is_symlink()
        and (entry / MARKER_NAME).is_file()
    ]
    return sorted(owned, key=lambda item: item.name)


def prune_service_captures(
    root: Path,
    max_total_bytes: int,
    *,
    keep: frozenset[Path] = frozenset(),
) -> tuple[list[Path], list[str]]:
    """Delete the oldest service-owned captures until the total fits.

    Directories in ``keep`` (the capture currently being written) and anything
    not created by this service are never removed. Returns removed paths and
    non-fatal errors.
    """
    if max_total_bytes <= 0:
        raise ValueError("max_total_bytes must be positive")
    owned = _service_directories(root)
    sizes = {path: _directory_size(path) for path in owned}
    total = sum(sizes.values())
    removed: list[Path] = []
    errors: list[str] = []
    resolved_keep = {path.resolve() for path in keep}
    for path in owned:
        if total <= max_total_bytes:
            break
        if path.resolve() in resolved_keep:
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            errors.append(f"prune {path.name}: {type(exc).__name__}: {exc}")
            continue
        total -= sizes[path]
        removed.append(path)
    return removed, errors


class _CaptureRotation:
    """Mark new captures and bound the service output root."""

    def __init__(self, root: Path, max_total_bytes: int) -> None:
        self._root = root
        self._max_total_bytes = max_total_bytes
        self._current: Path | None = None

    def created(self, directory: Path) -> None:
        (directory / MARKER_NAME).write_text(
            "created by rear_warning.cli.ioradar701_service; may be pruned\n",
            encoding="utf-8",
        )
        self._current = directory

    def closed(self, _directory: Path) -> None:
        self.prune()

    def prune(self) -> None:
        keep = frozenset() if self._current is None else frozenset({self._current})
        removed, errors = prune_service_captures(
            self._root, self._max_total_bytes, keep=keep
        )
        for path in removed:
            print(f"pruned_capture={path}", flush=True)
        for item in errors:
            print(f"service_warning: {item}", file=sys.stderr, flush=True)


@contextmanager
def _stop_signals_raise_interrupt() -> Iterator[None]:
    """Turn the first SIGTERM/SIGHUP into KeyboardInterrupt; ignore repeats."""
    stopping = False
    previous: dict[signal.Signals, Callable[[int, FrameType | None], object] | int] = {}

    def handler(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print(f"service_stop_signal={signal.Signals(signum).name}", flush=True)
        raise KeyboardInterrupt

    for signum in STOP_SIGNALS:
        old = signal.getsignal(signum)
        # getsignal() returns None for handlers not installed from Python.
        previous[signum] = signal.SIG_DFL if old is None else old
        signal.signal(signum, handler)
    try:
        yield
    finally:
        for signum, old_handler in previous.items():
            signal.signal(signum, old_handler)  # type: ignore[arg-type]


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(None if argv is None else list(argv))
    if not _validate(parser, options):
        return 2
    options.plain = True
    root = Path(options.output_root)
    max_total_bytes = options.max_total_mb * 1024 * 1024
    print(
        "ioRadar701 自啟動距離警示＋shared_3 LED（systemd，無 TTY）\n"
        f"UART={options.port} {options.baudrate}/8N1 receive-only；"
        f"I²C bus={options.i2c_bus} BNO055={options.bno055_address:#04x}；"
        f"gpiochip{options.gpio_chip}\n"
        f"capture={root} 每 {options.segment_seconds:g}s 切段，上限 "
        f"{options.max_total_mb} MiB；只刪除本服務建立且帶標記的 {CAPTURE_PREFIX}-* 目錄。\n"
        "selector、門檻、遲滯與 LED 規則與 ioradar701_led 相同；"
        "BNO055 初始化失敗即非零結束，由 systemd 重啟。",
        flush=True,
    )
    rotation = _CaptureRotation(root, max_total_bytes)
    try:
        root.mkdir(parents=True, exist_ok=True)
        rotation.prune()
    except OSError as exc:
        print(f"service_warning: capture root: {exc}", file=sys.stderr, flush=True)

    with _stop_signals_raise_interrupt():
        pins = LedPins.raspberry_pi_default()
        try:
            factory = _OUTPUT_FACTORY or cast(
                _OutputFactory, load_gpiozero_output_factory()
            )
            gpio = factory(pins, chip=options.gpio_chip)
        except KeyboardInterrupt:
            return 130
        except BaseException as exc:
            print(
                f"GPIO 啟動失敗，燈號未生效：{type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1

        observer = RadarLedObserver(gpio, pins)
        result = 1
        try:
            result = ioradar701_shadow._run_live(
                options,
                observer=observer,
                mode="SERVICE_RADAR_DISTANCE_LED",
                label="AUTOSTART RADAR DISTANCE LED／實體 GPIO 已啟用",
                gpio_enabled=True,
                finish_message="[AUTOSTART 結束] 感測已停止；已嘗試全關並釋放 GPIO",
                t4_phase_prompts=False,
                capture_prefix=CAPTURE_PREFIX,
                segment_seconds=options.segment_seconds,
                on_capture_created=rotation.created,
                on_capture_closed=rotation.closed,
                operator_input=False,
            )
        except KeyboardInterrupt:
            result = 130
        finally:
            if not observer.closed:
                try:
                    observer.abort_cleanup()
                except BaseException as exc:
                    print(
                        f"GPIO cleanup 失敗：{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    result = 1
        return result


if __name__ == "__main__":
    raise SystemExit(main())
