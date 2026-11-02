/**
 * View A - Race Command Center.
 * Position and mode, local+UTC time at the vehicle, next control stop, current
 * weather (observed and forecast side by side), modelled solar power, energy
 * summary, active alerts and the current recommendation.
 * @module views/command
 */
import { h, replace } from "../dom.js";
import { api } from "../api.js";
import {
  DASH, fmtLatLon, fmtNum, fmtWind, fmtDistanceKm, fmtDuration, fmtUtc, fmtLocal, fmtFraction, fmtEnergyWh, fmtPowerW, compass,
  fixFreshness, freshnessText, toMs, nowMs, VARS, limits, provenanceTitle, kindInfo,
} from "../format.js";
import { panel, kv, valueCell, numCell, textCell, kindChip, modeChip, chip, severityChip, ageSpan, provFooter, withResource, positionUnknown, stateBox, freshnessNode } from "../ui.js";
import {
  res, activeState, activePoint, effectiveTimeMs, isTimeFrozen, isSimulation, vehicleTimezone, displayRoute, signature, session, controlStops, routeProgress,
} from "../store.js";
import { loadProgress, loadForecast, loadObservations, loadEnergy, loadScenarios, loadSolar, loadRouteGeojson, loadAlerts, loadHealth, loadState, nearestStep } from "../loaders.js";

const KEYS = ["state", "health", "routes", "routeGeojson", "progress", "solarDay", "forecastNow", "observationsNow", "energy", "scenarios", "alerts"];

export function create(root) {
  const body = h("div");
  const ops = operatorControls();
  root.append(h("div", { class: "view-head" }, h("h1", null, "Race Command Center"),
    h("span", { class: "sub" }, "Current position, conditions, energy and recommendation")), body, ops.el);
  let lastSig = "";
  const clock = setInterval(() => paintClocks(body), 1000);

  return {
    update() {
      loadSolar(); loadRouteGeojson(); loadProgress();
      loadForecast({ hours: 6, key: "forecastNow" }); loadObservations({ hours: 3, key: "observationsNow" });
      loadEnergy(); loadScenarios(); loadAlerts(); loadHealth();
      const sig = signature(KEYS);
      if (sig !== lastSig) { lastSig = sig; replace(body, layout()); paintClocks(body); }
      ops.sync();
    },
    destroy() { clearInterval(clock); },
  };
}

/* ----------------------------------------------------------------- layout */

function layout() {
  const st = res("state");
  const g = h("div", { class: "grid g-cc" });
  const cell = (span, node) => { const w = h("div", { class: `span-${span}`, style: { display: "flex", flexDirection: "column" } }, node); g.append(w); return w; };
  cell(5, positionPanel(st));
  cell(3, timePanel(st));
  cell(4, controlStopPanel());
  cell(7, weatherPanel());
  const right = cell(5, h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } }, solarPanel(), energyPanel()));
  right.style.display = "block";
  cell(6, recommendationPanel());
  cell(6, alertsPanel());
  cell(12, sourcesPanel());
  return g;
}

/* --------------------------------------------------------------- position */

