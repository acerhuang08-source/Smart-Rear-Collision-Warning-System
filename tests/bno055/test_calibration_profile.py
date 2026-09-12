from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from rear_warning.sensors.bno055 import (
    BNO055CalibrationData,
    BNO055CalibrationProfile,
    BNO055CalibrationProfileError,
    decode_calibration_profile_bytes,
    deserialize_calibration_profile,
    encode_calibration_profile_bytes,
    serialize_calibration_profile,
    utc_timestamp,
    validate_created_at_utc,
    validate_sensor_label,
)


def calibration(**changes: object) -> BNO055CalibrationData:
    values: dict[str, object] = {
        "accelerometer_offset_x": -500,
        "accelerometer_offset_y": 0,
        "accelerometer_offset_z": 500,
        "magnetometer_offset_x": -6400,
        "magnetometer_offset_y": 0,
        "magnetometer_offset_z": 6400,
        "gyroscope_offset_x": -2000,
        "gyroscope_offset_y": 0,
        "gyroscope_offset_z": 2000,
        "accelerometer_radius": -2048,
        "magnetometer_radius": 1280,
    }
    values.update(changes)
    return BNO055CalibrationData(**values)  # type: ignore[arg-type]


def profile(**changes: object) -> BNO055CalibrationProfile:
    values: dict[str, object] = {
        "sensor_label": "rear-imu-primary",
        "chip_id": 0xA0,
        "accelerometer_id": 0xFB,
        "magnetometer_id": 0x32,
        "gyroscope_id": 0x0F,
        "software_revision": 0x0308,
        "bootloader_revision": 0x15,
        "unit_sel": 0,
        "power_mode": 0,
        "operation_mode": 0x0C,
        "axis_map_config": 0x24,
        "axis_map_sign": 0,
        "calibration": calibration(),
        "created_at_utc": "2026-09-08T12:34:56.123456Z",
    }
    values.update(changes)
    return BNO055CalibrationProfile(**values)  # type: ignore[arg-type]


def encoded_dict() -> dict[str, object]:
    return json.loads(serialize_calibration_profile(profile()))


def resign(value: dict[str, object]) -> bytes:
    payload = dict(value)
    payload.pop("payload_sha256", None)
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    value["payload_sha256"] = hashlib.sha256(canonical).hexdigest()
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode() + b"\n"


def test_complete_22_byte_fixture_and_little_endian_round_trip() -> None:
    expected = (
        0x0C, 0xFE, 0x00, 0x00, 0xF4, 0x01,
        0x00, 0xE7, 0x00, 0x00, 0x00, 0x19,
        0x30, 0xF8, 0x00, 0x00, 0xD0, 0x07,
        0x00, 0xF8, 0x00, 0x05,
    )
    assert encode_calibration_profile_bytes(calibration()) == expected
    assert decode_calibration_profile_bytes(expected) == calibration()


@pytest.mark.parametrize(
    ("field", "minimum", "maximum"),
    [
        ("accelerometer_offset_x", -500, 500),
        ("accelerometer_offset_y", -500, 500),
        ("accelerometer_offset_z", -500, 500),
        ("magnetometer_offset_x", -6400, 6400),
        ("magnetometer_offset_y", -6400, 6400),
        ("magnetometer_offset_z", -6400, 6400),
        ("gyroscope_offset_x", -2000, 2000),
        ("gyroscope_offset_y", -2000, 2000),
        ("gyroscope_offset_z", -2000, 2000),
        ("accelerometer_radius", -2048, 2048),
        ("magnetometer_radius", 144, 1280),
    ],
)
def test_every_calibration_field_accepts_inclusive_boundaries(
    field: str, minimum: int, maximum: int
) -> None:
    assert getattr(calibration(**{field: minimum}), field) == minimum
    assert getattr(calibration(**{field: maximum}), field) == maximum


