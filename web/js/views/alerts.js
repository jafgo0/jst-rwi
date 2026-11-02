/**
 * View G - Alerts & Risk.
 * Alerts grouped by severity with evidence, recommended action and provenance,
 * including data-quality and provider-health alerts, plus provider health.
 * @module views/alerts
 */
import { h, replace } from "../dom.js";
import { DASH, fmtNum, fmtUtc, fmtLatLon, provenanceTitle } from "../format.js";
import { panel, severityChip, chip, withResource, stateBox, ageSpan, kindChip, table } from "../ui.js";
import { res, signature, notify } from "../store.js";
import { loadAlerts, loadHealth } from "../loaders.js";

const ORDER = ["critical", "warning", "watch", "info"];
const SYSTEM_CATS = new Set(["data_quality", "provider", "position"]);
const filter = { cat: "all" };

export function create(root) {
  const body = h("div");
  root.append(h("div", { class: "view-head" }, h("h1", null, "Alerts & Risk"), h("span", { class: "sub" }, "Weather risk, energy risk and the health of the data feeding them")), body);
  let lastSig = "";
  return {
    update() {
      loadAlerts(); loadHealth();
      const sig = signature(["alerts", "health", "state"], filter.cat);
      if (sig === lastSig) return;
      lastSig = sig;
      replace(body, layout());
    },
    destroy() {},
  };
}

function layout() {
  const r = res("alerts");
  const wrap = h("div", { style: { display: "grid", gridTemplateColumns: "minmax(0,2fr) minmax(0,1fr)", gap: "12px", alignItems: "start" } });
  const main = withResource(r, (list) => {
    const cats = ["all", ...new Set(list.map((a) => a.category))];
    const sel = h("select", { class: "ctl", onchange: (e) => { filter.cat = e.target.value; notify(); } },
      cats.map((c) => h("option", { value: c, selected: c === filter.cat ? "" : null }, c)));
    const shown = list.filter((a) => filter.cat === "all" || a.category === filter.cat);
    const out = [h("div", { class: "row", style: { marginBottom: "8px" } }, h("div", { class: "field" }, h("label", null, "Category"), sel), h("span", { class: "note" }, `${shown.length} of ${list.length} alert(s)`))];
    if (!list.length) out.push(stateBox("", "No active alerts", "GET /api/alerts returned an empty list. An empty list means no rule fired; it does not by itself prove the data behind the rules is complete or current. Check provider health on the right."));
    for (const sev of ORDER) {
      const group = shown.filter((a) => a.severity === sev);
      if (!group.length) continue;
      out.push(h("h3", { style: { margin: "10px 0 6px" } }, severityChip(sev), ` ${group.length}`));
      group.forEach((a) => out.push(alertCard(a)));
    }
    return out;
  }, { label: "alerts" });
  wrap.append(panel("Active alerts", main), healthPanel());
  return wrap;
}

export function alertCard(a) {
  const sys = SYSTEM_CATS.has(a.category);
  return h("div", { class: `alert sev-${a.severity}`, style: { marginBottom: "8px" }, title: provenanceTitle(a.provenance) },
    h("h3", null, severityChip(a.severity), chip(a.category.replace(/_/g, " ").toUpperCase(), sys ? "warn" : "info", sys ? "Data-quality / system alert" : "Hazard alert"), sys ? chip("DATA HEALTH", "mute") : null, h("span", null, a.headline)),
    h("div", { style: { margin: "4px 0" } }, a.detail),
    a.evidence?.length ? h("div", null, h("b", null, "Evidence: "), h("ul", { class: "plain" }, a.evidence.map((e) => h("li", null, e)))) : h("div", { class: "note" }, "No evidence listed."),
    a.recommended_action ? h("div", { class: "act" }, h("b", null, "Recommended action: "), a.recommended_action) : null,
    h("div", { class: "meta" }, "Issued ", fmtUtc(a.issued_at_utc), " (", ageSpan(a.issued_at_utc), ")",
      a.valid_window ? `  ·  valid ${fmtUtc(a.valid_window.start_utc)} to ${fmtUtc(a.valid_window.end_utc)}` : "",
      a.location ? `  ·  at ${fmtLatLon(a.location.latitude_deg, a.location.longitude_deg, 3)}` : "",
      a.distance_along_route_m != null ? `  ·  ${fmtNum(a.distance_along_route_m / 1000, 1)} km along route` : "",
      `  ·  source ${a.provenance?.provider || DASH}/${a.provenance?.dataset || DASH} `, a.provenance ? kindChip(a.provenance.kind, { short: true }) : ""));
}

function healthPanel() {
  const r = res("health");
  return panel("Provider health", withResource(r, (hl) => [
    h("div", { style: { marginBottom: "6px" } }, "API status ", chip(String(hl.status).toUpperCase(), hl.status === "ok" ? "ok" : "warn"), h("span", { class: "note" }, `  v${hl.version}`)),
    hl.providers.length ? table([
      { h: "Provider", cell: (p) => p.provider },
      { h: "State", cell: (p) => (p.reachable ? chip("REACHABLE", "ok") : chip("UNREACHABLE", "crit")) },
      { h: "Latency", cls: "r", cell: (p) => (p.latency_ms != null ? `${fmtNum(p.latency_ms, 0)} ms` : DASH) },
      { h: "Checked", cell: (p) => ageSpan(p.checked_at_utc, { limitS: 300 }) },
      { h: "Message", cell: (p) => p.message || DASH },
    ], hl.providers) : stateBox("unknown", "No providers reported", "GET /api/health lists no provider."),
    h("div", { class: "note" }, "'Checked' flips to STALE after 5 min (UI default for health checks; the API reports no limit)."),
  ]));
}
