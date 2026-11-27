"""Hardware-free WAV helpers for voice prompt playback."""

from __future__ import annotations

import wave
from pathlib import Path

MAX_LEAD_SILENCE_MS = 3000


class VoiceAudioError(ValueError):
    """A prompt recording could not be read or transformed."""


def validate_lead_silence_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("lead_silence_ms must be an integer")
    if not 0 <= value <= MAX_LEAD_SILENCE_MS:
        raise ValueError(f"lead_silence_ms must be within 0..{MAX_LEAD_SILENCE_MS}")
    return value


def pad_wav_with_lead_silence(source: Path, destination: Path, lead_ms: int) -> int:
    """Write ``source`` to ``destination`` with ``lead_ms`` of digital silence first.

    The original recording is only read. Returns the number of silent frames
    added. Bluetooth A2DP sinks need a moment to resume from idle, which on the
    MOTO A2 Plus swallowed about the first 0.7 s of a prompt (2026-11-26).
    """
    validate_lead_silence_ms(lead_ms)
    if source.resolve() == destination.resolve():
        raise ValueError("destination must differ from the source recording")
    try:
        with wave.open(str(source), "rb") as reader:
            params = reader.getparams()
            frames = reader.readframes(reader.getnframes())
    except (wave.Error, EOFError, OSError) as exc:
        raise VoiceAudioError(f"cannot read WAV {source}: {exc}") from exc
    if params.comptype != "NONE":
        raise VoiceAudioError(f"compressed WAV is not supported: {source}")
    silent_frames = params.framerate * lead_ms // 1000
    with wave.open(str(destination), "wb") as writer:
        writer.setparams(params)
        writer.writeframes(b"\x00" * silent_frames * params.nchannels * params.sampwidth)
        writer.writeframes(frames)
    return silent_frames
