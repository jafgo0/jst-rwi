/**
 * View H - Historical Analysis.
 * CONTRACTS.md section 4 defines no archive or session endpoint, so this view is
 * an honest empty state plus the little the API does expose (health/uptime and
 * the provider list). It never shows invented history.
 * @module views/history
 */
import { h, replace } from "../dom.js";
import { fmtDuration, fmtUtc, fmtNum, DASH } from "../format.js";
import { panel, kv, stateBox, withResource, ageSpan, chip, table, textCell } from "../ui.js";
import { res, signature, notify } from "../store.js";
import { loadHealth, loadArchive } from "../loaders.js";
import { api } from "../api.js";
import { pairsExplanation } from "./forecastquality.js";

/** Last manual capture result (this page session only). */
const capture = { running: false, result: null, error: null, at: null };

/**
 * Archive status. Three different states, never merged:
 *  - endpoint failed        -> error panel (the status itself is unknown)
 *  - available === false    -> NO ARCHIVE (or a broken one: the message says which)
 *  - available, zero rows   -> an archive that exists and is EMPTY
 */
function archivePanel() {
  const r = res("archive");
  return panel("Forecast archive (verification history)", withResource(r, (a) => {
    if (!a.available) return stateBox("error", "ARCHIVE NOT AVAILABLE", `${a.message || "no reason given by the API"}. Nothing can be verified until an archive exists and is readable. This is different from an empty archive.`);
    const c = a.coverage || {};
    const fc = c.forecast_value_count ?? 0, ob = c.observation_value_count ?? 0;
    const out = [];
    if (!(c.value_count > 0)) out.push(stateBox("unknown", "ARCHIVE IS EMPTY", "The archive exists and is readable but holds no rows yet. Run a capture cycle or enable the capture worker."));
    else {
      out.push(kv([
        ["Forecast rows", h("b", { class: "num" }, fc.toLocaleString("en-US"))],
        ["Observation rows", h("b", { class: "num" }, ob.toLocaleString("en-US"))],
        ["Series", String(c.series_count ?? DASH)],
        ["Valid-time range", c.earliest_valid_time_utc ? `${fmtUtc(c.earliest_valid_time_utc)} to ${fmtUtc(c.latest_valid_time_utc)}` : DASH],
        ["Providers", (c.providers || []).map((p) => `${p.provider} (${p.series_count} series, ${p.value_count} values)`).join("; ") || DASH],
        ["Store", `${c.path || DASH} (schema ${c.schema_version ?? DASH}, ${c.file_size_bytes != null ? fmtNum(c.file_size_bytes / 1024, 0) + " KiB" : DASH})`],
      ], "tight"));
      if (fc === 0) out.push(h("div", { class: "note warn", style: { marginTop: "6px" } }, `${ob.toLocaleString("en-US")} observation rows but ZERO forecast rows: nothing can be scored. Forecast capture stores nothing while the forecast provider fails (see the capture result below).`));
      if (ob === 0 && fc > 0) out.push(h("div", { class: "note warn", style: { marginTop: "6px" } }, "Forecasts are archived but there are no observation rows, so nothing can be scored."));
    }
    const why = pairsExplanation(c); if (why) out.push(why);
    if ((c.by_variable || []).length) out.push(h("div", { style: { marginTop: "8px" } }, table([
      { h: "Variable", cell: (v) => v.variable },
      { h: "Forecast rows", cls: "r", cell: (v) => String(v.forecast_value_count) },
      { h: "Observation rows", cls: "r", cell: (v) => String(v.observation_value_count) },
      { h: "Scoreable pairs", cls: "r", cell: (v) => (v.pair_count > 0 ? String(v.pair_count) : textCell(null, "0 pairs")) },
      { h: "Valid-time range", cell: (v) => `${fmtUtc(v.earliest_valid_time_utc)} to ${fmtUtc(v.latest_valid_time_utc)}` },
    ], c.by_variable, { maxHeight: "240px" })));
    out.push(kv([["Scheduled capture", a.capture_enabled ? h("span", null, chip("ENABLED", "ok"), ` every ${fmtDuration(a.capture_interval_s)}`) : h("span", null, chip("DISABLED", "warn"), " no scheduled capture is running in this API process (the worker is a separate process: python -m rwi.capture)")]], "tight"));
    return out;
  }, { label: "archive status" }), { footer: r.loadedAt !== null ? h("footer", { class: "prov" }, "GET /api/forecast/archive, loaded ", ageSpan(r.loadedAt)) : null });
}

