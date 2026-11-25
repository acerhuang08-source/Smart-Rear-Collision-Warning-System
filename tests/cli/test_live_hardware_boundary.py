"""Test live ownership checks and factory cleanup without device access."""

from __future__ import annotations

import fcntl
import stat
import subprocess
from types import SimpleNamespace

import pytest

from rear_warning.live import hardware


@pytest.mark.parametrize("outcome", ["free", "busy", "permission", "unknown", "missing_tool", "timeout"])
def test_preflight_is_read_only_and_fails_closed(outcome: str, monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(hardware.os, "stat", lambda _path: SimpleNamespace(st_mode=stat.S_IFCHR))
    monkeypatch.setattr(hardware.os, "access", lambda *_args: outcome != "permission")
    monkeypatch.setattr(hardware.shutil, "which", lambda _name: None if outcome == "missing_tool" else "/usr/bin/fuser")

    def check(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        assert kwargs["timeout"] == 2.0
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, 2.0)
        return subprocess.CompletedProcess(
            command, 0 if outcome == "busy" else 1,
            "123" if outcome == "busy" else "", "cannot inspect" if outcome == "unknown" else "",
        )

    monkeypatch.setattr(hardware.subprocess, "run", check)
    if outcome == "free":
        hardware.check_devices_available("/dev/ttyAMA0", 1)
        assert len(commands) == 2
    else:
        with pytest.raises((OSError, subprocess.TimeoutExpired)):
            hardware.check_devices_available("/dev/ttyAMA0", 1)
    assert all(command[0] == "/usr/bin/fuser" and "-k" not in command for command in commands)


@pytest.mark.parametrize("fail_ioctl", [False, True])
def test_live_uart_factory_uses_existing_driver_with_exclusive_open(
    fail_ioctl: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial
    from rear_warning.sensors.bno055 import hardware as bno_hardware

    options: list[dict[str, object]] = []

    class Connection:
        def __init__(self, **kwargs: object) -> None:
            options.append(kwargs)
            self.port: str | None = None
            self.is_open = False
            self.closes = 0

        def open(self) -> None:
            self.is_open = True

        def fileno(self) -> int:
            return 123

        def close(self) -> None:
            self.closes += 1
            self.is_open = False

    connection = Connection()
    options.clear()

    def construct(**kwargs: object) -> Connection:
        options.append(kwargs)
        return connection

    def ioctl(fd: int, operation: int) -> None:
        assert fd == 123
        if fail_ioctl:
            raise OSError("exclusive lock failed")

    monkeypatch.setattr(serial, "Serial", construct)
    monkeypatch.setattr(fcntl, "ioctl", ioctl)
    monkeypatch.setattr(bno_hardware, "load_smbus_register_io", lambda: lambda _bus: None)
    factories = hardware.load_hardware_factories()
    device = factories.distance(port="fake", baudrate=115200, timeout=0.02)
    assert not options
    if fail_ioctl:
        with pytest.raises(OSError, match="exclusive lock failed"):
            device.open()
        assert connection.closes == 1
    else:
        device.open()
        device.close()
        assert connection.closes == 1
    assert options == [{"port": None, "baudrate": 115200, "timeout": 0.02, "exclusive": True}]
