/**
 * View C - Solar Intelligence.
 * Sun position, sunrise / solar noon / sunset, clear-sky MODEL BOUND vs
 * forecast (and observed, when available) GHI/DNI/DHI for the day, and the
 * modelled array power.
 * @module views/solar
 */
import { h, replace, append } from "../dom.js";
import { DASH, fmtNum, fmtUtc, fmtLocal, compass, toMs, fmtEnergyWh } from "../format.js";
import { panel, kv, numCell, textCell, kindChip, chip, withResource, positionUnknown, stateBox, provFooter, ageSpan } from "../ui.js";
import { res, activePoint, vehicleTimezone, effectiveTimeMs, session, signature, setSession } from "../store.js";
import { loadSolar, loadForecast, loadObservations, loadEnergy, loadSatcorps, loadGhiBias, solarDate } from "../loaders.js";
import { chartHost, styleLegend } from "../charts.js";
import { alignMaps, pointsOf } from "../seriesutil.js";

const KEYS = ["state", "observable", "solarPos", "solarDay", "clearskyDay", "forecastSolar", "observationsSolar", "energy", "satcorps", "ghiBias"];

export function create(root) {
  const body = h("div");
  const daySel = h("select", { class: "ctl", onchange: (e) => setSession({ solarDayOffset: Number(e.target.value) }) },
    [["0", "Local day of the selected time"], ["1", "Next day"], ["2", "Day after next"]].map(([v, t]) => h("option", { value: v, selected: Number(v) === session.solarDayOffset ? "" : null }, t)));
  root.append(h("div", { class: "view-head" }, h("h1", null, "Solar Intelligence"),
    h("span", { class: "sub" }, "Sun geometry, clear-sky bound versus forecast irradiance, modelled array power"),
    h("div", { class: "right" }, h("span", { class: "dim" }, "Irradiance chart day:"), daySel)), body);
  const charts = chartHost();
  let lastSig = "";
  return {
    update() {
      loadSolar(); loadForecast({ hours: 48, key: "forecastSolar" }); loadObservations({ hours: 24, key: "observationsSolar" }); loadEnergy(); loadSatcorps(); loadGhiBias();
      const sig = signature(KEYS, String(session.solarDayOffset));
      if (sig === lastSig) return;
      lastSig = sig; charts.destroyAll();
      replace(body, layout(charts));
    },
    destroy() { charts.destroyAll(); },
  };
}

function layout(charts) {
  if (!activePoint()) return positionUnknown("Sun position, day events and irradiance are computed only for a known position.");
  const g = h("div", { class: "grid", style: { gridTemplateColumns: "340px minmax(0, 1fr)" } });
  g.append(h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } }, positionPanel(), eventsPanel(), satcorpsPanel()),
    h("div", { style: { display: "flex", flexDirection: "column", gap: "12px", minWidth: 0 } }, irradiancePanel(charts), powerPanel(charts)));
  return g;
}

function positionPanel() {
  const r = res("solarPos");
  return panel("Sun position", withResource(r, (p) => {
    const day = p.apparent_elevation_deg > 0;
    return [
      h("div", { style: { marginBottom: "6px" } }, day ? chip("SUN ABOVE HORIZON", "ok") : chip("SUN BELOW HORIZON - irradiance is zero", "mute"), " ", kindChip(p.provenance?.kind || "derived")),
      kv([
        ["Apparent elevation", numCell(p.apparent_elevation_deg, "°", { digits: 2, prov: p.provenance, big: true })],
        ["Geometric elevation", numCell(p.elevation_deg, "°", { digits: 2, prov: p.provenance })],
        ["Azimuth (from N, clockwise)", h("span", null, numCell(p.azimuth_deg, "°", { digits: 1, prov: p.provenance }), ` ${compass(p.azimuth_deg)}`)],
        ["Zenith (geometric)", numCell(p.zenith_deg, "°", { digits: 2, prov: p.provenance })],
        ["Zenith (apparent)", numCell(p.apparent_zenith_deg, "°", { digits: 2, prov: p.provenance })],
        ["Relative air mass", p.air_mass !== null && p.air_mass !== undefined ? numCell(p.air_mass, "", { digits: 2, prov: p.provenance }) : textCell(null, day ? "not reported" : "undefined below the horizon")],
        ["Declination", numCell(p.declination_deg, "°", { digits: 2, reason: "not reported" })],
        ["Equation of time", numCell(p.equation_of_time_min, "min", { digits: 2, reason: "not reported" })],
        ["Computed for", `${fmtUtc(p.time_utc, { seconds: true })}`],
      ], "tight"),
    ];
  }, { blockedNode: () => positionUnknown() }), { footer: r.data ? provFooter(r.data.provenance) : null });
}

