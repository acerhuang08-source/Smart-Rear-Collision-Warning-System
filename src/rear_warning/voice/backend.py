"""Explicit-target PipeWire playback backend for one Bluetooth headset."""

from __future__ import annotations

import json
import math
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol

from .models import AudioRouteStatus


_MAC_PATTERN = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_FAIL_CLOSED_STREAM_PROPERTIES = json.dumps(
    {
        "node.dont-fallback": True,
        "node.dont-reconnect": True,
        "node.dont-move": True,
    },
    separators=(",", ":"),
)


class VoiceBackendError(RuntimeError):
    """Base error for route discovery and playback failures."""


class VoiceBackendUnavailable(VoiceBackendError):
    """Raised when the selected MOTO device or its audio sink is unavailable."""


class VoicePlaybackFailed(VoiceBackendError):
    """Raised when explicit-target playback fails or exceeds its time limit."""


class VoicePlaybackBackend(Protocol):
    """Backend boundary owned by the single reminder worker."""

    def status(self) -> AudioRouteStatus:
        """Return connection and route states without playing audio."""
        ...

    def play(self, audio_path: Path, *, timeout_seconds: float) -> AudioRouteStatus:
        """Play one existing recording only to the configured target."""
        ...


_CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class PipeWireBluetoothBackend:
    """Use BlueZ for identity and ``pw-play --target`` for fail-closed routing.

    Construction performs no command, Bluetooth operation, connection, or
    playback. Every playback rechecks both BlueZ and PipeWire state.
    """

    def __init__(
        self,
        *,
        device_address: str,
        expected_name: str,
        target_node: str,
        bluetoothctl_command: str = "/usr/bin/bluetoothctl",
        pw_dump_command: str = "/usr/bin/pw-dump",
        pw_play_command: str = "/usr/bin/pw-play",
        status_timeout_seconds: float = 3.0,
        environment: Mapping[str, str] | None = None,
        command_runner: _CommandRunner = subprocess.run,
    ) -> None:
        if not isinstance(device_address, str):
            raise TypeError("device_address must be a string")
        if not _MAC_PATTERN.fullmatch(device_address):
            raise ValueError("device_address must be a complete Bluetooth MAC address")
        if not isinstance(expected_name, str):
            raise TypeError("expected_name must be a string")
        if not expected_name.strip():
            raise ValueError("expected_name must not be empty")
        if not isinstance(target_node, str):
            raise TypeError("target_node must be a string")
        if not target_node.strip() or target_node == "auto":
            raise ValueError("target_node must explicitly identify the MOTO Audio/Sink")
        if (
            isinstance(status_timeout_seconds, bool)
            or not isinstance(status_timeout_seconds, (int, float))
            or not math.isfinite(float(status_timeout_seconds))
            or status_timeout_seconds <= 0
        ):
            raise ValueError("status_timeout_seconds must be greater than zero")
        self._address = device_address.upper()
        self._expected_name = expected_name.strip()
        self._target_node = target_node.strip()
        self._bluetoothctl = bluetoothctl_command
        self._pw_dump = pw_dump_command
        self._pw_play = pw_play_command
        self._status_timeout = float(status_timeout_seconds)
        self._environment = None if environment is None else dict(environment)
        self._run = command_runner

    @property
    def device_address(self) -> str:
        return self._address

    @property
    def target_node(self) -> str:
        return self._target_node

    def status(self) -> AudioRouteStatus:
        """Inspect only the configured address and configured PipeWire node."""
        paired, connected, reported_name, bluez_detail = self._bluez_status()
        name_matches = (
            reported_name is not None
            and reported_name.casefold() == self._expected_name.casefold()
        )
        if not paired or not connected or not name_matches:
            reasons = [bluez_detail]
            if reported_name is not None and not name_matches:
                reasons.append(
                    f"name mismatch: expected {self._expected_name!r}, "
                    f"reported {reported_name!r}"
                )
            return AudioRouteStatus(
                self._address,
                self._expected_name,
                reported_name,
                paired,
                connected,
                name_matches,
                False,
                self._target_node,
                "; ".join(reasons),
            )

        audio_available, audio_detail = self._pipewire_status()
        return AudioRouteStatus(
            self._address,
            self._expected_name,
            reported_name,
            paired,
            connected,
            name_matches,
            audio_available,
            self._target_node,
            f"{bluez_detail}; {audio_detail}",
        )

    def play(self, audio_path: Path, *, timeout_seconds: float) -> AudioRouteStatus:
        """Fail closed instead of falling back to the default HDMI/analog sink."""
        if not isinstance(audio_path, Path):
            raise TypeError("audio_path must be a pathlib.Path")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        if not audio_path.is_file():
            raise VoicePlaybackFailed(f"audio file is missing: {audio_path}")
        route = self.status()
        if not route.ready:
            raise VoiceBackendUnavailable(route.detail)
        try:
            result = self._run(
                [
                    self._pw_play,
                    f"--target={self._target_node}",
                    f"--properties={_FAIL_CLOSED_STREAM_PROPERTIES}",
                    "--media-category=Playback",
                    "--media-role=Notification",
                    str(audio_path),
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                env=self._environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise VoicePlaybackFailed(f"pw-play could not run: {exc}") from exc
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            raise VoicePlaybackFailed(f"pw-play failed: {detail}")
        return route

    def _bluez_status(self) -> tuple[bool, bool, str | None, str]:
        try:
            result = self._run(
                [self._bluetoothctl, "info", self._address],
                capture_output=True,
                text=True,
                timeout=self._status_timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, False, None, f"BlueZ status unavailable: {exc}"
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            return False, False, None, f"BlueZ device unavailable: {detail}"
        values = self._parse_bluetoothctl_info(result.stdout)
        reported_name = values.get("Name") or values.get("Alias")
        paired = values.get("Paired", "no").casefold() == "yes"
        connected = values.get("Connected", "no").casefold() == "yes"
        return paired, connected, reported_name, (
            f"BlueZ paired={str(paired).lower()}, connected={str(connected).lower()}"
        )

    def _pipewire_status(self) -> tuple[bool, str]:
        try:
            result = self._run(
                [self._pw_dump],
                capture_output=True,
                text=True,
                timeout=self._status_timeout,
                check=False,
                env=self._environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"PipeWire status unavailable: {exc}"
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or result.returncode
            return False, f"pw-dump failed: {detail}"
        try:
            objects = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            return False, f"pw-dump returned invalid JSON: {exc}"
        if not isinstance(objects, list):
            return False, "pw-dump root is not an object list"
        matches = [item for item in objects if self._matches_target(item)]
        if len(matches) != 1:
            return False, (
                f"configured MOTO Audio/Sink target matched {len(matches)} nodes: "
                f"{self._target_node}"
            )
        return True, f"PipeWire Audio/Sink target available: {self._target_node}"

    def _matches_target(self, item: object) -> bool:
        if not isinstance(item, dict):
            return False
        info = item.get("info")
        if not isinstance(info, dict):
            return False
        props = info.get("props")
        if not isinstance(props, dict):
            return False
        node_name = str(props.get("node.name", ""))
        object_serial = str(props.get("object.serial", ""))
        if self._target_node not in {node_name, object_serial}:
            return False
        if props.get("media.class") != "Audio/Sink":
            return False
        if props.get("device.api") != "bluez5" and not node_name.startswith(
            "bluez_output."
        ):
            return False
        address_properties = {
            str(props.get("api.bluez5.address", "")).upper(),
            str(props.get("bluez5.address", "")).upper(),
        }
        address_token = self._address.replace(":", "_")
        return self._address in address_properties or address_token in node_name.upper()

    @staticmethod
    def _parse_bluetoothctl_info(output: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.strip()
            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            values[key.strip()] = value.strip()
        return values
