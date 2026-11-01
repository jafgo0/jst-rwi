"""Shared builders for the energy tests (not a test module)."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from rwi.contracts import ArrayConfig, IrradianceComponents, POAIrradiance, SolarPosition
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance

POINT = GeoPoint(latitude_deg=-23.698, longitude_deg=133.8807, elevation_m=545.0)
T0 = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)


def prov() -> Provenance:
    return Provenance(
        provider="test",
        dataset="energy-unit-test",
        kind=DataKind.SIMULATION,
        retrieved_at_utc=T0,
    )


def solar_at(elevation_deg: float, azimuth_deg: float = 0.0, when: datetime = T0) -> SolarPosition:
    """Sun at a prescribed elevation (apparent == geometric here by construction)."""
    return SolarPosition(
        point=POINT,
        time_utc=when,
        elevation_deg=elevation_deg,
        apparent_elevation_deg=elevation_deg,
        azimuth_deg=azimuth_deg,
        zenith_deg=90.0 - elevation_deg,
        apparent_zenith_deg=90.0 - elevation_deg,
        provenance=prov(),
    )


def irradiance_for(
    solar: SolarPosition, dni: float, dhi: float, *, closed: bool = True, ghi: float | None = None
) -> IrradianceComponents:
    """Components; by default GHI = DNI*cos(zenith) + DHI (closed), else ``ghi`` is given."""
    if ghi is None:
        ghi = (
            max(dni * math.sin(math.radians(solar.apparent_elevation_deg)), 0.0) + dhi
            if closed
            else dni + dhi
        )
    return IrradianceComponents(
        time_utc=solar.time_utc,
        point=solar.point,
        ghi_w_m2=ghi,
        dni_w_m2=dni,
        dhi_w_m2=dhi,
        kind=DataKind.SIMULATION,
        provenance=prov(),
        solar_elevation_deg=solar.apparent_elevation_deg,
    )


def flat_poa(g: float, when: datetime = T0) -> POAIrradiance:
    """A POA record carrying only a prescribed global value (for array-model tests)."""
    return POAIrradiance(
        time_utc=when,
        poa_global_w_m2=g,
        poa_direct_w_m2=g,
        poa_sky_diffuse_w_m2=0.0,
        poa_ground_diffuse_w_m2=0.0,
        aoi_deg=0.0,
        surface_tilt_deg=0.0,
        surface_azimuth_deg=180.0,
        albedo=0.2,
        transposition_model="prescribed",
        provenance=prov(),
    )


def make_array(**kw: object) -> ArrayConfig:
    base: dict[str, object] = {
        "name": "unit-test array",
        "area_m2": 4.0,
        "cell_efficiency_stc": 0.24,
        "temperature_coefficient_pmax_per_c": -0.0035,
        "mppt_efficiency": 0.98,
        "wiring_efficiency": 0.98,
    }
    base.update(kw)
    return ArrayConfig(**base)  # type: ignore[arg-type]


def at(hours: float) -> datetime:
    return T0 + timedelta(hours=hours)
