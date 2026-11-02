/**
 * View F - Energy & Strategy.
 * Scenario comparison (generation, consumption, net, end SoC, arrival, feasible,
 * violations) with infeasible rows marked and blocking violations spelled out,
 * plus the recommendation, its rationale, uncertainty and safety notes.
 * Decision support only: no control commands are issued from here.
 * @module views/strategy
 */
import { h, replace } from "../dom.js";
import { DASH, fmtNum, fmtUtc, fmtLocal, fmtDuration, provenanceTitle } from "../format.js";
import { panel, kv, numCell, textCell, kindChip, chip, withResource, positionUnknown, stateBox, provFooter, table } from "../ui.js";
import { res, vehicleTimezone, signature, session, setSession } from "../store.js";
import { loadScenarios, loadRegulations } from "../loaders.js";
import { alertCard } from "./alerts.js";

export function create(root) {
  const body = h("div");
  const speed = h("input", { type: "number", min: "10", max: "130", step: "1", value: session.speedKmh });
  const soc = h("input", { type: "number", min: "0", max: "100", step: "1", placeholder: "backend default", value: session.startSocPct });
  const apply = () => {
    const sp = parseFloat(speed.value);
    if (!(sp > 0 && sp <= 130)) return;
    setSession({ speedKmh: sp, startSocPct: soc.value });
    loadScenarios(true);
  };
  const controls = h("div", { class: "row", style: { marginBottom: "10px" } },
    h("div", { class: "field" }, h("label", null, "Baseline speed (km/h)"), speed),
    h("div", { class: "field" }, h("label", null, "Start SoC (%) - model input"), soc),
    h("button", { class: "btn primary", onclick: apply }, "Re-evaluate scenarios"),
    h("span", { class: "note" }, "Both values are operator inputs to the model, not telemetry. Blank SoC uses the backend default, echoed under Inputs applied by the backend."));
  root.append(h("div", { class: "view-head" }, h("h1", null, "Energy & Strategy"),
    h("span", { class: "sub" }, "Scenario comparison and recommendation. The platform recommends; the strategist decides.")), controls, body);
  let lastSig = "";
  return {
    update() {
      loadScenarios(); loadRegulations();
      const sig = signature(["state", "scenarios", "regulations"]);
      if (sig === lastSig) return;
      lastSig = sig;
      replace(body, layout());
    },
    destroy() {},
  };
}

function layout() {
  const r = res("scenarios");
  return withResource(r, (d) => {
    const wrap = h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } });
    wrap.append(recommendationPanel(d), scenariosPanel(d));
    if (d.alerts?.length) wrap.append(panel(`Strategy alerts (${d.alerts.length})`, d.alerts.map((a) => alertCard(a))));
    wrap.append(contextPanel(d), regulationsPanel());
    return wrap;
  }, { label: "scenarios", blockedNode: () => positionUnknown("Scenarios are evaluated only from a known position.") });
}

/** Defaults the backend echoes in `assumptions`: [key, label, formatter]. */
const ECHO = [
  ["start_soc_fraction", "Start state of charge", (v) => numCell(v * 100, "%", { digits: 1 })],
  ["start_distance_m", "Start distance along route", (v) => numCell(v / 1000, "km", { digits: 1 })],
  ["baseline_speed_kmh", "Baseline speed", (v) => numCell(v, "km/h", { digits: 0 })],
  ["horizon_hours", "Horizon", (v) => numCell(v, "h", { digits: 1 })],
  ["posted_limit_kmh", "Posted speed limit", (v) => numCell(v, "km/h", { digits: 0 })],
  ["uncertainty_fraction", "Irradiance/wind uncertainty", (v) => numCell(v * 100, "%", { digits: 0 })],
  ["route_id", "Route", (v) => String(v)],
  ["segment_count", "Segments", (v) => String(v)],
  ["forecast_points", "Forecast points used", (v) => String(v)],
  ["generation_points", "Generation points used", (v) => String(v)],
];

