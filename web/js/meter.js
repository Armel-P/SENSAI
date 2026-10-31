import { els, state } from "./state.js";
import { api } from "./api.js";
import { toast } from "./ui.js";

const fmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

export function updateMeter() {
  const last = [...state.messages].reverse().find(m => m.prompt_tokens != null);
  const used = last ? (last.prompt_tokens || 0) + (last.gen_tokens || 0) : 0;
  const cap = state.budget > 0 ? Math.min(state.budget, state.numCtx) : state.numCtx;
  const pct = Math.min(100, Math.round((used / cap) * 100));
  els.contextText.textContent = `${fmt(used)} / ${fmt(cap)}`;
  els.contextFill.style.width = `${pct}%`;
  els.contextMeter.classList.toggle("warn", pct >= 75);
  els.contextMeter.classList.toggle("full", pct >= 90);
  els.contextMeter.title = `Context: ${used} tokens used, cap ${cap} (window ${state.numCtx}). ` + (state.budget > 0
    ? "Older messages are condensed automatically near the cap. Click to change."
    : "Budget off: Ollama silently drops the oldest messages when the window is full. Click to change.");
  if (pct >= 90 && state.budget <= 0 && !state.contextWarned) {
    state.contextWarned = true;
    toast("Context window almost full and the budget is off: the oldest messages may be forgotten.", "error", 6000);
  }
  if (pct < 75) state.contextWarned = false;
}

export function applySettings(cfg) {
  state.numCtx = cfg.num_ctx || state.numCtx;
  state.budget = cfg.effective_budget || 0;
  state.contextWarned = false;
  updateMeter();
}

export async function loadConfig() {
  try { applySettings(await api("/api/config")); } catch (_) { /* keep defaults */ }
}

const FIELDS = [["num_ctx", "Context window (tokens)"], ["token_budget", "Token budget (0 = off)"],
                ["keep_recent", "Newest messages never condensed"]];
const PRESETS = [["Default", { num_ctx: 8192, token_budget: 6000, keep_recent: 4 }],
                 ["Demo (small cap)", { num_ctx: 4096, token_budget: 1500, keep_recent: 4 }],
                 ["Off (big tests)", { token_budget: 0 }]];
let pop = null;

function closePopover() {
  pop?.remove(); pop = null;
  document.removeEventListener("click", onOutsideClick);
}
function onOutsideClick(e) { if (pop && !pop.contains(e.target) && !els.contextMeter.contains(e.target)) closePopover(); }

async function openPopover() {
  let cfg;
  try { cfg = await api("/api/settings"); } catch (_) { return; }
  pop = document.createElement("div");
  pop.className = "context-menu";
  pop.style.cssText = "width:300px;padding:14px";
  pop.innerHTML = '<strong style="font-size:12px">Context budget</strong>' +
    '<p style="margin:6px 0 4px;color:var(--muted);font-size:10.5px;line-height:1.5">Above the budget, older messages are condensed into a summary (nothing is deleted). Suggested: about 75% of the window.</p>' +
    FIELDS.map(([k, label]) => `<label class="field-label" style="margin:10px 0 5px">${label}</label><input class="text-field" type="number" min="0" data-k="${k}">`).join("") +
    '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:12px" data-presets></div>' +
    '<div style="display:flex;align-items:center;gap:8px;margin-top:12px"><span data-status style="flex:1;font-size:10.5px;color:var(--muted)"></span><button type="button" class="primary-btn" data-save>Save</button></div>';
  const inputs = [...pop.querySelectorAll("[data-k]")];
  const status = pop.querySelector("[data-status]");
  const show = (c) => inputs.forEach(i => { i.value = c[i.dataset.k]; });
  show(cfg);

  const save = async (patch) => {
    try {
      const next = await api("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
      show(next); applySettings(next);
      status.style.color = "#82d5a9";
      status.textContent = next.effective_budget ? `Saved. Cap: ${next.effective_budget} tokens.` : "Saved. Budget off.";
    } catch (e) { status.style.color = "var(--danger)"; status.textContent = e.message; }
  };
  for (const [name, values] of PRESETS) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "small-btn"; b.textContent = name;
    b.addEventListener("click", () => save(values));
    pop.querySelector("[data-presets]").append(b);
  }
  pop.querySelector("[data-save]").addEventListener("click", () =>
    save(Object.fromEntries(inputs.map(i => [i.dataset.k, parseInt(i.value, 10)]))));

  document.body.append(pop);
  const r = els.contextMeter.getBoundingClientRect();
  pop.style.top = `${Math.min(r.bottom + 6, window.innerHeight - pop.offsetHeight - 8)}px`;
  pop.style.left = `${Math.max(8, Math.min(r.right - 300, window.innerWidth - 308))}px`;
  setTimeout(() => document.addEventListener("click", onOutsideClick), 0);
}

export function initMeter() {
  els.contextMeter.style.cursor = "pointer";
  els.contextMeter.addEventListener("click", () => (pop ? closePopover() : openPopover()));
  document.addEventListener("keydown", e => { if (e.key === "Escape") closePopover(); });
}
