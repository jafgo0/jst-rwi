/**
 * View E - Route Intelligence.
 * Trajectory table (distance, ETA local+UTC, forecast weather at arrival,
 * head/tailwind), a speed control that re-requests the trajectory, and route
 * weather plotted along distance.
 * @module views/route
 */
import { h, replace } from "../dom.js";
import { DASH, fmtNum, fmtUtc, fmtLocal, fmtLatLon, fmtHeadTail, fmtDuration, compass, toMs, trueHeadwind } from "../format.js";
import { panel, kv, valueCell, numCell, textCell, kindChip, chip, withResource, stateBox, ageSpan, table, provFooter } from "../ui.js";
import { res, activeState, vehicleTimezone, displayRoute, session, setSession, signature, notify } from "../store.js";
import { loadRoutes, loadTrajectory, startDistance } from "../loaders.js";
import { chartHost, styleLegend } from "../charts.js";

const KEYS = ["state", "routes", "trajectory", "routeWeather"];

export function create(root) {
  const speed = h("input", { type: "number", min: "10", max: "130", step: "1", value: session.speedKmh });
  const start = h("input", { type: "number", min: "0", step: "0.1", placeholder: "km", value: session.startDistanceKm });
  const hours = h("select", { class: "ctl" }, [4, 6, 10, 12, 24].map((n) => h("option", { value: n, selected: n === session.routeHours ? "" : null }, `${n} h`)));
  const hint = h("span", { class: "note" });
  const apply = () => {
    const sp = parseFloat(speed.value);
    if (!(sp >= 1 && sp <= 200)) { hint.textContent = "Enter a speed between 1 and 200 km/h."; return; }
    setSession({ speedKmh: sp, startDistanceKm: start.value, routeHours: Number(hours.value) });
    loadTrajectory(true);
    hint.textContent = "Re-requested.";
  };
  const onKey = (e) => { if (e.key === "Enter") apply(); };
  speed.addEventListener("keydown", onKey); start.addEventListener("keydown", onKey);
  const controls = h("div", { class: "row", style: { marginBottom: "10px" } },
    h("div", { class: "field" }, h("label", null, "Assumed speed (km/h)"), speed),
    h("div", { class: "field" }, h("label", null, "Start distance (km)"), start),
    h("div", { class: "field" }, h("label", null, "Horizon"), hours),
    h("button", { class: "btn primary", onclick: apply }, "Recompute trajectory"), hint);
  const body = h("div");
  root.append(h("div", { class: "view-head" }, h("h1", null, "Route Intelligence"), h("span", { class: "sub" }, "Where the vehicle will be, when, and the forecast it will meet there")), controls, body);
  const charts = chartHost();
  let lastSig = "";
  return {
    update() {
      loadRoutes(); loadTrajectory();
      const sd = startDistance();
      start.disabled = sd.source === "state";
      if (sd.source === "state") start.value = String(Math.round(sd.m / 100) / 10);
      const sig = signature(KEYS, session.speedKmh + "|" + session.routeHours + "|" + sd.source + sd.m);
      if (sig === lastSig) return;
      lastSig = sig; charts.destroyAll();
      replace(body, layout(charts));
    },
    destroy() { charts.destroyAll(); },
  };
}

