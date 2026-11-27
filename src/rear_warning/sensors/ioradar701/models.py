"""Typed ioRadar701 frame models independent of UART I/O."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, TypeAlias


FRAME_SIZE = 12
FRAME_HEADER = 0xA5
STATUS_TYPE = 0x01
TARGET_TYPE = 0x02


@dataclass(frozen=True, slots=True)
class StatusFrame:
    """One checksum-validated status frame.

    Unknown bytes are retained in ``raw``.  The small integer fields are not
    converted to booleans because reserved values must remain observable.
    """

    raw: bytes
    check: int
    active: int
    error: int
    roll_count: int
    target_count: int
    kind: Literal["status"] = "status"

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe representation retaining the complete frame."""
        return {
            "kind": self.kind,
            "raw_hex": self.raw.hex(),
            "check": self.check,
            "active": self.active,
            "error": self.error,
            "roll_count": self.roll_count,
            "target_count": self.target_count,
        }


@dataclass(frozen=True, slots=True)
class TargetFrame:
    """One checksum-validated target frame using documented offset decoding."""

    raw: bytes
    target_id: int
    x_raw: int
    y_raw: int
    vx_raw: int
    energy: int
    x_m: float
    y_m: float
    vx_mps: float
    distance_m: float
    kind: Literal["target"] = "target"

    def as_dict(self) -> dict[str, object]:
        """Return raw and scaled values without inferring physical direction."""
        return {
            "kind": self.kind,
            "raw_hex": self.raw.hex(),
            "target_id": self.target_id,
            "x_raw": self.x_raw,
            "y_raw": self.y_raw,
            "vx_raw": self.vx_raw,
            "energy": self.energy,
            "x_m": self.x_m,
            "y_m": self.y_m,
            "vx_mps": self.vx_mps,
            "distance_m": self.distance_m,
        }


RadarFrame: TypeAlias = StatusFrame | TargetFrame


def decode_valid_frame(frame: bytes) -> RadarFrame:
    """Decode one already checksum-validated 12-byte protocol frame."""
    if len(frame) != FRAME_SIZE:
        raise ValueError(f"frame must contain exactly {FRAME_SIZE} bytes")
    if frame[0] != FRAME_HEADER:
        raise ValueError("frame does not start with 0xA5")
    if frame[1] == STATUS_TYPE:
        flags = frame[3]
        return StatusFrame(
            raw=frame,
            check=flags & 0x03,
            active=(flags >> 2) & 0x03,
            error=flags >> 4,
            roll_count=frame[4] & 0x03,
            target_count=frame[6],
        )
    if frame[1] == TARGET_TYPE:
        x_raw = int.from_bytes(frame[4:6], byteorder="big", signed=False)
        y_raw = int.from_bytes(frame[6:8], byteorder="big", signed=False)
        vx_raw = int.from_bytes(frame[8:10], byteorder="big", signed=False)
        x_m = x_raw * 0.01
        y_m = (y_raw - 1200) * 0.01
        vx_mps = (vx_raw - 6500) * 0.01
        return TargetFrame(
            raw=frame,
            target_id=frame[3],
            x_raw=x_raw,
            y_raw=y_raw,
            vx_raw=vx_raw,
            energy=frame[10],
            x_m=x_m,
            y_m=y_m,
            vx_mps=vx_mps,
            distance_m=math.hypot(x_m, y_m),
        )
    raise ValueError(f"unsupported frame type 0x{frame[1]:02x}")
