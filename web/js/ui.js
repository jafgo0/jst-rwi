/**
 * Shared UI components. Every displayed number goes through `valueCell` /
 * `numCell` so unit, provenance hover, kind chip and stale marking cannot be
 * forgotten; every API-backed region goes through `withResource` so loading,
 * blocked, error and "last loaded N ago" states are rendered uniformly.
 *
 * Honesty rules owned here:
 *  - kindChip(): OBSERVED / FORECAST / CLEAR-SKY MODEL are visually distinct.
 *  - valueCell(): value + unit + provenance title + stale marker + dash-with-reason.
 *  - positionUnknown(): the "POSITION UNKNOWN" panel; no coordinates are drawn.
 *  - withResource()/errorPanel(): API-unreachable state with endpoint, error and last-good age.
 *  - ageSpan() + startTicker(): live ages that flip to STALE when a limit passes.
 * @module ui
 */
import { h, append } from "./dom.js";
import { res as storeRes } from "./store.js";
import {
  DASH, fmtVar, fmtNum, fmtAge, fmtDuration, kindInfo, freshnessOf, freshnessText,
  provenanceTitle, nowMs, ageSeconds, toMs, provenanceSummary, fmtUtc,
} from "./format.js";

/* ------------------------------------------------------------------ chips */

/** @param {string} kind DataKind  @param {{short?:boolean}} [o] */
export function kindChip(kind, o = {}) {
  const k = kindInfo(kind);
  let label = k.label;
  if (o.short && kind === "clear_sky_model") label = "CLEAR-SKY MODEL";
  return h("span", { class: `chip ${k.cls}`, title: k.long }, label);
}

export function chip(text, cls = "info", title) { return h("span", { class: `chip ${cls}`, title }, text); }

export function severityChip(sev) {
  const cls = { critical: "crit", warning: "warn", watch: "watch", info: "info" }[sev] || "info";
  return chip(String(sev || "unknown").toUpperCase(), cls);
}

const MODE_LABEL = { live_gps: "LIVE GPS", manual: "MANUAL", route_sim: "ROUTE SIMULATION", historical_replay: "HISTORICAL REPLAY" };
export function modeChip(mode) {
  const sim = mode === "route_sim" || mode === "historical_replay";
  return chip(MODE_LABEL[mode] || String(mode || "unknown").toUpperCase(), sim ? "sim" : mode === "live_gps" ? "observed" : "info",
    sim ? "Simulated position: nothing on screen describes a real vehicle" : "Position mode");
}

/* ----------------------------------------------------------------- ages */

/**
 * Live age text. `ref` is the instant the age counts from; `limitS` (optional)
 * makes it flip to STALE with the limit shown. Updated once per second by the ticker.
 * @param {string|number|null} ref @param {{limitS?:number|null, limitSource?:string, label?:string, short?:boolean}} [o]
 */
export function ageSpan(ref, o = {}) {
  const el = h("span", { class: "js-age", dataset: { ref: String(toMs(ref) ?? ""), limit: o.limitS ?? "", src: o.limitSource ?? "", label: o.label ?? "", short: o.short ? "1" : "" } });
  paintAge(el);
  return el;
}

function paintAge(el) {
  const ref = Number(el.dataset.ref);
  if (!el.dataset.ref || !Number.isFinite(ref)) { el.textContent = DASH + " (time unknown)"; return; }
  const age = (nowMs() - ref) / 1000;
  const limit = el.dataset.limit === "" ? null : Number(el.dataset.limit);
  const stale = limit !== null && age > limit;
  const lab = el.dataset.label ? el.dataset.label + " " : "";
  const srcNote = el.dataset.src === "ui" ? ", UI default limit" : el.dataset.src === "api" ? ", API limit" : "";
  el.textContent = stale ? (el.dataset.short ? `STALE ${fmtAge(age)}` : `${lab}STALE ${fmtAge(age)} (limit ${fmtDuration(limit)}${srcNote})`) : `${lab}${fmtAge(age)}`;
  if (stale) el.title = `Limit ${fmtDuration(limit)}${srcNote}`;
  el.classList.toggle("is-stale", stale);
}

