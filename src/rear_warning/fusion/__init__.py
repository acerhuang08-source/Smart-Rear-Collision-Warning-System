"""Hardware-free feature extraction for future sensor fusion."""

from .adapter import InconsistentFusionSampleError, RelativeMotionFusionAdapter
from .classifier import (
    InconsistentClassifierInputError,
    RelativeMotionClassifier,
)
from .distance_trend import DistanceTrendEstimator
from .models import (
    DistanceTrend,
    DistanceTrendReason,
    DistanceTrendThresholds,
    OwnMotionEvidence,
    OwnMotionReason,
    OwnMotionThresholds,
    RelativeMotionAssessment,
    RelativeMotionClass,
    RelativeMotionReason,
    RelativeMotionThresholds,
)
from .own_motion import InconsistentMotionSampleError, OwnMotionEstimator
from .radar_adapter import RadarFusionInput, RadarRelativeMotionAdapter

__all__ = [
    "DistanceTrend",
    "DistanceTrendEstimator",
    "DistanceTrendReason",
    "DistanceTrendThresholds",
    "InconsistentClassifierInputError",
    "InconsistentFusionSampleError",
    "InconsistentMotionSampleError",
    "OwnMotionEstimator",
    "OwnMotionEvidence",
    "OwnMotionReason",
    "OwnMotionThresholds",
    "RelativeMotionAssessment",
    "RelativeMotionClass",
    "RelativeMotionClassifier",
    "RelativeMotionFusionAdapter",
    "RelativeMotionReason",
    "RelativeMotionThresholds",
    "RadarFusionInput",
    "RadarRelativeMotionAdapter",
]
