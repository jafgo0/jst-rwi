"""Comparisons against ground truth: clear-sky check and hourly bias (ADR 0017).

Two building blocks, both pure (no network):

* :func:`clear_sky_check` finds clear minutes in quality-controlled BSRN 1-minute GHI
  with pvlib's ``detect_clearsky`` (Reno & Hansen 2016) and measures how far the
  platform's clear-sky model (Ineichen-Perez, with a given Linke turbidity) is from
  the pyranometer and pyrheliometer on exactly those minutes. Too low a model on clear
  minutes means the turbidity is too high, and the other way round.
* :func:`bias_statistics` pairs an hourly candidate series (ERA5, MERRA-2, a forecast)
  with an hourly OBSERVATION series (BSRN) at identical valid times during daylight and
  reports bias, MAE and RMSE.

Every result is ``DataKind.DERIVED`` with provider ``rwi`` and names both inputs.
"""

import math
from collections.abc import Sequence
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pvlib
from pydantic import BaseModel, ConfigDict

from rwi.contracts import WeatherSeries
from rwi.core.geo import GeoPoint, geodesic_distance_m
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import utc_now
from rwi.core.variables import unit_for
from rwi.solar import clear_sky_series, solar_positions

# Reno & Hansen (2016), Renewable Energy 90, 520-531: 10-minute windows on 1-minute data.
CLEAR_SKY_WINDOW_MIN = 10
_HOUR_S = 3600.0


class Agreement(BaseModel):
    """Candidate minus reference over paired samples, in the variable's unit."""

    model_config = ConfigDict(frozen=True)

    variable: str
    unit: str
    n: int
    mean_reference: float
    mean_candidate: float
    mean_bias: float
    mae: float
    rmse: float
    relative_bias: float | None
    energy_ratio: float | None


class ClearSkyCheck(BaseModel):
    """Clear-sky model against measured clear minutes at one station (DERIVED)."""

    model_config = ConfigDict(frozen=True)

    point: GeoPoint
    first_utc: datetime
    last_utc: datetime
    n_minutes: int
    n_lit_minutes: int
    n_clear_minutes: int
    ghi: Agreement | None
    dni: Agreement | None
    linke_turbidity_note: str
    provenance: Provenance


class BiasStatistics(BaseModel):
    """Hourly candidate against an hourly observation during daylight (DERIVED)."""

    model_config = ConfigDict(frozen=True)

    candidate: str
    reference: str
    candidate_point: GeoPoint
    reference_point: GeoPoint
    separation_m: float
    first_utc: datetime | None
    last_utc: datetime | None
    n_common_steps: int
    agreement: Agreement | None
    provenance: Provenance


def _agreement(variable: str, candidate: np.ndarray, reference: np.ndarray) -> Agreement | None:
    if candidate.size == 0:
        return None
    diff = candidate - reference
    total_ref = float(reference.sum())
    mean_ref = float(reference.mean())
    return Agreement(
        variable=variable,
        unit=unit_for(variable),
        n=int(candidate.size),
        mean_reference=mean_ref,
        mean_candidate=float(candidate.mean()),
        mean_bias=float(diff.mean()),
        mae=float(np.abs(diff).mean()),
        rmse=float(math.sqrt(float((diff**2).mean()))),
        relative_bias=float(diff.mean()) / mean_ref if mean_ref > 0 else None,
        energy_ratio=float(candidate.sum()) / total_ref if total_ref > 0 else None,
    )


def _label(series: WeatherSeries) -> str:
    model = f"/{series.model_name}" if series.model_name else ""
    return f"{series.provider}{model} ({series.dataset or 'n/a'})"


