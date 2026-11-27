"""Hardware-free tests for per-prompt playback volume and lead silence."""

from __future__ import annotations

import subprocess
import threading
import wave
from pathlib import Path

import pytest

from rear_warning.voice import (
    AudioRouteStatus,
    PipeWireBluetoothBackend,
    VoiceDeliveryOutcome,
    VoiceDeliveryReport,
    VoicePrompt,
    VoicePromptCatalog,
    VoiceReminderDispatcher,
    VoiceReminderKind,
    load_voice_settings,
)
from rear_warning.warning import WarningState

ROOT = Path(__file__).resolve().parents[2]


def _wav(path: Path, frames: int = 441) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b"\x01\x02" * frames)
    return path


def _route() -> AudioRouteStatus:
    return AudioRouteStatus("AA:BB:CC:DD:EE:FF", "MOTO", "MOTO", True, True, True, True,
                            "node", "ready")


@pytest.mark.parametrize(
    ("volume", "lead"),
    [(0.0, 0), (1.5, 0), (float("nan"), 0), (0.6, -1), (0.6, 3001)],
)
def test_prompt_rejects_invalid_playback_settings(volume: float, lead: int) -> None:
    with pytest.raises(ValueError):
        VoicePrompt(VoiceReminderKind.SENSOR_FAULT, "感測資料異常", Path("x.wav"), volume, lead)


def test_prompt_defaults_keep_previous_behaviour() -> None:
    prompt = VoicePrompt(VoiceReminderKind.SENSOR_FAULT, "感測資料異常", Path("x.wav"))
    assert (prompt.playback_volume, prompt.lead_silence_ms) == (1.0, 0)


def _config(tmp_path: Path, playback: str) -> Path:
    path = tmp_path / "voice.toml"
    path.write_text(
        """
[voice]
enabled = false
cooldown_seconds = 8.0
event_ttl_seconds = 3.0
queue_capacity = 4
playback_timeout_seconds = 15.0
shutdown_timeout_seconds = 2.0

[device]
expected_name = "MOTO"
address = "REPLACE_WITH_MOTO_MAC"
target_node = "REPLACE"

[backend]
kind = "pipewire"
bluetoothctl_command = "/usr/bin/bluetoothctl"
pw_dump_command = "/usr/bin/pw-dump"
pw_play_command = "/usr/bin/pw-play"
status_timeout_seconds = 3.0

[prompts]
distance_attention = "a.wav"
distance_too_close = "b.wav"
sensor_fault = "c.wav"
""" + playback,
        encoding="utf-8",
    )
    return path


def test_config_reads_per_prompt_playback_and_defaults(tmp_path: Path) -> None:
    settings = load_voice_settings(_config(tmp_path, """
[prompt_playback.distance_too_close]
volume = 1.0
lead_silence_ms = 800

[prompt_playback.sensor_fault]
volume = 0.6
"""))
    too_close = settings.prompts.prompt_for(VoiceReminderKind.DISTANCE_TOO_CLOSE)
    fault = settings.prompts.prompt_for(VoiceReminderKind.SENSOR_FAULT)
    attention = settings.prompts.prompt_for(VoiceReminderKind.DISTANCE_ATTENTION)
    assert (too_close.playback_volume, too_close.lead_silence_ms) == (1.0, 800)
    assert (fault.playback_volume, fault.lead_silence_ms) == (0.6, 0)
    assert (attention.playback_volume, attention.lead_silence_ms) == (1.0, 0)
    assert too_close.audio_path == tmp_path / "b.wav"


@pytest.mark.parametrize(
    "playback",
    [
        "\n[prompt_playback.unknown]\nvolume = 0.5\n",
        "\n[prompt_playback.sensor_fault]\ngain = 2\n",
        "\n[prompt_playback.sensor_fault]\nvolume = 2.0\n",
        "\n[prompt_playback.sensor_fault]\nlead_silence_ms = 0.5\n",
    ],
)
def test_config_rejects_bad_playback_entries(tmp_path: Path, playback: str) -> None:
    with pytest.raises(ValueError):
        load_voice_settings(_config(tmp_path, playback))