let tickerStarted = false;
export function startTicker() {
  if (tickerStarted) return;
  tickerStarted = true;
  setInterval(() => document.querySelectorAll(".js-age").forEach(paintAge), 1000);
}

/* ---------------------------------------------------------------- values */

/**
 * Render one `Measured` (or a bare number) with unit, kind chip, stale flag and
 * provenance hover. Missing values render DASH + reason; they are never 0.
 *
 * @param {import("./api.js").Measured|undefined|null} m  the API value (undefined = variable absent)
 * @param {{variable?:string, digits?:number, chip?:boolean, big?:boolean, absentReason?:string, unitOverride?:string, compactStale?:boolean}} [o]
 */
export function valueCell(m, o = {}) {
  const variable = o.variable || m?.variable;
  const cls = ["val"];
  if (o.big) cls.push("big");
  if (!m) {
    cls.push("missing");
    return h("span", { class: cls.join(" ") }, h("span", { class: "n" }, DASH),
      h("span", { class: "why" }, o.absentReason || "not provided by this source"));
  }
  const q = m.quality;
  const hasNum = m.value !== null && m.value !== undefined && Number.isFinite(m.value);
  const title = provenanceTitle(m.provenance, { quality: q });
  if (!hasNum) {
    cls.push("missing");
    const why = q && q !== "ok" ? `no value (${q.replace(/_/g, " ")})` : "source returned no value";
    return h("span", { class: cls.join(" "), title }, h("span", { class: "n" }, DASH), h("span", { class: "why" }, why),
      o.chip ? [" ", kindChip(m.provenance?.kind, { short: true })] : null);
  }
  const f = freshnessOf(m.provenance, q);
  const { text, unit } = fmtVar(variable, m.value, o.digits);
  const stale = f.state === "stale";
  if (stale) cls.push("stale");
  const qcBad = q && !["ok", "interpolated", "unvalidated"].includes(q);
  return h("span", { class: cls.join(" "), title },
    h("span", { class: "n" }, text), unit ? h("span", { class: "u" }, o.unitOverride ?? unit) : null,
    stale ? h("span", { class: "flag chip stale", title: freshnessText(f) }, o.compactStale ? "STALE" : `STALE ${fmtDuration(f.ageS)}`) : null,
    qcBad ? h("span", { class: "flag chip warn", title: "Quality-control flag raised by the API" }, `QC ${q.replace(/_/g, " ")}`) : null,
    q === "interpolated" ? h("span", { class: "flag chip info" }, "INTERP") : null,
    o.chip ? [" ", kindChip(m.provenance?.kind, { short: true })] : null);
}

/**
 * A bare computed number (not a `Measured`) with explicit unit and provenance.
 * @param {number|null|undefined} value display-ready number
 * @param {string} unit
 * @param {{digits?:number, prov?:any, big?:boolean, reason?:string, quality?:string, extra?:string[]}} [o]
 */
export function numCell(value, unit, o = {}) {
  const cls = ["val"];
  if (o.big) cls.push("big");
  const title = o.prov ? provenanceTitle(o.prov, { quality: o.quality, extra: o.extra }) : o.extra ? o.extra.join("\n") : undefined;
  if (value === null || value === undefined || !Number.isFinite(value)) {
    cls.push("missing");
    return h("span", { class: cls.join(" "), title }, h("span", { class: "n" }, DASH), h("span", { class: "why" }, o.reason || "no value"));
  }
  let stale = null;
  if (o.prov) { const f = freshnessOf(o.prov, o.quality); if (f.state === "stale") { stale = f; cls.push("stale"); } }
  return h("span", { class: cls.join(" "), title },
    h("span", { class: "n" }, fmtNum(value, o.digits ?? 1)), unit ? h("span", { class: "u" }, unit) : null,
    stale ? h("span", { class: "flag chip stale", title: freshnessText(stale) }, `STALE ${fmtDuration(stale.ageS)}`) : null);
}

