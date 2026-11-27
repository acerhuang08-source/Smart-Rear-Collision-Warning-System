"""Non-blocking, bounded, single-player voice reminder dispatcher."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from rear_warning.warning import WarningState

from .backend import (
    VoiceBackendUnavailable,
    VoicePlaybackBackend,
    VoicePlaybackFailed,
)
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


_ReportCallback = Callable[[VoiceDeliveryReport], None]


class VoicePromptCatalog:
    """Complete mapping of reminder kinds to operator-supplied recordings."""

    def __init__(self, prompts: Mapping[VoiceReminderKind, VoicePrompt]) -> None:
        self._prompts = dict(prompts)
        missing = set(VoiceReminderKind) - set(self._prompts)
        extra = set(self._prompts) - set(VoiceReminderKind)
        if missing or extra:
            raise ValueError(
                "prompt catalog must contain every reminder kind exactly once; "
                f"missing={sorted(item.value for item in missing)}, "
                f"extra={sorted(str(item) for item in extra)}"
            )
        for kind, prompt in self._prompts.items():
            if prompt.kind is not kind:
                raise ValueError(f"prompt key and prompt.kind differ for {kind.value}")

    def prompt_for(self, kind: VoiceReminderKind) -> VoicePrompt:
        return self._prompts[kind]

    def missing_audio_files(self) -> tuple[Path, ...]:
        """List missing files without creating placeholders or claiming success."""
        return tuple(
            prompt.audio_path
            for prompt in self._prompts.values()
            if not prompt.audio_path.is_file()
        )


class VoiceReminderDispatcher:
    """Accept confirmed warning events without blocking sensor/fusion callers.

    Object construction is inert. ``start()`` explicitly creates one worker,
    and only that worker may inspect the route or invoke the playback backend.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        backend: VoicePlaybackBackend | None,
        prompts: VoicePromptCatalog,
        policy: VoiceReminderPolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        on_report: _ReportCallback | None = None,
    ) -> None:
        if enabled and backend is None:
            raise ValueError("an enabled dispatcher requires a playback backend")
        self._enabled = bool(enabled)
        self._backend = backend
        self._prompts = prompts
        self._policy = policy or VoiceReminderPolicy()
        self._monotonic = monotonic
        self._on_report = on_report
        self._condition = threading.Condition()
        self._queue: list[VoiceReminderEvent] = []
        self._last_accepted_at: dict[str, float] = {}
        self._thread: threading.Thread | None = None
        self._started = False
        self._closed = False
        self._stop_requested = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def pending_count(self) -> int:
        with self._condition:
            return len(self._queue)

    def start(self) -> bool:
        """Start the idle worker explicitly; never pair, connect, or play here."""
        with self._condition:
            if self._closed:
                raise RuntimeError("voice reminder dispatcher is closed")
            if not self._enabled:
                return False
            if self._started:
                return True
            self._started = True
            self._thread = threading.Thread(
                target=self._worker,
                name="voice-reminder-player",
                daemon=True,
            )
            self._thread.start()
            return True

    def submit_warning(
        self,
        state: object,
        *,
        occurred_at_monotonic: float | None = None,
        deduplication_key: str | None = None,
    ) -> VoiceSubmitResult:
        """Map only confirmed warning states, enqueue, and return immediately."""
        kind = self._kind_for_state(state)
        if state is WarningState.SAFE:
            return VoiceSubmitResult(
                VoiceSubmitOutcome.SAFE_IGNORED,
                "SAFE has no voice prompt by default",
            )
        if kind is None:
            return VoiceSubmitResult(
                VoiceSubmitOutcome.UNSUPPORTED_STATE,
                "only confirmed WARNING, DANGER, and SENSOR_FAULT are accepted",
            )
        occurred_at = (
            self._monotonic()
            if occurred_at_monotonic is None
            else occurred_at_monotonic
        )
        event = VoiceReminderEvent(
            kind,
            occurred_at,
            kind.value if deduplication_key is None else deduplication_key,
        )
        return self.submit(event)

    def submit(self, event: VoiceReminderEvent) -> VoiceSubmitResult:
        """Apply expiry, deduplication, cooldown, priority and capacity bounds."""
        if not isinstance(event, VoiceReminderEvent):
            raise TypeError("event must be a VoiceReminderEvent")
        now = self._monotonic()
        age = now - event.occurred_at_monotonic
        if age < 0:
            raise ValueError("event timestamp is in the future")
        with self._condition:
            if not self._enabled:
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.DISABLED,
                    "voice reminders are disabled",
                )
            if self._closed:
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.CLOSED,
                    "voice reminder dispatcher is closed",
                )
            if not self._started:
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.NOT_STARTED,
                    "start() must be called explicitly before accepting events",
                )
            if age > self._policy.event_ttl_seconds:
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.EXPIRED,
                    f"event age {age:.3f}s exceeds TTL",
                )
            if any(
                queued.deduplication_key == event.deduplication_key
                for queued in self._queue
            ):
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.DUPLICATE_PENDING,
                    "an event with this deduplication key is already pending",
                )
            last_accepted = self._last_accepted_at.get(event.deduplication_key)
            if (
                last_accepted is not None
                and now - last_accepted < self._policy.cooldown_seconds
            ):
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.COOLDOWN,
                    "event is inside its configured cooldown",
                )

            lower_priority = [
                queued
                for queued in self._queue
                if queued.kind.priority < event.kind.priority
            ]
            for queued in lower_priority:
                self._queue.remove(queued)
                self._last_accepted_at.pop(queued.deduplication_key, None)
            if len(self._queue) >= self._policy.queue_capacity:
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.QUEUE_FULL,
                    "bounded reminder queue is full with equal/higher priority events",
                )
            self._queue.append(event)
            self._last_accepted_at[event.deduplication_key] = now
            self._condition.notify()
            if lower_priority:
                return VoiceSubmitResult(
                    VoiceSubmitOutcome.REPLACED_LOWER_PRIORITY,
                    "higher-priority event replaced pending lower-priority reminders",
                    len(lower_priority),
                )
            return VoiceSubmitResult(
                VoiceSubmitOutcome.ACCEPTED,
                "event accepted by the bounded reminder queue",
            )

    def clear_pending(self, reason: str) -> int:
        """Clear waiting events, for example after an observed disconnect."""
        if not isinstance(reason, str):
            raise TypeError("clear reason must be a string")
        if not reason.strip():
            raise ValueError("clear reason must not be empty")
        with self._condition:
            cleared = tuple(self._queue)
            self._queue.clear()
            for event in cleared:
                self._last_accepted_at.pop(event.deduplication_key, None)
            return len(cleared)

    def close(self) -> bool:
        """Discard pending reminders and wait briefly for the one player thread."""
        with self._condition:
            if self._closed:
                return self._thread is None or not self._thread.is_alive()
            self._closed = True
            self._stop_requested = True
            self._queue.clear()
            self._condition.notify_all()
            thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=self._policy.shutdown_timeout_seconds)
        return not thread.is_alive()

    def _worker(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stop_requested:
                    self._condition.wait()
                if self._stop_requested:
                    return
                expired = self._drop_expired_locked(self._monotonic())
                event = None
                if self._queue:
                    event = max(
                        self._queue,
                        key=lambda item: (
                            item.kind.priority,
                            -item.occurred_at_monotonic,
                        ),
                    )
                    self._queue.remove(event)
            for expired_event in expired:
                self._report(
                    VoiceDeliveryReport(
                        expired_event,
                        VoiceDeliveryOutcome.EXPIRED,
                        "pending event expired before playback",
                    )
                )
            if event is None:
                continue
            self._deliver(event)

    def _drop_expired_locked(self, now: float) -> tuple[VoiceReminderEvent, ...]:
        expired = tuple(
            event
            for event in self._queue
            if now - event.occurred_at_monotonic > self._policy.event_ttl_seconds
        )
        for event in expired:
            self._queue.remove(event)
            self._last_accepted_at.pop(event.deduplication_key, None)
        return expired

    def _deliver(self, event: VoiceReminderEvent) -> None:
        now = self._monotonic()
        if now - event.occurred_at_monotonic > self._policy.event_ttl_seconds:
            with self._condition:
                self._last_accepted_at.pop(event.deduplication_key, None)
            self._report(
                VoiceDeliveryReport(
                    event,
                    VoiceDeliveryOutcome.EXPIRED,
                    "event expired before route inspection",
                )
            )
            return
        prompt = self._prompts.prompt_for(event.kind)
        if not prompt.audio_path.is_file():
            self._report(
                VoiceDeliveryReport(
                    event,
                    VoiceDeliveryOutcome.AUDIO_FILE_MISSING,
                    f"audio file is missing: {prompt.audio_path}",
                )
            )
            return
        backend = self._backend
        if backend is None:
            self._clear_after_disconnect(event, None, "playback backend is unavailable")
            return
        try:
            route = backend.status()
        except Exception as exc:
            self._clear_after_disconnect(event, None, f"route inspection failed: {exc}")
            return
        if not route.ready:
            self._clear_after_disconnect(event, route, route.detail)
            return
        try:
            played_route = backend.play(
                prompt.audio_path,
                timeout_seconds=self._policy.playback_timeout_seconds,
                volume=prompt.playback_volume,
                lead_silence_ms=prompt.lead_silence_ms,
            )
        except VoiceBackendUnavailable as exc:
            self._clear_after_disconnect(event, route, str(exc))
        except (VoicePlaybackFailed, OSError, RuntimeError) as exc:
            self._handle_playback_failure(event, route, exc)
        else:
            self._report(
                VoiceDeliveryReport(
                    event,
                    VoiceDeliveryOutcome.PLAYED,
                    f"played configured recording for {event.kind.value}",
                    played_route,
                )
            )

    def _handle_playback_failure(
        self,
        event: VoiceReminderEvent,
        prior_route: AudioRouteStatus,
        error: BaseException,
    ) -> None:
        """Recheck a failed play so route loss clears every waiting reminder."""
        backend = self._backend
        if backend is not None:
            try:
                current_route = backend.status()
            except Exception:
                current_route = None
            if current_route is None or not current_route.ready:
                self._clear_after_disconnect(
                    event,
                    current_route,
                    f"playback failed and route was lost: {error}",
                )
                return
        self._report(
            VoiceDeliveryReport(
                event,
                VoiceDeliveryOutcome.PLAYBACK_FAILED,
                str(error),
                prior_route,
            )
        )

    def _clear_after_disconnect(
        self,
        event: VoiceReminderEvent,
        route: AudioRouteStatus | None,
        detail: str,
    ) -> None:
        with self._condition:
            cleared = tuple(self._queue)
            self._queue.clear()
            for dropped in (event, *cleared):
                self._last_accepted_at.pop(dropped.deduplication_key, None)
        self._report(
            VoiceDeliveryReport(
                event,
                VoiceDeliveryOutcome.BACKEND_UNAVAILABLE,
                detail,
                route,
            )
        )
        for pending in cleared:
            self._report(
                VoiceDeliveryReport(
                    pending,
                    VoiceDeliveryOutcome.CLEARED_AFTER_DISCONNECT,
                    "pending reminder cleared after Bluetooth/audio route loss",
                    route,
                )
            )

    def _report(self, report: VoiceDeliveryReport) -> None:
        callback = self._on_report
        if callback is None:
            return
        try:
            callback(report)
        except Exception:
            # Diagnostics must never terminate the sole playback worker.
            return

    @staticmethod
    def _kind_for_state(state: object) -> VoiceReminderKind | None:
        if state is WarningState.WARNING:
            return VoiceReminderKind.DISTANCE_ATTENTION
        if state is WarningState.DANGER:
            return VoiceReminderKind.DISTANCE_TOO_CLOSE
        if state is WarningState.SENSOR_FAULT:
            return VoiceReminderKind.SENSOR_FAULT
        return None
