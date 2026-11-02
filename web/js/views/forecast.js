/**
 * View D - Forecast Center.
 * Time-series charts for temperature, wind, cloud and irradiance with a visible
 * observed/forecast boundary, a model-comparison area, and an honest
 * "no verification history yet" placeholder instead of invented accuracy numbers.
 * @module views/forecast
 */
import { h, replace } from "../dom.js";
import { DASH, fmtNum, fmtUtc, VARS, varLabel, varUnit, toMs, fmtDuration } from "../format.js";
import { panel, kindChip, chip, withResource, positionUnknown, stateBox, valueCell, textCell, ageSpan, table } from "../ui.js";
import { res, activePoint, vehicleTimezone, effectiveTimeMs, session, setSession, signature, notify } from "../store.js";
import { loadForecast, loadObservations, nearestStep, loadArchive, loadVerification, loadEnsemble, loadModels } from "../loaders.js";
import { qualityState, qualitySignature, verificationPanel, ensemblePanel, modelsPanel } from "./forecastquality.js";
import { chartHost, styleLegend } from "../charts.js";
import { alignMaps, pointsOf } from "../seriesutil.js";

const CHARTS = [
  { id: "temp", title: "Air temperature 2 m", ylabel: "°C", vars: [{ v: "temperature_2m", color: "--series-1" }] },
  { id: "wind", title: "Wind 10 m (speed and gust)", ylabel: "m/s", vars: [{ v: "wind_speed_10m", color: "--series-2" }, { v: "wind_gusts_10m", color: "--series-5", fcstOnly: true }] },
  { id: "cloud", title: "Cloud cover", ylabel: "%", vars: [{ v: "cloud_cover", color: "--series-3" }, { v: "cloud_cover_low", color: "--series-6", fcstOnly: true }, { v: "cloud_cover_mid", color: "--series-2", fcstOnly: true }, { v: "cloud_cover_high", color: "--series-4", fcstOnly: true }] },
  { id: "irr", title: "Irradiance (forecast / observed; see Solar for the clear-sky bound)", ylabel: "W/m²", vars: [{ v: "ghi", color: "--series-4" }, { v: "dni", color: "--series-5", fcstOnly: true }, { v: "dhi", color: "--series-6", fcstOnly: true }] },
];
const CMP_VARS = ["temperature_2m", "wind_speed_10m", "cloud_cover", "ghi"];
const cmpState = { variable: "temperature_2m", providers: [] };

export function create(root) {
  const hoursSel = h("select", { class: "ctl", onchange: (e) => setSession({ forecastHours: Number(e.target.value) }) },
    [24, 48, 72, 120].map((n) => h("option", { value: n, selected: n === session.forecastHours ? "" : null }, `${n} h`)));
  const provSel = h("select", { class: "ctl", onchange: (e) => setSession({ provider: e.target.value }) });
  const controls = h("div", { class: "row", style: { marginBottom: "10px" } },
    h("div", { class: "field" }, h("label", null, "Horizon"), hoursSel), h("div", { class: "field" }, h("label", null, "Provider"), provSel),
    h("button", { class: "btn", onclick: () => { loadForecast({ hours: session.forecastHours, provider: session.provider, force: true }); loadObservations({ hours: 24, force: true }); } }, "Refresh"));
  const body = h("div");
  root.append(h("div", { class: "view-head" }, h("h1", null, "Forecast Center"), h("span", { class: "sub" }, "Observed and predicted series are never merged: the dashed rule marks the boundary")), controls, body);
  const charts = chartHost();
  let lastSig = "", lastProv = "";

  function syncProviders() {
    const names = (res("health").data?.providers || []).map((p) => p.provider);
    const key = names.join(",") + "|" + session.provider;
    if (key === lastProv) return; lastProv = key;
    replace(provSel, h("option", { value: "" }, "Auto (API default / fallback)"), names.map((n) => h("option", { value: n, selected: n === session.provider ? "" : null }, n)));
  }

  return {
    update() {
      syncProviders();
      loadForecast({ hours: session.forecastHours, provider: session.provider }); loadObservations({ hours: 24 });
      loadArchive(); loadVerification({ days: qualityState.days }); loadEnsemble({ variable: qualityState.ensembleVariable, hours: session.forecastHours }); loadModels({ variable: qualityState.modelVariable, hours: session.forecastHours });
      for (const p of cmpState.providers) loadForecast({ hours: session.forecastHours, provider: p, key: "cmp:" + p });
      const sig = signature(["state", "forecast", "observations", "observable", "health", "archive", "verification", "ensemble", "models", ...cmpState.providers.map((p) => "cmp:" + p)], cmpState.variable + cmpState.providers.join() + qualitySignature());
      if (sig === lastSig) return;
      lastSig = sig; charts.destroyAll();
      replace(body, layout(charts, () => { lastSig = ""; }));
    },
    destroy() { charts.destroyAll(); },
  };
}

