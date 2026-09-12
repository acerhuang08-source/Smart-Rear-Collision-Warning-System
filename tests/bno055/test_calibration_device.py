from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from rear_warning.sensors.bno055 import (
    BNO055CalibrationData,
    BNO055CalibrationProfile,
    BNO055CalibrationTimeoutError,
    BNO055Device,
    BNO055IOError,
    BNO055IdentityError,
    BNO055MeasurementQualityTimeoutError,
    BNO055ProfileCompatibilityError,
    BNO055ProfileReadbackError,
    BNO055ReadinessTimeoutError,
)
from rear_warning.sensors.bno055.registers import *  # noqa: F403


SYS_TRIGGER_REGISTER = 0x3F
ACC_CONFIG_PAGE1_REGISTER = 0x08
PROFILE_REGISTERS = set(range(0x55, 0x6B))


def int16_bytes(*values: int) -> list[int]:
    result: list[int] = []
    for value in values:
        unsigned = value & 0xFFFF
        result.extend((unsigned & 0xFF, unsigned >> 8))
    return result


PROFILE_BYTES = int16_bytes(1, -2, 3, -4, 5, -6, 7, -8, 9, 1000, 480)
MEASUREMENT_BYTES = (
    int16_bytes(0, 0, 0)
    + int16_bytes(16384, 0, 0, 0)
    + int16_bytes(0, 0, 0)
    + int16_bytes(0, 0, 981)
)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ConstantClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    @staticmethod
    def monotonic() -> float:
        return 10.0

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class BackwardClock:
    def __init__(self) -> None:
        self.value = 0.0
        self.readings: list[float] = []
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        result = self.value
        self.value -= 1.0
        self.readings.append(result)
        return result

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


class FakeBus:
    def __init__(self) -> None:
        self.bytes = {
            CHIP_ID: EXPECTED_CHIP_ID,
            ACC_ID: EXPECTED_ACC_ID,
            MAG_ID: EXPECTED_MAG_ID,
            GYR_ID: EXPECTED_GYR_ID,
            SW_REV_ID_LSB: 0x08,
            SW_REV_ID_MSB: 0x03,
            BL_REV_ID: 0x15,
            OPR_MODE: NDOF_MODE,
            SYS_STATUS: 0x05,
            SYS_ERR: 0,
            CALIB_STAT: 0xFF,
            TEMPERATURE: 25,
            UNIT_SEL: DEFAULT_UNITS,
            PWR_MODE: NORMAL_POWER_MODE,
            AXIS_MAP_CONFIG: 0x24,
            AXIS_MAP_SIGN: 0,
        }
        self.profile_bytes = list(PROFILE_BYTES)
        self.measurement_bytes = list(MEASUREMENT_BYTES)
        self.byte_sequences: dict[int, list[int]] = {}
        self.events: list[tuple[object, ...]] = []
        self.writes: list[tuple[int, int, int]] = []
        self.fail_write_register: int | None = None
        self.profile_readback_mismatch: int | None = None
        self.profile_block_override: Sequence[object] | None = None
        self.fail_block_register: int | None = None
        self.close_error: Exception | None = None
        self.close_calls = 0

    def read_byte_data(self, address: int, register: int) -> int:
        self.events.append(("read", register))
        sequence = self.byte_sequences.get(register)
        if sequence:
            return sequence.pop(0) if len(sequence) > 1 else sequence[0]
        return self.bytes[register]

    def read_i2c_block_data(self, address: int, register: int, length: int) -> list[int]:
        self.events.append(("block", register, length))
        if register == self.fail_block_register:
            raise OSError("programmed block read failure")
        if register == CALIBRATION_PROFILE_START:
            if self.profile_block_override is not None:
                return list(self.profile_block_override)  # type: ignore[list-item]
            result = list(self.profile_bytes)
            if self.profile_readback_mismatch is not None:
                result[self.profile_readback_mismatch] ^= 0x01
            return result
        if register == MEASUREMENT_DATA_START:
            return list(self.measurement_bytes)
        raise KeyError(register)

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        self.events.append(("write", register, value))
        if register == self.fail_write_register:
            raise OSError("programmed write failure")
        self.writes.append((address, register, value))
        self.bytes[register] = value
        if CALIBRATION_PROFILE_START <= register <= CALIBRATION_PROFILE_END:
            self.profile_bytes[register - CALIBRATION_PROFILE_START] = value

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error:
            raise self.close_error


