/**
 * Forecast-quality panels used by the Forecast Center: measured verification
 * skill, ensemble uncertainty and numerical-model comparison.
 *
 * Honesty rules owned here:
 *  - isProvisional() / scoreCell(): a score with fewer than MIN_SAMPLES pairs is
 *    marked PROVISIONAL and is never shown without its sample count.
 *  - skillNode(): skill versus persistence is shown with its sign; negative skill
 *    is rendered as WORSE THAN PERSISTENCE, never as a rounding detail.
 *  - verificationPanel(): notes are always rendered; zero scores is a stated
 *    "no evidence" state, not an empty table.
 *  - ensemblePanel(): the backend caveat is always visible beside the chart; wind
 *    direction (empty percentiles) gets a circular-SD line, never a band.
 *  - modelsPanel(): preferred_model null is shown as "none" with the backend's
 *    reason; the first model is never promoted.
 *  - mlBandsPanel(): EXPERIMENTAL. status no_model / not_applicable is a stated
 *    state with the backend's reasons, never an empty band; an uncalibrated model
 *    is marked UNVALIDATED; the band is labelled a forecast, never an observation.
 *  - outageNote(): a provider outage is an error panel plus the age of the last
 *    good data, never an empty chart.
 * @module views/forecastquality
 */
import { h } from "../dom.js";
import { DASH, fmtNum, fmtUtc, fmtLocal, fmtDuration, compass, toDisplay, varUnit, varLabel, VARS, toMs } from "../format.js";
import { panel, table, stateBox, chip, numCell, textCell, kindChip, ageSpan, provFooter, withResource, positionUnknown } from "../ui.js";
import { res, vehicleTimezone, session, effectiveTimeMs, notify } from "../store.js";
import { alignMaps } from "../seriesutil.js";
import { styleLegend } from "../charts.js";

/** Pairs needed before a score means anything (the backend's own is_meaningful threshold). */
export const MIN_SAMPLES = 30;

/** UI state for the three panels; included in the Forecast view's render signature. */
export const qualityState = { days: 30, ensembleVariable: "ghi", modelVariable: "cloud_cover", mlVariable: "ghi" };
export function qualitySignature() { return JSON.stringify(qualityState); }

const ENSEMBLE_VARS = ["ghi", "temperature_2m", "wind_speed_10m", "wind_direction_10m", "cloud_cover"];
/** rwi.ml.constraints.ML_VARIABLES */
const ML_VARS = ["ghi", "dni", "cloud_cover", "wind_speed_10m"];
const MODEL_VARS = ["cloud_cover", "temperature_2m", "wind_speed_10m", "ghi"];

/* ----------------------------------------------------------------- helpers */

const select = (options, value, onchange) => h("select", { class: "ctl", onchange },
  options.map(([v, t]) => h("option", { value: v, selected: String(v) === String(value) ? "" : null }, t)));

/** @returns {boolean} true when the score has too few pairs to be meaningful (or the count is missing) */
export function isProvisional(score) { return !(Number.isFinite(score.sample_count) && score.sample_count >= MIN_SAMPLES); }

function bucketLabel([lo, hi]) { return `${fmtNum(lo / 3600, 0)}–${fmtNum(hi / 3600, 0)} h`; }

/** Outage companion note: says what the archive holds, without calling it "the last observation". */
function outageNote() {
  const a = res("archive").data;
  const last = a?.available ? a.coverage?.latest_valid_time_utc : null;
  return last
    ? h("div", { class: "note warn" }, `The live provider did not answer. The archive holds data up to valid time ${fmtUtc(last)} (this can include forecasts for future times; it is not the time of the last observation). The panel above shows the last good response from this session, if any, with its age.`)
    : h("div", { class: "note warn" }, "The live provider did not answer, and archive coverage is not loaded.");
}

/** Skill versus persistence, signed. Negative = the model is worse than assuming nothing changes. */
export function skillNode(skill) {
  if (skill === null || skill === undefined || !Number.isFinite(skill)) return textCell(null, "no persistence baseline");
  const txt = (skill > 0 ? "+" : skill < 0 ? "−" : "") + fmtNum(Math.abs(skill), 2);
  if (skill < 0) return h("span", { title: "skill = 1 - MAE / MAE(persistence). Negative: the forecast has larger errors than assuming nothing changes." }, chip("WORSE THAN PERSISTENCE", "crit"), " ", h("b", { class: "num", style: { color: "var(--crit)" } }, txt));
  if (skill > 0) return h("span", { title: "skill = 1 - MAE / MAE(persistence). Positive: better than assuming nothing changes." }, chip("BEATS PERSISTENCE", "ok"), " ", h("b", { class: "num" }, txt));
  return h("span", null, chip("EQUALS PERSISTENCE", "mute"), " ", h("b", { class: "num" }, txt));
}

