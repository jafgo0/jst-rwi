"""Tests for rwi.solar.decomposition (Erbs / DISC / DIRINT). No network access."""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Provenance, QualityFlag
from rwi.solar.decomposition import decompose_ghi
from rwi.solar.geometry import solar_position

JONKOPING = GeoPoint(latitude_deg=57.7826, longitude_deg=14.1618, elevation_m=227.0)
ALICE = GeoPoint(latitude_deg=-23.6980, longitude_deg=133.8807, elevation_m=545.0)
RACE_NOON = datetime(2027, 8, 24, 3, 0, tzinfo=UTC)
NIGHT = datetime(2026, 1, 15, 23, 0, tzinfo=UTC)


def _forecast_prov(when: datetime = RACE_NOON) -> Provenance:
    return Provenance(
        provider="open-meteo",
        dataset="forecast",
        kind=DataKind.FORECAST,
        retrieved_at_utc=datetime(2027, 8, 23, 12, 0, tzinfo=UTC),
        valid_time_utc=when,
        model_name="ecmwf_ifs",
        model_init_time_utc=datetime(2027, 8, 23, 0, 0, tzinfo=UTC),
        point=ALICE,
    )


def _closure(c, zenith_deg: float) -> float:
    return c.dni_w_m2 * math.cos(math.radians(zenith_deg)) + c.dhi_w_m2


@pytest.mark.parametrize("model", ["erbs", "disc", "dirint"])
def test_daytime_closure(model: str) -> None:
    sun = solar_position(ALICE, RACE_NOON)
    res = decompose_ghi(700.0, sun, model=model, provenance=_forecast_prov())
    assert res.ghi_w_m2 == 700.0
    assert res.dni_w_m2 > 0.0
    assert 0.0 <= res.dhi_w_m2 <= res.ghi_w_m2
    # GHI ~= DNI cos(z) + DHI; closure tolerance 0.5 W/m2 (clipping only).
    assert _closure(res, sun.zenith_deg) == pytest.approx(700.0, abs=0.5)


def test_erbs_closure_tight_across_clearness_range() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    for ghi in (50.0, 150.0, 300.0, 500.0, 700.0, 850.0):
        res = decompose_ghi(ghi, sun, provenance=_forecast_prov())
        assert _closure(res, sun.zenith_deg) == pytest.approx(ghi, abs=1e-6)
        assert res.dhi_w_m2 <= ghi


def test_erbs_clear_vs_overcast_behaviour() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    overcast = decompose_ghi(150.0, sun, provenance=_forecast_prov())
    clear = decompose_ghi(850.0, sun, provenance=_forecast_prov())
    assert overcast.dhi_w_m2 / overcast.ghi_w_m2 > 0.8  # mostly diffuse
    assert clear.dhi_w_m2 / clear.ghi_w_m2 < 0.2  # mostly direct
    assert clear.dni_w_m2 > overcast.dni_w_m2


@pytest.mark.science
def test_erbs_reference_value() -> None:
    """Erbs, Klein & Duffie (1982) correlation, evaluated independently here.

    Clearness index kt = GHI / (I0 cos z), I0 = 1367 (1 + 0.033 cos(2 pi doy / 365))
    (Duffie & Beckman). Alice Springs 2027-08-24 03:00Z: geometric zenith 34.95 deg,
    doy 236 -> I0 ~ 1340 W/m2, I0 cos z ~ 1099 W/m2; for GHI = 817 W/m2, kt ~ 0.743.
    For 0.22 < kt <= 0.80: DHI/GHI = 0.9511 - 0.1604 kt + 4.388 kt^2 - 16.638 kt^3
    + 12.336 kt^4 ~ 0.187 -> DHI ~ 153 W/m2, DNI = (GHI - DHI) / cos z ~ 810 W/m2.
    Tolerances: kt 0.01 (pvlib uses the Spencer I0 expansion), diffuse fraction 0.005,
    DHI +/- 6 W/m2, DNI +/- 1.5 %."""
    sun = solar_position(ALICE, RACE_NOON)
    ghi = 817.0
    res = decompose_ghi(ghi, sun, provenance=_forecast_prov())
    i0 = 1367.0 * (1.0 + 0.033 * math.cos(2.0 * math.pi * 236 / 365.0))
    cos_z = math.cos(math.radians(sun.zenith_deg))
    kt_ref = ghi / (i0 * cos_z)
    assert kt_ref == pytest.approx(0.743, abs=0.005)
    assert res.clearness_index == pytest.approx(kt_ref, abs=0.01)
    frac_ref = (
        0.9511 - 0.1604 * kt_ref + 4.388 * kt_ref**2 - 16.638 * kt_ref**3 + 12.336 * kt_ref**4
    )
    assert frac_ref == pytest.approx(0.187, abs=0.005)
    assert res.dhi_w_m2 / ghi == pytest.approx(frac_ref, abs=0.005)
    assert res.dhi_w_m2 == pytest.approx(153.0, abs=6.0)
    assert res.dni_w_m2 == pytest.approx((ghi - res.dhi_w_m2) / cos_z, rel=1e-6)
    assert res.dni_w_m2 == pytest.approx(810.0, rel=0.015)


