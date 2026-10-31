import { els, state } from "./state.js";
import { api, errorMessage, readSSE } from "./api.js";
import { closeSidebar, scrollToBottom, setConnection, setStreaming, toast } from "./ui.js";
import { appendLiveAssistant, appendLiveText, appendLiveThinking, appendUserMessage,
         finishLiveAssistant, liveText, renderMessages } from "./messages.js";
import { loadConversations } from "./conversations.js";

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

export async function refreshCurrentConversation() {
  if (state.conversationId == null) return null;
  const data = await api(`/api/conversations/${encodeURIComponent(state.conversationId)}`);
  els.topbarTitle.textContent = data.conversation?.title || "New conversation";
  renderMessages(data.messages || []);
  return data;
}

export function stopGeneration(showToast = true) {
  if (!state.controller) return;
  state.controller.abort();
  if (showToast) toast("Stopping generation…");
}

async function refreshAfterStop() {
  for (let i = 0; i < 6; i++) {
    await sleep(700);
    try {
      const data = await refreshCurrentConversation();
      if (data?.messages.at(-1)?.role === "assistant") return;
    } catch (_) {  }
  }
}

async function ensureConversation(firstMessage) {
  if (state.conversationId != null) return true;
  try {
    const conv = await api("/api/conversations", {
      method: "POST", body: JSON.stringify({ title: firstMessage.slice(0, 48), model: state.model || null }) });
    state.conversationId = conv.id ?? conv.conversation_id;
    els.topbarTitle.textContent = conv.title || "New conversation";
    return true;
  } catch (error) { toast(`Couldn't create conversation: ${error.message}`, "error"); return false; }
}

export async function sendMessage(content, editMessageId = null) {
  const clean = content.trim();
  if (!clean || state.isStreaming || !(await ensureConversation(clean))) return;

  if (editMessageId != null) {        // show the history up to the edited message, then its new text
    const index = state.messages.findIndex(m => Number(m.id) === Number(editMessageId));
    if (index >= 0) renderMessages(state.messages.slice(0, index));
  }
  appendUserMessage(clean);
  appendLiveAssistant();
  setStreaming(true);
  closeSidebar();
  const controller = new AbortController();
  state.controller = controller;

  try {
    const response = await fetch(`/api/conversations/${encodeURIComponent(state.conversationId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify({ content: clean, edit_message_id: editMessageId,
                             model: state.model || null, think: els.thinkToggle.checked }),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(await errorMessage(response));

    let meta = {};
    for await (const { event, data } of readSSE(response)) {
      if (event === "started") loadConversations();            // refresh the sidebar title
      else if (event === "thinking") { appendLiveThinking(data.text || ""); els.streamStatusText.textContent = "Thinking…"; }
      else if (event === "token") { appendLiveText(data.text || ""); els.streamStatusText.textContent = "Writing a response…"; }
      else if (event === "compressed") toast(`Older messages condensed (~${data.freed} tokens freed).`);
      else if (event === "finished") meta = data;
      else if (event === "error") throw new Error(data.message || "The model returned an error.");
    }
    finishLiveAssistant(meta);
    await refreshCurrentConversation();
    await loadConversations();
    setConnection(true, "Ollama connected");
  } catch (error) {
    if (error.name === "AbortError") {
      finishLiveAssistant({ stopped: true });
      await refreshAfterStop();
    } else {
      finishLiveAssistant({}, liveText() ? `Stream interrupted: ${error.message}` : `Couldn't get a response: ${error.message}`);
      toast(`Message failed: ${error.message}`, "error", 6000);
      setConnection(false, "Connection issue");
    }
  } finally {
    state.controller = null;
    setStreaming(false);
    els.messageInput.focus();
    scrollToBottom();
  }
}

export function regenerateFrom(messageId) {
  const index = state.messages.findIndex(m => Number(m.id) === Number(messageId));
  const previous = index < 0 ? null : state.messages.slice(0, index).reverse().find(m => m.role === "user");
  if (!previous) { toast("No preceding user message was found.", "error"); return; }
  return sendMessage(previous.content || "", previous.id);
}
