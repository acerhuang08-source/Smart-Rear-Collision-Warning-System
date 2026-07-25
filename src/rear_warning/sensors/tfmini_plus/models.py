"""Data models produced by the TFMini Plus packet parser."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TFMiniPlusMeasurement:
    """A decoded TFMini Plus standard UART measurement.

    Attributes:
        distance_cm: Distance field in centimetres.
        strength: Signal-strength field reported by the sensor.
        chip_temperature_c: Sensor chip temperature in degrees Celsius.
    """

    distance_cm: int
    strength: int
    chip_temperature_c: float
