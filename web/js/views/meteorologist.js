/**
 * View - Meteorologist.
 * The six-section cited briefing from /api/meteorologist/*: assessment,
 * supporting data, uncertainty, consequences, watch list, warnings. Every
 * number is shown with its citation; the free-text box needs the backend's
 * LLM classifier and says so honestly when it is off (HTTP 501).
 * @module views/meteorologist
 */
import { h, replace } from "../dom.js";
import { DASH, fmtNum, fmtUtc } from "../format.js";
import { panel, chip, withResource, stateBox, ageSpan, table } from "../ui.js";
import { res, signature, notify } from "../store.js";
import { loadBriefing } from "../loaders.js";
import { api } from "../api.js";

const KINDS = [
  { id: "morning-briefing", label: "Morgonbriefing" },
  { id: "risk-ahead", label: "Risk framåt" },
  { id: "forecast-reliability", label: "Prognostillförlitlighet" },
];
const sel = { kind: KINDS[0].id };
const asked = { status: "idle", question: "", data: null, error: null };

export function create(root) {
  const body = h("div");
  root.append(h("div", { class: "view-head" }, h("h1", null, "Meteorologist"), h("span", { class: "sub" }, "Citerad väderbriefing: varje siffra pekar på motorn som räknade fram den")), body);
  let lastSig = "";
  return {
    update() {
      loadBriefing(sel.kind);
      const sig = signature([`briefing:${sel.kind}`, "state"], `${sel.kind}|${asked.status}|${asked.question}`);
      if (sig === lastSig) return;
      lastSig = sig;
      replace(body, layout());
    },
    destroy() {},
  };
}

function layout() {
  const tabs = h("div", { class: "row", style: { marginBottom: "10px", gap: "6px" } },
    KINDS.map((k) => h("button", { class: `btn${k.id === sel.kind ? " primary" : ""}`, onclick: () => { sel.kind = k.id; notify(); } }, k.label)),
    h("button", { class: "btn", title: "Hämta om", onclick: () => loadBriefing(sel.kind, true) }, "↻"));
  const r = res(`briefing:${sel.kind}`);
  return h("div", null, tabs, askPanel(), asked.data ? briefingView(asked.data, `Svar: ${asked.question}`) : null,
    withResource(r, (b) => briefingView(b), { label: "briefing" }));
}

function askPanel() {
  const input = h("input", { class: "ctl", type: "text", placeholder: "Fråga meteorologen, t.ex. 'blir det moln vid Tennant Creek i eftermiddag?'", value: asked.question, style: { flex: "1" } });
  const submit = async () => {
    const q = input.value.trim();
    if (!q) return;
    Object.assign(asked, { status: "loading", question: q, data: null, error: null }); notify();
    try { asked.data = await api.ask(q, 12); asked.status = "ok"; }
    catch (e) { asked.error = e; asked.status = "error"; }
    notify();
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
  let msg = null;
  if (asked.status === "loading") msg = h("div", { class: "note" }, "Frågar…");
  else if (asked.status === "error") {
    msg = asked.error?.kind === "unsupported"
      ? stateBox("unknown", "Fritextfrågor är avstängda", `${asked.error.message}. Backend saknar LLM-klassificerare (RWI_ANTHROPIC_API_KEY ej satt). De fasta briefingarna ovan fungerar ändå — de använder ingen LLM.`)
      : stateBox("error", "Frågan misslyckades", asked.error?.message || String(asked.error));
  }
  return panel("Fråga", h("div", null, h("div", { class: "row", style: { gap: "6px" } }, input, h("button", { class: "btn", onclick: submit }, "Fråga")), msg));
}

function briefingView(b, title) {
  const cites = new Map((b.citations || []).map((c) => [c.id, c]));
  const citeChip = (id) => {
    const c = cites.get(id);
    return chip(id || DASH, "mute", c ? `${c.description}\n${c.source_module}${c.provider ? `\n${c.provider}${c.model_name ? "/" + c.model_name : ""}` : ""}` : "citation saknas");
  };
  const list = (items) => (items?.length ? h("ul", { class: "plain" }, items.map((t) => h("li", null, t))) : h("div", { class: "note" }, "Inget."));
  return h("div", { style: { display: "flex", flexDirection: "column", gap: "12px", marginBottom: "12px" } },
    panel(title || b.question, h("div", null,
      h("div", { style: { fontSize: "1.1em", margin: "4px 0 8px" } }, b.assessment),
      h("div", { class: "meta" }, "Skapad ", fmtUtc(b.asked_at_utc), " (", ageSpan(b.asked_at_utc), ")  ·  ", b.question_kind))),
    h("div", { style: { display: "grid", gridTemplateColumns: "minmax(0,3fr) minmax(0,2fr)", gap: "12px", alignItems: "start" } },
      h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } },
        panel("Stöddata", b.supporting_data?.length ? table([
          { h: "Storhet", cell: (d) => d.label },
          { h: "Värde", cls: "r", cell: (d) => (d.unit === "s (unix)" ? fmtUtc(new Date(d.value * 1000).toISOString()) : `${fmtNum(d.value, 1)} ${d.unit}`) },
          { h: "Giltig", cell: (d) => (d.valid_time_utc ? fmtUtc(d.valid_time_utc) : DASH) },
          { h: "Källa", cell: (d) => citeChip(d.citation_id) },
        ], b.supporting_data) : stateBox("unknown", "Ingen stöddata", "Briefingen innehåller inga siffror.")),
        panel("Operativa konsekvenser", list(b.operational_consequences)),
        panel("Bevaka härnäst", b.watch_next?.length ? table([
          { h: "Vad", cell: (w) => w.what },
          { h: "När", cell: (w) => w.when },
          { h: "Trigger", cell: (w) => w.trigger },
          { h: "Varför", cell: (w) => w.why },
        ], b.watch_next) : h("div", { class: "note" }, "Inget."))),
      h("div", { style: { display: "flex", flexDirection: "column", gap: "12px" } },
        panel("Osäkerhet", h("ul", { class: "plain" }, (b.uncertainty || []).map((u) =>
          h("li", null, chip(u.kind.replace(/_/g, " ").toUpperCase(), u.kind === "unproven" || u.kind === "data_gap" ? "warn" : "info"), " ", u.statement, u.citation_id ? [" ", citeChip(u.citation_id)] : null)))),
        panel("Varningar och begränsningar", list(b.warnings_and_limits)),
        panel("Okänt (datagap)", list(b.unknowns)))));
}
