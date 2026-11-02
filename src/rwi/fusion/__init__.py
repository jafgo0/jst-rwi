"""Multi-model comparison: where models disagree, and which (if any) has earned preference.

No blended forecast is produced here. See docs/science/fusion.md.
"""

from rwi.fusion.compare import (
    DEFAULT_ALIGNMENT_TOLERANCE_S,
    KNOWN_NATIVE_STEP_S,
    compare_models,
    disagreements,
)
from rwi.fusion.selection import (
    MEANINGFUL_SAMPLE_COUNT,
    ModelWeights,
    model_weights,
    preferred_model,
)
from rwi.fusion.thresholds import (
    DEFAULT_SIGNIFICANCE_THRESHOLDS,
    THRESHOLD_BASIS,
    ThresholdBasis,
    resolve_threshold,
)

__all__ = [
    "DEFAULT_ALIGNMENT_TOLERANCE_S",
    "DEFAULT_SIGNIFICANCE_THRESHOLDS",
    "KNOWN_NATIVE_STEP_S",
    "MEANINGFUL_SAMPLE_COUNT",
    "THRESHOLD_BASIS",
    "ModelWeights",
    "ThresholdBasis",
    "compare_models",
    "disagreements",
    "model_weights",
    "preferred_model",
    "resolve_threshold",
]
