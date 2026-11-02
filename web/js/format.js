/**
 * Formatting, units, age rendering and provenance text. This is the ONLY module
 * that decides how a number, unit, time, age or provenance is written, so every
 * view displays them identically.
 *
 * Honesty rules owned here:
 *  - DASH / describeValue(): missing values are a dash with a reason, never 0.
 *  - freshnessOf(): staleness is computed from the data's own reference time
 *    against a limit whose origin (API vs UI default) is carried along.
 *  - provenanceLines(): the hover/detail text for every displayed number.
 *  - kindInfo(): the OBSERVED / FORECAST / CLEAR-SKY MODEL vocabulary.
 * @module format
 */

export const DASH = "—";

/* ---------- clock ---------- */

let clockOffsetMs = 0;
/** Browser clock corrected by the API's Date header when readable (same-origin). */
export function nowMs() { return Date.now() + clockOffsetMs; }
export function setClockOffset(ms) { clockOffsetMs = Number.isFinite(ms) ? ms : 0; }
export function getClockOffset() { return clockOffsetMs; }

/** @param {string|number|Date|null|undefined} t @returns {number|null} epoch ms */
export function toMs(t) {
  if (t === null || t === undefined || t === "") return null;
  if (typeof t === "number") return t;
  const ms = t instanceof Date ? t.getTime() : Date.parse(t);
  return Number.isFinite(ms) ? ms : null;
}

/* ---------- numbers and units ---------- */

/**
 * Display registry for canonical variables (names/units from rwi.core.variables).
 * `scale` converts the API's canonical unit into the displayed engineering unit.
 */
export const VARS = {
  temperature_2m: { label: "Air temperature 2 m", short: "Temp", unit: "°C", digits: 1 },
  apparent_temperature: { label: "Apparent temperature", short: "Feels", unit: "°C", digits: 1 },
  dew_point_2m: { label: "Dew point 2 m", short: "Dew pt", unit: "°C", digits: 1 },
  relative_humidity_2m: { label: "Relative humidity 2 m", short: "RH", unit: "%", digits: 0 },
  pressure_msl: { label: "Pressure (MSL)", short: "P msl", unit: "hPa", digits: 1, scale: 0.01 },
  surface_pressure: { label: "Pressure (surface)", short: "P sfc", unit: "hPa", digits: 1, scale: 0.01 },
  wind_speed_10m: { label: "Wind speed 10 m", short: "Wind", unit: "m/s", digits: 1 },
  wind_direction_10m: { label: "Wind from", short: "Dir", unit: "°", digits: 0 },
  wind_gusts_10m: { label: "Wind gust 10 m", short: "Gust", unit: "m/s", digits: 1 },
  cloud_cover: { label: "Cloud cover (total)", short: "Cloud", unit: "%", digits: 0 },
  cloud_cover_low: { label: "Cloud cover (low)", short: "Low", unit: "%", digits: 0 },
  cloud_cover_mid: { label: "Cloud cover (mid)", short: "Mid", unit: "%", digits: 0 },
  cloud_cover_high: { label: "Cloud cover (high)", short: "High", unit: "%", digits: 0 },
  precipitation: { label: "Precipitation (per step)", short: "Precip", unit: "mm", digits: 2 },
  precipitation_probability: { label: "Precipitation probability", short: "P(precip)", unit: "%", digits: 0 },
  rain: { label: "Rain (per step)", short: "Rain", unit: "mm", digits: 2 },
  ghi: { label: "Global horizontal irradiance", short: "GHI", unit: "W/m²", digits: 0 },
  dni: { label: "Direct normal irradiance", short: "DNI", unit: "W/m²", digits: 0 },
  dhi: { label: "Diffuse horizontal irradiance", short: "DHI", unit: "W/m²", digits: 0 },
  terrestrial_radiation: { label: "Extraterrestrial horizontal", short: "TOA", unit: "W/m²", digits: 0 },
  uv_index: { label: "UV index", short: "UV", unit: "", digits: 1 },
  visibility: { label: "Visibility", short: "Vis", unit: "km", digits: 1, scale: 0.001 },
  is_day: { label: "Provider day flag", short: "is_day", unit: "", digits: 0 },
  weather_code: { label: "WMO weather code", short: "WMO", unit: "", digits: 0 },
  soil_temperature_0cm: { label: "Surface temperature", short: "T surf", unit: "°C", digits: 1 },
};

