import { els, state } from "./state.js";

export const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (ch) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);

export function toast(message, type = "info", timeout = 3600) {
  const node = document.createElement("div");
  node.className = `toast${type === "error" ? " error" : ""}`;
  node.textContent = message;
  els.toastRegion.append(node);
  setTimeout(() => node.remove(), timeout);
}

export function scrollToBottom() {
  requestAnimationFrame(() => { els.chatScroll.scrollTop = els.chatScroll.scrollHeight; });
}

export function closeSidebar() { els.sidebar.classList.remove("open"); }

export function setConnection(ok, message) {
  els.statusDot.classList.toggle("online", ok);
  els.statusDot.classList.toggle("offline", !ok);
  els.statusText.textContent = message;
}

function syncComposer() {
  const locked = state.isStreaming || Boolean(state.pendingInteraction);
  els.sendBtn.classList.toggle("hidden", state.isStreaming);
  els.stopBtn.classList.toggle("hidden", !state.isStreaming);
  for (const control of [els.messageInput, els.modelSelect, els.thinkToggle, els.sendBtn]) control.disabled = locked;
  els.composerHint.textContent = state.pendingInteraction
    ? "Sensai is waiting for your response above"
    : "Local and private · Powered by Ollama";
  els.streamStatus.classList.toggle("hidden", !state.isStreaming);
  if (!state.isStreaming) els.streamStatusText.textContent = "Thinking…";
}

export function setStreaming(active) {
  state.isStreaming = active;
  syncComposer();
}

export function setPendingInteraction(interaction) {
  state.pendingInteraction = interaction;
  syncComposer();
}