def profile(**changes: object) -> BNO055CalibrationProfile:
    values: dict[str, object] = {
        "sensor_label": "rear-imu-primary",
        "chip_id": EXPECTED_CHIP_ID,
        "accelerometer_id": EXPECTED_ACC_ID,
        "magnetometer_id": EXPECTED_MAG_ID,
        "gyroscope_id": EXPECTED_GYR_ID,
        "software_revision": 0x0308,
        "bootloader_revision": 0x15,
        "unit_sel": DEFAULT_UNITS,
        "power_mode": NORMAL_POWER_MODE,
        "operation_mode": NDOF_MODE,
        "axis_map_config": 0x24,
        "axis_map_sign": 0,
        "calibration": BNO055CalibrationData(1, -2, 3, -4, 5, -6, 7, -8, 9, 1000, 480),
        "created_at_utc": "2026-09-08T12:34:56.000000Z",
    }
    values.update(changes)
    return BNO055CalibrationProfile(**values)  # type: ignore[arg-type]


def device(
    bus: FakeBus,
    clock: Clock | ConstantClock | BackwardClock | None = None,
    **kwargs: float,
) -> BNO055Device:
    selected = clock or Clock()
    return BNO055Device(
        bus,
        sleep=selected.sleep,
        monotonic=selected.monotonic,
        **kwargs,
    )


def test_export_immediate_full_calibration_reads_exact_profile_range() -> None:
    bus = FakeBus()
    result = device(bus).capture_calibration_profile(
        sensor_label="rear-imu-primary",
        created_at_utc="2026-09-08T12:34:56.000000Z",
        calibration_timeout_seconds=1,
        calibration_poll_interval_seconds=0.1,
    )
    assert result == profile()
    assert bus.writes == [
        (0x29, OPR_MODE, CONFIG_MODE),
        (0x29, PAGE_ID, 0),
        (0x29, PWR_MODE, NORMAL_POWER_MODE),
        (0x29, UNIT_SEL, DEFAULT_UNITS),
        (0x29, OPR_MODE, NDOF_MODE),
        (0x29, OPR_MODE, CONFIG_MODE),
        (0x29, PAGE_ID, 0),
    ]
    assert ("block", CALIBRATION_PROFILE_START, 22) in bus.events


def test_export_waits_until_full_calibration() -> None:
    bus = FakeBus()
    bus.byte_sequences[CALIB_STAT] = [0x3F, 0x7F, 0xFF]
    clock = Clock()
    device(bus, clock).capture_calibration_profile(
        sensor_label="rear-imu-primary",
        created_at_utc="2026-09-08T12:34:56.000000Z",
        calibration_timeout_seconds=1,
        calibration_poll_interval_seconds=0.1,
    )
    assert clock.sleeps == [0.019, 0.007, 0.1, 0.1, 0.019]


def test_calibration_polling_reads_only_runtime_and_calibration_registers() -> None:
    bus = FakeBus()
    bus.byte_sequences[CALIB_STAT] = [0x3F, 0x7F, 0xFF]

    device(bus).capture_calibration_profile(
        sensor_label="rear-imu-primary",
        created_at_utc="2026-09-08T12:34:56.000000Z",
        calibration_timeout_seconds=1,
        calibration_poll_interval_seconds=0.1,
    )

    ndof_write = bus.events.index(("write", OPR_MODE, NDOF_MODE))
    cleanup_write = bus.events.index(("write", OPR_MODE, CONFIG_MODE), ndof_write + 1)
    polling_reads = {
        event[1]
        for event in bus.events[ndof_write + 1:cleanup_write]
        if event[0] == "read"
    }
    assert polling_reads <= {OPR_MODE, SYS_STATUS, SYS_ERR, CALIB_STAT}
    assert CALIB_STAT in polling_reads
    assert SYS_TRIGGER_REGISTER not in polling_reads
    assert ACC_CONFIG_PAGE1_REGISTER not in polling_reads
    assert not PROFILE_REGISTERS & polling_reads


