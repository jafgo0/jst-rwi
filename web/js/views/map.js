/**
 * View B - Live Weather Map (MapLibre GL JS).
 * Layers: route line, control stops, route weather samples (coloured by a
 * chosen forecast variable), wind barbs, vehicle marker with GPS accuracy circle.
 * The vehicle layer source is EMPTY whenever the API reports no fix.
 * @module views/map
 */
import { h, replace } from "../dom.js";
import { cfg, mapStyleUrl, prefs } from "../settings.js";
import {
  DASH, fmtLatLon, fmtNum, fmtUtc, fmtLocal, fmtHeadTail, trueHeadwind, compass, mpsToKnots, VARS, fmtDuration, fixFreshness,
} from "../format.js";
import { panel, valueCell, numCell, kindChip, chip, modeChip, positionUnknown, stateBox, ageSpan } from "../ui.js";
import { res, activeState, activePoint, vehicleTimezone, displayRoute, effectiveTimeMs, signature, session, controlStops } from "../store.js";
import { loadRoutes, loadRouteGeojson, loadTrajectory, loadForecast, nearestStep, startDistance as sd } from "../loaders.js";

const KEYS = ["state", "routes", "routeGeojson", "routeWeather", "forecastNow"];

/** Colour ramps for route-sample circles (display units). */
const RAMPS = {
  cloud_cover: { label: "Cloud cover", unit: "%", stops: [[0, "#ffe58a"], [35, "#c8d3df"], [70, "#8497ab"], [100, "#44566b"]] },
  ghi: { label: "GHI (forecast)", unit: "W/m²", stops: [[0, "#2b3a55"], [250, "#4f6fa8"], [600, "#f2c14e"], [1000, "#ff7a1a"]] },
  temperature_2m: { label: "Air temperature 2 m", unit: "°C", stops: [[0, "#3b6fd8"], [15, "#5ec2b7"], [28, "#f2c14e"], [42, "#d6412b"]] },
  wind_speed_10m: { label: "Wind speed 10 m", unit: "m/s", stops: [[0, "#dfe9f3"], [5, "#8fc1e8"], [10, "#3f86d4"], [18, "#2a2a8a"]] },
};

const LAYERS = [
  { id: "route", label: "Route line", on: true, layers: ["route-casing", "route-line"] },
  { id: "stops", label: "Control stops", on: true, layers: ["stops-circle", "stops-label"] },
  { id: "samples", label: "Route weather samples (forecast at ETA)", on: true, layers: ["samples-circle"] },
  { id: "barbs", label: "Wind barbs (forecast at ETA)", on: true, layers: ["barbs"] },
  { id: "vehicle", label: "Vehicle and GPS accuracy", on: true, layers: ["accuracy-fill", "accuracy-line", "vehicle-ring", "vehicle-circle", "vehicle-heading"] },
];

