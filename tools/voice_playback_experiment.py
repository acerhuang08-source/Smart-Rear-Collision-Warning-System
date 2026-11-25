#!/usr/bin/env python3
"""Operator-gated, explicit-target MOTO A2 Plus playback experiment.

This tool is deliberately separate from the sensor and warning pipelines.  It
keeps the persistent voice setting disabled, checks the configured Bluetooth
identity and unique PipeWire Audio/Sink through the existing backend, and owns
at most one ``pw-play`` child process at a time.
"""

from __future__ import annotations

import argparse
import json
import math
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from rear_warning.voice import (
    PipeWireBluetoothBackend,
    VoiceReminderKind,
    VoiceSettings,
    load_voice_settings,
)


_DEFAULT_CONFIG = Path("config/voice-alert.local.toml")
_DEFAULT_VOLUME = 0.25
_PLAYBACK_TIMEOUT_SECONDS = 15.0
_STOP_TIMEOUT_SECONDS = 1.0
_FAIL_CLOSED_PROPERTIES = json.dumps(
    {
        "node.dont-fallback": True,
        "node.dont-reconnect": True,
        "node.dont-move": True,
    },
    separators=(",", ":"),
)
_PROMPT_ORDER = (
    VoiceReminderKind.DISTANCE_ATTENTION,
    VoiceReminderKind.DISTANCE_TOO_CLOSE,
    VoiceReminderKind.SENSOR_FAULT,
)


class ExperimentError(RuntimeError):
    """A controlled setup, route, playback, or operator-input failure."""


@dataclass(frozen=True, slots=True)
class OperatorResult:
    """One operator's observations after a single playback."""

    heard_from_moto: bool
    content_clear_and_complete: bool
    volume: str
    stuttered: bool

    @property
    def passed(self) -> bool:
        return (
            self.heard_from_moto
            and self.content_clear_and_complete
            and self.volume == "appropriate"
            and not self.stuttered
        )


def _volume(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("volume must be a number") from exc
    if not math.isfinite(parsed) or not 0.0 < parsed <= 1.0:
        raise argparse.ArgumentTypeError("volume must be finite and in (0, 1]")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "逐句播放 MOTO A2 Plus 中文提示；每句須由操作員按 Enter 並回填聽感"
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=_DEFAULT_CONFIG,
        help="私有語音設定檔（預設：config/voice-alert.local.toml）",
    )
    parser.add_argument(
        "--volume",
        type=_volume,
        default=_DEFAULT_VOLUME,
        help="單次 pw-play 串流音量，範圍 (0, 1]（預設：0.25）",
    )
    return parser


def _backend(settings: VoiceSettings) -> PipeWireBluetoothBackend:
    backend = settings.backend
    return PipeWireBluetoothBackend(
        device_address=backend.device_address,
        expected_name=backend.expected_name,
        target_node=backend.target_node,
        bluetoothctl_command=backend.bluetoothctl_command,
        pw_dump_command=backend.pw_dump_command,
        pw_play_command=backend.pw_play_command,
        status_timeout_seconds=backend.status_timeout_seconds,
    )


def _command(
    settings: VoiceSettings,
    *,
    audio_path: Path,
    volume: float,
) -> list[str]:
    return [
        settings.backend.pw_play_command,
        f"--target={settings.backend.target_node}",
        f"--properties={_FAIL_CLOSED_PROPERTIES}",
        "--media-category=Playback",
        "--media-role=Notification",
        f"--volume={volume:.2f}",
        str(audio_path),
    ]


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    except OSError:
        if process.poll() is None:
            process.kill()
            process.wait()


