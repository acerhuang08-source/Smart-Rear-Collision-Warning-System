from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from rear_warning.sensors.bno055 import (
    BNO055CleanupError, BNO055Device, BNO055IOError, BNO055IdentityError,
    BNO055ReadinessTimeoutError, BNO055RuntimeStateError, BNO055SystemError,
)
from rear_warning.sensors.bno055.registers import *  # noqa: F403


def _int16_bytes(*values: int) -> list[int]:
    result: list[int] = []
    for value in values:
        unsigned = value & 0xFFFF
        result.extend((unsigned & 0xFF, unsigned >> 8))
    return result


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class NonAdvancingClock:
    def __init__(self, *, backwards: bool = False) -> None:
        self.backwards = backwards
        self.calls = 0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        self.calls += 1
        return float(-self.calls if self.backwards else 0)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class FakeBus:
    def __init__(self) -> None:
        self.bytes = {
            CHIP_ID: EXPECTED_CHIP_ID, ACC_ID: EXPECTED_ACC_ID,
            MAG_ID: EXPECTED_MAG_ID, GYR_ID: EXPECTED_GYR_ID,
            SW_REV_ID_LSB: 0x08, SW_REV_ID_MSB: 0x03, BL_REV_ID: 0x15,
            OPR_MODE: NDOF_MODE, SYS_STATUS: 0x05, SYS_ERR: 0,
            CALIB_STAT: 0b10011100, TEMPERATURE: 0xFE,
        }
        self.blocks = {(MEASUREMENT_DATA_START, MEASUREMENT_DATA_LENGTH): (
            _int16_bytes(16, -32, 48)
            + _int16_bytes(16384, -8192, 4096, -2048)
            + _int16_bytes(100, -200, 300)
            + _int16_bytes(-100, 200, -300)
        )}
        self.byte_sequences: dict[int, list[int]] = {}
        self.events: list[tuple[object, ...]] = []
        self.writes: list[tuple[int, int, int]] = []
        self.block_reads: list[tuple[int, int, int]] = []
        self.close_calls = 0
        self.read_error: Exception | None = None
        self.write_error: Exception | None = None
        self.close_error: Exception | None = None

    def read_byte_data(self, address: int, register: int) -> int:
        self.events.append(("byte", address, register))
        if self.read_error: raise self.read_error
        sequence = self.byte_sequences.get(register)
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return self.bytes[register]

    def read_i2c_block_data(self, address: int, register: int, length: int) -> list[int]:
        if self.read_error: raise self.read_error
        self.block_reads.append((address, register, length))
        self.events.append(("block", address, register, length))
        return list(self.blocks[(register, length)])

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        if self.write_error: raise self.write_error
        self.writes.append((address, register, value))

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error: raise self.close_error


@pytest.mark.parametrize("address", [0x28, 0x29])
def test_identity_accepts_both_addresses(address: int) -> None:
    identity = BNO055Device(FakeBus(), address=address).read_identity()
    assert identity.address == address
    assert identity.software_revision == 0x0308
    assert identity.bootloader_revision == 0x15


@pytest.mark.parametrize("address", [0x00, 0x27, 0x2A, True])
def test_invalid_address_rejected(address: object) -> None:
    with pytest.raises(ValueError):
        BNO055Device(FakeBus(), address=address)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("register", "label"),
    [(CHIP_ID, "chip"), (ACC_ID, "accelerometer"),
     (MAG_ID, "magnetometer"), (GYR_ID, "gyroscope")],
)
def test_each_identity_mismatch_is_rejected(register: int, label: str) -> None:
    bus = FakeBus(); bus.bytes[register] ^= 1
    with pytest.raises(BNO055IdentityError, match=label):
        BNO055Device(bus).read_identity()


def test_ndof_initialization_write_order_and_delays() -> None:
    bus = FakeBus(); sleeps: list[float] = []
    result = BNO055Device(bus, sleep=sleeps.append).initialize_ndof()
    assert bus.writes == [
        (0x29, OPR_MODE, CONFIG_MODE), (0x29, PAGE_ID, 0),
        (0x29, PWR_MODE, NORMAL_POWER_MODE), (0x29, UNIT_SEL, DEFAULT_UNITS),
        (0x29, OPR_MODE, NDOF_MODE),
    ]
    assert sleeps == [0.019, 0.007]
    assert result == (NDOF_MODE, 0x05, 0)