function positionPanel(st) {
  return panel("Position and mode", withResource(st, (s) => {
    const mode = modeChip(s.mode);
    if (!s.fix || !s.fix.point) {
      return [h("div", { style: { marginBottom: "8px" } }, "Mode: ", mode), positionUnknown(
        `No position fix in mode ${s.mode}. No coordinates are shown and no weather, solar or route value is computed for a default location.`)];
    }
    const f = s.fix;
    const fr = fixFreshness(f, s.mode);
    const acc = f.accuracy_m;
    const ageRef = f.gps_time_utc || f.received_at_utc;
    return [
      h("div", { style: { marginBottom: "8px", display: "flex", gap: "8px", alignItems: "center" } }, mode,
        h("span", { class: "dim" }, `source: ${f.source}`),
        f.mode !== s.mode ? chip("MODE MISMATCH", "crit", "fix.mode differs from state.mode") : null),
      kv([
        ["Latitude / longitude", h("span", { class: "num xl", title: "WGS84, from the active position fix" }, fmtLatLon(f.point.latitude_deg, f.point.longitude_deg))],
        ["Elevation", f.point.elevation_m !== null && f.point.elevation_m !== undefined ? numCell(f.point.elevation_m, "m", { digits: 0 }) : textCell(null, "elevation unknown (not 0 m)")],
        ["Horizontal accuracy", acc !== null && acc !== undefined ? numCell(acc, "m (1σ)", { digits: 1 }) : textCell(null, "accuracy not reported")],
        ["Ground speed", f.speed_mps !== null && f.speed_mps !== undefined
          ? h("span", { class: "val" }, numCell(f.speed_mps, "m/s", { digits: 1 }), " ", numCell(f.speed_mps * 3.6, "km/h", { digits: 1 })) : textCell(null, "speed not reported")],
        ["Course / heading", f.course_deg !== null && f.course_deg !== undefined ? numCell(f.course_deg, `° ${compass(f.course_deg)}`, { digits: 0 })
          : s.heading_deg !== null && s.heading_deg !== undefined ? numCell(s.heading_deg, `° ${compass(s.heading_deg)} (state heading)`, { digits: 0 }) : textCell(null, "course not reported")],
        ["Fix quality / satellites", `${f.fix_quality || DASH}${f.satellites !== null && f.satellites !== undefined ? " / " + f.satellites + " sats" : ""}`],
        ["Fix age", h("span", { class: fr.state === "stale" ? "is-stale" : "", title: fr.state === "na" ? "No age limit for this mode" : freshnessText({ ...fr, basis: "GPS time", reason: "" }) },
          ageSpan(ageRef, fr.state === "na" ? {} : { limitS: fr.limitS, limitSource: fr.limitSource }),
          f.gps_time_utc ? ` (GPS time ${fmtUtc(f.gps_time_utc, { seconds: true })})` : " (receipt time; no GPS timestamp)")],
        ["Route progress", (() => { const rp = routeProgress(); return rp.m !== null
          ? h("span", null, numCell(rp.m / 1000, "km", { digits: 1 }), ` on ${s.route_id || displayRoute()?.id || "route"}`, rp.source === "projection" ? h("span", { class: "note" }, ` (projected from the fix; cross-track ${fmtNum(Math.abs(rp.crossTrackM ?? 0), 0)} m)`) : "")
          : textCell(null, rp.offRoute ? "fix is not on the route" : "route progress unknown"); })()],
      ], "tight"),
    ];
  }), { right: positionKindChip() });
}

/** Chip describing what the shown position IS: GPS measurement, operator entry, or simulation. */
function positionKindChip() {
  const s = activeState();
  if (!s?.fix) return null;
  if (s.mode === "live_gps") return kindChip("observation");
  if (s.mode === "manual") return chip("OPERATOR-ENTERED", "derived", "Typed in by an operator; not a measurement");
  return kindChip("simulation");
}

/* ------------------------------------------------------------------- time */

function timePanel(st) {
  return panel("Time at the vehicle", withResource(st, (s) => {
    const { tz, source } = vehicleTimezone();
    const frozen = isTimeFrozen();
    const unknownPos = !s.fix;
    return [
      isSimulation() ? h("div", { class: "lastgood" }, h("b", null, "SIMULATED "), "local time of the simulated position.") : null,
      frozen ? h("div", { class: "lastgood" }, h("b", null, "TIME FROZEN "), `at ${fmtUtc(s.selected_time_utc, { seconds: true })}. Everything is computed for this instant, not for now.`) : null,
      h("div", { class: "dim" }, "Local"),
      h("div", { class: "num xl", dataset: { clock: "local" }, title: tz ? `IANA zone ${tz} (from ${source})` : "No timezone reported" }, DASH),
      !tz ? h("div", { class: "note warn" }, unknownPos ? "Local time unavailable: position unknown." : "Local time unavailable: the API reported no timezone for this position.") : h("div", { class: "note" }, `${tz} (from ${source})`),
      h("div", { class: "dim", style: { marginTop: "8px" } }, "UTC"),
      h("div", { class: "num xl", dataset: { clock: "utc" } }, DASH),
      h("div", { class: "note", style: { marginTop: "6px" } }, frozen ? "Selected time (frozen)" : "Following real time (browser clock aligned to API Date header when readable)"),
    ];
  }));
}

