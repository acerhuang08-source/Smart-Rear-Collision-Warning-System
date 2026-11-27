"""Bridge displayed radar LED states to the opt-in voice reminder dispatcher.

Voice follows the state that is actually shown on the LEDs (after any vehicle
motion relaxation): WARNING, DANGER and SENSOR_FAULT are submitted on every
displayed cycle, and the dispatcher's per-prompt cooldown turns a persisting
state into one reminder per cooldown period (8 s by default). When the LEDs
return to SAFE or all-off, reminders still waiting in the queue are dropped so
a stale prompt is not spoken after the situation cleared.

Voice is strictly best-effort: no voice error may affect LED control. Delivery
reports arrive on the dispatcher's worker thread and are buffered here so the
single capture writer only ever runs on the sensor loop thread.
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path
from typing import Protocol

from rear_warning.voice import (
    VoiceDeliveryReport,
    VoiceReminderDispatcher,
    VoiceSubmitOutcome,
    VoiceSubmitResult,
    build_voice_dispatcher,
    load_voice_settings,
)
from rear_warning.warning import WarningState

VOICE_STATES = frozenset(
    {WarningState.WARNING, WarningState.DANGER, WarningState.SENSOR_FAULT}
)
# Routine per-cycle refusals that would otherwise flood the record.
_QUIET_OUTCOMES = frozenset(
    {VoiceSubmitOutcome.COOLDOWN, VoiceSubmitOutcome.DUPLICATE_PENDING}
)


class VoiceSink(Protocol):
    def submit_warning(
        self,
        state: object,
        *,
        occurred_at_monotonic: float | None = None,
        deduplication_key: str | None = None,
    ) -> VoiceSubmitResult: ...

    def clear_pending(self, reason: str) -> int: ...


class RadarVoiceNotifier:
    """Submit displayed warning states and collect auditable voice records."""

    def __init__(self, sink: VoiceSink) -> None:
        self._sink = sink
        self._reports: deque[VoiceDeliveryReport] = deque(maxlen=256)
        self._last_outcome: dict[WarningState, VoiceSubmitOutcome] = {}
        self._alert_active = False

    def on_report(self, report: VoiceDeliveryReport) -> None:
        """Dispatcher callback (worker thread); deque.append is thread-safe."""
        self._reports.append(report)

    def notify(
        self,
        state: WarningState | None,
        *,
        all_off: bool,
        timestamp: float,
    ) -> list[dict[str, object]]:
        """Handle one displayed LED result; return records to persist."""
        records: list[dict[str, object]] = []
        if state in VOICE_STATES:
            assert state is not None
            self._alert_active = True
            try:
                result = self._sink.submit_warning(
                    state, occurred_at_monotonic=timestamp
                )
            except Exception as exc:  # voice must never break LED control
                records.append(self._record(timestamp, "submit_error", state=state,
                                            detail=f"{type(exc).__name__}: {exc}"))
                return records
            previous = self._last_outcome.get(state)
            self._last_outcome[state] = result.outcome
            if result.accepted or (
                result.outcome not in _QUIET_OUTCOMES and result.outcome is not previous
            ):
                records.append(self._record(timestamp, "submit", state=state,
                                            outcome=result.outcome.value,
                                            detail=result.detail))
        elif (all_off or state is WarningState.SAFE) and self._alert_active:
            self._alert_active = False
            self._last_outcome.clear()
            try:
                cleared = self._sink.clear_pending("LED display returned to SAFE or all-off")
            except Exception as exc:
                records.append(self._record(timestamp, "clear_error",
                                            detail=f"{type(exc).__name__}: {exc}"))
            else:
                if cleared:
                    records.append(self._record(timestamp, "cleared_pending",
                                                detail=f"{cleared} pending reminder(s) dropped"))
        return records

    def drain_reports(self, timestamp: float) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        while self._reports:
            report = self._reports.popleft()
            route = report.route_status
            records.append(
                {
                    "timestamp_seconds": timestamp,
                    "kind": "voice",
                    "voice_event": "delivery",
                    "voice_kind": report.event.kind.value,
                    "occurred_at_monotonic": report.event.occurred_at_monotonic,
                    "outcome": report.outcome.value,
                    "detail": report.detail,
                    "route_ready": None if route is None else route.ready,
                }
            )
        return records

    @staticmethod
    def _record(
        timestamp: float,
        event: str,
        *,
        state: WarningState | None = None,
        outcome: str | None = None,
        detail: str,
    ) -> dict[str, object]:
        return {
            "timestamp_seconds": timestamp,
            "kind": "voice",
            "voice_event": event,
            "warning_state": None if state is None else state.value,
            "outcome": outcome,
            "detail": detail,
        }


def open_radar_voice(
    config: Path,
) -> tuple[RadarVoiceNotifier | None, VoiceReminderDispatcher | None, str]:
    """Load the voice config and build an inert notifier/dispatcher pair.

    Returns ``(None, None, reason)`` when the config keeps ``enabled = false``.
    The caller must ``start()`` the dispatcher and ``close()`` it on exit.
    Loading never scans, pairs, connects or plays.
    """
    settings = load_voice_settings(config)
    if not settings.enabled:
        return None, None, f"voice config {config} has enabled=false; voice stays off"
    missing = settings.prompts.missing_audio_files()
    if missing:
        raise FileNotFoundError(f"voice prompt files missing: {[str(p) for p in missing]}")
    holder: list[RadarVoiceNotifier] = []
    dispatcher = build_voice_dispatcher(
        settings, on_report=lambda report: holder[0].on_report(report)
    )
    notifier = RadarVoiceNotifier(dispatcher)
    holder.append(notifier)
    return notifier, dispatcher, f"voice enabled from {config}"


def start_optional_voice(
    config: Path | None,
) -> tuple[RadarVoiceNotifier | None, VoiceReminderDispatcher | None]:
    """CLI helper: voice problems are reported but never stop the LED warning."""
    if config is None:
        print("語音提醒：未指定 --voice-config，維持關閉。", flush=True)
        return None, None
    try:
        notifier, dispatcher, message = open_radar_voice(config)
    except Exception as exc:
        print(
            f"語音提醒：設定無法使用，維持關閉，燈號照常：{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return None, None
    if dispatcher is None:
        print(f"語音提醒：{message}", flush=True)
        return None, None
    if not dispatcher.start():
        print("語音提醒：dispatcher 未啟動，維持關閉。", file=sys.stderr, flush=True)
        return None, None
    print(f"語音提醒：{message}（WARNING／DANGER／SENSOR_FAULT；同句冷卻內不重播）", flush=True)
    return notifier, dispatcher


def stop_optional_voice(dispatcher: VoiceReminderDispatcher | None) -> None:
    if dispatcher is None:
        return
    try:
        if not dispatcher.close():
            print("語音提醒：播放執行緒未在時限內結束。", file=sys.stderr, flush=True)
    except Exception as exc:
        print(f"語音提醒：關閉失敗：{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
