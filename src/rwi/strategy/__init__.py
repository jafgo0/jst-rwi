"""Race strategy: candidate scenarios, a constant-speed optimiser, recommendations.

Decision support only. Nothing in this package commands the vehicle, and every
recommendation it produces is advisory and requires human review.

Validation status: implemented, not validated.
"""

from __future__ import annotations

from rwi.strategy.scenarios import (
    MEDIUM_CONFIDENCE_MARGIN_PP,
    SAFETY_NOTES,
    ScenarioEnsembleResult,
    ScenarioSensitivity,
    ScenarioSpec,
    SpeedOptimisation,
    SpeedProfile,
    compare_scenarios,
    evaluate_scenario,
    optimal_constant_speed,
    recommend,
    scenario_cvar,
    scenario_sensitivity,
    standard_candidates,
    weather_aware_profile,
)
from rwi.strategy.segments import (
    SegmentConditions,
    attach_generation,
    scale_generation,
    scale_wind,
    segments_from_distance,
    segments_from_trajectory,
    total_distance_m,
)

__all__ = [
    "MEDIUM_CONFIDENCE_MARGIN_PP",
    "SAFETY_NOTES",
    "ScenarioEnsembleResult",
    "ScenarioSensitivity",
    "ScenarioSpec",
    "SegmentConditions",
    "SpeedOptimisation",
    "SpeedProfile",
    "attach_generation",
    "compare_scenarios",
    "evaluate_scenario",
    "optimal_constant_speed",
    "recommend",
    "scale_generation",
    "scale_wind",
    "scenario_cvar",
    "scenario_sensitivity",
    "segments_from_distance",
    "segments_from_trajectory",
    "standard_candidates",
    "total_distance_m",
    "weather_aware_profile",
]
