import { $, els, state } from "./state.js";
import { api } from "./api.js";
import { attachCopyButtons, renderMarkdown } from "./markdown.js";
import { escapeHTML, scrollToBottom, toast } from "./ui.js";
import { updateMeter } from "./meter.js";
import { forkConversation, renderConversationList } from "./conversations.js";
import { regenerateFrom, sendMessage } from "./chat.js";

const ACTIONS = '<button type="button" data-action="copy">Copy</button>' +
                '<button type="button" data-action="regenerate">Regenerate</button>';

function metaText(m) {
  if (m.role !== "assistant") return "";
  const bits = [];
  if (m.gen_tokens != null) bits.push(`${m.gen_tokens} tokens`);
  if (m.duration_s) bits.push(`${Number(m.duration_s).toFixed(1)}s`);
  if (m.interrupted) bits.push("stopped");
  return bits.join(" · ");
}

const thinkingHTML = (m) => (m.role === "assistant" && m.thinking)
  ? `<details class="thinking-block"><summary>Thinking</summary><div class="thinking-text">${escapeHTML(m.thinking)}</div></details>` : "";

export function makeMessageNode(message) {
  const user = message.role === "user";
  const node = document.createElement("article");
  node.className = `message ${user ? "user" : "assistant"}`;
  node.dataset.messageId = message.id ?? "";
  const siblings = Array.isArray(message.siblings) ? message.siblings : [];
  const branch = siblings.length > 1
    ? `<span class="branch-indicator">${Math.max(0, siblings.indexOf(message.id)) + 1} / ${siblings.length}</span>` +
      '<button type="button" data-action="branch-prev" title="Previous branch" aria-label="Previous branch">‹</button>' +
      '<button type="button" data-action="branch-next" title="Next branch" aria-label="Next branch">›</button>' : "";
  const fork = message.id
    ? '<button type="button" data-action="fork" title="Fork the conversation from this message">Fork</button>' : "";
  node.innerHTML =
    `<div class="message-avatar">${user ? "Y" : "✳"}</div><div class="message-main">` +
    `<div class="message-heading">${escapeHTML(user ? state.profile.name || "You" : "Sensai")}` +
    `<span class="message-meta">${escapeHTML(metaText(message))}</span></div>${thinkingHTML(message)}` +
    `<div class="message-content">${renderMarkdown(message.content || "")}</div>` +
    `<div class="message-actions">${user ? '<button type="button" data-action="edit">Edit & resend</button>' : ACTIONS}${fork}${branch}</div></div>`;
  attachCopyButtons(node);
  return node;
}

export function renderMessages(messages) {
  state.messages = messages || [];
  els.messages.replaceChildren(...state.messages.map(makeMessageNode));
  els.welcome.classList.toggle("hidden", state.messages.length > 0);
  updateMeter();
  scrollToBottom();
}

export function appendUserMessage(content) {
  els.messages.append(makeMessageNode({ role: "user", content }));
  els.welcome.classList.add("hidden");
  scrollToBottom();
}

let live = null, painting = false;

export const hasLive = () => live !== null;
export const liveText = () => (live ? live.text : "");

export function appendLiveAssistant() {
  const node = document.createElement("article");
  node.className = "message assistant";
  node.innerHTML = '<div class="message-avatar">✳</div><div class="message-main"><div class="message-heading">Sensai' +
    '<span class="message-meta"></span></div><div class="live-thinking"></div>' +
    '<div class="message-content"></div><div class="message-actions"></div></div>';
  els.messages.append(node);
  els.welcome.classList.add("hidden");
  live = { node, text: "", thinking: "" };
  scrollToBottom();
}

export function appendLiveText(text) { live.text += text; schedulePaint(); }
export function appendLiveThinking(text) { live.thinking += text; schedulePaint(); }

function schedulePaint() {
  if (painting) return;
  painting = true;
  requestAnimationFrame(paint);
}