/** Operator control: one capture cycle now. This makes REAL provider calls and writes to the archive. */
function capturePanel() {
  const sites = h("input", { class: "ctl", placeholder: "default sites", style: { width: "220px" } });
  const status = h("div", { role: "status" });
  const paint = () => {
    replace(status,
      capture.running ? h("div", { class: "note" }, "Running one capture cycle (real provider calls; can take a minute)\u2026") : null,
      capture.error ? stateBox("error", "CAPTURE FAILED", `${capture.error.endpoint}: ${capture.error.message}`) : null,
      capture.result ? h("div", { class: "state", style: { marginTop: "6px" } }, h("h3", null, `Capture finished ${capture.at ? fmtUtc(capture.at, { seconds: true }) : ""}`),
        h("div", null, capture.result.summary),
        kv([["Forecast rows stored", String(capture.result.forecast_rows)], ["Observation rows stored", String(capture.result.observation_rows)]], "tight"),
        capture.result.failures.length ? h("div", null, h("b", { style: { color: "var(--crit)" } }, `${capture.result.failures.length} failure(s)`), h("ul", { class: "plain" }, capture.result.failures.map((f) => h("li", null, f)))) : h("div", { class: "note" }, "No failures reported."),
        capture.result.notes.length ? h("ul", { class: "plain" }, capture.result.notes.map((n) => h("li", null, n))) : null) : null);
  };
  const run = async () => {
    if (capture.running) return;
    if (!window.confirm("Run one capture cycle now?\n\nThis makes real calls to the weather providers (Open-Meteo, BoM) and writes forecasts and observations into the archive.")) return;
    capture.running = true; capture.error = null; paint();
    try {
      capture.result = await api.capture(sites.value.trim() || undefined);
      capture.at = Date.now();
    } catch (e) { capture.error = e; capture.result = null; }
    capture.running = false; paint();
    loadArchive(true); notify();
  };
  paint();
  return panel("Seed the archive (operator)", [
    h("div", { class: "note warn", style: { marginBottom: "6px" } }, "POST /api/forecast/capture runs one capture cycle now. It makes real provider calls (rate-limited, non-commercial free tiers) and writes to the archive. Use it to seed verification history, not to refresh the dashboard."),
    h("div", { class: "row" }, h("div", { class: "field" }, h("label", null, "Site ids (optional, comma-separated)"), sites), h("button", { class: "btn primary", onclick: run }, "Run capture cycle now")),
    status]);
}

export function create(root) {
  const body = h("div");
  root.append(h("div", { class: "view-head" }, h("h1", null, "Historical Analysis"), h("span", { class: "sub" }, "Recorded sessions, forecast verification, replay")), body);
  let lastSig = "";
  return {
    update() {
      loadHealth(); loadArchive();
      const sig = signature(["health", "archive"], capture.running + "|" + (capture.result ? capture.at : "") + "|" + (capture.error ? "e" : ""));
      if (sig === lastSig) return;
      lastSig = sig;
      replace(body, layout());
    },
    destroy() {},
  };
}

function layout() {
  const top = h("div", { class: "grid g-2", style: { marginBottom: "12px", alignItems: "start" } }, archivePanel(), capturePanel());
  const g = h("div", { class: "grid g-2" });
  const wrap = h("div", null, top, g);
  g.append(
    panel("Recorded sessions", [
      stateBox("", "No sessions recorded yet", "Nothing is stored for analysis yet, so this page shows no history rather than invented history."),
      h("p", null, "Once session recording exists, this view will list:"),
      h("ul", { class: "plain" }, [
        "recorded sessions (GPS track, active mode, start/end in UTC and vehicle-local time);",
        "archived forecast runs, so any past decision can be reproduced with the data that was actually available at the time;",
        "historical replay of a recorded track, which will switch the platform into HISTORICAL REPLAY mode with the SIMULATION banner;",
        "alert history and the strategist's accepted / rejected recommendations.",
      ].map((t) => h("li", null, t))),
      h("div", { class: "note" }, "Session recording does not exist yet (no endpoint). Forecast verification is live in the Forecast Center; its archive is shown above."),
    ]),
    panel("What the API can show today", withResource(res("health"), (hl) => [
      kv([
        ["API status", chip(String(hl.status).toUpperCase(), hl.status === "ok" ? "ok" : "warn")],
        ["Version", hl.version],
        ["Server uptime", h("span", null, fmtDuration(hl.uptime_s), h("span", { class: "note" }, "  (process uptime, not recording time)"))],
        ["Providers", hl.providers.length ? hl.providers.map((p) => p.provider).join(", ") : "none reported"],
        ["Backend modules", Object.keys(hl.capabilities || {}).length ? Object.entries(hl.capabilities).map(([k, ok]) => `${k}: ${ok ? "available" : "UNAVAILABLE"}`).join(", ") : "not reported"],
        ["Health sampled", res("health").loadedAt !== null ? ageSpan(res("health").loadedAt) : "-"],
      ], "tight"),
      h("div", { class: "note", style: { marginTop: "8px" } }, "Forecast verification history is built in the archive above."),
    ])),
  );
  return wrap;
}