def test_export_legal_exception_reads_profile_registers_only_after_full_calibration() -> None:
    bus = FakeBus()

    device(bus).capture_calibration_profile(
        sensor_label="rear-imu-primary",
        created_at_utc="2026-09-08T12:34:56.000000Z",
        calibration_timeout_seconds=1,
        calibration_poll_interval_seconds=0.1,
    )

    profile_blocks = [
        event for event in bus.events
        if event == ("block", CALIBRATION_PROFILE_START, CALIBRATION_PROFILE_LENGTH)
    ]
    assert len(profile_blocks) == 1
    profile_read_index = bus.events.index(profile_blocks[0])
    full_calibration_read_index = max(
        index
        for index, event in enumerate(bus.events[:profile_read_index])
        if event == ("read", CALIB_STAT)
    )
    assert full_calibration_read_index < profile_read_index
    assert not any(
        event[0] == "read"
        and event[1] in {SYS_TRIGGER_REGISTER, ACC_CONFIG_PAGE1_REGISTER}
        for event in bus.events
    )


@pytest.mark.parametrize("status", range(0xFF))
def test_every_non_ff_calibration_status_is_not_exportable(status: int) -> None:
    bus = FakeBus()
    bus.bytes[CALIB_STAT] = status
    with pytest.raises(BNO055CalibrationTimeoutError, match=f"0x{status:02x}"):
        device(bus, ConstantClock()).capture_calibration_profile(
            sensor_label="rear-imu-primary",
            created_at_utc="2026-09-08T12:34:56.000000Z",
            calibration_timeout_seconds=0.01,
            calibration_poll_interval_seconds=0.02,
        )
    assert not any(event[0] == "block" and event[1] == CALIBRATION_PROFILE_START for event in bus.events)


def test_calibration_polling_is_bounded_with_constant_clock() -> None:
    bus = FakeBus()
    bus.bytes[CALIB_STAT] = 0
    clock = ConstantClock()
    timeout = 0.05
    interval = 0.02
    with pytest.raises(BNO055CalibrationTimeoutError):
        device(bus, clock).capture_calibration_profile(
            sensor_label="rear-imu-primary",
            created_at_utc="2026-09-08T12:34:56.000000Z",
            calibration_timeout_seconds=timeout,
            calibration_poll_interval_seconds=interval,
        )
    reads = [event for event in bus.events if event == ("read", CALIB_STAT)]
    assert len(reads) == math.ceil(timeout / interval) + 1


def test_calibration_polling_is_bounded_with_strictly_backward_clock() -> None:
    bus = FakeBus()
    bus.bytes[CALIB_STAT] = 0x3F
    clock = BackwardClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert second < first
    timeout = 0.05
    interval = 0.02
    maximum_attempts = math.ceil(timeout / interval) + 1
    with pytest.raises(BNO055CalibrationTimeoutError, match="CALIB_STAT=0x3f"):
        device(bus, clock).capture_calibration_profile(
            sensor_label="rear-imu-primary",
            created_at_utc="2026-09-08T12:34:56.000000Z",
            calibration_timeout_seconds=timeout,
            calibration_poll_interval_seconds=interval,
        )
    reads = [event for event in bus.events if event == ("read", CALIB_STAT)]
    assert len(reads) == maximum_attempts
    assert all(
        later < earlier
        for earlier, later in zip(clock.readings, clock.readings[1:])
    )


@pytest.mark.parametrize("override", [[0] * 21, [0] * 23, [0] * 21 + [True]])
def test_export_rejects_short_long_or_invalid_profile_block(override: list[object]) -> None:
    bus = FakeBus()
    bus.profile_block_override = override
    sensor = device(bus)
    with pytest.raises((BNO055IOError, ValueError)):
        try:
            sensor.capture_calibration_profile(
                sensor_label="rear-imu-primary",
                created_at_utc="2026-09-08T12:34:56.000000Z",
                calibration_timeout_seconds=1,
                calibration_poll_interval_seconds=0.1,
            )
        finally:
            sensor.close()
    assert bus.close_calls == 1


