/**
 * Race - the one-screen landing view for race day.
 * Six tiles: position/time, weather at the car, modelled solar, next 3 h along
 * the route, next control stop, warning+critical alerts. Battery SoC and
 * measured irradiance have no data source yet and say so.
 * @module views/race
 */
import { h, replace } from "../dom.js";
import { DASH, fmtNum, fmtUtc, fmtLocal, fmtWind, fmtPowerW, compass, toMs, provenanceTitle } from "../format.js";
import { panel, kindChip, modeChip, chip, severityChip, positionUnknown, stateBox, withResource } from "../ui.js";
import {
  res, activeState, activePoint, effectiveTimeMs, isSimulation, vehicleTimezone, displayRoute, signature, session, controlStops, routeProgress,
} from "../store.js";
import { loadSolar, loadRouteGeojson, loadProgress, loadForecast, loadEnergy, loadAlerts, loadTrajectory, nearestStep } from "../loaders.js";

const KEYS = ["state", "routes", "routeGeojson", "progress", "solarPos", "forecastRace", "energy", "alerts", "trajectory", "routeWeather"];
const STOP_HOUR = 17; // BWSC daily driving ends 17:00 local (docs/research)

export function create(root) {
  const body = h("div", { class: "race" });
  root.append(body);
  let lastSig = "";
  const clock = setInterval(() => paint(body), 1000);
  return {
    update() {
      loadSolar(); loadRouteGeojson(); loadProgress(); loadEnergy(); loadAlerts(); loadTrajectory();
      loadForecast({ hours: 4, key: "forecastRace" });
      const sig = signature(KEYS);
      if (sig !== lastSig) { lastSig = sig; replace(body, layout()); paint(body); }
    },
    destroy() { clearInterval(clock); },
  };
}

function layout() {
  return h("div", { class: "race-grid" }, positionTile(), weatherTile(), solarTile(), aheadTile(), stopTile(), alertsTile());
}

/* ---------------------------------------------------------------- helpers */

const big = (v, unit, prov) => h("span", { class: "race-big", title: prov ? provenanceTitle(prov) : "" }, v, h("small", null, unit));
const metric = (label, node) => h("div", { class: "race-m" }, h("div", { class: "race-l" }, label), node);
const nodata = (why) => h("span", { class: "race-na", title: why }, "ingen data");
const val = (m, digits = 0) => (m && m.value !== null && m.value !== undefined ? fmtNum(m.value, digits) : null);

/** Minutes from the effective time to 17:00 local at the vehicle, or null without a timezone. */
function minutesToStop(tz) {
  if (!tz) return null;
  const t = effectiveTimeMs();
  const parts = new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).formatToParts(new Date(t));
  const hh = Number(parts.find((p) => p.type === "hour").value), mm = Number(parts.find((p) => p.type === "minute").value);
  return STOP_HOUR * 60 - (hh * 60 + mm);
}

function paint(root) {
  const t = effectiveTimeMs();
  const { tz } = vehicleTimezone();
  const l = root.querySelector('[data-clock="local"]'), u = root.querySelector('[data-clock="utc"]'), s = root.querySelector('[data-clock="stop"]');
  if (l) l.textContent = tz && activeState()?.fix ? fmtLocal(t, tz, { timeOnly: true, seconds: true }) : DASH;
  if (u) u.textContent = fmtUtc(t, { seconds: true }).slice(11, -1) + " UTC";
  if (s) {
    const m = activeState()?.fix ? minutesToStop(tz) : null;
    s.textContent = m === null ? DASH : m <= 0 ? "efter 17:00" : `${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")} min`;
  }
}

/* ------------------------------------------------------------------ tiles */

function positionTile() {
  const st = res("state");
  return panel("Position och tid", withResource(st, (s) => {
    const { tz, source } = vehicleTimezone();
    const head = h("div", { class: "race-row" }, modeChip(s.mode), isSimulation() ? chip("SIMULERING", "warn") : null);
    if (!s.fix?.point) return [head, positionUnknown("Ingen position: inget beräknas för en gissad plats.")];
    const p = s.fix.point;
    const rp = routeProgress();
    return [head,
      h("div", { class: "race-clock", dataset: { clock: "local" }, title: tz ? `${tz} (${source})` : "ingen tidszon" }, DASH),
      h("div", { class: "race-sub", dataset: { clock: "utc" } }, DASH),
      h("div", { class: "race-sub" }, `${p.latitude_deg.toFixed(4)}, ${p.longitude_deg.toFixed(4)}`,
        rp.m !== null && rp.m !== undefined ? ` · ${fmtNum(rp.m / 1000, 1)} km längs rutten` : " · ej på rutten"),
    ];
  }));
}

