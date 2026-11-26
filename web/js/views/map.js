/**
 * Karta: full-screen dark weather map over the race route.
 * Layers (pills): modelled GHI heatmap, cloud cover overlay, wind arrows
 * (all from GET /api/map/grid, AIFS or Open-Meteo IFS) and the latest
 * Himawari-9 cloud index (GET /api/satellite/field). A time slider walks the
 * forecast valid times; the HUD and route popup read the same grid.
 * Nothing here is a measurement: grid values are forecasts, Himawari is a
 * satellite retrieval. The vehicle marker is drawn only when the API has a fix.
 * @module views/map
 */
import { h, replace } from "../dom.js";
import { api } from "../api.js";
import { cfg, mapStyleUrl, prefs } from "../settings.js";
import { DASH, compass, fmtLocal, fmtNum, fmtUtc, nowMs } from "../format.js";
import { res, activePoint, displayRoute, controlStops, vehicleTimezone, signature } from "../store.js";
import { loadRoutes, loadRouteGeojson } from "../loaders.js";

const KEYS = ["state", "routes", "routeGeojson"];
const GRID_REFRESH_MS = 15 * 60 * 1000;
const SAT_REFRESH_MS = 10 * 60 * 1000;
const PLAY_MS = 900;

const MODELS = [
  { id: "aifs", label: "AIFS" },
  { id: "ifs", label: "IFS (Open-Meteo)" },
];

const LAYERS = [
  { id: "ghi", label: "Sol (GHI)", on: true },
  { id: "cloud", label: "Moln", on: false },
  { id: "wind", label: "Vind", on: true },
  { id: "sat", label: "Himawari-9", on: false },
];

/** Warm GHI ramp, W/m². */
const GHI_STOPS = [[0, [20, 10, 40, 0]], [100, [59, 15, 112, 150]], [300, [140, 41, 129, 185]], [500, [222, 73, 104, 200]], [700, [254, 159, 109, 210]], [950, [252, 253, 191, 220]]];
/** Wind speed colour scale, km/h. */
const WIND_STOPS = [[0, "#7fd4ff"], [15, "#5ee37a"], [30, "#f5e04a"], [45, "#ff8a3d"], [60, "#ff3d6e"]];

const empty = () => ({ type: "FeatureCollection", features: [] });

/* ------------------------------------------------------------- pure helpers */

function lerpStops(stops, v) {
  if (v <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i++) {
    const [x1, c1] = stops[i];
    if (v <= x1) {
      const [x0, c0] = stops[i - 1];
      const f = (v - x0) / (x1 - x0);
      return c0.map((c, k) => c + (c1[k] - c) * f);
    }
  }
  return stops[stops.length - 1][1];
}

/** Bilinear value of field[t] at (lat, lon); null outside the grid or next to a null node. */
export function sampleGrid(grid, name, t, lat, lon) {
  const f = grid?.fields?.[name]?.[t];
  if (!f) return null;
  const { lats, lons } = grid;
  if (lats.length < 1 || lons.length < 1) return null;
  const step = grid.provenance?.grid_step_deg || (lats[1] - lats[0]) || 1;
  const fi = (lat - lats[0]) / step, fj = (lon - lons[0]) / step;
  if (fi < 0 || fj < 0 || fi > lats.length - 1 || fj > lons.length - 1) return null;
  const i0 = Math.floor(fi), j0 = Math.floor(fj);
  const i1 = Math.min(i0 + 1, lats.length - 1), j1 = Math.min(j0 + 1, lons.length - 1);
  const a = f[i0][j0], b = f[i0][j1], c = f[i1][j0], d = f[i1][j1];
  if ([a, b, c, d].some((x) => x === null || x === undefined)) return null;
  const di = fi - i0, dj = fj - j0;
  if (name === "wind_direction_deg") { // circular: interpolate the unit vector
    const rad = (x) => (x * Math.PI) / 180;
    const w = [(1 - di) * (1 - dj), (1 - di) * dj, di * (1 - dj), di * dj];
    const s = [a, b, c, d].reduce((acc, x, k) => acc + w[k] * Math.sin(rad(x)), 0);
    const co = [a, b, c, d].reduce((acc, x, k) => acc + w[k] * Math.cos(rad(x)), 0);
    return ((Math.atan2(s, co) * 180) / Math.PI + 360) % 360;
  }
  return (1 - di) * ((1 - dj) * a + dj * b) + di * ((1 - dj) * c + dj * d);
}

