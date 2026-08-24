"""Hardware-free tests for the gpiozero digital-output adapter."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rear_warning.outputs import LedPins
from rear_warning.outputs.gpiozero import GpioZeroDigitalOutput


@dataclass
class _FakeDevice:
    pin: int
    on_calls: int = 0
    off_calls: int = 0
    close_calls: int = 0
    off_error: Exception | None = None
    close_error: Exception | None = None

    def on(self) -> None:
        self.on_calls += 1

    def off(self) -> None:
        self.off_calls += 1
        if self.off_error is not None:
            raise self.off_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class _FakePinFactory:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeDeviceFactory:
    def __init__(
        self,
        *,
        fail_on_pin: int | None = None,
        first_device_off_error: Exception | None = None,
    ) -> None:
        self.fail_on_pin = fail_on_pin
        self.first_device_off_error = first_device_off_error
        self.devices: dict[int, _FakeDevice] = {}
        self.calls: list[tuple[int, dict[str, object]]] = []

    def __call__(self, pin: int, **kwargs: object) -> _FakeDevice:
        self.calls.append((pin, kwargs))
        if pin == self.fail_on_pin:
            raise RuntimeError("device creation failed")
        device = _FakeDevice(pin)
        if not self.devices:
            device.off_error = self.first_device_off_error
        self.devices[pin] = device
        return device


def _make_adapter() -> tuple[
    GpioZeroDigitalOutput, _FakeDeviceFactory, _FakePinFactory
]:
    devices = _FakeDeviceFactory()
    pin_factory = _FakePinFactory()
    adapter = GpioZeroDigitalOutput(
        LedPins.raspberry_pi_default(),
        pin_factory=pin_factory,
        device_factory=devices,
    )
    return adapter, devices, pin_factory


def test_devices_are_active_high_initially_off_and_share_factory() -> None:
    adapter, devices, pin_factory = _make_adapter()

    assert [call[0] for call in devices.calls] == [17, 27, 22]
    assert all(
        kwargs == {
            "active_high": True,
            "initial_value": False,
            "pin_factory": pin_factory,
        }
        for _, kwargs in devices.calls
    )
    adapter.close()


def test_write_switches_only_configured_device() -> None:
    adapter, devices, _ = _make_adapter()

    adapter.write(17, True)
    adapter.write(17, False)

    assert devices.devices[17].on_calls == 1
    assert devices.devices[17].off_calls == 1
    with pytest.raises(ValueError, match="not configured"):
        adapter.write(5, True)
    adapter.close()


def test_close_turns_off_closes_all_devices_and_factory_once() -> None:
    adapter, devices, pin_factory = _make_adapter()

    adapter.close()
    adapter.close()

    assert all(device.off_calls == 1 for device in devices.devices.values())
    assert all(device.close_calls == 1 for device in devices.devices.values())
    assert pin_factory.close_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        adapter.write(17, True)


@pytest.mark.parametrize("fail_on_pin", [27, 22])
def test_partial_construction_failure_releases_created_resources(
    fail_on_pin: int,
) -> None:
    devices = _FakeDeviceFactory(fail_on_pin=fail_on_pin)
    pin_factory = _FakePinFactory()

    with pytest.raises(RuntimeError, match="device creation failed"):
        GpioZeroDigitalOutput(
            LedPins.raspberry_pi_default(),
            pin_factory=pin_factory,
            device_factory=devices,
        )

    assert all(device.off_calls == 1 for device in devices.devices.values())
    assert all(device.close_calls == 1 for device in devices.devices.values())
    assert pin_factory.close_calls == 1


def test_construction_error_remains_primary_when_cleanup_also_fails() -> None:
    devices = _FakeDeviceFactory(
        fail_on_pin=27,
        first_device_off_error=RuntimeError("cleanup failed"),
    )
    pin_factory = _FakePinFactory()

    with pytest.raises(RuntimeError, match="device creation failed") as caught:
        GpioZeroDigitalOutput(
            LedPins.raspberry_pi_default(),
            pin_factory=pin_factory,
            device_factory=devices,
        )

    assert devices.devices[17].off_calls == 1
    assert devices.devices[17].close_calls == 1
    assert pin_factory.close_calls == 1
    assert caught.value.__notes__ == [
        "GPIO cleanup also failed after adapter construction failed: "
        "RuntimeError('cleanup failed')"
    ]
