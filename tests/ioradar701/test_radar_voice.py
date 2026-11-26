"""Hardware-free tests for radar LED -> voice reminder integration."""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path

import pytest

from rear_warning.cli import ioradar701_led, ioradar701_service
from rear_warning.live.ioradar701_shadow import IoRadar701ShadowCore, ShadowAssessment
from rear_warning.live.radar_led import RadarLedObserver
from rear_warning.live.radar_voice import (
    RadarVoiceNotifier,
    open_radar_voice,
    start_optional_voice,
)
from rear_warning.outputs import LedPins
from rear_warning.voice import (
    AudioRouteStatus,
    VoiceDeliveryOutcome,
    VoiceDeliveryReport,
    VoicePrompt,
    VoicePromptCatalog,
    VoiceReminderDispatcher,
    VoiceReminderEvent,
    VoiceReminderKind,
    VoiceSubmitOutcome,
    VoiceSubmitResult,
)
from rear_warning.warning import WarningState


def _frame(kind: int, *, identifier: int = 0, count: int = 0, x_raw: int = 100) -> bytes:
    body = bytearray(11)
    body[0] = 0xA5
    body[1] = kind
    if kind == 1:
        body[3] = 0x05
        body[6] = count
    else:
        body[3] = identifier
        body[4:6] = x_raw.to_bytes(2, "big")
        body[6:8] = (1200).to_bytes(2, "big")
        body[8:10] = (6500).to_bytes(2, "big")
        body[10] = 80
    return bytes(body) + bytes((sum(body) & 0xFF,))


def _selected(core: IoRadar701ShadowCore, distance_cm: int, timestamp: float) -> ShadowAssessment:
    result, = core.feed_bytes(
        _frame(1, count=1) + _frame(2, identifier=1, x_raw=distance_cm), timestamp
    ).assessments
    return result


class _Output:
    def __init__(self) -> None:
        self.states: dict[int, bool] = {}
        self.closed = False

    def write(self, pin: int, active: bool) -> None:
        self.states[pin] = active

    def close(self) -> None:
        self.closed = True

    @property
    def pattern(self) -> tuple[bool, bool, bool]:
        return tuple(self.states.get(pin, False) for pin in (17, 27, 22))  # type: ignore[return-value]


class _Writer:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def write_led(self, record: dict[str, object]) -> None:
        self.rows.append(record)


class _Sink:
    def __init__(self, outcomes: list[VoiceSubmitOutcome] | None = None) -> None:
        self.submitted: list[tuple[object, float | None]] = []
        self.cleared: list[str] = []
        self.outcomes = outcomes or []
        self.pending = 0

    def submit_warning(self, state: object, *, occurred_at_monotonic: float | None = None,
                       deduplication_key: str | None = None) -> VoiceSubmitResult:
        self.submitted.append((state, occurred_at_monotonic))
        outcome = self.outcomes.pop(0) if self.outcomes else VoiceSubmitOutcome.ACCEPTED
        if outcome is VoiceSubmitOutcome.ACCEPTED:
            self.pending += 1
        return VoiceSubmitResult(outcome, outcome.value)

    def clear_pending(self, reason: str) -> int:
        self.cleared.append(reason)
        cleared, self.pending = self.pending, 0
        return cleared


class _BrokenSink(_Sink):
    def submit_warning(self, state: object, **_kwargs: object) -> VoiceSubmitResult:
        raise RuntimeError("bluetooth exploded")


def test_displayed_alert_states_are_submitted_with_their_timestamp() -> None:
    sink = _Sink()
    notifier = RadarVoiceNotifier(sink)
    for state in (WarningState.WARNING, WarningState.DANGER, WarningState.SENSOR_FAULT):
        records = notifier.notify(state, all_off=False, timestamp=5.0)
        assert records and records[0]["outcome"] == "accepted"
    assert sink.submitted == [
        (WarningState.WARNING, 5.0), (WarningState.DANGER, 5.0), (WarningState.SENSOR_FAULT, 5.0),
    ]


def test_safe_and_hold_are_never_submitted() -> None:
    sink = _Sink()
    notifier = RadarVoiceNotifier(sink)
    assert notifier.notify(WarningState.SAFE, all_off=False, timestamp=1.0) == []
    assert notifier.notify(None, all_off=False, timestamp=1.1) == []
    assert sink.submitted == [] and sink.cleared == []


