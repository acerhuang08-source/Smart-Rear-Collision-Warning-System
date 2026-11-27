"""Hardware-free tests for the MOTO playback experiment's new test options."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import wave
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

from rear_warning.voice import (
    VoicePrompt,
    VoicePromptCatalog,
    VoiceReminderKind,
    VoiceReminderPolicy,
    VoiceSettings,
)
from rear_warning.voice.config import PipeWireBackendSettings

ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = ROOT / "tools" / "voice_playback_experiment.py"


@pytest.fixture()
def tool() -> Iterator[ModuleType]:
    spec = importlib.util.spec_from_file_location("voice_playback_experiment", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


def _write_wav(path: Path, *, frames: int = 2205, rate: int = 22050) -> bytes:
    payload = bytes((index % 200) + 1 for index in range(frames * 2))
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(payload)
    return payload


def test_padding_prepends_exact_silence_and_keeps_format(
    tool: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "prompt.wav"
    payload = _write_wav(source)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = tmp_path / "padded.wav"

    added = tool.pad_wav_with_lead_silence(source, destination, 600)

    assert added == 22050 * 600 // 1000
    with wave.open(str(destination), "rb") as reader:
        assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (
            1, 2, 22050,
        )
        assert reader.getnframes() == added + 2205
        data = reader.readframes(reader.getnframes())
    assert data[: added * 2] == b"\x00" * (added * 2)
    assert data[added * 2:] == payload
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_zero_padding_is_a_faithful_copy(tool: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "prompt.wav"
    payload = _write_wav(source)
    destination = tmp_path / "copy.wav"

    assert tool.pad_wav_with_lead_silence(source, destination, 0) == 0
    with wave.open(str(destination), "rb") as reader:
        assert reader.readframes(reader.getnframes()) == payload


def test_padding_rejects_bad_bounds_same_path_and_non_wav(
    tool: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "prompt.wav"
    _write_wav(source)
    with pytest.raises(ValueError):
        tool.pad_wav_with_lead_silence(source, tmp_path / "x.wav", -1)
    with pytest.raises(ValueError):
        tool.pad_wav_with_lead_silence(source, tmp_path / "x.wav", 3001)
    with pytest.raises(ValueError):
        tool.pad_wav_with_lead_silence(source, source, 100)
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not a wav")
    with pytest.raises(tool.ExperimentError):
        tool.pad_wav_with_lead_silence(broken, tmp_path / "y.wav", 100)


def test_parser_defaults_and_validation(tool: ModuleType) -> None:
    options = tool._parser().parse_args([])
    assert options.lead_silence_ms is None
    assert options.prompts is None
    assert options.volume is None

    options = tool._parser().parse_args(
        ["--volume", "1.0", "--lead-silence-ms", "600", "--prompts", "fault", "attention"]
    )
    assert (options.volume, options.lead_silence_ms) == (1.0, 600)
    assert options.prompts == ["fault", "attention"]
    for argv in (["--lead-silence-ms", "5000"], ["--prompts", "all"], ["--volume", "0"]):
        with pytest.raises(SystemExit):
            tool._parser().parse_args(argv)


class _ReadyRoute:
    ready = True
    detail = "ready"


class _Backend:
    def status(self) -> _ReadyRoute:
        return _ReadyRoute()


def _settings(tmp_path: Path) -> VoiceSettings:
    prompts = {}
    for kind in VoiceReminderKind:
        path = tmp_path / f"{kind.value}.wav"
        _write_wav(path)
        prompts[kind] = VoicePrompt(kind, f"text {kind.value}", path)
    return VoiceSettings(
        False,
        VoiceReminderPolicy(),
        PipeWireBackendSettings("AA:BB:CC:DD:EE:FF", "MOTO", "node", pw_play_command="pw-play"),
        VoicePromptCatalog(prompts),
    )


def test_selected_prompts_play_padded_copies_in_scratch_only(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(tool, "_play", lambda command: commands.append(command))
    monkeypatch.setattr(tool, "_wait_for_enter", lambda: None)
    monkeypatch.setattr(
        tool, "_feedback", lambda: tool.OperatorResult(True, True, "appropriate", False)
    )

    result = tool._play_selected(
        settings,
        _Backend(),
        (VoiceReminderKind.SENSOR_FAULT,),
        1.0,
        600,
        scratch,
    )

    assert result == 0
    assert len(commands) == 1
    played = Path(commands[0][-1])
    assert played.parent == scratch
    assert played.name == "lead600ms-sensor_fault.wav"
    assert "--volume=1.00" in commands[0]
    assert "--target=node" in commands[0]


def test_omitted_options_use_each_prompts_configured_playback(
    tool: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    prompts = {
        kind: VoicePrompt(
            kind,
            settings.prompts.prompt_for(kind).text,
            settings.prompts.prompt_for(kind).audio_path,
            1.0 if kind is VoiceReminderKind.DISTANCE_TOO_CLOSE else 0.6,
            800 if kind is VoiceReminderKind.DISTANCE_TOO_CLOSE else 600,
        )
        for kind in VoiceReminderKind
    }
    settings = VoiceSettings(
        False, settings.policy, settings.backend, VoicePromptCatalog(prompts)
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    commands: list[list[str]] = []
    monkeypatch.setattr(tool, "_play", lambda command: commands.append(command))
    monkeypatch.setattr(tool, "_wait_for_enter", lambda: None)
    monkeypatch.setattr(
        tool, "_feedback", lambda: tool.OperatorResult(True, True, "appropriate", False)
    )

    assert tool._play_selected(
        settings, _Backend(), tool._PROMPT_ORDER, None, None, scratch
    ) == 0

    assert [command[-2] for command in commands] == [
        "--volume=0.60", "--volume=1.00", "--volume=0.60",
    ]
    assert [Path(command[-1]).name for command in commands] == [
        "lead600ms-distance_attention.wav",
        "lead800ms-distance_too_close.wav",
        "lead600ms-sensor_fault.wav",
    ]