function weatherTile() {
  if (!activePoint()) return panel("Väder vid bilen", positionUnknown());
  const r = res("forecastRace");
  return panel("Väder vid bilen", withResource(r, (d) => {
    const step = nearestStep(d, effectiveTimeMs());
    if (!step) return stateBox("unknown", "Inget prognossteg inom 90 min", "");
    const v = step.values || {};
    const ws = v.wind_speed_10m?.value, wd = v.wind_direction_10m?.value;
    const pr = val(v.precipitation, 1), pp = val(v.precipitation_probability);
    return h("div", { class: "race-ms" },
      metric("Vind", ws !== null && ws !== undefined ? h("span", null, big(fmtNum(ws, 0), " km/h", v.wind_speed_10m.provenance), h("div", { class: "race-sub" }, `${wd !== null && wd !== undefined ? "från " + compass(wd) : ""} ${fmtWind(ws)}`)) : nodata("saknas i prognosen")),
      metric("Moln", val(v.cloud_cover) !== null ? big(val(v.cloud_cover), " %", v.cloud_cover.provenance) : nodata("saknas i prognosen")),
      metric("Temp", val(v.temperature_2m) !== null ? big(val(v.temperature_2m), " °C", v.temperature_2m.provenance) : nodata("saknas i prognosen")),
      metric("Regn", pr !== null || pp !== null ? big(pr ?? DASH, ` mm/h${pp !== null ? " · " + pp + " %" : ""}`, v.precipitation?.provenance) : nodata("saknas i prognosen")));
  }, { blockedNode: () => positionUnknown() }), {
    right: kindChip("forecast"),
    footer: r.data ? h("footer", { class: "prov" }, `${r.data.provider}${r.data.model_name ? " / " + r.data.model_name : ""} · prognos, inte mätning`) : null,
  });
}

function solarTile() {
  if (!activePoint()) return panel("Sol", positionUnknown());
  const en = res("energy"), sp = res("solarPos");
  let power = nodata("ingen energiprognos nära denna tid");
  if (en.data?.power?.length) {
    const tms = effectiveTimeMs();
    let best = null, bd = Infinity;
    for (const p of en.data.power) { const dt = Math.abs(toMs(p.time_utc) - tms); if (dt < bd) { bd = dt; best = p; } }
    if (best && bd <= 5400e3) {
      const w = fmtPowerW(best.net_power_w);
      power = big(fmtNum(w.unit === "kW" ? best.net_power_w / 1000 : best.net_power_w, w.unit === "kW" ? 2 : 0), " " + w.unit, best.provenance);
    }
  }
  const el = sp.data ? big(fmtNum(sp.data.apparent_elevation_deg, 1), "°", sp.data.provenance) : nodata(sp.blockedReason || sp.error?.message || "laddas");
  return panel("Sol", h("div", { class: "race-ms" },
    metric("Effekt (modell)", power),
    metric("Solhöjd", el),
    metric("Kvar till 17:00", h("span", { class: "race-big", dataset: { clock: "stop" } }, DASH)),
    metric("Batteri SoC", nodata("ingen batteritelemetri i API:t")),
    metric("Uppmätt instrålning", nodata("ingen pyranometer ansluten"))), {
    right: kindChip("physical_model"),
    footer: h("footer", { class: "prov" }, "Effekt är modellerad (pvlib + prognos), inte mätt."),
  });
}