function paint() {
  painting = false;
  if (!live) return;
  const { node, text, thinking } = live;
  if (thinking) {
    const host = $(".live-thinking", node);
    if (!host.firstChild) host.innerHTML = '<details class="thinking-block" open><summary>Thinking</summary><div class="thinking-text"></div></details>';
    $(".thinking-text", host).textContent = thinking;
  }
  $(".message-content", node).innerHTML = renderMarkdown(text);
  attachCopyButtons(node);
  scrollToBottom();
}

export function finishLiveAssistant(meta = {}, note = "") {
  if (!live) return;
  paint();
  const { node, text, thinking } = live;
  const content = $(".message-content", node);
  if (note) { content.insertAdjacentHTML("beforeend", `<p><em>${escapeHTML(note)}</em></p>`); }
  else if (!text.trim() && !thinking.trim()) content.innerHTML = "<p><em>No response was received.</em></p>";
  const bits = [];
  if (meta.gen_tokens != null) bits.push(`${meta.gen_tokens} tokens`);
  if (meta.duration_s > 0) bits.push(`${Number(meta.duration_s).toFixed(1)}s`);
  if (meta.stopped) bits.push("stopped");
  $(".message-meta", node).textContent = bits.join(" · ");
  $(".message-actions", node).innerHTML = ACTIONS;
  if (meta.assistant_message_id != null) node.dataset.messageId = String(meta.assistant_message_id);
  live = null;
}

const findMessage = (id) => state.messages.find(m => Number(m.id) === Number(id));

function startEditing(messageId, node) {
  const message = findMessage(messageId);
  if (!message || message.role !== "user") { toast("This message is not available to edit yet."); return; }
  const content = $(".message-content", node), actions = $(".message-actions", node);
  content.classList.add("hidden");
  actions.classList.add("hidden");
  const textarea = document.createElement("textarea");
  textarea.className = "inline-edit";
  textarea.value = message.content || "";
  const bar = document.createElement("div");
  bar.className = "edit-actions";
  const mk = (label, cls) => { const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = label; return b; };
  const cancel = mk("Cancel", "small-btn"), resend = mk("Save & resend", "small-btn primary");
  const close = () => { textarea.remove(); bar.remove(); content.classList.remove("hidden"); actions.classList.remove("hidden"); };
  cancel.addEventListener("click", close);
  resend.addEventListener("click", async () => {
    const value = textarea.value.trim();
    if (!value) { toast("Message can't be empty.", "error"); return; }
    close();
    await sendMessage(value, message.id);
  });
  bar.append(cancel, resend);
  content.after(textarea, bar);
  textarea.focus();
  textarea.setSelectionRange(textarea.value.length, textarea.value.length);
}

async function switchBranch(messageId, direction) {
  const message = findMessage(messageId);
  if (!message || !Array.isArray(message.siblings) || message.siblings.length < 2) return;
  const n = message.siblings.length;
  const target = message.siblings[(message.siblings.indexOf(message.id) + direction + n) % n];
  try {
    const data = await api(`/api/messages/${encodeURIComponent(target)}/switch`, { method: "POST" });
    renderMessages(data.messages || []);
    renderConversationList();
  } catch (error) { toast(`Couldn't switch branch: ${error.message}`, "error"); }
}

export function initMessageActions() {
  els.messages.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const node = button.closest(".message"), id = node?.dataset.messageId;
    switch (button.dataset.action) {
      case "edit": startEditing(id, node); break;
      case "branch-prev": switchBranch(id, -1); break;
      case "branch-next": switchBranch(id, 1); break;
      case "fork": forkConversation(state.conversationId, Number(id)); break;
      case "regenerate": regenerateFrom(id); break;
      case "copy":
        try { await navigator.clipboard.writeText($(".message-content", node)?.innerText || ""); toast("Message copied."); }
        catch (_) { toast("Couldn't access the clipboard in this browser.", "error"); }
    }
  });
}
