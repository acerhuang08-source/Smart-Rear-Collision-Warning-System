from __future__ import annotations

import pytest

from rear_warning.sensors.bno055.models import BNO055Calibration
from rear_warning.sensors.bno055.registers import (
    decode_acceleration, decode_calibration, decode_euler, decode_quaternion,
    signed_int16_le, signed_int8,
)


@pytest.mark.parametrize(
    ("lsb", "msb", "expected"),
    [(0x00, 0x00, 0), (0x01, 0x00, 1), (0xff, 0x7f, 32767),
     (0x00, 0x80, -32768), (0xff, 0xff, -1), (0x34, 0x12, 0x1234)],
)
def test_signed_int16_le(lsb: int, msb: int, expected: int) -> None:
    assert signed_int16_le(lsb, msb) == expected


@pytest.mark.parametrize("value", [-1, 256, True, 1.5, "1"])
def test_signed_int16_rejects_invalid_bytes(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        signed_int16_le(value, 0)  # type: ignore[arg-type]


@pytest.mark.parametrize(("raw", "expected"), [(0, 0), (127, 127), (128, -128), (255, -1)])
def test_signed_int8(raw: int, expected: int) -> None:
    assert signed_int8(raw) == expected


def test_euler_scaling_and_order() -> None:
    result = decode_euler([0x10, 0, 0xF0, 0xFF, 0, 0x80])
    assert (result.heading, result.roll, result.pitch) == pytest.approx(
        (1.0, -1.0, -2048.0)
    )


def test_quaternion_scaling_and_order() -> None:
    result = decode_quaternion([0, 0x40, 0, 0xC0, 0, 0, 0, 0x20])
    assert (result.w, result.x, result.y, result.z) == pytest.approx((1, -1, 0, 0.5))


def test_acceleration_scaling_and_order() -> None:
    result = decode_acceleration([100, 0, 156, 255, 0, 0])
    assert (result.x, result.y, result.z) == pytest.approx((1, -1, 0))


@pytest.mark.parametrize("decoder,length", [(decode_euler, 5), (decode_quaternion, 7), (decode_acceleration, 4)])
def test_scaled_decoder_rejects_wrong_length(decoder: object, length: int) -> None:
    with pytest.raises(ValueError):
        decoder([0] * length)  # type: ignore[operator]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0x00, BNO055Calibration(0, 0, 0, 0)),
     (0xFF, BNO055Calibration(3, 3, 3, 3)),
     (0b10011100, BNO055Calibration(2, 1, 3, 0))],
)
def test_calibration_bit_fields(raw: int, expected: BNO055Calibration) -> None:
    assert decode_calibration(raw) == expected


@pytest.mark.parametrize("field", ["system", "gyroscope", "accelerometer", "magnetometer"])
@pytest.mark.parametrize("value", [-1, 4, True, 1.5])
def test_calibration_model_validates_each_field(field: str, value: object) -> None:
    values: dict[str, object] = dict(system=0, gyroscope=0, accelerometer=0, magnetometer=0)
    values[field] = value
    with pytest.raises((TypeError, ValueError)):
        BNO055Calibration(**values)  # type: ignore[arg-type]