def test_routine_cooldown_refusals_are_not_recorded_each_cycle() -> None:
    sink = _Sink([VoiceSubmitOutcome.ACCEPTED] + [VoiceSubmitOutcome.COOLDOWN] * 5)
    notifier = RadarVoiceNotifier(sink)
    recorded = [notifier.notify(WarningState.DANGER, all_off=False, timestamp=1.0 + i / 20)
                for i in range(6)]
    assert [len(rows) for rows in recorded] == [1, 0, 0, 0, 0, 0]
    assert len(sink.submitted) == 6


def test_unusual_refusal_is_recorded_once_per_change() -> None:
    sink = _Sink([VoiceSubmitOutcome.QUEUE_FULL] * 3)
    notifier = RadarVoiceNotifier(sink)
    counts = [len(notifier.notify(WarningState.WARNING, all_off=False, timestamp=1.0))
              for _ in range(3)]
    assert counts == [1, 0, 0]


def test_returning_to_safe_or_off_drops_pending_reminders() -> None:
    sink = _Sink()
    notifier = RadarVoiceNotifier(sink)
    notifier.notify(WarningState.WARNING, all_off=False, timestamp=1.0)
    rows = notifier.notify(WarningState.SAFE, all_off=False, timestamp=1.1)
    assert len(sink.cleared) == 1
    assert rows[0]["voice_event"] == "cleared_pending"

    notifier.notify(WarningState.DANGER, all_off=False, timestamp=2.0)
    notifier.notify(None, all_off=True, timestamp=2.1)
    assert len(sink.cleared) == 2


def test_voice_errors_never_raise_into_the_led_loop() -> None:
    notifier = RadarVoiceNotifier(_BrokenSink())
    rows = notifier.notify(WarningState.DANGER, all_off=False, timestamp=1.0)
    assert rows[0]["voice_event"] == "submit_error"
    assert "bluetooth exploded" in str(rows[0]["detail"])


def test_delivery_reports_are_buffered_and_drained() -> None:
    notifier = RadarVoiceNotifier(_Sink())
    event = VoiceReminderEvent(VoiceReminderKind.DISTANCE_TOO_CLOSE, 1.0, "distance_too_close")
    notifier.on_report(VoiceDeliveryReport(event, VoiceDeliveryOutcome.BACKEND_UNAVAILABLE, "headset off"))
    rows = notifier.drain_reports(2.0)
    assert rows == [{
        "timestamp_seconds": 2.0, "kind": "voice", "voice_event": "delivery",
        "voice_kind": "distance_too_close", "occurred_at_monotonic": 1.0,
        "outcome": "backend_unavailable", "detail": "headset off", "route_ready": None,
    }]
    assert notifier.drain_reports(2.1) == []


def test_observer_writes_led_first_and_voice_cannot_change_leds() -> None:
    core = IoRadar701ShadowCore()
    output = _Output()
    writer = _Writer()
    observer = RadarLedObserver(output, LedPins.raspberry_pi_default(),
                                voice=RadarVoiceNotifier(_BrokenSink()))
    observer.start(writer, 0.0)
    observer.observe(_selected(core, 140, 1.0), writer)

    assert output.pattern == (True, False, False)
    assert [row["kind"] for row in writer.rows] == ["startup", "assessment", "voice"]
    assert observer.voice_enabled is True


def test_observer_without_voice_writes_no_voice_records() -> None:
    core = IoRadar701ShadowCore()
    writer = _Writer()
    observer = RadarLedObserver(_Output(), LedPins.raspberry_pi_default())
    observer.start(writer, 0.0)
    observer.observe(_selected(core, 140, 1.0), writer)
    assert all(row["kind"] != "voice" for row in writer.rows)
    assert observer.voice_enabled is False


def _route() -> AudioRouteStatus:
    return AudioRouteStatus("AA:BB:CC:DD:EE:FF", "MOTO", "MOTO", True, True, True, True, "node", "ready")


class _RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, float, int]] = []
        self.played = threading.Event()

    def status(self) -> AudioRouteStatus:
        return _route()

    def play(self, audio_path: Path, *, timeout_seconds: float, volume: float = 1.0,
             lead_silence_ms: int = 0) -> AudioRouteStatus:
        self.calls.append((audio_path.name, volume, lead_silence_ms))
        self.played.set()
        return _route()


