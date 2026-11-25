"""Explicitly opted-in ioRadar701 distance-warning LED live entry."""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Callable, Protocol, cast

from rear_warning.cli import ioradar701_shadow
from rear_warning.live.radar_led import RadarLedObserver
from rear_warning.outputs import LedPins
from rear_warning.outputs.hardware import load_gpiozero_output_factory
from rear_warning.warning import HysteresisThresholds, WarningPolicy


class _ClosableOutput(Protocol):
    def write(self, pin: int, active: bool) -> None: ...

    def close(self) -> None: ...


class _OutputFactory(Protocol):
    def __call__(self, pins: LedPins, *, chip: int) -> _ClosableOutput: ...


_OUTPUT_FACTORY: _OutputFactory | None = None
_SLEEP: Callable[[float], None] = time.sleep


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ioradar701-led",
        description=(
            "Opt-in live ioRadar701 distance warning to shared-three GPIO LEDs; "
            "BNO055 remains recorded but does not gate LEDs."
        ),
    )
    ioradar701_shadow._add_live_arguments(parser)
    parser.set_defaults(output_root=Path("hardware-results/radar-led"))
    parser.add_argument(
        "--enable-led",
        action="store_true",
        help="explicitly opt in to BCM17/27/22 physical GPIO output",
    )
    parser.add_argument("--gpio-chip", type=int, default=0)
    parser.add_argument(
        "--start-delay",
        type=float,
        default=0.0,
        metavar="SECONDS",
        help=(
            "wait this many seconds after operator confirmation before opening "
            "GPIO/UART/I2C (default: 0)"
        ),
    )
    return parser


def _validate(parser: argparse.ArgumentParser, options: argparse.Namespace) -> bool:
    if options.baudrate <= 0 or options.i2c_bus < 0 or not options.port.strip():
        parser.error("UART／I²C 參數無效")
    if options.refresh_hz > 20 or options.uart_timeout > options.motion_period:
        parser.error("refresh-hz 不得超過 20，UART timeout 不得超過 motion-period")
    if options.gpio_chip < 0:
        parser.error("gpio-chip 不得為負數")
    if not math.isfinite(options.start_delay) or options.start_delay < 0:
        parser.error("start-delay 必須是有限的非負秒數")
    if not options.confirm_hardware or not options.enable_led:
        parser.print_usage(sys.stderr)
        print(
            f"{parser.prog}: error: --confirm-hardware 與 --enable-led 都是必要條件；"
            "零 UART／I²C／GPIO I/O",
            file=sys.stderr,
        )
        return False
    return True


def _print_plan(options: argparse.Namespace) -> None:
    entry = WarningPolicy().thresholds
    release = HysteresisThresholds()
    print(
        "ioRadar701 距離警示＋shared_3 LED（明確 opt-in）\n"
        f"UART={options.port} {options.baudrate}/8N1 receive-only；"
        f"I²C bus={options.i2c_bus} BNO055={options.bno055_address:#04x}；"
        f"gpiochip{options.gpio_chip}\n"
        "LED：紅對 BCM17/Pin11、黃對 BCM27/Pin13、綠對 BCM22/Pin15，"
        "active-high；每顆各自 220 Ω，同色兩顆共控。\n"
        f"距離：DANGER < {entry.danger_below_m:g}m；"
        f"WARNING 至 {entry.safe_above_m:g}m（含）；"
        f"SAFE > {entry.safe_above_m:g}m 至 {entry.maximum_valid_m:g}m；"
        f"解除門檻 {release.danger_release_m:g}m/{release.safe_release_m:g}m；"
        f"TTL={options.stale_after:g}s。\n"
        "SAFE=綠、WARNING=黃、DANGER=紅、SENSOR_FAULT=紅＋黃；"
        "正常 NO_TARGETS 與未啟動=全關。BNO055 只顯示／記錄，不門控燈號。\n"
        "每個位置穩定後可逐行輸入 far、warning、danger、release 並按 Enter；"
        "只有看到『已接收並保存』才算事件已保存。\n"
        "不改 GPIO drive strength。GPIO、UART、I²C 各只有本程序一個 owner；"
        "Ctrl+C、正常結束或錯誤都會嘗試全關並 close。\n"
        f"操作員確認後先保留 {options.start_delay:g} 秒啟動空窗；"
        "空窗結束才開啟硬體並開始採集。",
        flush=True,
    )


def _wait_start_delay(seconds: float) -> None:
    """Wait before hardware acquisition while keeping the countdown interruptible."""

    if seconds <= 0:
        return
    whole_seconds = int(seconds)
    remainder = seconds - whole_seconds
    for remaining in range(whole_seconds, 0, -1):
        print(f"啟動空窗：剩餘 {remaining} 秒（尚未開啟硬體）", flush=True)
        _SLEEP(1.0)
    if remainder > 0:
        print(
            f"啟動空窗：剩餘 {remainder:g} 秒（尚未開啟硬體）",
            flush=True,
        )
        _SLEEP(remainder)
    print("啟動空窗結束；現在開啟硬體並開始採集。", flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    options = parser.parse_args(None if argv is None else list(argv))
    if not _validate(parser, options):
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("需要互動終端機；拒絕 pipe／EOF，零硬體 I/O。", file=sys.stderr)
        return 2
    _print_plan(options)
    try:
        print("確認 LED 診斷已結束且接線／負載可接受後，按 Enter 開啟硬體；Ctrl+C 取消")
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        return 130
    if answer not in ("\n", "\r\n"):
        print("已取消；零硬體 I/O。")
        return 2

    try:
        _wait_start_delay(options.start_delay)
    except KeyboardInterrupt:
        print("已於啟動空窗取消；零硬體 I/O。")
        return 130

    pins = LedPins.raspberry_pi_default()
    try:
        factory = _OUTPUT_FACTORY or cast(
            _OutputFactory,
            load_gpiozero_output_factory(),
        )
        gpio = factory(pins, chip=options.gpio_chip)
    except BaseException as exc:
        print(
            f"GPIO 啟動失敗，燈號未生效：{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    observer = RadarLedObserver(gpio, pins)
    result = ioradar701_shadow._run_live(
        options,
        observer=observer,
        mode="LIVE_RADAR_DISTANCE_LED",
        label="RADAR DISTANCE LED／實體 GPIO 已啟用",
        gpio_enabled=True,
        finish_message="[RADAR LED 結束] 感測已停止；已嘗試全關並釋放 GPIO",
        t4_phase_prompts=False,
    )
    if not observer.closed:
        try:
            observer.abort_cleanup()
        except BaseException as exc:
            print(
                f"GPIO cleanup 失敗：{type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            return 1
    return result


if __name__ == "__main__":
    raise SystemExit(main())
