/**
 * API client - the only module that knows endpoint paths, timeouts and error
 * mapping. Shapes follow src/rwi/contracts.py and src/rwi/core/*.py (pydantic
 * field names, JSON-serialised). Where docs/CONTRACTS.md section 4 does not fix
 * an envelope (energy, strategy, alerts, state extras), the normalisers below
 * accept the plausible variants and the README lists them as assumptions.
 * @module api
 */
import { apiBase, cfg } from "./settings.js";
import { setClockOffset } from "./format.js";

/* ------------------------------------------------------------------ typedefs */

/** @typedef {{latitude_deg:number, longitude_deg:number, elevation_m:(number|null)}} GeoPoint */
/** @typedef {"observation"|"analysis"|"forecast"|"clear_sky_model"|"physical_model"|"simulation"|"derived"|"climatology"} DataKind */
/**
 * @typedef {Object} Provenance
 * @property {string} provider
 * @property {string} dataset
 * @property {DataKind} kind
 * @property {string} retrieved_at_utc ISO-8601 UTC
 * @property {string|null} [valid_time_utc]
 * @property {string|null} [observation_time_utc]
 * @property {string|null} [model_name]
 * @property {string|null} [model_version]
 * @property {string|null} [model_init_time_utc]
 * @property {GeoPoint|null} [point]
 * @property {GeoPoint|null} [requested_point]
 * @property {number|null} [spatial_resolution_km]
 * @property {number|null} [temporal_resolution_s]
 * @property {string|null} [processing]
 * @property {string|null} [license]
 * @property {string|null} [source_url]
 * @property {string|null} [notes]
 */
/**
 * @typedef {Object} Measured one scalar with unit, provenance, quality
 * @property {string} variable canonical name (rwi.core.variables)
 * @property {number|null} value null = source had no number (never 0)
 * @property {string} unit canonical unit, e.g. "m/s", "Pa", "W/m2"
 * @property {Provenance} provenance
 * @property {"ok"|"stale"|"missing"|"out_of_range"|"rate_implausible"|"sensor_suspect"|"unvalidated"|"interpolated"} quality
 * @property {{standard_deviation?:number, lower?:number, upper?:number, method?:string}|null} [uncertainty]
 */
/**
 * @typedef {Object} PositionFix
 * @property {GeoPoint} point
 * @property {"live_gps"|"manual"|"route_sim"|"historical_replay"} mode
 * @property {string} source
 * @property {string} received_at_utc
 * @property {string|null} [gps_time_utc]
 * @property {number|null} [accuracy_m] horizontal 1-sigma, metres
 * @property {number|null} [speed_mps]
 * @property {number|null} [course_deg]
 * @property {string} [fix_quality] no_fix|gps|dgps|rtk|manual|simulated|unknown
 * @property {number|null} [satellites]
 */
/**
 * @typedef {Object} ActiveState GET /api/state
 * @property {"live_gps"|"manual"|"route_sim"|"historical_replay"} mode
 * @property {PositionFix|null} fix null = position unknown
 * @property {string|null} selected_time_utc null = follow real time
 * @property {string|null} route_id
 * @property {number|null} route_distance_m progress along the active route
 * @property {number|null} heading_deg
 * @property {string} updated_at_utc
 * @property {string} [timezone] IANA zone at the vehicle (name assumed; see normaliseState)
 * @property {string} [local_time] resolved local time (name assumed)
 */
/** @typedef {{provider:string, reachable:boolean, checked_at_utc:string, latency_ms:(number|null), message:(string|null)}} ProviderHealth */
/** @typedef {{status:string, version:string, providers:ProviderHealth[], uptime_s:number, server_time_utc?:string, capabilities?:Object<string,boolean>, unavailable_modules?:Object<string,string>, provider_errors?:Object<string,string>, network_enabled?:boolean}} HealthResponse */
/**
 * @typedef {Object} SolarPosition
 * @property {GeoPoint} point @property {string} time_utc
 * @property {number} elevation_deg geometric @property {number} apparent_elevation_deg
 * @property {number} azimuth_deg clockwise from true north @property {number} zenith_deg @property {number} apparent_zenith_deg
 * @property {number|null} [declination_deg] @property {number|null} [equation_of_time_min] @property {number|null} [air_mass]
 * @property {Provenance} provenance
 */