def test_mode_and_status_sequences_eventually_become_ready() -> None:
    bus = FakeBus(); clock = FakeClock()
    bus.byte_sequences[OPR_MODE] = [CONFIG_MODE, NDOF_MODE, NDOF_MODE]
    bus.byte_sequences[SYS_STATUS] = [0x02, 0x03, 0x05]
    bus.byte_sequences[SYS_ERR] = [0, 0, 0]
    result = BNO055Device(
        bus, sleep=clock.sleep, monotonic=clock.monotonic,
        readiness_timeout_seconds=0.1, readiness_poll_interval_seconds=0.02,
    ).initialize_ndof()
    assert result == (NDOF_MODE, 0x05, 0)
    assert clock.sleeps == [0.019, 0.007, 0.02, 0.02]


def test_readiness_timeout_contains_last_state() -> None:
    bus = FakeBus(); clock = FakeClock()
    bus.bytes[OPR_MODE] = CONFIG_MODE; bus.bytes[SYS_STATUS] = 0x03
    device = BNO055Device(
        bus, sleep=clock.sleep, monotonic=clock.monotonic,
        readiness_timeout_seconds=0.05, readiness_poll_interval_seconds=0.02,
    )
    with pytest.raises(BNO055ReadinessTimeoutError) as caught:
        device.initialize_ndof()
    message = str(caught.value)
    assert "0.050s" in message and "mode=0x00" in message
    assert "status=0x03" in message and "error=0x00" in message
    assert clock.now == pytest.approx(0.076)


@pytest.mark.parametrize("backwards", [False, True])
def test_poll_count_bounds_stalled_or_backwards_clock(backwards: bool) -> None:
    bus = FakeBus(); clock = NonAdvancingClock(backwards=backwards)
    bus.bytes[OPR_MODE] = CONFIG_MODE; bus.bytes[SYS_STATUS] = 0x03
    timeout = 0.05; interval = 0.02
    maximum_polls = math.ceil(timeout / interval) + 1
    device = BNO055Device(
        bus, sleep=clock.sleep, monotonic=clock.monotonic,
        readiness_timeout_seconds=timeout,
        readiness_poll_interval_seconds=interval,
    )
    with pytest.raises(BNO055ReadinessTimeoutError):
        device.initialize_ndof()
    assert sum(event == ("byte", 0x29, OPR_MODE) for event in bus.events) == maximum_polls
    assert sum(event == ("byte", 0x29, SYS_STATUS) for event in bus.events) == maximum_polls
    assert sum(event == ("byte", 0x29, SYS_ERR) for event in bus.events) == maximum_polls
    assert len(clock.sleeps) == 2 + maximum_polls - 1


def test_nonzero_system_error_after_ndof() -> None:
    bus = FakeBus(); bus.bytes[SYS_ERR] = 0x09
    with pytest.raises(BNO055SystemError, match="0x09"):
        BNO055Device(bus, sleep=lambda _: None).initialize_ndof()


def test_system_error_status_fails_even_with_zero_error_code() -> None:
    bus = FakeBus(); bus.bytes[SYS_STATUS] = 0x01
    with pytest.raises(BNO055SystemError, match="status=0x01"):
        BNO055Device(bus, sleep=lambda _: None).initialize_ndof()