/**
 * One verification cell. The MAE is never shown bare: it carries its unit, the
 * sample count, a PROVISIONAL chip below MIN_SAMPLES and the signed skill.
 * @param {import("../api.js").VerificationScore|undefined} sc
 */
export function scoreCell(sc) {
  if (!sc) return textCell(null, "no pairs");
  const prov = isProvisional(sc);
  const dec = Math.abs(sc.mean_absolute_error ?? 0) < 10 ? 2 : 1;
  const u = sc.unit === "W/m2" ? "W/m²" : sc.unit === "degC" ? "°C" : sc.unit === "percent" ? "%" : sc.unit === "deg" ? "°" : sc.unit;
  const detail = [`bias ${sc.bias == null ? DASH : fmtNum(sc.bias, dec)} ${u}`, `RMSE ${sc.root_mean_square_error == null ? DASH : fmtNum(sc.root_mean_square_error, dec)} ${u}`, `persistence MAE ${sc.persistence_mae == null ? DASH : fmtNum(sc.persistence_mae, dec)} ${u}`].join(" · ");
  return h("div", { class: prov ? "provisional" : "", title: `${detail}\nprovider ${sc.provider}${sc.model_name ? " / " + sc.model_name : ""}, lead ${bucketLabel(sc.lead_time_bucket_s)}, ${sc.sample_count} pairs` },
    h("div", null, h("span", { class: "dim" }, "MAE "), numCell(sc.mean_absolute_error, u, { digits: dec, reason: "no MAE" })),
    h("div", null, h("span", { class: "dim" }, `n = ${sc.sample_count} `), prov ? chip(`PROVISIONAL (<${MIN_SAMPLES})`, "warn", `Fewer than ${MIN_SAMPLES} forecast/observation pairs: not evidence of skill`) : chip("n ≥ " + MIN_SAMPLES, "mute")),
    h("div", null, skillNode(sc.skill_vs_persistence)),
    h("div", { class: "note" }, detail));
}

/* ------------------------------------------------------------ verification */

const n0 = (v) => (Number.isFinite(v) ? v.toLocaleString("en-US") : DASH);

/**
 * Plain-words account of why archived forecasts are or are not scoreable, from
 * archive coverage.pairs. Each reason is different in kind, so each gets its own
 * line: waiting (will resolve), structurally unscoreable (no model run time),
 * and excluded by the leakage guard.
 * @param {any} coverage GET /api/forecast/archive -> coverage
 */
export function pairsExplanation(coverage) {
  const p = coverage?.pairs;
  if (!p) return null;
  const lines = [];
  if (p.unmatched_forecasts > 0) lines.push(h("li", null, h("b", null, `${n0(p.unmatched_forecasts)} archived forecasts are waiting`), " for an observation to match them (for example the time they predict has not arrived yet). These become scoreable simply by waiting."));
  if (p.dropped_no_init_time > 0) lines.push(h("li", null, h("b", null, `${n0(p.dropped_no_init_time)} can never be scored`), ": their model does not publish a run time, so the lead time is unknown. This is a documented limitation (docs/science/capture.md), not a fault."));
  if (p.dropped_recorded_after_valid > 0) lines.push(h("li", null, h("b", null, `${n0(p.dropped_recorded_after_valid)} were excluded by the leakage guard`), ": they were recorded after the time they predict, and a forecast made after the fact is not a forecast."));
  if (p.dropped_negative_lead > 0) lines.push(h("li", null, h("b", null, `${n0(p.dropped_negative_lead)} had a negative lead time`), " and were excluded."));
  const head = p.pair_count > 0 ? `${n0(p.pair_count)} forecast/observation pair(s) are scoreable.` : "No forecast has been scored yet.";
  return h("div", { class: "state", style: { marginTop: "8px", borderColor: "var(--k-forecast)" } }, h("h3", null, head),
    h("div", { class: "note", style: { marginBottom: "4px" } }, `Of ${n0(p.forecast_values_considered)} archived forecast values considered (pairing within ${fmtNum((p.max_separation_m ?? 0) / 1000, 0)} km and ${fmtDuration(p.max_time_offset_s)} of an observation):`),
    lines.length ? h("ul", { class: "plain" }, lines) : textCell(null, "no exclusion reasons were reported"));
}

