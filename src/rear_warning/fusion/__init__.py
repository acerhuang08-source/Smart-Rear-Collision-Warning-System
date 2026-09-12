"""Hardware-free feature extraction for future sensor fusion."""

from .distance_trend import DistanceTrendEstimator
from .models import (
    DistanceTrend,
    DistanceTrendReason,
    DistanceTrendThresholds,
)

__all__ = [
    "DistanceTrend",
    "DistanceTrendEstimator",
    "DistanceTrendReason",
    "DistanceTrendThresholds",
]
