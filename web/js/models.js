import { els, state } from "./state.js";
import { api } from "./api.js";
import { setConnection, toast } from "./ui.js";

export async function loadModels() {
  try {
    const data = await api("/api/models");
    state.defaultModel = data.default || "";
    state.models = (data.models || []).map(m => (typeof m === "string" ? m : m.name)).filter(Boolean);
    if (!state.model) state.model = state.defaultModel;
    const names = new Set(state.models);
    for (const extra of [state.model, state.defaultModel]) if (extra) names.add(extra);
    els.modelSelect.replaceChildren(...[...names].map(name => new Option(name, name)));
    els.modelSelect.value = state.model;
    setConnection(true, "Ollama connected");
  } catch (error) {
    els.modelSelect.replaceChildren(new Option("Ollama unavailable", ""));
    setConnection(false, "Ollama unavailable");
    toast(`Couldn't load Ollama models: ${error.message}`, "error", 6000);
  }
}

export function initModelSelect() {
  els.modelSelect.addEventListener("change", () => { state.model = els.modelSelect.value; });
}
