"""Zero-hardware acceptance tests of manual-start live acquisition."""

from __future__ import annotations

import io
import os
import pty
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, NoReturn
from types import SimpleNamespace

import pytest

from rear_warning.cli import live_detection as cli
from rear_warning.live.hardware import HardwareFactories
from rear_warning.live.terminal import (
    BoundedTerminal, LivePresentation, PanelEvent, PanelSnapshot, TerminalBackpressureError,
)
from rear_warning.sensors.bno055.models import (
    BNO055Calibration, BNO055Measurement, EulerAngles, Quaternion, Vector3,
)
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement
from rear_warning.synchronization import (
    SensorSamplingCoordinator, SamplingSchedule, SensorSynchronizer,
    TimedDistanceMeasurement, TimedMotionMeasurement,
)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def motion(timestamp: float = 0.0) -> BNO055Measurement:
    return BNO055Measurement(
        timestamp, EulerAngles(0, 0, 0), Quaternion(1, 0, 0, 0),
        Vector3(0, 0, 0), Vector3(0, 0, 9.8), 25,
        BNO055Calibration(0, 3, 0, 0), 0x0C, 0x05, 0x00,
    )


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return 1


class MemoryTerminal:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.lines: list[str] = []
        self.dropped_status = 0
        self.dropped_events = 0
        self.closed = False

    def publish(self, line: str, now: float, *, event: bool = False) -> None:
        self.lines.append(line)

    def record_event(self, event: PanelEvent, plain: str, now: float) -> None:
        self.publish(plain, now, event=True)

    def display(self, snapshot: PanelSnapshot, plain: str, now: float) -> None:
        self.publish(plain, now)

    def message(self, text: str, now: float, *, started: float) -> None:
        self.publish(text, now, event=True)

    def flush_once(self, now: float) -> None:
        pass

    def finish(self, line: str) -> None:
        self.lines.append(line)

    def discard_pending(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class Distance:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.opens = 0
        self.closes = 0
        self.reads = 0
        self.value: Callable[[int], int | None] = lambda _index: 400

    def open(self) -> None:
        self.opens += 1

    def close(self) -> None:
        self.closes += 1

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        self.reads += 1
        self.clock.t = round(self.clock.t + 0.01, 8)
        value = self.value(self.reads)
        return [] if value is None else [TFMiniPlusMeasurement(value, 500, 25.0)]


class Bus:
    def __init__(self) -> None:
        self.closes = 0

    def close(self) -> None:
        self.closes += 1


class Motion:
    def __init__(self, clock: Clock, bus: Bus) -> None:
        self.clock = clock
        self.bus = bus
        self.initializations = 0
        self.reads = 0
        self.closes = 0

    def read_identity(self) -> object:
        return None

    def initialize_ndof(self) -> tuple[int, int, int]:
        self.initializations += 1
        return 0x0C, 0x05, 0x00

    def read_measurement(self) -> BNO055Measurement:
        self.reads += 1
        return motion(self.clock.t)

    def close(self) -> None:
        self.closes += 1
        self.bus.close()


@pytest.fixture
def setup(monkeypatch: pytest.MonkeyPatch) -> tuple[Clock, Distance, Motion, Bus, MemoryTerminal]:
    clock = Clock()
    bus = Bus()
    distance = Distance(clock)
    imu = Motion(clock, bus)
    terminal = MemoryTerminal()
    monkeypatch.setattr(cli, "_CLOCK", clock.now)
    monkeypatch.setattr(cli, "_SLEEP", clock.sleep)
    monkeypatch.setattr(cli, "_PREFLIGHT", lambda *_args: None)
    monkeypatch.setattr(cli, "_FACTORIES", HardwareFactories(
        lambda **_kwargs: distance, lambda _bus: bus, lambda *_args, **_kwargs: imu,
    ))
    monkeypatch.setattr(cli, "_TERMINAL_FACTORY", lambda *_args, **_kwargs: terminal)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(
        stdin=FakeTTY("\n"), stdout=FakeTTY(), stderr=FakeTTY(),
    ))
    return clock, distance, imu, bus, terminal