@pytest.mark.parametrize(
    "name", ["readiness_timeout_seconds", "readiness_poll_interval_seconds"]
)
@pytest.mark.parametrize(
    "value", [True, "1", 0, -1, float("nan"), float("inf"), float("-inf")]
)
def test_readiness_settings_require_positive_finite_numbers(
    name: str, value: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        BNO055Device(FakeBus(), **{name: value})  # type: ignore[arg-type]


def test_default_readiness_poll_limit_is_explicitly_finite() -> None:
    device = BNO055Device(FakeBus())
    assert device._maximum_readiness_polls == 101


def test_measurement_batches_and_scales_all_fields() -> None:
    bus = FakeBus()
    measurement = BNO055Device(bus, monotonic=lambda: 12.5).read_measurement()
    assert measurement.monotonic_timestamp == 12.5
    assert (measurement.euler.heading, measurement.euler.roll, measurement.euler.pitch) == pytest.approx((1, -2, 3))
    assert (measurement.quaternion.w, measurement.quaternion.x, measurement.quaternion.y, measurement.quaternion.z) == pytest.approx((1, -0.5, 0.25, -0.125))
    assert (measurement.linear_acceleration.x, measurement.linear_acceleration.y, measurement.linear_acceleration.z) == pytest.approx((1, -2, 3))
    assert (measurement.gravity.x, measurement.gravity.y, measurement.gravity.z) == pytest.approx((-1, 2, -3))
    assert measurement.temperature_c == -2
    assert measurement.calibration.system == 2
    assert measurement.operation_mode == NDOF_MODE
    assert measurement.system_status == 5 and measurement.system_error == 0
    assert bus.block_reads == [(0x29, MEASUREMENT_DATA_START, 26)]
    assert bus.events == [
        ("block", 0x29, MEASUREMENT_DATA_START, 26),
        ("byte", 0x29, TEMPERATURE),
        ("byte", 0x29, CALIB_STAT),
        ("byte", 0x29, OPR_MODE),
        ("byte", 0x29, SYS_STATUS),
        ("byte", 0x29, SYS_ERR),
    ]


def test_short_block_read_is_error() -> None:
    bus = FakeBus(); bus.blocks[(MEASUREMENT_DATA_START, 26)] = [0] * 25
    with pytest.raises(BNO055IOError, match="short block"):
        BNO055Device(bus).read_measurement()


@pytest.mark.parametrize(
    ("mode", "status", "error"),
    [(NDOF_MODE, 0x04, 0), (NDOF_MODE, 0x05, 0x07), (CONFIG_MODE, 0x05, 0)],
)
def test_measurement_rejects_invalid_runtime_state(
    mode: int, status: int, error: int
) -> None:
    bus = FakeBus(); bus.bytes[OPR_MODE] = mode
    bus.bytes[SYS_STATUS] = status; bus.bytes[SYS_ERR] = error
    with pytest.raises(BNO055RuntimeStateError) as caught:
        BNO055Device(bus).read_measurement()
    assert caught.value.system_status == status
    assert caught.value.system_error == error
    assert caught.value.operation_mode == mode
    assert bus.events == [
        ("block", 0x29, MEASUREMENT_DATA_START, 26),
        ("byte", 0x29, TEMPERATURE),
        ("byte", 0x29, CALIB_STAT),
        ("byte", 0x29, OPR_MODE),
        ("byte", 0x29, SYS_STATUS),
        ("byte", 0x29, SYS_ERR),
    ]


@pytest.mark.parametrize("operation", ["read", "write"])
def test_bus_exceptions_are_wrapped(operation: str) -> None:
    bus = FakeBus()
    if operation == "read":
        bus.read_error = OSError("I2C failed")
        action: Callable[[], object] = BNO055Device(bus).read_identity
    else:
        bus.write_error = OSError("I2C failed")
        action = lambda: BNO055Device(bus).initialize_ndof()
    with pytest.raises(BNO055IOError): action()


def test_cleanup_returns_to_config_and_closes() -> None:
    bus = FakeBus(); sleeps: list[float] = []; device = BNO055Device(bus, sleep=sleeps.append)
    device.initialize_ndof(); bus.writes.clear(); sleeps.clear()
    device.close(); device.close()
    assert bus.writes == [(0x29, OPR_MODE, CONFIG_MODE)]
    assert sleeps == [0.019] and bus.close_calls == 1


@pytest.mark.parametrize(("write_fails", "close_fails"), [(True, False), (False, True), (True, True)])
def test_cleanup_failure_is_reported_and_close_attempted(write_fails: bool, close_fails: bool) -> None:
    bus = FakeBus()
    device = BNO055Device(bus, sleep=lambda _: None)
    device.initialize_ndof()
    if write_fails: bus.write_error = OSError("write")
    if close_fails: bus.close_error = OSError("close")
    with pytest.raises(BNO055CleanupError): device.close()
    assert bus.close_calls == 1


def test_identity_failure_cleanup_only_closes_bus() -> None:
    bus = FakeBus(); bus.bytes[CHIP_ID] = 0
    device = BNO055Device(bus, sleep=lambda _: None)
    with pytest.raises(BNO055IdentityError): device.read_identity()
    device.close()
    assert bus.writes == [] and bus.close_calls == 1


def test_devices_do_not_share_state() -> None:
    first, second = FakeBus(), FakeBus()
    one, two = BNO055Device(first), BNO055Device(second, address=0x28)
    one.close()
    assert first.close_calls == 1 and second.close_calls == 0
    two.read_identity()