export function create(root) {
  const mapDiv = h("div", { class: "mapbox", style: { flex: "1 1 auto", minHeight: "520px", borderRadius: "4px", border: "1px solid var(--border)" } });
  const notice = h("div", { class: "map-notice" });
  const readout = h("div"), legend = h("div"), status = h("div"), layerBox = h("div");
  const toggles = {};
  let colorBy = prefs.get("map.colorBy", "cloud_cover");
  if (!RAMPS[colorBy]) colorBy = "cloud_cover";
  const layerOn = {};
  for (const L of LAYERS) layerOn[L.id] = prefs.get("map.layer." + L.id, L.on ? "1" : "0") === "1";

  const side = h("div", { class: "mapside" }, panel("Layers", layerBox), panel("Hover readout", readout), panel("Legend", legend), panel("Map and data status", status));
  root.append(h("div", { class: "view-head" }, h("h1", null, "Live Weather Map"),
    h("span", { class: "sub" }, "Vehicle, route and forecast along the route"),
    h("div", { class: "right" },
      h("button", { class: "btn", id: "fitRoute", onclick: () => fitRoute() }, "Fit route"),
      h("button", { class: "btn", id: "toVehicle", onclick: () => toVehicle() }, "Centre on vehicle"))),
    h("div", { class: "mapgrid" }, h("div", { style: { display: "flex", flexDirection: "column", gap: "6px", minWidth: 0 } }, mapDiv, notice), side));

  /** @type {any} */ let map = null;
  let resizeObs = null;
  let loaded = false, fitted = false, fittedRoute = false, destroyed = false, styleFallback = false, tileErrors = 0;
  let samples = []; // current sample list for hover lookups
  let lastSig = "";
  let pinned = false; // true after a tap/Enter pins the readout; mouseout no longer clears

  /* ---------------------------------------------------------------- map setup */
  const ML = /** @type {any} */ (window).maplibregl;
  if (!ML) {
    mapDiv.append(h("div", { class: "state error" }, h("h3", null, "Map library not loaded"), "MapLibre GL JS 4.7.1 could not be loaded from vendor/maplibre-gl-4.7.1/. The rest of the dashboard still works."));
  } else {
    map = new ML.Map({ container: mapDiv, style: mapStyleUrl(), center: [0, 0], zoom: 1, attributionControl: { compact: true }, hash: false });
    // The initial [0,0] zoom-1 view is only a world view, not a position or default location.
    /** @type {any} */ (window).__rwiMap = map; // debugging handle
    // The container gets its final size after mount; without this the canvas stays at its first size and draws nothing.
    resizeObs = new ResizeObserver(() => map.resize());
    resizeObs.observe(mapDiv);
    map.addControl(new ML.NavigationControl({ visualizePitch: false }), "top-right");
    map.addControl(new ML.ScaleControl({ unit: "metric" }), "bottom-left");
    // Overlays only need the STYLE, not every tile, so use style.load (the "load" event waits for all tiles).
    map.on("style.load", () => { loaded = true; clearTimeout(styleTimer); setupOverlays(); refresh(true); });
    map.on("error", (e) => {
      const msg = e?.error?.message || String(e?.error || e);
      if (!loaded && !styleFallback) { fallbackStyle(msg); return; }
      tileErrors++;
      notice.textContent = `Basemap: ${tileErrors} tile/resource error(s); last: ${msg}`;
      notice.className = "map-notice warn";
    });
    map.on("mousemove", onHover);
    map.on("mouseout", () => { if (!pinned) paintReadout(null); });
    // Touch devices have no hover: a tap pins the readout to the tapped
    // point. A second tap on empty map clears it. Keyboard users get the
    // same behaviour via Enter/Space on the focused map canvas.
    map.on("click", (e) => { pinned = true; onHover(e); });
    const canvas = map.getCanvas();
    if (canvas) {
      canvas.setAttribute("tabindex", "0");
      canvas.setAttribute("aria-label",
        "Race weather map. Arrow keys pan, plus and minus zoom. Press Enter to pin a readout at the map centre, Escape to clear.");
      canvas.addEventListener("keydown", (ke) => {
        if (ke.key === "Enter" || ke.key === " ") {
          ke.preventDefault();
          const c = map.getCenter();
          pinned = true;
          onHover({ point: map.project(c), lngLat: c });
        } else if (ke.key === "Escape") {
          pinned = false; paintReadout(null);
        }
      });
    }
    var styleTimer = setTimeout(() => { if (!loaded && !styleFallback) fallbackStyle("style did not load within 25 s"); }, 25000);
  }

  function fallbackStyle(reason) {
    styleFallback = true;
    notice.className = "map-notice err";
    notice.textContent = `BASEMAP UNAVAILABLE (${reason}). Overlays are drawn on a plain background; geography is NOT shown.`;
    map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#c9d3de" } }] });
  }

  /* ---------------------------------------------------------------- overlays */
  const empty = () => ({ type: "FeatureCollection", features: [] });

  function setupOverlays() {
    if (map.getSource("route")) return;
    map.addSource("route", { type: "geojson", data: empty() });
    map.addSource("stops", { type: "geojson", data: empty() });
    map.addSource("samples", { type: "geojson", data: empty() });
    map.addSource("vehicle", { type: "geojson", data: empty() });
    map.addSource("accuracy", { type: "geojson", data: empty() });
    map.addLayer({ id: "route-casing", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#ffffff", "line-width": 7, "line-opacity": 0.85 } });
    map.addLayer({ id: "route-line", type: "line", source: "route", layout: { "line-cap": "round", "line-join": "round" }, paint: { "line-color": "#0b5fd0", "line-width": 3.5 } });
    map.addLayer({ id: "samples-circle", type: "circle", source: "samples", paint: {
      "circle-radius": 6, "circle-stroke-width": 1.5, "circle-stroke-color": ["case", ["get", "hasV"], "#10263f", "#7a1f1f"],
      "circle-color": sampleColor(),
    } });
    map.addLayer({ id: "stops-circle", type: "circle", source: "stops", paint: { "circle-radius": 6, "circle-color": "#ffffff", "circle-stroke-color": "#7a1fa2", "circle-stroke-width": 3 } });
    if (map.getStyle().glyphs) {
      map.addLayer({ id: "stops-label", type: "symbol", source: "stops", layout: { "text-field": ["get", "name"], "text-size": 11, "text-offset": [0, 1.3], "text-anchor": "top", "text-font": ["Noto Sans Bold"], "text-optional": true },
        paint: { "text-color": "#3b0f52", "text-halo-color": "#ffffff", "text-halo-width": 1.5 } });
    }
    map.addLayer({ id: "barbs", type: "symbol", source: "samples", filter: ["has", "barb"], layout: { "icon-image": ["get", "barb"], "icon-rotate": ["get", "from"], "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-ignore-placement": true, "icon-size": 1.35 } });
    map.addLayer({ id: "accuracy-fill", type: "fill", source: "accuracy", paint: { "fill-color": ["get", "color"], "fill-opacity": 0.16 } });
    map.addLayer({ id: "accuracy-line", type: "line", source: "accuracy", paint: { "line-color": ["get", "color"], "line-width": 1.5, "line-dasharray": [2, 2] } });
    map.addLayer({ id: "vehicle-ring", type: "circle", source: "vehicle", paint: { "circle-radius": 12, "circle-color": "#ffffff", "circle-opacity": 0.9 } });
    map.addLayer({ id: "vehicle-circle", type: "circle", source: "vehicle", paint: { "circle-radius": 8, "circle-color": ["get", "color"], "circle-stroke-width": 2, "circle-stroke-color": "#10263f" } });
    ensureArrowImage();
    map.addLayer({ id: "vehicle-heading", type: "symbol", source: "vehicle", filter: ["has", "heading"], layout: { "icon-image": "veh-arrow", "icon-rotate": ["get", "heading"], "icon-rotation-alignment": "map", "icon-allow-overlap": true, "icon-offset": [0, -14], "icon-size": 0.9 } });
    applyVisibility();
  }

  function sampleColor() {
    const r = RAMPS[colorBy];
    return ["case", ["get", "hasV"], ["interpolate", ["linear"], ["get", "v"], ...r.stops.flat()], "#b0b7c0"];
  }

  function applyVisibility() {
    if (!loaded) return;
    for (const L of LAYERS) for (const id of L.layers) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", layerOn[L.id] ? "visible" : "none");
  }

  function ensureArrowImage() {
    if (map.hasImage("veh-arrow")) return;
    const c = document.createElement("canvas"); c.width = 28; c.height = 28; const x = c.getContext("2d");
    x.fillStyle = "#10263f"; x.strokeStyle = "#ffffff"; x.lineWidth = 2; x.beginPath(); x.moveTo(14, 2); x.lineTo(24, 24); x.lineTo(14, 18); x.lineTo(4, 24); x.closePath(); x.stroke(); x.fill();
    map.addImage("veh-arrow", x.getImageData(0, 0, 28, 28), { pixelRatio: 2 });
  }

  /** Wind barb icon for a speed bucket (units of 5 kt). Staff points toward the "from" bearing (up = from north). */
  function ensureBarb(n5) {
    const id = "barb-" + n5;
    if (map.hasImage(id)) return id;
    const S = 96, cx = S / 2, cy = S / 2;
    const c = document.createElement("canvas"); c.width = S; c.height = S; const x = c.getContext("2d");
    x.strokeStyle = "#10263f"; x.fillStyle = "#10263f"; x.lineWidth = 2.5; x.lineCap = "round";
    if (n5 === 0) { x.beginPath(); x.arc(cx, cy, 9, 0, Math.PI * 2); x.stroke(); }
    else {
      const len = 40; let y = cy - len;
      x.beginPath(); x.moveTo(cx, cy); x.lineTo(cx, y); x.stroke();
      let rem = n5; const flags = Math.floor(rem / 10); rem -= flags * 10; const fulls = Math.floor(rem / 2); const half = rem % 2;
      let pos = y;
      for (let i = 0; i < flags; i++) { x.beginPath(); x.moveTo(cx, pos); x.lineTo(cx + 15, pos - 3); x.lineTo(cx, pos + 8); x.closePath(); x.fill(); pos += 9; }
      if (flags) pos += 2;
      for (let i = 0; i < fulls; i++) { x.beginPath(); x.moveTo(cx, pos); x.lineTo(cx + 15, pos - 7); x.stroke(); pos += 6; }
      if (half) { if (!fulls && !flags) pos += 6; x.beginPath(); x.moveTo(cx, pos); x.lineTo(cx + 8, pos - 4); x.stroke(); }
    }
    map.addImage(id, x.getImageData(0, 0, S, S), { pixelRatio: 2 });
    return id;
  }

  /* ------------------------------------------------------------------ data */
  function circlePolygon(lat, lon, r, n = 72) {
    const coords = [];
    const dLat = r / 111320, dLon = r / (111320 * Math.cos((lat * Math.PI) / 180));
    for (let i = 0; i <= n; i++) { const a = (i / n) * 2 * Math.PI; coords.push([lon + dLon * Math.cos(a), lat + dLat * Math.sin(a)]); }
    return coords;
  }

  function modeColor(mode) { return mode === "live_gps" ? "#16a36a" : mode === "manual" ? "#2f7fe0" : "#e0216b"; }

  function refresh(force = false) {
    if (!loaded || destroyed) return;
    setupOverlays();
    const s = activeState();
    const dr = displayRoute();

    // route line
    const gj = res("routeGeojson").data;
    const lines = (gj?.features || []).filter((f) => f.geometry && /LineString/.test(f.geometry.type));
    map.getSource("route").setData({ type: "FeatureCollection", features: lines });

    // control stops from the route GeoJSON markers (or the route list when it carries them)
    const stops = controlStops().stops.filter((c) => c.point).map((c) => ({
      type: "Feature", geometry: { type: "Point", coordinates: [c.point.longitude_deg, c.point.latitude_deg] }, properties: { name: c.name || "Control stop", dist_km: c.cumulative_distance_m / 1000 },
    }));
    map.getSource("stops").setData({ type: "FeatureCollection", features: stops });

    // vehicle: EMPTY collection when position is unknown - no marker, no default
    const fix = s?.fix;
    if (fix?.point) {
      const col = modeColor(s.mode);
      const hd = fix.course_deg ?? s.heading_deg;
      const props = { color: col, mode: s.mode }; if (hd !== null && hd !== undefined) props.heading = hd;
      map.getSource("vehicle").setData({ type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: [fix.point.longitude_deg, fix.point.latitude_deg] }, properties: props }] });
      map.getSource("accuracy").setData(fix.accuracy_m ? { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Polygon", coordinates: [circlePolygon(fix.point.latitude_deg, fix.point.longitude_deg, fix.accuracy_m)] }, properties: { color: col } }] } : empty());
    } else {
      map.getSource("vehicle").setData(empty());
      map.getSource("accuracy").setData(empty());
    }

    // route weather samples
    const rw = res("routeWeather").data;
    samples = [];
    const feats = [];
    (rw?.samples || []).forEach((smp, i) => {
      const pt = smp.series_point || smp.trajectory_point?.point;
      if (!pt) return;
      const tp = smp.trajectory_point;
      const vm = smp.values?.[colorBy];
      const hasV = vm && vm.value !== null && vm.value !== undefined && Number.isFinite(vm.value);
      const ws = smp.values?.wind_speed_10m, wd = smp.values?.wind_direction_10m;
      const props = { idx: i, hasV: !!hasV, v: hasV ? (VARS[colorBy]?.scale ? vm.value * VARS[colorBy].scale : vm.value) : 0 };
      if (ws?.value !== null && ws?.value !== undefined && wd?.value !== null && wd?.value !== undefined) {
        props.barb = ensureBarb(Math.min(40, Math.round(mpsToKnots(ws.value) / 5))); props.from = wd.value;
      }
      samples.push({ smp, lon: pt.longitude_deg, lat: pt.latitude_deg });
      feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [pt.longitude_deg, pt.latitude_deg] }, properties: props });
    });
    map.getSource("samples").setData({ type: "FeatureCollection", features: feats });
    if (map.getLayer("samples-circle")) map.setPaintProperty("samples-circle", "circle-color", sampleColor());

    if (lines.length && !fittedRoute) { fittedRoute = true; fitted = true; fitRoute(); }
    else if (!fitted && fix?.point) { fitted = true; toVehicle(); }
    void force;
  }

  function routeBounds() {
    const lines = (res("routeGeojson").data?.features || []).filter((f) => f.geometry && /LineString/.test(f.geometry.type));
    let b = null;
    for (const f of lines) {
      const arrs = f.geometry.type === "LineString" ? [f.geometry.coordinates] : f.geometry.coordinates;
      for (const a of arrs) for (const [x, y] of a) { b = b ? [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)] : [x, y, x, y]; }
    }
    return b;
  }
  function fitRoute() { if (!map) return; const b = routeBounds(); if (b) map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 50, duration: 600 }); }
  function toVehicle() { const p = activePoint(); if (map && p) map.easeTo({ center: [p.lon, p.lat], zoom: Math.max(map.getZoom(), 8), duration: 600 }); }

  /* ----------------------------------------------------------------- hover */
  function onHover(e) {
    if (!loaded) return;
    let best = null, bd = 32;
    samples.forEach((s, i) => { const p = map.project([s.lon, s.lat]); const d = Math.hypot(p.x - e.point.x, p.y - e.point.y); if (d < bd) { bd = d; best = { type: "sample", i, s }; } });
    const v = activePoint();
    if (v) { const p = map.project([v.lon, v.lat]); const d = Math.hypot(p.x - e.point.x, p.y - e.point.y); if (d < Math.min(bd, 24)) best = { type: "vehicle" }; }
    paintReadout({ lngLat: e.lngLat, hit: best });
  }

  function paintReadout(h0) {
    if (!h0) { replace(readout, h("div", { class: "note" }, "Move the pointer over the map.")); return; }
    const { lngLat, hit } = h0;
    const rows = [h("div", { class: "num" }, "Cursor ", fmtLatLon(lngLat.lat, lngLat.lng, 3))];
    const { tz } = vehicleTimezone();
    if (hit?.type === "sample") {
      const smp = hit.s.smp, tp = smp.trajectory_point, v = smp.values || {};
      const rel = smp.relative_wind; const ht = fmtHeadTail(trueHeadwind(v.wind_speed_10m?.value, v.wind_direction_10m?.value, tp.heading_deg));
      rows.push(h("div", { style: { margin: "6px 0 2px" } }, kindChip("forecast"), " route sample at ", h("b", null, fmtNum(tp.distance_m / 1000, 1) + " km")));
      rows.push(h("dl", { class: "kv tight" },
        h("dt", null, "ETA local"), h("dd", null, fmtLocal(tp.eta_utc, tz)), h("dt", null, "ETA UTC"), h("dd", null, fmtUtc(tp.eta_utc)),
        h("dt", null, "Wind (from)"), h("dd", null, valueCell(v.wind_speed_10m, { variable: "wind_speed_10m" }), " ", valueCell(v.wind_direction_10m, { variable: "wind_direction_10m" }), v.wind_direction_10m?.value != null ? ` ${compass(v.wind_direction_10m.value)}` : ""),
        h("dt", null, "Wind (km/h)"), h("dd", null, v.wind_speed_10m?.value != null ? numCell(v.wind_speed_10m.value * 3.6, "km/h", { digits: 1, prov: v.wind_speed_10m.provenance }) : DASH),
        h("dt", null, "Head/tail (wind)"), h("dd", null, ht.text !== DASH ? [ht.label ? chip(ht.label, ht.label === "HEAD" ? "warn" : "ok") : "", " ", numCell(parseFloat(ht.text), "m/s", { digits: 1, prov: v.wind_speed_10m?.provenance }), rel ? ["  cross ", numCell(Math.abs(rel.crosswind_mps), "m/s", { digits: 1 })] : ""] : h("span", { class: "faint" }, DASH + " no wind/heading")),
        h("dt", null, "Temperature"), h("dd", null, valueCell(v.temperature_2m, { variable: "temperature_2m" })),
        h("dt", null, "Cloud"), h("dd", null, valueCell(v.cloud_cover, { variable: "cloud_cover" })),
        h("dt", null, "GHI"), h("dd", null, valueCell(v.ghi, { variable: "ghi" }))));
      if (smp.lead_time_s !== null && smp.lead_time_s !== undefined) rows.push(h("div", { class: "note" }, `Lead time ${fmtDuration(smp.lead_time_s)} (forecast issued before arrival)`));
    } else if (hit?.type === "vehicle") {
      const step = nearestStep(res("forecastNow").data, effectiveTimeMs());
      rows.push(h("div", { style: { margin: "6px 0 2px" } }, "Vehicle position - ", kindChip("forecast"), " at nearest step"));
      if (step) {
        const v = step.values;
        rows.push(h("dl", { class: "kv tight" }, h("dt", null, "Wind (from)"), h("dd", null, valueCell(v.wind_speed_10m, { variable: "wind_speed_10m" }), " ", valueCell(v.wind_direction_10m, { variable: "wind_direction_10m" }), v.wind_direction_10m?.value != null ? ` ${compass(v.wind_direction_10m.value)}` : ""),
          h("dt", null, "Valid"), h("dd", null, fmtUtc(step.valid_time_utc))));
      } else rows.push(h("div", { class: "note warn" }, "No forecast loaded for the vehicle position."));
    } else {
      rows.push(h("div", { class: "note" }, samples.length ? "No route weather sample within 32 px. Hover a coloured sample circle for forecast wind and weather at its ETA." : "No route weather samples are loaded, so there is no wind value to show for this point."));
    }
    replace(readout, rows);
  }

  /* ---------------------------------------------------------------- sidebar */
  function paintLayers() {
    const nodes = LAYERS.map((L) => {
      const cb = h("input", { type: "checkbox", checked: layerOn[L.id] ? "" : null, onchange: (e) => { layerOn[L.id] = e.target.checked; prefs.set("map.layer." + L.id, layerOn[L.id] ? "1" : "0"); applyVisibility(); paintLegend(); } });
      cb.checked = layerOn[L.id]; toggles[L.id] = cb;
      return h("label", { style: { display: "flex", gap: "6px", alignItems: "center", padding: "2px 0" } }, cb, L.label);
    });
    const sel = h("select", { class: "ctl", onchange: (e) => { colorBy = e.target.value; prefs.set("map.colorBy", colorBy); refresh(true); paintLegend(); } },
      Object.entries(RAMPS).map(([k, r]) => h("option", { value: k, selected: k === colorBy ? "" : null }, r.label)));
    replace(layerBox, nodes, h("div", { class: "field", style: { marginTop: "8px" } }, h("label", null, "Colour samples by"), sel));
  }

  function paintLegend() {
    const r = RAMPS[colorBy];
    const items = [];
    if (layerOn.route) items.push(h("div", { class: "lg" }, h("i", { class: "sw line" }), "Race route (GeoJSON from API)"));
    if (layerOn.stops) items.push(h("div", { class: "lg" }, h("i", { class: "sw stop" }), "Control stop"));
    if (layerOn.vehicle) {
      items.push(h("div", { class: "lg" }, h("i", { class: "sw dot", style: { background: "#16a36a" } }), "Vehicle: LIVE GPS"));
      items.push(h("div", { class: "lg" }, h("i", { class: "sw dot", style: { background: "#2f7fe0" } }), "Vehicle: MANUAL position"));
      items.push(h("div", { class: "lg" }, h("i", { class: "sw dot", style: { background: "#e0216b" } }), "Vehicle: SIMULATED position"));
      items.push(h("div", { class: "lg" }, h("i", { class: "sw ring" }), "Dashed circle = GPS accuracy (1σ, reported)"));
    }
    if (layerOn.samples) {
      const grad = `linear-gradient(90deg, ${r.stops.map(([v, c]) => `${c} ${(100 * (v - r.stops[0][0])) / (r.stops[r.stops.length - 1][0] - r.stops[0][0])}%`).join(", ")})`;
      items.push(h("div", { style: { marginTop: "6px" } }, h("b", null, r.label), " (", r.unit, ") ", kindChip("forecast")),
        h("div", { style: { height: "10px", background: grad, border: "1px solid var(--border-strong)", margin: "3px 0" } }),
        h("div", { style: { display: "flex", justifyContent: "space-between" }, class: "note" }, h("span", null, `${r.stops[0][0]} ${r.unit}`), h("span", null, `${r.stops[r.stops.length - 1][0]} ${r.unit}`)),
        h("div", { class: "lg" }, h("i", { class: "sw dot", style: { background: "#b0b7c0", borderColor: "#7a1f1f" } }), "Grey = value missing for this sample"));
    }
    if (layerOn.barbs) items.push(h("div", { class: "note", style: { marginTop: "6px" } }, "Wind barbs: staff points toward the direction the wind blows FROM; feathers on the clockwise side. Half feather = 5 kt, full = 10 kt, flag = 50 kt (knots by convention). Hover for m/s and km/h."));
    replace(legend, items);
  }

  function paintStatus() {
    const s = activeState(); const items = [];
    if (s) {
      items.push(h("div", null, modeChip(s.mode)));
      if (!s.fix) items.push(positionUnknown("No marker is drawn."));
      else {
        const fr = fixFreshness(s.fix, s.mode);
        items.push(h("div", { class: "num" }, fmtLatLon(s.fix.point.latitude_deg, s.fix.point.longitude_deg)));
        items.push(h("div", { class: fr.state === "stale" ? "is-stale" : "note" }, "Fix ", ageSpan(s.fix.gps_time_utc || s.fix.received_at_utc, fr.state === "na" ? {} : { limitS: fr.limitS, limitSource: fr.limitSource })));
      }
    } else if (res("state").status === "error") items.push(stateBox("error", "State unavailable", res("state").error?.message));
    const rw = res("routeWeather");
    if (rw.status === "blocked") items.push(h("div", { class: "note warn" }, "No route weather samples: " + rw.blockedReason + "."));
    else if (rw.status === "error") items.push(h("div", { class: "note warn" }, `Route weather error (${rw.endpoint}): ${rw.error?.message}`));
    else if (rw.data) {
      const hyp = sd().source === "operator";
      items.push(h("div", { class: "note" }, `${rw.data.samples.length} route samples; provider ${rw.data.provider}; assumed speed ${session.speedKmh} km/h; generated `, ageSpan(rw.data.generated_at_utc), hyp ? " - HYPOTHETICAL start distance entered by operator" : ""));
    }
    const rr = res("routeGeojson");
    if (rr.status === "error") items.push(h("div", { class: "note warn" }, `Route geometry error (${rr.endpoint}): ${rr.error?.message}`));
    else if (rr.status === "blocked") items.push(h("div", { class: "note warn" }, "No route geometry: " + rr.blockedReason));
    items.push(h("div", { class: "note" }, `Basemap: ${cfg.map.providerName}`));
    replace(status, items);
  }

  paintLayers(); paintLegend(); paintReadout(null); paintStatus();

  return {
    update() {
      loadRoutes(); loadRouteGeojson(); loadTrajectory(); loadForecast({ hours: 6, key: "forecastNow" });
      const sig = signature(KEYS, colorBy);
      if (sig === lastSig) return;
      lastSig = sig;
      refresh();
      paintStatus(); paintLegend();
    },
    destroy() { destroyed = true; clearTimeout(styleTimer); resizeObs?.disconnect(); if (map) map.remove(); },
  };
}

