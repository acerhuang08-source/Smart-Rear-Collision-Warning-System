"""Hardware-free panel checks, including bytes sent through a real pseudo-TTY."""

from __future__ import annotations

import os
import pty
import termios
from collections.abc import Iterator
from typing import Any

import pytest

from rear_warning.cli import live_detection as cli
from rear_warning.live.terminal import (
    BoundedTerminal, LivePresentation, PanelEvent, PanelSnapshot,
    TerminalBackpressureError, cell_width, format_summary, plain_text, render_panel,
    wrap_cells,
)
from rear_warning.synchronization import SensorSamplingCoordinator
from test_live_detection import Bus, Clock, Distance, MemoryTerminal, Motion, pipeline, setup


@pytest.fixture
def tty(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[int, int]]:
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 80))
    os.set_blocking(master, False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("NO_COLOR", raising=False)
    try:
        yield master, slave
    finally:
        os.close(master)
        os.close(slave)


def read_available(fd: int) -> str:
    data = b""
    while True:
        try:
            data += os.read(fd, 65536)
        except BlockingIOError:
            return data.decode("utf-8")


def drain(terminal: BoundedTerminal, now: float) -> None:
    for _ in range(100):
        if not terminal.pending:
            return
        terminal.flush_once(now)
    pytest.fail("bounded test frame did not drain")


def captured_frame(data: str) -> list[str]:
    """Decode the last HOME/erase-row frame, not an illustrative mockup."""
    frame = data.rsplit("\x1b[H", 1)[1].split("\x1b[J", 1)[0]
    return [plain_text(row) for row in frame.replace("\r", "").split("\n")]


def snapshots() -> list[PanelSnapshot]:
    clock = Clock()
    distance = Distance(clock)
    distance.value = lambda i: 500 if i < 150 else 500 - (i - 150)
    view = LivePresentation(MemoryTerminal(), started=0)
    values = [view.snapshot(0)]
    coordinator = SensorSamplingCoordinator(
        distance, Motion(clock, Bus()), monotonic=clock.now, sleep=clock.sleep,
    )

    def sample(sequence: int, value: Any) -> None:
        view.on_sample(sequence, value)
        values.append(view.snapshot(clock.now()))

    coordinator.run(duration_seconds=3.5, maximum_samples=None, on_sample=sample, on_tick=view.on_tick)
    values.append(view.snapshot(clock.now() + 1))
    return values


def test_pending_is_not_a_confirmed_result_and_stale_hides_values() -> None:
    values = snapshots()
    pending = next(s for s in values if s.candidate == "符合目標接近判定條件")
    assert pending.state == "待確認"
    assert pending.classification == "距離穩定或遠離"
    assert pending.reason == "候選持續時間未滿"
    panel = "\n".join(render_panel(pending, (), width=80, height=24) or [])
    assert "最近確認分類  距離穩定或遠離" in panel
    assert "待確認候選    符合目標接近判定條件" in panel
    assert "有效輸出" not in panel and "安全" not in panel
    assert any(s.state == "有效輸出" for s in values)
    assert any("暖機" in s.state for s in values)
    stale = values[-1]
    assert stale.fault and "過期" in stale.reason
    assert stale.distance == stale.rate == "資料不足"
    assert "過期" in stale.classification


def test_uncertain_without_baseline_is_visible_in_panel() -> None:
    view, _terminal, _assessments, _coordinator = pipeline(5, lambda i: 500 - i)
    assert view.sample is not None
    snapshot = view.snapshot(view.sample.distance_timestamp)
    assert snapshot.state == "不確定" and not snapshot.fault
    assert snapshot.reason == "缺少有效近期靜止基準"
    assert snapshot.classification == "不可用／不確定"
    assert snapshot.baseline == "未成立"
    lines = render_panel(snapshot, (), width=48, height=24)
    assert lines is not None and snapshot.reason in "\n".join(lines)


@pytest.mark.parametrize("width,height", [(80, 24), (120, 30), (48, 30)])
def test_real_pty_layouts_and_chinese_widths(tty: tuple[int, int], width: int, height: int) -> None:
    master, slave = tty
    termios.tcsetwinsize(slave, (height, width))
    terminal = BoundedTerminal(slave, label="SIM 版面預覽")
    value = next(s for s in snapshots() if s.candidate == "符合目標接近判定條件")
    try:
        for event in (PanelEvent(0.858, "分類更新：距離穩定或遠離"),
                      PanelEvent(1.6, "待確認：候選持續時間未滿")):
            terminal.record_event(event, "SIM event", 2)
        terminal.display(value, "SIM plain", 2)
        drain(terminal, 2)
        raw = read_available(master)
        lines = captured_frame(raw)
        assert len(lines) <= height - 1
        assert all(cell_width(line) < width for line in lines)
        assert "SIM 版面預覽" in "\n".join(lines)
        assert "LIVE" not in "\n".join(lines)
        assert "候選持續時間未滿" in "\n".join(lines)
        assert "01.600" in "\n".join(lines)
        if width >= 80:
            assert raw.index("00:00.858") < raw.index("00:01.600")
        print(f"\nPTY {width}x{height} (SIM):\n" + "\n".join(lines))
    finally:
        terminal.finish("SIM 結束 cleanup_completed=true exit_code=0")
        terminal.close()
    ending = read_available(master)
    assert "\x1b[?25h" in ending and "\x1b[?1049l" in ending
    assert ending.index("\x1b[?1049l") < ending.index("cleanup_completed")
    assert os.get_blocking(slave)


def test_long_reason_wraps_without_truncation_and_small_window_falls_back(tty: tuple[int, int]) -> None:
    master, slave = tty
    reason = "讀取失敗：" + "裝置回傳的資料不完整，請保留這項原因。" * 2
    value = PanelSnapshot(2, state="失效／不確定", reason=reason, fault=True)
    lines = render_panel(value, (), width=80, height=24, color=True)
    assert lines is not None
    assert reason in "".join(plain_text(line).strip() for line in lines)
    assert all(cell_width(line) < 80 for line in lines)
    terminal = BoundedTerminal(slave)
    try:
        terminal.display(value, reason, 2)
        drain(terminal, 2)
        raw = read_available(master)
        assert "\x1b[1;31m" in raw
        termios.tcsetwinsize(slave, (12, 25))
        terminal.display(value, reason, 2.1)
        drain(terminal, 2.1)
        raw = read_available(master)
        assert "\x1b[?1049l" in raw
        assert "改用純文字" in raw and reason in raw
        assert not terminal.panel_enabled
    finally:
        terminal.close()


def test_resize_and_shorter_content_erase_old_rows(tty: tuple[int, int]) -> None:
    master, slave = tty
    terminal = BoundedTerminal(slave)
    try:
        terminal.display(PanelSnapshot(0, reason="很長的原因" * 12), "long", 0)
        drain(terminal, 0)
        old = captured_frame(read_available(master))
        termios.tcsetwinsize(slave, (30, 100))
        terminal.display(PanelSnapshot(0.2, reason="暖機"), "short", 0.2)
        drain(terminal, 0.2)
        raw = read_available(master)
        new = captured_frame(raw)
        assert len(new) < len(old)
        assert raw.count("\x1b[2K") == len(new)
        assert "\x1b[J" in raw and "\x1b[2J" not in raw
        assert "很長的原因" not in raw
    finally:
        terminal.close()


@pytest.mark.parametrize("mode", ["pipe", "dumb", "plain"])
def test_plain_outputs_have_no_ansi(tty: tuple[int, int], monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    reader, writer = tty
    pipe = mode == "pipe"
    if pipe:
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
    if mode == "dumb":
        monkeypatch.setenv("TERM", "dumb")
    terminal = BoundedTerminal(writer, plain=mode == "plain")
    try:
        terminal.message("初始化", 0, started=0)
        terminal.record_event(PanelEvent(0, "暖機"), "plain event", 0)
        terminal.display(PanelSnapshot(0), "plain status", 0)
        drain(terminal, 0)
        terminal.finish("plain summary")
        terminal.close()
        text = read_available(reader)
        assert "\x1b" not in text
        assert all(s in text for s in ("初始化", "plain event", "plain status", "plain summary"))
    finally:
        if pipe:
            os.close(reader)
            os.close(writer)


def test_combining_fullwidth_and_ansi_widths() -> None:
    assert cell_width("\x1b[31m距離ｅe\u0301\x1b[0m") == 7
    lines = wrap_cells("距離ｅe\u0301很長", 8)
    assert "".join(lines) == "距離ｅe\u0301很長"
    assert all(cell_width(line) <= 8 for line in lines)
    assert "\x1b" not in plain_text("錯誤\x1b[2J訊息\r\x00")


def test_event_burst_keeps_chronological_latest_three(tty: tuple[int, int]) -> None:
    master, slave = tty
    terminal = BoundedTerminal(slave)
    try:
        for i in range(1000):
            terminal.record_event(PanelEvent(i / 1000, f"事件 {i}"), "plain", 1)
        terminal.display(PanelSnapshot(1), "plain", 1)
        drain(terminal, 1)
        raw = read_available(master)
        assert terminal.dropped_events == 997
        assert raw.index("事件 997") < raw.index("事件 998") < raw.index("事件 999")
    finally:
        terminal.close()


def test_slow_panel_stays_bounded_and_restores_on_error(tty: tuple[int, int], monkeypatch: pytest.MonkeyPatch) -> None:
    master, slave = tty
    terminal = BoundedTerminal(slave)
    original_write = os.write

    def blocked(fd: int, data: bytes) -> int:
        raise BlockingIOError()

    monkeypatch.setattr(os, "write", blocked)
    for i in range(1000):
        terminal.display(PanelSnapshot(i / 1000), "plain", i / 1000)
        terminal.flush_once(i / 1000)
    assert terminal.dropped_status == 999
    with pytest.raises(TerminalBackpressureError):
        terminal.flush_once(2.1)
    monkeypatch.setattr(os, "write", original_write)
    terminal.close()
    assert "\x1b[?25h" in read_available(master)
    assert os.get_blocking(slave)


def test_broken_pipe_still_restores_fd_flags(tty: tuple[int, int], monkeypatch: pytest.MonkeyPatch) -> None:
    _master, slave = tty
    terminal = BoundedTerminal(slave)
    terminal.display(PanelSnapshot(0), "plain", 0)

    def broken(fd: int, data: bytes) -> int:
        raise BrokenPipeError("disconnected")

    monkeypatch.setattr(os, "write", broken)
    with pytest.raises(BrokenPipeError):
        terminal.flush_once(0)
    with pytest.raises(BrokenPipeError):
        terminal.close()
    assert os.get_blocking(slave)


def test_summary_restore_follows_partial_frame_without_corrupting_ansi(
    tty: tuple[int, int], monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, slave = tty
    terminal = BoundedTerminal(slave)
    terminal.display(PanelSnapshot(0), "plain", 0)
    terminal.flush_once(0)  # enter alternate screen
    write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: write(fd, data[:2]))
    terminal.flush_once(0)  # first two bytes of the frame
    terminal.finish("SIM 摘要 cleanup_completed=true exit_code=130")
    terminal.close()
    text = read_available(master)
    assert "\x1b[H\x1b[2K" in text
    assert text.index("\x1b[?1049l") > text.index("\x1b[J")
    assert "SIM 摘要 cleanup_completed=true exit_code=130" in text
    assert os.get_blocking(slave)


@pytest.mark.parametrize("failure", ["none", "interrupt", "initialize", "display"])
def test_cli_fake_lifecycle_restores_panel_and_preserves_summary(
    setup: tuple[Any, ...], tty: tuple[int, int], monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    clock, distance, imu, bus, _memory = setup
    master, slave = tty
    terminal = BoundedTerminal(slave, monotonic=clock.now, label="SIM 版面預覽")
    captured: list[str] = []
    flush = terminal.flush_once

    def recording(now: float) -> None:
        flush(now)
        captured.append(read_available(master))
        if failure == "display" and distance.reads > 5:
            raise ValueError("injected display error")

    monkeypatch.setattr(terminal, "flush_once", recording)
    monkeypatch.setattr(cli, "_TERMINAL_FACTORY", lambda *args, **kwargs: terminal)
    if failure in ("interrupt", "initialize"):
        def fail() -> None:
            if failure == "interrupt":
                raise KeyboardInterrupt()
            raise OSError("injected initialization error")
        monkeypatch.setattr(imu, "initialize_ndof", fail)
    code = cli.main(["--confirm-hardware", "--duration", "1"])
    captured.append(read_available(master))
    text = "".join(captured)
    assert distance.closes == imu.closes == bus.closes == 1
    assert code == (0 if failure == "none" else 130 if failure == "interrupt" else 1)
    assert "\x1b[?25h" in text and "\x1b[?1049l" in text
    assert "cleanup_completed=true" in text and f"exit_code={code}" in text
    assert "停止原因" in text and "sampling_elapsed=" in text
    assert os.get_blocking(slave)


def test_summary_keeps_every_stat_and_error_in_short_lines() -> None:
    text = format_summary("[LIVE ERROR UNCERTAIN] " + "較長錯誤原因" * 20 +
                          "\n[LIVE 結束] total_elapsed=2s matched_samples=30 errors=1 "
                          "cleanup_completed=false exit_code=1")
    assert all(cell_width(line) <= 76 for line in text.splitlines())
    assert "較長錯誤原因" * 20 in text.replace("\n", "")
    for field in ("total_elapsed=2s", "matched_samples=30", "errors=1", "cleanup_completed=false", "exit_code=1"):
        assert field in text


def test_plain_flag_keeps_confirmation_gate(setup: tuple[Any, ...]) -> None:
    _clock, distance, imu, _bus, _terminal = setup
    assert cli.main(["--plain"]) == 2
    assert distance.opens == imu.initializations == 0
    assert cli.parser().parse_args(["--plain"]).plain
