/**
 * Effective configuration = built-in defaults overlaid with window.RWI_CONFIG
 * (web/config.js). Also resolves the API base URL.
 * @module settings
 */

const DEFAULTS = {
  apiBase: "",
  requestTimeoutMs: 20000,
  pollStateMs: 5000,
  pollSlowMs: 30000,
  map: {
    styleUrl: "https://demotiles.maplibre.org/style.json",
    apiKey: "",
    apiKeyParam: "key",
    providerName: "MapLibre demo tiles (demotiles.maplibre.org)",
    licenceText: "Natural Earth data, public domain; style and tiles by MapLibre contributors",
    requiresOsmCredit: false,
  },
  freshnessDefaultsS: { observation: 1800, forecast: 10800, gps: 30 },
  defaultSpeedKmh: 80,
  // Shared secret for POST /api/state/position with mode=live_gps (must equal the
  // backend's RWI_TELEMETRY_SHARED_SECRET). Set it in the gitignored config.js only.
  telemetry: { sharedSecret: "", computerLocationIntervalS: 10 },
};

function merge(base, over) {
  const out = { ...base };
  for (const [k, v] of Object.entries(over || {})) {
    out[k] = v && typeof v === "object" && !Array.isArray(v) && typeof base[k] === "object" ? merge(base[k], v) : v;
  }
  return out;
}

/** @type {typeof DEFAULTS} */
export const cfg = merge(DEFAULTS, /** @type {any} */ (window).RWI_CONFIG);

/**
 * Resolve the API base URL. Precedence: ?api= query parameter, config.apiBase,
 * then same-origin (http) or http://localhost:8000 (file://).
 * @returns {string} base URL without trailing slash ("" means same origin)
 */
export function apiBase() {
  let q = null;
  try { q = new URLSearchParams(location.search).get("api"); } catch { /* ignore */ }
  const raw = q || cfg.apiBase || (location.protocol === "file:" ? "http://localhost:8000" : "");
  return raw.replace(/\/+$/, "");
}

/** Style URL with the optional API key appended. */
export function mapStyleUrl() {
  const m = cfg.map;
  if (!m.apiKey) return m.styleUrl;
  const sep = m.styleUrl.includes("?") ? "&" : "?";
  return `${m.styleUrl}${sep}${encodeURIComponent(m.apiKeyParam || "key")}=${encodeURIComponent(m.apiKey)}`;
}

/** localStorage helpers that never throw. */
export const prefs = {
  get(key, fallback = null) {
    try { const v = localStorage.getItem("rwi." + key); return v === null ? fallback : v; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem("rwi." + key, String(value)); } catch { /* storage unavailable */ }
  },
};