/**
 * @typedef {Object} SolarDayEvents
 * @property {string} local_date @property {string|null} timezone_name
 * @property {string|null} sunrise_utc @property {string|null} sunset_utc @property {string} solar_noon_utc
 * @property {number} transit_elevation_deg @property {number} day_length_s
 * @property {boolean} is_polar_day @property {boolean} is_polar_night @property {Provenance} provenance
 */
/**
 * @typedef {Object} IrradianceComponents
 * @property {string} time_utc @property {number} ghi_w_m2 @property {number} dni_w_m2 @property {number} dhi_w_m2
 * @property {DataKind} kind clear_sky_model for /api/solar/clearsky*
 * @property {Provenance} provenance @property {number|null} [solar_elevation_deg] @property {number|null} [clearness_index] @property {string} [quality]
 */
/** @typedef {{valid_time_utc:string, values:Object<string, Measured>}} WeatherStep */
/**
 * @typedef {Object} WeatherSeries
 * @property {GeoPoint} point @property {GeoPoint|null} [requested_point] @property {string} provider @property {string} dataset
 * @property {DataKind} kind @property {string|null} [model_name] @property {string|null} [model_version] @property {string|null} [model_init_time_utc]
 * @property {string} retrieved_at_utc @property {number|null} [spatial_resolution_km] @property {number|null} [temporal_resolution_s]
 * @property {string|null} [license] @property {string|null} [source_url] @property {WeatherStep[]} steps
 */
/** @typedef {{point:GeoPoint, cumulative_distance_m:number, kind:string, name:(string|null), mandatory_dwell_s:(number|null), notes:(string|null)}} RouteWaypoint */
/** @typedef {{id:string, name:string, total_distance_m:number, control_stops:RouteWaypoint[]}} RouteInfo GET /api/route */
/**
 * @typedef {Object} TrajectoryPoint
 * @property {number} distance_m @property {GeoPoint} point @property {string} eta_utc @property {number} heading_deg
 * @property {number} speed_mps @property {number} grade @property {RouteWaypoint|null} [waypoint]
 */
/** @typedef {{route_id:string, generated_at_utc:string, points:TrajectoryPoint[], assumptions:Object, provenance:Provenance}} Trajectory */
/** @typedef {{headwind_mps:number, crosswind_mps:number, apparent_speed_mps:number, yaw_angle_deg:number}} RelativeWind positive headwind = headwind */
/** @typedef {{trajectory_point:TrajectoryPoint, series_point:GeoPoint, values:Object<string,Measured>, lead_time_s:(number|null), relative_wind:(RelativeWind|null)}} RouteWeatherSample */
/** @typedef {{route_id:string, generated_at_utc:string, samples:RouteWeatherSample[], provider:string, assumptions:Object}} RouteWeatherForecast */
/**
 * @typedef {Object} PowerEstimate modelled array power
 * @property {string} time_utc @property {{poa_global_w_m2:number, aoi_deg:number, transposition_model:string}} poa
 * @property {number} cell_temperature_c @property {number|null} air_temperature_c @property {number|null} wind_speed_mps
 * @property {number} dc_power_w @property {number} net_power_w @property {number} effective_efficiency
 * @property {{name:string, area_m2:number, parameter_source:string, source_notes:(string|null)}} array @property {Provenance} provenance
 */
/** @typedef {{window:{start_utc:string, end_utc:string}, energy_wh:number, sample_count:number, integration_method:string, provenance:Provenance, uncertainty_wh:(number|null)}} EnergyEstimate */
/**
 * @typedef {Object} ScenarioResult
 * @property {string} name @property {string} description @property {number} target_speed_kmh @property {number} distance_m
 * @property {number} energy_generated_wh @property {number} energy_consumed_wh @property {number} net_energy_wh
 * @property {number} start_soc_fraction @property {number} end_soc_fraction @property {string|null} arrival_utc @property {number} travel_time_s
 * @property {boolean} feasible @property {{constraint:string, detail:string, severity:"blocking"|"warning"}[]} violations
 * @property {Object} assumptions @property {string[]} uncertainty_notes @property {Provenance} provenance
 */
/**
 * @typedef {Object} Recommendation
 * @property {string} headline @property {string} rationale @property {string|null} recommended_scenario @property {string[]} alternatives
 * @property {number|null} energy_impact_wh @property {number|null} time_impact_s @property {"low"|"medium"|"high"} confidence
 * @property {string[]} uncertainty_notes @property {string[]} safety_and_regulatory_notes @property {string|null} reevaluate_after_utc
 * @property {string[]} evidence @property {Provenance} provenance
 */
