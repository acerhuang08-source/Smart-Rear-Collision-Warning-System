"""Injectable BNO055 device layer with explicit configuration lifecycle."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Protocol

from .calibration_profile import (
    BNO055CalibrationProfile,
    BNO055CalibrationProfileError,
    decode_calibration_profile_bytes,
    encode_calibration_profile_bytes,
    validate_created_at_utc,
    validate_sensor_label,
)
from .models import BNO055Calibration, BNO055Identity, BNO055Measurement
from .quality import MeasurementQualityThresholds, assess_measurement_quality
from .registers import (
    ACC_ID, AXIS_MAP_CONFIG, AXIS_MAP_SIGN, BL_REV_ID, CALIB_STAT,
    CALIBRATION_PROFILE_LENGTH, CALIBRATION_PROFILE_START, CHIP_ID, CONFIG_MODE,
    DEFAULT_UNITS,
    EULER_LENGTH, EULER_OFFSET, EXPECTED_ACC_ID, EXPECTED_CHIP_ID,
    EXPECTED_GYR_ID, EXPECTED_MAG_ID, GRAVITY_LENGTH, GRAVITY_OFFSET, GYR_ID,
    LINEAR_ACCELERATION_LENGTH, LINEAR_ACCELERATION_OFFSET, MAG_ID,
    MEASUREMENT_DATA_LENGTH, MEASUREMENT_DATA_START, NDOF_MODE,
    NORMAL_POWER_MODE, OPR_MODE, PAGE_ID, PWR_MODE, QUATERNION_LENGTH,
    QUATERNION_OFFSET, SW_REV_ID_LSB, SW_REV_ID_MSB, SYS_ERR, SYS_STATUS,
    TEMPERATURE, UNIT_SEL, decode_acceleration, decode_calibration, decode_euler,
    decode_quaternion, signed_int8,
)


class RegisterIO(Protocol):
    def read_byte_data(self, address: int, register: int) -> int: ...
    def read_i2c_block_data(self, address: int, register: int, length: int) -> list[int]: ...
    def write_byte_data(self, address: int, register: int, value: int) -> None: ...
    def close(self) -> None: ...


class BNO055Error(Exception):
    """Base error for device and diagnostic failures."""


class BNO055IOError(BNO055Error):
    """Register I/O failed or returned malformed data."""


class BNO055IdentityError(BNO055Error):
    """A fixed identity register did not match the BNO055."""


class BNO055ModeError(BNO055Error):
    """The requested operation mode was not accepted."""


class BNO055SystemError(BNO055Error):
    """The sensor reported a non-zero system error after NDOF entry."""


class BNO055CleanupError(BNO055Error):
    """Returning to CONFIGMODE or closing the bus failed."""


class BNO055ReadinessTimeoutError(BNO055Error):
    """NDOF did not become ready within the configured finite timeout."""


class BNO055RuntimeStateError(BNO055Error):
    """A measurement was accompanied by an invalid fusion state."""

    def __init__(self, *, operation_mode: int, system_status: int, system_error: int) -> None:
        self.operation_mode = operation_mode
        self.system_status = system_status
        self.system_error = system_error
        super().__init__(
            f"mode=0x{operation_mode:02x} status=0x{system_status:02x} "
            f"error=0x{system_error:02x}"
        )


class BNO055CalibrationTimeoutError(BNO055Error):
    """Full calibration was not reached within both finite polling bounds."""


class BNO055ProfileCompatibilityError(BNO055Error):
    """A valid profile does not match the connected/configured sensor."""


class BNO055ProfileReadbackError(BNO055Error):
    """A calibration byte differed immediately after restore."""

    def __init__(self, *, register: int, expected: int, actual: int) -> None:
        self.register = register
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"calibration read-back mismatch at register 0x{register:02x}: "
            f"expected 0x{expected:02x}, got 0x{actual:02x}"
        )


class BNO055MeasurementQualityTimeoutError(BNO055Error):
    """No measurement passed the existing quality gate within finite bounds."""


class BNO055Device:
    CONFIG_MODE_DELAY_SECONDS = 0.019
    OPERATION_MODE_DELAY_SECONDS = 0.007
    DEFAULT_READINESS_TIMEOUT_SECONDS = 2.0
    DEFAULT_READINESS_POLL_INTERVAL_SECONDS = 0.02
    DEFAULT_CALIBRATION_TIMEOUT_SECONDS = 120.0
    DEFAULT_CALIBRATION_POLL_INTERVAL_SECONDS = 0.1

    def __init__(
        self,
        register_io: RegisterIO,
        *,
        address: int = 0x29,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        readiness_poll_interval_seconds: float = DEFAULT_READINESS_POLL_INTERVAL_SECONDS,
    ) -> None:
        if isinstance(address, bool) or address not in (0x28, 0x29):
            raise ValueError("address must be 0x28 or 0x29")
        self._validate_positive_finite(
            "readiness_timeout_seconds", readiness_timeout_seconds
        )
        self._validate_positive_finite(
            "readiness_poll_interval_seconds", readiness_poll_interval_seconds
        )
        self._io = register_io
        self.address = address
        self._sleep = sleep
        self._monotonic = monotonic
        self._readiness_timeout_seconds = float(readiness_timeout_seconds)
        self._readiness_poll_interval_seconds = float(
            readiness_poll_interval_seconds
        )
        self._maximum_readiness_polls = (
            math.ceil(
                self._readiness_timeout_seconds
                / self._readiness_poll_interval_seconds
            )
            + 1
        )
        self._closed = False
        self._configuration_started = False

    @staticmethod
    def _validate_positive_finite(name: str, value: object) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be a number")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be a positive finite number")

    def _read_byte(self, register: int) -> int:
        try:
            value = self._io.read_byte_data(self.address, register)
        except Exception as exc:
            raise BNO055IOError(f"failed to read register 0x{register:02x}") from exc
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
            raise BNO055IOError(f"invalid byte from register 0x{register:02x}: {value!r}")
        return value

    def _read_block(self, register: int, length: int) -> list[int]:
        try:
            data = self._io.read_i2c_block_data(self.address, register, length)
        except Exception as exc:
            raise BNO055IOError(f"failed to read register block 0x{register:02x}") from exc
        if not isinstance(data, list):
            raise BNO055IOError(f"invalid block type at 0x{register:02x}")
        if len(data) != length:
            raise BNO055IOError(f"short block read at 0x{register:02x}: expected {length}, got {len(data)}")
        if any(isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF for value in data):
            raise BNO055IOError(f"invalid block data at 0x{register:02x}")
        return data

    def _write_byte(self, register: int, value: int) -> None:
        try:
            self._io.write_byte_data(self.address, register, value)
        except Exception as exc:
            raise BNO055IOError(f"failed to write register 0x{register:02x}") from exc

    def read_identity(self) -> BNO055Identity:
        identity = BNO055Identity(
            address=self.address,
            chip_id=self._read_byte(CHIP_ID),
            accelerometer_id=self._read_byte(ACC_ID),
            magnetometer_id=self._read_byte(MAG_ID),
            gyroscope_id=self._read_byte(GYR_ID),
            software_revision=self._read_byte(SW_REV_ID_LSB) | (self._read_byte(SW_REV_ID_MSB) << 8),
            bootloader_revision=self._read_byte(BL_REV_ID),
        )
        expected = (
            ("chip", identity.chip_id, EXPECTED_CHIP_ID),
            ("accelerometer", identity.accelerometer_id, EXPECTED_ACC_ID),
            ("magnetometer", identity.magnetometer_id, EXPECTED_MAG_ID),
            ("gyroscope", identity.gyroscope_id, EXPECTED_GYR_ID),
        )
        for name, actual, wanted in expected:
            if actual != wanted:
                raise BNO055IdentityError(f"{name} ID mismatch: expected 0x{wanted:02x}, got 0x{actual:02x}")
        return identity

    def read_status(self) -> tuple[int, int]:
        return self._read_byte(SYS_STATUS), self._read_byte(SYS_ERR)

    def read_calibration(self) -> BNO055Calibration:
        return decode_calibration(self._read_byte(CALIB_STAT))

    def initialize_ndof(self) -> tuple[int, int, int]:
        self._configuration_started = True
        self._write_byte(OPR_MODE, CONFIG_MODE)
        self._sleep(self.CONFIG_MODE_DELAY_SECONDS)
        self._write_byte(PAGE_ID, 0x00)
        self._write_byte(PWR_MODE, NORMAL_POWER_MODE)
        self._write_byte(UNIT_SEL, DEFAULT_UNITS)
        self._write_byte(OPR_MODE, NDOF_MODE)
        self._sleep(self.OPERATION_MODE_DELAY_SECONDS)
        return self._wait_until_ndof_ready()

    def _enter_config_mode(self) -> None:
        self._configuration_started = True
        self._write_byte(OPR_MODE, CONFIG_MODE)
        self._sleep(self.CONFIG_MODE_DELAY_SECONDS)

    def _wait_until_fully_calibrated(
        self, *, timeout_seconds: float, poll_interval_seconds: float
    ) -> None:
        self._validate_positive_finite("timeout_seconds", timeout_seconds)
        self._validate_positive_finite("poll_interval_seconds", poll_interval_seconds)
        timeout = float(timeout_seconds)
        interval = float(poll_interval_seconds)
        maximum_attempts = math.ceil(timeout / interval) + 1
        deadline = self._monotonic() + timeout
        last_status = 0
        for attempt in range(1, maximum_attempts + 1):
            last_status = self._read_byte(CALIB_STAT)
            if last_status == 0xFF:
                return
            now = self._monotonic()
            if now >= deadline or attempt >= maximum_attempts:
                raise BNO055CalibrationTimeoutError(
                    "BNO055 full calibration timed out after "
                    f"{timeout:.3f}s and {attempt} attempts: "
                    f"CALIB_STAT=0x{last_status:02x}"
                )
            self._sleep(min(interval, max(deadline - now, 0.0)))

    def capture_calibration_profile(
        self,
        *,
        sensor_label: str,
        created_at_utc: str,
        calibration_timeout_seconds: float = DEFAULT_CALIBRATION_TIMEOUT_SECONDS,
        calibration_poll_interval_seconds: float = DEFAULT_CALIBRATION_POLL_INTERVAL_SECONDS,
    ) -> BNO055CalibrationProfile:
        """Capture validated page-0 offsets/radii after full NDOF calibration."""

        validate_sensor_label(sensor_label)
        validate_created_at_utc(created_at_utc)
        self._validate_positive_finite(
            "calibration_timeout_seconds", calibration_timeout_seconds
        )
        self._validate_positive_finite(
            "calibration_poll_interval_seconds", calibration_poll_interval_seconds
        )
        identity = self.read_identity()
        self.initialize_ndof()
        self._wait_until_fully_calibrated(
            timeout_seconds=calibration_timeout_seconds,
            poll_interval_seconds=calibration_poll_interval_seconds,
        )
        self._enter_config_mode()
        self._write_byte(PAGE_ID, 0x00)
        unit_sel = self._read_byte(UNIT_SEL)
        power_mode = self._read_byte(PWR_MODE)
        axis_map_config = self._read_byte(AXIS_MAP_CONFIG)
        axis_map_sign = self._read_byte(AXIS_MAP_SIGN)
        raw = self._read_block(
            CALIBRATION_PROFILE_START, CALIBRATION_PROFILE_LENGTH
        )
        calibration = decode_calibration_profile_bytes(raw)
        return BNO055CalibrationProfile(
            sensor_label=sensor_label,
            chip_id=identity.chip_id,
            accelerometer_id=identity.accelerometer_id,
            magnetometer_id=identity.magnetometer_id,
            gyroscope_id=identity.gyroscope_id,
            software_revision=identity.software_revision,
            bootloader_revision=identity.bootloader_revision,
            unit_sel=unit_sel,
            power_mode=power_mode,
            operation_mode=NDOF_MODE,
            axis_map_config=axis_map_config,
            axis_map_sign=axis_map_sign,
            calibration=calibration,
            created_at_utc=created_at_utc,
        )

    @staticmethod
    def _require_profile_identity(
        profile: BNO055CalibrationProfile, identity: BNO055Identity
    ) -> None:
        comparisons = (
            ("chip_id", profile.chip_id, identity.chip_id),
            ("accelerometer_id", profile.accelerometer_id, identity.accelerometer_id),
            ("magnetometer_id", profile.magnetometer_id, identity.magnetometer_id),
            ("gyroscope_id", profile.gyroscope_id, identity.gyroscope_id),
            ("software_revision", profile.software_revision, identity.software_revision),
            ("bootloader_revision", profile.bootloader_revision, identity.bootloader_revision),
        )
        for name, expected, actual in comparisons:
            if expected != actual:
                raise BNO055ProfileCompatibilityError(
                    f"profile {name} mismatch: expected 0x{expected:x}, got 0x{actual:x}"
                )

    def _wait_for_quality_measurement(
        self, *, timeout_seconds: float, poll_interval_seconds: float
    ) -> BNO055Measurement:
        self._validate_positive_finite("timeout_seconds", timeout_seconds)
        self._validate_positive_finite("poll_interval_seconds", poll_interval_seconds)
        timeout = float(timeout_seconds)
        interval = float(poll_interval_seconds)
        maximum_attempts = math.ceil(timeout / interval) + 1
        deadline = self._monotonic() + timeout
        last_issues: tuple[str, ...] = ()
        thresholds = MeasurementQualityThresholds()
        for attempt in range(1, maximum_attempts + 1):
            measurement = self.read_measurement()
            assessment = assess_measurement_quality(measurement, thresholds)
            if assessment.is_valid:
                return measurement
            if assessment.state_issues:
                raise BNO055RuntimeStateError(
                    operation_mode=measurement.operation_mode,
                    system_status=measurement.system_status,
                    system_error=measurement.system_error,
                )
            last_issues = assessment.data_issues
            now = self._monotonic()
            if now >= deadline or attempt >= maximum_attempts:
                raise BNO055MeasurementQualityTimeoutError(
                    "BNO055 measurement quality timed out after "
                    f"{timeout:.3f}s and {attempt} attempts: "
                    f"last_reasons={'; '.join(last_issues)}"
                )
            self._sleep(min(interval, max(deadline - now, 0.0)))
        raise BNO055MeasurementQualityTimeoutError(
            "BNO055 measurement quality ended without a valid sample"
        )

    def restore_calibration_profile(
        self,
        profile: BNO055CalibrationProfile,
        *,
        data_ready_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
        data_ready_poll_interval_seconds: float = DEFAULT_READINESS_POLL_INTERVAL_SECONDS,
    ) -> BNO055Measurement:
        """Write, verify, activate, and quality-check one validated profile."""

        if not isinstance(profile, BNO055CalibrationProfile):
            raise BNO055CalibrationProfileError(
                "profile must be BNO055CalibrationProfile"
            )
        self._validate_positive_finite(
            "data_ready_timeout_seconds", data_ready_timeout_seconds
        )
        self._validate_positive_finite(
            "data_ready_poll_interval_seconds", data_ready_poll_interval_seconds
        )
        raw = encode_calibration_profile_bytes(profile.calibration)
        identity = self.read_identity()
        self._require_profile_identity(profile, identity)
        self._enter_config_mode()
        self._write_byte(PAGE_ID, 0x00)
        self._write_byte(PWR_MODE, NORMAL_POWER_MODE)
        self._write_byte(UNIT_SEL, DEFAULT_UNITS)
        actual_axis_config = self._read_byte(AXIS_MAP_CONFIG)
        actual_axis_sign = self._read_byte(AXIS_MAP_SIGN)
        if (
            actual_axis_config != profile.axis_map_config
            or actual_axis_sign != profile.axis_map_sign
        ):
            raise BNO055ProfileCompatibilityError(
                "axis-map mismatch: "
                f"profile=0x{profile.axis_map_config:02x}/0x{profile.axis_map_sign:02x} "
                f"sensor=0x{actual_axis_config:02x}/0x{actual_axis_sign:02x}"
            )
        for offset, value in enumerate(raw):
            self._write_byte(CALIBRATION_PROFILE_START + offset, value)
        readback = self._read_block(
            CALIBRATION_PROFILE_START, CALIBRATION_PROFILE_LENGTH
        )
        for offset, (expected, actual) in enumerate(zip(raw, readback, strict=True)):
            if expected != actual:
                raise BNO055ProfileReadbackError(
                    register=CALIBRATION_PROFILE_START + offset,
                    expected=expected,
                    actual=actual,
                )
        self._write_byte(OPR_MODE, NDOF_MODE)
        self._sleep(self.OPERATION_MODE_DELAY_SECONDS)
        self._wait_until_ndof_ready()
        return self._wait_for_quality_measurement(
            timeout_seconds=data_ready_timeout_seconds,
            poll_interval_seconds=data_ready_poll_interval_seconds,
        )

    def _wait_until_ndof_ready(self) -> tuple[int, int, int]:
        deadline = self._monotonic() + self._readiness_timeout_seconds
        for poll_number in range(1, self._maximum_readiness_polls + 1):
            actual_mode = self._read_byte(OPR_MODE)
            system_status, system_error = self.read_status()
            if system_error != 0 or system_status == 0x01:
                raise BNO055SystemError(
                    "BNO055 system error after NDOF entry: "
                    f"mode=0x{actual_mode:02x} status=0x{system_status:02x} "
                    f"error=0x{system_error:02x}"
                )
            if (
                actual_mode == NDOF_MODE
                and system_status == 0x05
                and system_error == 0x00
            ):
                return actual_mode, system_status, system_error
            now = self._monotonic()
            if now >= deadline or poll_number >= self._maximum_readiness_polls:
                raise BNO055ReadinessTimeoutError(
                    f"BNO055 readiness timed out after "
                    f"{self._readiness_timeout_seconds:.3f}s: "
                    f"mode=0x{actual_mode:02x} status=0x{system_status:02x} "
                    f"error=0x{system_error:02x}"
                )
            self._sleep(
                min(self._readiness_poll_interval_seconds, deadline - now)
            )

    def read_measurement(self) -> BNO055Measurement:
        timestamp = self._monotonic()
        data = self._read_block(MEASUREMENT_DATA_START, MEASUREMENT_DATA_LENGTH)
        euler = decode_euler(data[EULER_OFFSET:EULER_OFFSET + EULER_LENGTH])
        quaternion = decode_quaternion(
            data[QUATERNION_OFFSET:QUATERNION_OFFSET + QUATERNION_LENGTH]
        )
        linear = decode_acceleration(
            data[
                LINEAR_ACCELERATION_OFFSET:
                LINEAR_ACCELERATION_OFFSET + LINEAR_ACCELERATION_LENGTH
            ]
        )
        gravity = decode_acceleration(
            data[GRAVITY_OFFSET:GRAVITY_OFFSET + GRAVITY_LENGTH]
        )
        temperature = signed_int8(self._read_byte(TEMPERATURE))
        calibration = self.read_calibration()
        operation_mode = self._read_byte(OPR_MODE)
        system_status, system_error = self.read_status()
        if (
            operation_mode != NDOF_MODE
            or system_status != 0x05
            or system_error != 0x00
        ):
            raise BNO055RuntimeStateError(
                operation_mode=operation_mode,
                system_status=system_status,
                system_error=system_error,
            )
        return BNO055Measurement(
            timestamp, euler, quaternion, linear, gravity, temperature,
            calibration, operation_mode, system_status, system_error,
        )

    def close(self) -> None:
        if self._closed:
            return
        errors: list[BaseException] = []
        if self._configuration_started:
            try:
                self._write_byte(OPR_MODE, CONFIG_MODE)
                self._sleep(self.CONFIG_MODE_DELAY_SECONDS)
            except BaseException as exc:
                errors.append(exc)
        try:
            self._io.close()
        except BaseException as exc:
            errors.append(exc)
        self._closed = True
        if errors:
            details = "; ".join(str(detail) for detail in errors)
            error = BNO055CleanupError(f"BNO055 cleanup failed: {details}")
            for detail in errors:
                error.add_note(repr(detail))
            raise error from errors[0]