function layout(charts, invalidate) {
  if (!activePoint()) return positionUnknown("Forecasts are requested only for a known position.");
  const fc = res("forecast"), ob = res("observations");
  const wrap = h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } });
  const status = [];
  if (fc.status !== "ok") status.push(withResource(fc, () => h("span"), { label: "forecast" }));
  else if (fc.error) status.push(withResource(fc, () => h("span")));
  if (ob.status === "blocked") status.push(h("div", { class: "state unknown" }, h("h3", null, "NO OBSERVATIONS REQUESTED"), `${ob.blockedReason}. The charts below show forecast only; nothing here is an observation.`));
  if (ob.status === "error") status.push(h("div", { class: ob.error?.kind === "unsupported" ? "state unknown" : "state error" },
    h("h3", null, ob.error?.kind === "unsupported" ? "NO OBSERVATIONS AVAILABLE" : "OBSERVATIONS UNAVAILABLE"),
    ob.error?.kind === "unsupported" ? `Observations are not available (HTTP 501): ${ob.error.message}. The charts below show forecast only; nothing here is an observation.` : `${ob.error.endpoint}: ${ob.error.message}`));
  wrap.append(...status);

  const grid = h("div", { class: "grid g-2" });
  const nowS = effectiveTimeMs() / 1000;
  const { tz } = vehicleTimezone(); const utc = session.timeBasis === "utc";
  for (const c of CHARTS) grid.append(chartPanel(c, fc.data, ob.data, nowS, tz, utc, charts));
  wrap.append(grid, ensemblePanel(charts), modelsPanel(charts), verificationPanel(), comparePanel(charts, invalidate));
  return wrap;
}

function chartPanel(c, fcData, obData, nowS, tz, utc, charts) {
  const maps = [], meta = [];
  let missing = 0;
  for (const s of c.vars) {
    const f = pointsOf(fcData, s.v); missing += f.missing;
    maps.push(f.map); meta.push({ ...s, kind: "fcst" });
    if (!s.fcstOnly && obData) { const o = pointsOf(obData, s.v); missing += o.missing; maps.push(o.map); meta.push({ ...s, kind: "obs" }); }
  }
  // Drop variables the source did not supply at all (they would be empty legend entries).
  const keep = maps.map((m) => m.size > 0);
  const { x, values: allValues } = alignMaps(maps);
  const values = allValues.filter((_, i) => keep[i]);
  const unit = varUnit(c.vars[0].v);
  const series = meta.filter((_, i) => keep[i]).map((m, i) => ({ label: `${VARS[m.v]?.short || m.v} ${m.kind === "obs" ? "observed" : "forecast"}`, unit, color: m.color, kind: m.kind, values: values[i], digits: VARS[m.v]?.digits ?? 1 }));
  const div = h("div", { class: "chart" });
  charts.add(div, { x, series, tz, utc, boundarySec: x.length && nowS >= x[0] && nowS <= x[x.length - 1] ? nowS : null, boundaryLabels: obData ? ["OBSERVED", "FORECAST"] : ["PAST", "FUTURE"], yLabel: `${c.title.split(" (")[0]} (${unit})`, height: 190, emptyText: fcData ? "No points for this variable in the forecast." : res("forecast").status === "error" ? "NO DATA: the forecast provider is unavailable (see the error above). An empty chart here is an outage, not calm weather." : "No forecast loaded." });
  const legendEntries = [{ color: c.vars[0].color, kind: "fcst", text: "Forecast (dashed)" }, ...(obData ? [{ color: c.vars[0].color, kind: "obs", text: "Observed (solid, markers)" }] : [])];
  return panel(c.title, [div, styleLegend(legendEntries), missing ? h("div", { class: "note warn" }, `${missing} missing value(s) in this chart's source data; the line bridges across them, the markers/legend show real samples only.`) : null],
    { footer: h("footer", { class: "prov" }, fcData ? ["Forecast ", chip("FORECAST", "forecast"), ` ${fcData.provider} / ${fcData.dataset}${fcData.model_name ? " / " + fcData.model_name : ""}`, fcData.model_init_time_utc ? `, run ${fmtUtc(fcData.model_init_time_utc)}` : "", ", retrieved ", ageSpan(fcData.retrieved_at_utc, {})] : "No forecast loaded", obData ? ["   Observed ", chip("OBSERVED", "observed"), ` ${obData.provider} / ${obData.dataset}, retrieved `, ageSpan(obData.retrieved_at_utc)] : "") });
}

