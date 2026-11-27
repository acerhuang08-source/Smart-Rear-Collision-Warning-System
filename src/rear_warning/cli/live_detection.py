"""Manual Enter-to-start live fusion on Raspberry Pi, SSH and terminal sessions."""

from __future__ import annotations

import argparse
import math
import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from types import FrameType
from typing import NoReturn

from rear_warning.live.hardware import (
    DistanceDevice, HardwareFactories, MotionDevice,
    check_devices_available, load_hardware_factories,
)
from rear_warning.live.terminal import BoundedTerminal, LivePresentation, format_summary
from rear_warning.sensors.bno055.device import RegisterIO
from rear_warning.synchronization import SamplingSchedule, SensorSamplingCoordinator

_CLOCK: Callable[[], float] = time.monotonic
_SLEEP: Callable[[float], None] = time.sleep
_FACTORIES: HardwareFactories | None = None
_PREFLIGHT: Callable[[str, int], None] = check_devices_available
_TERMINAL_FACTORY: Callable[..., BoundedTerminal] = BoundedTerminal


def positive(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("必須是有限正數")
    return parsed


def address(value: str) -> int:
    parsed = int(value, 0)
    if parsed not in (0x28, 0x29):
        raise argparse.ArgumentTypeError("位址必須是 0x28 或 0x29")
    return parsed


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="LIVE 實際感測：Enter 開始，Ctrl+C 停止")
    result.add_argument("--port", default="/dev/ttyAMA0")
    result.add_argument("--baudrate", type=int, default=115200)
    result.add_argument("--i2c-bus", type=int, default=1)
    result.add_argument("--bno055-address", type=address, default=0x29)
    result.add_argument("--uart-timeout", type=positive, default=0.02)
    result.add_argument("--refresh-hz", type=positive, default=5.0)
    result.add_argument("--duration", type=positive, help="可選的採集秒數上限；預設等 Ctrl+C")
    result.add_argument("--confirm-hardware", action="store_true")
    result.add_argument("--plain", action="store_true", help="逐行純文字輸出，不使用 ANSI 固定面板")
    return result


class LiveSignals:
    """Main-thread interruption and five-second stalled-operation watchdog.

    Signals interrupt normal userspace/syscall waits, not an uninterruptible
    kernel/driver hang. No kernel I2C settings are changed.
    """

    def __init__(self) -> None:
        self.saved: dict[int, object] = {}

    @staticmethod
    def interrupted(signum: int, frame: FrameType | None) -> NoReturn:
        raise KeyboardInterrupt(f"signal={signum}")

    @staticmethod
    def timeout(signum: int, frame: FrameType | None) -> NoReturn:
        raise TimeoutError("感測／初始化操作超過 5 秒，停止並嘗試 cleanup")

    def install(self) -> None:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
            self.saved[signum] = signal.getsignal(signum)
            signal.signal(signum, self.timeout if signum == signal.SIGALRM else self.interrupted)

    def arm(self) -> None:
        signal.setitimer(signal.ITIMER_REAL, 5.0)

    def restore(self) -> None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for signum, handler in self.saved.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]


