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

export function setStreaming(active) {
  state.isStreaming = active;
  els.sendBtn.classList.toggle("hidden", active);
  els.stopBtn.classList.toggle("hidden", !active);
  for (const control of [els.messageInput, els.modelSelect, els.thinkToggle]) control.disabled = active;
  els.streamStatus.classList.toggle("hidden", !active);
  if (!active) els.streamStatusText.textContent = "Thinking…";
}
