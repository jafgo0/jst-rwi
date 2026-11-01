"""Power time series, energy integration and model-vs-measurement error metrics.

Dimensions: input irradiance W/m2; ``PowerEstimate`` power in W; energy in Wh
obtained only through :func:`rwi.core.units.integrate_power_wh` (trapezoidal
rule on power vs time in seconds).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from itertools import pairwise
from typing import Any

from pydantic import BaseModel, ConfigDict

from rwi.contracts import (
    ArrayConfig,
    EnergyEstimate,
    IrradianceComponents,
    PowerEstimate,
    SolarPosition,
)
from rwi.core.errors import ModelConfigurationError
from rwi.core.provenance import DataKind, Provenance
from rwi.core.timebase import TimeWindow, ensure_utc, utc_now
from rwi.core.units import integrate_power_wh
from rwi.energy.array import array_power
from rwi.energy.poa import poa_irradiance, surface_azimuth_for_heading

__all__ = [
    "ErrorMetrics",
    "PowerSample",
    "compare_with_measured",
    "energy_along_trajectory",
    "integrate_energy",
    "power_series",
]


class PowerSample(BaseModel):
    """One input row for :func:`power_series` (all plain, route-agnostic inputs)."""

    model_config = ConfigDict(frozen=True)

    solar: SolarPosition
    irradiance: IrradianceComponents
    air_temperature_c: float | None = None
    wind_speed_mps: float | None = None
    heading_deg: float | None = None
    """Vehicle heading (deg clockwise from north); required for vehicle_relative arrays."""

    @property
    def time_utc(self) -> datetime:
        return self.irradiance.time_utc


def _as_sample(item: Any) -> PowerSample:
    if isinstance(item, PowerSample):
        return item
    if isinstance(item, tuple) and len(item) in (5, 6):
        when, solar, irr, t_air, wind, *rest = item
        sample = PowerSample(
            solar=solar,
            irradiance=irr,
            air_temperature_c=t_air,
            wind_speed_mps=wind,
            heading_deg=rest[0] if rest else None,
        )
        if abs((ensure_utc(when) - ensure_utc(irr.time_utc)).total_seconds()) > 1.0:
            raise ModelConfigurationError(
                f"sample time {when.isoformat()} != irradiance time {irr.time_utc.isoformat()}"
            )
        return sample
    raise ModelConfigurationError(
        "power_series items must be PowerSample or "
        "(time, solar, irradiance, T_air, wind[, heading])"
    )


def power_series(
    samples: Sequence[PowerSample | tuple[Any, ...]],
    array: ArrayConfig,
    *,
    transposition_model: str = "haydavies",
    cell_temperature_model: str = "faiman",
    wind_height_factor: float | None = None,
) -> list[PowerEstimate]:
    """Run POA -> power for each sample; output order equals input order.

    Panel orientation comes from ``array`` (tilt, mounting) via
    :func:`surface_azimuth_for_heading`; ``vehicle_relative`` arrays need a
    heading on every sample or :class:`ModelConfigurationError` is raised.
    """
    out: list[PowerEstimate] = []
    for item in samples:
        s = _as_sample(item)
        azimuth = surface_azimuth_for_heading(array, s.heading_deg)
        poa = poa_irradiance(
            s.solar,
            s.irradiance,
            surface_tilt_deg=array.surface_tilt_deg,
            surface_azimuth_deg=azimuth,
            albedo=array.albedo,
            model=transposition_model,
        )
        out.append(
            array_power(
                poa,
                array,
                air_temperature_c=s.air_temperature_c,
                wind_speed_mps=s.wind_speed_mps,
                cell_temperature_model=cell_temperature_model,
                wind_height_factor=wind_height_factor,
            )
        )
    return out


def _seconds_since_first(times: Sequence[datetime]) -> list[float]:
    t0 = ensure_utc(times[0])
    secs = [(ensure_utc(t) - t0).total_seconds() for t in times]
    if any(b < a for a, b in pairwise(secs)):
        raise ValueError("estimate times must be non-decreasing")
    return secs


def integrate_energy(estimates: Sequence[PowerEstimate]) -> EnergyEstimate:
    """Energy (Wh) from net power (W) by the trapezoidal rule over sample times.

    ``window`` spans first to last sample; a single sample gives 0.0 Wh (no
    time interval). Trapezoids interpolate linearly between samples, so coarse
    sampling around sunrise/sunset or cloud edges biases the result; the
    largest gap is recorded in the provenance notes. Raises ``ValueError`` for
    an empty or time-decreasing sequence.
    """
    if not estimates:
        raise ValueError("cannot integrate an empty sequence of estimates")
    times = [e.time_utc for e in estimates]
    secs = _seconds_since_first(times)
    energy = integrate_power_wh([e.net_power_w for e in estimates], secs)
    max_gap = max((b - a for a, b in pairwise(secs)), default=0.0)
    assumed = sum(1 for e in estimates if e.array.parameter_source != "measured")
    prov = Provenance(
        provider="rwi",
        dataset="energy.integrate_energy",
        kind=DataKind.PHYSICAL_MODEL,
        retrieved_at_utc=utc_now(),
        model_name="trapezoidal(net_power_w)",
        processing="rwi.core.units.integrate_power_wh",
        notes=(
            f"{len(estimates)} samples; max sample gap {max_gap:.0f} s; "
            f"{assumed} samples use non-measured array parameters; no uncertainty estimate"
        ),
    )
    return EnergyEstimate(
        window=TimeWindow(start_utc=times[0], end_utc=times[-1]),
        energy_wh=energy,
        sample_count=len(estimates),
        integration_method="trapezoidal",
        provenance=prov,
    )


def energy_along_trajectory(estimates: Sequence[PowerEstimate]) -> list[float]:
    """Cumulative net energy (Wh) at each sample; element 0 is 0.0.

    Decoupled from rwi.route: pass estimates ordered by time (one per
    trajectory point). The last element equals ``integrate_energy(...).energy_wh``.
    """
    if not estimates:
        return []
    secs = _seconds_since_first([e.time_utc for e in estimates])
    power = [e.net_power_w for e in estimates]
    return [integrate_power_wh(power[: i + 1], secs[: i + 1]) for i in range(len(estimates))]


class ErrorMetrics(BaseModel):
    """Model-vs-measurement statistics. Power in W. Error = modelled - measured."""

    model_config = ConfigDict(frozen=True)

    n: int
    n_dropped: int
    bias_w: float
    mae_w: float
    rmse_w: float
    mean_measured_w: float
    relative_bias: float | None
    """bias / mean measured power (dimensionless); None when mean measured is 0."""


def compare_with_measured(
    modelled_w: Sequence[float | None], measured_w: Sequence[float | None]
) -> ErrorMetrics:
    """Bias, MAE and RMSE of modelled vs measured array power (W).

    Works on any supplied pairs; no telemetry exists yet, so this is untested
    against real data. Pairs where either value is None/NaN are dropped and
    counted in ``n_dropped``. Positive bias = model over-predicts.
    """
    if len(modelled_w) != len(measured_w):
        raise ValueError("modelled_w and measured_w must have equal length")
    pairs = [
        (float(m), float(o))
        for m, o in zip(modelled_w, measured_w, strict=True)
        if m is not None and o is not None and math.isfinite(m) and math.isfinite(o)
    ]
    if not pairs:
        raise ValueError("no valid (modelled, measured) pairs")
    errs = [m - o for m, o in pairs]
    n = len(errs)
    mean_meas = sum(o for _, o in pairs) / n
    bias = sum(errs) / n
    return ErrorMetrics(
        n=n,
        n_dropped=len(modelled_w) - n,
        bias_w=bias,
        mae_w=sum(abs(e) for e in errs) / n,
        rmse_w=math.sqrt(sum(e * e for e in errs) / n),
        mean_measured_w=mean_meas,
        relative_bias=bias / mean_meas if mean_meas != 0.0 else None,
    )
