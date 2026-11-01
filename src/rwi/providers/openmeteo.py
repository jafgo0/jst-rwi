"""Open-Meteo adapter (forecast, archive/reanalysis, ensemble).

Open-Meteo is a *model* API, not an observing network. Everything it returns is
numerical-weather-prediction or reanalysis output, so:

* `fetch_forecast`  -> ``DataKind.FORECAST`` (``/v1/forecast``);
* `fetch_ensemble`  -> one ``DataKind.FORECAST`` series per member (``/v1/ensemble``);
* `fetch_analysis`  -> ``DataKind.ANALYSIS`` from ``/v1/archive`` (reanalysis or the
  IFS short-range archive). Open-Meteo-specific method, not part of the protocol;
* `fetch_observations` -> **always raises** `CapabilityNotSupportedError`.
  DECISION: nothing Open-Meteo serves is a measurement, so it must never come
  back from an "observations" call, not even relabelled. The nearest honest
  substitute (`fetch_analysis`) is a different method with a different ``kind``
  so a caller cannot pick it up by accident.

Verified against the live API and https://open-meteo.com/en/docs on 2026-11-01
(see docs/science/providers.md for the evidence and what is NOT exposed).

Boundary rules enforced here:

* every request carries ``timezone=UTC``; the response is rejected unless
  ``utc_offset_seconds == 0``; timestamps are parsed as aware UTC;
* names map to the canonical registry (``shortwave_radiation`` -> ``ghi``,
  ``direct_normal_irradiance`` -> ``dni``, ``diffuse_radiation`` -> ``dhi``);
* units are converted from the unit string the API *reports* in ``hourly_units``
  (hPa -> Pa, km/h -> m/s). An unrecognised unit raises instead of being guessed;
* converted values outside the registry range are flagged ``OUT_OF_RANGE``;
  ``null`` becomes ``MISSING`` (never 0);
* the grid cell actually used is recorded as ``point`` and the caller's request
  as ``requested_point``.
"""

from __future__ import annotations

import itertools
import re
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from rwi.config import Settings
from rwi.contracts import (
    EnsembleForecast,
    ProviderCapabilities,
    WeatherSeries,
    WeatherStep,
)
from rwi.core.errors import (
    CapabilityNotSupportedError,
    ProviderError,
    ProviderUnavailableError,
)
from rwi.core.geo import GeoPoint
from rwi.core.provenance import DataKind, Measured, Provenance
from rwi.core.timebase import TimeWindow, ensure_utc
from rwi.core.units import hpa_to_pa, kmh_to_ms
from rwi.core.variables import CANONICAL_VARIABLES, unit_for
from rwi.providers.base import (
    BaseProvider,
    FetchResult,
    ceil_hour,
    classify_value,
    floor_hour,
    make_cache_key,
)

__all__ = [
    "OPENMETEO_ATTRIBUTION",
    "OPENMETEO_LICENSE",
    "OpenMeteoProvider",
]

PROVIDER_NAME = "open-meteo"

OPENMETEO_LICENSE = (
    "CC BY 4.0 (API data). Free API: non-commercial use only, fewer than 10,000 calls/day, "
    "5,000/hour, 600/minute; commercial use requires a paid plan "
    "(https://open-meteo.com/en/terms, https://open-meteo.com/en/licence, verified 2026-11-01). "
    "Underlying NWP licences (ECMWF, NOAA, DWD, ...) are listed on the licence page."
)
OPENMETEO_ATTRIBUTION = (
    "Weather data by Open-Meteo.com (https://open-meteo.com/) - a link is required next to "
    "wherever the data are displayed; indicate if changes were made (unit conversion here)."
)

# ---------------------------------------------------------------------------
# Variable / unit mapping (Open-Meteo name -> canonical name, accepted units)
# ---------------------------------------------------------------------------

_Conv = Callable[[float], float]


def _identity(v: float) -> float:
    return v


