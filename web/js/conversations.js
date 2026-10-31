import { els, state } from "./state.js";
import { api } from "./api.js";
import { closeSidebar, toast } from "./ui.js";
import { renderMessages } from "./messages.js";
import { stopGeneration } from "./chat.js";

const idOf = (c) => c.id ?? c.conversation_id;
const titleOf = (c) => c.title || "New conversation";
const same = (a, b) => Number(a) === Number(b);
const button = (cls, text) => {
  const b = document.createElement("button");
  b.type = "button"; b.className = cls; b.textContent = text;
  return b;
};

export async function loadConversations() {
  try {
    const list = await api("/api/conversations");
    state.conversations = Array.isArray(list) ? list : [];
    renderConversationList();
  } catch (error) { toast(`Couldn't load conversations: ${error.message}`, "error"); }
}

export function renderConversationList() {
  els.conversationList.replaceChildren();
  if (!state.conversations.length) {
    const empty = document.createElement("div");
    empty.className = "sidebar-empty";
    empty.textContent = "Your conversations will appear here.";
    els.conversationList.append(empty);
    return;
  }
  for (const conv of state.conversations) {
    const id = idOf(conv), title = titleOf(conv);
    const item = document.createElement("div");
    item.className = `conversation-item${same(id, state.conversationId) ? " active" : ""}`;
    const open = button("conversation-open", title);
    open.title = title;
    open.addEventListener("click", () => openConversation(id));
    const menu = button("conversation-menu", "···");
    menu.title = "Conversation actions";
    menu.setAttribute("aria-label", `Actions for ${title}`);
    menu.addEventListener("click", (event) => { event.stopPropagation(); showMenu(id, title, menu); });
    item.append(open, menu);
    els.conversationList.append(item);
  }
}

function resetView() {
  state.conversationId = null;
  state.messages = [];
  renderMessages([]);
  els.topbarTitle.textContent = "New conversation";
  renderConversationList();
}

export function newChat() {
  stopGeneration(false);
  resetView();
  closeSidebar();
  els.messageInput.focus();
}

export async function openConversation(id) {
  if (id == null) return;
  if (state.isStreaming) stopGeneration(false);
  try {
    const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
    state.conversationId = id;
    const conv = data.conversation || {};
    if (conv.model) {
      state.model = conv.model;
      if (![...els.modelSelect.options].some(o => o.value === conv.model)) els.modelSelect.add(new Option(conv.model, conv.model));
      els.modelSelect.value = conv.model;
    }
    els.topbarTitle.textContent = titleOf(conv);
    renderMessages(data.messages || []);
    renderConversationList();
    closeSidebar();
  } catch (error) { toast(`Couldn't open conversation: ${error.message}`, "error"); }
}

export async function forkConversation(id, fromMessageId = null) {
  if (id == null) return;
  try {
    const data = await api(`/api/conversations/${encodeURIComponent(id)}/fork`, {
      method: "POST", body: JSON.stringify({ from_message_id: fromMessageId }) });
    await loadConversations();
    await openConversation(data.conversation.id);
    toast(fromMessageId ? "Forked from this message." : "Conversation forked.");
  } catch (error) { toast(`Fork failed: ${error.message}`, "error"); }
}

function exportConversation(id, format) {
  const link = document.createElement("a");
  link.href = `/api/conversations/${encodeURIComponent(id)}/export?format=${format}`;
  document.body.append(link);
  link.click();
  link.remove();
}

function closeMenu() {
  document.getElementById("conversationMenu")?.remove();
  document.removeEventListener("click", closeMenu);
  document.removeEventListener("keydown", onMenuKey);
}
const onMenuKey = (event) => { if (event.key === "Escape") closeMenu(); };

function showMenu(id, title, anchor) {
  closeMenu();
  const menu = document.createElement("div");
  menu.id = "conversationMenu";
  menu.className = "context-menu";
  menu.setAttribute("role", "menu");
  const actions = [
    ["✎", "Rename", () => renameConversation(id, title)],
    ["⑂", "Fork conversation", () => forkConversation(id)],
    ["⇩", "Export as Markdown", () => exportConversation(id, "markdown")],
    ["⇩", "Export as JSON", () => exportConversation(id, "json")],
    ["✕", "Delete", () => confirmDelete(id, title), true],
  ];
  for (const [icon, label, run, danger] of actions) {
    const item = document.createElement("button");
    item.type = "button";
    item.setAttribute("role", "menuitem");
    item.className = `context-menu-item${danger ? " danger" : ""}`;
    item.innerHTML = `<span>${icon}</span>${label}`;
    item.addEventListener("click", (event) => { event.stopPropagation(); closeMenu(); run(); });
    menu.append(item);
  }
  document.body.append(menu);
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.max(8, Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 8))}px`;
  menu.style.left = `${Math.max(8, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.querySelector("button").focus();
  setTimeout(() => document.addEventListener("click", closeMenu), 0);
  document.addEventListener("keydown", onMenuKey);
}

function askText(title, value) {
  return new Promise(resolve => {
    const dlg = document.createElement("dialog");
    dlg.className = "modal confirm-modal";
    dlg.innerHTML = '<form method="dialog" class="modal-card"><div class="modal-header"><h2></h2></div>' +
      '<input class="text-field" maxlength="100" style="margin-top:16px">' +
      '<div class="modal-actions"><button type="button" class="secondary-btn" data-cancel>Cancel</button>' +
      '<button type="submit" class="primary-btn">Save</button></div></form>';
    dlg.querySelector("h2").textContent = title;
    const input = dlg.querySelector("input");
    input.value = value;
    let result = null;
    dlg.querySelector("form").addEventListener("submit", () => { result = input.value; });
    dlg.querySelector("[data-cancel]").addEventListener("click", () => dlg.close());
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    document.body.append(dlg);
    dlg.showModal();
    input.select();
  });
}

async function renameConversation(id, title) {
  const next = await askText("Rename conversation", title);
  if (next == null || !next.trim()) return;
  try {
    const conv = await api(`/api/conversations/${encodeURIComponent(id)}`, {
      method: "PATCH", body: JSON.stringify({ title: next.trim().slice(0, 100) }) });
    if (same(state.conversationId, id)) els.topbarTitle.textContent = titleOf(conv);
    await loadConversations();
    toast("Conversation renamed.");
  } catch (error) { toast(`Rename failed: ${error.message}`, "error"); }
}

function confirmDelete(id, title) {
  state.pendingDeleteId = id;
  els.confirmTitle.textContent = "Delete conversation?";
  els.confirmText.textContent = `“${title}” and its messages will be deleted. This cannot be undone.`;
  els.confirmModal.showModal();
}

export async function deletePending() {
  const id = state.pendingDeleteId;
  if (id == null) return;
  try {
    await api(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
    els.confirmModal.close();
    state.pendingDeleteId = null;
    if (same(state.conversationId, id)) resetView();
    await loadConversations();
    toast("Conversation deleted.");
  } catch (error) { toast(`Delete failed: ${error.message}`, "error"); }
}
