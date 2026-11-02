"""Default significance thresholds for model disagreement.

THESE ARE ENGINEERING JUDGEMENTS, NOT PHYSICS. No law of nature says that a
2 degC spread between two models is "significant". Each number below is the
point at which we think a race strategist should stop trusting a single model's
number and hold a margin, chosen from what the quantity does to the car. They
have not been tuned against any verification history (there is none yet) and
must be revisited once `rwi.verification` produces real spread/error
statistics. Every value is overridable per call (`significance_thresholds=`).

Each threshold carries its justification in :class:`ThresholdBasis` so that the
UI and `docs/science/fusion.md` quote the same words.

Arithmetic quoted in the justifications uses explicit assumptions (stated in
the text); none of them is a measured property of the team's vehicle.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "DEFAULT_SIGNIFICANCE_THRESHOLDS",
    "THRESHOLD_BASIS",
    "ThresholdBasis",
    "resolve_threshold",
]


@dataclass(frozen=True)
class ThresholdBasis:
    """A default threshold, its unit and why it was chosen."""

    value: float
    unit: str
    justification: str
    status: str = "engineering judgement - not derived from verification data"


_BASES: dict[str, ThresholdBasis] = {
    "ghi": ThresholdBasis(
        150.0,
        "W/m2",
        "PV output is close to linear in irradiance, so a spread in GHI is a spread in array "
        "output. Taking 1000 W/m2 as a round clear-sky midday reference, 150 W/m2 is about 15 % "
        "of midday array output - several times the 'few percent' that would change an hourly "
        "energy estimate enough to matter. Deliberately set well above the tens of W/m2 by which "
        "two healthy models routinely differ, so that a flag means 'hold a margin', not 'any "
        "difference'.",
    ),
    "dni": ThresholdBasis(
        200.0,
        "W/m2",
        "DNI is more volatile than GHI (thin cloud removes it much faster than it removes "
        "global irradiance), so its threshold is looser. 200 W/m2 is about 20 % of a 1000 W/m2 "
        "clear-sky reference; for a near-flat array only the cosine-projected part of it reaches "
        "the cells, so DNI disagreement matters less directly than GHI disagreement.",
    ),
    "cloud_cover": ThresholdBasis(
        30.0,
        "percent",
        "Cloud cover is the dominant driver of irradiance but the relation is strongly "
        "non-linear (empirical cloud-to-irradiance relations lose little below roughly half "
        "cover and a great deal above roughly 70 %), so a fixed percentage-point spread is a "
        "blunt screen: 30 points between 70 % and 100 % cover can be a large irradiance "
        "difference, between 0 % and 30 % almost none. 30 points is the size of a disagreement "
        "that, at the cloudy end, clearly separates 'sunny day' from 'overcast day'. Models also "
        "define total cloud differently, so smaller spreads are not informative.",
    ),
    "temperature_2m": ThresholdBasis(
        2.0,
        "degC",
        "Direct effect on the car is small: with a typical crystalline-silicon power coefficient "
        "of about -0.3 %/K (check the team's cell datasheet) 2 K changes cell power by about "
        "0.6 %, and air density (hence aerodynamic drag) by about 0.7 %, both below the 'few "
        "percent' level. The threshold is therefore a diagnostic more than an energy criterion: "
        "in a dry outback airmass a 2 K disagreement at 2 m usually means the models differ on "
        "cloud, a front or a wind change, i.e. on something that does matter.",
    ),
    "wind_speed_10m": ThresholdBasis(
        3.0,
        "m/s",
        "Aerodynamic drag goes with the square of airspeed. Assuming cruise at 25 m/s (90 km/h; "
        "an illustrative number, not the team's plan), a 3 m/s difference in the along-track "
        "wind component moves airspeed by 12 % and drag by about 25 %. Only part of any "
        "10 m wind spread lies along the track, and the car sees wind near 1 m height, "
        "so 3 m/s is a lenient screen; a tighter value is defensible for headwind-sensitive "
        "legs and can be passed as an override.",
    ),
    "wind_gusts_10m": ThresholdBasis(
        5.0,
        "m/s",
        "Gusts matter for handling and crosswind stability rather than for mean energy use. "
        "Models represent gusts with a parameterisation and differ more on them than on mean "
        "wind, so the threshold is larger than for mean wind speed.",
    ),
    "wind_direction_10m": ThresholdBasis(
        45.0,
        "deg",
        "The along-track wind component is proportional to the cosine of the angle between "
        "wind and heading; a 45 degree disagreement can turn a crosswind into a quartering "
        "headwind (cos 45 = 0.71 of the full speed). The spread is circular (smallest arc "
        "containing every model). Direction disagreement is meaningless in light wind and this "
        "phase does not condition on speed: read it together with wind_speed_10m.",
    ),
    "precipitation": ThresholdBasis(
        1.0,
        "mm per step",
        "Rain removes most direct irradiance and wets the road. Models differ mostly on whether "
        "and where it rains rather than on amounts, so a spread of 1 mm in a step (hourly from "
        "the Open-Meteo adapter) separates 'dry' from 'measurable rain' in at least one model. "
        "The unit is mm per provider step, so the threshold does not transfer to other step "
        "lengths.",
    ),
}

THRESHOLD_BASIS: Mapping[str, ThresholdBasis] = MappingProxyType(_BASES)
"""Per-variable default, unit and justification (read-only)."""

DEFAULT_SIGNIFICANCE_THRESHOLDS: Mapping[str, float] = MappingProxyType(
    {name: basis.value for name, basis in _BASES.items()}
)
"""Per-variable default spread (max minus min across models) above which a
disagreement is flagged significant. Engineering judgements; see module docstring."""


def resolve_threshold(variable: str, overrides: Mapping[str, float] | None = None) -> float:
    """Threshold for ``variable``: the override if given, else the default.

    Raises ``ValueError`` when neither exists. A threshold is never invented for
    a variable nobody has thought about.
    """
    if overrides is not None and variable in overrides:
        value = float(overrides[variable])
    elif variable in DEFAULT_SIGNIFICANCE_THRESHOLDS:
        value = DEFAULT_SIGNIFICANCE_THRESHOLDS[variable]
    else:
        raise ValueError(
            f"no significance threshold for {variable!r}: pass significance_thresholds="
            f"{{{variable!r}: ...}} (defaults exist for {sorted(DEFAULT_SIGNIFICANCE_THRESHOLDS)})"
        )
    if not math.isfinite(value) or value < 0.0:
        raise ValueError(f"significance threshold for {variable!r} must be finite and >= 0")
    return value