_TEMP = {"°C": _identity}
_PCT = {"%": _identity}
_PRESSURE = {"hPa": hpa_to_pa, "Pa": _identity}
_SPEED = {"km/h": kmh_to_ms, "m/s": _identity}
_DEG = {"°": _identity}
_MM = {"mm": _identity}
_IRRADIANCE = {"W/m²": _identity}
_METRE = {"m": _identity}
_UNITLESS = {"": _identity, "wmo code": _identity}


@dataclass(frozen=True)
class _VarMap:
    canonical: str
    api: str
    units: Mapping[str, _Conv]
    instantaneous_api: str | None = None
    """Alternative API name giving an instantaneous value (radiation only)."""


_VAR_MAPS: tuple[_VarMap, ...] = (
    _VarMap("temperature_2m", "temperature_2m", _TEMP),
    _VarMap("apparent_temperature", "apparent_temperature", _TEMP),
    _VarMap("dew_point_2m", "dew_point_2m", _TEMP),
    _VarMap("relative_humidity_2m", "relative_humidity_2m", _PCT),
    _VarMap("pressure_msl", "pressure_msl", _PRESSURE),
    _VarMap("surface_pressure", "surface_pressure", _PRESSURE),
    _VarMap("wind_speed_10m", "wind_speed_10m", _SPEED),
    _VarMap("wind_direction_10m", "wind_direction_10m", _DEG),
    _VarMap("wind_gusts_10m", "wind_gusts_10m", _SPEED),
    _VarMap("cloud_cover", "cloud_cover", _PCT),
    _VarMap("cloud_cover_low", "cloud_cover_low", _PCT),
    _VarMap("cloud_cover_mid", "cloud_cover_mid", _PCT),
    _VarMap("cloud_cover_high", "cloud_cover_high", _PCT),
    _VarMap("precipitation", "precipitation", _MM),
    _VarMap("precipitation_probability", "precipitation_probability", _PCT),
    _VarMap("rain", "rain", _MM),
    _VarMap("ghi", "shortwave_radiation", _IRRADIANCE, "shortwave_radiation_instant"),
    _VarMap("dni", "direct_normal_irradiance", _IRRADIANCE, "direct_normal_irradiance_instant"),
    _VarMap("dhi", "diffuse_radiation", _IRRADIANCE, "diffuse_radiation_instant"),
    _VarMap(
        "terrestrial_radiation",
        "terrestrial_radiation",
        _IRRADIANCE,
        "terrestrial_radiation_instant",
    ),
    _VarMap("uv_index", "uv_index", _UNITLESS),
    _VarMap("visibility", "visibility", _METRE),
    _VarMap("is_day", "is_day", _UNITLESS),
    _VarMap("weather_code", "weather_code", _UNITLESS),
    _VarMap("soil_temperature_0cm", "soil_temperature_0cm", _TEMP),
)
_BY_CANONICAL: dict[str, _VarMap] = {m.canonical: m for m in _VAR_MAPS}
assert set(_BY_CANONICAL) == set(CANONICAL_VARIABLES), "registry and Open-Meteo map out of sync"

RadiationMode = str  # "preceding_hour_mean" (API default) | "instant"

# ---------------------------------------------------------------------------
# Model facts (each verified on 2026-11-01 from the sources named in providers.md)
# ---------------------------------------------------------------------------

_SPATIAL_RESOLUTION_KM: dict[tuple[str, str], float] = {
    ("forecast", "ecmwf_ifs"): 9.0,
    ("forecast", "ecmwf_ifs025"): 25.0,
    ("forecast", "ecmwf_aifs025_single"): 25.0,
    ("archive", "ecmwf_ifs"): 9.0,
    ("archive", "era5"): 25.0,
    ("archive", "era5_land"): 11.0,
    ("ensemble", "ecmwf_ifs025"): 25.0,
    ("ensemble", "gfs025"): 25.0,
}