/**
 * @typedef {Object} Alert
 * @property {string} id @property {"info"|"watch"|"warning"|"critical"} severity @property {string} category
 * @property {string} headline @property {string} detail @property {string} issued_at_utc
 * @property {{start_utc:string, end_utc:string}|null} [valid_window] @property {GeoPoint|null} [location]
 * @property {number|null} [distance_along_route_m] @property {string[]} evidence @property {string|null} [recommended_action] @property {Provenance} provenance
 */

/* --------------------------------------------------------------------- errors */

/**
 * Failure of an API call. `kind` drives the UI wording:
 * network | timeout | http | parse | position (409) | unsupported (501) | provider (503)
 */
export class ApiError extends Error {
  /**
   * @param {{endpoint:string, url:string, kind:string, status?:number, code?:string, message:string, detail?:any}} o
   */
  constructor(o) {
    super(o.message);
    this.name = "ApiError";
    this.endpoint = o.endpoint;
    this.url = o.url;
    this.kind = o.kind;
    this.status = o.status ?? null;
    this.code = o.code ?? null;
    this.detail = o.detail ?? null;
  }
}

/* -------------------------------------------------------------------- request */

/**
 * @param {string} path  e.g. "/api/state"
 * @param {{method?:string, params?:Object, body?:any, timeoutMs?:number}} [o]
 */
async function request(path, o = {}) {
  const method = o.method || "GET";
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(o.params || {})) {
    if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
  }
  const url = apiBase() + path + (qs.toString() ? "?" + qs : "");
  const endpoint = `${method} ${path}`;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), o.timeoutMs ?? cfg.requestTimeoutMs);
  let resp;
  try {
    resp = await fetch(url, {
      method,
      signal: ctl.signal,
      headers: o.body ? { "Content-Type": "application/json", Accept: "application/json" } : { Accept: "application/json" },
      body: o.body ? JSON.stringify(o.body) : undefined,
      cache: "no-store",
    });
  } catch (e) {
    clearTimeout(timer);
    if (ctl.signal.aborted) {
      throw new ApiError({ endpoint, url, kind: "timeout", message: `no response within ${(o.timeoutMs ?? cfg.requestTimeoutMs) / 1000} s` });
    }
    throw new ApiError({ endpoint, url, kind: "network", message: `${e?.message || e} (API unreachable or blocked by CORS)` });
  }
  clearTimeout(timer);
  const dateHdr = resp.headers.get("Date");
  if (dateHdr) { const t = Date.parse(dateHdr); if (Number.isFinite(t)) setClockOffset(t - Date.now()); }

  let text = "";
  try { text = await resp.text(); } catch { /* handled below */ }
  let json = null;
  if (text) { try { json = JSON.parse(text); } catch { /* not JSON */ } }

  if (!resp.ok) {
    const err = extractError(json);
    const kind = resp.status === 409 ? "position" : resp.status === 501 ? "unsupported" : resp.status === 503 ? "provider" : "http";
    throw new ApiError({
      endpoint, url, kind, status: resp.status, code: err?.code,
      message: err?.message || `HTTP ${resp.status} ${resp.statusText || ""}`.trim(),
      detail: err?.detail ?? (json ? null : text.slice(0, 300)),
    });
  }
  if (json === null) {
    throw new ApiError({ endpoint, url, kind: "parse", status: resp.status, message: "response was not valid JSON", detail: text.slice(0, 200) });
  }
  return json;
}

/**
 * Pull {code, message, detail} out of the error shapes the backend produces:
 *   {"error": {code, message, detail}}                      (RwiError handler)
 *   {"detail": {"error": {code, message, detail}}}          (501 CapabilityNotImplemented)
 *   {"detail": "text"} / {"detail": [{msg, loc}, ...]}      (FastAPI HTTPException / validation)
 * @returns {{code?:string, message?:string, detail?:any}|null}
 */
function extractError(json) {
  if (!json || typeof json !== "object") return null;
  if (json.error && typeof json.error === "object") return json.error;
  const d = json.detail;
  if (d && typeof d === "object" && !Array.isArray(d) && d.error) return d.error;
  if (typeof d === "string") return { message: d };
  if (Array.isArray(d)) return { message: d.map((x) => `${(x.loc || []).slice(1).join(".")}: ${x.msg}`).join("; ") };
  return null;
}

/* ---------------------------------------------------------------- normalisers */

