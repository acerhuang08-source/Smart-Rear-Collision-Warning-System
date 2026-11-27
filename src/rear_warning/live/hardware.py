"""Linux ownership checks and lazy construction of existing device layers."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from rear_warning.sensors.bno055.device import RegisterIO
from rear_warning.sensors.bno055.models import BNO055Measurement
from rear_warning.sensors.tfmini_plus.models import TFMiniPlusMeasurement


class DistanceDevice(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read_measurements(self) -> list[TFMiniPlusMeasurement]: ...


class MotionDevice(Protocol):
    def read_identity(self) -> object: ...
    def initialize_ndof(self) -> tuple[int, int, int]: ...
    def read_measurement(self) -> BNO055Measurement: ...
    def close(self) -> None: ...


class DistanceFactory(Protocol):
    def __call__(self, *, port: str, baudrate: int, timeout: float) -> DistanceDevice: ...


class MotionFactory(Protocol):
    def __call__(
        self, register_io: RegisterIO, *, address: int,
        monotonic: Callable[[], float], sleep: Callable[[float], None],
    ) -> MotionDevice: ...


@dataclass(frozen=True)
class HardwareFactories:
    distance: DistanceFactory
    register_io: Callable[[int], RegisterIO]
    motion: MotionFactory


def check_devices_available(port: str, bus: int) -> None:
    """Check device metadata and visible owners without opening either device.

    fuser is read-only, never uses -k, and has a finite subprocess timeout.
    It cannot inspect owners hidden by OS permissions; the operator must also
    ensure no other account owns the sensors. Serial open additionally locks.
    """
    paths = (port, f"/dev/i2c-{bus}")
    for path in paths:
        if not stat.S_ISCHR(os.stat(path).st_mode):
            raise OSError(f"感測器路徑不是字元裝置：{path}")
        if not os.access(path, os.R_OK | os.W_OK):
            raise PermissionError(f"裝置權限不足：{path}；請確認 dialout／i2c 群組")
    executable = shutil.which("fuser")
    if executable is None:
        raise OSError("缺少 fuser，無法檢查既有裝置占用；本次未開啟感測器")
    for path in paths:
        result = subprocess.run(
            [executable, path], capture_output=True, text=True, timeout=2.0,
            check=False,
        )
        if result.returncode == 0:
            raise OSError(f"裝置忙碌：{path}，PID={result.stdout.strip()}；請由操作員釋放")
        if result.returncode != 1 or result.stderr.strip():
            raise OSError(f"無法確認裝置占用：{path}；{result.stderr.strip()}")


def load_hardware_factories() -> HardwareFactories:
    """Called only after both confirmation flag and interactive Enter."""
    import fcntl
    import termios
    import serial

    from rear_warning.sensors.bno055.device import BNO055Device
    from rear_warning.sensors.bno055.hardware import load_smbus_register_io
    from rear_warning.sensors.tfmini_plus.serial_device import TFMiniPlusSerialDevice

    def exclusive_serial(*, port: str, baudrate: int, timeout: float | None) -> serial.Serial:
        # Allocate first so interruption during open also has a cleanup owner.
        connection = serial.Serial(port=None, baudrate=baudrate, timeout=timeout, exclusive=True)
        try:
            connection.port = port
            connection.open()
            fcntl.ioctl(connection.fileno(), termios.TIOCEXCL)
            return connection
        except BaseException:
            connection.close()
            raise

    def distance(*, port: str, baudrate: int, timeout: float) -> DistanceDevice:
        return TFMiniPlusSerialDevice(
            port=port, baudrate=baudrate, timeout=timeout,
            serial_factory=exclusive_serial,
        )

    return HardwareFactories(distance, load_smbus_register_io(), BNO055Device)