/** Text value with a dash+reason fallback (times, names). */
export function textCell(text, reason = "not available", title) {
  if (text === null || text === undefined || text === "" || text === DASH) {
    return h("span", { class: "val missing", title }, h("span", { class: "n" }, DASH), h("span", { class: "why" }, reason));
  }
  return h("span", { class: "has-prov", title }, text);
}

/** Visible compact source line for a panel footer, built from a Provenance. */
export function provFooter(prov, extra) {
  if (!prov) return h("footer", { class: "prov" }, "Source: not reported by the API.");
  const f = freshnessOf(prov);
  const bits = [kindChip(prov.kind, { short: true }), " ", provenanceSummary(prov)];
  if (prov.model_init_time_utc) bits.push(`  ·  run ${fmtUtc(prov.model_init_time_utc)}`);
  bits.push("  ·  retrieved ", ageSpan(prov.retrieved_at_utc, f.state !== "na" && prov.kind === "forecast" ? { limitS: f.limitS, limitSource: f.limitSource } : {}));
  if (extra) bits.push("  ·  ", extra);
  return h("footer", { class: "prov", title: provenanceTitle(prov) }, bits);
}

/* --------------------------------------------------------------- panels */

/** @param {string} title @param {any} body @param {{right?:any, cls?:string, footer?:any, id?:string}} [o] */
export function panel(title, body, o = {}) {
  return h("section", { class: `panel ${o.cls || ""}`, id: o.id },
    h("header", null, h("h2", null, title), o.right ? h("div", { class: "right" }, o.right) : null),
    h("div", { class: "body" }, body), o.footer || null);
}

/** Definition list. rows: [label, node|string] */
export function kv(rows, cls = "") {
  const dl = h("dl", { class: `kv ${cls}` });
  for (const [k, v] of rows) { dl.append(h("dt", null, k), h("dd", null, v)); }
  return dl;
}

export function stateBox(kind, title, text) {
  return h("div", { class: `state ${kind}` }, h("h3", null, title), text ? h("div", null, text) : null);
}

/**
 * "POSITION UNKNOWN": no coordinates, no marker, no default. If the cause is
 * that /api/state itself could not be loaded, say so instead of claiming the
 * API reported "no fix".
 */
export function positionUnknown(detail) {
  const st = storeRes("state");
  let why = detail || "The API reports no usable position fix. Nothing on this page is computed for a guessed location.";
  if (st.status === "error" && st.data === null) why = `Position could not be loaded: ${st.error?.endpoint || "GET /api/state"} failed (${st.error?.message}). Nothing is computed for a guessed location.`;
  else if (st.status !== "ok" && st.data === null) why = "Position has not been loaded yet. Nothing is computed for a guessed location.";
  return h("div", { class: "state unknown", role: "status" },
    h("h3", null, "POSITION UNKNOWN"),
    h("div", null, why),
    h("div", { class: "note" }, "Set a manual coordinate in Race Command Center, or connect a GPS source."));
}

/* ------------------------------------------------------ resource states */

/**
 * Error panel naming the endpoint and error, plus the age of the last good load.
 * @param {import("./store.js").Resource} r
 */
