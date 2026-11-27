"""Bounded MOTO A2 Plus reconnect manager for a lingering user session."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from .config import VoiceSettings, load_voice_settings


@dataclass(frozen=True, slots=True)
class MOTOAudioStatus:
    paired: bool
    trusted: bool
    connected: bool
    name_matches: bool
    audio_sink_count: int
    reason: str

    @property
    def ready(self) -> bool:
        return (
            self.paired
            and self.trusted
            and self.connected
            and self.name_matches
            and self.audio_sink_count == 1
        )


Runner = Callable[..., subprocess.CompletedProcess[str]]


class MOTOAutoConnector:
    """Check one configured device and connect it without discovery or pairing."""

    def __init__(self, settings: VoiceSettings, *, runner: Runner = subprocess.run) -> None:
        self.settings = settings
        self._run = runner

    def status(self) -> MOTOAudioStatus:
        backend = self.settings.backend
        info = self._command(
            [backend.bluetoothctl_command, "info", backend.device_address],
            timeout=backend.status_timeout_seconds,
        )
        if info.returncode != 0:
            return MOTOAudioStatus(False, False, False, False, 0, "device_info_unavailable")
        values = _parse_bluetoothctl_info(info.stdout)
        reported_name = values.get("Name") or values.get("Alias")
        paired = values.get("Paired", "no").casefold() == "yes"
        trusted = values.get("Trusted", "no").casefold() == "yes"
        connected = values.get("Connected", "no").casefold() == "yes"
        name_matches = bool(
            reported_name
            and reported_name.casefold() == backend.expected_name.casefold()
        )
        sinks = 0
        if connected and name_matches:
            dump = self._command(
                [backend.pw_dump_command], timeout=backend.status_timeout_seconds
            )
            if dump.returncode != 0:
                return MOTOAudioStatus(
                    paired, trusted, connected, name_matches, 0, "pipewire_unavailable"
                )
            try:
                objects = json.loads(dump.stdout)
            except (json.JSONDecodeError, TypeError):
                return MOTOAudioStatus(
                    paired, trusted, connected, name_matches, 0, "pipewire_invalid_json"
                )
            sinks = _matching_audio_sinks(objects, backend.device_address)
        if not paired:
            reason = "device_not_paired"
        elif not trusted:
            reason = "device_not_trusted"
        elif not name_matches:
            reason = "device_name_mismatch"
        elif not connected:
            reason = "device_disconnected"
        elif sinks == 0:
            reason = "audio_sink_missing"
        elif sinks > 1:
            reason = "audio_sink_ambiguous"
        else:
            reason = "audio_ready"
        return MOTOAudioStatus(paired, trusted, connected, name_matches, sinks, reason)

    def connect_once(self, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        backend = self.settings.backend
        return self._command(
            [
                backend.bluetoothctl_command,
                "--timeout",
                str(max(1, math.ceil(timeout_seconds))),
                "connect",
                backend.device_address,
            ],
            timeout=timeout_seconds,
        )

    def bounded_round(
        self,
        *,
        timeout_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> MOTOAudioStatus:
        started = monotonic()
        current = self.status()
        if current.ready or not (current.paired and current.trusted and current.name_matches):
            return current
        remaining = timeout_seconds - (monotonic() - started)
        if remaining <= 0:
            return current
        try:
            self.connect_once(remaining)
        except (OSError, subprocess.SubprocessError):
            return MOTOAudioStatus(
                current.paired, current.trusted, current.connected,
                current.name_matches, current.audio_sink_count, "connect_command_failed"
            )
        while True:
            current = self.status()
            if current.ready:
                return current
            remaining = timeout_seconds - (monotonic() - started)
            if remaining <= 0:
                return current
            sleep(min(0.5, remaining))

    def _command(self, command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
        try:
            return self._run(
                command, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(command, 124, exc.stdout or "", exc.stderr or "")


def _parse_bluetoothctl_info(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        stripped = line.strip()
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            values[key.strip()] = value.strip()
    return values


def _matching_audio_sinks(value: object, address: str) -> int:
    if not isinstance(value, list):
        return 0
    token = address.replace(":", "_").upper()
    matches = 0
    for item in value:
        if not isinstance(item, dict):
            continue
        info = item.get("info")
        props = info.get("props") if isinstance(info, dict) else None
        if not isinstance(props, dict) or props.get("media.class") != "Audio/Sink":
            continue
        node_name = str(props.get("node.name", ""))
        addresses = {
            str(props.get("api.bluez5.address", "")).upper(),
            str(props.get("bluez5.address", "")).upper(),
        }
        if (
            props.get("device.api") == "bluez5"
            and (address.upper() in addresses or token in node_name.upper())
        ):
            matches += 1
    return matches


def _positive(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="指定 MOTO A2 Plus 有界自動連線管理者")
    parser.add_argument("--config", type=Path, default=Path("config/voice-alert.local.toml"))
    parser.add_argument("--interval", type=_positive, default=30.0)
    parser.add_argument("--attempt-timeout", type=_positive, default=20.0)
    parser.add_argument("--once", action="store_true", help="只執行一輪後退出")
    parser.add_argument("--status", action="store_true", help="只查狀態，不 connect")
    return parser


def _log(status: MOTOAudioStatus, now: float) -> None:
    print(
        "moto_autoconnect "
        f"monotonic_seconds={now:.3f} ready={str(status.ready).lower()} "
        f"connected={str(status.connected).lower()} "
        f"audio_sink_count={status.audio_sink_count} reason={status.reason}",
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(None if argv is None else list(argv))
    try:
        settings = load_voice_settings(options.config)
        if settings.enabled:
            raise ValueError("voice.enabled must remain false for autoconnect service")
        connector = MOTOAutoConnector(settings)
    except (OSError, TypeError, ValueError) as exc:
        print(f"moto_autoconnect configuration_error={exc}", file=sys.stderr)
        return 2
    if options.status:
        status = connector.status()
        _log(status, time.monotonic())
        return 0 if status.ready else 1

    last_key: tuple[bool, str] | None = None
    last_log_at = 0.0
    while True:
        round_started = time.monotonic()
        status = connector.bounded_round(timeout_seconds=options.attempt_timeout)
        key = (status.ready, status.reason)
        if key != last_key or round_started - last_log_at >= 600.0:
            _log(status, round_started)
            last_key = key
            last_log_at = round_started
        if options.once:
            return 0 if status.ready else 1
        remaining = options.interval - (time.monotonic() - round_started)
        if remaining > 0:
            time.sleep(remaining)


if __name__ == "__main__":
    raise SystemExit(main())
