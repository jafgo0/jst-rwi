/**
 * Tiny DOM helper. Text is always inserted with textContent / text nodes, so
 * API-supplied strings can never inject markup.
 * @module dom
 */

/**
 * @param {string} tag
 * @param {Record<string, any> | null} [attrs]  class, dataset, style(object), on<event>, text, any attribute
 * @param {...any} kids  strings, Nodes, arrays (nested ok), null/false skipped
 * @returns {HTMLElement}
 */
export function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else if (k === "dataset") Object.assign(el.dataset, v);
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : String(v));
    }
  }
  append(el, kids);
  return el;
}

/** Append children (strings become text nodes). */
export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
  return el;
}

/** Replace all children of `el`. */
export function replace(el, ...kids) {
  el.replaceChildren();
  return append(el, kids);
}