/** Archive asymmetry sentence (observations without forecasts cannot be scored). */
export function archiveSummaryLine() {
  const a = res("archive");
  if (a.status === "error") return h("div", { class: "note warn" }, `Archive status unavailable (${a.error?.endpoint}: ${a.error?.message}).`);
  if (!a.data) return null;
  if (!a.data.available) return h("div", { class: "note warn" }, `Archive NOT available: ${a.data.message || "no reason given"}.`);
  const c = a.data.coverage || {};
  return h("div", { class: "note" }, `Archive: ${c.observation_value_count ?? 0} observation rows and ${c.forecast_value_count ?? 0} forecast rows (${c.pairs?.pair_count ?? 0} matched pairs). `,
    (c.forecast_value_count ?? 0) === 0 ? "With no archived forecasts nothing can be scored. " : "", h("a", { href: "#/history" }, "Archive status"));
}

export function verificationPanel() {
  const r = res("verification");
  const days = h("div", { class: "field" }, h("label", null, "Period"), select([[7, "7 days"], [30, "30 days"], [90, "90 days"], [365, "365 days"]], qualityState.days, (e) => { qualityState.days = Number(e.target.value); notify(); }));
  const body = withResource(r, (rep) => {
    const out = [];
    const period = rep.period ? `${fmtUtc(rep.period.start_utc, { dateOnly: true })} to ${fmtUtc(rep.period.end_utc, { dateOnly: true })}` : "period not stated";
    out.push(h("div", { class: "row", style: { marginBottom: "6px", gap: "16px" } },
      h("span", null, h("b", null, rep.scores.length), " score(s)"), h("span", null, h("b", null, rep.pair_count), " forecast/observation pair(s)"),
      h("span", { class: "dim" }, period), h("span", { class: "dim" }, "computed ", ageSpan(rep.computed_at_utc))));
    if (!rep.scores.length) {
      out.push(stateBox("unknown", "NO VERIFICATION EVIDENCE YET",
        `0 scores from ${rep.pair_count} forecast/observation pairs. No accuracy figure is shown or implied. Scoring needs forecasts that were archived BEFORE the observation they predict; see the notes below and the archive status.`));
    } else {
      const buckets = [...new Map(rep.scores.map((s) => [s.lead_time_bucket_s[0], s.lead_time_bucket_s])).values()].sort((a, b) => a[0] - b[0]);
      const rowsMap = new Map();
      for (const s of rep.scores) { const k = `${s.variable}|${s.provider}|${s.model_name || ""}`; if (!rowsMap.has(k)) rowsMap.set(k, { variable: s.variable, provider: s.provider, model: s.model_name, cells: new Map() }); rowsMap.get(k).cells.set(s.lead_time_bucket_s[0], s); }
      const rows = [...rowsMap.values()];
      out.push(table([
        { h: "Variable / model", cell: (x) => h("div", null, h("b", null, varLabel(x.variable)), h("div", { class: "note" }, `${x.provider}${x.model ? " / " + x.model : ""}`)) },
        ...buckets.map((b) => ({ h: `Lead ${bucketLabel(b)}`, cell: (x) => scoreCell(x.cells.get(b[0])) })),
      ], rows));
      const prov = rep.scores.filter(isProvisional).length;
      out.push(h("div", { class: "note", style: { marginTop: "6px" } }, `${prov} of ${rep.scores.length} score(s) are PROVISIONAL (fewer than ${MIN_SAMPLES} pairs). Skill = 1 − MAE / MAE of persistence ("nothing changes"). Positive beats persistence; negative means the forecast is worse than assuming no change.`));
    }
    out.push(h("h3", { style: { margin: "10px 0 4px" } }, `Notes from the verification (${rep.notes.length})`));
    out.push(rep.notes.length ? h("ul", { class: "plain" }, rep.notes.map((n) => h("li", null, n))) : textCell(null, "the report carried no notes"));
    const why = pairsExplanation(res("archive").data?.coverage); if (why) out.push(why);
    const line = archiveSummaryLine(); if (line) out.push(line);
    return out;
  }, { label: "verification" });
  return panel("Forecast verification (measured skill)", body, { right: days });
}