function eventsPanel() {
  const r = res("solarDay"); const { tz } = vehicleTimezone();
  return panel("Sunrise, solar noon, sunset", withResource(r, (d) => {
    const tzName = d.timezone_name || tz;
    const row = (label, iso, reason) => [label, iso
      ? h("div", null, h("span", { class: "num xl" }, tzName ? fmtLocal(iso, tzName, { timeOnly: true }) : DASH), h("div", { class: "note" }, `${fmtUtc(iso)}${tzName ? "" : " - local time unavailable (no timezone)"}`))
      : textCell(null, reason)];
    return [
      !tzName ? h("div", { class: "note warn" }, "No timezone reported: local times unavailable, UTC shown.") : h("div", { class: "note" }, `Local date ${d.local_date}, zone ${tzName}`),
      kv([
        row("Sunrise", d.sunrise_utc, d.is_polar_night ? "polar night: Sun does not rise" : d.is_polar_day ? "polar day: Sun does not set" : "not reported"),
        row("Solar noon", d.solar_noon_utc, "not reported"),
        row("Sunset", d.sunset_utc, d.is_polar_night ? "polar night: Sun does not rise" : d.is_polar_day ? "polar day: Sun does not set" : "not reported"),
        ["Day length", numCell(d.day_length_s / 3600, "h", { digits: 2, prov: d.provenance })],
        ["Transit elevation", numCell(d.transit_elevation_deg, "°", { digits: 1, prov: d.provenance })],
      ]),
    ];
  }, { blockedNode: () => positionUnknown() }), { footer: r.data ? provFooter(r.data.provenance) : null });
}

function satcorpsPanel() {
  const r = res("satcorps");
  return panel("NASA SatCORPS cross-check", withResource(r, (d) => {
    const v = d.values || {};
    const ageH = d.age_s != null ? (d.age_s / 3600) : null;
    const phaseLbl = d.cloud_phase_label || (v.cloud_phase != null ? `code ${v.cloud_phase}` : null);
    return [
      h("div", { style: { marginBottom: "6px" } },
        v.cloud_phase === 4 ? chip("CLEAR SKY", "ok", "SatCORPS phase 4") :
        v.cloud_phase === 1 || v.cloud_phase === 2 ? chip(phaseLbl?.toUpperCase() || "CLOUDY", "warn") :
        chip(phaseLbl?.toUpperCase() || "NO RETRIEVAL", "mute"),
        " ", kindChip("derived")),
      kv([
        ["Cloud phase", textCell(phaseLbl)],
        ["Cloud optical depth (VIS)", numCell(v.cloud_visible_optical_depth, "", { digits: 2, reason: "clear pixel or no retrieval" })],
        ["Cloud top height", numCell(v.cloud_top_height, "km", { digits: 2, reason: "clear pixel or no retrieval" })],
        ["Cloud top temperature", numCell(v.cloud_top_temperature, "K", { digits: 1, reason: "clear pixel or no retrieval" })],
        ["Broadband SW albedo", numCell(v.broadband_shortwave_albedo, "%", { digits: 1, reason: "no retrieval" })],
        ["Scene time (UTC)", textCell(d.scene_time_utc)],
        ["Age (NASA latency)", ageH != null ? textCell(`${ageH.toFixed(1)} h`) : textCell(null, "not computed")],
      ], "tight"),
      h("div", { class: "note" }, d.caveat || ""),
    ];
  }, { blockedNode: () => positionUnknown("SatCORPS reads the pixel at the car's position; without a fix there is no point to read.") }),
  { footer: r.data ? h("span", { class: "faint" }, r.data.attribution || r.data.source || "") : null });
}