function paintClocks(root) {
  const t = effectiveTimeMs();
  const { tz } = vehicleTimezone();
  const l = root.querySelector('[data-clock="local"]'), u = root.querySelector('[data-clock="utc"]');
  if (l) l.textContent = tz && activeState()?.fix ? fmtLocal(t, tz, { seconds: true }) : DASH;
  if (u) u.textContent = fmtUtc(t, { seconds: true });
}

/* ----------------------------------------------------------- control stop */

function controlStopPanel() {
  const dr = displayRoute();
  const routes = res("routes"), gj = res("routeGeojson");
  const s = activeState();
  return panel("Next control stop", withResource(routes, () => {
    if (!dr || !dr.info) return stateBox("unknown", "NO ROUTE", "GET /api/route lists no route; control stops unavailable.");
    if (gj.status === "error") return withResource(gj, () => h("span"));
    const cs = controlStops();
    const stops = [...cs.stops].sort((a, b) => a.cumulative_distance_m - b.cumulative_distance_m);
    const prog = routeProgress().m;
    const note = dr.assumed ? h("div", { class: "note warn" }, `No active route in state; showing first listed route "${dr.info.name}".`) : null;
    if (gj.status !== "ok" && !stops.length) return stateBox("loading", "Loading control stops\u2026", h("code", null, "GET /api/route/{id}/geojson"));
    if (!stops.length) return [note, stateBox("unknown", "No control stops defined", `Route "${dr.info.name}" has no control_stop waypoints (${cs.source}). The official control-stop list is not yet published; see /api/regulations.`)];
    if (prog === null || prog === undefined) {
      return [note, stateBox("unknown", "Route progress unknown", `Distance to the next stop cannot be computed: route progress is unavailable (vehicle not matched to the route). ${stops.length} control stop(s) are defined on ${dr.info.name}.`)];
    }
    const next = stops.find((c) => c.cumulative_distance_m > prog);
    if (!next) return [note, stateBox("", "No further control stop", `Progress ${fmtNum(prog / 1000, 1)} km of ${fmtNum(dr.info.total_distance_m / 1000, 1)} km; no control stop ahead.`)];
    const rem = next.cumulative_distance_m - prog;
    return [note,
      h("div", { class: "xl" }, next.name || "Unnamed control stop"),
      kv([
        ["Distance to go", numCell(rem / 1000, "km", { digits: 1, extra: ["Route distance difference: stop chainage minus route progress", `Route: ${dr.info.name}`, `Stops from ${cs.source}`] })],
        ["Stop chainage", numCell(next.cumulative_distance_m / 1000, "km along route", { digits: 1 })],
        ["Mandatory dwell", next.mandatory_dwell_s !== null && next.mandatory_dwell_s !== undefined ? numCell(next.mandatory_dwell_s / 60, "min", { digits: 0 }) : textCell(null, "dwell not specified")],
        ["Remaining route", numCell((dr.info.total_distance_m - prog) / 1000, "km", { digits: 1 })],
        ["ETA", h("span", null, textCell(null, "see Route view for ETA at an assumed speed"), " ", h("a", { href: "#/route" }, "open"))],
      ], "tight")];
  }));
}

/* ---------------------------------------------------------------- weather */

