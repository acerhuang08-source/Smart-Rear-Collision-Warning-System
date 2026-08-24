"""Tests for the pure TFMini Plus standard UART packet parser."""

from __future__ import annotations

import pytest

from rear_warning.sensors.tfmini_plus import (
    TFMiniPlusMeasurement,
    TFMiniPlusParser,
)


def _make_frame(
    *,
    distance_cm: int = 300,
    strength: int = 1200,
    raw_temperature: int = 2248,
) -> bytes:
    """Build an independent standard-format fixture with a valid checksum."""
    frame_without_checksum = (
        b"\x59\x59"
        + distance_cm.to_bytes(2, byteorder="little", signed=False)
        + strength.to_bytes(2, byteorder="little", signed=False)
        + raw_temperature.to_bytes(2, byteorder="little", signed=False)
    )
    checksum = sum(frame_without_checksum) & 0xFF
    return frame_without_checksum + bytes((checksum,))


def test_parses_valid_standard_packet() -> None:
    parser = TFMiniPlusParser()

    measurements = parser.feed(bytes.fromhex("59 59 2C 01 B0 04 C8 08 63"))

    assert measurements == [
        TFMiniPlusMeasurement(
            distance_cm=300,
            strength=1200,
            chip_temperature_c=25.0,
        )
    ]
    assert parser.buffered_bytes == 0


def test_rejects_packet_with_bad_checksum() -> None:
    parser = TFMiniPlusParser()
    packet = bytearray(_make_frame())
    packet[-1] = (packet[-1] + 1) & 0xFF

    assert parser.feed(packet) == []


def test_rejects_packet_with_wrong_start_marker() -> None:
    parser = TFMiniPlusParser()
    packet = bytearray(_make_frame())
    packet[0:2] = b"\x58\x58"
    packet[-1] = sum(packet[:8]) & 0xFF

    assert parser.feed(packet) == []


def test_buffers_incomplete_packet() -> None:
    parser = TFMiniPlusParser()
    packet = _make_frame()

    assert parser.feed(packet[:8]) == []
    assert parser.buffered_bytes == 8


def test_skips_noise_before_packet() -> None:
    parser = TFMiniPlusParser()
    packet = _make_frame(distance_cm=450)

    measurements = parser.feed(b"\x00\xff\x10\x59\x00noise" + packet)

    assert [measurement.distance_cm for measurement in measurements] == [450]


def test_decodes_multiple_packets_from_one_chunk() -> None:
    parser = TFMiniPlusParser()
    first = _make_frame(distance_cm=100, strength=200)
    second = _make_frame(distance_cm=1200, strength=5000)

    measurements = parser.feed(first + second)

    assert [(item.distance_cm, item.strength) for item in measurements] == [
        (100, 200),
        (1200, 5000),
    ]


@pytest.mark.parametrize("split_index", range(1, 9))
def test_decodes_packet_split_at_every_boundary(split_index: int) -> None:
    parser = TFMiniPlusParser()
    packet = _make_frame(distance_cm=321)

    assert parser.feed(packet[:split_index]) == []
    measurements = parser.feed(packet[split_index:])

    assert [measurement.distance_cm for measurement in measurements] == [321]
    assert parser.buffered_bytes == 0


def test_resynchronizes_after_bad_packet() -> None:
    parser = TFMiniPlusParser()
    bad_packet = bytearray(_make_frame(distance_cm=111))
    bad_packet[-1] ^= 0xFF
    good_packet = _make_frame(distance_cm=222)

    measurements = parser.feed(bad_packet + good_packet)

    assert [measurement.distance_cm for measurement in measurements] == [222]


def test_parses_distance_as_unsigned_little_endian() -> None:
    parser = TFMiniPlusParser()

    measurement = parser.feed(_make_frame(distance_cm=0x1234))[0]

    assert measurement.distance_cm == 0x1234


def test_parses_strength_as_unsigned_little_endian() -> None:
    parser = TFMiniPlusParser()

    measurement = parser.feed(_make_frame(strength=0xABCD))[0]

    assert measurement.strength == 0xABCD


def test_parses_official_chip_temperature_conversion() -> None:
    parser = TFMiniPlusParser()

    measurement = parser.feed(_make_frame(raw_temperature=2248))[0]

    assert measurement.chip_temperature_c == 25.0


def test_empty_input_returns_no_measurements() -> None:
    parser = TFMiniPlusParser()

    assert parser.feed(b"") == []
    assert parser.buffered_bytes == 0


@pytest.mark.parametrize(
    "packet_type",
    [bytearray, memoryview],
)
def test_accepts_supported_mutable_and_view_inputs(
    packet_type: type[bytearray] | type[memoryview],
) -> None:
    parser = TFMiniPlusParser()

    measurements = parser.feed(packet_type(_make_frame(distance_cm=123)))

    assert [measurement.distance_cm for measurement in measurements] == [123]


@pytest.mark.parametrize(
    "noise",
    [
        b"\x00",
        b"\xff\x00\x01\x02",
        b"\x59",
        b"\x59" * 32,
        bytes(range(256)),
    ],
)
def test_arbitrary_noise_does_not_raise(noise: bytes) -> None:
    parser = TFMiniPlusParser()

    assert parser.feed(noise) == []


def test_retains_partial_second_packet_for_next_feed() -> None:
    parser = TFMiniPlusParser()
    first = _make_frame(distance_cm=10)
    second = _make_frame(distance_cm=20)

    first_measurements = parser.feed(first + second[:4])
    second_measurements = parser.feed(second[4:])

    assert [item.distance_cm for item in first_measurements] == [10]
    assert [item.distance_cm for item in second_measurements] == [20]


def test_reset_discards_buffered_partial_packet() -> None:
    parser = TFMiniPlusParser()
    parser.feed(_make_frame()[:4])

    parser.reset()

    assert parser.buffered_bytes == 0
    assert parser.feed(_make_frame()[4:]) == []
