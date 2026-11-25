/**
 * uPlot wrappers. Visual language for honesty:
 *   observed  = solid line with point markers
 *   forecast  = dashed line, no markers
 *   clear-sky = dotted amber line with light fill, ALWAYS labelled "model upper bound"
 *   model     = thin solid line
 * An observed/forecast boundary (the effective time) is drawn as a vertical rule
 * with both region names; night can be shaded from the sunrise/sunset of the day.
 * @module charts
 */
import { h } from "./dom.js";
import { DASH, fmtNum, fmtUtc, fmtLocal } from "./format.js";

/** Read a CSS custom property from :root (so charts follow the theme). */
export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";
}

/**
 * @typedef {Object} ChartSeries
 * @property {string} label @property {string} unit @property {string} color CSS var name like "--series-1" or a colour
 * @property {"obs"|"fcst"|"clear"|"model"|"edge"} kind @property {(number|null)[]} values @property {number} [digits] @property {string} [scale]
 */
/**
 * @param {HTMLElement} container
 * @param {{ x:number[], series:ChartSeries[], height?:number, tz?:(string|null), utc?:boolean,
 *   xMode?:"time"|"distance", boundarySec?:(number|null), boundaryLabels?:[string,string], bands?:[number,number,string?][], fans?:{upper:number,lower:number}[],
 *   yLabel?:string, y2Label?:string, xLabel?:string, emptyText?:string }} spec
 * @returns {{destroy:()=>void}}
 */
