/**
 * Application store: every API resource (data, status, error, last-good time),
 * plus the derived "active state" facts all views share (position, timezone,
 * simulation flag, effective time) and persisted UI preferences.
 *
 * A failed refresh keeps the previous `data` and records `error`, so views can
 * show the error AND how old the last successful load is (see ui.withResource).
 * @module store
 */
import { nowMs, setLimits, validTimezone, toMs } from "./format.js";
import { cfg, prefs } from "./settings.js";

/**
 * @typedef {Object} Resource
 * @property {string} key
 * @property {"idle"|"loading"|"ok"|"error"|"blocked"} status
 * @property {any} data last successful payload (kept across failures)
 * @property {import("./api.js").ApiError|null} error
 * @property {number|null} loadedAt ms epoch of last success
 * @property {number|null} requestedAt
 * @property {string} endpoint human label of the call, e.g. "GET /api/state"
 * @property {string|null} paramsKey
 * @property {string|null} blockedReason why no request was made (e.g. position unknown)
 */

const listeners = new Set();
/** @type {Map<string, Resource>} */
const resources = new Map();
const inflight = new Map();
let notifyQueued = false;

function blank(key) {
  return { key, status: "idle", data: null, error: null, loadedAt: null, requestedAt: null, endpoint: "", paramsKey: null, blockedReason: null, _sig: "" };
}

/** @returns {Resource} */
export function res(key) {
  if (!resources.has(key)) resources.set(key, blank(key));
  return resources.get(key);
}

export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }

/** Coalesced notification (one per animation frame). */
export function notify() {
  if (notifyQueued) return;
  notifyQueued = true;
  requestAnimationFrame(() => { notifyQueued = false; for (const fn of [...listeners]) { try { fn(); } catch (e) { console.error("store listener failed", e); } } });
}

/**
 * Load a resource, de-duplicating in-flight calls and honouring a TTL.
 * @param {string} key
 * @param {{endpoint:string, paramsKey?:string, ttlMs?:number, force?:boolean}} o
 * @param {() => Promise<any>} fetcher
 * @returns {Promise<Resource>}
 */
export async function load(key, o, fetcher) {
  const r = res(key);
  const pk = o.paramsKey ?? "";
  const fresh = r.loadedAt !== null && r.paramsKey === pk && nowMs() - r.loadedAt < (o.ttlMs ?? 0);
  if (!o.force && fresh && r.status !== "error") return r;
  // Do not hammer a failing endpoint: retry no sooner than 10 s after the last attempt.
  if (!o.force && r.paramsKey === pk && r.status === "error" && r.requestedAt !== null && nowMs() - r.requestedAt < 10000) return r;
  if (inflight.has(key) && r.paramsKey === pk) return inflight.get(key);
  r.endpoint = o.endpoint;
  const paramsChanged = r.paramsKey !== pk;
  r.paramsKey = pk;
  r.requestedAt = nowMs();
  r.blockedReason = null;
  const prevStatus = r.status;
  if (r.data === null || paramsChanged) { r.status = "loading"; if (paramsChanged) { r.data = null; r.loadedAt = null; r.error = null; r._sig = ""; } }
  if (prevStatus !== r.status) notify();
  const p = (async () => {
    try {
      const data = await fetcher();
      if (r.paramsKey !== pk) return r; // superseded by a newer request
      const sig = safeSig(data);
      const changed = sig !== r._sig || r.error !== null || r.status !== "ok";
      r.data = data; r._sig = sig; r.error = null; r.status = "ok"; r.loadedAt = nowMs();
      if (changed) notify();
    } catch (e) {
      if (r.paramsKey !== pk) return r;
      r.error = e; r.status = "error";
      notify();
    } finally {
      inflight.delete(key);
    }
    return r;
  })();
  inflight.set(key, p);
  return p;
}

function safeSig(d) { try { return JSON.stringify(d); } catch { return String(Math.random()); } }

/** Mark a resource as intentionally not requested (e.g. no position). */
export function block(key, reason, endpoint = "") {
  const r = res(key);
  if (r.status === "blocked" && r.blockedReason === reason) return;
  r.status = "blocked"; r.blockedReason = reason; r.error = null; r.data = null; r.loadedAt = null; r.paramsKey = null;
  if (endpoint) r.endpoint = endpoint;
  notify();
}

/* ------------------------------------------------------------- derived facts */