def error_chain(error: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = error
    while current is not None and len(parts) < 6:
        parts.append(f"{type(current).__name__}: {current}")
        current = current.__cause__
    return " <- ".join(parts).replace("\n", " ")[:800]


def emergency_report(message: str) -> None:
    """Best effort bounded stderr reporting after resources have been closed."""
    try:
        fd = sys.stderr.fileno()
        blocking = os.get_blocking(fd)
        try:
            os.set_blocking(fd, False)
            os.write(fd, (message + "\n").encode("utf-8"))
        finally:
            os.set_blocking(fd, blocking)
    except (OSError, ValueError):
        pass  # A disconnected terminal cannot receive the cleanup report.


def run(options: argparse.Namespace) -> int:
    started = _CLOCK()
    distance: DistanceDevice | None = None
    motion: MotionDevice | None = None
    register_io: RegisterIO | None = None
    coordinator: SensorSamplingCoordinator | None = None
    terminal: BoundedTerminal | None = None
    signals = LiveSignals()
    errors: list[str] = []
    cleanup_errors: list[str] = []
    exit_code = 0
    interrupted = False
    try:
        signals.install()
        signals.arm()
        terminal = _TERMINAL_FACTORY(sys.stdout.fileno(), monotonic=_CLOCK, plain=options.plain)
        terminal.message("[LIVE 實際感測] 初始化 UART／BNO055 NDOF…", _CLOCK(), started=started)
        terminal.flush_once(_CLOCK())
        terminal.flush_once(_CLOCK())
        _PREFLIGHT(options.port, options.i2c_bus)
        signals.arm()
        factories = _FACTORIES or load_hardware_factories()
        distance = factories.distance(
            port=options.port, baudrate=options.baudrate, timeout=options.uart_timeout,
        )
        distance.open()
        signals.arm()
        register_io = factories.register_io(options.i2c_bus)
        motion = factories.motion(
            register_io, address=options.bno055_address, monotonic=_CLOCK, sleep=_SLEEP,
        )
        motion.read_identity()
        signals.arm()
        mode, status, error = motion.initialize_ndof()
        terminal.message(
            f"[LIVE 實際感測] NDOF={mode:#04x}/{status:#04x}/{error:#04x} 暖機／等待有效資料",
            _CLOCK(), started=started,
        )
        presentation = LivePresentation(terminal, started=started, refresh_hz=options.refresh_hz)
        coordinator = SensorSamplingCoordinator(
            distance, motion, schedule=SamplingSchedule(), monotonic=_CLOCK, sleep=_SLEEP,
        )

        def tick(now: float) -> None:
            signals.arm()
            presentation.last_valid_motion = coordinator.last_valid_motion_timestamp
            presentation.on_tick(now)

        coordinator.run(
            duration_seconds=options.duration, maximum_samples=None,
            should_stop=lambda: False, on_tick=tick, on_sample=presentation.on_sample,
        )
        if coordinator.statistics.matched_samples == 0:
            raise RuntimeError("本次沒有有效配對資料；請確認 UART／I²C 接線與供電")
    except KeyboardInterrupt:
        interrupted = True
        exit_code = 130
    except Exception as exc:
        exit_code = 1
        errors.append(error_chain(exc))
    finally:
        # Keep independent ownership of the bus if BNO055 construction failed.
        resources = [("UART", distance), ("BNO055", motion if motion is not None else register_io)]
        for name, resource in resources:
            if resource is not None:
                try:
                    signals.arm()
                    resource.close()
                except BaseException as exc:
                    cleanup_errors.append(f"{name}: {error_chain(exc)}")
        signal.setitimer(signal.ITIMER_REAL, 0)
        if terminal is not None:
            terminal.discard_pending()
        if cleanup_errors:
            exit_code = 1
        elapsed = max(0.0, _CLOCK() - started)
        stats = {} if coordinator is None else asdict(coordinator.statistics)
        sample_elapsed = 0.0 if coordinator is None else coordinator.sampling_elapsed_seconds
        counts = " ".join(f"{name}={stats.get(name, 0)}" for name in (
            "valid_distance_measurements", "bno055_measurements", "startup_motion_reads",
            "startup_motion_discards", "matched_samples", "uart_empty_reads",
            "invalid_distance_count", "invalid_motion_count", "stale_motion_count",
            "future_motion_count", "no_motion_count", "missed_bno055_periods",
        ))
        summary = (
            f"[LIVE 結束] total_elapsed={elapsed:.3f}s sampling_elapsed={sample_elapsed:.3f}s "
            f"{counts} valid_motion_total="
            f"{stats.get('bno055_measurements', 0) + stats.get('startup_valid_motion_measurements', 0)} "
            f"errors={len(errors)} interrupted={str(interrupted).lower()} "
            f"cleanup_completed={str(not cleanup_errors).lower()} "
            f"dropped_status={0 if terminal is None else terminal.dropped_status} "
            f"dropped_events={0 if terminal is None else terminal.dropped_events} exit_code={exit_code}"
        )
        stop_reason = "使用者／訊號中止" if interrupted else "異常" if exit_code else "採集時間上限"
        report = format_summary("\n".join([
            *(f"[LIVE ERROR UNCERTAIN] {item}" for item in errors),
            *(f"[cleanup ERROR] {item}" for item in cleanup_errors),
            f"停止原因：{stop_reason}", summary,
        ]))
        try:
            if terminal is None:
                emergency_report(report)
            else:
                output_error: str | None = None
                try:
                    terminal.finish(report)
                except (OSError, ValueError) as exc:
                    output_error = f"[輸出 ERROR] {exc}; exit_code=1"
                    exit_code = 1
                finally:
                    try:
                        terminal.close()
                    except OSError as exc:
                        output_error = (output_error or "") + f"\n[終端機還原 ERROR] {exc}; exit_code=1"
                        exit_code = 1
                if output_error is not None:
                    # Report only after leaving the alternate screen, otherwise
                    # a successful restore could hide the original error.
                    emergency_report(report + "\n" + output_error)
        finally:
            signals.restore()
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    cli = parser()
    options = cli.parse_args(None if argv is None else list(argv))
    if not options.port.strip() or options.baudrate <= 0 or options.i2c_bus < 0:
        cli.error("UART 路徑／鮑率／I²C bus 無效")
    if options.uart_timeout > 0.1 or options.refresh_hz > 20:
        cli.error("UART timeout 不得超過動作採樣間隔 0.1 秒；refresh-hz 不得超過 20")
    try:
        print(
            f"LIVE 實際感測 | UART={options.port} baud={options.baudrate} "
            f"timeout={options.uart_timeout}s | I²C bus={options.i2c_bus} "
            f"address={options.bno055_address:#04x}\n"
            f"動作間隔=0.1s 最大配對 age=0.2s 刷新={options.refresh_hz}Hz "
            f"duration={options.duration if options.duration is not None else '直到 Ctrl+C'}\n"
            "Enter 後開 UART／I²C，寫入 BNO055 CONFIGMODE／NDOF；結束回 CONFIGMODE。\n"
            "請確認供電與接線，且裝置沒有其他使用者；本程式不會停止既有程序。",
            file=sys.stdout, flush=True,
        )
        if not options.confirm_hardware:
            print("尚未確認硬體：請提供 --confirm-hardware；未開啟感測器。", file=sys.stderr)
            return 2
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print("LIVE 需要互動終端機 stdin/stdout；拒絕 pipe／EOF 自動啟動。", file=sys.stderr)
            return 2
        print("按 Enter 開始偵測，Ctrl+C 離開", file=sys.stdout, flush=True)
        answer = sys.stdin.readline()
        if answer not in ("\n", "\r\n"):
            print("已取消（EOF 或非空白 Enter）；未開啟感測器。", file=sys.stdout, flush=True)
            return 2
    except KeyboardInterrupt:
        return 130
    except (EOFError, OSError):
        return 2
    return run(options)


if __name__ == "__main__":
    raise SystemExit(main())