# Open-Meteo "Metadata API" directory per (dataset, models value). The directory
# names are those shown to exist on https://api.open-meteo.com/data/<dir>/static/meta.json.
_META_DIR: dict[tuple[str, str], str] = {
    ("forecast", "ecmwf_ifs"): "ecmwf_ifs",
    ("forecast", "ecmwf_ifs025"): "ecmwf_ifs025",
    ("forecast", "ecmwf_aifs025_single"): "ecmwf_aifs025_single",
    ("ensemble", "ecmwf_ifs025"): "ecmwf_ifs025_ensemble",
    ("ensemble", "gfs025"): "ncep_gefs025",
}

_ENSEMBLE_MAX_HOURS: dict[str, float] = {"ecmwf_ifs025": 360.0, "gfs025": 240.0}

MAX_FORECAST_HOURS = 384.0
"""16 days from 00:00 UTC of the current day (the API rejects later `end_hour`)."""
_FORECAST_PAST_LIMIT = timedelta(days=90)
"""Conservative: the API accepted 93 days back on 2026-11-01."""
_ENSEMBLE_PAST_LIMIT = timedelta(days=3)
_META_SAFETY_S = 600.0
"""Open-Meteo asks clients to wait 10 min after a run's availability time."""

_MODEL_RE = re.compile(r"^[a-z0-9_]+$")