/** @returns {import("./api.js").ActiveState|null} */
export function activeState() { return res("state").data; }

/** @returns {{lat:number, lon:number, elev:(number|null)}|null}  null = position unknown; never a default. */
export function activePoint() {
  const f = activeState()?.fix;
  if (!f?.point || !Number.isFinite(f.point.latitude_deg) || !Number.isFinite(f.point.longitude_deg)) return null;
  return { lat: f.point.latitude_deg, lon: f.point.longitude_deg, elev: f.point.elevation_m ?? null };
}

export function isSimulation() {
  const s = activeState();
  if (!s) return false;
  return s.mode === "route_sim" || s.mode === "historical_replay" || s.fix?.mode === "route_sim" || s.fix?.mode === "historical_replay" || s.fix?.fix_quality === "simulated";
}

/**
 * Timezone of the vehicle's own position: the zone the API reports on /api/state,
 * else the zone reported on /api/solar/day for the same point. null when neither
 * is available (local time is then shown as unavailable, never guessed).
 * @returns {{tz:string|null, source:string}}
 */
export function vehicleTimezone() {
  const s = activeState();
  if (!s?.fix) return { tz: null, source: "position unknown" };
  if (s?.timezone && validTimezone(s.timezone)) return { tz: s.timezone, source: "GET /api/state" };
  const d = res("solarDay").data;
  if (d?.timezone_name && validTimezone(d.timezone_name)) return { tz: d.timezone_name, source: "GET /api/solar/day" };
  return { tz: null, source: "not reported by the API" };
}

/** Instant all calculations use: selected (frozen) time if set, else now. */
export function effectiveTimeMs() {
  const sel = toMs(activeState()?.selected_time_utc);
  return sel !== null ? sel : nowMs();
}
export function isTimeFrozen() { return toMs(activeState()?.selected_time_utc) !== null; }

/** Active route id: the state's route, else null (views may offer the first listed route explicitly). */
export function activeRouteId() {
  return activeState()?.route_id || null;
}
/** Route used for display: state route, else first listed route, flagged `assumed`. */
export function displayRoute() {
  const list = res("routes").data || [];
  const id = activeRouteId();
  if (id) return { id, info: list.find((r) => r.id === id) || null, assumed: false };
  if (list.length) return { id: list[0].id, info: list[0], assumed: true };
  return null;
}

/** Push API-reported freshness limits into format.js when they appear. */
export function absorbLimits() {
  const src = [activeState(), res("health").data];
  for (const s of src) {
    const l = s?.freshness_limits ?? s?.freshness ?? s?.limits;
    if (l && typeof l === "object") {
      setLimits({
        observation: l.max_observation_age_s ?? l.observation_s ?? l.observation,
        forecast: l.max_forecast_age_s ?? l.forecast_s ?? l.forecast,
        gps: l.max_gps_age_s ?? l.gps_s ?? l.gps,
      }, "api");
    }
  }
}
setLimits(cfg.freshnessDefaultsS, "ui");

/* ------------------------------------------------------------------- prefs */

export const session = {
  theme: prefs.get("theme", "dark") === "light" ? "light" : "dark",
  /** "local" = vehicle-local time on chart axes; "utc" */
  timeBasis: prefs.get("timeBasis", "local") === "utc" ? "utc" : "local",
  speedKmh: Number(prefs.get("speedKmh", cfg.defaultSpeedKmh)) || cfg.defaultSpeedKmh,
  startDistanceKm: prefs.get("startDistanceKm", ""),
  routeHours: Number(prefs.get("routeHours", 10)) || 10,
  forecastHours: Number(prefs.get("forecastHours", 48)) || 48,
  provider: prefs.get("provider", ""),
};

export function setSession(patch) {
  Object.assign(session, patch);
  for (const [k, v] of Object.entries(patch)) prefs.set(k, v);
  notify();
}

/**
 * Change signature of a set of resources + theme/time basis. Views use it to
 * skip a full re-render (and so keep charts, scroll and tooltips) when nothing
 * they display has changed.
 * @param {string[]} keys
 */
export function signature(keys, extra = "") {
  return keys.map((k) => { const r = res(k); return `${k}:${r.status}:${r.error ? r.error.message : ""}:${r._sig}`; }).join("|") + `|${session.theme}|${session.timeBasis}|${extra}`;
}