def clear_sky_check(
    minutes: pd.DataFrame,
    point: GeoPoint,
    *,
    linke_turbidity: float | Sequence[float] | None = None,
    linke_turbidity_note: str | None = None,
) -> ClearSkyCheck:
    """Measured (reference) vs Ineichen-Perez (candidate) GHI and DNI on clear minutes.

    ``minutes`` is :func:`rwi.providers.bsrn.quality_control` output: a 1-minute index
    labelling the start of each minute and columns ``ghi`` and ``dni`` in W/m2, NaN where
    missing or rejected. Gaps in the index are filled with NaN so the detection windows
    stay contiguous. Clear minutes must be lit (apparent elevation > 0), have a measured
    GHI and pass ``detect_clearsky`` with its published defaults.
    """
    if minutes.empty:
        raise ValueError("no minutes to check")
    index = pd.DatetimeIndex(minutes.index)
    if index.tz is None:
        raise ValueError("minute index must be tz-aware UTC")
    full = pd.date_range(index.min(), index.max(), freq="1min")
    data = minutes.reindex(full)
    mid = full + pd.Timedelta(seconds=30)
    if linke_turbidity is not None and not isinstance(linke_turbidity, int | float):
        values = list(linke_turbidity)
        if len(values) != len(minutes):
            raise ValueError("one linke_turbidity per minute row is required")
        linke_turbidity = list(
            pd.Series(values, index=index).reindex(full).ffill().bfill().to_numpy(dtype=float)
        )
    model = clear_sky_series(
        point,
        [t.to_pydatetime() for t in mid],
        linke_turbidity=linke_turbidity,
        linke_turbidity_note=linke_turbidity_note,
    )
    cs_ghi = pd.Series([c.ghi_w_m2 for c in model], index=full, dtype=float)
    cs_dni = np.array([c.dni_w_m2 for c in model], dtype=float)
    lit = np.array([(c.solar_elevation_deg or 0.0) > 0.0 for c in model])
    ghi = data["ghi"].astype(float)
    detected = pvlib.clearsky.detect_clearsky(ghi, cs_ghi, window_length=CLEAR_SKY_WINDOW_MIN)
    clear = np.asarray(detected, dtype=bool) & lit & ghi.notna().to_numpy()
    dni = data["dni"].astype(float).to_numpy()
    dni_ok = clear & ~np.isnan(dni)
    note = linke_turbidity_note or (
        "pvlib monthly Linke turbidity climatology" if linke_turbidity is None else "given"
    )
    first, last = full[0].to_pydatetime(), full[-1].to_pydatetime() + timedelta(minutes=1)
    return ClearSkyCheck(
        point=point,
        first_utc=first,
        last_utc=last,
        n_minutes=len(full),
        n_lit_minutes=int(lit.sum()),
        n_clear_minutes=int(clear.sum()),
        ghi=_agreement("ghi", cs_ghi.to_numpy()[clear], ghi.to_numpy()[clear]),
        dni=_agreement("dni", cs_dni[dni_ok], dni[dni_ok]),
        linke_turbidity_note=note,
        provenance=Provenance(
            provider="rwi",
            dataset="validation/clear_sky_check",
            kind=DataKind.DERIVED,
            retrieved_at_utc=utc_now(),
            valid_time_utc=last,
            point=point,
            temporal_resolution_s=60.0,
            model_name="ineichen-perez",
            processing=(
                f"clear minutes by pvlib detect_clearsky (Reno & Hansen 2016, "
                f"{CLEAR_SKY_WINDOW_MIN}-min windows, default thresholds) on measured GHI; "
                f"candidate = clear-sky model at minute midpoint, reference = measurement; "
                f"Linke turbidity: {note}"
            ),
            notes="Reference is BSRN 1-minute data after the physically possible limits.",
        ),
    )


def bias_statistics(
    candidate: WeatherSeries, reference: WeatherSeries, variable: str = "ghi"
) -> BiasStatistics:
    """Candidate minus observation for ``variable`` at identical hourly times in daylight.

    Both series must be hourly means labelled at the end of the hour; the reference
    must be ``DataKind.OBSERVATION``. A pair counts when both values are usable and the
    apparent solar elevation at the reference point is above 0 at the interval midpoint.
    """
    if reference.kind is not DataKind.OBSERVATION:
        raise ValueError(f"reference must be an observation, got {reference.kind.value}")
    for name, s in (("candidate", candidate), ("reference", reference)):
        if s.temporal_resolution_s != _HOUR_S:
            raise ValueError(f"{name} must be hourly, got {s.temporal_resolution_s!r} s")
    ref_by_time = {s.valid_time_utc: s for s in reference.steps}
    pairs: list[tuple[datetime, float, float]] = []
    common = 0
    for step in candidate.steps:
        other = ref_by_time.get(step.valid_time_utc)
        if other is None:
            continue
        common += 1
        a, b = step.values.get(variable), other.values.get(variable)
        if a is None or b is None or not a.is_usable or not b.is_usable:
            continue
        assert a.value is not None and b.value is not None
        pairs.append((step.valid_time_utc, float(a.value), float(b.value)))
    if pairs:
        sun = solar_positions(reference.point, [t - timedelta(minutes=30) for t, _, _ in pairs])
        pairs = [p for p, s in zip(pairs, sun, strict=True) if s.apparent_elevation_deg > 0.0]
    cand = np.array([c for _, c, _ in pairs], dtype=float)
    ref = np.array([r for _, _, r in pairs], dtype=float)
    first = pairs[0][0] if pairs else None
    last = pairs[-1][0] if pairs else None
    separation = geodesic_distance_m(candidate.point, reference.point)
    return BiasStatistics(
        candidate=_label(candidate),
        reference=_label(reference),
        candidate_point=candidate.point,
        reference_point=reference.point,
        separation_m=separation,
        first_utc=first,
        last_utc=last,
        n_common_steps=common,
        agreement=_agreement(variable, cand, ref),
        provenance=Provenance(
            provider="rwi",
            dataset="validation/bias_statistics",
            kind=DataKind.DERIVED,
            retrieved_at_utc=utc_now(),
            valid_time_utc=last,
            point=reference.point,
            temporal_resolution_s=_HOUR_S,
            processing=(
                f"{variable}: candidate {_label(candidate)} minus reference "
                f"{_label(reference)}; identical end-of-hour labels, both usable, "
                "apparent elevation > 0 at t - 30 min (pvlib SPA at the reference point)"
            ),
            notes=(
                f"Candidate point is {separation / 1000:.1f} km from the reference; a grid "
                "cell mean against a point measurement, so part of the error is "
                "representativeness, not model bias."
            ),
        ),
    )