/** Fixed-decimal number with a real minus sign, or DASH for non-finite input. */
export function fmtNum(v, digits = 1) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return DASH;
  const s = Number(v).toFixed(digits);
  return s.startsWith("-") ? "−" + s.slice(1) : s;
}

/**
 * Convert a canonical value into display text + unit for a named variable.
 * @param {string} variable
 * @param {number|null|undefined} raw  value in the API's canonical unit
 * @param {number} [digitsOverride]
 * @returns {{text:string, unit:string}}
 */
export function fmtVar(variable, raw, digitsOverride) {
  const spec = VARS[variable] || { unit: "", digits: 2 };
  if (raw === null || raw === undefined || !Number.isFinite(Number(raw))) return { text: DASH, unit: spec.unit };
  const v = spec.scale ? Number(raw) * spec.scale : Number(raw);
  return { text: fmtNum(v, digitsOverride ?? spec.digits), unit: spec.unit };
}

export function varLabel(variable) { return (VARS[variable] || {}).label || variable; }
export function varUnit(variable) { return (VARS[variable] || {}).unit ?? ""; }
export function varShort(variable) { return (VARS[variable] || {}).short || variable; }
/** Scale a canonical value to the display unit (for charts). */
export function toDisplay(variable, raw) {
  const spec = VARS[variable];
  if (raw === null || raw === undefined) return null;
  return spec && spec.scale ? raw * spec.scale : raw;
}

/** "5.2 m/s (18.7 km/h)" */
export function fmtWind(mps) {
  if (mps === null || mps === undefined || !Number.isFinite(mps)) return DASH;
  return `${fmtNum(mps, 1)} m/s (${fmtNum(mps * 3.6, 1)} km/h)`;
}
export function mpsToKnots(mps) { return mps * 1.943844; }

export function fmtDistanceKm(m, digits = 1) {
  return m === null || m === undefined || !Number.isFinite(m) ? DASH : fmtNum(m / 1000, digits);
}
export function fmtPowerW(w) {
  if (w === null || w === undefined || !Number.isFinite(w)) return { text: DASH, unit: "W" };
  return Math.abs(w) >= 10000 ? { text: fmtNum(w / 1000, 2), unit: "kW" } : { text: fmtNum(w, 0), unit: "W" };
}
export function fmtEnergyWh(wh) {
  if (wh === null || wh === undefined || !Number.isFinite(wh)) return { text: DASH, unit: "Wh" };
  return Math.abs(wh) >= 10000 ? { text: fmtNum(wh / 1000, 2), unit: "kWh" } : { text: fmtNum(wh, 0), unit: "Wh" };
}
export function fmtFraction(f, digits = 1) {
  return f === null || f === undefined || !Number.isFinite(f) ? DASH : fmtNum(f * 100, digits);
}

const COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];
export function compass(deg) {
  if (deg === null || deg === undefined || !Number.isFinite(deg)) return DASH;
  return COMPASS[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
}

/** "12.46340° S, 130.84560° E" */
export function fmtLatLon(lat, lon, digits = 5) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return DASH;
  return `${fmtNum(Math.abs(lat), digits)}° ${lat < 0 ? "S" : "N"}, ${fmtNum(Math.abs(lon), digits)}° ${lon < 0 ? "W" : "E"}`;
}

/**
 * Head/tail component. Positive headwind_mps = headwind (rwi.core.wind).
 * @returns {{label:string, text:string, unit:string}}
 */
export function fmtHeadTail(headwindMps) {
  if (headwindMps === null || headwindMps === undefined || !Number.isFinite(headwindMps)) return { label: "", text: DASH, unit: "m/s" };
  const label = Math.abs(headwindMps) < 0.05 ? "" : headwindMps > 0 ? "HEAD" : "TAIL";
  return { label, text: fmtNum(Math.abs(headwindMps), 1), unit: "m/s" };
}