function irradiancePanel(charts) {
  const cs = res("clearskyDay"), fc = res("forecastSolar"), ob = res("observationsSolar"), day = res("solarDay");
  const box = h("div");
  const { tz } = vehicleTimezone();
  const utc = session.timeBasis === "utc";

  for (const r of [cs, fc]) if (r.status === "error" || r.status === "loading") box.append(withResource(r, () => h("span")));

  const csMaps = { ghi: new Map(), dni: new Map(), dhi: new Map() };
  for (const p of cs.data || []) {
    const t = Math.round(toMs(p.time_utc) / 1000);
    csMaps.ghi.set(t, p.ghi_w_m2); csMaps.dni.set(t, p.dni_w_m2); csMaps.dhi.set(t, p.dhi_w_m2);
  }
  const csTimes = [...csMaps.ghi.keys()].sort((a, b) => a - b);
  const lo = csTimes.length ? csTimes[0] : null, hi = csTimes.length ? csTimes[csTimes.length - 1] : null;
  const clip = (m) => { if (lo === null) return m; const o = new Map(); m.forEach((v, k) => { if (k >= lo && k <= hi) o.set(k, v); }); return o; };

  const fcPts = ["ghi", "dni", "dhi"].map((v) => clip(pointsOf(fc.data, v).map));
  const obPts = [clip(pointsOf(ob.data, "ghi").map)];
  const { x, values } = alignMaps([csMaps.ghi, csMaps.dni, csMaps.dhi, ...fcPts, ...obPts]);

  const series = [
    { label: "GHI clear-sky MODEL BOUND", unit: "W/m²", color: "--series-4", kind: "clear", values: values[0], digits: 0 },
    { label: "DNI clear-sky MODEL BOUND", unit: "W/m²", color: "--series-5", kind: "clear", values: values[1], digits: 0 },
    { label: "DHI clear-sky MODEL BOUND", unit: "W/m²", color: "--series-6", kind: "clear", values: values[2], digits: 0 },
    { label: "GHI forecast", unit: "W/m²", color: "--series-4", kind: "fcst", values: values[3], digits: 0 },
    { label: "DNI forecast", unit: "W/m²", color: "--series-5", kind: "fcst", values: values[4], digits: 0 },
    { label: "DHI forecast", unit: "W/m²", color: "--series-6", kind: "fcst", values: values[5], digits: 0 },
  ];
  const hasObsGhi = obPts[0].size > 0;
  if (hasObsGhi) series.push({ label: "GHI observed", unit: "W/m²", color: "--text", kind: "obs", values: values[6], digits: 0 });

  const bands = [];
  const d = day.data;
  if (d && lo !== null) {
    if (d.sunrise_utc) bands.push([lo, toMs(d.sunrise_utc) / 1000]);
    if (d.sunset_utc) bands.push([toMs(d.sunset_utc) / 1000, hi]);
    if (d.is_polar_night) bands.push([lo, hi]);
  }
  const nowS = effectiveTimeMs() / 1000;
  const chartDiv = h("div", { class: "chart" });
  append(box, [
    h("div", { class: "lastgood", style: { borderColor: "var(--k-clearsky)", background: "rgba(229,167,59,0.1)" } }, kindChip("clear_sky_model"),
      " The dotted amber-filled curves are a physical MODEL UPPER BOUND for a cloudless sky. They are not measured irradiance and not a forecast."),
    ghiBiasBanner(),
    chartDiv,
    styleLegend([
      { color: "--series-4", kind: "clear", text: "Clear-sky model bound (GHI, DNI, DHI)" },
      { color: "--series-4", kind: "fcst", text: "Forecast (dashed)" },
      ...(hasObsGhi ? [{ color: "--text", kind: "obs", text: "Observed GHI (solid, markers)" }] : []),
    ]),
    !hasObsGhi && res("observable").status === "ok" ? h("div", { class: "note warn" }, "Observed irradiance: " + (Object.values(res("observable").data).flat().includes("ghi") ? "the provider returned no GHI observations for this place and time." : "no configured provider measures GHI (not listed by GET /api/weather/observable), so only the forecast and the model bound are drawn.")) : null,
    ob.status === "error" ? h("div", { class: "note warn" }, ob.error?.kind === "unsupported" ? `Observed irradiance: observations unavailable (HTTP 501: ${ob.error.message}), so only the forecast and the model bound are drawn.` : `Observed irradiance unavailable: ${ob.error?.message}`) : null,
    d && (d.sunrise_utc || d.sunset_utc) ? h("div", { class: "note" }, "Grey shading = Sun below the horizon (from API sunrise/sunset).") : null,
    lo === null && cs.status === "ok" ? stateBox("unknown", "Clear-sky series is empty", "GET /api/solar/clearsky/day returned no points.") : null]);
  charts.add(chartDiv, { x, series, tz, utc, boundarySec: x.length && nowS >= x[0] && nowS <= x[x.length - 1] ? nowS : null, boundaryLabels: ["PAST", "FUTURE"], bands, yLabel: "Irradiance (W/m²)", height: 260, emptyText: "No irradiance points to plot." });

  const prov = fc.data ? [fc.data.provider, fc.data.dataset, fc.data.model_name].filter(Boolean).join(" / ") : "forecast unavailable";
  const dateInfo = solarDate();
  return panel(`Irradiance for ${dateInfo.basis === "utc" ? "UTC date" : "local day"} ${dateInfo.date}${dateInfo.basis === "utc" ? " (timezone unknown)" : ""}`, box, {
    footer: h("footer", { class: "prov" }, "Clear-sky: ", cs.data?.[0] ? provenanceLabel(cs.data[0].provenance) : DASH, "  ·  Forecast: ", prov, fc.data ? [" retrieved ", ageSpan(fc.data.retrieved_at_utc)] : ""),
  });
}

function provenanceLabel(p) { return [p.provider, p.dataset, p.model_name].filter(Boolean).join(" / "); }