# --- Night / zero rules -------------------------------------------------------


@pytest.mark.parametrize("model", ["erbs", "disc", "dirint"])
def test_zero_ghi_gives_all_zeros(model: str) -> None:
    sun = solar_position(ALICE, RACE_NOON)
    res = decompose_ghi(0.0, sun, model=model, provenance=_forecast_prov())
    assert (res.ghi_w_m2, res.dni_w_m2, res.dhi_w_m2) == (0.0, 0.0, 0.0)


def test_night_forces_exact_zeros_even_with_positive_input() -> None:
    sun = solar_position(JONKOPING, NIGHT)
    assert sun.is_daylight is False
    res = decompose_ghi(120.0, sun, provenance=_forecast_prov(NIGHT))
    assert (res.ghi_w_m2, res.dni_w_m2, res.dhi_w_m2) == (0.0, 0.0, 0.0)
    assert res.quality is QualityFlag.OUT_OF_RANGE  # provider claimed 120 W/m2 at night
    tiny = decompose_ghi(0.5, sun, provenance=_forecast_prov(NIGHT))
    assert tiny.ghi_w_m2 == 0.0 and tiny.quality is QualityFlag.OK


def test_very_low_sun_does_not_blow_up() -> None:
    # Just after local sunrise: apparent elevation small and positive.
    t = datetime(2027, 8, 23, 21, 30, tzinfo=UTC)
    sun = solar_position(ALICE, t)
    assert sun.is_daylight and sun.apparent_elevation_deg < 10.0
    res = decompose_ghi(40.0, sun, provenance=_forecast_prov(t))
    assert 0.0 <= res.dhi_w_m2 <= res.ghi_w_m2 == 40.0
    assert res.dni_w_m2 >= 0.0


# --- Provenance ---------------------------------------------------------------


def test_forecast_kind_preserved_and_processing_recorded() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    prov = _forecast_prov()
    res = decompose_ghi(700.0, sun, provenance=prov)
    assert res.kind is DataKind.FORECAST
    assert res.provenance.kind is DataKind.FORECAST
    assert res.provenance.processing == "erbs_decomposition"
    assert res.provenance.model_name == "ecmwf_ifs"
    assert res.provenance.model_init_time_utc == prov.model_init_time_utc
    assert res.provenance.provider == "open-meteo"
    assert res.time_utc == RACE_NOON and res.point == ALICE


def test_input_provenance_is_not_mutated() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    prov = _forecast_prov()
    snapshot = prov.model_dump()
    res = decompose_ghi(700.0, sun, provenance=prov)
    assert prov.model_dump() == snapshot
    assert prov.processing is None
    assert res.provenance is not prov


def test_existing_processing_is_extended() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    prov = _forecast_prov().model_copy(update={"processing": "linear_time_interp"})
    res = decompose_ghi(700.0, sun, model="disc", provenance=prov)
    assert res.provenance.processing == "linear_time_interp+disc_decomposition"


def test_observation_becomes_derived_not_observation() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    obs = Provenance(
        provider="bom",
        dataset="aws",
        kind=DataKind.OBSERVATION,
        retrieved_at_utc=RACE_NOON,
        observation_time_utc=RACE_NOON,
    )
    res = decompose_ghi(700.0, sun, provenance=obs)
    assert res.kind is DataKind.DERIVED
    assert res.provenance.observation_time_utc == RACE_NOON


def test_clear_sky_kind_stays_clear_sky() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    prov = Provenance(
        provider="rwi",
        dataset="pvlib",
        kind=DataKind.CLEAR_SKY_MODEL,
        retrieved_at_utc=RACE_NOON,
    )
    assert decompose_ghi(700.0, sun, provenance=prov).kind is DataKind.CLEAR_SKY_MODEL


# --- Failure ------------------------------------------------------------------


def test_invalid_inputs_raise() -> None:
    sun = solar_position(ALICE, RACE_NOON)
    prov = _forecast_prov()
    with pytest.raises(ValueError):
        decompose_ghi(-1.0, sun, provenance=prov)
    with pytest.raises(ValueError):
        decompose_ghi(float("nan"), sun, provenance=prov)
    with pytest.raises(ValueError):
        decompose_ghi(2000.0, sun, provenance=prov)
    with pytest.raises(ValueError):
        decompose_ghi(500.0, sun, model="nonsense", provenance=prov)