/** First array found under any of `keys`, or the value itself if it is an array. */
function arrayFrom(resp, keys) {
  if (Array.isArray(resp)) return resp;
  for (const k of keys) if (Array.isArray(resp?.[k])) return resp[k];
  return null;
}

/**
 * GET /api/state returns StateResponse:
 *   {state: ActiveState, effective_time_utc, local_time, timezone_name, utc_offset_hours,
 *    position_available, is_simulated, position_age_s, server_time_utc}
 * (docs/CONTRACTS.md only says "ActiveState + resolved local time + timezone", so a flat
 * ActiveState with timezone/local_time fields is accepted too.) The result is the flat
 * ActiveState plus the extras.
 * @returns {ActiveState & {timezone:(string|null), local_time:(string|null), is_simulated:boolean, position_available:boolean, server_time_utc:(string|null), effective_time_utc:(string|null)}}
 */
function normaliseState(r) {
  const base = r && r.state && typeof r.state === "object" && "mode" in r.state ? r.state : r;
  const ext = r;
  const tz = ext.timezone_name ?? ext.timezone ?? ext.tz ?? ext.local?.timezone_name ?? null;
  const lt = ext.local_time ?? ext.local_time_iso ?? ext.local?.time ?? null;
  const st = ext.server_time_utc ?? null;
  if (st) { const t = Date.parse(st); if (Number.isFinite(t)) setClockOffset(t - Date.now()); }
  return {
    ...base, timezone: tz, local_time: lt,
    is_simulated: Boolean(ext.is_simulated), position_available: ext.position_available ?? Boolean(base.fix),
    server_time_utc: st, effective_time_utc: ext.effective_time_utc ?? null, utc_offset_hours: ext.utc_offset_hours ?? null,
    position_age_s: ext.position_age_s ?? null,
  };
}

/* ------------------------------------------------------------------ endpoints */

const iso = (d) => (d instanceof Date ? d.toISOString() : d);