@pytest.mark.parametrize("bad_kind", ["bool", "string", "float", "below", "above"])
@pytest.mark.parametrize(
    ("field", "minimum", "maximum"),
    [
        ("accelerometer_offset_x", -500, 500),
        ("accelerometer_offset_y", -500, 500),
        ("accelerometer_offset_z", -500, 500),
        ("magnetometer_offset_x", -6400, 6400),
        ("magnetometer_offset_y", -6400, 6400),
        ("magnetometer_offset_z", -6400, 6400),
        ("gyroscope_offset_x", -2000, 2000),
        ("gyroscope_offset_y", -2000, 2000),
        ("gyroscope_offset_z", -2000, 2000),
        ("accelerometer_radius", -2048, 2048),
        ("magnetometer_radius", 144, 1280),
    ],
)
def test_each_calibration_field_rejects_each_bad_type_and_range(
    field: str, minimum: int, maximum: int, bad_kind: str
) -> None:
    bad_values: dict[str, object] = {
        "bool": True,
        "string": "0",
        "float": 0.0,
        "below": minimum - 1,
        "above": maximum + 1,
    }
    with pytest.raises(BNO055CalibrationProfileError, match=field):
        calibration(**{field: bad_values[bad_kind]})


@pytest.mark.parametrize(
    ("field", "minimum", "maximum"),
    [
        ("schema_version", 1, 1),
        ("chip_id", 0xA0, 0xA0),
        ("accelerometer_id", 0xFB, 0xFB),
        ("magnetometer_id", 0x32, 0x32),
        ("gyroscope_id", 0x0F, 0x0F),
        ("software_revision", 0, 0xFFFF),
        ("bootloader_revision", 0, 0xFF),
        ("unit_sel", 0, 0),
        ("power_mode", 0, 0),
        ("operation_mode", 0x0C, 0x0C),
        ("axis_map_config", 0x06, 0x24),
        ("axis_map_sign", 0, 7),
    ],
)
def test_each_profile_numeric_field_accepts_its_legal_extremes(
    field: str, minimum: int, maximum: int
) -> None:
    assert getattr(profile(**{field: minimum}), field) == minimum
    assert getattr(profile(**{field: maximum}), field) == maximum


@pytest.mark.parametrize("bad_kind", ["bool", "string", "float", "below", "above"])
@pytest.mark.parametrize(
    ("field", "minimum", "maximum"),
    [
        ("schema_version", 1, 1),
        ("chip_id", 0xA0, 0xA0),
        ("accelerometer_id", 0xFB, 0xFB),
        ("magnetometer_id", 0x32, 0x32),
        ("gyroscope_id", 0x0F, 0x0F),
        ("software_revision", 0, 0xFFFF),
        ("bootloader_revision", 0, 0xFF),
        ("unit_sel", 0, 0),
        ("power_mode", 0, 0),
        ("operation_mode", 0x0C, 0x0C),
        ("axis_map_config", 0x06, 0x24),
        ("axis_map_sign", 0, 7),
    ],
)
def test_each_profile_numeric_field_rejects_each_bad_type_and_range(
    field: str, minimum: int, maximum: int, bad_kind: str
) -> None:
    bad_values: dict[str, object] = {
        "bool": True,
        "string": "0",
        "float": float(minimum),
        "below": minimum - 1,
        "above": maximum + 1,
    }
    with pytest.raises(BNO055CalibrationProfileError):
        profile(**{field: bad_values[bad_kind]})


@pytest.mark.parametrize("bad", [(), [0] * 21, [0] * 23, "x", [False] * 22, [256] * 22])
def test_decode_rejects_invalid_byte_data(bad: object) -> None:
    with pytest.raises(BNO055CalibrationProfileError):
        decode_calibration_profile_bytes(bad)  # type: ignore[arg-type]


def test_profile_is_frozen_slotted_and_canonical_round_trip_is_stable() -> None:
    original = profile()
    with pytest.raises(FrozenInstanceError):
        original.sensor_label = "changed"  # type: ignore[misc]
    assert not hasattr(original, "__dict__")
    encoded = serialize_calibration_profile(original)
    assert encoded.endswith(b"\n")
    assert serialize_calibration_profile(deserialize_calibration_profile(encoded)) == encoded


@pytest.mark.parametrize("bad", [True, 1, "", " ", " x", "x ", "x\n", "x" * 65])
def test_sensor_label_rejects_bad_values(bad: object) -> None:
    with pytest.raises(BNO055CalibrationProfileError, match="sensor_label"):
        validate_sensor_label(bad)