/**
 * Component of the TRUE (ground-frame) wind along the direction of travel.
 * Positive = headwind (wind from ahead), negative = tailwind.
 * Note: RelativeWind.headwind_mps from the API is the APPARENT headwind
 * (vehicle speed minus wind along track), so it includes the vehicle's own speed.
 * @param {number} speedMps wind speed @param {number} fromDeg direction wind blows FROM @param {number} headingDeg vehicle heading
 * @returns {number|null}
 */
export function trueHeadwind(speedMps, fromDeg, headingDeg) {
  if (![speedMps, fromDeg, headingDeg].every((x) => typeof x === "number" && Number.isFinite(x))) return null;
  return speedMps * Math.cos(((fromDeg - headingDeg) * Math.PI) / 180);
}

/* ---------- durations and ages ---------- */

/** Duration in seconds -> "3 h 05 min", "5 min 12 s", "42 s", "2 d 4 h". */
export function fmtDuration(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return DASH;
  const neg = s < 0;
  let t = Math.round(Math.abs(s));
  let out;
  if (t < 60) out = `${t} s`;
  else if (t < 3600) out = `${Math.floor(t / 60)} min ${String(t % 60).padStart(2, "0")} s`;
  else if (t < 86400) out = `${Math.floor(t / 3600)} h ${String(Math.floor((t % 3600) / 60)).padStart(2, "0")} min`;
  else out = `${Math.floor(t / 86400)} d ${Math.floor((t % 86400) / 3600)} h`;
  return neg ? "−" + out : out;
}

/** Age in seconds -> "12 min 03 s old"; negative ages are "in the future". */
export function fmtAge(s) {
  if (s === null || s === undefined || !Number.isFinite(s)) return DASH;
  return s < -5 ? `${fmtDuration(-s)} in the future` : `${fmtDuration(Math.max(0, s))} old`;
}

export function ageSeconds(t, now = nowMs()) {
  const ms = toMs(t);
  return ms === null ? null : (now - ms) / 1000;
}

/* ---------- time ---------- */

/** "2027-08-22 05:30Z" (UTC). */
export function fmtUtc(t, { seconds = false, dateOnly = false } = {}) {
  const ms = toMs(t);
  if (ms === null) return DASH;
  const iso = new Date(ms).toISOString();
  if (dateOnly) return iso.slice(0, 10);
  return iso.slice(0, seconds ? 19 : 16).replace("T", " ") + "Z";
}

const dtfCache = new Map();
function dtf(tz, seconds) {
  const key = tz + "|" + seconds;
  let f = dtfCache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat("en-AU", {
      timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined, timeZoneName: "short",
    });
    dtfCache.set(key, f);
  }
  return f;
}

/** True if the IANA zone name is usable in this browser. */
export function validTimezone(tz) {
  if (!tz || typeof tz !== "string") return false;
  try { dtf(tz, false); return true; } catch { return false; }
}

/**
 * Local civil time in an IANA zone: "2027-08-22 15:00 ACST".
 * Returns DASH when the zone is unknown - local time is never guessed.
 */