function aheadTile() {
  const rw = res("routeWeather"), tr = res("trajectory");
  const samples = rw.data?.samples || rw.data?.forecast?.samples || [];
  if (!samples.length) {
    const why = tr.status === "blocked" ? tr.blockedReason : rw.error?.message || (rw.status === "loading" ? "laddas…" : "ingen ruttprognos");
    return panel("Nästa 3 h längs rutten", stateBox("unknown", "Ingen ruttprognos", why));
  }
  const t0 = effectiveTimeMs();
  const { tz } = vehicleTimezone();
  // one row per ~30 min of predicted arrival, first 3 h
  const rows = [];
  let lastMs = -Infinity;
  for (const s of samples) {
    const eta = toMs(s.trajectory_point?.eta_utc);
    if (!Number.isFinite(eta) || eta < t0 - 60e3 || eta > t0 + 3 * 3600e3) continue;
    if (eta - lastMs < 30 * 60e3) continue;
    lastMs = eta;
    const cc = s.values?.cloud_cover, g = s.values?.ghi;
    rows.push(h("tr", null,
      h("td", { class: "num" }, tz ? fmtLocal(eta, tz, { timeOnly: true }) : fmtUtc(eta).slice(11)),
      h("td", { class: "num" }, fmtNum(s.trajectory_point.distance_m / 1000, 0), " km"),
      h("td", { class: "num", title: cc ? provenanceTitle(cc.provenance) : "" }, val(cc) !== null ? val(cc) + " %" : DASH),
      h("td", { class: "num", title: g ? provenanceTitle(g.provenance) : "" }, val(g) !== null ? val(g) + " W/m²" : DASH)));
  }
  return panel("Nästa 3 h längs rutten", rows.length
    ? h("table", { class: "t race-t" }, h("thead", null, h("tr", null, h("th", null, "Ankomst"), h("th", null, "Rutt"), h("th", null, "Moln"), h("th", null, "GHI"))), h("tbody", null, rows))
    : stateBox("unknown", "Inga punkter inom 3 h", ""), {
    right: kindChip("forecast"),
    footer: h("footer", { class: "prov" }, `${rw.data?.provider || rw.data?.forecast?.provider || ""} · antagen fart ${session.speedKmh} km/h · prognos`),
  });
}

function stopTile() {
  const dr = displayRoute();
  if (!dr?.info) return panel("Nästa kontrollstopp", stateBox("unknown", "Ingen rutt", ""));
  const stops = [...controlStops().stops].sort((a, b) => a.cumulative_distance_m - b.cumulative_distance_m);
  const prog = routeProgress().m;
  if (!stops.length) return panel("Nästa kontrollstopp", stateBox("unknown", "Inga kontrollstopp definierade", "Officiell lista ej publicerad."));
  if (prog === null || prog === undefined) return panel("Nästa kontrollstopp", stateBox("unknown", "Okänd ruttposition", "Avstånd kan inte beräknas utan position på rutten."));
  const next = stops.find((c) => c.cumulative_distance_m > prog);
  if (!next) return panel("Nästa kontrollstopp", stateBox("", "Inga fler kontrollstopp", ""));
  const rem = next.cumulative_distance_m - prog;
  const pts = res("trajectory").data?.points || [];
  const at = pts.find((p) => p.distance_m >= next.cumulative_distance_m);
  const { tz } = vehicleTimezone();
  const eta = at ? (tz ? fmtLocal(at.eta_utc, tz, { timeOnly: true }) : fmtUtc(at.eta_utc).slice(11) + " UTC") : null;
  return panel("Nästa kontrollstopp", [
    h("div", { class: "race-name" }, next.name || "Namnlöst stopp"),
    h("div", { class: "race-ms" },
      metric("Avstånd", big(fmtNum(rem / 1000, 1), " km")),
      metric("ETA", eta ? h("span", null, big(eta, ""), h("div", { class: "race-sub" }, `vid ${session.speedKmh} km/h (antagen)`)) : nodata("ETA bortom prognosfönstret"))),
  ]);
}

function alertsTile() {
  const r = res("alerts");
  return panel("Varningar", withResource(r, (list) => {
    const urgent = list.filter((a) => a.severity === "critical" || a.severity === "warning")
      .sort((a, b) => (a.severity === "critical" ? 0 : 1) - (b.severity === "critical" ? 0 : 1));
    if (!urgent.length) return stateBox("", "Inga varningar", `${list.length} lägre nivå(er) - se Varningar.`);
    return h("ul", { class: "race-alerts" }, urgent.slice(0, 4).map((a) => h("li", { title: provenanceTitle(a.provenance) },
      severityChip(a.severity), " ", h("b", null, a.headline),
      a.recommended_action ? h("div", { class: "race-sub" }, a.recommended_action) : null)),
      urgent.length > 4 ? h("li", null, h("a", { href: "#/alerts" }, `${urgent.length - 4} till`)) : null);
  }), { footer: h("footer", { class: "prov" }, "Plattformen rekommenderar, människor beslutar.") });
}