def test_export_profile_block_i2c_error_attempts_cleanup() -> None:
    bus = FakeBus()
    bus.fail_block_register = CALIBRATION_PROFILE_START
    sensor = device(bus)
    with pytest.raises(BNO055IOError, match="block 0x55"):
        try:
            sensor.capture_calibration_profile(
                sensor_label="rear-imu-primary",
                created_at_utc="2026-09-08T12:34:56Z",
                calibration_timeout_seconds=1,
                calibration_poll_interval_seconds=0.1,
            )
        finally:
            sensor.close()
    assert bus.close_calls == 1
    assert bus.writes[-1] == (0x29, OPR_MODE, CONFIG_MODE)


def test_identity_failure_never_writes_config_mode() -> None:
    bus = FakeBus()
    bus.bytes[CHIP_ID] = 0
    with pytest.raises(BNO055IdentityError):
        device(bus).restore_calibration_profile(profile())
    assert bus.writes == []


def test_invalid_export_metadata_is_rejected_before_register_io() -> None:
    bus = FakeBus()
    with pytest.raises(ValueError, match="created_at_utc"):
        device(bus).capture_calibration_profile(
            sensor_label="rear-imu-primary",
            created_at_utc="not-a-time",
            calibration_timeout_seconds=1,
            calibration_poll_interval_seconds=0.1,
        )
    assert bus.events == []


@pytest.mark.parametrize("field", ["software_revision", "bootloader_revision"])
def test_restore_rejects_revision_mismatch_before_config(field: str) -> None:
    bus = FakeBus()
    with pytest.raises(BNO055ProfileCompatibilityError, match=field):
        device(bus).restore_calibration_profile(profile(**{field: 1}))
    assert bus.writes == []


def test_restore_legal_profile_write_readback_precedes_ndof() -> None:
    bus = FakeBus()
    bus.profile_bytes = [0] * 22
    restored = device(bus).restore_calibration_profile(profile())
    assert restored.operation_mode == NDOF_MODE
    expected_writes = [
        (0x29, OPR_MODE, CONFIG_MODE),
        (0x29, PAGE_ID, 0),
        (0x29, PWR_MODE, NORMAL_POWER_MODE),
        (0x29, UNIT_SEL, DEFAULT_UNITS),
        *[
            (0x29, CALIBRATION_PROFILE_START + index, value)
            for index, value in enumerate(PROFILE_BYTES)
        ],
        (0x29, OPR_MODE, NDOF_MODE),
    ]
    assert bus.writes == expected_writes
    profile_read = bus.events.index(("block", CALIBRATION_PROFILE_START, 22))
    ndof_write = bus.events.index(("write", OPR_MODE, NDOF_MODE))
    measurement_read = bus.events.index(("block", MEASUREMENT_DATA_START, 26))
    assert profile_read < ndof_write < measurement_read


def test_axis_map_mismatch_is_rejected_without_profile_write() -> None:
    bus = FakeBus()
    bus.bytes[AXIS_MAP_SIGN] = 1
    with pytest.raises(BNO055ProfileCompatibilityError, match="axis-map"):
        device(bus).restore_calibration_profile(profile())
    assert not any(
        CALIBRATION_PROFILE_START <= register <= CALIBRATION_PROFILE_END
        for _, register, _ in bus.writes
    )


@pytest.mark.parametrize("position", range(22))
def test_each_profile_byte_write_failure_names_register_and_cleanup_runs(position: int) -> None:
    bus = FakeBus()
    failed_register = CALIBRATION_PROFILE_START + position
    bus.fail_write_register = failed_register
    sensor = device(bus)
    with pytest.raises(BNO055IOError, match=f"0x{failed_register:02x}"):
        try:
            sensor.restore_calibration_profile(profile())
        finally:
            sensor.close()
    assert bus.close_calls == 1
    assert bus.writes[-1][1] == OPR_MODE


@pytest.mark.parametrize("position", range(22))
def test_each_readback_mismatch_reports_first_register(position: int) -> None:
    bus = FakeBus()
    bus.profile_readback_mismatch = position
    sensor = device(bus)
    with pytest.raises(BNO055ProfileReadbackError) as captured:
        try:
            sensor.restore_calibration_profile(profile())
        finally:
            sensor.close()
    assert captured.value.register == CALIBRATION_PROFILE_START + position
    assert captured.value.expected != captured.value.actual
    assert not any(register == OPR_MODE and value == NDOF_MODE for _, register, value in bus.writes)
    assert bus.close_calls == 1


