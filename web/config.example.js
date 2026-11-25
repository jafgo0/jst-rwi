/*
 * JST-RWI dashboard configuration — EXAMPLE.
 *
 * Copy this file to `web/config.js` on each operator machine. `web/config.js`
 * is kept OUT of git (see .gitignore) so operator-specific values, API keys
 * and tokens never land in version control.
 *
 * Every key is optional; anything omitted falls back to the defaults in
 * `web/js/settings.js`. Nothing here is a vehicle position or a weather value.
 */
window.RWI_CONFIG = {
  /*
   * API base URL, no trailing slash. "" = same origin (API serves the UI).
   */
  apiBase: "",

  requestTimeoutMs: 20000,
  pollStateMs: 5000,
  pollSlowMs: 30000,

  /*
   * Base map. Default: free MapLibre demo tiles (coarse, no road detail).
   *
   * MapTiler Streets:
   *   styleUrl:  "https://api.maptiler.com/maps/streets-v2/style.json",
   *   apiKey:    "YOUR_MAPTILER_KEY",
   *   apiKeyParam: "key",
   *   providerName: "MapTiler Streets v2",
   *   licenceText:  "(c) MapTiler (c) OpenStreetMap contributors",
   *   requiresOsmCredit: true,
   *
   * Mapbox (via MapLibre, no Mapbox SDK):
   *   styleUrl:  "https://api.mapbox.com/styles/v1/mapbox/streets-v12",
   *   apiKey:    "YOUR_MAPBOX_PK_TOKEN",       // pk.*, URL-referrer restricted
   *   apiKeyParam: "access_token",
   *   providerName: "Mapbox Streets v12",
   *   licenceText:  "(c) Mapbox (c) OpenStreetMap contributors",
   *   requiresOsmCredit: true,
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
   * Fallback freshness limits (seconds). Used ONLY until GET /api/health has
   * been read; the backend's own limits replace these on first response.
   */
  freshnessDefaultsS: { observation: 1800, forecast: 10800, gps: 30 },

  defaultSpeedKmh: 80,

  /*
   * Live position from this computer's location services ("Use computer
   * location" under Operator controls). It is sent as mode=live_gps with
   * source=computer_location_services and fix_quality=unknown, so it is never
   * passed off as a GNSS receiver fix. sharedSecret must equal the backend's
   * RWI_TELEMETRY_SHARED_SECRET; without it the backend answers 403.
   * Keep the real value in config.js (gitignored), never in this template.
   */
  telemetry: { sharedSecret: "", computerLocationIntervalS: 10 },
};
