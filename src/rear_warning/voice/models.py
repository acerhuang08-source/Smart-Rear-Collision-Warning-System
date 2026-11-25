"""Typed, hardware-independent models for bounded voice reminders."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class VoiceReminderKind(str, Enum):
    """Voice messages accepted from confirmed warning states."""

    DISTANCE_ATTENTION = "distance_attention"
    DISTANCE_TOO_CLOSE = "distance_too_close"
    SENSOR_FAULT = "sensor_fault"

    @property
    def priority(self) -> int:
        """Return a stable priority used only for pending reminder ordering."""
        return {
            VoiceReminderKind.DISTANCE_ATTENTION: 100,
            VoiceReminderKind.SENSOR_FAULT: 200,
            VoiceReminderKind.DISTANCE_TOO_CLOSE: 300,
        }[self]


@dataclass(frozen=True, slots=True)
class VoiceReminderEvent:
    """One timestamped reminder request created by an existing warning event."""

    kind: VoiceReminderKind
    occurred_at_monotonic: float
    deduplication_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, VoiceReminderKind):
            raise TypeError("kind must be a VoiceReminderKind")
        if isinstance(self.occurred_at_monotonic, bool) or not isinstance(
            self.occurred_at_monotonic, (int, float)
        ):
            raise TypeError("occurred_at_monotonic must be a number")
        if not math.isfinite(float(self.occurred_at_monotonic)):
            raise ValueError("occurred_at_monotonic must be finite")
        if self.occurred_at_monotonic < 0:
            raise ValueError("occurred_at_monotonic must be non-negative")
        if not isinstance(self.deduplication_key, str):
            raise TypeError("deduplication_key must be a string")
        if not self.deduplication_key.strip():
            raise ValueError("deduplication_key must not be empty")


@dataclass(frozen=True, slots=True)
class VoiceReminderPolicy:
    """Queue and lifetime limits; all values are monotonic-clock seconds."""

    cooldown_seconds: float = 8.0
    event_ttl_seconds: float = 3.0
    queue_capacity: int = 4
    playback_timeout_seconds: float = 15.0
    shutdown_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name in (
            "cooldown_seconds",
            "event_ttl_seconds",
            "playback_timeout_seconds",
            "shutdown_timeout_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise TypeError(f"{name} must be a number")
            if not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if isinstance(self.queue_capacity, bool) or not isinstance(
            self.queue_capacity, int
        ):
            raise TypeError("queue_capacity must be an integer")
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be greater than zero")


@dataclass(frozen=True, slots=True)
class VoicePrompt:
    """Operator-supplied recording metadata; the module never synthesizes it."""

    kind: VoiceReminderKind
    text: str
    audio_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.kind, VoiceReminderKind):
            raise TypeError("kind must be a VoiceReminderKind")
        if not isinstance(self.text, str):
            raise TypeError("prompt text must be a string")
        if not self.text.strip():
            raise ValueError("prompt text must not be empty")
        if not isinstance(self.audio_path, Path):
            raise TypeError("audio_path must be a pathlib.Path")


class VoiceSubmitOutcome(str, Enum):
    """Immediate, non-blocking submission result."""

    ACCEPTED = "accepted"
    REPLACED_LOWER_PRIORITY = "replaced_lower_priority"
    DISABLED = "disabled"
    NOT_STARTED = "not_started"
    CLOSED = "closed"
    SAFE_IGNORED = "safe_ignored"
    UNSUPPORTED_STATE = "unsupported_state"
    EXPIRED = "expired"
    DUPLICATE_PENDING = "duplicate_pending"
    COOLDOWN = "cooldown"
    QUEUE_FULL = "queue_full"


@dataclass(frozen=True, slots=True)
class VoiceSubmitResult:
    """Result returned to the sensor/fusion caller without waiting for audio."""

    outcome: VoiceSubmitOutcome
    detail: str
    dropped_pending_events: int = 0

    @property
    def accepted(self) -> bool:
        return self.outcome in {
            VoiceSubmitOutcome.ACCEPTED,
            VoiceSubmitOutcome.REPLACED_LOWER_PRIORITY,
        }


@dataclass(frozen=True, slots=True)
class AudioRouteStatus:
    """Keep BlueZ connection and PipeWire route availability distinct."""

    device_address: str
    expected_name: str
    reported_name: str | None
    paired: bool
    connected: bool
    name_matches: bool
    audio_output_available: bool
    target_node: str
    detail: str

    @property
    def ready(self) -> bool:
        return (
            self.paired
            and self.connected
            and self.name_matches
            and self.audio_output_available
        )


class VoiceDeliveryOutcome(str, Enum):
    """Asynchronous worker result for logging or operator diagnostics."""

    PLAYED = "played"
    EXPIRED = "expired"
    AUDIO_FILE_MISSING = "audio_file_missing"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    PLAYBACK_FAILED = "playback_failed"
    CLEARED_AFTER_DISCONNECT = "cleared_after_disconnect"


@dataclass(frozen=True, slots=True)
class VoiceDeliveryReport:
    """One auditable asynchronous delivery outcome."""

    event: VoiceReminderEvent
    outcome: VoiceDeliveryOutcome
    detail: str
    route_status: AudioRouteStatus | None = None