@pytest.mark.parametrize("override", [[0] * 21, [0] * 21 + [False]])
def test_restore_rejects_short_or_invalid_readback(override: list[object]) -> None:
    bus = FakeBus()
    bus.profile_block_override = override
    sensor = device(bus)
    with pytest.raises(BNO055IOError):
        try:
            sensor.restore_calibration_profile(profile())
        finally:
            sensor.close()
    assert bus.close_calls == 1
    assert not any(
        register == OPR_MODE and value == NDOF_MODE
        for _, register, value in bus.writes
    )


def test_restore_readback_i2c_error_attempts_cleanup_without_entering_ndof() -> None:
    bus = FakeBus()
    bus.fail_block_register = CALIBRATION_PROFILE_START
    sensor = device(bus)
    with pytest.raises(BNO055IOError, match="block 0x55"):
        try:
            sensor.restore_calibration_profile(profile())
        finally:
            sensor.close()
    assert bus.close_calls == 1
    assert not any(
        register == OPR_MODE and value == NDOF_MODE
        for _, register, value in bus.writes
    )
    assert not any(
        event == ("block", MEASUREMENT_DATA_START, MEASUREMENT_DATA_LENGTH)
        for event in bus.events
    )


def test_restore_readiness_failure_attempts_cleanup_without_measurement() -> None:
    bus = FakeBus()
    bus.bytes[SYS_STATUS] = 0x03
    sensor = device(
        bus,
        ConstantClock(),
        readiness_timeout_seconds=0.05,
        readiness_poll_interval_seconds=0.02,
    )
    with pytest.raises(BNO055ReadinessTimeoutError, match="readiness timed out"):
        try:
            sensor.restore_calibration_profile(profile())
        finally:
            sensor.close()
    assert bus.close_calls == 1
    assert bus.writes[-1] == (0x29, OPR_MODE, CONFIG_MODE)
    readiness_reads = [
        event for event in bus.events if event == ("read", OPR_MODE)
    ]
    assert len(readiness_reads) == math.ceil(0.05 / 0.02) + 1
    assert not any(
        event == ("block", MEASUREMENT_DATA_START, MEASUREMENT_DATA_LENGTH)
        for event in bus.events
    )


def test_restore_measurement_quality_poll_is_bounded_with_constant_clock() -> None:
    bus = FakeBus()
    bus.measurement_bytes = [0] * 26
    clock = ConstantClock()
    timeout = 0.05
    sensor = device(bus, clock)
    with pytest.raises(BNO055MeasurementQualityTimeoutError, match="last_reasons"):
        try:
            sensor.restore_calibration_profile(
                profile(),
                data_ready_timeout_seconds=timeout,
                data_ready_poll_interval_seconds=0.02,
            )
        finally:
            sensor.close()
    reads = [event for event in bus.events if event == ("block", MEASUREMENT_DATA_START, 26)]
    assert len(reads) == math.ceil(timeout / 0.02) + 1
    assert bus.close_calls == 1


def test_restore_measurement_quality_poll_is_bounded_with_backward_clock() -> None:
    bus = FakeBus()
    bus.measurement_bytes = [0] * 26
    clock = BackwardClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert second < first
    timeout = 0.05
    maximum_attempts = math.ceil(timeout / 0.02) + 1
    sensor = device(bus, clock)
    with pytest.raises(BNO055MeasurementQualityTimeoutError, match="last_reasons"):
        try:
            sensor.restore_calibration_profile(
                profile(),
                data_ready_timeout_seconds=timeout,
                data_ready_poll_interval_seconds=0.02,
            )
        finally:
            sensor.close()
    reads = [event for event in bus.events if event == ("block", MEASUREMENT_DATA_START, 26)]
    assert len(reads) == maximum_attempts
    assert all(
        later < earlier
        for earlier, later in zip(clock.readings, clock.readings[1:])
    )
    assert bus.close_calls == 1