def _play(command: list[str]) -> None:
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = process.communicate(timeout=_PLAYBACK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        if process is not None:
            _stop_process(process)
        raise ExperimentError("pw-play exceeded the 15 second timeout") from exc
    except OSError as exc:
        raise ExperimentError(f"pw-play could not start: {exc}") from exc
    except KeyboardInterrupt:
        if process is not None:
            _stop_process(process)
        raise
    finally:
        if process is not None and process.poll() is None:
            _stop_process(process)
    if process.returncode != 0:
        detail = stderr.strip() or stdout.strip() or str(process.returncode)
        raise ExperimentError(f"pw-play failed: {detail}")


def _wait_for_enter() -> None:
    try:
        response = input("確認耳機已戴妥；按 Enter 播放一次，Ctrl+C 立即停止：")
    except EOFError as exc:
        raise ExperimentError("standard input closed before operator confirmation") from exc
    if response:
        raise ExperimentError("playback cancelled because input was not an empty Enter")


def _ask_yes_no(question: str) -> bool:
    while True:
        try:
            response = input(f"{question} [y/n]：").strip().casefold()
        except EOFError as exc:
            raise ExperimentError("standard input closed during operator feedback") from exc
        if response in {"y", "yes", "是"}:
            return True
        if response in {"n", "no", "否"}:
            return False
        print("請輸入 y 或 n。")


def _ask_volume() -> str:
    choices = {
        "s": "too_small",
        "small": "too_small",
        "太小": "too_small",
        "a": "appropriate",
        "appropriate": "appropriate",
        "適中": "appropriate",
        "l": "too_large",
        "large": "too_large",
        "太大": "too_large",
    }
    while True:
        try:
            response = input("音量 [s=太小/a=適中/l=太大]：").strip().casefold()
        except EOFError as exc:
            raise ExperimentError("standard input closed during volume feedback") from exc
        result = choices.get(response)
        if result is not None:
            return result
        print("請輸入 s、a 或 l。")


def _feedback() -> OperatorResult:
    return OperatorResult(
        heard_from_moto=_ask_yes_no("是否確實從 MOTO A2 Plus 聽到"),
        content_clear_and_complete=_ask_yes_no("內容是否正確清楚、無漏字或截斷"),
        volume=_ask_volume(),
        stuttered=_ask_yes_no("是否卡頓"),
    )


def _run(config: Path, volume: float) -> int:
    settings = load_voice_settings(config)
    if settings.enabled:
        raise ExperimentError(
            "persistent voice setting must remain enabled=false for this experiment"
        )

    backend = _backend(settings)
    route = backend.status()
    if not route.ready:
        raise ExperimentError(f"configured MOTO route is not ready: {route.detail}")

    print("\n預檢通過：")
    print(f"- 指定耳機：{route.expected_name}")
    print("- BlueZ 已配對且已連線：yes")
    print("- 設定指定的唯一 PipeWire Audio/Sink：available")
    print("- 持久設定：enabled=false（不修改）")
    print(f"- 單次播放串流音量：{volume:.2f}")
    print("- 不修改預設輸出、全域音量、配對或服務")

    results: list[tuple[str, OperatorResult]] = []
    for index, kind in enumerate(_PROMPT_ORDER, start=1):
        prompt = settings.prompts.prompt_for(kind)
        if not prompt.audio_path.is_file():
            raise ExperimentError(f"audio file is missing: {prompt.audio_path}")
        command = _command(settings, audio_path=prompt.audio_path, volume=volume)
        print(f"\n[{index}/3] {prompt.audio_path.name}")
        print(f"預期文字：{prompt.text}")
        print(f"實際命令：{shlex.join(command)}")
        _wait_for_enter()

        current_route = backend.status()
        if not current_route.ready:
            raise ExperimentError(
                f"configured MOTO route became unavailable: {current_route.detail}"
            )
        _play(command)
        print("pw-play 已退出；這不代表操作員確實聽到。請記錄聽感：")
        result = _feedback()
        results.append((prompt.audio_path.name, result))
        print(
            "記錄："
            f"heard={result.heard_from_moto}, "
            f"content_ok={result.content_clear_and_complete}, "
            f"volume={result.volume}, stuttered={result.stuttered}"
        )
        if not result.passed:
            print("聽感未全部通過；依安全邊界停止，不重播、不調音量、不切換裝置。")
            return 1

    print("\n三句均已播放一次並由操作員回填：")
    for filename, result in results:
        print(
            f"- {filename}: heard={result.heard_from_moto}, "
            f"content_ok={result.content_clear_and_complete}, "
            f"volume={result.volume}, stuttered={result.stuttered}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    try:
        return _run(options.config, options.volume)
    except KeyboardInterrupt:
        print("\n已由操作員中止；播放子程序已清理。", file=sys.stderr)
        return 130
    except (ExperimentError, OSError, ValueError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