/** Data gaps, regulatory notes, model parameters and assumptions returned with the scenarios. */
function contextPanel(d) {
  const list = (items, empty) => (items?.length ? h("ul", { class: "plain" }, items.map((t) => h("li", null, t))) : textCell(null, empty));
  const veh = d.vehicle;
  const a = d.assumptions || {};
  const rest = Object.entries(a).filter(([k]) => !ECHO.some(([e]) => e === k));
  return h("div", { class: "grid g-3" },
    panel("Data gaps and regulatory notes", [
      h("h3", { style: { margin: "0 0 4px" } }, "Data gaps (what the evaluation could NOT use)"),
      list(d.dataGaps, "the API reported no data gaps (not a guarantee of completeness)"),
      h("h3", { style: { margin: "10px 0 4px" } }, "Regulatory notes"),
      list(d.regulatoryNotes, "no regulatory notes supplied"),
      d.humanReviewRequired ? h("div", { class: "note warn", style: { marginTop: "8px" } }, "HUMAN REVIEW REQUIRED: the API flags every result as decision support.") : null,
    ]),
    panel("Model parameters and assumptions", [
      kv([
        ["Vehicle model", veh ? h("span", null, veh.name, " ", chip(String(veh.parameter_source || "unspecified").toUpperCase(), veh.parameter_source === "measured" ? "ok" : "warn", veh.source_notes || "")) : textCell(null, "not reported")],
        ["Battery capacity", veh ? numCell(veh.battery_capacity_wh, "Wh", { digits: 0 }) : textCell(null, "not reported")],
        ["Battery reserve", veh ? numCell(veh.battery_reserve_fraction * 100, "%", { digits: 0 }) : textCell(null, "not reported")],
        ["Mass / CdA", veh ? h("span", null, numCell(veh.mass_kg, "kg", { digits: 0 }), " ", numCell(veh.drag_area_cda_m2, "m\u00b2", { digits: 3 })) : textCell(null, "not reported")],
        ["Optimum constant speed", d.optimalSpeedKmh != null ? h("span", null, numCell(d.optimalSpeedKmh, "km/h", { digits: 1 }), " ", d.optimalFeasible ? chip("FEASIBLE", "ok") : chip("INFEASIBLE", "crit")) : textCell(null, "optimiser gave no result")],
      ], "tight"),
      veh && veh.parameter_source !== "measured" ? h("div", { class: "note warn" }, "Vehicle parameters are placeholders: results are illustrative and are not predictions for the team's car.") : null
    ]),
    panel("Inputs applied by the backend (echoed)", [
      h("div", { class: "note", style: { marginBottom: "4px" } }, "Every default the backend applied is echoed in the response; none of these are telemetry or measurements."),
      kv(ECHO.map(([key, label, fmt]) => [label, key in a ? (a[key] === null ? textCell(null, "none supplied") : fmt(a[key])) : textCell(null, "not echoed by the API")]), "tight"),
      rest.length ? h("details", { style: { marginTop: "6px" } }, h("summary", null, `Other assumptions (${rest.length})`),
        h("dl", { class: "kv tight" }, rest.flatMap(([k, v]) => [h("dt", null, k.replace(/_/g, " ")), h("dd", { style: { whiteSpace: "normal" } }, typeof v === "object" ? JSON.stringify(v) : String(v))]))) : null,
    ]));
}

/** GET /api/regulations: what the platform enforces and what is not (yet) published. */
function regulationsPanel() {
  const r = res("regulations");
  const render = (g) => {
    const items = (arr) => h("ul", { class: "plain" }, (arr || []).map((t) => h("li", null, t)));
    const left = kv([
      ["Regulations version", `${g.version || DASH}${g.read_on ? ", read " + g.read_on : ""}`],
      ["Event timezone", g.event_timezone || DASH],
      ["Event dates", Array.isArray(g.event_dates) ? g.event_dates.join(" to ") : DASH],
      ["Daily driving window (ACST)", g.daily_start_acst && g.daily_stop_acst ? `${g.daily_start_acst} to ${g.daily_stop_acst}` : DASH],
      ["Control stop duration", g.control_stop_duration_s != null ? numCell(g.control_stop_duration_s / 60, "min", { digits: 0 }) : DASH],
      ["Night driving prohibited", g.night_driving_prohibited == null ? DASH : String(g.night_driving_prohibited)],
      ["Min open-road speed", g.min_open_road_speed_kmh != null ? numCell(g.min_open_road_speed_kmh, "km/h", { digits: 0 }) : DASH],
    ], "tight");
    const right = h("div", null,
      h("h3", { style: { margin: "0 0 4px" } }, "Not yet published"), items(g.not_yet_published),
      h("h3", { style: { margin: "8px 0 4px" } }, "Regulations are silent on"), items(g.silent_in_regulations));
    const limits = Object.entries(g.class_limits || {});
    const classTable = limits.length ? table([
      { h: "Class", cell: ([k]) => k },
      { h: "Max collector area", cls: "r", cell: ([, v]) => (v.max_collector_area_m2 != null ? numCell(v.max_collector_area_m2, "m\u00b2", { digits: 1 }) : textCell(null, "not stated")) },
      { h: "Max energy storage", cls: "r", cell: ([, v]) => (v.max_energy_storage_wh != null ? numCell(v.max_energy_storage_wh, "Wh", { digits: 0 }) : textCell(null, "not stated")) },
      { h: "Max energy storage", cls: "r", cell: ([, v]) => (v.max_energy_storage_mj != null ? numCell(v.max_energy_storage_mj, "MJ", { digits: 1 }) : textCell(null, "not stated")) },
      { h: "Clause(s)", cell: ([, v]) => (v.clauses ? (Array.isArray(v.clauses) ? v.clauses.join(", ") : typeof v.clauses === "object" ? Object.entries(v.clauses).map(([a, b]) => `${a}: ${b}`).join("; ") : String(v.clauses)) : textCell(null, "no clause supplied")) },
    ], limits) : null;
    return h("div", null, h("div", { class: "grid g-2" }, left, right), classTable ? h("div", { style: { marginTop: "10px" } }, h("h3", { style: { margin: "0 0 4px" } }, "Class limits (with clause numbers)"), classTable) : null,
      h("div", { class: "note", style: { marginTop: "6px" } }, "Only values returned by GET /api/regulations are shown. Where the API supplies no clause number for a figure, none is invented; verify against the regulations document linked below."));
  };
  return panel("Regulatory context (BWSC 2027)", withResource(r, render, { label: "regulations" }),
    { footer: r.data?.url ? h("footer", { class: "prov" }, "Source: ", r.data.url) : null });
}