/** Corner coordinates for a MapLibre image source covering node-centred cells. */
function gridCorners(grid) {
  const s = grid.provenance?.grid_step_deg || 1;
  const la0 = grid.lats[0] - s / 2, la1 = grid.lats[grid.lats.length - 1] + s / 2;
  const lo0 = grid.lons[0] - s / 2, lo1 = grid.lons[grid.lons.length - 1] + s / 2;
  return [[lo0, la1], [lo1, la1], [lo1, la0], [lo0, la0]];
}

/** Paint a (rows x cols) value array north-up into a smooth, upscaled data URL. */
function rasterUrl(rows, northFirst, colour) {
  const nr = rows.length, nc = nr ? rows[0].length : 0;
  if (!nr || !nc) return null;
  const small = document.createElement("canvas"); small.width = nc; small.height = nr;
  const sx = small.getContext("2d"); const img = sx.createImageData(nc, nr);
  for (let r = 0; r < nr; r++) {
    const src = rows[northFirst ? r : nr - 1 - r];
    for (let c = 0; c < nc; c++) {
      const v = src[c]; const o = (r * nc + c) * 4;
      if (v === null || v === undefined || !Number.isFinite(v)) { img.data[o + 3] = 0; continue; }
      const [R, G, B, A] = colour(v);
      img.data[o] = R; img.data[o + 1] = G; img.data[o + 2] = B; img.data[o + 3] = A;
    }
  }
  sx.putImageData(img, 0, 0);
  const k = Math.max(1, Math.min(16, Math.floor(1024 / Math.max(nr, nc))));
  const big = document.createElement("canvas"); big.width = nc * k; big.height = nr * k;
  const bx = big.getContext("2d"); bx.imageSmoothingEnabled = true; bx.imageSmoothingQuality = "high";
  bx.drawImage(small, 0, 0, big.width, big.height);
  return big.toDataURL("image/png");
}

const ghiColour = (v) => lerpStops(GHI_STOPS, v);
const cloudColour = (v) => { const g = 200 + 55 * (v / 100); return [g, g, g + 4, Math.round(Math.min(1, v / 100) * 200)]; };
const satColour = (v) => [222, 236, 255, Math.round(Math.max(0, Math.min(1, v)) * 215)];

/* --------------------------------------------------------------------- view */