const WX_ROWS = ["temperature_2m", "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "cloud_cover", "precipitation", "precipitation_probability", "relative_humidity_2m", "pressure_msl", "visibility", "ghi", "dni", "dhi"];

function weatherPanel() {
  const fcR = res("forecastNow"), obR = res("observationsNow");
  const tms = effectiveTimeMs();
  const body = h("div");
  if (!activePoint()) { body.append(positionUnknown("Weather is requested only for a known position.")); return panel("Current weather", body); }

  const fcStep = fcR.data ? nearestStep(fcR.data, tms) : null;
  let obsStep = null;
  if (obR.data) {
    const past = obR.data.steps.filter((x) => toMs(x.valid_time_utc) <= tms + 60000);
    obsStep = past.length ? past[past.length - 1] : null;
  }
  const obsNote = obR.status === "blocked" ? obR.blockedReason : obR.status === "error"
    ? (obR.error?.kind === "unsupported" ? `Observations not available (HTTP 501): ${obR.error.message}` : `Observations unavailable: ${obR.error?.message}`)
    : obR.status === "ok" && !obsStep ? "Observation series contains no step at or before the selected time." : null;
  const fcNote = fcR.status === "error" ? `Forecast unavailable: ${fcR.error?.kind === "network" ? "API unreachable" : fcR.error?.message}` : fcR.status === "ok" && !fcStep ? "No forecast step within 90 min of the selected time." : null;

  const rows = WX_ROWS.map((v) => {
    const o = obsStep?.values?.[v], f = fcStep?.values?.[v];
    const dirNote = (m) => (v === "wind_direction_10m" && m?.value !== null && m?.value !== undefined ? ` ${compass(m.value)}` : "");
    const windNote = (m) => (v.startsWith("wind_s") || v === "wind_gusts_10m") && m?.value ? h("div", { class: "note" }, fmtWind(m.value)) : null;
    return h("tr", null, h("td", null, VARS[v].label),
      h("td", null, obsStep ? [valueCell(o, { variable: v, absentReason: "not in observation set", compactStale: true }), dirNote(o), windNote(o)] : h("span", { class: "faint" }, DASH)),
      h("td", null, fcStep ? [valueCell(f, { variable: v, absentReason: "not in forecast set", compactStale: true }), dirNote(f), windNote(f)] : h("span", { class: "faint" }, DASH)));
  });
  if (isSimulation()) body.append(h("div", { class: "note warn" }, "SIMULATION: this is real provider weather for the SIMULATED position, not for a real vehicle."));
  body.append(h("div", { class: "tbl-wrap" }, h("table", { class: "t" },
    h("thead", null, h("tr", null, h("th", null, "Variable"),
      h("th", null, kindChip("observation"), " ", obsStep ? [fmtUtc(obsStep.valid_time_utc).slice(11), " ", freshnessNodeFor(obR)] : ""),
      h("th", null, kindChip("forecast"), " ", fcStep ? ["valid ", fmtUtc(fcStep.valid_time_utc).slice(11)] : ""))),
    h("tbody", null, rows))));
  if (obsNote) body.append(h("div", { class: "note warn" }, "Observed: " + obsNote));
  if (fcNote) body.append(h("div", { class: "note warn" }, "Forecast: " + fcNote));
  if (obR.status === "loading" || fcR.status === "loading") body.append(h("div", { class: "note" }, "Loading…"));
  const footers = [];
  if (fcR.data) footers.push(["Forecast: ", fcR.data.provider, " / ", fcR.data.dataset, fcR.data.model_name ? " / " + fcR.data.model_name : "", fcR.data.model_init_time_utc ? ` (run ${fmtUtc(fcR.data.model_init_time_utc)})` : "", " retrieved ", ageSpan(fcR.data.retrieved_at_utc)]);
  if (obR.data) footers.push(["Observations: ", obR.data.provider, " / ", obR.data.dataset, " retrieved ", ageSpan(obR.data.retrieved_at_utc)]);
  return panel("Current weather", body, {
    footer: footers.length ? h("footer", { class: "prov" }, footers.map((f, i) => h("div", null, f))) : null,
    right: fcR.status === "error" ? chip("ERROR", "crit") : null,
  });
}

function freshnessNodeFor(obR) {
  const last = obR.data?.steps?.length ? obR.data : null;
  if (!last) return DASH;
  const t = last.steps[last.steps.length - 1].valid_time_utc;
  return ageSpan(t, { limitS: limits.observation, limitSource: limits.source.observation, short: true });
}

/* ------------------------------------------------------------------ solar */

function solarPanel() {
  const r = res("energy");
  return panel("Solar power now", withResource(r, (d) => {
    const tms = effectiveTimeMs();
    let best = null, bd = Infinity;
    for (const p of d.power) { const dt = Math.abs(toMs(p.time_utc) - tms); if (dt < bd) { bd = dt; best = p; } }
    if (!best || bd > 5400e3) return stateBox("unknown", "No power estimate near this time", `Energy forecast has ${d.power.length} point(s); none within 90 min of the selected time.`);
    const w = fmtPowerW(best.net_power_w);
    const assumed = best.array?.parameter_source !== "measured";
    return [
      h("div", { class: "big", title: provenanceTitle(best.provenance) },
        numCell(w.unit === "kW" ? best.net_power_w / 1000 : best.net_power_w, w.unit, { digits: w.unit === "kW" ? 2 : 0, prov: best.provenance, big: true })),
      h("div", { style: { margin: "4px 0" } }, kindChip(best.provenance?.kind || "physical_model"), " ", h("span", { class: "note" }, "modelled, not measured")),
      kv([
        ["Valid time", `${fmtUtc(best.time_utc)}`],
        ["Array DC", numCell(best.dc_power_w, "W", { digits: 0, prov: best.provenance })],
        ["POA irradiance", numCell(best.poa?.poa_global_w_m2, "W/m²", { digits: 0, prov: best.provenance })],
        ["Cell temperature", numCell(best.cell_temperature_c, "°C", { digits: 1, prov: best.provenance })],
        ["Efficiency", numCell(best.effective_efficiency * 100, "%", { digits: 1 })],
      ], "tight"),
      assumed ? h("div", { class: "note warn" }, `Array "${best.array?.name}" parameters are ${best.array?.parameter_source || "unspecified"}, not a measured JU Solar Team specification.`) : null,
    ];
  }, { blockedNode: () => positionUnknown() }), { footer: r.data?.power?.[0] ? provFooter(r.data.power[0].provenance) : null });
}

/* ----------------------------------------------------------------- energy */

function energyPanel() {
  const sc = res("scenarios"), en = res("energy");
  return panel("Energy summary", withResource(sc, (d) => {
    const rec = d.recommendation;
    const chosen = d.scenarios.find((x) => x.name === rec?.recommended_scenario) || null;
    const rows = [["Battery state of charge now", textCell(null, "no live battery telemetry endpoint in the API")]];
    if (chosen) {
      rows.push(["Scenario start SoC", h("span", null, numCell(chosen.start_soc_fraction * 100, "%", { digits: 1, prov: chosen.provenance }), " ", chip("MODEL INPUT", "mute", "Start state of charge applied by the backend (echoed in scenario assumptions: " + JSON.stringify(d.assumptions?.start_soc_fraction ?? null) + "). Not a telemetry reading."))]);
      rows.push(["Scenario end SoC", numCell(chosen.end_soc_fraction * 100, "%", { digits: 1, prov: chosen.provenance })]);
      const ne = fmtEnergyWh(chosen.net_energy_wh);
      rows.push(["Scenario net energy", numCell(ne.unit === "kWh" ? chosen.net_energy_wh / 1000 : chosen.net_energy_wh, ne.unit, { digits: ne.unit === "kWh" ? 2 : 0, prov: chosen.provenance })]);
      rows.push(["Scenario", h("span", null, chosen.name, " ", chosen.feasible ? chip("FEASIBLE", "ok") : chip("INFEASIBLE", "crit"))]);
    } else rows.push(["Recommended scenario", textCell(null, rec?.recommended_scenario ? "named scenario not in list" : "no scenario recommended")]);
    if (en.data?.energy) {
      const e = fmtEnergyWh(en.data.energy.energy_wh);
      rows.push(["Modelled array energy (window)", numCell(e.unit === "kWh" ? en.data.energy.energy_wh / 1000 : en.data.energy.energy_wh, e.unit, { digits: e.unit === "kWh" ? 2 : 0, prov: en.data.energy.provenance })]);
    }
    return kv(rows, "tight");
  }, { blockedNode: () => positionUnknown() }));
}

/* --------------------------------------------------------- recommendation */

function recommendationPanel() {
  const sc = res("scenarios");
  return panel("Current recommendation", withResource(sc, (d) => {
    const r = d.recommendation;
    if (!r) return stateBox("unknown", "NO RECOMMENDATION", "GET /api/strategy/scenarios returned no recommendation object.");
    return [
      h("div", { style: { display: "flex", gap: "8px", alignItems: "center", marginBottom: "4px" } }, h("span", { class: "xl" }, r.headline), chip(`CONFIDENCE ${String(r.confidence).toUpperCase()}`, r.confidence === "high" ? "ok" : r.confidence === "medium" ? "watch" : "warn")),
      h("div", null, r.rationale),
      r.reevaluate_after_utc ? h("div", { class: "note", style: { marginTop: "4px" } }, `Re-evaluate after ${fmtUtc(r.reevaluate_after_utc)}`) : null,
      r.uncertainty_notes?.length ? h("div", { class: "note warn", style: { marginTop: "4px" } }, `${r.uncertainty_notes.length} uncertainty note(s) - see `, h("a", { href: "#/strategy" }, "Energy & Strategy")) : null,
      h("div", { class: "note", style: { marginTop: "4px" } }, "The platform recommends; the strategist decides."),
    ];
  }, { blockedNode: () => positionUnknown() }), { footer: res("scenarios").data?.recommendation ? provFooter(res("scenarios").data.recommendation.provenance) : null });
}

/* ------------------------------------------------------------------ alerts */

const SEV_ORDER = { critical: 0, warning: 1, watch: 2, info: 3 };
function alertsPanel() {
  const r = res("alerts");
  return panel("Active alerts", withResource(r, (list) => {
    if (!list.length) return stateBox("", "No active alerts", "GET /api/alerts returned an empty list. This is not proof that conditions are benign: see data-quality alerts and source status.");
    const sorted = [...list].sort((a, b) => (SEV_ORDER[a.severity] ?? 9) - (SEV_ORDER[b.severity] ?? 9));
    const counts = ["critical", "warning", "watch", "info"].map((s) => [s, list.filter((a) => a.severity === s).length]).filter(([, n]) => n);
    return [
      h("div", { style: { display: "flex", gap: "6px", marginBottom: "6px" } }, counts.map(([s, n]) => h("span", null, severityChip(s), ` ×${n}`))),
      h("ul", { class: "plain", style: { listStyle: "none", paddingLeft: 0 } }, sorted.slice(0, 5).map((a) =>
        h("li", { title: provenanceTitle(a.provenance) }, severityChip(a.severity), " ", h("b", null, a.headline), " ", h("span", { class: "dim" }, `[${a.category}]`),
          a.recommended_action ? h("div", { class: "note" }, "Action: " + a.recommended_action) : null))),
      list.length > 5 ? h("div", { class: "note" }, `${list.length - 5} more - `, h("a", { href: "#/alerts" }, "Alerts & Risk")) : h("a", { href: "#/alerts" }, "Alerts & Risk"),
    ];
  }, { label: "alerts" }));
}

/* ------------------------------------------------------------------ sources */

function sourcesPanel() {
  const r = res("health");
  return panel("Data sources and backend capabilities", withResource(r, (hl) => {
    const caps = Object.entries(hl.capabilities || {});
    const um = hl.unavailable_modules || {};
    return [
      h("div", { style: { display: "flex", flexWrap: "wrap", gap: "14px", alignItems: "center" } },
        h("span", null, "API ", chip(String(hl.status).toUpperCase(), hl.status === "ok" ? "ok" : "warn")),
        hl.network_enabled === false ? chip("NETWORK DISABLED - providers serve cache or fail", "warn") : null,
        hl.providers.length ? hl.providers.map((p) => h("span", { title: p.message || "" }, p.provider, " ", p.reachable ? chip("REACHABLE", "ok") : chip("UNREACHABLE", "crit"),
          h("span", { class: "faint" }, p.latency_ms !== null && p.latency_ms !== undefined ? ` ${fmtNum(p.latency_ms, 0)} ms` : " latency unknown"), " checked ", ageSpan(p.checked_at_utc)))
          : h("span", { class: "faint" }, "no providers reported")),
      caps.length ? h("div", { style: { display: "flex", flexWrap: "wrap", gap: "10px", marginTop: "6px" } }, h("span", { class: "dim" }, "Backend modules:"),
        caps.map(([k, ok]) => h("span", { title: ok ? "importable" : Object.entries(um).filter(([m]) => m.includes(k)).map(([m, e]) => `${m}: ${e}`).join("\n") || "module unavailable" }, k, " ", ok ? chip("AVAILABLE", "ok") : chip("UNAVAILABLE", "crit")))) : null,
      Object.keys(hl.provider_errors || {}).length ? h("div", { class: "note warn", style: { marginTop: "4px" } }, "Provider errors: " + Object.entries(hl.provider_errors).map(([k, v]) => `${k}: ${v}`).join(" | ")) : null,
    ];
  }));
}

/* ---------------------------------------------------------- operator form */

function operatorControls() {
  const el = h("details", { style: { marginTop: "12px" } });
  const status = h("div", { class: "note", role: "status" });
  const f = {
    mode: h("select", { class: "ctl" }, ["manual", "route_sim", "historical_replay", "live_gps"].map((m) => h("option", { value: m }, m))),
    lat: h("input", { type: "number", step: "any", placeholder: "-90..90" }),
    lon: h("input", { type: "number", step: "any", placeholder: "-180..180" }),
    elev: h("input", { type: "number", step: "any", placeholder: "optional" }),
    hdg: h("input", { type: "number", step: "any", placeholder: "optional" }),
    time: h("input", { type: "datetime-local", step: "60" }),
  };
  const fld = (label, input) => h("div", { class: "field" }, h("label", null, label), input);
  const doPos = async () => {
    const lat = parseFloat(f.lat.value), lon = parseFloat(f.lon.value);
    if (!(lat >= -90 && lat <= 90) || !(lon >= -180 && lon <= 180)) { status.textContent = "Enter a valid latitude and longitude; the dashboard never supplies defaults."; return; }
    const body = { mode: f.mode.value, latitude_deg: lat, longitude_deg: lon, source: "dashboard operator" };
    if (f.elev.value !== "") body.elevation_m = parseFloat(f.elev.value);
    if (f.hdg.value !== "") body.heading_deg = parseFloat(f.hdg.value);
    status.textContent = "Sending POST /api/state/position …";
    try { await api.setPosition(body); status.textContent = "Position updated."; await loadState(true); } catch (e) { status.textContent = `POST /api/state/position failed: ${e.message}`; }
  };
  const doTime = async (release) => {
    let v = null;
    if (!release) {
      if (!f.time.value) { status.textContent = "Pick a UTC date/time to freeze."; return; }
      v = new Date(f.time.value + ":00Z").toISOString();
    }
    status.textContent = "Sending POST /api/state/time …";
    try { await api.setTime(v); status.textContent = release ? "Following real time." : `Time frozen at ${fmtUtc(v)}.`; await loadState(true); } catch (e) { status.textContent = `POST /api/state/time failed: ${e.message}`; }
  };
  el.append(h("summary", null, "Operator controls (changes the shared platform state for every viewer)"),
    h("div", { class: "panel", style: { marginTop: "8px" } }, h("div", { class: "body" },
      h("div", { class: "row" }, fld("Mode", f.mode), fld("Latitude (deg)", f.lat), fld("Longitude (deg)", f.lon), fld("Elevation (m)", f.elev), fld("Heading (deg)", f.hdg),
        h("button", { class: "btn primary", onclick: doPos }, "Set position")),
      h("div", { class: "row", style: { marginTop: "8px" } }, fld("Freeze time (UTC)", f.time),
        h("button", { class: "btn", onclick: () => doTime(false) }, "Freeze time"), h("button", { class: "btn", onclick: () => doTime(true) }, "Follow real time")),
      status)));
  return { el, sync() {} };
}