function recommendationPanel(d) {
  const r = d.recommendation;
  if (!r) return panel("Recommendation", stateBox("unknown", "NO RECOMMENDATION", "The API returned scenarios but no recommendation object."));
  const conf = { high: "ok", medium: "watch", low: "warn" }[r.confidence] || "info";
  const list = (items) => h("ul", { class: "plain" }, items.map((t) => h("li", null, t)));
  return panel("Recommendation", h("div", { class: "grid", style: { gridTemplateColumns: "minmax(0,3fr) minmax(0,2fr)" } },
    h("div", null,
      h("div", { style: { display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap" } }, h("span", { style: { fontSize: "18px", fontWeight: 600 } }, r.headline), chip(`CONFIDENCE ${String(r.confidence).toUpperCase()}`, conf)),
      rationaleNode(r.rationale),
      h("dl", { class: "kv tight" },
        h("dt", null, "Recommended scenario"), h("dd", null, r.recommended_scenario || textCell(null, "none recommended")),
        h("dt", null, "Alternatives"), h("dd", null, r.alternatives?.length ? r.alternatives.join(", ") : textCell(null, "none listed")),
        h("dt", null, "Energy impact"), h("dd", null, r.energy_impact_wh != null ? numCell(r.energy_impact_wh, "Wh", { digits: 0, prov: r.provenance }) : textCell(null, "not quantified")),
        h("dt", null, "Time impact"), h("dd", null, r.time_impact_s != null ? numCell(r.time_impact_s / 60, "min", { digits: 1, prov: r.provenance }) : textCell(null, "not quantified")),
        h("dt", null, "Re-evaluate after"), h("dd", null, r.reevaluate_after_utc ? fmtUtc(r.reevaluate_after_utc) : textCell(null, "not specified"))),
      r.evidence?.length ? h("div", { style: { marginTop: "8px" } }, h("b", null, "Evidence"), list(r.evidence)) : h("div", { class: "note", style: { marginTop: "8px" } }, "No evidence list supplied.")),
    h("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } },
      h("div", { class: "state" }, h("h3", null, "Uncertainty"), r.uncertainty_notes?.length ? list(r.uncertainty_notes) : textCell(null, "no uncertainty notes supplied (absence is not certainty)")),
      h("div", { class: "state", style: { borderColor: "var(--warn)" } }, h("h3", null, "Safety and regulatory notes"), r.safety_and_regulatory_notes?.length ? list(r.safety_and_regulatory_notes) : textCell(null, "no safety/regulatory notes supplied")))),
    { footer: provFooter(r.provenance), right: [kindChip(r.provenance?.kind || "derived")] });
}

/** The backend joins per-scenario reasons with " || "; show them as a list, not one run-on paragraph. */
function rationaleNode(text) {
  const parts = String(text || "").split(/\s*\|\|\s*/).filter(Boolean);
  if (parts.length < 2) return h("p", { style: { margin: "6px 0" } }, text);
  return h("div", { style: { margin: "6px 0" } }, h("div", null, parts[0]), h("ul", { class: "plain", style: { marginTop: "4px" } }, parts.slice(1).map((t) => h("li", null, t))));
}

function scenariosPanel(d) {
  const { tz } = vehicleTimezone();
  const recName = d.recommendation?.recommended_scenario;
  const whCell = (v, s) => numCell(v, "Wh", { digits: 0, prov: s.provenance });
  const cols = [
    { h: "Scenario", cell: (s) => h("div", null, h("b", null, s.name), s.name === recName ? [" ", chip("RECOMMENDED", "observed")] : "", h("div", { class: "note", style: { whiteSpace: "normal", maxWidth: "260px" } }, s.description)) },
    { h: "Speed", cls: "r", cell: (s) => numCell(s.target_speed_kmh, "km/h", { digits: 0 }) },
    { h: "Distance", cls: "r", cell: (s) => numCell(s.distance_m / 1000, "km", { digits: 1 }) },
    { h: "Generation", cls: "r", cell: (s) => whCell(s.energy_generated_wh, s) },
    { h: "Consumption", cls: "r", cell: (s) => whCell(s.energy_consumed_wh, s) },
    { h: "Net", cls: "r", cell: (s) => whCell(s.net_energy_wh, s) },
    { h: "Start SoC", cls: "r", cell: (s) => numCell(s.start_soc_fraction * 100, "%", { digits: 1 }) },
    { h: "End SoC", cls: "r", cell: (s) => h("span", { class: s.end_soc_fraction < 0 ? "is-stale" : "" }, numCell(s.end_soc_fraction * 100, "%", { digits: 1, prov: s.provenance })) },
    { h: "Arrival (local | UTC)", cell: (s) => (s.arrival_utc ? h("div", null, tz ? fmtLocal(s.arrival_utc, tz) : textCell(null, "no timezone"), h("div", { class: "note" }, fmtUtc(s.arrival_utc))) : textCell(null, "not reached within horizon")) },
    { h: "Travel time", cls: "r", cell: (s) => fmtDuration(s.travel_time_s) },
    { h: "Feasible", cell: (s) => { const blocking = (s.violations || []).filter((v) => v.severity === "blocking"); return s.feasible ? chip("FEASIBLE", "ok") : h("span", null, chip("INFEASIBLE", "crit"), blocking.length ? h("div", { class: "note" }, `${blocking.length} blocking`) : ""); } },
    { h: "Violations", cls: "r", cell: (s) => { const v = s.violations || []; return v.length ? `${v.filter((x) => x.severity === "blocking").length} blocking, ${v.filter((x) => x.severity === "warning").length} warning` : "none"; } },
  ];
  // Uncertainty notes identical across every scenario are shown once, below the table.
  const allNotes = d.scenarios.map((x) => x.uncertainty_notes || []);
  const shared = d.scenarios.length > 1 ? (allNotes[0] || []).filter((n) => allNotes.every((l) => l.includes(n))) : [];
  const detail = (s) => {
    const v = s.violations || [];
    const assumptions = Object.entries(s.assumptions || {});
    if (!v.length && !s.uncertainty_notes?.filter((n) => !shared.includes(n)).length && !assumptions.length) return null;
    return h("div", null,
      v.length ? h("div", null, v.map((x) => h("div", { style: { margin: "2px 0" } }, chip(x.severity === "blocking" ? "BLOCKING" : "WARNING", x.severity === "blocking" ? "crit" : "warn"), " ", h("b", null, x.constraint), ": ", x.detail))) : null,
      s.uncertainty_notes?.filter((n) => !shared.includes(n)).length ? h("div", { class: "note", style: { marginTop: "4px" } }, "Uncertainty: " + s.uncertainty_notes.filter((n) => !shared.includes(n)).join(" | ")) : null,
      assumptions.length ? h("details", { style: { marginTop: "4px" } }, h("summary", null, `Assumptions (${assumptions.length})`), h("div", { class: "note" }, assumptions.map(([k, val]) => `${k.replace(/_/g, " ")}: ${val}`).join("  |  "))) : null);
  };
  const prov = d.scenarios[0]?.provenance;
  return panel(`Scenario comparison (${d.scenarios.length})`, d.scenarios.length
    ? [h("div", { class: "note", style: { marginBottom: "6px" } }, "Infeasible scenarios are shaded red; blocking violations are listed under the row. Energy values are model results: ", kindChip(prov?.kind || "physical_model"), " see the assumptions before relying on them."),
      table(cols, d.scenarios, { rowClass: (s) => (s.feasible ? "" : "bad"), detail }),
      shared.length ? h("div", { class: "state", style: { marginTop: "8px" } }, h("h3", null, "Uncertainty common to every scenario"), h("ul", { class: "plain" }, shared.map((n) => h("li", null, n)))) : null]
    : stateBox("unknown", "No scenarios", "GET /api/strategy/scenarios returned an empty list."),
  { footer: prov ? provFooter(prov) : null });
}

void DASH; void fmtNum; void provenanceTitle;
