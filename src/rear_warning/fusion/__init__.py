"""Hardware-free feature extraction for future sensor fusion."""

from .distance_trend import DistanceTrendEstimator
from .models import (
    DistanceTrend,
    DistanceTrendReason,
    DistanceTrendThresholds,
    OwnMotionEvidence,
    OwnMotionReason,
    OwnMotionThresholds,
)
from .own_motion import InconsistentMotionSampleError, OwnMotionEstimator

__all__ = [
    "DistanceTrend",
    "DistanceTrendEstimator",
    "DistanceTrendReason",
    "DistanceTrendThresholds",
    "InconsistentMotionSampleError",
    "OwnMotionEstimator",
    "OwnMotionEvidence",
    "OwnMotionReason",
    "OwnMotionThresholds",
]
