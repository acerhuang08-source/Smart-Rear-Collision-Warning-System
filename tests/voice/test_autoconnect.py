from __future__ import annotations

import subprocess
from pathlib import Path

from rear_warning.voice.autoconnect import MOTOAutoConnector, _matching_audio_sinks
from rear_warning.voice.config import PipeWireBackendSettings, VoiceSettings
from rear_warning.voice.dispatcher import VoicePromptCatalog
from rear_warning.voice.models import VoicePrompt, VoiceReminderKind, VoiceReminderPolicy


ADDRESS = "AA:BB:CC:DD:EE:FF"


def settings() -> VoiceSettings:
    prompts = VoicePromptCatalog({
        kind: VoicePrompt(kind, kind.value, Path(f"{kind.value}.wav"))
        for kind in VoiceReminderKind
    })
    return VoiceSettings(
        False,
        VoiceReminderPolicy(),
        PipeWireBackendSettings(ADDRESS, "MOTO", "unused-dynamic-target"),
        prompts,
    )


def info(*, connected: bool) -> str:
    return (
        "Name: MOTO\nPaired: yes\nTrusted: yes\n"
        f"Connected: {'yes' if connected else 'no'}\n"
    )


def dump(count: int = 1) -> str:
    items = [
        {
            "info": {"props": {
                "media.class": "Audio/Sink",
                "device.api": "bluez5",
                "node.name": f"bluez_output.{ADDRESS.replace(':', '_')}.{index}",
            }}
        }
        for index in range(count)
    ]
    import json
    return json.dumps(items)


def completed(command: list[str], output: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, code, output, "")


def test_ready_status_does_not_connect() -> None:
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        is_info = len(command) > 1 and command[1] == "info"
        return completed(command, info(connected=True) if is_info else dump())

    result = MOTOAutoConnector(settings(), runner=runner).bounded_round(timeout_seconds=20)
    assert result.ready
    assert not any("connect" in command for command in commands)


def test_disconnected_device_connects_then_requires_unique_sink() -> None:
    infos = iter((info(connected=False), info(connected=True)))
    commands: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if len(command) > 1 and command[1] == "info":
            return completed(command, next(infos))
        if "connect" in command:
            return completed(command, "Connection successful")
        return completed(command, dump())

    result = MOTOAutoConnector(settings(), runner=runner).bounded_round(timeout_seconds=20)
    assert result.ready
    connect = [command for command in commands if "connect" in command]
    assert len(connect) == 1
    assert connect[0][-1] == ADDRESS


def test_connected_without_sink_is_not_audio_ready() -> None:
    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if len(command) > 1 and command[1] == "info":
            return completed(command, info(connected=True))
        if "connect" in command:
            return completed(command)
        return completed(command, dump(0))

    times = iter((0.0, 0.0, 20.0, 20.0))
    result = MOTOAutoConnector(settings(), runner=runner).bounded_round(
        timeout_seconds=20,
        monotonic=lambda: next(times),
        sleep=lambda _seconds: None,
    )
    assert not result.ready
    assert result.reason == "audio_sink_missing"


def test_sink_filter_rejects_other_device_and_ambiguity() -> None:
    import json
    other = json.loads(dump())[0]
    other["info"]["props"]["node.name"] = "bluez_output.11_22_33_44_55_66.1"
    own = json.loads(dump(2))
    assert _matching_audio_sinks([other], ADDRESS) == 0
    assert _matching_audio_sinks(own, ADDRESS) == 2


def test_versioned_units_do_not_contain_private_identity() -> None:
    unit = Path("config/systemd/user/rear-warning-moto-autoconnect.service").read_text()
    wireplumber = Path(
        "config/wireplumber/91-rear-warning-headless-bluetooth.conf"
    ).read_text()
    assert ADDRESS not in unit + wireplumber
    assert "monitor.bluez.seat-monitoring = disabled" in wireplumber
    assert "--interval 30 --attempt-timeout 20" in unit