def _wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(44100)
        writer.writeframes(b"\x00\x00" * 100)
    return path


def test_end_to_end_danger_plays_too_close_once_per_cooldown(tmp_path: Path) -> None:
    prompts = VoicePromptCatalog({
        VoiceReminderKind.DISTANCE_ATTENTION: VoicePrompt(
            VoiceReminderKind.DISTANCE_ATTENTION, "請留意後方距離", _wav(tmp_path / "a.wav"), 0.6, 600),
        VoiceReminderKind.DISTANCE_TOO_CLOSE: VoicePrompt(
            VoiceReminderKind.DISTANCE_TOO_CLOSE, "注意後方，距離過近", _wav(tmp_path / "b.wav"), 1.0, 800),
        VoiceReminderKind.SENSOR_FAULT: VoicePrompt(
            VoiceReminderKind.SENSOR_FAULT, "感測資料異常", _wav(tmp_path / "c.wav"), 0.6, 600),
    })
    backend = _RecordingBackend()
    holder: list[RadarVoiceNotifier] = []
    dispatcher = VoiceReminderDispatcher(
        enabled=True, backend=backend, prompts=prompts,
        on_report=lambda report: holder[0].on_report(report),
    )
    notifier = RadarVoiceNotifier(dispatcher)
    holder.append(notifier)
    core = IoRadar701ShadowCore()
    writer = _Writer()
    observer = RadarLedObserver(_Output(), LedPins.raspberry_pi_default(), voice=notifier)
    try:
        assert dispatcher.start()
        observer.start(writer, 0.0)
        now = time.monotonic()
        for index in range(5):  # DANGER persists for 0.4 s
            observer.observe(_selected(core, 140, now + index / 10), writer)
        assert backend.played.wait(timeout=5.0)
        time.sleep(0.2)
        observer.observe(_selected(core, 140, time.monotonic()), writer)
    finally:
        assert dispatcher.close()
    assert backend.calls == [("b.wav", 1.0, 800)]
    voice_rows = [row for row in writer.rows if row["kind"] == "voice"]
    assert [row["voice_event"] for row in voice_rows][:1] == ["submit"]
    assert any(row.get("outcome") == "played" for row in voice_rows)


def _config(tmp_path: Path, *, enabled: bool, wav: bool = True) -> Path:
    for name in ("a.wav", "b.wav", "c.wav"):
        if wav:
            _wav(tmp_path / name)
    path = tmp_path / "voice.toml"
    path.write_text(f"""
[voice]
enabled = {"true" if enabled else "false"}
cooldown_seconds = 8.0
event_ttl_seconds = 3.0
queue_capacity = 4
playback_timeout_seconds = 15.0
shutdown_timeout_seconds = 2.0

[device]
expected_name = "MOTO"
address = "AA:BB:CC:DD:EE:FF"
target_node = "bluez_output.test"

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
""", encoding="utf-8")
    return path


def test_open_radar_voice_respects_enabled_false(tmp_path: Path) -> None:
    notifier, dispatcher, message = open_radar_voice(_config(tmp_path, enabled=False))
    assert notifier is None and dispatcher is None
    assert "enabled=false" in message


def test_open_radar_voice_builds_inert_pair_when_enabled(tmp_path: Path) -> None:
    notifier, dispatcher, _message = open_radar_voice(_config(tmp_path, enabled=True))
    assert notifier is not None and dispatcher is not None
    assert dispatcher.enabled is True
    assert dispatcher.close()  # never started: no thread, no playback


def test_open_radar_voice_rejects_missing_audio(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        open_radar_voice(_config(tmp_path, enabled=True, wav=False))


def test_start_optional_voice_never_raises(tmp_path: Path) -> None:
    assert start_optional_voice(None) == (None, None)
    assert start_optional_voice(tmp_path / "missing.toml") == (None, None)
    assert start_optional_voice(_config(tmp_path, enabled=False)) == (None, None)


@pytest.mark.parametrize("module", [ioradar701_led, ioradar701_service])
def test_cli_voice_config_defaults_to_none(module: object) -> None:
    build = getattr(module, "_build_parser")
    assert build().parse_args([]).voice_config is None
    assert build().parse_args(["--voice-config", "x.toml"]).voice_config == Path("x.toml")
