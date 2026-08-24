"""Shared warning-state model."""

from __future__ import annotations

from enum import Enum


class WarningState(Enum):
    """State produced by a warning policy and consumed by output drivers."""

    SAFE = "safe"
    WARNING = "warning"
    DANGER = "danger"
    SENSOR_FAULT = "sensor_fault"