export function makeChart(container, spec) {
  const U = /** @type {any} */ (window).uPlot;
  container.replaceChildren();
  const hasData = spec.x.length > 0 && spec.series.some((s) => s.values.some((v) => v !== null && v !== undefined));
  if (!U) { container.append(h("div", { class: "state error" }, h("h3", null, "Chart library not loaded"), "uPlot 1.6.31 could not be loaded from vendor/uplot-1.6.31/.")); return { destroy() {} }; }
  if (!hasData) { container.append(h("div", { class: "state" }, spec.emptyText || "No data points to plot.")); return { destroy() {} }; }

  const text = cssVar("--text-dim"), grid = cssVar("--border"), accent = cssVar("--accent");
  const isDist = spec.xMode === "distance";
  const tz = !spec.utc && spec.tz ? spec.tz : null;
  const tzLabel = isDist ? "Distance along route (km)" : tz ? `Time (${tz}, vehicle local)` : "Time (UTC)";

  const axes = [
    { stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, label: spec.xLabel || tzLabel, labelSize: 14, font: "11px system-ui", labelFont: "11px system-ui" },
    { scale: "y", stroke: text, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid }, label: spec.yLabel || "", labelSize: 14, size: 56, font: "11px system-ui", labelFont: "11px system-ui" },
  ];
  const usesY2 = spec.series.some((s) => s.scale === "y2");
  if (usesY2) axes.push({ scale: "y2", side: 1, stroke: text, grid: { show: false }, ticks: { stroke: grid }, label: spec.y2Label || "", labelSize: 14, size: 56, font: "11px system-ui", labelFont: "11px system-ui" });

  const fmtX = (u, v) => {
    if (v === null || v === undefined) return DASH;
    if (isDist) return `${fmtNum(v, 1)} km`;
    return `${fmtLocal(v * 1000, spec.tz || "", { seconds: false })} | ${fmtUtc(v * 1000)}`;
  };

  const series = [{ label: isDist ? "Distance" : "Time", value: fmtX }];
  for (const s of spec.series) {
    const col = s.color.startsWith("--") ? cssVar(s.color) : s.color;
    const o = {
      label: s.label + (s.unit ? ` (${s.unit})` : ""),
      scale: s.scale || "y",
      stroke: col,
      width: s.kind === "model" ? 1.2 : s.kind === "edge" ? 0.8 : 2,
      spanGaps: true,
      points: s.kind === "obs" ? { show: true, size: 4, fill: col, stroke: col } : { show: false },
      value: (u, v) => (v === null || v === undefined ? DASH : `${fmtNum(v, s.digits ?? 1)} ${s.unit}`),
    };
    if (s.kind === "fcst") o.dash = [7, 5];
    if (s.kind === "clear") { o.dash = [2, 4]; o.fill = col + "22"; o.width = 2; }
    series.push(o);
  }

  const hooks = {
    drawClear: [(u) => {
      const { ctx } = u; const { left, top, width, height } = u.bbox;
      ctx.save();
      for (const [a, b, c] of spec.bands || []) {
        const x0 = Math.max(left, u.valToPos(a, "x", true)), x1 = Math.min(left + width, u.valToPos(b, "x", true));
        if (x1 > x0) { ctx.fillStyle = c || "rgba(120,130,150,0.16)"; ctx.fillRect(x0, top, x1 - x0, height); }
      }
      if (spec.boundarySec !== null && spec.boundarySec !== undefined) {
        const bx = u.valToPos(spec.boundarySec, "x", true);
        if (bx > left && bx < left + width) { ctx.fillStyle = "rgba(111,168,255,0.07)"; ctx.fillRect(bx, top, left + width - bx, height); }
      }
      ctx.restore();
    }],
    draw: [(u) => {
      if (spec.boundarySec === null || spec.boundarySec === undefined) return;
      const { ctx } = u; const { left, top, width, height } = u.bbox;
      const bx = u.valToPos(spec.boundarySec, "x", true);
      if (bx < left || bx > left + width) return;
      const dpr = window.devicePixelRatio || 1;
      ctx.save();
      ctx.strokeStyle = accent; ctx.lineWidth = 1.5 * dpr; ctx.setLineDash([6 * dpr, 4 * dpr]);
      ctx.beginPath(); ctx.moveTo(bx, top); ctx.lineTo(bx, top + height); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = accent; ctx.font = `600 ${10 * dpr}px ui-monospace, monospace`;
      const [l, r] = spec.boundaryLabels || ["OBSERVED", "FORECAST"];
      ctx.textAlign = "right"; ctx.fillText(l + " ◂", bx - 5 * dpr, top + 12 * dpr);
      ctx.textAlign = "left"; ctx.fillText("▸ " + r, bx + 5 * dpr, top + 12 * dpr);
      ctx.restore();
    }],
  };

  const scales = { x: { time: !isDist } };
  /** @type {any} */
  const opts = {
    width: Math.max(300, container.clientWidth), height: spec.height || 190,
    series, axes, scales, hooks,
    cursor: { drag: { x: false, y: false } },
    legend: { live: true },
  };
  if (!isDist && tz) opts.tzDate = (ts) => U.tzDate(new Date(ts * 1000), tz);

  // Fan / uncertainty bands between two series (indices into spec.series), e.g. ensemble P10 to P90.
  if (spec.fans?.length) opts.bands = spec.fans.map((f) => ({ series: [f.upper + 1, f.lower + 1], fill: (spec.series[f.upper].color.startsWith("--") ? cssVar(spec.series[f.upper].color) : spec.series[f.upper].color) + "33" }));

  const data = [spec.x, ...spec.series.map((s) => s.values)];
  const u = new U(opts, data, container);
  const ro = new ResizeObserver(() => { const w = Math.max(300, container.clientWidth); if (Math.abs(w - u.width) > 2) u.setSize({ width: w, height: opts.height }); });
  ro.observe(container);
  return { destroy() { ro.disconnect(); u.destroy(); } };
}

/** Compact legend row explaining line styles (shown under charts). */
export function styleLegend(entries) {
  const kinds = { obs: "", fcst: "dash", clear: "dot", model: "" };
  return h("div", { class: "chart-legend" }, entries.map((e) =>
    h("span", { class: "legend-line", style: { color: e.color.startsWith("--") ? `var(${e.color})` : e.color } },
      h("i", { class: kinds[e.kind] || "" }), e.text)));
}

/**
 * Collects charts created during one render so they can be destroyed together
 * before the next. Charts are built after the container is in the document.
 */
export function chartHost() {
  const list = [];
  return {
    /** Build the chart on the next frame (container must be attached by then). */
    add(container, spec) { setTimeout(() => { if (container.isConnected) list.push(makeChart(container, spec)); }, 0); },
    destroyAll() { while (list.length) { try { list.pop().destroy(); } catch { /* already gone */ } } },
  };
}