def test_repository_example_config_uses_the_auditioned_values() -> None:
    settings = load_voice_settings(ROOT / "config" / "voice-alert.example.toml")
    assert settings.enabled is False
    expected = {
        VoiceReminderKind.DISTANCE_ATTENTION: ("rear_distance_attention_v2.wav", 0.6, 600),
        VoiceReminderKind.DISTANCE_TOO_CLOSE: ("rear_distance_too_close_v2.wav", 1.0, 800),
        VoiceReminderKind.SENSOR_FAULT: ("sensor_fault_v2.wav", 0.6, 600),
    }
    for kind, (name, volume, lead) in expected.items():
        prompt = settings.prompts.prompt_for(kind)
        assert prompt.audio_path.name == name
        assert prompt.audio_path.is_file()
        assert (prompt.playback_volume, prompt.lead_silence_ms) == (volume, lead)


class _ReadyBackend(PipeWireBluetoothBackend):
    def status(self) -> AudioRouteStatus:
        return _route()


def test_backend_plays_padded_temporary_copy_at_stream_volume(tmp_path: Path) -> None:
    source = _wav(tmp_path / "prompt.wav")
    original = source.read_bytes()
    seen: dict[str, object] = {}

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        played = Path(command[-1])
        seen["command"] = command
        seen["played"] = played
        with wave.open(str(played), "rb") as reader:
            seen["frames"] = reader.getnframes()
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = _ReadyBackend(
        device_address="AA:BB:CC:DD:EE:FF",
        expected_name="MOTO",
        target_node="node",
        command_runner=runner,
    )
    backend.play(source, timeout_seconds=5.0, volume=0.6, lead_silence_ms=600)

    command = seen["command"]
    assert isinstance(command, list)
    assert "--volume=0.60" in command
    assert "--target=node" in command
    played = seen["played"]
    assert isinstance(played, Path)
    assert played != source
    assert seen["frames"] == 441 + 44100 * 600 // 1000
    assert not played.exists()
    assert source.read_bytes() == original


def test_backend_without_lead_plays_original_file(tmp_path: Path) -> None:
    source = _wav(tmp_path / "prompt.wav")
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    backend = _ReadyBackend(
        device_address="AA:BB:CC:DD:EE:FF",
        expected_name="MOTO",
        target_node="node",
        command_runner=runner,
    )
    backend.play(source, timeout_seconds=5.0)

    assert commands[0][-1] == str(source)
    assert "--volume=1.00" in commands[0]


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, float, int]] = []

    def status(self) -> AudioRouteStatus:
        return _route()

    def play(
        self,
        audio_path: Path,
        *,
        timeout_seconds: float,
        volume: float = 1.0,
        lead_silence_ms: int = 0,
    ) -> AudioRouteStatus:
        self.calls.append((audio_path, volume, lead_silence_ms))
        return _route()


def test_dispatcher_passes_each_prompts_playback_settings(tmp_path: Path) -> None:
    prompts = VoicePromptCatalog({
        VoiceReminderKind.DISTANCE_ATTENTION: VoicePrompt(
            VoiceReminderKind.DISTANCE_ATTENTION, "請留意後方距離", _wav(tmp_path / "a.wav"), 0.6, 600),
        VoiceReminderKind.DISTANCE_TOO_CLOSE: VoicePrompt(
            VoiceReminderKind.DISTANCE_TOO_CLOSE, "注意後方，距離過近", _wav(tmp_path / "b.wav"), 1.0, 800),
        VoiceReminderKind.SENSOR_FAULT: VoicePrompt(
            VoiceReminderKind.SENSOR_FAULT, "感測資料異常", _wav(tmp_path / "c.wav"), 0.6, 600),
    })
    backend = _RecordingBackend()
    played = threading.Event()
    reports: list[VoiceDeliveryReport] = []

    def on_report(report: VoiceDeliveryReport) -> None:
        reports.append(report)
        played.set()

    dispatcher = VoiceReminderDispatcher(
        enabled=True, backend=backend, prompts=prompts, on_report=on_report
    )
    try:
        assert dispatcher.start() is True
        assert dispatcher.submit_warning(WarningState.DANGER).accepted
        assert played.wait(timeout=5.0)
    finally:
        assert dispatcher.close()

    assert reports[0].outcome is VoiceDeliveryOutcome.PLAYED
    assert backend.calls == [(tmp_path / "b.wav", 1.0, 800)]