/* ---------------------------------------------------------------- ensemble */

export function ensemblePanel(charts) {
  const r = res("ensemble");
  const sel = h("div", { class: "field" }, h("label", null, "Variable"), select(ENSEMBLE_VARS.map((v) => [v, varLabel(v)]), qualityState.ensembleVariable, (e) => { qualityState.ensembleVariable = e.target.value; notify(); }));
  const body = withResource(r, (d) => {
    const out = [];
    const sums = d.summaries;
    out.push(h("div", { class: "state", style: { borderColor: "var(--k-forecast)", marginBottom: "8px" } }, h("h3", null, "What this spread means (from the API)"), d.caveat || textCell(null, "no caveat supplied by the API")));
    if (!sums.length) { out.push(stateBox("unknown", "No ensemble members covered this period", d.caveat)); return out; }
    const v = d.variable, unit = varUnit(v);
    const x = sums.map((s) => Math.round(toMs(s.valid_time_utc) / 1000));
    const tz = vehicleTimezone().tz, utc = session.timeBasis === "utc";
    const dsp = (val) => (val === null || val === undefined ? null : toDisplay(v, val));
    const noBand = sums.every((s) => !s.percentiles || !Object.keys(s.percentiles).length);
    const div = h("div", { class: "chart" });
    const nowS = effectiveTimeMs() / 1000;
    const common = { x, tz, utc, height: 230, boundarySec: nowS >= x[0] && nowS <= x[x.length - 1] ? nowS : null, boundaryLabels: ["PAST", "FUTURE"] };
    if (noBand) {
      // Circular quantity (wind direction): percentiles are deliberately empty. Show the circular SD, not a band.
      out.push(h("div", { class: "note warn" }, `No percentile band for ${varLabel(v)}: the API leaves percentiles empty on purpose (an arc from 350° to 10° has no meaningful tenth percentile). The line is the circular standard deviation of the ${d.member_count} members.`));
      charts.add(div, { ...common, yLabel: "Circular standard deviation (°)", series: [{ label: "Spread: circular SD", unit: "°", color: "--series-5", kind: "fcst", values: sums.map((s) => s.standard_deviation), digits: 0 }] });
      out.push(div);
      out.push(h("div", { class: "tbl-wrap", style: { marginTop: "6px", maxHeight: "170px" } }, table([
        { h: "Valid (local | UTC)", cell: (s) => `${tz ? fmtLocal(s.valid_time_utc, tz) : DASH} | ${fmtUtc(s.valid_time_utc)}` },
        { h: "Median direction (from)", cls: "r", cell: (s) => h("span", null, numCell(s.median, "°", { digits: 0, prov: s.provenance }), " ", compass(s.median)) },
        { h: "Circular SD", cls: "r", cell: (s) => numCell(s.standard_deviation, "°", { digits: 0, prov: s.provenance }) },
      ], sums.slice(0, 24))));
    } else {
      const pc = (k) => sums.map((s) => dsp(s.percentiles?.[k] ?? null));
      charts.add(div, { ...common, yLabel: `${varLabel(v)} (${unit})`, fans: [{ upper: 1, lower: 0 }], series: [
        { label: "P10", unit, color: "--series-2", kind: "edge", values: pc("10"), digits: VARS[v]?.digits ?? 1 },
        { label: "P90", unit, color: "--series-2", kind: "edge", values: pc("90"), digits: VARS[v]?.digits ?? 1 },
        { label: "Median", unit, color: "--series-2", kind: "fcst", values: sums.map((s) => dsp(s.median)), digits: VARS[v]?.digits ?? 1 },
        { label: "Minimum member", unit, color: "--series-3", kind: "edge", values: sums.map((s) => dsp(s.minimum)), digits: VARS[v]?.digits ?? 1 },
        { label: "Maximum member", unit, color: "--series-3", kind: "edge", values: sums.map((s) => dsp(s.maximum)), digits: VARS[v]?.digits ?? 1 },
      ] });
      out.push(div);
      out.push(styleLegend([{ color: "--series-2", kind: "fcst", text: `Median; shaded band = P10 to P90 of ${d.member_count} members` }, { color: "--series-3", kind: "fcst", text: "thin lines: member minimum / maximum" }]));
    }
    return out;
  }, { label: "ensemble", blockedNode: () => positionUnknown() });
  const d = r.data;
  const foot = d?.summaries?.[0] ? provFooter(d.summaries[0].provenance, `${d.member_count} members, ${d.model_name || "model not named"}`) : null;
  const wrap = h("div", null, body, r.status === "error" ? outageNote() : null);
  return panel("Ensemble uncertainty (members that actually ran)", wrap, { right: sel, footer: foot });
}

