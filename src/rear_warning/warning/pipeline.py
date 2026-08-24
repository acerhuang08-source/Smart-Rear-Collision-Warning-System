"""One-poll orchestration for the minimal rear-warning chain."""

from __future__ import annotations

from typing import Protocol

from rear_warning.sensors.tfmini_plus import TFMiniPlusMeasurement

from .controller import WarningController
from .models import WarningState


class MeasurementDevice(Protocol):
    """Measurement source required by :class:`WarningPipeline`."""

    def read_measurements(self) -> list[TFMiniPlusMeasurement]:
        """Return measurements completed by one device read."""
        ...


class WarningPipeline:
    """Connect one device read to policy classification and LED output."""

    def __init__(
        self, device: MeasurementDevice, controller: WarningController
    ) -> None:
        self._device = device
        self._controller = controller

    def poll(self) -> list[WarningState]:
        """Process one read; an empty read displays a sensor fault."""
        measurements = self._device.read_measurements()
        if not measurements:
            return [self._controller.process(None)]
        return [
            self._controller.process(measurement)
            for measurement in measurements
        ]
