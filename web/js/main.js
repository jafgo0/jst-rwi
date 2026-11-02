/**
 * Entry point: chrome (nav, status chips, banners, footer), hash router,
 * polling, theme and time-basis toggles.
 *
 * Honesty rules owned here:
 *  - renderBanners(): SIMULATION banner (persistent, no dismiss control), API
 *    error banner, stale-position banner.
 *  - renderStatus(): "position unknown" in the header when no fix exists.
 * @module main
 */
import { h, replace } from "./dom.js";
import { cfg, apiBase } from "./settings.js";
import { res, subscribe, session, setSession, isSimulation, activeState, vehicleTimezone } from "./store.js";
import { loadState, loadHealth, loadAlerts, loadRoutes } from "./loaders.js";
import { startTicker, ageSpan, modeChip, chip } from "./ui.js";
import { DASH, fmtDuration, limits, fixFreshness, getClockOffset } from "./format.js";
import * as command from "./views/command.js";
import * as mapView from "./views/map.js";
import * as solar from "./views/solar.js";
import * as forecast from "./views/forecast.js";
import * as route from "./views/route.js";
import * as strategy from "./views/strategy.js";
import * as alerts from "./views/alerts.js";
import * as history from "./views/history.js";

const VIEWS = [
  { id: "command", title: "Command Center", nav: "Command", mod: command },
  { id: "map", title: "Weather Map", nav: "Map", mod: mapView },
  { id: "solar", title: "Solar Intelligence", nav: "Solar", mod: solar },
  { id: "forecast", title: "Forecast Center", nav: "Forecast", mod: forecast },
  { id: "route", title: "Route Intelligence", nav: "Route", mod: route },
  { id: "strategy", title: "Energy & Strategy", nav: "Strategy", mod: strategy },
  { id: "alerts", title: "Alerts & Risk", nav: "Alerts", mod: alerts },
  { id: "history", title: "Historical Analysis", nav: "History", mod: history },
];

let current = null;
let currentId = null;

/* ------------------------------------------------------------------ theme */

function applyTheme() {
  document.documentElement.setAttribute("data-theme", session.theme);
}

/* ------------------------------------------------------------------- chrome */

function renderNav() {
  const nav = document.getElementById("nav");
  const alertsList = res("alerts").data || [];
  const urgent = alertsList.filter((a) => a.severity === "critical" || a.severity === "warning").length;
  replace(nav, VIEWS.map((v) => h("a", { href: "#/" + v.id, title: v.title, class: v.id === currentId ? "active" : "" },
    v.nav, v.id === "alerts" && urgent ? h("span", { class: "badge", title: `${urgent} warning/critical alert(s)` }, urgent) : null)));
}

function renderTools() {
  const el = document.getElementById("tools");
  replace(el,
    h("span", { class: "seg", title: "Time basis for chart axes. Tables always show vehicle-local and UTC." },
      ["local", "utc"].map((b) => h("button", { class: session.timeBasis === b ? "on" : "", onclick: () => setSession({ timeBasis: b }) }, b === "local" ? "Local" : "UTC"))),
    h("button", { class: "btn", title: "Toggle light / dark theme", onclick: () => { setSession({ theme: session.theme === "dark" ? "light" : "dark" }); applyTheme(); } },
      session.theme === "dark" ? "Light theme" : "Dark theme"));
}

/** Header status: API connectivity + position summary. */
function renderStatus() {
  const el = document.getElementById("status");
  const r = res("state");
  const s = r.data;
  const nodes = [];
  if (r.status === "error") nodes.push(chip("API UNREACHABLE", "crit", r.error?.message));
  else if (r.status === "ok") nodes.push(chip("API OK", "ok"));
  else nodes.push(chip("API …", "mute"));
  if (r.loadedAt !== null) nodes.push(h("span", { class: "faint", title: "Age of the last successful GET /api/state" }, "state ", ageSpan(r.loadedAt, { limitS: Math.max(15, cfg.pollStateMs / 1000 * 3) })));
  if (s) {
    nodes.push(modeChip(s.mode));
    if (!s.fix) nodes.push(chip("POSITION UNKNOWN", "warn", "No fix: nothing is computed for a guessed location"));
  }
  replace(el, h("div", { class: "tools" }, nodes));
}

/**
 * Persistent banners. The SIMULATION banner has no close control and is rebuilt
 * from the state on every render, so it cannot be hidden while mode is simulated.
 */