function layout(charts) {
  const wrap = h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } });
  const dr = displayRoute();
  const sd = startDistance();
  const tr = res("trajectory"), rw = res("routeWeather");
  wrap.append(h("div", { class: "note" }, dr ? `Route: ${dr.info?.name || dr.id}${dr.assumed ? " (no active route in state; first listed route used)" : ""}` : "No route available."));
  if (tr.status === "blocked") {
    wrap.append(stateBox("unknown", "Trajectory not computed", `${tr.blockedReason}. ${res("routes").status === "error" ? "" : "Route progress is not guessed: enter a start distance above to compute a hypothetical trajectory."}`));
    return wrap;
  }
  if (sd.source === "operator") wrap.append(h("div", { class: "banner warn", style: { borderRadius: "4px" } }, h("span", null, "HYPOTHETICAL"), h("small", null, `Start distance ${fmtNum(sd.m / 1000, 1)} km was entered by the operator; the platform does not know the vehicle is there.`)));
  if (tr.status !== "ok") wrap.append(withResource(tr, () => h("span"), { label: "trajectory" }));
  else if (tr.error) wrap.append(withResource(tr, () => h("span")));
  if (rw.status === "error") wrap.append(withResource(rw, () => h("span")));
  if (!tr.data) return wrap;

  const t = tr.data, { tz } = vehicleTimezone();
  const samples = rw.data?.samples || [];
  const byDist = new Map(samples.map((s, k) => [Math.round(s.trajectory_point?.distance_m ?? -1), k]));
  const sampleIdx = (p, i) => (byDist.has(Math.round(p.distance_m)) ? byDist.get(Math.round(p.distance_m)) : samples.length === t.points.length ? i : -1);
  const sampleFor = (p, i) => { const k = sampleIdx(p, i); return k >= 0 ? samples[k] : null; };
  const localFromApi = (p, i) => { const k = sampleIdx(p, i); const l = k >= 0 ? rw.data?.local_arrival_times?.[k] : null; return l ? `${l.slice(0, 10)} ${l.slice(11, 16)} (UTC${l.slice(19) || ""})` : null; };

  const ass = Object.entries(t.assumptions || {});
  wrap.append(panel("Trajectory summary", h("div", { class: "grid", style: { gridTemplateColumns: "1fr 1fr" } },
    kv([
      ["Points", `${t.points.length}`],
      ["Start", t.points.length ? h("span", null, fmtNum(t.points[0].distance_m / 1000, 1) + " km, ", fmtUtc(t.points[0].eta_utc)) : DASH],
      ["Arrival at end of horizon", t.points.length ? h("span", null, fmtNum(t.points[t.points.length - 1].distance_m / 1000, 1) + " km, ", fmtLocal(t.points[t.points.length - 1].eta_utc, tz), " / ", fmtUtc(t.points[t.points.length - 1].eta_utc)) : DASH],
      ["Generated", ageSpan(t.generated_at_utc)],
      ["Speed", h("span", null, numCell(session.speedKmh, "km/h", { digits: 0 }), " ", chip("OPERATOR PARAMETER", "mute", "Assumed speed passed to the API; not a measurement or a recommendation"))],
    ], "tight"),
    kv(ass.length ? ass.map(([k, v]) => [k.replace(/_/g, " "), String(v)]) : [["Assumptions", textCell(null, "API reported none")]], "tight")),
    { footer: provFooter(t.provenance), right: [kindChip(t.provenance?.kind || "derived")] }));

  // ---- table
  const cols = [
    { h: "Dist (km)", cls: "r", cell: (p) => h("span", { class: "num" }, fmtNum(p.distance_m / 1000, 1)) },
    { h: "Position", cell: (p) => h("span", { class: "num faint" }, fmtLatLon(p.point.latitude_deg, p.point.longitude_deg, 3)) },
    { h: "ETA local", title: "Rows with forecast: local time at that route point as computed by the API (UTC offset shown). Rows without: local time in the vehicle's CURRENT timezone (" + (tz || "unknown") + "), marked with *.", cell: (p, i) => { const l = localFromApi(p, i); if (l) return l; return tz ? h("span", { class: "faint", title: "Vehicle's current timezone, not computed at this point" }, fmtLocal(p.eta_utc, tz) + " *") : textCell(null, "no timezone"); } },
    { h: "ETA UTC", cell: (p) => fmtUtc(p.eta_utc) },
    { h: "Speed (km/h)", cls: "r", cell: (p) => numCell(p.speed_mps * 3.6, "km/h", { digits: 0 }) },
    { h: "Grade (%)", cls: "r", cell: (p) => numCell(p.grade * 100, "%", { digits: 1 }) },
    { h: "Waypoint", cell: (p) => (p.waypoint ? h("span", null, p.waypoint.kind === "control_stop" ? chip("STOP", "watch") : "", " ", p.waypoint.name || p.waypoint.kind) : "") },
    { h: "Temp (°C)", cls: "r", cell: (p, i) => (sampleFor(p, i) ? wx(sampleFor(p, i), "temperature_2m") : textCell(null, "not sampled", "The API samples a limited number of points inside the forecast horizon")) },
    { h: "Wind (m/s)", cls: "r", cell: (p, i) => wx(sampleFor(p, i), "wind_speed_10m") },
    { h: "Wind from", cell: (p, i) => { const s = sampleFor(p, i); const m = s?.values?.wind_direction_10m; return s ? h("span", null, valueCell(m, { variable: "wind_direction_10m" }), m?.value != null ? " " + compass(m.value) : "") : DASH; } },
    { h: "Head / tail (wind)", title: "Component of the TRUE wind along the direction of travel (computed from forecast wind speed/direction and the heading at that point). HEAD opposes the vehicle, TAIL assists.", cell: (p, i) => { const s = sampleFor(p, i); if (!s) return DASH; const w = s.values?.wind_speed_10m, d = s.values?.wind_direction_10m; const ht = fmtHeadTail(trueHeadwind(w?.value, d?.value, p.heading_deg)); return ht.text === DASH ? textCell(null, "no wind") : h("span", { class: "has-prov", title: "True-wind component along track = speed x cos(wind-from minus heading)\n" + (w ? "Wind: " + w.provenance?.provider + " / " + (w.provenance?.model_name || "") : "") }, ht.label ? chip(ht.label, ht.label === "HEAD" ? "warn" : "ok") : "", " ", numCell(parseFloat(ht.text), "m/s", { digits: 1 })); } },
    { h: "Apparent head (m/s)", title: "relative_wind.headwind_mps from the API: the wind the vehicle feels along its axis. It includes the vehicle's own speed, so it is large and positive at speed.", cls: "r", cell: (p, i) => { const s = sampleFor(p, i); return s?.relative_wind ? numCell(s.relative_wind.headwind_mps, "m/s", { digits: 1 }) : DASH; } },
    { h: "Cross (m/s)", cls: "r", cell: (p, i) => { const s = sampleFor(p, i); return s?.relative_wind ? numCell(Math.abs(s.relative_wind.crosswind_mps), "m/s", { digits: 1 }) : DASH; } },
    { h: "Cloud (%)", cls: "r", cell: (p, i) => wx(sampleFor(p, i), "cloud_cover") },
    { h: "GHI (W/m²)", cls: "r", cell: (p, i) => wx(sampleFor(p, i), "ghi") },
    { h: "Lead", cls: "r", cell: (p, i) => { const s = sampleFor(p, i); return s?.lead_time_s != null ? fmtDuration(s.lead_time_s) : DASH; } },
  ];
  wrap.append(panel("Trajectory and forecast at arrival", [
    h("div", { style: { marginBottom: "6px" } }, kindChip("forecast"), h("span", { class: "note" }, " Weather columns are forecasts valid at each predicted arrival time (not observations). Hover a value for provider, model run and age.")),
    rw.status === "blocked" || (rw.status === "ok" && !samples.length) ? stateBox("unknown", "No route weather", rw.blockedReason || "GET /api/route/{id}/weather returned no samples.") : null,
    rw.data?.notes?.length ? h("div", { class: "note warn", style: { marginBottom: "6px" } }, "Route weather notes: " + rw.data.notes.join(" | ")) : null,
    h("div", { class: "note", style: { marginBottom: "6px" } }, "Rows marked \u201cnot sampled\u201d are outside the forecast horizon or were not selected (the API samples a limited number of route points). * = local time in the vehicle's current timezone."),
    table(cols, t.points, { maxHeight: "440px", rowClass: (p) => (p.waypoint?.kind === "control_stop" ? "mark" : "") }),
  ], { footer: rw.data ? h("footer", { class: "prov" }, `Weather: provider ${rw.data.provider}; generated `, ageSpan(rw.data.generated_at_utc), samples[0]?.values?.temperature_2m ? "; model " + (samples[0].values.temperature_2m.provenance?.model_name || DASH) : "") : null }));

  // ---- charts along distance
  if (samples.length) {
    const x = samples.map((s) => (s.trajectory_point?.distance_m ?? 0) / 1000);
    const col = (v, f = (a) => a) => samples.map((s) => { const m = s.values?.[v]; return m && m.value != null ? f(m.value) : null; });
    const ws = col("wind_speed_10m"), head = samples.map((s) => trueHeadwind(s.values?.wind_speed_10m?.value, s.values?.wind_direction_10m?.value, s.trajectory_point?.heading_deg)), cross = samples.map((s) => (s.relative_wind ? Math.abs(s.relative_wind.crosswind_mps) : null));
    const d1 = h("div", { class: "chart" }), d2 = h("div", { class: "chart" });
    charts.add(d1, { xMode: "distance", x, height: 200, yLabel: "Wind (m/s)", series: [
      { label: "Wind speed 10 m", unit: "m/s", color: "--series-2", kind: "fcst", values: ws },
      { label: "Wind along track: head (+) / tail (−)", unit: "m/s", color: "--series-1", kind: "fcst", values: head },
      { label: "Crosswind (abs)", unit: "m/s", color: "--series-5", kind: "fcst", values: cross },
    ] });
    charts.add(d2, { xMode: "distance", x, height: 200, yLabel: "Cloud cover (%)", y2Label: "GHI (W/m²)", series: [
      { label: "Cloud cover", unit: "%", color: "--series-3", kind: "fcst", values: col("cloud_cover"), digits: 0 },
      { label: "GHI forecast at ETA", unit: "W/m²", color: "--series-4", kind: "fcst", values: col("ghi"), scale: "y2", digits: 0 },
    ] });
    wrap.append(h("div", { class: "grid g-2" },
      panel("Route wind along distance", [d1, styleLegend([{ color: "--series-2", kind: "fcst", text: "All values FORECAST at predicted arrival time" }])]),
      panel("Cloud and irradiance along distance", [d2, styleLegend([{ color: "--series-3", kind: "fcst", text: "All values FORECAST at predicted arrival time. GHI is forecast, not clear-sky" }])])));
  }
  return wrap;
}

function wx(sample, v) {
  if (!sample) return DASH;
  const m = sample.values?.[v];
  return valueCell(m, { variable: v });
}

void toMs; void activeState; void notify;
