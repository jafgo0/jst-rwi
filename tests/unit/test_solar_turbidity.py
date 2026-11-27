"""Dynamic Linke turbidity from AOD550, T and RH via pvlib kasten96_lt (ADR 0014)."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from rwi.core.geo import GeoPoint
from rwi.solar.clearsky import clear_sky_series
from rwi.solar.turbidity import climatology_linke_turbidity, dynamic_linke_turbidity

DARWIN = GeoPoint(latitude_deg=-12.4634, longitude_deg=130.8456, elevation_m=30.0)
T0 = datetime(2026, 11, 27, 3, tzinfo=UTC)


@pytest.mark.science
def test_kasten96_darwin_reference() -> None:
    # Hand check of Ineichen (2008), Solar Energy 82, 1095, eq. as coded in pvlib.kasten96_lt:
    #   TL = (9.4 + 0.9 m) * (-0.101 + 0.235 m^-0.16 + 0.112 m^-0.55 w^0.34 + tau_bb), m = 2.
    # AOD550 0.42 (CAMS fixture, Darwin 2026-11-27 00Z), alpha 1.3:
    #   tau_700 = 0.42 * (700/550)^-1.3 = 0.3070 (Angstrom law).
    # T = 25 C, RH = 70 % -> Gueymard (1994) w = 3.509 cm.
    #   TL = 11.2 * (0.1093 + 0.1112 + 0.3070) = 5.98.
    est = dynamic_linke_turbidity(
        DARWIN,
        [T0],
        aod550=[0.42],
        temperature_c=[25.0],
        relative_humidity_pct=[70.0],
        angstrom_exponent=1.3,
    )
    assert est.dynamic.tolist() == [True]
    assert est.values[0] == pytest.approx(5.975, abs=0.005)
    assert "alpha=1.3" in est.note and "configured" in est.note


def test_missing_and_out_of_fit_hours_keep_climatology() -> None:
    times = [T0 + timedelta(hours=i) for i in range(3)]
    clim, _ = climatology_linke_turbidity(DARWIN, times)
    est = dynamic_linke_turbidity(
        DARWIN,
        times,
        aod550=[None, 0.3, 0.3],
        temperature_c=[25.0, 25.0, 40.0],
        relative_humidity_pct=[70.0, 70.0, 95.0],  # 40 C / 95 % -> w > 5 cm, outside the fit
        angstrom_exponent=1.3,
    )
    assert est.dynamic.tolist() == [False, True, False]
    assert est.values[0] == clim[0] and est.values[2] == clim[2]
    assert "missing AOD/T/RH: 1 h" in est.note and "outside the fit" in est.note


def test_length_mismatch_and_bad_alpha_raise() -> None:
    with pytest.raises(ValueError, match="aod550"):
        dynamic_linke_turbidity(
            DARWIN,
            [T0],
            aod550=[],
            temperature_c=[1.0],
            relative_humidity_pct=[1.0],
            angstrom_exponent=1.3,
        )
    with pytest.raises(ValueError, match="finite"):
        dynamic_linke_turbidity(
            DARWIN,
            [T0],
            aod550=[0.1],
            temperature_c=[1.0],
            relative_humidity_pct=[1.0],
            angstrom_exponent=float("nan"),
        )


@pytest.mark.science
def test_higher_turbidity_lowers_clear_sky_dni() -> None:
    # Ineichen-Perez: DNI falls monotonically with TL at fixed geometry.
    times = [T0]
    low = clear_sky_series(DARWIN, times, linke_turbidity=np.array([3.0]), linke_turbidity_note="t")
    high = clear_sky_series(
        DARWIN, times, linke_turbidity=np.array([6.0]), linke_turbidity_note="t"
    )
    assert high[0].dni_w_m2 < low[0].dni_w_m2