export function fmtLocal(t, tz, { seconds = false, timeOnly = false } = {}) {
  const ms = toMs(t);
  if (ms === null || !validTimezone(tz)) return DASH;
  const p = Object.fromEntries(dtf(tz, seconds).formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  const time = `${p.hour}:${p.minute}${seconds ? ":" + p.second : ""}`;
  return timeOnly ? `${time} ${p.timeZoneName}` : `${p.year}-${p.month}-${p.day} ${time} ${p.timeZoneName}`;
}

/** Local calendar date (YYYY-MM-DD) in a zone, or null. */
export function localDate(t, tz) {
  const ms = toMs(t);
  if (ms === null || !validTimezone(tz)) return null;
  const p = Object.fromEntries(dtf(tz, false).formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day}`;
}

/** Both clocks, as strings: {local, utc}. local is DASH when tz is unknown. */
export function fmtDual(t, tz, opts) {
  return { local: fmtLocal(t, tz, opts), utc: fmtUtc(t, opts) };
}

/* ---------- data kinds ---------- */

const KINDS = {
  observation: { cls: "observed", label: "OBSERVED", long: "Measured by an instrument" },
  analysis: { cls: "model", label: "ANALYSIS", long: "Model state assimilated to observations - not a raw measurement" },
  forecast: { cls: "forecast", label: "FORECAST", long: "Model prediction for a valid time - not a measurement" },
  clear_sky_model: { cls: "clearsky", label: "CLEAR-SKY MODEL · UPPER BOUND", long: "Physical clear-sky model: the most irradiance a cloudless sky could give. It is NOT measured irradiance and NOT a forecast." },
  physical_model: { cls: "model", label: "MODEL", long: "Deterministic physical model output (e.g. PV power from irradiance) - not measured" },
  simulation: { cls: "sim", label: "SIMULATION", long: "Synthetic value produced by the platform - not real" },
  derived: { cls: "derived", label: "DERIVED", long: "Computed inside this platform from other values" },
  climatology: { cls: "derived", label: "CLIMATOLOGY", long: "Long-term statistic, not a forecast for a specific day" },
};

/** @param {string|undefined} kind */
export function kindInfo(kind) {
  return KINDS[kind] || { cls: "mute", label: kind ? String(kind).toUpperCase() : "UNKNOWN KIND", long: "Data kind not reported by the API" };
}

/* ---------- freshness ---------- */

/** Active freshness limits; replaced when the API reports its own. */
export const limits = { observation: null, forecast: null, gps: null, source: { observation: "ui", forecast: "ui", gps: "ui" } };

/**
 * @param {{observation?:number, forecast?:number, gps?:number}} values
 * @param {"api"|"ui"} source
 */
export function setLimits(values, source) {
  for (const k of ["observation", "forecast", "gps"]) {
    if (Number.isFinite(values?.[k])) { limits[k] = values[k]; limits.source[k] = source; }
  }
}

/** The limit applying to a data kind: {limitS, source} (limitS null = none applies). */
export function limitForKind(kind) {
  if (kind === "observation" || kind === "analysis") return { limitS: limits.observation, source: limits.source.observation };
  if (kind === "forecast") return { limitS: limits.forecast, source: limits.source.forecast };
  return { limitS: null, source: "n/a" };
}

/**
 * Freshness of a value from its provenance.
 *  - observation: age of observation_time_utc vs the observation limit;
 *  - forecast: age of retrieved_at_utc vs the forecast limit (a forecast for a
 *    future valid time is not "old"; the fetched product can be);
 *  - model kinds: computed on request, no limit applies;
 *  - an explicit quality flag "stale" from the API always wins.
 * @param {any} prov  Provenance
 * @param {string} [quality]  QualityFlag from the API
 * @param {number} [now]
 * @returns {{state:"fresh"|"stale"|"na", ageS:number|null, limitS:number|null, limitSource:string, basis:string, reason:string}}
 */
export function freshnessOf(prov, quality, now = nowMs()) {
  if (!prov) return { state: "na", ageS: null, limitS: null, limitSource: "n/a", basis: "", reason: "no provenance" };
  const { limitS, source } = limitForKind(prov.kind);
  const isObs = prov.kind === "observation" || prov.kind === "analysis";
  const refT = isObs ? (prov.observation_time_utc || prov.valid_time_utc || prov.retrieved_at_utc) : prov.retrieved_at_utc;
  const basis = isObs ? "observation time" : "retrieval time";
  const ageS = ageSeconds(refT, now);
  if (quality === "stale") return { state: "stale", ageS, limitS, limitSource: source, basis, reason: "flagged stale by the API quality control" };
  if (limitS === null || ageS === null) return { state: "na", ageS, limitS, limitSource: source, basis, reason: "no freshness limit applies to this kind" };
  return ageS > limitS
    ? { state: "stale", ageS, limitS, limitSource: source, basis, reason: "older than limit" }
    : { state: "fresh", ageS, limitS, limitSource: source, basis, reason: "within limit" };
}

/** One-line freshness description, e.g. "STALE: 3 h 05 min old (limit 30 min 00 s, UI default limit)". */
export function freshnessText(f) {
  const src = f.limitSource === "api" ? "API-reported limit" : f.limitSource === "ui" ? "UI default limit; API did not report one" : "";
  if (f.state === "stale") return `STALE: ${fmtAge(f.ageS)} by ${f.basis}${f.limitS !== null ? ` (limit ${fmtDuration(f.limitS)}, ${src})` : `; ${f.reason}`}`;
  if (f.state === "fresh") return `Fresh: ${fmtAge(f.ageS)} by ${f.basis} (limit ${fmtDuration(f.limitS)}, ${src})`;
  return f.ageS !== null ? `${fmtAge(f.ageS)} by ${f.basis}; ${f.reason}` : f.reason;
}

/**
 * Freshness of a position fix. The GPS limit only applies to LIVE_GPS: a manual
 * coordinate does not "age out" and simulated fixes are generated by the platform.
 * @param {any} fix PositionFix
 * @param {string} mode
 * @returns {{state:"fresh"|"stale"|"na", ageS:number|null, limitS:number|null, limitSource:string}}
 */
export function fixFreshness(fix, mode, now = nowMs()) {
  if (!fix) return { state: "na", ageS: null, limitS: null, limitSource: "n/a" };
  const ageS = ageSeconds(fix.gps_time_utc || fix.received_at_utc, now);
  if (mode !== "live_gps" || limits.gps === null || ageS === null) return { state: "na", ageS, limitS: null, limitSource: "n/a" };
  return { state: ageS > limits.gps ? "stale" : "fresh", ageS, limitS: limits.gps, limitSource: limits.source.gps };
}

/* ---------- provenance ---------- */

/**
 * Provenance detail lines for hover text or a detail row.
 * @param {any} p Provenance (rwi.core.provenance.Provenance)
 * @param {{quality?:string, extra?:string[]}} [opts]
 * @returns {string[]}
 */
export function provenanceLines(p, opts = {}) {
  if (!p) return ["No provenance supplied by the API for this value."];
  const k = kindInfo(p.kind);
  const lines = [`${k.label}: ${k.long}`];
  lines.push(`Provider: ${p.provider || DASH}   Dataset: ${p.dataset || DASH}`);
  if (p.model_name || p.model_version) lines.push(`Model: ${p.model_name || DASH}${p.model_version ? " v" + p.model_version : ""}`);
  if (p.model_init_time_utc) {
    const lead = p.valid_time_utc ? ` (lead ${fmtDuration((toMs(p.valid_time_utc) - toMs(p.model_init_time_utc)) / 1000)})` : "";
    lines.push(`Model run initialised: ${fmtUtc(p.model_init_time_utc)}${lead}`);
  }
  if (p.observation_time_utc) lines.push(`Observed at: ${fmtUtc(p.observation_time_utc, { seconds: true })}`);
  if (p.valid_time_utc) lines.push(`Valid time: ${fmtUtc(p.valid_time_utc, { seconds: true })}`);
  lines.push(`Retrieved: ${fmtUtc(p.retrieved_at_utc, { seconds: true })} (${fmtAge(ageSeconds(p.retrieved_at_utc))})`);
  if (p.point) lines.push(`Location used: ${fmtLatLon(p.point.latitude_deg, p.point.longitude_deg, 4)}${p.processing ? " [" + p.processing + "]" : ""}`);
  if (p.spatial_resolution_km || p.temporal_resolution_s) {
    lines.push(`Resolution: ${p.spatial_resolution_km ? fmtNum(p.spatial_resolution_km, 1) + " km" : DASH} / ${p.temporal_resolution_s ? fmtDuration(p.temporal_resolution_s) : DASH}`);
  }
  if (p.license) lines.push(`Licence: ${p.license}`);
  if (opts.quality && opts.quality !== "ok") lines.push(`Quality flag: ${opts.quality}`);
  lines.push(freshnessText(freshnessOf(p, opts.quality)));
  if (p.notes) lines.push(`Notes: ${p.notes}`);
  if (opts.extra) lines.push(...opts.extra);
  return lines;
}

/** Same as provenanceLines, joined for a title attribute. */
export function provenanceTitle(p, opts) { return provenanceLines(p, opts).join("\n"); }

/** Short one-line source summary for a panel footer. */
export function provenanceSummary(p) {
  if (!p) return "source not reported";
  const bits = [p.provider, p.dataset];
  if (p.model_name) bits.push(p.model_name);
  return bits.filter(Boolean).join(" / ");
}

/* ---------- sizes/misc ---------- */

export function pct(v, digits = 0) { return v === null || v === undefined ? DASH : fmtNum(v, digits); }
export function titleCase(s) { return String(s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()); }
