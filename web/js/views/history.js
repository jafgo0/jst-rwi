/**
 * View H - Historical Analysis.
 * CONTRACTS.md section 4 defines no archive or session endpoint, so this view is
 * an honest empty state plus the little the API does expose (health/uptime and
 * the provider list). It never shows invented history.
 * @module views/history
 */
import { h, replace } from "../dom.js";
import { fmtDuration, fmtUtc } from "../format.js";
import { panel, kv, stateBox, withResource, ageSpan, chip } from "../ui.js";
import { res, signature } from "../store.js";
import { loadHealth } from "../loaders.js";

export function create(root) {
  const body = h("div");
  root.append(h("div", { class: "view-head" }, h("h1", null, "Historical Analysis"), h("span", { class: "sub" }, "Recorded sessions, forecast verification, replay")), body);
  let lastSig = "";
  return {
    update() {
      loadHealth();
      const sig = signature(["health"]);
      if (sig === lastSig) return;
      lastSig = sig;
      replace(body, layout());
    },
    destroy() {},
  };
}

function layout() {
  const g = h("div", { class: "grid g-2" });
  g.append(
    panel("Recorded sessions", [
      stateBox("", "No sessions recorded yet", "Nothing is stored for analysis yet, so this page shows no history rather than invented history."),
      h("p", null, "Once session recording exists, this view will list:"),
      h("ul", { class: "plain" }, [
        "recorded sessions (GPS track, active mode, start/end in UTC and vehicle-local time);",
        "forecast-versus-observation verification by variable and lead time (bias, MAE, RMSE, with sample counts);",
        "archived forecast runs, so any past decision can be reproduced with the data that was actually available at the time;",
        "historical replay of a recorded track, which will switch the platform into HISTORICAL REPLAY mode with the SIMULATION banner;",
        "alert history and the strategist's accepted / rejected recommendations.",
      ].map((t) => h("li", null, t))),
      h("div", { class: "note" }, "Placeholder: the API contract (docs/CONTRACTS.md section 4) has no archive, session or verification endpoint. The dashboard will not call endpoints that are not specified."),
    ]),
    panel("What the API can show today", withResource(res("health"), (hl) => [
      kv([
        ["API status", chip(String(hl.status).toUpperCase(), hl.status === "ok" ? "ok" : "warn")],
        ["Version", hl.version],
        ["Server uptime", h("span", null, fmtDuration(hl.uptime_s), h("span", { class: "note" }, "  (process uptime, not recording time)"))],
        ["Providers", hl.providers.length ? hl.providers.map((p) => p.provider).join(", ") : "none reported"],
        ["Health sampled", res("health").loadedAt !== null ? ageSpan(res("health").loadedAt) : "-"],
      ], "tight"),
      h("div", { class: "note", style: { marginTop: "8px" } }, "Archive data (e.g. Open-Meteo historical archive) is configured in the backend settings but no endpoint exposes it to the dashboard yet."),
    ])),
  );
  void fmtUtc;
  return g;
}