function renderBanners() {
  const el = document.getElementById("banners");
  const out = [];
  const s = activeState();
  if (isSimulation()) {
    out.push(h("div", { class: "banner sim", role: "alert" }, h("span", null, "SIMULATION"),
      h("small", null, `Position mode is ${s.mode === "historical_replay" ? "HISTORICAL REPLAY" : "ROUTE SIMULATION"}: the vehicle position, local time at the vehicle and everything derived from them are NOT a real vehicle.`)));
  }
  const hl = res("health").data;
  if (hl && hl.network_enabled === false) {
    out.push(h("div", { class: "banner warn", role: "alert" }, h("span", null, "OFFLINE MODE"), h("small", null, "The backend has network access disabled: weather providers serve cache or fail; nothing is fetched live.")));
  }
  const st = res("state");
  if (st.status === "error") {
    out.push(h("div", { class: "banner err", role: "alert" }, h("span", null, "API ERROR"),
      h("small", null, `${st.error?.endpoint || "GET /api/state"} at ${apiBase() || location.origin}: ${st.error?.message}. `, st.loadedAt !== null ? ["Showing data last loaded ", ageSpan(st.loadedAt), " - NOT current."] : "Nothing has been loaded yet.")));
  }
  if (s?.fix) {
    const f = fixFreshness(s.fix, s.mode);
    if (f.state === "stale") {
      out.push(h("div", { class: "banner warn", role: "alert" }, h("span", null, "STALE POSITION"),
        h("small", null, `Last GPS fix is ${fmtDuration(f.ageS)} old (limit ${fmtDuration(f.limitS)}, ${f.limitSource === "api" ? "API-reported" : "UI default limit"}). Values below describe where the vehicle WAS.`)));
    }
  }
  replace(el, out);
}

function renderFooter() {
  const el = document.getElementById("footer");
  const m = cfg.map;
  const health = res("health").data;
  const tz = vehicleTimezone();
  const src = (k) => (limits.source[k] === "api" ? "/api/health" : "UI default fallback (health not loaded)");
  replace(el,
    h("span", null, h("b", null, "Basemap: "), m.providerName, " — ", m.licenceText, m.requiresOsmCredit ? " — © OpenStreetMap contributors" : "",
      " — rendered with MapLibre GL JS 4.7.1"),
    h("span", null, h("b", null, "API: "), apiBase() || location.origin, health ? ` — v${health.version}, up ${fmtDuration(health.uptime_s)}` : " — health not loaded"),
    h("span", null, h("b", null, "Freshness limits: "), `observation ${limits.observation !== null ? fmtDuration(limits.observation) : DASH} (${src("observation")}), forecast ${limits.forecast !== null ? fmtDuration(limits.forecast) : DASH} (${src("forecast")}), GPS ${limits.gps !== null ? fmtDuration(limits.gps) : DASH} (${src("gps")})`),
    h("span", null, h("b", null, "Vehicle timezone: "), tz.tz || "unknown", ` (${tz.source})`),
    h("span", null, h("b", null, "Clock: "), getClockOffset() ? `browser clock corrected by ${(getClockOffset() / 1000).toFixed(1)} s from API Date header` : "browser clock, uncorrected"),
    h("span", null, "Decision support only: the platform recommends, humans decide."));
}

function renderChrome() {
  renderNav(); renderStatus(); renderBanners(); renderFooter(); renderTools();
}

/* ------------------------------------------------------------------ router */

function parseHash() {
  const id = (location.hash.replace(/^#\/?/, "").split(/[/?]/)[0]) || "command";
  return VIEWS.find((v) => v.id === id) || VIEWS[0];
}

function mount() {
  const v = parseHash();
  if (current && currentId === v.id) return;
  if (current) { try { current.destroy(); } catch (e) { console.error(e); } }
  currentId = v.id;
  const root = document.getElementById("view");
  root.replaceChildren();
  document.title = `${v.title} — JST-RWI`;
  current = v.mod.create(root);
  renderChrome();
  safeUpdate();
}

function safeUpdate() {
  if (!current) return;
  try { current.update(); } catch (e) {
    console.error("view update failed", e);
    const root = document.getElementById("view");
    root.prepend(h("div", { class: "state error" }, h("h3", null, "View rendering error"), String(e?.stack || e)));
  }
}

/* ------------------------------------------------------------------- boot */

function boot() {
  applyTheme();
  startTicker();
  subscribe(() => { renderChrome(); safeUpdate(); });
  window.addEventListener("hashchange", mount);
  mount();

  const fast = () => loadState();
  const slow = () => { loadHealth(); loadAlerts(); loadRoutes(); };
  fast(); slow();
  setInterval(fast, cfg.pollStateMs);
  setInterval(slow, cfg.pollSlowMs);
  setInterval(safeUpdate, 15000); // lets TTL-expired views refetch
  window.__rwiBooted = true;
}

boot();
