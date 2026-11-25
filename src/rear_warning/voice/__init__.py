"""Optional, explicit-start Bluetooth voice reminder output."""

from .backend import (
    PipeWireBluetoothBackend,
    VoiceBackendError,
    VoiceBackendUnavailable,
    VoicePlaybackBackend,
    VoicePlaybackFailed,
)
from .config import (
    PipeWireBackendSettings,
    VoiceSettings,
    build_voice_dispatcher,
    load_voice_settings,
)
from .dispatcher import VoicePromptCatalog, VoiceReminderDispatcher
from .models import (
    AudioRouteStatus,
    VoiceDeliveryOutcome,
    VoiceDeliveryReport,
    VoicePrompt,
    VoiceReminderEvent,
    VoiceReminderKind,
    VoiceReminderPolicy,
    VoiceSubmitOutcome,
    VoiceSubmitResult,
)

__all__ = [
    "AudioRouteStatus",
    "PipeWireBackendSettings",
    "PipeWireBluetoothBackend",
    "VoiceBackendError",
    "VoiceBackendUnavailable",
    "VoiceDeliveryOutcome",
    "VoiceDeliveryReport",
    "VoicePlaybackBackend",
    "VoicePlaybackFailed",
    "VoicePrompt",
    "VoicePromptCatalog",
    "VoiceReminderDispatcher",
    "VoiceReminderEvent",
    "VoiceReminderKind",
    "VoiceReminderPolicy",
    "VoiceSettings",
    "VoiceSubmitOutcome",
    "VoiceSubmitResult",
    "build_voice_dispatcher",
    "load_voice_settings",
]
