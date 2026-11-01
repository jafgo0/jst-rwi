/**
 * Helpers that turn API series into aligned arrays for charts.
 * Missing values stay null (a gap) - they are never coerced to 0.
 * @module seriesutil
 */
import { toMs, toDisplay } from "./format.js";

/**
 * Pull one variable from a WeatherSeries.
 * @param {import("./api.js").WeatherSeries|null} ws
 * @param {string} variable
 * @returns {{map:Map<number,number>, missing:number, total:number}} map: epoch-seconds -> display-unit value
 */
export function pointsOf(ws, variable) {
  const map = new Map();
  let missing = 0, total = 0;
  for (const st of ws?.steps || []) {
    const m = st.values?.[variable];
    if (!m) continue;
    total++;
    const usable = m.value !== null && m.value !== undefined && Number.isFinite(m.value);
    if (!usable) { missing++; continue; }
    map.set(Math.round(toMs(st.valid_time_utc) / 1000), toDisplay(variable, m.value));
  }
  return { map, missing, total };
}

/**
 * Align several (time -> value) maps onto the sorted union of their times.
 * @param {Map<number,number>[]} maps
 * @returns {{x:number[], values:(number|null)[][]}}
 */
export function alignMaps(maps) {
  const set = new Set();
  maps.forEach((m) => m.forEach((_, k) => set.add(k)));
  const x = [...set].sort((a, b) => a - b);
  return { x, values: maps.map((m) => x.map((t) => (m.has(t) ? m.get(t) : null))) };
}

/** Min/max time (seconds) across a series' steps, or null. */
export function timeSpan(ws) {
  const t = (ws?.steps || []).map((s) => toMs(s.valid_time_utc) / 1000);
  return t.length ? [Math.min(...t), Math.max(...t)] : null;
}
