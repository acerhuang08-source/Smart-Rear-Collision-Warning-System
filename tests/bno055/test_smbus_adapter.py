from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

import pytest


class FakeSMBus:
    instances: list[FakeSMBus] = []
    constructor_error: Exception | None = None

    def __init__(self, bus: int) -> None:
        if self.constructor_error is not None:
            raise self.constructor_error
        self.bus = bus
        self.calls: list[tuple[object, ...]] = []
        self.read_byte_result: object = 0xA0
        self.block_result: object = (1, 2, 3)
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.close_error: Exception | None = None
        self.__class__.instances.append(self)

    def read_byte_data(self, address: int, register: int) -> object:
        self.calls.append(("read_byte", address, register))
        if self.read_error: raise self.read_error
        return self.read_byte_result

    def read_i2c_block_data(
        self, address: int, register: int, length: int
    ) -> object:
        self.calls.append(("read_block", address, register, length))
        if self.read_error: raise self.read_error
        return self.block_result

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        self.calls.append(("write", address, register, value))
        if self.write_error: raise self.write_error

    def close(self) -> None:
        self.calls.append(("close",))
        if self.close_error: raise self.close_error


@pytest.fixture
def adapter_module(monkeypatch: pytest.MonkeyPatch) -> Any:
    FakeSMBus.instances.clear()
    FakeSMBus.constructor_error = None
    fake_module = ModuleType("smbus2")
    fake_module.SMBus = FakeSMBus  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "smbus2", fake_module)
    sys.modules.pop("rear_warning.sensors.bno055.smbus", None)
    module = importlib.import_module("rear_warning.sensors.bno055.smbus")
    yield module
    sys.modules.pop("rear_warning.sensors.bno055.smbus", None)


def test_adapter_constructs_bus_lazily_and_forwards_every_operation(
    adapter_module: Any,
) -> None:
    assert FakeSMBus.instances == []
    adapter = adapter_module.SMBusRegisterIO(1)
    bus = FakeSMBus.instances[0]
    assert bus.bus == 1
    block = tuple(range(26))
    bus.block_result = block
    assert adapter.read_byte_data(0x29, 0x00) == 0xA0
    assert adapter.read_i2c_block_data(0x29, 0x1A, 26) == list(block)
    adapter.write_byte_data(0x29, 0x3D, 0x0C)
    adapter.close(); adapter.close()
    assert bus.calls == [
        ("read_byte", 0x29, 0x00),
        ("read_block", 0x29, 0x1A, 26),
        ("write", 0x29, 0x3D, 0x0C),
        ("close",),
    ]


def test_adapter_converts_native_return_types(adapter_module: Any) -> None:
    adapter = adapter_module.SMBusRegisterIO(1)
    bus = FakeSMBus.instances[0]
    bus.read_byte_result = "160"
    bus.block_result = (4, 5)
    assert adapter.read_byte_data(0x29, 0) == 160
    assert adapter.read_i2c_block_data(0x29, 0x1A, 2) == [4, 5]


def test_constructor_failure_does_not_return_adapter(adapter_module: Any) -> None:
    FakeSMBus.constructor_error = OSError("constructor failed")
    with pytest.raises(OSError, match="constructor failed"):
        adapter_module.SMBusRegisterIO(1)
    assert FakeSMBus.instances == []


@pytest.mark.parametrize("operation", ["read", "write", "close"])
def test_adapter_does_not_swallow_bus_exceptions(
    operation: str,
    adapter_module: Any,
) -> None:
    adapter = adapter_module.SMBusRegisterIO(1)
    bus = FakeSMBus.instances[0]
    error = OSError(f"{operation} failed")
    if operation == "read":
        bus.read_error = error
        action = lambda: adapter.read_byte_data(0x29, 0)
    elif operation == "write":
        bus.write_error = error
        action = lambda: adapter.write_byte_data(0x29, 0x3D, 0)
    else:
        bus.close_error = error
        action = adapter.close
    with pytest.raises(OSError, match=f"{operation} failed"):
        action()


def test_operations_after_successful_close_are_rejected(
    adapter_module: Any,
) -> None:
    adapter = adapter_module.SMBusRegisterIO(1)
    adapter.close()
    with pytest.raises(RuntimeError, match="closed"):
        adapter.read_byte_data(0x29, 0)
