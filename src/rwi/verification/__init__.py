"""Forecast verification: scores, skill against persistence, and bias correction.

Public API (see ``docs/CONTRACTS.md`` section 3b). Nothing here imports
``rwi.archive``; pairs come from anything satisfying ``ForecastArchive``.
"""

from rwi.verification.calibration import apply_bias_correction, bias_correction
from rwi.verification.metrics import (
    DEFAULT_LEAD_TIME_BUCKETS_S,
    circular_difference,
    circular_mean,
    is_circular_variable,
    persistence_baseline,
    score_pairs,
    skill_score,
)
from rwi.verification.report import verify

__all__ = [
    "DEFAULT_LEAD_TIME_BUCKETS_S",
    "apply_bias_correction",
    "bias_correction",
    "circular_difference",
    "circular_mean",
    "is_circular_variable",
    "persistence_baseline",
    "score_pairs",
    "skill_score",
    "verify",
]
