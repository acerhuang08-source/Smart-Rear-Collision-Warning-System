"""Incremental parser for Benewake TFMini Plus standard UART packets.

Protocol source:
    Benewake, "TFmini Plus User Manual", REV 01/04/2024, sections 5.2-5.4.
    Official PDF:
    https://en.benewake.com/uploadfiles/2025/04/20250430175221028.pdf

Confirmed standard-output format:
    Byte 0..1: frame header, 0x59 0x59
    Byte 2..3: distance, unsigned little-endian
    Byte 4..5: signal strength, unsigned little-endian
    Byte 6..7: chip temperature, unsigned little-endian
    Byte 8: lower 8 bits of the sum of bytes 0..7

The official temperature conversion is ``raw / 8 - 256`` degrees Celsius.

Assumptions and scope:
    The sensor is configured for the default "standard 9 bytes (cm)" output.
    The parser does not support the optional millimetre, Pixhawk, or text formats.
    It consumes bytes only and intentionally has no UART or pyserial dependency.
"""

from __future__ import annotations

from .models import TFMiniPlusMeasurement

_FRAME_HEADER = b"\x59\x59"
_FRAME_LENGTH = 9


class TFMiniPlusParser:
    """Incrementally decode validated TFMini Plus standard UART packets.

    Arbitrary byte chunks may be supplied to :meth:`feed`. Incomplete data is
    retained for the next call. Noise and invalid packets are discarded while
    the parser searches for the next valid frame header.
    """

    def __init__(self) -> None:
        """Create a parser with an empty receive buffer."""
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        """Return the number of bytes retained for a possible future frame."""
        return len(self._buffer)

    def reset(self) -> None:
        """Discard all buffered partial data."""
        self._buffer.clear()

    def feed(
        self, data: bytes | bytearray | memoryview
    ) -> list[TFMiniPlusMeasurement]:
        """Consume a byte chunk and return every complete valid measurement.

        Empty chunks and arbitrary byte noise are safe. Packets with an invalid
        header or checksum are ignored, and parsing resumes at the next possible
        ``0x59 0x59`` header.

        Args:
            data: A bytes-like chunk from any source.

        Returns:
            All valid measurements completed by this chunk, in stream order.

        Raises:
            TypeError: If ``data`` is not bytes, bytearray, or memoryview.
        """
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes, bytearray, or memoryview")

        if not data:
            return []

        chunk = data.tobytes() if isinstance(data, memoryview) else data
        self._buffer.extend(chunk)
        measurements: list[TFMiniPlusMeasurement] = []

        while True:
            header_index = self._buffer.find(_FRAME_HEADER)
            if header_index < 0:
                self._discard_noise_without_losing_partial_header()
                break

            if header_index > 0:
                del self._buffer[:header_index]

            if len(self._buffer) < _FRAME_LENGTH:
                break

            frame = bytes(self._buffer[:_FRAME_LENGTH])
            if _is_valid_standard_frame(frame):
                measurements.append(_decode_standard_frame(frame))
                del self._buffer[:_FRAME_LENGTH]
                continue

            # Drop only the first candidate-header byte. Keeping the remainder
            # allows a valid header inside or immediately after a bad frame to
            # be found on the next iteration.
            del self._buffer[0]

        return measurements

    def _discard_noise_without_losing_partial_header(self) -> None:
        """Discard noise while retaining a trailing first-header byte."""
        if self._buffer.endswith(_FRAME_HEADER[:1]):
            del self._buffer[:-1]
        else:
            self._buffer.clear()


def _is_valid_standard_frame(frame: bytes) -> bool:
    """Return whether a complete frame has the official header and checksum."""
    return (
        len(frame) == _FRAME_LENGTH
        and frame.startswith(_FRAME_HEADER)
        and (sum(frame[:8]) & 0xFF) == frame[8]
    )


def _decode_standard_frame(frame: bytes) -> TFMiniPlusMeasurement:
    """Decode a frame already validated by ``_is_valid_standard_frame``."""
    distance_cm = int.from_bytes(frame[2:4], byteorder="little", signed=False)
    strength = int.from_bytes(frame[4:6], byteorder="little", signed=False)
    raw_temperature = int.from_bytes(
        frame[6:8], byteorder="little", signed=False
    )
    chip_temperature_c = raw_temperature / 8.0 - 256.0

    return TFMiniPlusMeasurement(
        distance_cm=distance_cm,
        strength=strength,
        chip_temperature_c=chip_temperature_c,
    )
