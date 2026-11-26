/**
 * Resource loaders. Each one knows which API call a store key maps to, builds
 * the request from the ACTIVE state (never from a default coordinate), and
 * blocks itself with an explicit reason when a precondition is missing.
 * @module loaders
 */
import { api } from "./api.js";
import { load, block, res, activePoint, activeState, displayRoute, effectiveTimeMs, vehicleTimezone, absorbLimits, session } from "./store.js";
import { cfg } from "./settings.js";
import { localDate, toMs } from "./format.js";

const NO_POS = "position unknown - no request was made for a guessed location";

/** Rounded position key: refetch when the vehicle moves ~2 km or the selected time changes. */
function posKey(p) { return `${p.lat.toFixed(2)},${p.lon.toFixed(2)}`; }
function timeKey() { const s = activeState()?.selected_time_utc; return s ? "sel:" + s : "live"; }

export const POLL = { state: 0, slow: 0 };

export function loadState(force = false) {
  return load("state", { endpoint: "GET /api/state", ttlMs: 0, force }, () => api.state()).then((r) => { absorbLimits(); return r; });
}
export function loadHealth(force = false) {
  return load("health", { endpoint: "GET /api/health", ttlMs: 10000, force }, () => api.health()).then((r) => { absorbLimits(); return r; });
}
export function loadAlerts(force = false) {
  return load("alerts", { endpoint: "GET /api/alerts", ttlMs: 10000, force }, () => api.alerts());
}
export function loadRoutes(force = false) {
  return load("routes", { endpoint: "GET /api/route", ttlMs: 300000, force }, () => api.routes());
}
export async function loadRouteGeojson(force = false) {
  const dr = displayRoute();
  if (!dr) { block("routeGeojson", "no route listed by GET /api/route", "GET /api/route/{id}/geojson"); return; }
  return load("routeGeojson", { endpoint: `GET /api/route/${dr.id}/geojson`, paramsKey: dr.id, ttlMs: 600000, force }, () => api.routeGeojson(dr.id));
}

/** Local calendar date at the vehicle (falls back to UTC date, flagged). */
export function solarDate() {
  const { tz } = vehicleTimezone();
  const t = effectiveTimeMs() + session.solarDayOffset * 86400000;
  const d = tz ? localDate(t, tz) : null;
  return { date: d || new Date(t).toISOString().slice(0, 10), basis: d ? "local" : "utc" };
}

export async function loadSatcorps(force = false) {
  const p = activePoint();
  if (!p) { block("satcorps", NO_POS, "GET /api/satellite/satcorps-crosscheck"); return; }
  // Cached on disk by the backend; frontend TTL = 15 min so the dashboard
  // does not poke NASA every poll but a manual force still refreshes.
  return load("satcorps", {
    endpoint: "GET /api/satellite/satcorps-crosscheck",
    paramsKey: posKey(p),
    ttlMs: 900000,
    force,
  }, () => api.satcorpsCrossCheck({ lat: p.lat, lon: p.lon, elev: p.elev, maxAgeHours: 2880 }));
}

export async function loadGhiBias(force = false) {
  const p = activePoint();
  if (!p) { block("ghiBias", NO_POS, "GET /api/validation/ghi-bias"); return; }
  // Static table, so a long TTL is fine; keyed on position so moving along
  // the route picks up the nearest tabulated point.
  return load("ghiBias", {
    endpoint: "GET /api/validation/ghi-bias",
    paramsKey: posKey(p),
    ttlMs: 3600000,
    force,
  }, () => api.ghiBias({ lat: p.lat, lon: p.lon, elev: p.elev }));
}

export async function loadSolar(force = false) {
  const p = activePoint();
  if (!p) { for (const k of ["solarPos", "solarDay", "clearskyDay"]) block(k, NO_POS); return; }
  const t = new Date(effectiveTimeMs()).toISOString();
  const dayT = new Date(effectiveTimeMs() + session.solarDayOffset * 86400000).toISOString();
  const { date } = solarDate();
  const key = posKey(p);
  await Promise.all([
    load("solarPos", { endpoint: "GET /api/solar/position", paramsKey: key + "|" + timeKey(), ttlMs: 20000, force },
      () => api.solarPosition({ lat: p.lat, lon: p.lon, elev: p.elev, time: t })),
    load("solarDay", { endpoint: "GET /api/solar/day", paramsKey: key + "|" + date, ttlMs: 600000, force },
      () => api.solarDay({ lat: p.lat, lon: p.lon, date, time: dayT })),
    load("clearskyDay", { endpoint: "GET /api/solar/clearsky/day", paramsKey: key + "|" + date, ttlMs: 600000, force },
      () => api.clearskyDay({ lat: p.lat, lon: p.lon, date, time: dayT, stepS: 900 })),
  ]);
}