/* -------------------------------------------------------------- ML bands */

/** The backend's per-variable "missing" reasons, always listed when present. */
function missingList(missing) {
  const e = Object.entries(missing || {});
  return e.length ? h("ul", { class: "plain" }, e.map(([v, why]) => h("li", null, h("b", null, varLabel(v)), `: ${why}`))) : null;
}

export function mlBandsPanel(charts) {
  const r = res("mlbands");
  const sel = h("div", { class: "field" }, h("label", null, "Variable"), select(ML_VARS.map((v) => [v, varLabel(v)]), qualityState.mlVariable, (e) => { qualityState.mlVariable = e.target.value; notify(); }));
  const body = withResource(r, (d) => {
    const out = [];
    if (d.status === "no_model") {
      out.push(stateBox("unknown", "NO ML MODEL", `${d.reason || "the API gave no reason"}. No ML band is shown or implied; use the ensemble panel above.`));
      const m = missingList(d.missing); if (m) out.push(m);
      return out;
    }
    const models = d.loaded.map((l) => h("span", { style: { marginRight: "12px" } }, h("b", null, varLabel(l.variable)), ` ${l.model_id} ${l.model_version} `, l.calibrated ? chip("CALIBRATED", "ok") : chip("UNVALIDATED", "warn", "The model's 80 % interval has not passed the calibration check on its holdout")));
    out.push(h("div", { class: "note" }, "Loaded: ", models.length ? models : DASH));
    if (d.status === "not_applicable") {
      out.push(stateBox("unknown", "ML MODEL NOT APPLICABLE HERE", d.reason || "the API gave no reason"));
      const m = missingList(d.missing); if (m) out.push(m);
      return out;
    }
    const v = qualityState.mlVariable, unit = varUnit(v), digits = VARS[v]?.digits ?? 1;
    const steps = (d.series?.steps || []).filter((s) => s.values?.[v]);
    if (!steps.length) {
      out.push(stateBox("unknown", "NO ML BAND FOR THIS VARIABLE", d.missing?.[v] || "the model returned no steps for this variable"));
      return out;
    }
    const dsp = (val) => (val === null || val === undefined ? null : toDisplay(v, val));
    const x = steps.map((s) => Math.round(toMs(s.valid_time_utc) / 1000));
    const tz = vehicleTimezone().tz, utc = session.timeBasis === "utc";
    const nowS = effectiveTimeMs() / 1000;
    const div = h("div", { class: "chart" });
    charts.add(div, { x, tz, utc, height: 220, yLabel: `${varLabel(v)} (${unit})`, boundarySec: nowS >= x[0] && nowS <= x[x.length - 1] ? nowS : null, boundaryLabels: ["PAST", "FUTURE"], fans: [{ upper: 1, lower: 0 }], series: [
      { label: "P10", unit, color: "--series-4", kind: "edge", values: steps.map((s) => dsp(s.values[v].uncertainty?.lower)), digits },
      { label: "P90", unit, color: "--series-4", kind: "edge", values: steps.map((s) => dsp(s.values[v].uncertainty?.upper)), digits },
      { label: "P50", unit, color: "--series-4", kind: "fcst", values: steps.map((s) => dsp(s.values[v].value)), digits },
    ] });
    out.push(div);
    out.push(styleLegend([{ color: "--series-4", kind: "fcst", text: "ML P50; shaded band = P10 to P90 (80 % interval)" }]));
    const unval = steps.some((s) => s.values[v].quality === "unvalidated");
    out.push(h("div", { class: "note" }, kindChip("forecast"), " Statistical post-processing of an upstream forecast, not an observation.", unval ? h("b", { style: { color: "var(--warn)" } }, " UNVALIDATED: the interval is not calibrated; do not read it as an 80 % probability.") : null));
    const m = missingList(d.missing); if (m) out.push(m);
    return out;
  }, { label: "ML bands", blockedNode: () => positionUnknown() });
  const d = r.data, v = qualityState.mlVariable;
  const first = d?.series?.steps?.find((s) => s.values?.[v]);
  const foot = first ? provFooter(first.values[v].provenance, first.values[v].provenance?.processing || "") : null;
  return panel("ML quantile bands (EXPERIMENTAL, ADR 0008)", body, { right: sel, footer: foot });
}