class OpenMeteoProvider(BaseProvider):
    """Open-Meteo forecast / archive / ensemble adapter (free tier, no key)."""

    name = PROVIDER_NAME

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        forecast_model: str = "best_match",
        archive_model: str = "ecmwf_ifs",
        ensemble_model: str = "ecmwf_ifs025",
        radiation_mode: RadiationMode = "preceding_hour_mean",
        **kwargs: Any,
    ) -> None:
        super().__init__(settings, **kwargs)
        for m in (forecast_model, archive_model, ensemble_model):
            self._check_model(m)
        if radiation_mode not in ("preceding_hour_mean", "instant"):
            raise ValueError("radiation_mode must be 'preceding_hour_mean' or 'instant'")
        self.forecast_model = forecast_model
        self.archive_model = archive_model
        self.ensemble_model = ensemble_model
        self.radiation_mode = radiation_mode

    # -- capabilities ---------------------------------------------------------

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=self.name,
            datasets=("forecast", "archive", "ensemble"),
            supports_observations=False,
            supports_forecast=True,
            supports_ensemble=True,
            supports_archive=True,
            variables=frozenset(CANONICAL_VARIABLES),
            max_forecast_hours=MAX_FORECAST_HOURS,
            spatial_resolution_km=None,
            update_interval_s=None,
            license=OPENMETEO_LICENSE,
            attribution=OPENMETEO_ATTRIBUTION,
            documentation_url="https://open-meteo.com/en/docs",
            requires_api_key=False,
            notes=(
                "Model output only - NOT observations (fetch_observations raises). "
                "Spatial resolution 9-25 km depending on model ('best_match' mixes ICON, GFS "
                "and IFS globally; its resolution is not a single number, hence None). Update "
                "interval is per model (1-6 h), see docs/science/providers.md. 'Supported' means "
                "the name is accepted by the API; a given model may return null (-> MISSING) for "
                "some variables, e.g. uv_index and visibility for several models. Archive: "
                "fetch_analysis -> DataKind.ANALYSIS. Past limits: forecast ~90 d, ensemble 3 d."
            ),
        )

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _check_model(model: str) -> None:
        if not _MODEL_RE.match(model):
            raise CapabilityNotSupportedError(
                f"open-meteo: exactly one model name is supported per request, got {model!r}"
            )

    def _api_name(self, variable: str) -> str:
        m = _BY_CANONICAL[variable]
        if self.radiation_mode == "instant" and m.instantaneous_api is not None:
            return m.instantaneous_api
        return m.api

    def _url(self, dataset: str) -> str:
        if dataset == "forecast":
            return self.settings.openmeteo_base_url.rstrip("/") + "/forecast"
        if dataset == "archive":
            return self.settings.openmeteo_archive_url.rstrip("/") + "/archive"
        if dataset == "ensemble":
            return self.settings.openmeteo_ensemble_url.rstrip("/") + "/ensemble"
        raise ValueError(dataset)

    def _meta_url(self, directory: str) -> str:
        parts = urlsplit(self.settings.openmeteo_base_url)
        return f"{parts.scheme}://{parts.netloc}/data/{directory}/static/meta.json"

    def _with_key(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Add the commercial API key, only ever to a ``customer-*`` host."""
        key = self.settings.openmeteo_api_key
        host = urlsplit(url).hostname or ""
        if key and host.startswith("customer-"):
            return {**params, "apikey": key}
        return params

    async def _probe(self) -> None:
        url = self._url("forecast")
        data = await self._get_json(
            url,
            self._with_key(
                url,
                {
                    "latitude": 0.0,
                    "longitude": 0.0,
                    "hourly": "temperature_2m",
                    "forecast_hours": 1,
                    "timezone": "UTC",
                },
            ),
        )
        self._validate_hourly(data)

    # -- public fetch methods ---------------------------------------------------

    async def fetch_forecast(
        self,
        point: GeoPoint,
        window: TimeWindow,
        variables: Sequence[str],
        *,
        models: str | None = None,
    ) -> WeatherSeries:
        """Forecast series (``DataKind.FORECAST``) at the nearest suitable grid cell.

        Returns every native hourly step from ``floor_hour(window.start_utc)`` to
        ``ceil_hour(window.end_utc)`` inclusive (i.e. the steps bracketing the window).
        Radiation, precipitation and gusts are backward-looking aggregates ending
        at the stamp (see ``radiation_mode``). ``models`` selects one Open-Meteo
        model (default: ``best_match``).
        """
        model = models or self.forecast_model
        self._check_model(model)
        vars_ = self._check_variables(variables)
        first, last = floor_hour(window.start_utc), ceil_hour(window.end_utc)
        self._check_forecast_range(first, last)
        fetched, api_vars = await self._request_hourly(
            "forecast",
            point,
            first,
            last,
            vars_,
            model,
            {"start_hour": _fmt_hour(first), "end_hour": _fmt_hour(last)},
        )
        return self._series_from_fetch(
            fetched,
            dataset="forecast",
            kind=DataKind.FORECAST,
            requested=point,
            variables=vars_,
            model=model,
            first=first,
            last=last,
            columns={v: self._api_name(v) for v in vars_},
            api_vars=api_vars,
        )

    async def fetch_observations(
        self, point: GeoPoint, window: TimeWindow, variables: Sequence[str]
    ) -> WeatherSeries:
        """Not supported: Open-Meteo serves model output, never measurements."""
        raise CapabilityNotSupportedError(
            "open-meteo is a numerical-model API and has no observations. Model-derived past "
            "values are available through OpenMeteoProvider.fetch_analysis (kind=ANALYSIS); use "
            "a station provider (e.g. bom) for measurements."
        )

    async def fetch_analysis(
        self,
        point: GeoPoint,
        window: TimeWindow,
        variables: Sequence[str],
        *,
        models: str | None = None,
    ) -> WeatherSeries:
        """Past model state from ``/v1/archive`` as ``DataKind.ANALYSIS``.

        Default model ``ecmwf_ifs`` (9 km, available up to the present); ``era5``
        is the true reanalysis but lags by about 5 days (recent steps come back
        ``null`` -> MISSING). Not a measurement; do not treat as one.
        """
        model = models or self.archive_model
        self._check_model(model)
        vars_ = self._check_variables(variables)
        first, last = floor_hour(window.start_utc), ceil_hour(window.end_utc)
        now = self._now()
        if first > now:
            raise CapabilityNotSupportedError(
                "open-meteo archive cannot return future times; use fetch_forecast"
            )
        last = min(last, floor_hour(now))  # the archive API rejects dates after today (UTC)
        fetched, api_vars = await self._request_hourly(
            "archive",
            point,
            first,
            last,
            vars_,
            model,
            {"start_date": first.date().isoformat(), "end_date": last.date().isoformat()},
        )
        return self._series_from_fetch(
            fetched,
            dataset="archive",
            kind=DataKind.ANALYSIS,
            requested=point,
            variables=vars_,
            model=model,
            first=first,
            last=last,
            columns={v: self._api_name(v) for v in vars_},
            api_vars=api_vars,
        )

    async def fetch_ensemble(
        self,
        point: GeoPoint,
        window: TimeWindow,
        variables: Sequence[str],
        *,
        models: str | None = None,
    ) -> EnsembleForecast:
        """One ``WeatherSeries`` per ensemble member (control first).

        ``dataset`` of each member is ``"ensemble/control"``, ``"ensemble/member07"``...
        Hourly values may be interpolated from the model's native 3-6 h step.
        """
        model = models or self.ensemble_model
        self._check_model(model)
        vars_ = self._check_variables(variables)
        first, last = floor_hour(window.start_utc), ceil_hour(window.end_utc)
        now = self._now()
        if first < floor_hour(now - _ENSEMBLE_PAST_LIMIT):
            raise CapabilityNotSupportedError(
                "open-meteo ensemble keeps only ~3 days of history; window starts too early"
            )
        limit_h = _ENSEMBLE_MAX_HOURS.get(model)
        if limit_h is not None and last > floor_hour(now) + timedelta(hours=limit_h):
            raise CapabilityNotSupportedError(
                f"ensemble model {model} forecasts only {limit_h:g} h ahead"
            )
        fetched, api_vars = await self._request_hourly(
            "ensemble",
            point,
            first,
            last,
            vars_,
            model,
            {"start_hour": _fmt_hour(first), "end_hour": _fmt_hour(last)},
        )
        payload = fetched.payload["response"]
        hourly = payload["hourly"]
        member_ids: set[str] = set()
        pattern = re.compile(r"_member(\d+)$")
        for col in hourly:
            m = pattern.search(col)
            if m and col[: m.start()] in api_vars:
                member_ids.add(m.group(1))
        if not member_ids:
            raise ProviderUnavailableError(
                "open-meteo: ensemble response contains no member columns"
            )
        labels = ["control"] + [f"member{n}" for n in sorted(member_ids, key=int)]
        members: list[WeatherSeries] = []
        for label in labels:
            suffix = "" if label == "control" else f"_{label}"
            members.append(
                self._series_from_fetch(
                    fetched,
                    dataset=f"ensemble/{label}",
                    kind=DataKind.FORECAST,
                    requested=point,
                    variables=vars_,
                    model=model,
                    first=first,
                    last=last,
                    columns={v: self._api_name(v) + suffix for v in vars_},
                    api_vars=api_vars,
                    member=label,
                    dataset_endpoint="ensemble",
                )
            )
        return EnsembleForecast(
            point=members[0].point,
            provider=self.name,
            model_name=model,
            retrieved_at_utc=fetched.retrieved_at_utc,
            members=members,
        )

    # -- range checks -----------------------------------------------------------

    def _check_forecast_range(self, first: datetime, last: datetime) -> None:
        now = self._now()
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if last > day0 + timedelta(hours=MAX_FORECAST_HOURS) - timedelta(hours=1):
            raise CapabilityNotSupportedError(
                f"open-meteo forecasts reach at most {MAX_FORECAST_HOURS:g} h from 00:00 UTC "
                "today; the requested window ends later"
            )
        if first < day0 - _FORECAST_PAST_LIMIT:
            raise CapabilityNotSupportedError(
                "open-meteo forecast API serves ~90 days of past steps at most; "
                "window starts too early"
            )

    # -- transport ----------------------------------------------------------------

    async def _request_hourly(
        self,
        dataset: str,
        point: GeoPoint,
        first: datetime,
        last: datetime,
        variables: list[str],
        model: str,
        window_params: Mapping[str, str],
    ) -> tuple[FetchResult, list[str]]:
        url = self._url(dataset)
        api_vars = [self._api_name(v) for v in variables]
        params: dict[str, Any] = {
            "latitude": round(point.latitude_deg, 5),
            "longitude": round(point.longitude_deg, 5),
            "hourly": ",".join(api_vars),
            "models": model,
            "timezone": "UTC",
            **window_params,
        }
        key = make_cache_key(
            provider=self.name,
            dataset=dataset,
            point=point,
            window=TimeWindow(start_utc=first, end_utc=last),
            variables=variables,
            extra={"models": model, "radiation_mode": self.radiation_mode},
        )
        kind = DataKind.FORECAST if dataset != "archive" else DataKind.ANALYSIS

        async def fetch() -> dict[str, Any]:
            data = await self._get_json(url, self._with_key(url, params))
            self._validate_hourly(data)
            meta, meta_error, meta_at = await self._fetch_meta(dataset, model)
            return {
                "request_url": str(httpx.URL(url, params=params)),
                "response": data,
                "model_meta": meta,
                "model_meta_error": meta_error,
                "model_meta_checked_at_utc": meta_at.isoformat() if meta_at else None,
            }

        return await self._cached(key, self._ttl_s(kind), fetch), api_vars

    async def _fetch_meta(
        self, dataset: str, model: str
    ) -> tuple[dict[str, Any] | None, str | None, datetime | None]:
        directory = _META_DIR.get((dataset, model))
        if directory is None:
            return None, "no single-model metadata directory for this model selection", None
        try:
            meta = await self._get_json(self._meta_url(directory))
            if not isinstance(meta, dict) or not isinstance(
                meta.get("last_run_initialisation_time"), int | float
            ):
                return None, "metadata response lacks last_run_initialisation_time", None
            return meta, None, self._now()
        except ProviderError as exc:
            return None, f"metadata request failed: {exc}", None

    @staticmethod
    def _validate_hourly(data: Any) -> None:
        if not isinstance(data, dict):
            raise ProviderUnavailableError("open-meteo: response is not a JSON object")
        if data.get("error"):
            raise ProviderUnavailableError(f"open-meteo: API error: {data.get('reason')}")
        if data.get("utc_offset_seconds") != 0:
            raise ProviderUnavailableError(
                "open-meteo: response is not in UTC (utc_offset_seconds="
                f"{data.get('utc_offset_seconds')!r}); refusing to guess a time zone"
            )
        hourly = data.get("hourly")
        if not isinstance(hourly, dict) or not isinstance(hourly.get("time"), list):
            raise ProviderUnavailableError("open-meteo: response has no hourly.time array")
        n = len(hourly["time"])
        for col, values in hourly.items():
            if col != "time" and (not isinstance(values, list) or len(values) != n):
                raise ProviderUnavailableError(
                    f"open-meteo: column {col!r} length does not match time axis"
                )
        for lat_lon in ("latitude", "longitude"):
            if not isinstance(data.get(lat_lon), int | float):
                raise ProviderUnavailableError(f"open-meteo: response lacks {lat_lon}")

    # -- parsing ------------------------------------------------------------------

    def _series_from_fetch(
        self,
        fetched: FetchResult,
        *,
        dataset: str,
        kind: DataKind,
        requested: GeoPoint,
        variables: list[str],
        model: str,
        first: datetime,
        last: datetime,
        columns: Mapping[str, str],
        api_vars: list[str],
        member: str | None = None,
        dataset_endpoint: str | None = None,
    ) -> WeatherSeries:
        endpoint = dataset_endpoint or dataset
        blob = fetched.payload
        data = blob["response"]
        hourly: dict[str, list[Any]] = data["hourly"]
        units: Mapping[str, str] = data.get("hourly_units", {})

        times = [_parse_time(t) for t in hourly["time"]]
        keep = [i for i, t in enumerate(times) if first <= t <= last]
        if len(set(times)) != len(times):
            raise ProviderUnavailableError("open-meteo: duplicate timestamps in response")
        diffs = [(b - a).total_seconds() for a, b in itertools.pairwise(times)]
        temporal_s = float(statistics.median(diffs)) if diffs else None

        try:
            returned = GeoPoint(
                latitude_deg=float(data["latitude"]),
                longitude_deg=float(data["longitude"]),
                elevation_m=float(data["elevation"]) if data.get("elevation") is not None else None,
            )
        except (ValidationError, ValueError) as exc:
            raise ProviderUnavailableError(f"open-meteo: invalid returned location: {exc}") from exc

        # unit converters, resolved once per column from the unit the API reports
        converters: dict[str, _Conv | None] = {}
        for v in variables:
            col = columns[v]
            if col not in hourly:
                converters[v] = None  # column absent -> every value MISSING
                continue
            unit = units.get(col, "")
            conv = _BY_CANONICAL[v].units.get(unit)
            if conv is None:
                raise ProviderUnavailableError(
                    f"open-meteo: unexpected unit {unit!r} for {col!r}; expected one of "
                    f"{sorted(_BY_CANONICAL[v].units)} - refusing to guess a conversion"
                )
            converters[v] = conv

        init_time, init_note = self._model_init(blob, fetched.retrieved_at_utc, endpoint, model)
        resolution_km = _SPATIAL_RESOLUTION_KM.get((endpoint, model))
        license_ = OPENMETEO_LICENSE
        source_url = str(blob.get("request_url", ""))
        notes = self._notes(endpoint, model, fetched, temporal_s, init_note, member, blob)

        steps: list[WeatherStep] = []
        for i in keep:
            t = times[i]
            prov = Provenance(
                provider=self.name,
                dataset=dataset,
                kind=kind,
                retrieved_at_utc=fetched.retrieved_at_utc,
                valid_time_utc=t,
                model_name=model,
                model_init_time_utc=init_time,
                point=returned,
                requested_point=requested,
                spatial_resolution_km=resolution_km,
                temporal_resolution_s=temporal_s,
                processing=(
                    "nearest_grid_cell (cell_selection=land, 90 m DEM statistical downscaling "
                    "to the returned elevation); unit conversion to canonical units"
                ),
                license=license_,
                source_url=source_url,
                notes=notes,
            )
            values: dict[str, Measured] = {}
            for v in variables:
                conv = converters[v]
                raw = hourly[columns[v]][i] if conv is not None else None
                converted = _convert(conv, raw)
                value, flag = classify_value(v, converted)
                values[v] = Measured(
                    variable=v, value=value, unit=unit_for(v), provenance=prov, quality=flag
                )
            steps.append(WeatherStep(valid_time_utc=t, values=values))
        if not steps:
            raise ProviderUnavailableError("open-meteo: response contains no steps in the window")

        try:
            return WeatherSeries(
                point=returned,
                requested_point=requested,
                provider=self.name,
                dataset=dataset,
                kind=kind,
                model_name=model,
                model_version=None,
                model_init_time_utc=init_time,
                retrieved_at_utc=fetched.retrieved_at_utc,
                spatial_resolution_km=resolution_km,
                temporal_resolution_s=temporal_s,
                license=license_,
                source_url=source_url,
                steps=steps,
            )
        except ValueError as exc:
            raise ProviderUnavailableError(f"open-meteo: inconsistent series: {exc}") from exc

    def _model_init(
        self, blob: Mapping[str, Any], retrieved: datetime, endpoint: str, model: str
    ) -> tuple[datetime | None, str]:
        """Model run time, only when it can be asserted safely.

        The forecast/ensemble/archive *responses* carry no run time. It is read
        from the separate Metadata API (model-level, not response-level) and used
        only if the run became available at least 10 minutes before the metadata
        was fetched - Open-Meteo's own guidance for eventual consistency.
        """
        if endpoint == "archive":
            return None, "archive data are not tied to a single model run"
        meta = blob.get("model_meta")
        if not meta:
            reason = blob.get("model_meta_error") or "metadata unavailable"
            return None, f"model_init_time_utc unknown ({reason})"
        try:
            init = datetime.fromtimestamp(float(meta["last_run_initialisation_time"]), tz=UTC)
            avail = datetime.fromtimestamp(float(meta["last_run_availability_time"]), tz=UTC)
            checked = ensure_utc(datetime.fromisoformat(blob["model_meta_checked_at_utc"]))
        except (KeyError, TypeError, ValueError):
            return None, "model_init_time_utc unknown (metadata incomplete)"
        if (checked - avail).total_seconds() < _META_SAFETY_S:
            return None, (
                "model_init_time_utc withheld: newest run became available <10 min before the "
                "metadata check, so the response may still come from the previous run"
            )
        return init, (
            f"model_init_time_utc from Open-Meteo Metadata API (latest published run of {model}); "
            "the data response itself does not state which run it used"
        )

    def _notes(
        self,
        endpoint: str,
        model: str,
        fetched: FetchResult,
        temporal_s: float | None,
        init_note: str,
        member: str | None,
        blob: Mapping[str, Any],
    ) -> str:
        parts = [
            "Model output, not a measurement.",
            "Time stamps are UTC; ghi/dni/dhi/terrestrial_radiation are "
            + (
                "instantaneous values"
                if self.radiation_mode == "instant"
                else "means of the PRECEDING hour"
            )
            + ", precipitation/rain are PRECEDING-hour sums, wind_gusts_10m is the PRECEDING-hour "
            "maximum; all other variables are instantaneous.",
            init_note + ".",
        ]
        if model == "best_match":
            parts.append(
                "best_match is a per-variable, per-hour blend of ICON, GFS, IFS (+regional "
                "models); there is no single run or resolution."
            )
        meta = blob.get("model_meta") or {}
        native = meta.get("temporal_resolution_seconds")
        if native and temporal_s and native > temporal_s:
            parts.append(
                f"Model native step is {int(native)} s; hourly values are interpolated by "
                "Open-Meteo."
            )
        if endpoint == "archive":
            parts.append(
                "Archive/reanalysis state (ERA5 lags ~5 days; ecmwf_ifs is Open-Meteo's assembly "
                "of early forecast hours of each 6-hourly IFS run). Kind=ANALYSIS."
            )
        if member:
            parts.append(f"ensemble member: {member}.")
        if fetched.from_cache:
            parts.append(
                f"Served from local cache; retrieved_at_utc is the ORIGINAL retrieval time "
                f"({fetched.age_s:.0f} s ago)."
            )
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Module-level parsing helpers
# ---------------------------------------------------------------------------


def _fmt_hour(when: datetime) -> str:
    return ensure_utc(when).strftime("%Y-%m-%dT%H:%M")


def _parse_time(text: Any) -> datetime:
    """Parse an Open-Meteo ISO time as aware UTC.

    The request forces ``timezone=UTC`` and `_validate_hourly` has checked
    ``utc_offset_seconds == 0``, so a naive stamp is UTC by construction.
    """
    if not isinstance(text, str):
        raise ProviderUnavailableError(f"open-meteo: non-string timestamp {text!r}")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProviderUnavailableError(f"open-meteo: unparsable timestamp {text!r}") from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else ensure_utc(parsed)


def _convert(conv: _Conv | None, raw: Any) -> float | None:
    if conv is None or raw is None or isinstance(raw, bool):
        return None
    try:
        return round(conv(float(raw)), 9)
    except (TypeError, ValueError):
        return None
