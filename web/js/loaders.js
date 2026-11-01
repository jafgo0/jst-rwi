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
  const t = effectiveTimeMs();
  const d = tz ? localDate(t, tz) : null;
  return { date: d || new Date(t).toISOString().slice(0, 10), basis: d ? "local" : "utc" };
}

export async function loadSolar(force = false) {
  const p = activePoint();
  if (!p) { for (const k of ["solarPos", "solarDay", "clearskyDay"]) block(k, NO_POS); return; }
  const t = new Date(effectiveTimeMs()).toISOString();
  const { date } = solarDate();
  const key = posKey(p);
  await Promise.all([
    load("solarPos", { endpoint: "GET /api/solar/position", paramsKey: key + "|" + timeKey(), ttlMs: 20000, force },
      () => api.solarPosition({ lat: p.lat, lon: p.lon, elev: p.elev, time: t })),
    load("solarDay", { endpoint: "GET /api/solar/day", paramsKey: key + "|" + date, ttlMs: 600000, force },
      () => api.solarDay({ lat: p.lat, lon: p.lon, date })),
    load("clearskyDay", { endpoint: "GET /api/solar/clearsky/day", paramsKey: key + "|" + date, ttlMs: 600000, force },
      () => api.clearskyDay({ lat: p.lat, lon: p.lon, date, stepS: 900 })),
  ]);
}

export async function loadForecast({ hours = session.forecastHours, provider = "", key = "forecast", force = false } = {}) {
  const p = activePoint();
  if (!p) { block(key, NO_POS, "GET /api/weather/forecast"); return; }
  return load(key, { endpoint: "GET /api/weather/forecast", paramsKey: `${posKey(p)}|${hours}|${provider}|${timeKey()}`, ttlMs: 120000, force },
    () => api.forecast({ lat: p.lat, lon: p.lon, hours, provider: provider || undefined,
      variables: ["temperature_2m", "relative_humidity_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high", "precipitation", "pressure_msl", "ghi", "dni", "dhi"] }));
}

export async function loadObservations({ hours = 24, key = "observations", force = false } = {}) {
  const p = activePoint();
  if (!p) { block(key, NO_POS, "GET /api/weather/observations"); return; }
  return load(key, { endpoint: "GET /api/weather/observations", paramsKey: `${posKey(p)}|${hours}`, ttlMs: 120000, force },
    () => api.observations({ lat: p.lat, lon: p.lon, hours }));
}

/** Start distance for route calls: the state's route progress, or the operator-entered value (hypothetical). */
export function startDistance() {
  const s = activeState();
  if (s?.route_distance_m !== null && s?.route_distance_m !== undefined) return { m: s.route_distance_m, source: "state" };
  const km = parseFloat(session.startDistanceKm);
  if (Number.isFinite(km) && km >= 0) return { m: km * 1000, source: "operator" };
  return { m: null, source: "none" };
}

export async function loadTrajectory(force = false) {
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
  if (!activePoint()) { block("scenarios", NO_POS, "GET /api/strategy/scenarios"); return; }
  return load("scenarios", { endpoint: "GET /api/strategy/scenarios", paramsKey: `${posKey(activePoint())}|${timeKey()}`, ttlMs: 120000, force }, () => api.scenarios());
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
