/*
 * JST-RWI dashboard configuration (classic script, not a module, so it also
 * loads when index.html is opened straight from disk).
 *
 * Edit this file to point the dashboard at a different API or map style.
 * Every key is optional; anything omitted falls back to the defaults in
 * js/settings.js. Nothing here is a vehicle position or a weather value.
 */
window.RWI_CONFIG = {
  /*
   * API base URL, no trailing slash.
   *   ""  (empty)  -> same origin when served over http(s) (API serves the UI at
   *                   http://localhost:8000/), or http://localhost:8000 when the
   *                   page is opened from file://.
   * A "?api=http://host:port" query parameter overrides this for one session.
   */
  apiBase: "",

  /* Per-request timeout. Provider-backed endpoints can be slow. */
  requestTimeoutMs: 20000,

  /* Polling intervals (ms). */
  pollStateMs: 5000,
  pollSlowMs: 30000,

  /*
   * Base map.
   *
   * Default: the free MapLibre demo tiles. They are a coarse world vector layer
   * (Natural Earth country polygons, coastline, graticule) - geographically
   * real but NOT road-level. Use a MapTiler (or any MapLibre-compatible) style
   * for street / terrain detail:
   *
   *   styleUrl:  "https://api.maptiler.com/maps/streets-v2/style.json",
   *   apiKey:    "YOUR_MAPTILER_KEY",   // appended as ?key=... (see apiKeyParam)
   *   providerName: "MapTiler Streets v2",
   *   licenceText:  "(c) MapTiler (c) OpenStreetMap contributors",
   *   requiresOsmCredit: true,
   *
   * The footer prints providerName + licenceText exactly as configured, so keep
   * them truthful. Keys placed here are visible to anyone who can load the page:
   * use a domain-restricted key.
   */
  map: {
    styleUrl: "https://demotiles.maplibre.org/style.json",
    apiKey: "",
    apiKeyParam: "key",
    providerName: "MapLibre demo tiles (demotiles.maplibre.org)",
    licenceText: "Natural Earth data, public domain; style and tiles by MapLibre contributors",
    requiresOsmCredit: false,
  },

  /*
   * Freshness limits (seconds) used ONLY when the API does not report its own.
   * They mirror rwi.config.Settings defaults. Whenever a limit comes from here
   * the UI says "UI default limit", never presents it as an API setting.
   */
  freshnessDefaultsS: {
    observation: 1800,
    forecast: 10800,
    gps: 30,
  },

  /* Operator-parameter defaults (not measurements). */
  defaultSpeedKmh: 80,
};
