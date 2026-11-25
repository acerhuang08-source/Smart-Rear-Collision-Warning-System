"""Offline analysis and receive-only live monitoring for ioRadar701."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import select
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO, NoReturn, Protocol, TextIO

import serial

from rear_warning.sensors.ioradar701.analysis import RadarAnalysis
from rear_warning.sensors.ioradar701.models import RadarFrame, TargetFrame
from rear_warning.sensors.ioradar701.parser import IoRadar701StreamParser
from rear_warning.sensors.ioradar701.state import RadarSnapshot

_DEFAULT_PORT = "/dev/ttyAMA0"
_DEFAULT_BAUDRATE = 230400
_DEFAULT_OUTPUT_ROOT = Path("captures/ioradar701")
_DEFAULT_STALE_SECONDS = 1.0
_DEFAULT_REFRESH_HZ = 5.0
_READ_SIZE = 4096
_READ_TIMEOUT_SECONDS = 0.05


class _SerialConnection(Protocol):
    @property
    def is_open(self) -> bool: ...
    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class _LiveOptions:
    port: str
    baudrate: int
    stale_after: float
    refresh_hz: float
    duration: float | None
    output_root: Path
    plain: bool
    scenario: str
    initial_events: tuple[str, ...]


@dataclass(slots=True)
class _CaptureFiles:
    directory: Path
    raw_path: Path
    decoded_path: Path
    events_path: Path
    metadata_path: Path
    summary_path: Path
    raw_file: BinaryIO
    decoded_file: TextIO
    events_file: TextIO
    received_bytes: int = 0
    persisted_bytes: int = 0
    decoded_records: int = 0
    event_records: int = 0
    close_succeeded: bool = False

    def write_raw(self, chunk: bytes) -> None:
        self.received_bytes += len(chunk)
        written = self.raw_file.write(chunk)
        self.persisted_bytes += max(0, written or 0)
        if written != len(chunk):
            raise OSError(
                f"raw.bin short write: expected {len(chunk)}, wrote {written}"
            )

    def write_decoded(self, record: dict[str, object]) -> None:
        self.decoded_file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self.decoded_records += 1

    def write_event(self, record: dict[str, object]) -> None:
        self.events_file.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self.events_file.flush()
        self.event_records += 1

    def close(self) -> list[str]:
        errors: list[str] = []
        for label, file in (
            ("raw.bin", self.raw_file),
            ("decoded.jsonl", self.decoded_file),
            ("events.jsonl", self.events_file),
        ):
            try:
                file.flush()
                os.fsync(file.fileno())
            except OSError as exc:
                errors.append(f"{label} flush failed: {exc}")
            try:
                file.close()
            except OSError as exc:
                errors.append(f"{label} close failed: {exc}")
        self.close_succeeded = not errors
        return errors


class _LiveDisplay:
    """A fixed Chinese panel or line-oriented text refreshed independently."""

    def __init__(self, output: TextIO, *, plain: bool) -> None:
        self.output = output
        self.panel = not plain and output.isatty() and os.environ.get("TERM") != "dumb"
        self.active = False

    def show(
        self,
        snapshot: RadarSnapshot,
        parser: IoRadar701StreamParser,
        *,
        elapsed: float,
    ) -> None:
        line = _format_plain(snapshot, parser, elapsed=elapsed)
        if not self.panel:
            self.output.write(line + "\n")
            self.output.flush()
            return
        if not self.active:
            self.output.write("\x1b[?1049h\x1b[?25l")
            self.active = True
        lines = _format_panel(snapshot, parser, elapsed=elapsed)
        self.output.write("\x1b[H" + "\r\n".join("\x1b[2K" + row for row in lines) + "\x1b[J")
        self.output.flush()

    def restore(self) -> None:
        if self.active:
            self.output.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            self.output.flush()
            self.active = False


_MONOTONIC: Callable[[], float] = time.monotonic
_WALL_CLOCK: Callable[[], datetime] = lambda: datetime.now().astimezone()
_INPUT: Callable[[str], str] = input


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _build_parser() -> _ArgumentParser:
    parser = _ArgumentParser(prog="ioradar701", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    offline = subparsers.add_parser("offline", help="parse an existing raw.bin")
    offline.add_argument("raw_file", type=Path)
    offline.add_argument("--json", action="store_true", help="print the complete JSON report")
    offline.add_argument(
        "--decoded-output",
        type=Path,
        help="optionally create, never overwrite, decoded JSON Lines",
    )

    live = subparsers.add_parser("live", help="receive, decode, display, and record UART")
    live.add_argument("--confirm-hardware", action="store_true", required=True)
    live.add_argument("--port", default=_DEFAULT_PORT)
    live.add_argument("--baudrate", type=_positive_int, default=_DEFAULT_BAUDRATE)
    live.add_argument("--stale-after", type=_positive_float, default=_DEFAULT_STALE_SECONDS)
    live.add_argument("--refresh-hz", type=_positive_float, default=_DEFAULT_REFRESH_HZ)
    live.add_argument("--duration", type=_positive_float)
    live.add_argument("--output-root", type=Path, default=_DEFAULT_OUTPUT_ROOT)
    live.add_argument("--plain", action="store_true")
    live.add_argument("--scenario", default="unspecified")
    live.add_argument("--event", action="append", default=[])
    return parser


def _frame_record(frame: RadarFrame) -> dict[str, object]:
    return frame.as_dict()


def _offline(raw_path: Path, *, json_output: bool, decoded_output: Path | None) -> int:
    if not raw_path.is_file():
        print(f"offline_error: raw file does not exist: {raw_path}", file=sys.stderr)
        return 1
    parser = IoRadar701StreamParser()
    analysis = RadarAnalysis()
    digest = hashlib.sha256()
    decoded_file: TextIO | None = None
    try:
        if decoded_output is not None:
            decoded_file = decoded_output.open("x", encoding="utf-8", newline="\n")
        with raw_path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
                for frame in parser.feed(chunk):
                    analysis.observe(frame)
                    if decoded_file is not None:
                        decoded_file.write(
                            json.dumps(_frame_record(frame), ensure_ascii=False, sort_keys=True)
                            + "\n"
                        )
    except OSError as exc:
        print(f"offline_error: {exc}", file=sys.stderr)
        return 1
    finally:
        if decoded_file is not None:
            try:
                decoded_file.close()
            except OSError as exc:
                print(f"offline_error: decoded output close failed: {exc}", file=sys.stderr)
                return 1

    source_metadata = _read_source_metadata(raw_path.with_name("metadata.json"))
    elapsed = _metadata_elapsed(source_metadata)
    report = analysis.as_dict(parser.statistics, elapsed_seconds=elapsed)
    report.update(
        {
            "source_raw": str(raw_path),
            "sha256": digest.hexdigest(),
            "source_metadata": source_metadata,
            "source_metadata_size_matches": (
                None if source_metadata is None or "total_bytes" not in source_metadata
                else source_metadata["total_bytes"] == parser.statistics.input_bytes
            ),
            "decoded_output": None if decoded_output is None else str(decoded_output),
            "timing_note": "offline raw.bin has no per-frame receipt timestamps",
            "scaling_note": (
                "X/Y/Vx use the supplied unsigned raw-offset mapping; signed-field "
                "ambiguity has not been independently resolved"
            ),
        }
    )
    if json_output:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_offline_report(report)
    return 0


def _read_source_metadata(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _metadata_elapsed(metadata: dict[str, object] | None) -> float | None:
    if metadata is None:
        return None
    value = metadata.get("elapsed_seconds")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    elapsed = float(value)
    return elapsed if math.isfinite(elapsed) and elapsed > 0 else None


def _print_offline_report(report: dict[str, object]) -> None:
    print("ioRadar701 offline analysis")
    for key in (
        "source_raw", "sha256", "bytes", "status_frames", "target_frames",
        "checksum_failures", "sync_discarded_bytes", "trailing_bytes",
        "rate_denominator_seconds", "status_frames_per_second",
        "target_frames_per_second",
    ):
        print(f"{key}={report[key]}")
    print("status_values=" + json.dumps(report["status_values"], ensure_ascii=False, sort_keys=True))
    print("target_ids=" + json.dumps(report["target_ids"], ensure_ascii=False, sort_keys=True))
    print("timing_note=" + str(report["timing_note"]))
    print("scaling_note=" + str(report["scaling_note"]))


def _open_serial(options: _LiveOptions) -> _SerialConnection:
    connection = serial.Serial(
        port=None,
        baudrate=options.baudrate,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=_READ_TIMEOUT_SECONDS,
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


_SERIAL_FACTORY: Callable[[_LiveOptions], _SerialConnection] = _open_serial


def _allocate_capture_directory(root: Path, timestamp: datetime) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    base = "capture-" + timestamp.strftime("%Y%m%dT%H%M%S.%f%z")
    for suffix in range(1000):
        candidate = root / (base if suffix == 0 else f"{base}-{suffix:03d}")
        try:
            candidate.mkdir(exist_ok=False)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(f"cannot allocate a unique capture under {root}")


def _open_capture_files(directory: Path) -> _CaptureFiles:
    paths = {
        "raw": directory / "raw.bin",
        "decoded": directory / "decoded.jsonl",
        "events": directory / "events.jsonl",
        "metadata": directory / "metadata.json",
        "summary": directory / "summary.json",
    }
    opened: list[BinaryIO | TextIO] = []
    try:
        raw = paths["raw"].open("xb")
        opened.append(raw)
        decoded = paths["decoded"].open("x", encoding="utf-8", newline="\n")
        opened.append(decoded)
        events = paths["events"].open("x", encoding="utf-8", newline="\n")
        opened.append(events)
    except BaseException:
        for file in opened:
            try:
                file.close()
            except OSError:
                pass
        raise
    return _CaptureFiles(
        directory=directory,
        raw_path=paths["raw"],
        decoded_path=paths["decoded"],
        events_path=paths["events"],
        metadata_path=paths["metadata"],
        summary_path=paths["summary"],
        raw_file=raw,
        decoded_file=decoded,
        events_file=events,
    )


def _git_identity() -> dict[str, object]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
            check=False, timeout=2.0,
        )
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True,
            check=False, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return {"head": None, "dirty": None}
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "dirty": bool(dirty.stdout) if dirty.returncode == 0 else None,
    }


def _event_record(kind: str, detail: str, *, started: float) -> dict[str, object]:
    return {
        "kind": kind,
        "detail": detail,
        "recorded_at": _WALL_CLOCK().isoformat(timespec="microseconds"),
        "elapsed_seconds": max(0.0, _MONOTONIC() - started),
    }


def _poll_operator_line(input_stream: TextIO) -> str | None:
    try:
        descriptor = input_stream.fileno()
    except (AttributeError, OSError):
        return None
    try:
        ready, _, _ = select.select([descriptor], [], [], 0)
    except (OSError, ValueError):
        return None
    if not ready:
        return None
    line = input_stream.readline()
    return None if line == "" else line.strip()


def _live(options: _LiveOptions) -> int:
    _print_live_plan(options)
    try:
        _INPUT(
            "確認只接雷達 TX→Pi RX、3.3V 相容、共地且 ttyAMA0 無其他讀取者後，"
            "按 Enter 開始；EOF/Ctrl+C 取消："
        )
    except EOFError:
        print("已取消：未建立檔案、未開啟 UART。", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消：未建立檔案、未開啟 UART。", file=sys.stderr)
        return 130

    started_wall = _WALL_CLOCK()
    try:
        directory = _allocate_capture_directory(options.output_root, started_wall)
        files = _open_capture_files(directory)
    except OSError as exc:
        print(f"live_error: unable to create capture files; UART not opened: {exc}", file=sys.stderr)
        return 1

    parser = IoRadar701StreamParser()
    analysis = RadarAnalysis()
    hasher = hashlib.sha256()
    connection: _SerialConnection | None = None
    serial_closed: bool | None = None
    file_errors: list[str] = []
    detail: str | None = None
    exit_reason = "completed"
    started = _MONOTONIC()
    display = _LiveDisplay(sys.stdout, plain=options.plain)
    next_refresh = started
    deadline = None if options.duration is None else started + options.duration

    try:
        try:
            connection = _SERIAL_FACTORY(options)
        except (OSError, ValueError, serial.SerialException) as exc:
            exit_reason = "open_error"
            detail = f"failed to open UART {options.port}: {exc}"
        else:
            files.write_event(_event_record("capture_started", options.scenario, started=started))
            for note in options.initial_events:
                files.write_event(_event_record("operator", note, started=started))
            while exit_reason == "completed":
                now = _MONOTONIC()
                if deadline is not None and now >= deadline:
                    break
                try:
                    chunk = connection.read(_READ_SIZE)
                except KeyboardInterrupt:
                    exit_reason = "interrupted"
                    break
                except (OSError, serial.SerialException) as exc:
                    exit_reason = "read_error"
                    detail = f"UART read failed: {exc}"
                    analysis.tracker.set_serial_error(detail)
                    break
                now = _MONOTONIC()
                if chunk:
                    try:
                        files.write_raw(chunk)
                        hasher.update(chunk)
                        analysis.tracker.note_bytes(now)
                        for frame in parser.feed(chunk):
                            analysis.observe(frame, now)
                            record = _frame_record(frame)
                            record.update(
                                {
                                    "received_at": _WALL_CLOCK().isoformat(timespec="microseconds"),
                                    "elapsed_seconds": max(0.0, now - started),
                                }
                            )
                            files.write_decoded(record)
                    except OSError as exc:
                        exit_reason = "storage_error"
                        detail = f"capture write failed: {exc}"
                        break

                operator_line = _poll_operator_line(sys.stdin)
                if operator_line == ":stop":
                    files.write_event(_event_record("operator_stop", operator_line, started=started))
                    break
                if operator_line:
                    note = operator_line.removeprefix(":event").strip()
                    if note:
                        try:
                            files.write_event(_event_record("operator", note, started=started))
                        except OSError as exc:
                            exit_reason = "storage_error"
                            detail = f"event write failed: {exc}"
                            break

                if now >= next_refresh:
                    try:
                        display.show(
                            analysis.tracker.snapshot(now, stale_after=options.stale_after),
                            parser,
                            elapsed=max(0.0, now - started),
                        )
                    except OSError as exc:
                        exit_reason = "output_error"
                        detail = f"terminal output failed: {exc}"
                        break
                    next_refresh = now + 1.0 / options.refresh_hz
    except KeyboardInterrupt:
        exit_reason = "interrupted"
    except OSError as exc:
        exit_reason = "storage_error"
        detail = f"capture lifecycle failed: {exc}"
    finally:
        elapsed = max(0.0, _MONOTONIC() - started)
        try:
            files.write_event(_event_record("capture_ending", exit_reason, started=started))
        except OSError as exc:
            file_errors.append(f"events final write failed: {exc}")
        if connection is not None:
            try:
                connection.close()
            except (OSError, serial.SerialException) as exc:
                serial_closed = False
                file_errors.append(f"UART close failed: {exc}")
                if exit_reason == "completed":
                    exit_reason = "close_error"
            else:
                serial_closed = True
        file_errors.extend(files.close())
        try:
            display.restore()
        except OSError as exc:
            file_errors.append(f"terminal restore failed: {exc}")

    if files.received_bytes == 0 and exit_reason == "completed":
        exit_reason = "no_data"
        detail = "UART returned no bytes"
    capture_file_error = next(
        (
            item for item in file_errors
            if item.startswith(("raw.bin", "decoded.jsonl", "events.jsonl", "events final"))
        ),
        None,
    )
    if capture_file_error is not None and exit_reason in ("completed", "interrupted", "no_data"):
        exit_reason = "storage_error"
        detail = detail or capture_file_error
    ended_wall = _WALL_CLOCK()
    report = analysis.as_dict(parser.statistics, elapsed_seconds=elapsed)
    report.update(
        {
            "format_version": 1,
            "exit_reason": exit_reason,
            "detail": detail,
            "raw_sha256": hasher.hexdigest(),
            "persisted_bytes": files.persisted_bytes,
            "received_bytes": files.received_bytes,
            "decoded_records": files.decoded_records,
            "event_records": files.event_records,
            "serial_close_succeeded": serial_closed,
            "capture_files_close_succeeded": files.close_succeeded,
            "full_capture_saved": (
                files.close_succeeded
                and files.received_bytes == files.persisted_bytes
            ),
            "storage_errors": file_errors,
        }
    )
    metadata: dict[str, object] = {
        "format_version": 1,
        "device_family": "ioRadar701",
        "port": options.port,
        "requested_baudrate": options.baudrate,
        "requested_serial_format": "8N1",
        "flow_control": {"xonxoff": False, "rtscts": False, "dsrdtr": False},
        "receive_only": True,
        "stale_after_seconds": options.stale_after,
        "refresh_hz": options.refresh_hz,
        "requested_duration_seconds": options.duration,
        "scenario": options.scenario,
        "started_at": started_wall.isoformat(timespec="microseconds"),
        "ended_at": ended_wall.isoformat(timespec="microseconds"),
        "elapsed_seconds": elapsed,
        "exit_reason": exit_reason,
        "detail": detail,
        "git": _git_identity(),
        "working_directory": str(Path.cwd()),
        "process_argv": list(sys.argv),
        "files": {
            "raw": files.raw_path.name,
            "decoded": files.decoded_path.name,
            "events": files.events_path.name,
            "summary": files.summary_path.name,
        },
        "receipt_timestamp_note": "host receipt time; not a radar-internal measurement time",
        "vx_note": "reported X-axis relative velocity; not absolute vehicle speed",
    }
    summary_saved, summary_error = _save_json_once(files.summary_path, report)
    metadata["summary_saved"] = summary_saved
    if summary_error is not None:
        file_errors.append(summary_error)
    metadata_saved, metadata_error = _save_json_once(files.metadata_path, metadata)
    if metadata_error is not None:
        file_errors.append(metadata_error)

    _print_live_summary(files, report, metadata_saved=metadata_saved, summary_saved=summary_saved)
    for error in file_errors:
        print(f"storage_warning: {error}", file=sys.stderr)
    if detail is not None:
        print(f"live_error: {detail}", file=sys.stderr)
    return 0 if exit_reason == "completed" and metadata_saved and summary_saved else (
        130 if exit_reason == "interrupted" else 1
    )


def _save_json_once(path: Path, value: dict[str, object]) -> tuple[bool, str | None]:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        return False, f"{path.name} save failed: {exc}"
    return True, None


def _print_live_plan(options: _LiveOptions) -> None:
    print("ioRadar701 即時目標監看（單一 UART、只接收、不送命令）")
    print(f"port={options.port} baudrate={options.baudrate} format=8N1")
    print(f"stale_after_seconds={options.stale_after:g} refresh_hz={options.refresh_hz:g}")
    duration = "until_Ctrl+C" if options.duration is None else f"{options.duration:g}"
    print(f"duration_seconds={duration}")
    print(f"output_root={options.output_root} scenario={options.scenario}")
    print("執行中可輸入 `:event 說明` 後按 Enter 記錄階段，或 `:stop` 正常停止。")


def _age(value: float | None) -> str:
    return "尚無" if value is None else f"{value:.3f}s"


def _state_label(snapshot: RadarSnapshot) -> str:
    return {
        "waiting_bytes": "等待 bytes",
        "bytes_stale": "串流過期",
        "waiting_status": "等待有效狀態",
        "status_stale": "狀態過期",
        "no_targets": "當輪無目標",
        "waiting_targets": "等待當輪目標",
        "targets_stale": "目標過期",
        "live": "有效",
        "serial_error": "串口錯誤",
    }[snapshot.state]


def _format_plain(
    snapshot: RadarSnapshot,
    parser: IoRadar701StreamParser,
    *,
    elapsed: float,
) -> str:
    status = snapshot.status
    status_text = (
        "尚無" if status is None else
        f"check={status.check},active={status.active},error={status.error}"
    )
    targets = ";".join(
        f"ID={target.target_id} X={target.x_m:.2f}m Y={target.y_m:.2f}m "
        f"D={target.distance_m:.2f}m Vx={target.vx_mps:.2f}m/s E={target.energy}"
        for target in snapshot.targets
    ) or "無即時目標值"
    stats = parser.statistics
    expected = "?" if snapshot.expected_targets is None else str(snapshot.expected_targets)
    return (
        f"[ioRadar701 t={elapsed:.2f}s] 狀態={_state_label(snapshot)} "
        f"雷達狀態=({status_text}) 當輪目標={len(snapshot.targets)}/{expected} "
        f"年齡(bytes/status/target)={_age(snapshot.bytes_age_seconds)}/"
        f"{_age(snapshot.status_age_seconds)}/{_age(snapshot.target_age_seconds)} "
        f"幀(status/target)={stats.status_frames}/{stats.target_frames} "
        f"checksum失敗={stats.checksum_failures} sync丟棄={stats.sync_discarded_bytes} "
        f"目標=[{targets}]"
    )


def _format_panel(
    snapshot: RadarSnapshot,
    parser: IoRadar701StreamParser,
    *,
    elapsed: float,
) -> list[str]:
    stats = parser.statistics
    status = snapshot.status
    status_text = "尚無"
    if status is not None:
        status_text = (
            f"check={status.check} active={status.active} error={status.error} "
            f"roll={status.roll_count}"
        )
    expected = "?" if snapshot.expected_targets is None else str(snapshot.expected_targets)
    lines = [
        "ioRadar701 即時目標監看（LIVE 實際 UART）",
        f"運行 {elapsed:.2f} 秒    Ctrl+C 或 :stop 結束    :event 說明 記錄階段",
        "-" * 72,
        f"資料狀態    {_state_label(snapshot)}",
        f"雷達狀態    {status_text}",
        f"當輪目標    {len(snapshot.targets)}/{expected} "
        f"({'完整' if snapshot.cycle_complete else '未完整'})",
        f"資料年齡    bytes {_age(snapshot.bytes_age_seconds)} / "
        f"status {_age(snapshot.status_age_seconds)} / target {_age(snapshot.target_age_seconds)}",
        f"品質計數    status {stats.status_frames} / target {stats.target_frames} / "
        f"checksum失敗 {stats.checksum_failures} / sync丟棄 {stats.sync_discarded_bytes}",
        "-" * 72,
    ]
    if not snapshot.targets:
        lines.append("目前沒有可當作即時值顯示的完整目標資料。")
    else:
        lines.append("ID    X(m)    Y(m)    幾何距離(m)    Vx(m/s)    能量")
        for target in snapshot.targets:
            lines.append(
                f"{target.target_id:<4}  {target.x_m:>6.2f}  {target.y_m:>6.2f}  "
                f"{target.distance_m:>11.2f}  {target.vx_mps:>8.2f}  {target.energy:>4}"
            )
    if snapshot.error_detail:
        lines.append("錯誤：" + snapshot.error_detail)
    return lines


def _print_live_summary(
    files: _CaptureFiles,
    report: dict[str, object],
    *,
    metadata_saved: bool,
    summary_saved: bool,
) -> None:
    print("ioRadar701_live_summary")
    for key in (
        "exit_reason", "bytes", "status_frames", "target_frames",
        "checksum_failures", "sync_discarded_bytes", "trailing_bytes",
        "max_valid_receipt_gap_seconds",
    ):
        print(f"{key}={report[key]}")
    print(f"capture_directory={files.directory}")
    print(f"raw_path={files.raw_path}")
    print(f"decoded_path={files.decoded_path}")
    print(f"events_path={files.events_path}")
    print(f"metadata_path={files.metadata_path if metadata_saved else 'NOT_SAVED'}")
    print(f"summary_path={files.summary_path if summary_saved else 'NOT_SAVED'}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(None if argv is None else list(argv))
    except (ValueError, argparse.ArgumentTypeError) as exc:
        parser.print_usage(sys.stderr)
        print(f"{parser.prog}: error: {exc}", file=sys.stderr)
        return 2
    if args.command == "offline":
        return _offline(
            Path(args.raw_file),
            json_output=bool(args.json),
            decoded_output=None if args.decoded_output is None else Path(args.decoded_output),
        )
    options = _LiveOptions(
        port=str(args.port),
        baudrate=int(args.baudrate),
        stale_after=float(args.stale_after),
        refresh_hz=float(args.refresh_hz),
        duration=None if args.duration is None else float(args.duration),
        output_root=Path(args.output_root),
        plain=bool(args.plain),
        scenario=str(args.scenario),
        initial_events=tuple(str(item) for item in args.event),
    )
    return _live(options)


if __name__ == "__main__":
    raise SystemExit(main())