export function errorPanel(r) {
  const e = r.error;
  const lines = [];
  lines.push(h("div", null, "Endpoint: ", h("code", null, e?.endpoint || r.endpoint || "unknown"), e?.url ? [" ", h("span", { class: "faint" }, e.url)] : null));
  const kindText = { network: "API unreachable", timeout: "Request timed out", http: "HTTP error", parse: "Unreadable response", position: "Position unavailable (HTTP 409)", unsupported: "Not supported by any configured provider (HTTP 501)", provider: "Provider failure (HTTP 503)" }[e?.kind] || "Error";
  lines.push(h("div", null, "Error: ", h("b", null, kindText), e?.message ? " - " + e.message : "", e?.code ? ` [${e.code}]` : ""));
  if (e?.detail) lines.push(h("div", { class: "note" }, "Detail: " + (typeof e.detail === "string" ? e.detail : JSON.stringify(e.detail))));
  lines.push(h("div", null, r.loadedAt !== null
    ? ["Last successful load: ", fmtUtc(r.loadedAt, { seconds: true }), " (", ageSpan(r.loadedAt), ")"]
    : "Nothing has been loaded successfully from this endpoint."));
  const cls = e?.kind === "unsupported" || e?.kind === "position" ? "state unknown" : "state error";
  return h("div", { class: cls, role: "alert" }, h("h3", null, e?.kind === "unsupported" ? "NOT AVAILABLE" : e?.kind === "position" ? "POSITION UNAVAILABLE" : "DATA UNAVAILABLE"), lines);
}

/**
 * Render an API-backed region.
 *  - blocked  -> explanatory box (or position-unknown)
 *  - loading  -> loading box
 *  - error    -> error panel; if earlier data exists it is still rendered, under a
 *                "LAST LOADED n ago - NOT CURRENT" ribbon
 *  - ok       -> render(data)
 * @param {import("./store.js").Resource} r
 * @param {(data:any) => any} render
 * @param {{label?:string, blockedNode?:() => any}} [o]
 */
export function withResource(r, render, o = {}) {
  const wrap = h("div");
  if (r.status === "blocked") {
    wrap.append(o.blockedNode ? o.blockedNode() : stateBox("unknown", "NOT REQUESTED", r.blockedReason || "preconditions not met"));
    return wrap;
  }
  if (r.status === "idle" || (r.status === "loading" && r.data === null)) {
    wrap.append(stateBox("loading", "Loading" + (o.label ? " " + o.label : "") + "…", r.endpoint ? h("code", null, r.endpoint) : null));
    return wrap;
  }
  if (r.status === "error") {
    wrap.append(errorPanel(r));
    if (r.data !== null) {
      wrap.append(h("div", { class: "lastgood" }, h("b", null, "LAST LOADED "), ageSpan(r.loadedAt), " — the data below is NOT current."));
      append(wrap, [render(r.data)]);
    }
    return wrap;
  }
  append(wrap, [render(r.data)]);
  return wrap;
}

/** Small table helper. cols: [{h, cls, cell(row,i)->node|string}] */
export function table(cols, rows, o = {}) {
  const thead = h("thead", null, h("tr", null, cols.map((c) => h("th", { class: c.cls || "", title: c.title }, c.h))));
  const tbody = h("tbody");
  rows.forEach((row, i) => {
    const tr = h("tr", { class: o.rowClass ? o.rowClass(row, i) : "" }, cols.map((c) => h("td", { class: c.cls || "" }, c.cell(row, i))));
    tbody.append(tr);
    if (o.detail) { const d = o.detail(row, i); if (d) tbody.append(h("tr", { class: "detail" }, h("td", { colspan: cols.length }, d))); }
  });
  return h("div", { class: "tbl-wrap", style: o.maxHeight ? { maxHeight: o.maxHeight } : null }, h("table", { class: "t" }, thead, tbody));
}

/** Freshness summary node for a provenance: age + STALE flag. */
export function freshnessNode(prov, quality) {
  const f = freshnessOf(prov, quality);
  const ref = prov?.kind === "observation" ? (prov.observation_time_utc || prov.retrieved_at_utc) : prov?.retrieved_at_utc;
  return ageSpan(ref, f.state === "na" ? {} : { limitS: f.limitS, limitSource: f.limitSource });
}

export { ageSeconds };