function ghiBiasBanner() {
  const r = res("ghiBias");
  if (r.status !== "ok" || !r.data) return null;
  const d = r.data;
  const b = d.bias_vs_truth || {};
  const agree = Math.sign(b.ecmwf) === Math.sign(b.power) && Math.sign(b.power) === Math.sign(b.merra2);
  // Flag only when the three products agree in sign AND the median bias is
  // worth caring about operationally (>=15 W/m^2 by convention).
  const strong = agree && Math.abs(b.median) >= 15;
  if (!strong) return null;
  const sign = b.median > 0 ? "+" : "";
  const dir = b.median > 0 ? "higher" : "lower";
  const dist = d.distance_km > 50
    ? ` (nearest tabulated point, ${d.distance_km} km away)`
    : "";
  return h("div", { class: "note warn", style: { borderLeft: "3px solid var(--warn)", paddingLeft: "8px" } },
    "GHI bias at ", h("b", null, d.nearest_point), dist,
    `: three gridded reanalyses read ${sign}${b.median.toFixed(0)} W/m² ${dir} than BoM-anchored SILO over BWSC-2025 race week `,
    `(ECMWF ${b.ecmwf > 0 ? "+" : ""}${b.ecmwf.toFixed(1)}, POWER ${b.power > 0 ? "+" : ""}${b.power.toFixed(1)}, MERRA-2 ${b.merra2 > 0 ? "+" : ""}${b.merra2.toFixed(1)}). `,
    `Treat the clear-sky and forecast GHI as likely to ${b.median > 0 ? "overread" : "underread"} by this amount here.`);
}

function powerPanel(charts) {
  const r = res("energy"); const { tz } = vehicleTimezone();
  return panel("Modelled array power", withResource(r, (d) => {
    if (!d.power.length) return stateBox("unknown", "No power estimates", "GET /api/energy/forecast returned no points.");
    const net = new Map(), dc = new Map(), poa = new Map();
    for (const p of d.power) { const t = Math.round(toMs(p.time_utc) / 1000); net.set(t, p.net_power_w); dc.set(t, p.dc_power_w); poa.set(t, p.poa?.poa_global_w_m2 ?? null); }
    const { x, values } = alignMaps([net, dc, poa]);
    const peak = d.power.reduce((m, p) => (p.net_power_w > m.net_power_w ? p : m), d.power[0]);
    const a = peak.array;
    const div = h("div", { class: "chart" });
    const nowS = effectiveTimeMs() / 1000;
    charts.add(div, { x, tz, utc: session.timeBasis === "utc", height: 200, yLabel: "Power (W) / POA (W/m²)",
      boundarySec: nowS >= x[0] && nowS <= x[x.length - 1] ? nowS : null, boundaryLabels: ["PAST", "FUTURE"],
      series: [
        { label: "Net array power (MODEL)", unit: "W", color: "--series-2", kind: "model", values: values[0], digits: 0 },
        { label: "DC array power (MODEL)", unit: "W", color: "--series-3", kind: "fcst", values: values[1], digits: 0 },
        { label: "POA irradiance (MODEL)", unit: "W/m²", color: "--series-4", kind: "clear", values: values[2], digits: 0 },
      ] });
    const e = d.energy; const ew = e ? fmtEnergyWh(e.energy_wh) : null;
    return [
      h("div", { style: { marginBottom: "6px" } }, kindChip(peak.provenance?.kind || "physical_model"), " ", h("span", { class: "note" }, "Output of a PV model driven by forecast irradiance. Never a measurement of the real array.")),
      div,
      kv([
        ["Peak net power in window", h("span", null, numCell(peak.net_power_w, "W", { digits: 0, prov: peak.provenance }), ` at ${fmtUtc(peak.time_utc)}`)],
        ["Integrated energy in window", e ? numCell(ew.unit === "kWh" ? e.energy_wh / 1000 : e.energy_wh, ew.unit, { digits: ew.unit === "kWh" ? 2 : 0, prov: e.provenance, extra: [`Method: ${e.integration_method}`, `${e.sample_count} samples`, e.uncertainty_wh != null ? `Uncertainty: +/-${fmtNum(e.uncertainty_wh, 0)} Wh` : "No uncertainty estimate supplied"] }) : textCell(null, "EnergyEstimate not in response")],
        ["Array", `${a?.name || DASH}, ${a?.area_m2 != null ? fmtNum(a.area_m2, 2) + " m²" : DASH}`],
        ["Array parameters", a?.parameter_source === "measured" ? "measured" : h("span", { class: "is-stale" }, `${(a?.parameter_source || "unspecified").toUpperCase()} - not a measured JU Solar Team specification`)],
        ["Cell temperature model", peak.cell_temperature_model || DASH],
      ], "tight"),
    ];
  }, { blockedNode: () => positionUnknown() }), { footer: r.data?.power?.[0] ? provFooter(r.data.power[0].provenance) : null });
}