@pytest.mark.parametrize(
    "value",
    ["2026-09-08T12:34:56Z", "2026-09-08T12:34:56.1Z", "2026-09-08T12:34:56.123456Z"],
)
def test_created_at_accepts_documented_rfc3339_utc_subset(value: str) -> None:
    assert validate_created_at_utc(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-W37-2T12:34:56Z",
        "2026-09-08 12:34:56Z",
        "20260908T123456Z",
        "2026-09-08T12:34Z",
        "2026-09-08t12:34:56Z",
        "2026-09-08T12:34:56z",
        "2026-09-08T12:34:56+00:00",
        "2026-09-08T12:34:56.1234567Z",
        "2026-02-29T12:34:56Z",
        "2026-09-08T24:00:00Z",
    ],
)
def test_created_at_rejects_formats_outside_documented_subset(value: str) -> None:
    with pytest.raises(BNO055CalibrationProfileError, match="created_at_utc"):
        validate_created_at_utc(value)


def test_generated_utc_timestamp_uses_the_same_validated_contract() -> None:
    generated = utc_timestamp(
        datetime(2026, 9, 8, 20, 34, 56, 123456, tzinfo=timezone(timedelta(hours=8)))
    )
    assert generated == "2026-09-08T12:34:56.123456Z"
    assert validate_created_at_utc(generated) == generated


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        (("schema_name",), "wrong"),
        (("schema_version",), 2),
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("settings", "unit_sel"), 1),
        (("settings", "unit_sel"), 0.0),
        (("settings", "power_mode"), 1),
        (("settings", "power_mode"), 0.0),
        (("settings", "operation_mode"), 0),
        (("settings", "operation_mode"), 12.0),
        (("settings", "axis_map_config"), 0),
        (("settings", "axis_map_sign"), 8),
        (("identity", "chip_id"), 0),
        (("identity", "accelerometer_id"), True),
        (("identity", "software_revision"), "776"),
        (("created_at_utc",), "2026-09-08T00:00:00+08:00"),
    ],
)
def test_intrinsic_validation_rejects_unsupported_metadata(
    path: tuple[str, ...], bad: object
) -> None:
    value = encoded_dict()
    target: dict[str, object] = value
    for key in path[:-1]:
        target = target[key]  # type: ignore[assignment]
    target[path[-1]] = bad
    with pytest.raises(BNO055CalibrationProfileError):
        deserialize_calibration_profile(resign(value))


@pytest.mark.parametrize("section", ["profile", "identity", "settings", "calibration"])
@pytest.mark.parametrize("operation", ["missing", "extra"])
def test_missing_and_extra_fields_are_rejected(section: str, operation: str) -> None:
    value = encoded_dict()
    target = value if section == "profile" else value[section]
    assert isinstance(target, dict)
    if operation == "missing":
        target.pop(next(iter(target)))
    else:
        target["unexpected"] = 1
    with pytest.raises(BNO055CalibrationProfileError, match="fields mismatch"):
        deserialize_calibration_profile(resign(value))


def test_hash_and_metadata_tampering_are_rejected() -> None:
    value = encoded_dict()
    value["sensor_label"] = "another-sensor"
    with pytest.raises(BNO055CalibrationProfileError, match="payload_sha256 mismatch"):
        deserialize_calibration_profile(json.dumps(value).encode())


def test_duplicate_keys_nan_infinity_invalid_utf8_and_oversize_are_rejected() -> None:
    samples = [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b"\xff",
        b"{}" + b" " * (64 * 1024),
    ]
    for sample in samples:
        with pytest.raises(BNO055CalibrationProfileError):
            deserialize_calibration_profile(sample)


def test_constructor_rejects_supplied_wrong_hash() -> None:
    with pytest.raises(BNO055CalibrationProfileError, match="payload_sha256 mismatch"):
        profile(payload_sha256="0" * 64)


@pytest.mark.parametrize("bad_hash", ["", "A" * 64, 0, None])
def test_deserializer_requires_explicit_lowercase_sha256(bad_hash: object) -> None:
    value = encoded_dict()
    value["payload_sha256"] = bad_hash
    with pytest.raises(BNO055CalibrationProfileError, match="payload_sha256"):
        deserialize_calibration_profile(json.dumps(value).encode())
