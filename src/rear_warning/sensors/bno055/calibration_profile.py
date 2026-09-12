"""I/O-free BNO055 calibration profile model and canonical JSON codec."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .registers import (
    DEFAULT_UNITS,
    EXPECTED_ACC_ID,
    EXPECTED_CHIP_ID,
    EXPECTED_GYR_ID,
    EXPECTED_MAG_ID,
    NDOF_MODE,
    NORMAL_POWER_MODE,
    signed_int16_le,
)

SCHEMA_NAME = "rear-warning.bno055-calibration-profile"
SCHEMA_VERSION = 1
MAX_SENSOR_LABEL_LENGTH = 64
PROFILE_JSON_MAX_BYTES = 64 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_RFC3339_UTC_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z\Z"
)


class BNO055CalibrationProfileError(ValueError):
    """A calibration profile is malformed, unsupported, or incompatible."""


def _integer(name: str, value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BNO055CalibrationProfileError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise BNO055CalibrationProfileError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def validate_sensor_label(value: object) -> str:
    if not isinstance(value, str):
        raise BNO055CalibrationProfileError("sensor_label must be a string")
    if not value or value != value.strip():
        raise BNO055CalibrationProfileError(
            "sensor_label must be non-blank with no surrounding whitespace"
        )
    if len(value) > MAX_SENSOR_LABEL_LENGTH:
        raise BNO055CalibrationProfileError(
            f"sensor_label must be at most {MAX_SENSOR_LABEL_LENGTH} characters"
        )
    if not value.isprintable():
        raise BNO055CalibrationProfileError("sensor_label must contain printable characters")
    return value


@dataclass(frozen=True, slots=True)
class BNO055CalibrationData:
    accelerometer_offset_x: int
    accelerometer_offset_y: int
    accelerometer_offset_z: int
    magnetometer_offset_x: int
    magnetometer_offset_y: int
    magnetometer_offset_z: int
    gyroscope_offset_x: int
    gyroscope_offset_y: int
    gyroscope_offset_z: int
    accelerometer_radius: int
    magnetometer_radius: int

    def __post_init__(self) -> None:
        for name in (
            "accelerometer_offset_x",
            "accelerometer_offset_y",
            "accelerometer_offset_z",
        ):
            _integer(name, getattr(self, name), -500, 500)
        for name in (
            "magnetometer_offset_x",
            "magnetometer_offset_y",
            "magnetometer_offset_z",
        ):
            _integer(name, getattr(self, name), -6400, 6400)
        for name in (
            "gyroscope_offset_x",
            "gyroscope_offset_y",
            "gyroscope_offset_z",
        ):
            _integer(name, getattr(self, name), -2000, 2000)
        _integer("accelerometer_radius", self.accelerometer_radius, -2048, 2048)
        _integer("magnetometer_radius", self.magnetometer_radius, 144, 1280)


_CALIBRATION_FIELDS = tuple(BNO055CalibrationData.__dataclass_fields__)


def decode_calibration_profile_bytes(data: Sequence[int]) -> BNO055CalibrationData:
    if isinstance(data, str) or not isinstance(data, Sequence):
        raise BNO055CalibrationProfileError(
            "calibration register data must be a byte sequence"
        )
    values = data
    if len(values) != 22:
        raise BNO055CalibrationProfileError(
            f"calibration register data must contain 22 bytes, got {len(values)}"
        )
    checked = [
        _integer(f"calibration byte {index}", value, 0, 0xFF)
        for index, value in enumerate(values)
    ]
    decoded = [
        signed_int16_le(checked[index], checked[index + 1])
        for index in range(0, 22, 2)
    ]
    return BNO055CalibrationData(*decoded)


def encode_calibration_profile_bytes(data: BNO055CalibrationData) -> tuple[int, ...]:
    if not isinstance(data, BNO055CalibrationData):
        raise BNO055CalibrationProfileError(
            "calibration must be BNO055CalibrationData"
        )
    result: list[int] = []
    for field_name in _CALIBRATION_FIELDS:
        unsigned = getattr(data, field_name) & 0xFFFF
        result.extend((unsigned & 0xFF, unsigned >> 8))
    return tuple(result)


def _validate_axis_map(config: object, sign: object) -> tuple[int, int]:
    checked_config = _integer("axis_map_config", config, 0, 0x3F)
    checked_sign = _integer("axis_map_sign", sign, 0, 0x07)
    axes = (
        checked_config & 0x03,
        (checked_config >> 2) & 0x03,
        (checked_config >> 4) & 0x03,
    )
    if sorted(axes) != [0, 1, 2]:
        raise BNO055CalibrationProfileError(
            "axis_map_config must map X/Y/Z to a permutation of axes 0, 1, and 2"
        )
    return checked_config, checked_sign


def validate_created_at_utc(value: object) -> str:
    if (
        not isinstance(value, str)
        or _RFC3339_UTC_PATTERN.fullmatch(value) is None
    ):
        raise BNO055CalibrationProfileError(
            "created_at_utc must use YYYY-MM-DDTHH:MM:SS[.ffffff]Z"
        )
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BNO055CalibrationProfileError("created_at_utc is invalid") from exc
    return value


def utc_timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BNO055CalibrationProfileError("creation time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True, slots=True)
class BNO055CalibrationProfile:
    sensor_label: str
    chip_id: int
    accelerometer_id: int
    magnetometer_id: int
    gyroscope_id: int
    software_revision: int
    bootloader_revision: int
    unit_sel: int
    power_mode: int
    operation_mode: int
    axis_map_config: int
    axis_map_sign: int
    calibration: BNO055CalibrationData
    created_at_utc: str
    schema_name: str = SCHEMA_NAME
    schema_version: int = SCHEMA_VERSION
    payload_sha256: str | None = None

    def __post_init__(self) -> None:
        validate_sensor_label(self.sensor_label)
        if self.schema_name != SCHEMA_NAME:
            raise BNO055CalibrationProfileError("unsupported schema_name")
        _integer("schema_version", self.schema_version, SCHEMA_VERSION, SCHEMA_VERSION)
        if self.schema_version != SCHEMA_VERSION:
            raise BNO055CalibrationProfileError("unsupported schema_version")
        expected_ids = (
            ("chip_id", self.chip_id, EXPECTED_CHIP_ID),
            ("accelerometer_id", self.accelerometer_id, EXPECTED_ACC_ID),
            ("magnetometer_id", self.magnetometer_id, EXPECTED_MAG_ID),
            ("gyroscope_id", self.gyroscope_id, EXPECTED_GYR_ID),
        )
        for name, actual, expected in expected_ids:
            _integer(name, actual, 0, 0xFF)
            if actual != expected:
                raise BNO055CalibrationProfileError(
                    f"{name} must be 0x{expected:02x}"
                )
        _integer("software_revision", self.software_revision, 0, 0xFFFF)
        _integer("bootloader_revision", self.bootloader_revision, 0, 0xFF)
        _integer("unit_sel", self.unit_sel, 0, 0xFF)
        if self.unit_sel != DEFAULT_UNITS:
            raise BNO055CalibrationProfileError("unsupported UNIT_SEL")
        _integer("power_mode", self.power_mode, 0, 0xFF)
        if self.power_mode != NORMAL_POWER_MODE:
            raise BNO055CalibrationProfileError("unsupported power mode")
        _integer("operation_mode", self.operation_mode, 0, 0xFF)
        if self.operation_mode != NDOF_MODE:
            raise BNO055CalibrationProfileError("unsupported operation mode")
        _validate_axis_map(self.axis_map_config, self.axis_map_sign)
        if not isinstance(self.calibration, BNO055CalibrationData):
            raise BNO055CalibrationProfileError(
                "calibration must be BNO055CalibrationData"
            )
        validate_created_at_utc(self.created_at_utc)
        calculated = _payload_digest(self._payload_dict())
        if self.payload_sha256 is None:
            object.__setattr__(self, "payload_sha256", calculated)
        elif (
            not isinstance(self.payload_sha256, str)
            or _SHA256_PATTERN.fullmatch(self.payload_sha256) is None
            or self.payload_sha256 != calculated
        ):
            raise BNO055CalibrationProfileError("payload_sha256 mismatch")

    def _payload_dict(self) -> dict[str, object]:
        return {
            "schema_name": self.schema_name,
            "schema_version": self.schema_version,
            "sensor_label": self.sensor_label,
            "identity": {
                "chip_id": self.chip_id,
                "accelerometer_id": self.accelerometer_id,
                "magnetometer_id": self.magnetometer_id,
                "gyroscope_id": self.gyroscope_id,
                "software_revision": self.software_revision,
                "bootloader_revision": self.bootloader_revision,
            },
            "settings": {
                "unit_sel": self.unit_sel,
                "power_mode": self.power_mode,
                "operation_mode": self.operation_mode,
                "axis_map_config": self.axis_map_config,
                "axis_map_sign": self.axis_map_sign,
            },
            "calibration": {
                name: getattr(self.calibration, name) for name in _CALIBRATION_FIELDS
            },
            "created_at_utc": self.created_at_utc,
        }

    def to_dict(self) -> dict[str, object]:
        result = self._payload_dict()
        if not isinstance(self.payload_sha256, str):
            raise BNO055CalibrationProfileError("payload_sha256 is unavailable")
        result["payload_sha256"] = self.payload_sha256
        return result


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _payload_digest(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def serialize_calibration_profile(profile: BNO055CalibrationProfile) -> bytes:
    if not isinstance(profile, BNO055CalibrationProfile):
        raise BNO055CalibrationProfileError(
            "profile must be BNO055CalibrationProfile"
        )
    return _canonical_json_bytes(profile.to_dict()) + b"\n"


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BNO055CalibrationProfileError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _exact_mapping(
    name: str, value: object, required: set[str]
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BNO055CalibrationProfileError(f"{name} must be an object")
    keys = set(value)
    if keys != required:
        missing = sorted(required - keys)
        extra = sorted(keys - required)
        raise BNO055CalibrationProfileError(
            f"{name} fields mismatch: missing={missing} extra={extra}"
        )
    if any(not isinstance(key, str) for key in value):
        raise BNO055CalibrationProfileError(f"{name} keys must be strings")
    return value


def deserialize_calibration_profile(data: bytes) -> BNO055CalibrationProfile:
    if not isinstance(data, bytes):
        raise BNO055CalibrationProfileError("profile input must be bytes")
    if len(data) > PROFILE_JSON_MAX_BYTES:
        raise BNO055CalibrationProfileError("profile input exceeds size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BNO055CalibrationProfileError("profile is not valid UTF-8") from exc
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                BNO055CalibrationProfileError(
                    f"non-finite JSON number is not allowed: {value}"
                )
            ),
        )
    except BNO055CalibrationProfileError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BNO055CalibrationProfileError("profile is not valid JSON") from exc
    top = _exact_mapping(
        "profile",
        raw,
        {
            "schema_name",
            "schema_version",
            "sensor_label",
            "identity",
            "settings",
            "calibration",
            "created_at_utc",
            "payload_sha256",
        },
    )
    identity = _exact_mapping(
        "identity",
        top["identity"],
        {
            "chip_id",
            "accelerometer_id",
            "magnetometer_id",
            "gyroscope_id",
            "software_revision",
            "bootloader_revision",
        },
    )
    settings = _exact_mapping(
        "settings",
        top["settings"],
        {
            "unit_sel",
            "power_mode",
            "operation_mode",
            "axis_map_config",
            "axis_map_sign",
        },
    )
    calibration = _exact_mapping(
        "calibration", top["calibration"], set(_CALIBRATION_FIELDS)
    )
    supplied_hash = top["payload_sha256"]
    if (
        not isinstance(supplied_hash, str)
        or _SHA256_PATTERN.fullmatch(supplied_hash) is None
    ):
        raise BNO055CalibrationProfileError(
            "payload_sha256 must be 64 lowercase hexadecimal characters"
        )
    return BNO055CalibrationProfile(
        sensor_label=top["sensor_label"],  # type: ignore[arg-type]
        chip_id=identity["chip_id"],  # type: ignore[arg-type]
        accelerometer_id=identity["accelerometer_id"],  # type: ignore[arg-type]
        magnetometer_id=identity["magnetometer_id"],  # type: ignore[arg-type]
        gyroscope_id=identity["gyroscope_id"],  # type: ignore[arg-type]
        software_revision=identity["software_revision"],  # type: ignore[arg-type]
        bootloader_revision=identity["bootloader_revision"],  # type: ignore[arg-type]
        unit_sel=settings["unit_sel"],  # type: ignore[arg-type]
        power_mode=settings["power_mode"],  # type: ignore[arg-type]
        operation_mode=settings["operation_mode"],  # type: ignore[arg-type]
        axis_map_config=settings["axis_map_config"],  # type: ignore[arg-type]
        axis_map_sign=settings["axis_map_sign"],  # type: ignore[arg-type]
        calibration=BNO055CalibrationData(
            **{name: calibration[name] for name in _CALIBRATION_FIELDS}  # type: ignore[arg-type]
        ),
        created_at_utc=top["created_at_utc"],  # type: ignore[arg-type]
        schema_name=top["schema_name"],  # type: ignore[arg-type]
        schema_version=top["schema_version"],  # type: ignore[arg-type]
        payload_sha256=supplied_hash,
    )