function comparePanel(charts, invalidate) {
  const providers = (res("health").data?.providers || []).map((p) => p.provider);
  const box = h("div");
  if (providers.length < 2) {
    box.append(stateBox("", "Model comparison needs two or more providers", `GET /api/health lists ${providers.length} provider(s). A comparison against a single source would not be a comparison. Model-level comparison (e.g. ECMWF vs GFS inside one provider) is not exposed by the current API.`));
    return panel("Provider comparison", box);
  }
  const chosen = cmpState.providers;
  const chips = h("div", { class: "row", style: { marginBottom: "8px" } }, providers.map((p) => {
    const cb = h("input", { type: "checkbox", onchange: (e) => { cmpState.providers = e.target.checked ? [...new Set([...cmpState.providers, p])] : cmpState.providers.filter((x) => x !== p); invalidate(); notify(); } });
    cb.checked = chosen.includes(p);
    return h("label", { style: { display: "flex", gap: "4px", marginRight: "12px" } }, cb, p);
  }), h("div", { class: "field" }, h("label", null, "Variable"), h("select", { class: "ctl", onchange: (e) => { cmpState.variable = e.target.value; invalidate(); notify(); } }, CMP_VARS.map((v) => h("option", { value: v, selected: v === cmpState.variable ? "" : null }, varLabel(v))))));
  box.append(chips);
  if (!chosen.length) { box.append(h("div", { class: "note" }, "Tick two or more providers to request their forecasts side by side.")); return panel("Provider comparison", box); }

  const rows = chosen.map((p) => ({ p, r: res("cmp:" + p) }));
  const ok = rows.filter((x) => x.r.data);
  const tms = effectiveTimeMs();
  box.append(table([
    { h: "Provider", cell: (x) => x.p }, { h: "Dataset / model", cell: (x) => (x.r.data ? `${x.r.data.dataset}${x.r.data.model_name ? " / " + x.r.data.model_name : ""}` : textCell(null, x.r.status === "error" ? x.r.error?.message : "loading")) },
    { h: "Model run (UTC)", cell: (x) => (x.r.data?.model_init_time_utc ? fmtUtc(x.r.data.model_init_time_utc) : textCell(null, "not reported")) },
    { h: "Retrieved", cell: (x) => (x.r.data ? ageSpan(x.r.data.retrieved_at_utc) : DASH) },
    { h: "Resolution", cell: (x) => (x.r.data?.spatial_resolution_km ? `${fmtNum(x.r.data.spatial_resolution_km, 1)} km / ${x.r.data.temporal_resolution_s ? fmtDuration(x.r.data.temporal_resolution_s) : DASH}` : textCell(null, "not reported")) },
    ...CMP_VARS.map((v) => ({ h: `${VARS[v].short} now (${VARS[v].unit})`, cls: "r", cell: (x) => { const st = x.r.data ? nearestStep(x.r.data, tms) : null; return st ? valueCell(st.values[v], { variable: v }) : DASH; } })),
  ], rows));
  const maps = ok.map((x) => pointsOf(x.r.data, cmpState.variable).map);
  if (maps.length) {
    const { x, values } = alignMaps(maps);
    const div = h("div", { class: "chart", style: { marginTop: "8px" } });
    const palette = ["--series-1", "--series-2", "--series-5", "--series-6"];
    const nowS = effectiveTimeMs() / 1000;
    charts.add(div, { x, tz: vehicleTimezone().tz, utc: session.timeBasis === "utc", height: 200, boundarySec: x.length && nowS >= x[0] && nowS <= x[x.length - 1] ? nowS : null, boundaryLabels: ["PAST", "FUTURE"],
      yLabel: `${varLabel(cmpState.variable)} (${varUnit(cmpState.variable)})`,
      series: ok.map((m, i) => ({ label: m.p, unit: varUnit(cmpState.variable), color: palette[i % 4], kind: "fcst", values: values[i], digits: VARS[cmpState.variable].digits })) });
    box.append(div, h("div", { class: "note" }, "All lines are forecasts from different sources; none is a reference truth. Differences show spread, not error."));
  }
  return panel("Provider comparison", box);
}

void toMs;
