"""Incremental, resynchronizing ioRadar701 byte-stream parser."""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    FRAME_HEADER,
    FRAME_SIZE,
    STATUS_TYPE,
    TARGET_TYPE,
    RadarFrame,
    StatusFrame,
    decode_valid_frame,
)


@dataclass(frozen=True, slots=True)
class ParserStatistics:
    """Cumulative parser quality counters."""

    input_bytes: int
    status_frames: int
    target_frames: int
    checksum_failures: int
    sync_discarded_bytes: int
    trailing_bytes: int

    @property
    def valid_frames(self) -> int:
        """Return the number of checksum-valid supported frames."""
        return self.status_frames + self.target_frames


class IoRadar701StreamParser:
    """Parse arbitrary chunks while retaining any incomplete final frame."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._input_bytes = 0
        self._status_frames = 0
        self._target_frames = 0
        self._checksum_failures = 0
        self._sync_discarded_bytes = 0

    @property
    def pending_bytes(self) -> bytes:
        """Return the currently retained incomplete candidate."""
        return bytes(self._buffer)

    @property
    def statistics(self) -> ParserStatistics:
        """Return an immutable counter snapshot."""
        return ParserStatistics(
            input_bytes=self._input_bytes,
            status_frames=self._status_frames,
            target_frames=self._target_frames,
            checksum_failures=self._checksum_failures,
            sync_discarded_bytes=self._sync_discarded_bytes,
            trailing_bytes=len(self._buffer),
        )

    def feed(self, data: bytes | bytearray | memoryview) -> list[RadarFrame]:
        """Consume one byte chunk and return all newly completed valid frames."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        chunk = bytes(data)
        self._input_bytes += len(chunk)
        self._buffer.extend(chunk)
        decoded: list[RadarFrame] = []

        while self._buffer:
            header_index = self._buffer.find(FRAME_HEADER)
            if header_index < 0:
                self._discard(len(self._buffer))
                break
            if header_index:
                self._discard(header_index)
            if len(self._buffer) < 2:
                break
            if self._buffer[1] not in (STATUS_TYPE, TARGET_TYPE):
                self._discard(1)
                continue
            if len(self._buffer) < FRAME_SIZE:
                break

            candidate = bytes(self._buffer[:FRAME_SIZE])
            if candidate[FRAME_SIZE - 1] != sum(candidate[: FRAME_SIZE - 1]) & 0xFF:
                self._checksum_failures += 1
                # Drop only the current header so an embedded next header can
                # be found without assuming the damaged candidate's length.
                self._discard(1)
                continue

            del self._buffer[:FRAME_SIZE]
            frame = decode_valid_frame(candidate)
            if isinstance(frame, StatusFrame):
                self._status_frames += 1
            else:
                self._target_frames += 1
            decoded.append(frame)

        return decoded

    def _discard(self, count: int) -> None:
        """Discard bytes that cannot begin a valid frame and count them."""
        del self._buffer[:count]
        self._sync_discarded_bytes += count
