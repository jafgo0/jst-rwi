"""Ensembles and uncertainty: summary statistics, member frequencies, attaching spread.

Public API (see docs/science/ensemble.md for methods and validation status):

* :func:`summarise` - per-valid-time distribution of an ensemble;
* :func:`probability_above` / :func:`probability_below` - member frequencies
  (NOT calibrated probabilities), with :func:`probability_note` for the UI;
* :func:`attach_uncertainty` - add ensemble spread to a series without changing values;
* :func:`spread_skill_note` - one-sentence reading of a summary's spread.
"""

from rwi.ensemble.attach import attach_uncertainty, spread_skill_note
from rwi.ensemble.probability import (
    MemberFrequency,
    irradiance_stays_above,
    member_frequency_throughout,
    probability_above,
    probability_below,
    probability_in_sector,
    probability_note,
)
from rwi.ensemble.summary import summarise

__all__ = [
    "MemberFrequency",
    "attach_uncertainty",
    "irradiance_stays_above",
    "member_frequency_throughout",
    "probability_above",
    "probability_below",
    "probability_in_sector",
    "probability_note",
    "spread_skill_note",
    "summarise",
]
