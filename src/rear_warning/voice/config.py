"""TOML configuration loading without activating Bluetooth or audio."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .backend import PipeWireBluetoothBackend
from .dispatcher import VoicePromptCatalog, VoiceReminderDispatcher
from .models import VoicePrompt, VoiceReminderKind, VoiceReminderPolicy


@dataclass(frozen=True, slots=True)
class PipeWireBackendSettings:
    device_address: str
    expected_name: str
    target_node: str
    bluetoothctl_command: str = "/usr/bin/bluetoothctl"
    pw_dump_command: str = "/usr/bin/pw-dump"
    pw_play_command: str = "/usr/bin/pw-play"
    status_timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    enabled: bool
    policy: VoiceReminderPolicy
    backend: PipeWireBackendSettings
    prompts: VoicePromptCatalog


def load_voice_settings(path: Path) -> VoiceSettings:
    """Load and validate configuration; never query, connect, or play."""
    if not isinstance(path, Path):
        raise TypeError("path must be a pathlib.Path")
    with path.open("rb") as file:
        document = tomllib.load(file)
    if not isinstance(document, dict):
        raise ValueError("voice configuration root must be a TOML table")
    voice = _table(document, "voice")
    device = _table(document, "device")
    backend = _table(document, "backend")
    prompts = _table(document, "prompts")

    enabled = _boolean(voice, "enabled")
    policy = VoiceReminderPolicy(
        cooldown_seconds=_number(voice, "cooldown_seconds"),
        event_ttl_seconds=_number(voice, "event_ttl_seconds"),
        queue_capacity=_integer(voice, "queue_capacity"),
        playback_timeout_seconds=_number(voice, "playback_timeout_seconds"),
        shutdown_timeout_seconds=_number(voice, "shutdown_timeout_seconds"),
    )
    backend_kind = _string(backend, "kind")
    if backend_kind != "pipewire":
        raise ValueError("backend.kind must be 'pipewire'")
    backend_settings = PipeWireBackendSettings(
        device_address=_string(device, "address"),
        expected_name=_string(device, "expected_name"),
        target_node=_string(device, "target_node"),
        bluetoothctl_command=_string(backend, "bluetoothctl_command"),
        pw_dump_command=_string(backend, "pw_dump_command"),
        pw_play_command=_string(backend, "pw_play_command"),
        status_timeout_seconds=_number(backend, "status_timeout_seconds"),
    )
    base = path.parent
    catalog = VoicePromptCatalog(
        {
            VoiceReminderKind.DISTANCE_ATTENTION: VoicePrompt(
                VoiceReminderKind.DISTANCE_ATTENTION,
                "請留意後方距離",
                _resolve_path(base, _string(prompts, "distance_attention")),
            ),
            VoiceReminderKind.DISTANCE_TOO_CLOSE: VoicePrompt(
                VoiceReminderKind.DISTANCE_TOO_CLOSE,
                "注意後方，距離過近",
                _resolve_path(base, _string(prompts, "distance_too_close")),
            ),
            VoiceReminderKind.SENSOR_FAULT: VoicePrompt(
                VoiceReminderKind.SENSOR_FAULT,
                "感測資料異常",
                _resolve_path(base, _string(prompts, "sensor_fault")),
            ),
        }
    )
    if enabled:
        _reject_placeholders(backend_settings)
    return VoiceSettings(enabled, policy, backend_settings, catalog)


def build_voice_dispatcher(settings: VoiceSettings) -> VoiceReminderDispatcher:
    """Build an inert dispatcher; callers must still invoke ``start()``."""
    backend = None
    if settings.enabled:
        backend = PipeWireBluetoothBackend(
            device_address=settings.backend.device_address,
            expected_name=settings.backend.expected_name,
            target_node=settings.backend.target_node,
            bluetoothctl_command=settings.backend.bluetoothctl_command,
            pw_dump_command=settings.backend.pw_dump_command,
            pw_play_command=settings.backend.pw_play_command,
            status_timeout_seconds=settings.backend.status_timeout_seconds,
        )
    return VoiceReminderDispatcher(
        enabled=settings.enabled,
        backend=backend,
        prompts=settings.prompts,
        policy=settings.policy,
    )


def _table(document: dict[str, Any], key: str) -> dict[str, Any]:
    value = document.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid [{key}] table")
    return value


def _string(table: dict[str, Any], key: str) -> str:
    value = table.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _boolean(table: dict[str, Any], key: str) -> bool:
    value = table.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def _number(table: dict[str, Any], key: str) -> float:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _integer(table: dict[str, Any], key: str) -> int:
    value = table.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _resolve_path(base: Path, value: str) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else (base / candidate).resolve()


def _reject_placeholders(settings: PipeWireBackendSettings) -> None:
    values = (settings.device_address, settings.target_node)
    if any("REPLACE_" in value or "UNKNOWN" in value for value in values):
        raise ValueError(
            "enabled voice configuration requires the verified MOTO MAC and "
            "explicit PipeWire Audio/Sink target"
        )