export const api = {
  /** @returns {Promise<HealthResponse>} */
  health: () => request("/api/health"),
  /** @returns {Promise<ActiveState>} */
  state: async () => normaliseState(await request("/api/state")),
  /** Operator action: set the active position (explicit coordinates only). */
  setPosition: (body) => request("/api/state/position", { method: "POST", body }),
  /** Operator action: freeze (ISO string) or release (null) the selected time. */
  setTime: (selectedTimeUtc) => request("/api/state/time", { method: "POST", body: { selected_time_utc: selectedTimeUtc } }),

  /** @returns {Promise<SolarPosition>} */
  solarPosition: ({ lat, lon, elev, time }) => request("/api/solar/position", { params: { lat, lon, elev, time: iso(time) } }),
  /** @returns {Promise<SolarDayEvents>} */
  // The backend takes any instant inside the local day as `time`; `date` is sent as well because
  // CONTRACTS.md names it (unknown query parameters are ignored by FastAPI).
  solarDay: ({ lat, lon, date, time }) => request("/api/solar/day", { params: { lat, lon, date, time: iso(time) } }),
  /** @returns {Promise<IrradianceComponents[]>} */
  clearskyDay: async ({ lat, lon, date, time, stepS }) => {
    const r = await request("/api/solar/clearsky/day", { params: { lat, lon, date, time: iso(time), step_s: stepS } });
    return arrayFrom(r, ["series", "points", "items", "irradiance"]) || [];
  },

  /** @returns {Promise<WeatherSeries>} */
  forecast: ({ lat, lon, hours, variables, provider }) =>
    request("/api/weather/forecast", { params: { lat, lon, hours, variables: variables?.join(","), provider } }),
  /** Throws ApiError kind "unsupported" (501) when no provider offers observations. @returns {Promise<WeatherSeries>} */
  observations: ({ lat, lon, hours, variables }) => request("/api/weather/observations", { params: { lat, lon, hours, variables: variables?.join(",") } }),

  /** @returns {Promise<RouteInfo[]>} */
  routes: async () => arrayFrom(await request("/api/route"), ["routes", "items"]) || [],
  /** GeoJSON FeatureCollection: LineString + waypoint Point features. */
  routeGeojson: (id) => request(`/api/route/${encodeURIComponent(id)}/geojson`),
  /**
   * GET /api/route/{id}/progress -> {route_id, projection:{distance_along_m, cross_track_m, is_on_route, tolerance_m, ...},
   * distance_remaining_m, next_waypoint_name, next_waypoint_distance_m, next_control_stop_name, next_control_stop_distance_m}.
   * 409 when no fix. The fix is projected onto the route polyline by the backend.
   */
  progress: (id) => request(`/api/route/${encodeURIComponent(id)}/progress`),
  /** @returns {Promise<Trajectory>} */
  trajectory: (id, { speedKmh, startDistanceM, hours }) =>
    request(`/api/route/${encodeURIComponent(id)}/trajectory`, { params: { speed_kmh: speedKmh, start_distance_m: startDistanceM, hours } }),
  /**
   * Backend returns {forecast: RouteWeatherForecast, local_arrival_times: string[], notes: string[]};
   * a bare RouteWeatherForecast (CONTRACTS.md) is accepted too.
   * @returns {Promise<RouteWeatherForecast & {local_arrival_times:string[], notes:string[]}>}
   */
  routeWeather: async (id, { speedKmh, startDistanceM, hours }) => {
    const r = await request(`/api/route/${encodeURIComponent(id)}/weather`, { params: { speed_kmh: speedKmh, start_distance_m: startDistanceM, hours } });
    const f = r.forecast && r.forecast.samples ? r.forecast : r;
    return { ...f, local_arrival_times: r.local_arrival_times || [], notes: r.notes || [] };
  },

  /** @returns {Promise<{power:PowerEstimate[], energy:(EnergyEstimate|null), array:(object|null), notes:string[], weatherProvider:(string|null), weatherKind:(string|null)}>} */
  energyForecast: async ({ hours, speedKmh }) => {
    const r = await request("/api/energy/forecast", { params: { hours, speed_kmh: speedKmh } });
    const power = arrayFrom(r, ["power", "estimates", "power_estimates", "series", "points"]) || [];
    const energy = Array.isArray(r) ? (r.find((x) => x && "energy_wh" in x) || null) : (r.energy ?? r.energy_estimate ?? (r.energy_wh !== undefined ? r : null));
    return {
      power: power.filter((x) => x && "net_power_w" in x), energy,
      array: Array.isArray(r) ? null : r.array ?? null, notes: Array.isArray(r) ? [] : r.notes ?? [],
      weatherProvider: Array.isArray(r) ? null : r.weather_provider ?? null, weatherKind: Array.isArray(r) ? null : r.weather_kind ?? null,
    };
  },
  /**
   * GET /api/strategy/scenarios -> ScenarioResponse. Extra fields beyond the contract are
   * surfaced: data_gaps, regulatory_notes, assumptions, vehicle, alerts, optimum, human_review_required.
   * @param {{speedKmh?:number, startSoc?:number, hours?:number, startDistanceM?:number}} [p] startSoc is a fraction 0..1
   */
  scenarios: async (p = {}) => {
    const r = await request("/api/strategy/scenarios", { params: { speed_kmh: p.speedKmh, start_soc: p.startSoc, hours: p.hours, start_distance_m: p.startDistanceM } });
    const scenarios = arrayFrom(r, ["scenarios", "results"]) || [];
    return {
      scenarios: scenarios.filter((x) => x && "feasible" in x), recommendation: r.recommendation ?? null,
      dataGaps: r.data_gaps ?? [], regulatoryNotes: r.regulatory_notes ?? [], assumptions: r.assumptions ?? {}, vehicle: r.vehicle ?? null,
      alerts: r.alerts ?? [], optimalSpeedKmh: r.optimal_constant_speed_kmh ?? null, optimalFeasible: r.optimal_is_feasible ?? null,
      humanReviewRequired: r.human_review_required ?? true,
    };
  },
  /** GET /api/regulations: verified BWSC 2027 limits and what is not yet published. */
  regulations: () => request("/api/regulations"),
  /** @returns {Promise<Alert[]>} */
  alerts: async () => arrayFrom(await request("/api/alerts"), ["alerts", "items"]) || [],
};

/** Every endpoint this UI calls (documentation / footer / README check). */
export const ENDPOINTS = [
  "GET /api/health", "GET /api/state", "POST /api/state/position", "POST /api/state/time",
  "GET /api/solar/position", "GET /api/solar/day", "GET /api/solar/clearsky/day",
  "GET /api/weather/forecast", "GET /api/weather/observations",
  "GET /api/route", "GET /api/route/{id}/geojson", "GET /api/route/{id}/progress", "GET /api/route/{id}/trajectory", "GET /api/route/{id}/weather",
  "GET /api/energy/forecast", "GET /api/strategy/scenarios", "GET /api/regulations", "GET /api/alerts",
];