@pytest.mark.parametrize("case", ["unconfirmed", "help", "bad", "eof", "cancel", "pipe", "output_pipe", "text"])
def test_gate_never_constructs_hardware(
    case: str, setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []

    def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
        called.append("hardware")
        raise AssertionError("hardware before Enter")

    monkeypatch.setattr(cli, "run", forbidden)
    args = ["--confirm-hardware"]
    if case == "unconfirmed":
        args = []
    elif case == "help":
        args = ["--help"]
    elif case == "bad":
        args += ["--uart-timeout", "nan"]
    elif case == "eof":
        monkeypatch.setattr(cli.sys, "stdin", FakeTTY(""))
    elif case == "text":
        monkeypatch.setattr(cli.sys, "stdin", FakeTTY("yes\n"))
    elif case == "pipe":
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n"))
    elif case == "output_pipe":
        monkeypatch.setattr(cli.sys, "stdout", io.StringIO())
    elif case == "cancel":
        monkeypatch.setattr(cli.sys.stdin, "readline", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        result = cli.main(args)
        assert result in (2, 130)
    except SystemExit as exc:
        assert exc.code == (0 if case == "help" else 2)
    assert not called


def test_enter_initializes_once_real_coordinator_fusion_formats_and_cleans(
    setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, distance, imu, bus, terminal = setup

    def enter() -> str:
        assert distance.opens == imu.initializations == imu.reads == 0
        assert "按 Enter 開始偵測" in cli.sys.stdout.getvalue()
        return "\n"

    monkeypatch.setattr(cli.sys.stdin, "readline", enter)
    assert cli.main(["--confirm-hardware", "--duration", "2"]) == 0
    assert distance.opens == imu.initializations == 1
    assert distance.closes == imu.closes == bus.closes == 1
    text = "\n".join(terminal.lines)
    assert "基準=成立" in text and "STABLE_OR_RECEDING" in text
    assert "資料不足" in text and "4.00m" in text
    assert "cleanup_completed=true" in text and "exit_code=0" in text
    assert terminal.closed
    assert len([line for line in terminal.lines if "樣本年齡" in line]) <= 12


@pytest.mark.parametrize("stage", [
    "open", "construct", "initialize", "initialize_interrupt", "read",
    "interrupt", "watchdog", "close", "output", "motion_read",
])
def test_failures_release_all_owned_resources(
    stage: str, setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clock, distance, imu, bus, terminal = setup

    def fail(*_args: object, **_kwargs: object) -> NoReturn:
        if stage in ("interrupt", "initialize_interrupt"):
            raise KeyboardInterrupt()
        if stage == "watchdog":
            signal.raise_signal(signal.SIGALRM)
        raise OSError("injected failure")

    if stage == "open":
        monkeypatch.setattr(distance, "open", fail)
    elif stage == "construct":
        monkeypatch.setattr(cli, "_FACTORIES", replace(cli._FACTORIES, motion=fail))
    elif stage in ("initialize", "initialize_interrupt"):
        monkeypatch.setattr(imu, "initialize_ndof", fail)
    elif stage in ("read", "interrupt", "watchdog"):
        monkeypatch.setattr(distance, "read_measurements", fail)
    elif stage == "motion_read":
        monkeypatch.setattr(imu, "read_measurement", fail)
    elif stage == "close":
        monkeypatch.setattr(distance, "close", fail)
    elif stage == "output":
        original = terminal.flush_once

        def disconnected(now: float) -> None:
            if distance.reads > 5:
                raise BrokenPipeError("SSH disconnected")
            original(now)

        monkeypatch.setattr(terminal, "flush_once", disconnected)
    was_interrupted = stage in ("interrupt", "initialize_interrupt")
    assert cli.main(["--confirm-hardware", "--duration", "1"]) == (130 if was_interrupted else 1)
    assert distance.closes == (0 if stage == "close" else 1)
    assert bus.closes == (0 if stage == "open" else 1)
    assert imu.closes == (0 if stage in ("open", "construct") else 1)
    text = "\n".join(terminal.lines)
    assert f"cleanup_completed={'false' if stage == 'close' else 'true'}" in text
    if not was_interrupted:
        assert "ERROR" in text


def test_primary_and_cleanup_error_are_both_preserved(setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    _clock, distance, imu, bus, terminal = setup

    def read_error() -> NoReturn:
        raise OSError("primary read failed")

    def close_error() -> NoReturn:
        raise OSError("secondary cleanup failed")

    monkeypatch.setattr(distance, "read_measurements", read_error)
    monkeypatch.setattr(distance, "close", close_error)
    assert cli.main(["--confirm-hardware", "--duration", "1"]) == 1
    text = "\n".join(terminal.lines)
    assert "primary read failed" in text and "secondary cleanup failed" in text
    assert imu.closes == bus.closes == 1


def pipeline(refresh: float, distance_value: Callable[[int], int | None]) -> tuple[LivePresentation, MemoryTerminal, list[object], SensorSamplingCoordinator]:
    clock = Clock()
    distance = Distance(clock)
    distance.value = distance_value
    terminal = MemoryTerminal()
    view = LivePresentation(terminal, started=0, refresh_hz=refresh)
    coordinator = SensorSamplingCoordinator(distance, Motion(clock, Bus()), monotonic=clock.now, sleep=clock.sleep)
    results: list[object] = []

    def sample(sequence: int, value: object) -> None:
        view.on_sample(sequence, value)
        results.append(view.assessment)

    coordinator.run(duration_seconds=3.5, maximum_samples=None, on_sample=sample, on_tick=view.on_tick)
    return view, terminal, results, coordinator


def test_refresh_never_changes_canonical_samples_or_fusion_results() -> None:
    value = lambda i: 500 if i < 150 else 500 - (i - 150)
    a = pipeline(1, value)
    b = pipeline(20, value)
    assert a[2] == b[2]
    assert len(a[1].lines) < len(b[1].lines)
    assert any(item.classification.value == "target_approach" for item in a[2])
    assert all(item.own_motion_evidence.unique_sample_count <= 11 for item in a[2])
    assert a[3].statistics.valid_distance_measurements > 300


def test_closing_without_baseline_never_target_and_motion_is_not_overcounted() -> None:
    view, terminal, assessments, coordinator = pipeline(5, lambda i: 500 - i)
    assert all(item.classification.value == "uncertain" for item in assessments)
    assert assessments[39].own_motion_evidence.unique_sample_count == 4
    assert "缺少有效近期靜止基準" in "\n".join(terminal.lines)


def test_idle_ticks_hide_old_classification_and_recovery_rebuilds_baseline() -> None:
    view, terminal, assessments, coordinator = pipeline(5, lambda i: None if 151 <= i <= 200 else 500)
    text = "\n".join(terminal.lines)
    assert "資料斷流／過期" in text
    stale_lines = [line for line in terminal.lines if "樣本年齡" in line and "資料斷流／過期" in line]
    assert stale_lines and all("距離=資料不足" in line and "UNCERTAIN" in line for line in stale_lines)
    assert assessments[149].stationary_baseline_ready
    assert not assessments[150].stationary_baseline_ready
    assert assessments[-1].stationary_baseline_ready
    assert coordinator.statistics.uart_empty_reads == 50


def test_invalid_clears_baseline_trend_is_not_presented_as_current() -> None:
    view, terminal, assessments, coordinator = pipeline(20, lambda i: 0 if 151 <= i <= 170 else 500)
    assert assessments[149].stationary_baseline_ready
    assert assessments[150].distance_trend is assessments[149].distance_trend
    assert not assessments[150].stationary_baseline_ready
    invalid_lines = [line for line in terminal.lines if "距離=無效" in line]
    assert invalid_lines and all("接近率=資料不足" in line and "UNCERTAIN" in line for line in invalid_lines)
    assert coordinator.statistics.invalid_distance_count == 20
    assert assessments[-1].stationary_baseline_ready


def test_stale_and_future_are_uncertain_through_synchronizer_adapter_formatter() -> None:
    terminal = MemoryTerminal()
    view = LivePresentation(terminal, started=0)
    sync = SensorSynchronizer()
    sync.update_motion(TimedMotionMeasurement(motion(), 0))
    for i in range(30):
        t = i / 10
        if i < 16:
            sync.update_motion(TimedMotionMeasurement(motion(t), t))
        sample = sync.associate(TimedDistanceMeasurement(TFMiniPlusMeasurement(500, 500, 25), t))
        view.on_sample(i + 1, sample)
        view.on_tick(t)
    assert view.assessment.classification.value == "uncertain"
    assert "健康=stale_motion_sample" in view.format_status(2.9)
    sync.update_motion(TimedMotionMeasurement(motion(4), 4))
    view.on_sample(31, sync.associate(TimedDistanceMeasurement(TFMiniPlusMeasurement(500, 500, 25), 3)))
    assert "健康=future_motion_sample" in view.format_status(3)


def test_bounded_slow_output_coalesces_and_interrupts_without_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    terminal = BoundedTerminal(write_fd)
    try:
        def blocked(*_args: object) -> NoReturn:
            raise BlockingIOError()

        monkeypatch.setattr(os, "write", blocked)
        for i in range(1000):
            terminal.publish(f"status {i}", i / 1000)
            terminal.publish(f"event {i}", i / 1000, event=True)
            terminal.flush_once(i / 1000)
        assert terminal.dropped_status == 999
        assert terminal.dropped_events >= 983
        with pytest.raises(TerminalBackpressureError):
            terminal.flush_once(2.1)
    finally:
        terminal.close()
        assert os.get_blocking(write_fd)
        os.close(read_fd)
        os.close(write_fd)


def test_output_partial_write_and_broken_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    read_fd, write_fd = os.pipe()
    terminal = BoundedTerminal(write_fd)
    original_write = os.write
    try:
        monkeypatch.setattr(os, "write", lambda fd, data: original_write(fd, data[:2]))
        terminal.publish("測試", 0)
        for i in range(4):
            terminal.flush_once(i / 100)
        assert os.read(read_fd, 100).decode() == "測試\n"
        os.close(read_fd)
        read_fd = -1
        terminal.publish("closed", 1)
        with pytest.raises(BrokenPipeError):
            terminal.flush_once(1)
    finally:
        terminal.close()
        if read_fd >= 0:
            os.close(read_fd)
        os.close(write_fd)


def test_import_and_help_do_not_import_hardware() -> None:
    script = '''
import builtins
original = builtins.__import__
def guard(name, *args, **kwargs):
    if name.split('.')[0] in {'serial', 'smbus2', 'gpiozero', 'lgpio'}:
        raise AssertionError(name)
    return original(name, *args, **kwargs)
builtins.__import__ = guard
from rear_warning.cli.live_detection import main
main(['--help'])
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert "--confirm-hardware" in result.stdout


def test_watchdog_and_signal_handlers_restore() -> None:
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGALRM)}
    guard = cli.LiveSignals()
    try:
        guard.install()
        with pytest.raises(TimeoutError, match="5 秒"):
            signal.raise_signal(signal.SIGALRM)
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGHUP)
    finally:
        guard.restore()
    assert {s: signal.getsignal(s) for s in before} == before


def test_stoppable_coordinator_runs_past_old_iteration_cap_and_checks_empty_reads() -> None:
    clock = Clock()
    distance = Distance(clock)
    distance.value = lambda _i: None
    ticks: list[float] = []
    coordinator = SensorSamplingCoordinator(
        distance, Motion(clock, Bus()), schedule=SamplingSchedule(maximum_iterations=2),
        monotonic=clock.now, sleep=clock.sleep,
    )
    stats, _ = coordinator.run(
        duration_seconds=None, maximum_samples=None,
        should_stop=lambda: distance.reads >= 8, on_tick=ticks.append,
    )
    assert stats.uart_empty_reads == 8
    assert len(ticks) == 9
    assert coordinator.last_valid_motion_timestamp is not None


def test_self_motion_from_real_pipeline_keeps_self_approach_semantics() -> None:
    clock = Clock()
    distance = Distance(clock)
    distance.value = lambda i: 500 - i

    class MovingMotion(Motion):
        def read_measurement(self) -> BNO055Measurement:
            return replace(super().read_measurement(), linear_acceleration=Vector3(0.8, 0, 0))

    terminal = MemoryTerminal()
    view = LivePresentation(terminal, started=0)
    coordinator = SensorSamplingCoordinator(
        distance, MovingMotion(clock, Bus()), monotonic=clock.now, sleep=clock.sleep,
    )
    coordinator.run(duration_seconds=1.5, maximum_samples=None, on_sample=view.on_sample, on_tick=view.on_tick)
    assert view.assessment.classification.value == "static_or_self_approach"
    assert "平移=達條件" in view.format_status(clock.now())


def test_second_start_creates_fresh_baseline_state(setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    _clock, distance, imu, bus, terminal = setup
    assert cli.main(["--confirm-hardware", "--duration", "2"]) == 0
    assert "基準=成立" in "\n".join(terminal.lines)
    terminal.lines.clear()
    monkeypatch.setattr(cli.sys, "stdin", FakeTTY("\n"))
    assert cli.main(["--confirm-hardware", "--duration", "0.3"]) == 0
    assert "基準=成立" not in "\n".join(terminal.lines)
    assert distance.opens == distance.closes == imu.initializations == bus.closes == 2


def test_pending_status_is_replaced_until_first_byte_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    reader, writer = os.pipe()
    output = BoundedTerminal(writer)
    original = os.write
    try:
        def blocked(_fd: int, _data: bytes) -> int:
            raise BlockingIOError()

        monkeypatch.setattr(os, "write", blocked)
        output.publish("outdated", 0)
        output.flush_once(0)
        output.publish("current", 0.4)
        monkeypatch.setattr(os, "write", original)
        output.flush_once(0.4)
        assert os.read(reader, 100) == b"current\n"
        assert output.dropped_status == 1
    finally:
        output.close()
        os.close(reader)
        os.close(writer)


def test_real_pty_enter_and_ctrl_c_before_hardware_import() -> None:
    """Exercise the actual interactive gate without ever pressing Enter."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "rear_warning.cli.live_detection", "--confirm-hardware"],
        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
    )
    os.close(slave)
    captured = b""
    try:
        deadline = time.monotonic() + 5
        while "按 Enter 開始偵測".encode() not in captured:
            assert time.monotonic() < deadline
            if select.select([master], [], [], 0.05)[0]:
                captured += os.read(master, 8192)
        assert "初始化 UART".encode() not in captured
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=3) == 130
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        os.close(master)


def test_device_busy_preflight_stops_before_construction(setup: tuple[Any, ...], monkeypatch: pytest.MonkeyPatch) -> None:
    _clock, distance, imu, bus, terminal = setup

    def busy(_port: str, _bus: int) -> NoReturn:
        raise OSError("裝置忙碌：已有 UART owner")

    monkeypatch.setattr(cli, "_PREFLIGHT", busy)
    assert cli.main(["--confirm-hardware"]) == 1
    assert distance.opens == imu.initializations == bus.closes == 0
    assert "裝置忙碌" in "\n".join(terminal.lines)
