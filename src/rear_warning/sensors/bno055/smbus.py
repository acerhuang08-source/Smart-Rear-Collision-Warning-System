"""smbus2-backed register adapter; import only through ``hardware``."""

from __future__ import annotations

from smbus2 import SMBus


class SMBusRegisterIO:
    def __init__(self, bus: int) -> None:
        if isinstance(bus, bool) or not isinstance(bus, int) or bus < 0:
            raise ValueError("bus must be a non-negative integer")
        self._bus = SMBus(bus)
        self._closed = False

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("SMBus register I/O is closed")

    def read_byte_data(self, address: int, register: int) -> int:
        self._require_open()
        return int(self._bus.read_byte_data(address, register))

    def read_i2c_block_data(self, address: int, register: int, length: int) -> list[int]:
        self._require_open()
        return list(self._bus.read_i2c_block_data(address, register, length))

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        self._require_open()
        self._bus.write_byte_data(address, register, value)

    def close(self) -> None:
        if self._closed:
            return
        self._bus.close()
        self._closed = True