export function create(root) {
  const ML = /** @type {any} */ (window).maplibregl;
  let model = prefs.get("map.model", "aifs");
  if (!MODELS.some((m) => m.id === model)) model = "aifs";
  const layerOn = {};
  for (const L of LAYERS) layerOn[L.id] = prefs.get("map2.layer." + L.id, L.on ? "1" : "0") === "1";

  /** @type {any} */ let grid = null;
  let gridErr = "", gridLoading = false, gridAt = 0, gridKey = "";
  /** @type {any} */ let sat = null;
  let satErr = "", satLoading = false, satAt = 0;
  let tIdx = 0, playing = false, playTimer = null;
  let inspectorOpen = prefs.get("map.inspector", "0") === "1";
  let loaded = false, styleFallback = false, destroyed = false, fitted = false, lastSig = "";
  /** @type {any} */ let map = null; let resizeObs = null; let popup = null; let popupLngLat = null;

  /* ---- DOM */
  const mapDiv = h("div", { class: "mapfull-canvas" });
  const notice = h("div", { class: "map-toast", hidden: true });
  const hud = h("div", { class: "map-hud" });
  const pills = h("div", { class: "map-pills", role: "group", "aria-label": "Kartlager" });
  const modelSw = h("div", { class: "map-seg", role: "group", "aria-label": "Prognosmodell" });
  const legend = h("div", { class: "map-legend" });
  const timeLabel = h("div", { class: "map-time-label" });
  const slider = h("input", { type: "range", min: "0", max: "0", step: "1", value: "0", class: "map-slider", "aria-label": "Prognostid" });
  const playBtn = h("button", { class: "map-play", "aria-label": "Spela upp", onclick: () => setPlaying(!playing) }, "▶");
  const inspectorBody = h("div", { class: "map-inspector-body" });
  const inspector = h("aside", { class: "map-inspector", "aria-label": "Inspector" },
    h("div", { class: "map-inspector-head" }, h("b", null, "Inspector"),
      h("button", { class: "map-x", "aria-label": "Stäng", onclick: () => setInspector(false) }, "×")),
    inspectorBody);
  const inspBtn = h("button", { class: "map-pill ghost", onclick: () => setInspector(!inspectorOpen) }, "Inspector");
  const fitBtn = h("button", { class: "map-pill ghost", onclick: () => fitRoute(true) }, "Hela rutten");

  root.append(h("div", { class: "mapfull" }, mapDiv,
    h("div", { class: "map-top" }, pills, h("div", { class: "map-top-right" }, modelSw, fitBtn, inspBtn)),
    hud, legend, notice, inspector,
    h("div", { class: "map-timebar" }, playBtn, h("div", { class: "map-time-main" }, timeLabel, slider))));

  slider.addEventListener("input", () => { setPlaying(false); setTime(Number(slider.value)); });

  /* ---- map setup */
  if (!ML) {
    mapDiv.append(h("div", { class: "state error" }, h("h3", null, "Kartbiblioteket laddades inte"), "MapLibre GL JS 4.7.1 saknas i vendor/."));
  } else {
    map = new ML.Map({ container: mapDiv, style: darkStyleUrl(), center: [0, 0], zoom: 1, attributionControl: { compact: true }, hash: false });
    // [0,0] zoom 1 is a world view, not a position; the view fits the route once it loads.
    /** @type {any} */ (window).__rwiMap = map;
    resizeObs = new ResizeObserver(() => map.resize());
    resizeObs.observe(mapDiv);
    map.addControl(new ML.NavigationControl({ visualizePitch: false }), "bottom-right");
    map.addControl(new ML.ScaleControl({ unit: "metric" }), "bottom-left");
    map.on("style.load", () => { loaded = true; clearTimeout(styleTimer); setupOverlays(); paintAll(); });
    map.on("error", (e) => {
      const msg = e?.error?.message || String(e?.error || e);
      if (!loaded && !styleFallback) fallbackStyle(msg);
    });
    map.on("moveend", () => { if (layerOn.sat) maybeLoadSat(); });
    map.on("mousemove", () => { if (!activePoint()) paintHud(); });
    const canvas = map.getCanvas();
    canvas?.setAttribute("tabindex", "0");
    canvas?.setAttribute("aria-label", "Väderkarta. Piltangenter panorerar, plus och minus zoomar.");
    var styleTimer = setTimeout(() => { if (!loaded && !styleFallback) fallbackStyle("stilen laddades inte inom 25 s"); }, 25000);
  }

  function darkStyleUrl() {
    if (cfg.map.darkStyleUrl) return cfg.map.darkStyleUrl;
    if (/openfreemap\.org/.test(cfg.map.styleUrl || "")) return "https://tiles.openfreemap.org/styles/dark";
    return mapStyleUrl();
  }

  function fallbackStyle(reason) {
    styleFallback = true;
    toast(`Bakgrundskarta otillgänglig (${reason}). Lagren ritas på mörk bakgrund utan geografi.`, "err");
    map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#0b0f14" } }] });
  }

  function toast(text, cls = "warn") {
    notice.hidden = !text; notice.className = `map-toast ${cls}`; notice.textContent = text || "";
  }

  /* ---- overlays */
  function firstSymbolId() {
    return (map.getStyle().layers || []).find((l) => l.type === "symbol")?.id;
  }

  function setupOverlays() {
    if (map.getSource("route")) return;
    const before = firstSymbolId();
    const blank = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";
    const corners = [[0, 1], [1, 1], [1, 0], [0, 0]];
    for (const id of ["sat", "ghi", "cloud"]) {
      map.addSource("img-" + id, { type: "image", url: blank, coordinates: corners });
    }
    map.addLayer({ id: "ghi-raster", type: "raster", source: "img-ghi", paint: { "raster-opacity": 0.75, "raster-fade-duration": 0, "raster-resampling": "linear" } }, before);
    map.addLayer({ id: "cloud-raster", type: "raster", source: "img-cloud", paint: { "raster-opacity": 0.9, "raster-fade-duration": 0 } }, before);
    map.addLayer({ id: "sat-raster", type: "raster", source: "img-sat", paint: { "raster-opacity": 0.9, "raster-fade-duration": 0 } }, before);

    map.addSource("route", { type: "geojson", data: empty() });
    map.addSource("stops", { type: "geojson", data: empty() });
    map.addSource("wind", { type: "geojson", data: empty() });
    map.addSource("vehicle", { type: "geojson", data: empty() });
    map.addLayer({ id: "route-glow", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#4da3ff", "line-width": 12, "line-opacity": 0.22, "line-blur": 6 } });
    map.addLayer({ id: "route-line", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#8fd0ff", "line-width": 3 } });
    map.addLayer({ id: "route-hit", type: "line", source: "route", paint: { "line-color": "#000", "line-width": 18, "line-opacity": 0 } });
    ensureArrow();
    map.addLayer({ id: "wind-arrows", type: "symbol", source: "wind", layout: {
      "icon-image": "wind-arrow", "icon-rotate": ["get", "to"], "icon-rotation-alignment": "map",
      "icon-size": ["interpolate", ["linear"], ["get", "spd"], 0, 0.55, 40, 0.95], "icon-allow-overlap": true, "icon-ignore-placement": true,
    }, paint: { "icon-color": ["interpolate", ["linear"], ["get", "spd"], ...WIND_STOPS.flat()], "icon-opacity": 0.95 } });
    map.addLayer({ id: "stops-circle", type: "circle", source: "stops", paint: { "circle-radius": 5, "circle-color": "#0b0f14", "circle-stroke-color": "#ffd166", "circle-stroke-width": 2.5 } });
    if (map.getStyle().glyphs) {
      map.addLayer({ id: "stops-label", type: "symbol", source: "stops", layout: { "text-field": ["get", "name"], "text-size": 11, "text-offset": [0, 1.2], "text-anchor": "top", "text-font": ["Noto Sans Regular"], "text-optional": true },
        paint: { "text-color": "#ffe6a8", "text-halo-color": "#0b0f14", "text-halo-width": 1.5 } });
    }
    map.addLayer({ id: "vehicle-halo", type: "circle", source: "vehicle", paint: { "circle-radius": 13, "circle-color": "#4da3ff", "circle-opacity": 0.25 } });
    map.addLayer({ id: "vehicle-dot", type: "circle", source: "vehicle", paint: { "circle-radius": 6, "circle-color": "#ffffff", "circle-stroke-color": "#4da3ff", "circle-stroke-width": 3 } });

    map.on("click", "route-hit", (e) => openPopup(e.lngLat));
    map.on("mouseenter", "route-hit", () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", "route-hit", () => { map.getCanvas().style.cursor = ""; });
  }

  function ensureArrow() {
    if (map.hasImage("wind-arrow")) return;
    const S = 48, c = document.createElement("canvas"); c.width = S; c.height = S;
    const x = c.getContext("2d"); x.fillStyle = "#fff"; x.strokeStyle = "#fff"; x.lineWidth = 4; x.lineCap = "round";
    x.beginPath(); x.moveTo(S / 2, S - 6); x.lineTo(S / 2, 14); x.stroke();
    x.beginPath(); x.moveTo(S / 2, 4); x.lineTo(S / 2 + 10, 18); x.lineTo(S / 2 - 10, 18); x.closePath(); x.fill();
    map.addImage("wind-arrow", x.getImageData(0, 0, S, S), { pixelRatio: 2, sdf: true });
  }

  function vis(id, on) { if (map?.getLayer(id)) map.setLayoutProperty(id, "visibility", on ? "visible" : "none"); }

  /* ---- data */
  async function loadGrid(force = false) {
    const routeId = displayRoute()?.id || "";
    const key = `${model}|${routeId}`;
    if (gridLoading || (!force && key === gridKey && nowMs() - gridAt < GRID_REFRESH_MS)) return;
    const keepTime = grid?.valid_times_utc?.[tIdx];
    gridLoading = true; gridKey = key; paintInspector(); paintTime();
    try {
      const g = await api.mapGrid({ model, routeId: routeId || undefined });
      if (destroyed || gridKey !== key) return;
      grid = g; gridErr = g.available ? "" : (g.message || "modellen är inte tillgänglig");
    } catch (e) {
      if (destroyed || gridKey !== key) return;
      grid = null; gridErr = e?.message || String(e);
    } finally {
      gridLoading = false; gridAt = nowMs();
    }
    tIdx = pickTime(keepTime);
    paintAll();
  }

  /** Index of the given valid time, else the time nearest to now. */
  function pickTime(prefer) {
    const ts = grid?.valid_times_utc || [];
    if (!ts.length) return 0;
    if (prefer) { const i = ts.indexOf(prefer); if (i >= 0) return i; }
    const now = nowMs(); let best = 0, bd = Infinity;
    ts.forEach((t, i) => { const d = Math.abs(Date.parse(t) - now); if (d < bd) { bd = d; best = i; } });
    return best;
  }

  function satCentre() {
    const p = activePoint();
    if (p) return { lat: p.lat, lon: p.lon, at: "bilen" };
    if (map && fitted) { const c = map.getCenter(); return { lat: c.lat, lon: c.lng, at: "kartans mitt" }; }
    return null;
  }

  let satCentreUsed = null;
  async function maybeLoadSat(force = false) {
    const c = satCentre();
    if (!c || satLoading) return;
    const moved = !satCentreUsed || Math.hypot(c.lat - satCentreUsed.lat, c.lon - satCentreUsed.lon) > 3;
    if (!force && !moved && nowMs() - satAt < SAT_REFRESH_MS) return;
    satLoading = true; paintInspector();
    try {
      sat = await api.satelliteField({ lat: c.lat, lon: c.lon, regionKm: 1500, cells: 96 });
      sat._at = c.at; satErr = "";
    } catch (e) {
      sat = null; satErr = e?.message || String(e);
    } finally {
      satLoading = false; satAt = nowMs(); satCentreUsed = c;
    }
    if (!destroyed) { paintSat(); paintInspector(); }
  }

  /* ---- painting */
  function paintAll() {
    paintPills(); paintModel(); paintRoute(); paintGrid(); paintSat(); paintTime(); paintHud(); paintLegend(); paintInspector(); refreshPopup();
  }

  function paintPills() {
    replace(pills, ...LAYERS.map((L) => h("button", {
      class: "map-pill" + (layerOn[L.id] ? " on" : ""), "aria-pressed": String(layerOn[L.id]),
      onclick: () => {
        layerOn[L.id] = !layerOn[L.id]; prefs.set("map2.layer." + L.id, layerOn[L.id] ? "1" : "0");
        if (L.id === "sat" && layerOn.sat) maybeLoadSat();
        paintPills(); paintGrid(); paintSat(); paintLegend();
      },
    }, L.label)));
  }

  function paintModel() {
    replace(modelSw, ...MODELS.map((m) => h("button", {
      class: "map-seg-btn" + (m.id === model ? " on" : ""), "aria-pressed": String(m.id === model),
      onclick: () => { if (m.id === model) return; model = m.id; prefs.set("map.model", model); grid = null; gridErr = ""; paintAll(); loadGrid(true); },
    }, m.label)));
  }

  function paintRoute() {
    if (!loaded) return;
    const gj = res("routeGeojson").data;
    const lines = (gj?.features || []).filter((f) => f.geometry && /LineString/.test(f.geometry.type));
    map.getSource("route")?.setData({ type: "FeatureCollection", features: lines });
    const stops = controlStops().stops.map((s) => ({ type: "Feature", properties: { name: s.name || "" }, geometry: { type: "Point", coordinates: [s.point.longitude_deg, s.point.latitude_deg] } }));
    map.getSource("stops")?.setData({ type: "FeatureCollection", features: stops });
    const p = activePoint();
    map.getSource("vehicle")?.setData(p ? { type: "FeatureCollection", features: [{ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [p.lon, p.lat] } }] } : empty());
    fitRoute(false);
  }

  function fitRoute(force) {
    if (!map || !loaded || (fitted && !force)) return;
    const gj = res("routeGeojson").data;
    const coords = [];
    for (const f of gj?.features || []) {
      const g = f.geometry; if (!g) continue;
      if (g.type === "LineString") coords.push(...g.coordinates);
      else if (g.type === "MultiLineString") for (const l of g.coordinates) coords.push(...l);
    }
    if (!coords.length) return;
    const b = coords.reduce((acc, c) => [Math.min(acc[0], c[0]), Math.min(acc[1], c[1]), Math.max(acc[2], c[0]), Math.max(acc[3], c[1])], [Infinity, Infinity, -Infinity, -Infinity]);
    map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: { top: 70, bottom: 110, left: 40, right: inspectorOpen ? 360 : 40 }, duration: force ? 600 : 0 });
    fitted = true;
    if (layerOn.sat) maybeLoadSat();
  }

  function paintGrid() {
    if (!loaded) return;
    const ok = grid?.available && grid.lats?.length && grid.lons?.length;
    for (const [id, field, colour] of [["ghi", "ghi", ghiColour], ["cloud", "cloud_cover", cloudColour]]) {
      const rows = ok ? grid.fields[field]?.[tIdx] : null;
      const url = rows && layerOn[id] ? rasterUrl(rows, false, colour) : null;
      if (url) map.getSource("img-" + id).updateImage({ url, coordinates: gridCorners(grid) });
      vis(id + "-raster", Boolean(url));
    }
    const feats = [];
    if (ok && layerOn.wind) {
      const spd = grid.fields.wind_speed_kmh?.[tIdx], dir = grid.fields.wind_direction_deg?.[tIdx];
      grid.lats.forEach((lat, i) => grid.lons.forEach((lon, j) => {
        const s = spd?.[i]?.[j], d = dir?.[i]?.[j];
        if (s === null || s === undefined || d === null || d === undefined) return;
        feats.push({ type: "Feature", properties: { spd: s, to: (d + 180) % 360 }, geometry: { type: "Point", coordinates: [lon, lat] } });
      }));
    }
    map.getSource("wind")?.setData({ type: "FeatureCollection", features: feats });
    vis("wind-arrows", layerOn.wind);
  }

  function paintSat() {
    if (!loaded) return;
    const ok = layerOn.sat && sat?.usable && sat.cloud_index?.length;
    if (ok) {
      const [la0, lo0, la1, lo1] = sat.bounds;
      const url = rasterUrl(sat.cloud_index, true, satColour);
      if (url) map.getSource("img-sat").updateImage({ url, coordinates: [[lo0, la1], [lo1, la1], [lo1, la0], [lo0, la0]] });
    }
    vis("sat-raster", Boolean(ok));
  }

  function timeText(t) {
    if (!t) return DASH;
    const { tz } = vehicleTimezone();
    const local = tz ? ` · ${fmtLocal(t, tz, { timeOnly: false })} lokal` : "";
    const lead = Math.round((Date.parse(t) - nowMs()) / 3600000);
    const rel = lead === 0 ? "nu" : lead > 0 ? `+${lead} h` : `${lead} h`;
    return `${fmtUtc(t)}${local} (${rel})`;
  }

  function paintTime() {
    const ts = grid?.available ? grid.valid_times_utc : [];
    slider.max = String(Math.max(0, ts.length - 1));
    slider.value = String(tIdx);
    slider.disabled = ts.length < 2;
    playBtn.disabled = ts.length < 2;
    const label = gridLoading ? "Hämtar prognos…" : ts.length ? timeText(ts[tIdx]) : gridErr ? "Ingen prognos" : DASH;
    const step = grid?.provenance?.temporal_step_h;
    replace(timeLabel, h("b", null, label), ts.length ? h("span", { class: "dim" }, ` · ${tIdx + 1}/${ts.length}${step ? ` · steg ${step} h` : ""}`) : null);
  }

  function setTime(i) {
    const n = grid?.valid_times_utc?.length || 0;
    if (!n) return;
    tIdx = ((i % n) + n) % n;
    paintGrid(); paintTime(); paintHud(); refreshPopup();
  }

  function setPlaying(on) {
    playing = on && (grid?.valid_times_utc?.length || 0) > 1;
    clearInterval(playTimer); playTimer = null;
    if (playing) playTimer = setInterval(() => setTime(tIdx + 1), PLAY_MS);
    playBtn.textContent = playing ? "❚❚" : "▶";
    playBtn.setAttribute("aria-label", playing ? "Pausa" : "Spela upp");
  }

  function setInspector(open) {
    inspectorOpen = open; prefs.set("map.inspector", open ? "1" : "0");
    inspector.classList.toggle("open", open); inspBtn.classList.toggle("on", open);
  }

  /** The four values at a point for the current time, formatted. */
  function valuesAt(lat, lon) {
    const g = grid?.available ? grid : null;
    const v = (n) => (g ? sampleGrid(g, n, tIdx, lat, lon) : null);
    const ghi = v("ghi"), cc = v("cloud_cover"), ws = v("wind_speed_kmh"), wd = v("wind_direction_deg"), tc = v("temperature_c");
    return [
      ["Sol", ghi === null ? DASH : fmtNum(ghi, 0), "W/m²"],
      ["Moln", cc === null ? DASH : fmtNum(cc, 0), "%"],
      ["Vind", ws === null ? DASH : fmtNum(ws, 0), ws === null || wd === null ? "km/h" : `km/h från ${compass(wd)}`],
      ["Temp", tc === null ? DASH : fmtNum(tc, 0), "°C"],
    ];
  }

  function valueRows(rows) {
    return h("div", { class: "map-vals" }, ...rows.map(([k, v, u]) => h("div", { class: "map-val" }, h("span", { class: "k" }, k), h("b", null, v), h("span", { class: "u" }, u))));
  }

  function paintHud() {
    const p = activePoint();
    const where = p ? { lat: p.lat, lon: p.lon, at: "vid bilen" } : map && fitted ? { lat: map.getCenter().lat, lon: map.getCenter().lng, at: "kartans mitt (ingen position)" } : null;
    const modelLabel = MODELS.find((m) => m.id === model)?.label || model;
    const head = h("div", { class: "map-hud-head" }, h("b", null, modelLabel), h("span", { class: "dim" }, " prognos · " + (where ? where.at : "ingen punkt")));
    if (!grid?.available) {
      replace(hud, head, h("div", { class: "map-hud-msg" }, gridLoading ? "Hämtar…" : gridErr || DASH));
      return;
    }
    replace(hud, head, valueRows(where ? valuesAt(where.lat, where.lon) : valuesAt(NaN, NaN)),
      h("div", { class: "map-hud-foot dim" }, "Sol = modellerad GHI, inte uppmätt"));
  }

  function openPopup(lngLat) {
    if (!map) return;
    popupLngLat = lngLat;
    popup?.remove();
    popup = new ML.Popup({ closeButton: true, className: "map-popup", maxWidth: "260px" }).setLngLat(lngLat).setDOMContent(popupBody()).addTo(map);
    popup.on("close", () => { popup = null; popupLngLat = null; });
  }

  function popupBody() {
    const t = grid?.valid_times_utc?.[tIdx];
    const modelLabel = MODELS.find((m) => m.id === model)?.label || model;
    return h("div", null, h("div", { class: "map-popup-head" }, h("b", null, modelLabel), h("span", { class: "dim" }, " · " + (t ? fmtUtc(t) : DASH))),
      valueRows(valuesAt(popupLngLat.lat, popupLngLat.lng)));
  }

  function refreshPopup() { if (popup && popupLngLat) popup.setDOMContent(popupBody()); }

  function paintLegend() {
    const parts = [];
    if (layerOn.ghi) parts.push(h("div", { class: "lg-row" }, h("span", null, "GHI"), h("i", { class: "ramp ghi" }), h("span", { class: "dim" }, "0–950 W/m²")));
    if (layerOn.cloud) parts.push(h("div", { class: "lg-row" }, h("span", null, "Moln"), h("i", { class: "ramp cloud" }), h("span", { class: "dim" }, "0–100 %")));
    if (layerOn.wind) parts.push(h("div", { class: "lg-row" }, h("span", null, "Vind"), h("i", { class: "ramp wind" }), h("span", { class: "dim" }, "0–60 km/h")));
    if (layerOn.sat) parts.push(h("div", { class: "lg-row" }, h("span", null, "Himawari"), h("i", { class: "ramp sat" }), h("span", { class: "dim" }, "klart–tjockt")));
    legend.hidden = !parts.length;
    replace(legend, ...parts);
  }

  function kv(rows) {
    return h("dl", { class: "map-kv" }, ...rows.filter(([, v]) => v !== null && v !== undefined && v !== "").flatMap(([k, v]) => [h("dt", null, k), h("dd", null, String(v))]));
  }

  function paintInspector() {
    const p = grid?.provenance;
    const blocks = [h("h4", null, "Prognos")];
    if (gridLoading) blocks.push(h("p", { class: "dim" }, "Hämtar…"));
    if (gridErr) blocks.push(h("p", { class: "warn-text" }, gridErr));
    if (p) {
      blocks.push(kv([
        ["Leverantör", p.provider], ["Dataset", p.dataset], ["Modell", `${p.model_name}${p.model_version ? " · " + p.model_version : ""}`],
        ["Typ", p.kind], ["Körning (init)", p.init_time_utc ? fmtUtc(p.init_time_utc) : "okänd"], ["Hämtad", p.retrieved_at_utc ? fmtUtc(p.retrieved_at_utc) : null],
        ["Rutnät", `${p.grid_step_deg}° · ${grid.lats.length}×${grid.lons.length} noder`], ["Tidssteg", p.temporal_step_h ? `${p.temporal_step_h} h` : null],
        ["Licens", p.license], ["Källa", p.source_url],
      ]));
      if (p.notes?.length) blocks.push(h("ul", { class: "map-notes" }, ...p.notes.map((n) => h("li", null, n))));
    }
    blocks.push(h("h4", null, "Himawari-9"));
    if (!layerOn.sat) blocks.push(h("p", { class: "dim" }, "Lagret är av."));
    else if (satLoading) blocks.push(h("p", { class: "dim" }, "Hämtar senaste bilden…"));
    else if (satErr) blocks.push(h("p", { class: "warn-text" }, satErr));
    else if (!satCentre()) blocks.push(h("p", { class: "dim" }, "Väntar på rutt eller position."));
    if (layerOn.sat && sat) {
      blocks.push(kv([
        ["Satellit", `${sat.satellite} · ${sat.band}`], ["Bild", fmtUtc(sat.slot_time_utc)], ["Ålder", `${Math.round(sat.image_age_s / 60)} min`],
        ["Centrerad på", sat._at], ["Typ", "satellithärledning (molnindex), inte mätning"], ["Attribution", sat.attribution],
      ]));
      if (!sat.usable) blocks.push(h("p", { class: "warn-text" }, `Inte användbar: ${sat.unusable_reason || "okänd orsak"}`));
      blocks.push(h("p", { class: "dim" }, "Visar alltid senaste bilden, oberoende av tidsreglaget."));
    }
    blocks.push(h("h4", null, "Bakgrundskarta"), h("p", { class: "dim" }, styleFallback ? "Ej tillgänglig — mörk bakgrund utan geografi." : cfg.map.providerName || DASH));
    replace(inspectorBody, ...blocks);
  }

  setInspector(inspectorOpen);
  paintAll();

  return {
    update() {
      loadRoutes(); loadRouteGeojson();
      loadGrid();
      if (layerOn.sat) maybeLoadSat();
      const sig = signature(KEYS, model);
      if (sig === lastSig) return;
      lastSig = sig;
      paintRoute(); paintHud(); paintInspector();
    },
    destroy() {
      destroyed = true; clearTimeout(styleTimer); clearInterval(playTimer);
      resizeObs?.disconnect(); popup?.remove(); if (map) map.remove();
    },
  };
}
