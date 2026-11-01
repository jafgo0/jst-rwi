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
import { panel, numCell, textCell, kindChip, chip, withResource, positionUnknown, stateBox, provFooter, table } from "../ui.js";
import { res, vehicleTimezone, signature } from "../store.js";
import { loadScenarios } from "../loaders.js";

export function create(root) {
  const body = h("div");
  root.append(h("div", { class: "view-head" }, h("h1", null, "Energy & Strategy"),
    h("span", { class: "sub" }, "Scenario comparison and recommendation. The platform recommends; the strategist decides.")), body);
  let lastSig = "";
  return {
    update() {
      loadScenarios();
      const sig = signature(["state", "scenarios"]);
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
    return wrap;
  }, { label: "scenarios", blockedNode: () => positionUnknown("Scenarios are evaluated only from a known position.") });
}

function recommendationPanel(d) {
  const r = d.recommendation;
  if (!r) return panel("Recommendation", stateBox("unknown", "NO RECOMMENDATION", "The API returned scenarios but no recommendation object."));
  const conf = { high: "ok", medium: "watch", low: "warn" }[r.confidence] || "info";
  const list = (items) => h("ul", { class: "plain" }, items.map((t) => h("li", null, t)));
  return panel("Recommendation", h("div", { class: "grid", style: { gridTemplateColumns: "minmax(0,3fr) minmax(0,2fr)" } },
    h("div", null,
      h("div", { style: { display: "flex", gap: "10px", alignItems: "center", flexWrap: "wrap" } }, h("span", { style: { fontSize: "18px", fontWeight: 600 } }, r.headline), chip(`CONFIDENCE ${String(r.confidence).toUpperCase()}`, conf)),
      h("p", { style: { margin: "6px 0" } }, r.rationale),
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
  const detail = (s) => {
    const v = s.violations || [];
    const assumptions = Object.entries(s.assumptions || {});
    if (!v.length && !s.uncertainty_notes?.length && !assumptions.length) return null;
    return h("div", null,
      v.length ? h("div", null, v.map((x) => h("div", { style: { margin: "2px 0" } }, chip(x.severity === "blocking" ? "BLOCKING" : "WARNING", x.severity === "blocking" ? "crit" : "warn"), " ", h("b", null, x.constraint), ": ", x.detail))) : null,
      s.uncertainty_notes?.length ? h("div", { class: "note", style: { marginTop: "4px" } }, "Uncertainty: " + s.uncertainty_notes.join(" | ")) : null,
      assumptions.length ? h("details", { style: { marginTop: "4px" } }, h("summary", null, `Assumptions (${assumptions.length})`), h("div", { class: "note" }, assumptions.map(([k, val]) => `${k.replace(/_/g, " ")}: ${val}`).join("  |  "))) : null);
  };
  const prov = d.scenarios[0]?.provenance;
  return panel(`Scenario comparison (${d.scenarios.length})`, d.scenarios.length
    ? [h("div", { class: "note", style: { marginBottom: "6px" } }, "Infeasible scenarios are shaded red; blocking violations are listed under the row. Energy values are model results: ", kindChip(prov?.kind || "physical_model"), " see the assumptions before relying on them."),
      table(cols, d.scenarios, { rowClass: (s) => (s.feasible ? "" : "bad"), detail })]
    : stateBox("unknown", "No scenarios", "GET /api/strategy/scenarios returned an empty list."),
  { footer: prov ? provFooter(prov) : null });
}

void DASH; void fmtNum; void provenanceTitle;