/* ------------------------------------------------------------------ models */

export function modelsPanel(charts) {
  const r = res("models");
  const sel = h("div", { class: "field" }, h("label", null, "Variable"), select(MODEL_VARS.map((v) => [v, varLabel(v)]), qualityState.modelVariable, (e) => { qualityState.modelVariable = e.target.value; notify(); }));
  const body = withResource(r, (d) => {
    const out = [];
    const v = d.variable, unit = varUnit(v);
    const names = Object.keys(d.series_by_model);
    const maps = names.map((n) => new Map(d.series_by_model[n].map(([t, val]) => [Math.round(toMs(t) / 1000), toDisplay(v, val)])));
    const { x, values } = alignMaps(maps);
    const sig = d.disagreements.filter((g) => g.is_significant);
    const bands = sig.map((g) => { const t = toMs(g.valid_time_utc) / 1000; return [t - 1800, t + 1800, "rgba(255,93,93,0.20)"]; });
    const tz = vehicleTimezone().tz, utc = session.timeBasis === "utc";
    const palette = ["--series-1", "--series-2", "--series-5", "--series-6", "--series-4"];
    // preferred model: stated reason, never a silent default
    out.push(h("div", { class: "state", style: { marginBottom: "8px" } }, h("h3", null, "Preferred model"),
      d.preferred_model ? h("span", null, h("b", null, d.preferred_model), ` — ${d.preference_basis || "basis not stated"}`)
        : h("span", null, chip("NONE", "mute"), ` No model has earned preference yet. ${d.preference_basis || "(the API gave no reason)"}`)));
    if (!names.length) out.push(stateBox("unknown", "No model series returned", "Every requested model failed; see the notes."));
    else {
      const div = h("div", { class: "chart" });
      const nowS = effectiveTimeMs() / 1000;
      charts.add(div, { x, tz, utc, height: 220, bands, yLabel: `${varLabel(v)} (${unit})`, boundarySec: nowS >= x[0] && nowS <= x[x.length - 1] ? nowS : null, boundaryLabels: ["PAST", "FUTURE"],
        series: names.map((n, i) => ({ label: n, unit, color: palette[i % palette.length], kind: "fcst", values: values[i], digits: VARS[v]?.digits ?? 1 })) });
      out.push(div);
      out.push(h("div", { class: "note" }, kindChip("forecast"), " All lines are forecasts from different models. ", sig.length ? h("b", { style: { color: "var(--crit)" } }, `Red bands: ${sig.length} of ${d.disagreements.length} hour(s) with SIGNIFICANT model disagreement (spread above the threshold).`) : `No significant disagreement in ${d.disagreements.length} compared hour(s).`));
      if (sig.length) {
        const thr = sig[0].threshold;
        out.push(h("div", { style: { marginTop: "6px" } }, table([
          { h: "Valid (local | UTC)", cell: (g) => `${tz ? fmtLocal(g.valid_time_utc, tz) : DASH} | ${fmtUtc(g.valid_time_utc)}` },
          { h: "Spread", cls: "r", cell: (g) => numCell(toDisplay(v, g.spread), unit, { digits: VARS[v]?.digits ?? 1, prov: g.provenance }) },
          { h: "Threshold", cls: "r", cell: (g) => numCell(toDisplay(v, g.threshold), unit, { digits: VARS[v]?.digits ?? 1 }) },
          { h: "By model", cell: (g) => Object.entries(g.values_by_model).map(([m, val]) => `${m}: ${fmtNum(toDisplay(v, val), VARS[v]?.digits ?? 1)} ${unit}`).join("  |  ") },
        ], sig.slice(0, 12), { maxHeight: "200px" })));
        void thr;
      }
    }
    out.push(h("h3", { style: { margin: "10px 0 4px" } }, `Notes (${d.notes.length})`));
    out.push(d.notes.length ? h("ul", { class: "plain" }, d.notes.map((n) => h("li", null, n))) : textCell(null, "no notes"));
    return out;
  }, { label: "model comparison", blockedNode: () => positionUnknown() });
  const d = r.data;
  const wrap = h("div", null, body, r.status === "error" ? outageNote() : null);
  return panel("Numerical model comparison", wrap, { right: sel, footer: d?.disagreements?.[0] ? provFooter(d.disagreements[0].provenance) : null });
}