export async function loadForecast({ hours = session.forecastHours, provider = "", key = "forecast", force = false } = {}) {
  const p = activePoint();
  if (!p) { block(key, NO_POS, "GET /api/weather/forecast"); return; }
  return load(key, { endpoint: "GET /api/weather/forecast", paramsKey: `${posKey(p)}|${hours}|${provider}|${timeKey()}`, ttlMs: 120000, force },
    // No `variables` list: the backend's default set is used so one unsupported variable cannot fail the whole request.
    () => api.forecast({ lat: p.lat, lon: p.lon, hours, provider: provider || undefined }));
}

export function loadObservable(force = false) {
  return load("observable", { endpoint: "GET /api/weather/observable", ttlMs: 600000, force }, () => api.observable());
}

/**
 * Observations are requested only for variables the backend says a provider MEASURES
 * (GET /api/weather/observable). {} means the platform measures nothing: no request is made.
 */
export async function loadObservations({ hours = 24, key = "observations", force = false } = {}) {
  const p = activePoint();
  if (!p) { block(key, NO_POS, "GET /api/weather/observations"); return; }
  const ob = await loadObservable(force);
  if (ob.status !== "ok") {
    block(key, `the list of measurable variables could not be read (${ob.error?.endpoint || "GET /api/weather/observable"}: ${ob.error?.message || "not loaded"}); no observation request was made`, "GET /api/weather/observations");
    return;
  }
  const variables = [...new Set(Object.values(ob.data).flat())].sort();
  if (!variables.length) {
    block(key, "no configured provider measures any variable (GET /api/weather/observable returned an empty map), so there are no observations to request", "GET /api/weather/observations");
    return;
  }
  return load(key, { endpoint: "GET /api/weather/observations", paramsKey: `${posKey(p)}|${hours}|${variables.join(",")}`, ttlMs: 120000, force },
    () => api.observations({ lat: p.lat, lon: p.lon, hours, variables }));
}

/** What the verification history contains (archive coverage). */
export function loadArchive(force = false) {
  return load("archive", { endpoint: "GET /api/forecast/archive", ttlMs: 30000, force }, () => api.archiveStatus());
}

/** Measured forecast skill by variable and lead time (needs no position: it reads the archive). */
export function loadVerification({ days = 30, provider = "", force = false } = {}) {
  return load("verification", { endpoint: "GET /api/forecast/verification", paramsKey: `${days}|${provider}`, ttlMs: 60000, force },
    () => api.verification({ days, provider: provider || undefined }));
}

/** Ensemble spread for one variable at the active position (51 ECMWF IFS members). */
export async function loadEnsemble({ variable = "ghi", hours = 48, force = false } = {}) {
  const p = activePoint();
  if (!p) { block("ensemble", NO_POS, "GET /api/forecast/ensemble"); return; }
  return load("ensemble", { endpoint: "GET /api/forecast/ensemble", paramsKey: `${posKey(p)}|${variable}|${hours}|${timeKey()}`, ttlMs: 300000, force },
    () => api.ensemble({ variable, lat: p.lat, lon: p.lon, hours }));
}

/** EXPERIMENTAL ML quantile bands at the active position (ADR 0008). */
export async function loadMlBands({ variable = "ghi", hours = 48, force = false } = {}) {
  const p = activePoint();
  if (!p) { block("mlbands", NO_POS, "GET /api/ml/bands"); return; }
  return load("mlbands", { endpoint: "GET /api/ml/bands", paramsKey: `${posKey(p)}|${variable}|${hours}|${timeKey()}`, ttlMs: 300000, force },
    () => api.mlBands({ variable, lat: p.lat, lon: p.lon, hours }));
}

/** Several numerical models for one variable at the active position. */
export async function loadModels({ variable = "cloud_cover", hours = 48, force = false } = {}) {
  const p = activePoint();
  if (!p) { block("models", NO_POS, "GET /api/forecast/models"); return; }
  return load("models", { endpoint: "GET /api/forecast/models", paramsKey: `${posKey(p)}|${variable}|${hours}|${timeKey()}`, ttlMs: 300000, force },
    () => api.models({ variable, lat: p.lat, lon: p.lon, hours }));
}

/** Projection of the active fix onto the display route (backend map-matching). */
export async function loadProgress(force = false) {
  const dr = displayRoute();
  const p = activePoint();
  if (!dr || !p) { block("progress", !p ? NO_POS : "no route available", "GET /api/route/{id}/progress"); return; }
  return load("progress", { endpoint: `GET /api/route/${dr.id}/progress`, paramsKey: `${dr.id}|${posKey(p)}|${p.lat.toFixed(3)},${p.lon.toFixed(3)}`, ttlMs: 20000, force }, () => api.progress(dr.id));
}

/** Start distance for route calls: the state's route progress, or the operator-entered value (hypothetical). */
export function startDistance() {
  const s = activeState();
  if (s?.route_distance_m !== null && s?.route_distance_m !== undefined) return { m: s.route_distance_m, source: "state" };
  const pr = res("progress").data;
  if (pr?.projection && pr.projection.is_on_route !== false && Number.isFinite(pr.projection.distance_along_m)) return { m: pr.projection.distance_along_m, source: "projection" };
  const km = parseFloat(session.startDistanceKm);
  if (Number.isFinite(km) && km >= 0) return { m: km * 1000, source: "operator" };
  return { m: null, source: "none" };
}

export async function loadTrajectory(force = false) {
  loadProgress();
  const dr = displayRoute();
  if (!dr) { block("trajectory", "no route available from GET /api/route", "GET /api/route/{id}/trajectory"); block("routeWeather", "no route available from GET /api/route", "GET /api/route/{id}/weather"); return; }
  const sd = startDistance();
  if (sd.m === null) {
    const why = "route progress is unknown (state.route_distance_m is null) and no start distance was entered";
    block("trajectory", why, "GET /api/route/{id}/trajectory"); block("routeWeather", why, "GET /api/route/{id}/weather");
    return;
  }
  const args = { speedKmh: session.speedKmh, startDistanceM: Math.round(sd.m), hours: session.routeHours };
  const pk = `${dr.id}|${args.speedKmh}|${args.startDistanceM}|${args.hours}|${timeKey()}`;
  await Promise.all([
    load("trajectory", { endpoint: `GET /api/route/${dr.id}/trajectory`, paramsKey: pk, ttlMs: 120000, force }, () => api.trajectory(dr.id, args)),
    load("routeWeather", { endpoint: `GET /api/route/${dr.id}/weather`, paramsKey: pk, ttlMs: 120000, force }, () => api.routeWeather(dr.id, args)),
  ]);
}

export async function loadEnergy(force = false) {
  const p = activePoint();
  if (!p) { block("energy", NO_POS, "GET /api/energy/forecast"); return; }
  const hours = 24;
  return load("energy", { endpoint: "GET /api/energy/forecast", paramsKey: `${posKey(p)}|${hours}|${session.speedKmh}|${timeKey()}`, ttlMs: 120000, force },
    () => api.energyForecast({ hours, speedKmh: session.speedKmh }));
}

export async function loadScenarios(force = false) {
  const p = activePoint();
  if (!p) { block("scenarios", NO_POS, "GET /api/strategy/scenarios"); return; }
  const soc = parseFloat(session.startSocPct);
  const args = { speedKmh: session.speedKmh, startSoc: Number.isFinite(soc) ? Math.min(1, Math.max(0, soc / 100)) : undefined, hours: session.routeHours };
  return load("scenarios", { endpoint: "GET /api/strategy/scenarios", paramsKey: `${posKey(p)}|${timeKey()}|${args.speedKmh}|${args.startSoc}|${args.hours}`, ttlMs: 120000, force }, () => api.scenarios(args));
}

export function loadBriefing(kind, force = false) {
  return load(`briefing:${kind}`, { endpoint: `GET /api/meteorologist/${kind}`, ttlMs: 60000, force }, () => api.briefing(kind));
}
export function loadRegulations(force = false) {
  return load("regulations", { endpoint: "GET /api/regulations", ttlMs: 3600000, force }, () => api.regulations());
}

/** Nearest step of a WeatherSeries to a time (ms); null if none within tolerance. */
export function nearestStep(series, ms, toleranceS = 5400) {
  if (!series?.steps?.length) return null;
  let best = null, bd = Infinity;
  for (const s of series.steps) {
    const d = Math.abs(toMs(s.valid_time_utc) - ms);
    if (d < bd) { bd = d; best = s; }
  }
  return bd / 1000 <= toleranceS ? best : null;
}
export { res, cfg };
