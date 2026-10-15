(() => {
  "use strict";

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = {
    conversations: [],
    conversationId: null,
    messages: [],
    model: "",
    defaultModel: "",
    models: [],
    profile: { name: "", preferences: [], instructions: "" },
    activeController: null,
    isStreaming: false,
    currentAssistantNode: null,
    currentAssistantText: "",
    currentThinkingText: "",
    editMessageId: null,
    pendingDeleteId: null,
    messageMeta: {},
  };

  const els = {
    sidebar: $("#sidebar"), conversationList: $("#conversationList"), newChat: $("#newChat"),
    topbarTitle: $("#topbarTitle"), modelSelect: $("#modelSelect"), chatScroll: $("#chatScroll"),
    welcome: $("#welcome"), messages: $("#messages"), streamStatus: $("#streamStatus"),
    streamStatusText: $("#streamStatusText"), composerForm: $("#composerForm"),
    messageInput: $("#messageInput"), sendBtn: $("#sendBtn"), stopBtn: $("#stopBtn"),
    thinkToggle: $("#thinkToggle"), statusDot: $("#statusDot"), statusText: $("#statusText"),
    toastRegion: $("#toastRegion"), profileModal: $("#profileModal"), profileForm: $("#profileForm"),
    profileName: $("#profileName"), profilePreferences: $("#profilePreferences"),
    profileInstructions: $("#profileInstructions"), profileSaveStatus: $("#profileSaveStatus"),
    saveProfile: $("#saveProfile"), confirmModal: $("#confirmModal"), confirmTitle: $("#confirmTitle"),
    confirmText: $("#confirmText"), confirmDelete: $("#confirmDelete"),
  };

  function toast(message, type = "info", timeout = 3600) {
    const node = document.createElement("div");
    node.className = `toast${type === "error" ? " error" : ""}`;
    node.textContent = message;
    els.toastRegion.append(node);
    window.setTimeout(() => node.remove(), timeout);
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { ...(options.body ? { "Content-Type": "application/json" } : {}), ...(options.headers || {}) },
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const data = await response.json();
        message = data.detail || data.message || message;
      } catch (_) {
        try { message = (await response.text()) || message; } catch (_) {}
      }
      throw new Error(message);
    }
    if (response.status === 204) return null;
    const type = response.headers.get("content-type") || "";
    return type.includes("application/json") ? response.json() : response;
  }

  function setConnection(ok, message) {
    els.statusDot.classList.toggle("online", ok);
    els.statusDot.classList.toggle("offline", !ok);
    els.statusText.textContent = message;
  }

  function setStreaming(active) {
    state.isStreaming = active;
    els.sendBtn.classList.toggle("hidden", active);
    els.stopBtn.classList.toggle("hidden", !active);
    els.messageInput.disabled = active;
    els.modelSelect.disabled = active;
    els.thinkToggle.disabled = active;
    els.streamStatus.classList.toggle("hidden", !active);
    if (!active) els.streamStatusText.textContent = "Thinking…";
  }

  function scrollToBottom() {
    requestAnimationFrame(() => { els.chatScroll.scrollTop = els.chatScroll.scrollHeight; });
  }

  function escapeHTML(value) {
    return String(value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[ch]);
  }

  // Small dependency-free Markdown renderer for paragraphs, headings, lists, links,
  // blockquotes, inline code, and fenced code blocks. Raw HTML is always escaped.
  function renderMarkdown(markdown) {
    const blocks = [];
    let text = String(markdown || "").replace(/\r\n?/g, "\n");
    text = text.replace(/```([^\n`]*)\n([\s\S]*?)```/g, (_, lang, code) => {
      const token = `\u0000BLOCK${blocks.length}\u0000`;
      blocks.push({ lang: (lang || "").trim(), code });
      return token;
    });

    const inline = (raw) => {
      let s = escapeHTML(raw);
      s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      s = s.replace(/__([^_]+)__/g, "<strong>$1</strong>");
      s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      s = s.replace(/(^|[^_])_([^_\n]+)_/g, "$1<em>$2</em>");
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, label, url) =>
        `<a href="${url}" target="_blank" rel="noopener noreferrer">${label}</a>`);
      s = s.replace(/(^|[\s(])(https?:\/\/[^\s<]+)/g, (_, prefix, url) => {
        if (s.includes(`href="${url}"`)) return _;
        const clean = url.replace(/[.,!?;:]$/, "");
        return `${prefix}<a href="${clean}" target="_blank" rel="noopener noreferrer">${clean}</a>${url.slice(clean.length)}`;
      });
      return s;
    };

    const lines = text.split("\n");
    const out = [];
    let paragraph = [], listType = null, listItems = [], quote = [];
    const flushParagraph = () => {
      if (paragraph.length) { out.push(`<p>${inline(paragraph.join("\n")).replace(/\n/g, "<br>")}</p>`); paragraph = []; }
    };
    const flushList = () => {
      if (listItems.length) {
        out.push(`<${listType}>${listItems.map(item => `<li>${inline(item)}</li>`).join("")}</${listType}>`);
        listItems = []; listType = null;
      }
    };
    const flushQuote = () => {
      if (quote.length) { out.push(`<blockquote>${quote.map(line => `<p>${inline(line)}</p>`).join("")}</blockquote>`); quote = []; }
    };

    for (const line of lines) {
      const blockMatch = line.match(/^\u0000BLOCK(\d+)\u0000$/);
      if (blockMatch) {
        flushParagraph(); flushList(); flushQuote();
        const b = blocks[Number(blockMatch[1])];
        out.push(`<div class="code-wrap"><div class="code-header"><span>${escapeHTML(b.lang || "code")}</span><button class="copy-code" type="button">Copy code</button></div><pre><code>${escapeHTML(b.code.replace(/\n$/, ""))}</code></pre></div>`);
        continue;
      }
      const heading = line.match(/^(#{1,3})\s+(.+)$/);
      if (heading) {
        flushParagraph(); flushList(); flushQuote();
        const level = heading[1].length;
        out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        continue;
      }
      const quoteMatch = line.match(/^\s*>\s?(.*)$/);
      if (quoteMatch) { flushParagraph(); flushList(); quote.push(quoteMatch[1]); continue; }
      if (!line.trim()) { flushParagraph(); flushList(); flushQuote(); continue; }
      const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (bullet || numbered) {
        flushParagraph(); flushQuote();
        const type = bullet ? "ul" : "ol";
        if (listType && listType !== type) flushList();
        listType = type; listItems.push((bullet || numbered)[1]); continue;
      }
      flushList(); flushQuote(); paragraph.push(line);
    }
    flushParagraph(); flushList(); flushQuote();

    return out.join("").replace(/\u0000BLOCK(\d+)\u0000/g, (_, n) => {
      const b = blocks[Number(n)];
      return `<div class="code-wrap"><div class="code-header"><span>${escapeHTML(b.lang || "code")}</span><button class="copy-code" type="button">Copy code</button></div><pre><code>${escapeHTML(b.code.replace(/\n$/, ""))}</code></pre></div>`;
    });
  }

  function attachCopyButtons(root) {
    $$(".copy-code", root).forEach(button => {
      button.addEventListener("click", async () => {
        const code = $("code", button.closest(".code-wrap"))?.textContent || "";
        try {
          await navigator.clipboard.writeText(code);
          button.textContent = "Copied";
          setTimeout(() => { button.textContent = "Copy code"; }, 1400);
        } catch (_) { toast("Couldn't access clipboard in this browser.", "error"); }
      });
    });
  }

  function makeMessageNode(message) {
    const role = message.role === "user" ? "user" : "assistant";
    const node = document.createElement("article");
    node.className = `message ${role}`;
    node.dataset.messageId = message.id ?? "";
    node.dataset.role = role;
    const avatar = role === "user" ? "Y" : "✳";
    const label = role === "user" ? "You" : "Sensai";
    const content = message.content || message.text || "";
    const meta = state.messageMeta[message.id] || {};
    const siblingIds = Array.isArray(message.siblings) ? message.siblings : [];
    const siblingIndex = Math.max(0, siblingIds.indexOf(message.id));
    const branchControls = siblingIds.length > 1
      ? `<span class="branch-indicator">${siblingIndex + 1} / ${siblingIds.length}</span>
         <button type="button" data-action="branch-prev" title="Previous branch" aria-label="Previous branch">‹</button>
         <button type="button" data-action="branch-next" title="Next branch" aria-label="Next branch">›</button>` : "";
    node.innerHTML = `
      <div class="message-avatar">${avatar}</div>
      <div class="message-main">
        <div class="message-heading">${label}<span class="message-meta">${escapeHTML(meta.status || "")}</span></div>
        <div class="message-content">${renderMarkdown(content)}</div>
        <div class="message-actions">
          ${role === "user" ? '<button type="button" data-action="edit">Edit & resend</button>' : '<button type="button" data-action="copy">Copy</button><button type="button" data-action="regenerate">Regenerate</button>'}
          ${branchControls}
        </div>
      </div>`;
    attachCopyButtons(node);
    return node;
  }

  function renderMessages(messages) {
    state.messages = messages || [];
    els.messages.replaceChildren();
    state.messages.forEach(message => els.messages.append(makeMessageNode(message)));
    els.welcome.classList.toggle("hidden", state.messages.length > 0);
    scrollToBottom();
  }

  function appendLiveAssistant() {
    const node = document.createElement("article");
    node.className = "message assistant";
    node.innerHTML = `<div class="message-avatar">✳</div><div class="message-main"><div class="message-heading">Sensai<span class="message-meta"></span></div><div class="live-thinking"></div><div class="message-content"></div><div class="message-actions"></div></div>`;
    els.messages.append(node);
    state.currentAssistantNode = node;
    state.currentAssistantText = "";
    state.currentThinkingText = "";
    els.welcome.classList.add("hidden");
    scrollToBottom();
  }

  function updateLiveAssistant() {
    const node = state.currentAssistantNode;
    if (!node) return;
    const thinkingHost = $(".live-thinking", node);
    thinkingHost.replaceChildren();
    if (state.currentThinkingText) {
      const details = document.createElement("details");
      details.className = "thinking-block";
      details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = "Thinking";
      const body = document.createElement("div");
      body.className = "thinking-text";
      body.textContent = state.currentThinkingText;
      details.append(summary, body);
      thinkingHost.append(details);
    }
    $(".message-content", node).innerHTML = renderMarkdown(state.currentAssistantText);
    attachCopyButtons(node);
    scrollToBottom();
  }

  function finishLiveAssistant(meta = {}) {
    const node = state.currentAssistantNode;
    if (!node) return;
    const content = $(".message-content", node);
    if (!state.currentAssistantText.trim() && !state.currentThinkingText.trim()) {
      content.innerHTML = '<p><em>No response was received.</em></p>';
    }
    const headingMeta = $(".message-meta", node);
    if (meta.gen_tokens != null) {
      const bits = [`${meta.gen_tokens} tokens`];
      if (meta.duration_s != null && Number(meta.duration_s) > 0) bits.push(`${Number(meta.duration_s).toFixed(1)}s`);
      headingMeta.textContent = bits.join(" · ");
    }
    const actions = $(".message-actions", node);
    actions.innerHTML = '<button type="button" data-action="copy">Copy</button><button type="button" data-action="regenerate">Regenerate</button>';
    node.dataset.messageId = meta.assistant_message_id ?? "";
    if (meta.assistant_message_id != null) node.dataset.messageId = String(meta.assistant_message_id);
    attachCopyButtons(node);
    state.currentAssistantNode = null;
    state.currentAssistantText = "";
    state.currentThinkingText = "";
  }

  async function loadModels() {
    try {
      const data = await api("/api/models");
      state.models = Array.isArray(data.models) ? data.models : [];
      state.defaultModel = data.default || "";
      if (!state.model) state.model = state.defaultModel;
      els.modelSelect.replaceChildren();
      if (!state.models.length && state.defaultModel) state.models = [state.defaultModel];
      state.models.forEach(model => {
        const name = typeof model === "string" ? model : model.name;
        if (!name) return;
        const option = document.createElement("option");
        option.value = name;
        option.textContent = name;
        els.modelSelect.append(option);
      });
      if (state.model && !state.models.some(m => (typeof m === "string" ? m : m.name) === state.model)) {
        const option = document.createElement("option");
        option.value = state.model; option.textContent = state.model;
        els.modelSelect.append(option);
      }
      els.modelSelect.value = state.model || state.defaultModel;
      setConnection(true, "Ollama connected");
    } catch (error) {
      els.modelSelect.replaceChildren(new Option("Ollama unavailable", ""));
      setConnection(false, "Ollama unavailable");
      toast(`Couldn't load Ollama models: ${error.message}`, "error", 6000);
    }
  }

  async function loadConversations() {
    try {
      state.conversations = await api("/api/conversations");
      if (!Array.isArray(state.conversations)) state.conversations = [];
      renderConversationList();
    } catch (error) {
      toast(`Couldn't load conversations: ${error.message}`, "error");
    }
  }

  function conversationIdOf(conv) {
    return conv.id ?? conv.conversation_id ?? conv.conv_id;
  }
  function conversationTitleOf(conv) {
    return conv.title || "New conversation";
  }

  function renderConversationList() {
    els.conversationList.replaceChildren();
    if (!state.conversations.length) {
      const empty = document.createElement("div");
      empty.className = "sidebar-empty";
      empty.textContent = "Your conversations will appear here.";
      els.conversationList.append(empty);
      return;
    }
    state.conversations.forEach(conv => {
      const id = conversationIdOf(conv);
      const item = document.createElement("div");
      item.className = `conversation-item${Number(id) === Number(state.conversationId) ? " active" : ""}`;
      const open = document.createElement("button");
      open.className = "conversation-open";
      open.type = "button";
      open.textContent = conversationTitleOf(conv);
      open.title = conversationTitleOf(conv);
      open.addEventListener("click", () => openConversation(id));
      const menu = document.createElement("button");
      menu.className = "conversation-menu";
      menu.type = "button";
      menu.textContent = "···";
      menu.title = "Conversation actions";
      menu.setAttribute("aria-label", `Actions for ${conversationTitleOf(conv)}`);
      menu.addEventListener("click", (event) => {
        event.stopPropagation();
        showConversationActions(id, conversationTitleOf(conv));
      });
      item.append(open, menu);
      els.conversationList.append(item);
    });
  }

  async function createConversation() {
    stopGeneration(false);
    try {
      const conv = await api("/api/conversations", {
        method: "POST",
        body: JSON.stringify({ title: "New conversation", model: state.model || null }),
      });
      state.conversationId = conversationIdOf(conv);
      state.editMessageId = null;
      state.messages = [];
      state.messageMeta = {};
      renderMessages([]);
      els.topbarTitle.textContent = conversationTitleOf(conv);
      renderConversationList();
      closeSidebar();
      els.messageInput.focus();
    } catch (error) {
      toast(`Couldn't create conversation: ${error.message}`, "error");
    }
  }

  async function openConversation(id) {
    if (id == null) return;
    if (state.isStreaming) stopGeneration(false);
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
      state.conversationId = id;
      const conv = data.conversation || {};
      if (conv.model) {
        state.model = conv.model;
        if (![...els.modelSelect.options].some(option => option.value === state.model)) {
          els.modelSelect.add(new Option(state.model, state.model));
        }
        els.modelSelect.value = state.model;
      }
      els.topbarTitle.textContent = conversationTitleOf(conv);
      renderMessages(data.messages || []);
      renderConversationList();
      closeSidebar();
    } catch (error) {
      toast(`Couldn't open conversation: ${error.message}`, "error");
    }
  }

  async function showConversationActions(id, title) {
    const choice = window.prompt(`Conversation: ${title}\nType "rename" or "delete". Leave blank to cancel.`);
    if (!choice) return;
    if (choice.trim().toLowerCase() === "rename") {
      const next = window.prompt("New conversation title:", title);
      if (next == null || !next.trim()) return;
      try {
        const conv = await api(`/api/conversations/${encodeURIComponent(id)}`, {
          method: "PATCH", body: JSON.stringify({ title: next.trim().slice(0, 100) }),
        });
        const idx = state.conversations.findIndex(c => Number(conversationIdOf(c)) === Number(id));
        if (idx >= 0) state.conversations[idx] = conv;
        if (Number(state.conversationId) === Number(id)) els.topbarTitle.textContent = conversationTitleOf(conv);
        await loadConversations();
        toast("Conversation renamed.");
      } catch (error) { toast(`Rename failed: ${error.message}`, "error"); }
    } else if (choice.trim().toLowerCase() === "delete") {
      state.pendingDeleteId = id;
      els.confirmTitle.textContent = "Delete conversation?";
      els.confirmText.textContent = `“${title}” and its messages will be deleted. This cannot be undone.`;
      els.confirmModal.showModal();
    }
  }

  async function deletePendingConversation() {
    const id = state.pendingDeleteId;
    if (id == null) return;
    try {
      await api(`/api/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
      els.confirmModal.close();
      if (Number(state.conversationId) === Number(id)) {
        state.conversationId = null;
        state.messages = [];
        renderMessages([]);
        els.topbarTitle.textContent = "New conversation";
      }
      state.pendingDeleteId = null;
      await loadConversations();
      toast("Conversation deleted.");
    } catch (error) { toast(`Delete failed: ${error.message}`, "error"); }
  }

  async function loadProfile() {
    try {
      const profile = await api("/api/profile");
      state.profile = {
        name: profile.name || "",
        preferences: Array.isArray(profile.preferences) ? profile.preferences : [],
        instructions: profile.instructions || "",
      };
      els.profileName.value = state.profile.name;
      els.profilePreferences.value = state.profile.preferences.join("\n");
      els.profileInstructions.value = state.profile.instructions;
      $(".avatar").textContent = state.profile.name.trim() ? state.profile.name.trim()[0].toUpperCase() : "S";
    } catch (error) {
      toast(`Couldn't load profile: ${error.message}`, "error");
    }
  }

  function openProfile() {
    els.profileSaveStatus.textContent = "";
    els.profileModal.showModal();
  }

  async function saveProfile(event) {
    event.preventDefault();
    els.saveProfile.disabled = true;
    els.profileSaveStatus.textContent = "Saving…";
    const body = {
      name: els.profileName.value.trim() || null,
      preferences: els.profilePreferences.value.split("\n").map(x => x.trim()).filter(Boolean),
      instructions: els.profileInstructions.value.trim() || null,
    };
    try {
      const profile = await api("/api/profile", { method: "PUT", body: JSON.stringify(body) });
      state.profile = { name: profile.name || "", preferences: profile.preferences || [], instructions: profile.instructions || "" };
      $(".avatar").textContent = state.profile.name ? state.profile.name[0].toUpperCase() : "S";
      els.profileSaveStatus.textContent = "Saved";
      toast("Profile saved.");
      setTimeout(() => { if (els.profileModal.open) els.profileModal.close(); }, 500);
    } catch (error) {
      els.profileSaveStatus.textContent = "";
      toast(`Couldn't save profile: ${error.message}`, "error");
    } finally { els.saveProfile.disabled = false; }
  }

  function parseSSEBlock(block) {
    let event = "message";
    const data = [];
    for (const line of block.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
    }
    if (!data.length) return null;
    try { return { event, data: JSON.parse(data.join("\n")) }; }
    catch (_) { return { event, data: { text: data.join("\n") } }; }
  }

  async function streamResponse(response, signal) {
    if (!response.body) throw new Error("This browser does not support response streaming.");
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let finishedMeta = null;
    while (true) {
      if (signal.aborted) break;
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const chunks = buffer.split(/\r?\n\r?\n/);
      buffer = chunks.pop() || "";
      for (const chunk of chunks) {
        const parsed = parseSSEBlock(chunk);
        if (!parsed) continue;
        const { event, data } = parsed;
        if (event === "started") {
          // Save the server-generated user message id to support subsequent edit/branch actions.
          state.lastUserMessageId = data.user_message_id;
          await loadConversations();
        } else if (event === "thinking") {
          state.currentThinkingText += data.text || "";
          els.streamStatusText.textContent = "Thinking…";
          updateLiveAssistant();
        } else if (event === "token") {
          state.currentAssistantText += data.text || "";
          els.streamStatusText.textContent = "Writing a response…";
          updateLiveAssistant();
        } else if (event === "finished") {
          finishedMeta = data;
        } else if (event === "error") {
          throw new Error(data.message || "The model returned an error.");
        }
      }
    }
    if (buffer.trim()) {
      const parsed = parseSSEBlock(buffer);
      if (parsed?.event === "finished") finishedMeta = parsed.data;
      else if (parsed?.event === "error") throw new Error(parsed.data.message || "The model returned an error.");
      else if (parsed?.event === "token") { state.currentAssistantText += parsed.data.text || ""; updateLiveAssistant(); }
      else if (parsed?.event === "thinking") { state.currentThinkingText += parsed.data.text || ""; updateLiveAssistant(); }
    }
    return finishedMeta;
  }

  async function sendMessage(content, editMessageId = null) {
    const clean = content.trim();
    if (!clean || state.isStreaming) return;
    if (state.conversationId == null) {
      try {
        const conv = await api("/api/conversations", {
          method: "POST", body: JSON.stringify({ title: clean.slice(0, 48), model: state.model || null }),
        });
        state.conversationId = conversationIdOf(conv);
        els.topbarTitle.textContent = conversationTitleOf(conv);
      } catch (error) { toast(`Couldn't create conversation: ${error.message}`, "error"); return; }
    }

    if (editMessageId != null) {
      const index = state.messages.findIndex(m => Number(m.id) === Number(editMessageId));
      if (index >= 0) renderMessages(state.messages.slice(0, index));
    } else {
      const userNode = makeMessageNode({ role: "user", content: clean });
      userNode.dataset.pending = "true";
      // Remove stale inline edit surface before appending the new message.
      els.messages.append(userNode);
      els.welcome.classList.add("hidden");
    }
    appendLiveAssistant();
    setStreaming(true);
    closeSidebar();
    const controller = new AbortController();
    state.activeController = controller;
    state.lastUserMessageId = editMessageId;
    scrollToBottom();

    try {
      const response = await fetch(`/api/conversations/${encodeURIComponent(state.conversationId)}/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "Accept": "text/event-stream" },
        body: JSON.stringify({
          content: clean,
          edit_message_id: editMessageId,
          model: state.model || null,
          think: els.thinkToggle.checked,
        }),
        signal: controller.signal,
      });
      if (!response.ok) {
        let message = `${response.status} ${response.statusText}`;
        try { const data = await response.json(); message = data.detail || message; } catch (_) {}
        throw new Error(message);
      }
      const meta = await streamResponse(response, controller.signal);
      finishLiveAssistant(meta || {});
      await refreshCurrentConversation();
      await loadConversations();
      setConnection(true, "Ollama connected");
    } catch (error) {
      if (error.name === "AbortError") {
        if (state.currentAssistantNode) {
          const meta = $(".message-meta", state.currentAssistantNode);
          if (meta) meta.textContent = "Stopped";
          finishLiveAssistant({});
        }
        // Server saves the partial assistant reply when the generator is closed.
        await refreshCurrentConversation().catch(() => {});
      } else {
        if (state.currentAssistantNode) {
          const contentNode = $(".message-content", state.currentAssistantNode);
          contentNode.innerHTML = state.currentAssistantText
            ? renderMarkdown(state.currentAssistantText) + `<p><em>Stream interrupted: ${escapeHTML(error.message)}</em></p>`
            : `<p><em>Couldn't get a response: ${escapeHTML(error.message)}</em></p>`;
          finishLiveAssistant({});
        }
        toast(`Message failed: ${error.message}`, "error", 6000);
        setConnection(false, "Connection issue");
      }
    } finally {
      state.activeController = null;
      setStreaming(false);
      els.messageInput.focus();
      scrollToBottom();
    }
  }

  async function refreshCurrentConversation() {
    if (state.conversationId == null) return;
    const data = await api(`/api/conversations/${encodeURIComponent(state.conversationId)}`);
    const conv = data.conversation || {};
    els.topbarTitle.textContent = conversationTitleOf(conv);
    renderMessages(data.messages || []);
  }

  function stopGeneration(showToast = true) {
    if (!state.activeController) return;
    state.activeController.abort();
    if (showToast) toast("Stopping generation…");
  }

  async function switchBranch(messageId, direction) {
    const message = state.messages.find(m => Number(m.id) === Number(messageId));
    if (!message || !Array.isArray(message.siblings) || message.siblings.length < 2) return;
    const index = message.siblings.findIndex(id => Number(id) === Number(message.id));
    const nextIndex = (index + direction + message.siblings.length) % message.siblings.length;
    const siblingId = message.siblings[nextIndex];
    try {
      const data = await api(`/api/messages/${encodeURIComponent(siblingId)}/switch`, { method: "POST" });
      state.messages = data.messages || [];
      renderMessages(state.messages);
      renderConversationList();
    } catch (error) { toast(`Couldn't switch branch: ${error.message}`, "error"); }
  }

  function startEditing(messageId, node) {
    const message = state.messages.find(m => Number(m.id) === Number(messageId));
    if (!message || message.role !== "user") {
      toast("This message is not available to edit yet.");
      return;
    }
    $$(".inline-edit", node).forEach(el => el.remove());
    const content = $(".message-content", node);
    content.classList.add("hidden");
    const actions = $(".message-actions", node);
    actions.classList.add("hidden");
    const textarea = document.createElement("textarea");
    textarea.className = "inline-edit";
    textarea.value = message.content || "";
    const editActions = document.createElement("div");
    editActions.className = "edit-actions";
    const cancel = document.createElement("button");
    cancel.type = "button"; cancel.className = "small-btn"; cancel.textContent = "Cancel";
    const resend = document.createElement("button");
    resend.type = "button"; resend.className = "small-btn primary"; resend.textContent = "Save & resend";
    cancel.addEventListener("click", () => { textarea.remove(); editActions.remove(); content.classList.remove("hidden"); actions.classList.remove("hidden"); });
    resend.addEventListener("click", async () => {
      const value = textarea.value.trim();
      if (!value) { toast("Message can't be empty.", "error"); return; }
      textarea.remove(); editActions.remove(); content.classList.remove("hidden"); actions.classList.remove("hidden");
      await sendMessage(value, message.id);
    });
    editActions.append(cancel, resend);
    content.after(textarea, editActions);
    textarea.focus();
    textarea.setSelectionRange(textarea.value.length, textarea.value.length);
  }

  async function regenerateFrom(messageId) {
    const node = els.messages.querySelector(`.message[data-message-id="${CSS.escape(String(messageId))}"]`);
    const index = state.messages.findIndex(m => Number(m.id) === Number(messageId));
    if (index < 0) { toast("Couldn't find this message in the current chat.", "error"); return; }
    // Regenerate by forking from the preceding user message. This keeps the backend's
    // existing branch model intact without requiring an extra regenerate endpoint.
    const previousUser = [...state.messages.slice(0, index)].reverse().find(m => m.role === "user");
    if (!previousUser) { toast("No preceding user message was found.", "error"); return; }
    const userIndex = state.messages.findIndex(m => Number(m.id) === Number(previousUser.id));
    const text = previousUser.content || "";
    await sendMessage(text, previousUser.id);
  }

  els.composerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = els.messageInput.value;
    if (!content.trim() || state.isStreaming) return;
    els.messageInput.value = "";
    resizeInput();
    await sendMessage(content, state.editMessageId);
    state.editMessageId = null;
  });

  function resizeInput() {
    els.messageInput.style.height = "auto";
    els.messageInput.style.height = `${Math.min(els.messageInput.scrollHeight, 190)}px`;
  }
  els.messageInput.addEventListener("input", resizeInput);
  els.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      els.composerForm.requestSubmit();
    }
  });
  els.newChat.addEventListener("click", createConversation);
  els.stopBtn.addEventListener("click", () => stopGeneration(true));
  els.modelSelect.addEventListener("change", () => { state.model = els.modelSelect.value; });
  $("#openProfile").addEventListener("click", openProfile);
  $("#headerProfile").addEventListener("click", openProfile);
  $("#closeSidebar").addEventListener("click", () => els.sidebar.classList.remove("open"));
  $("#openSidebar").addEventListener("click", () => els.sidebar.classList.add("open"));
  $("#cancelProfile").addEventListener("click", () => els.profileModal.close());
  els.profileForm.addEventListener("submit", saveProfile);
  els.confirmDelete.addEventListener("click", (event) => { event.preventDefault(); deletePendingConversation(); });
  els.messages.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const node = button.closest(".message");
    const messageId = node?.dataset.messageId;
    const action = button.dataset.action;
    if (action === "edit") startEditing(messageId, node);
    else if (action === "branch-prev") switchBranch(messageId, -1);
    else if (action === "branch-next") switchBranch(messageId, 1);
    else if (action === "copy") {
      const text = $(".message-content", node)?.innerText || "";
      try { await navigator.clipboard.writeText(text); toast("Message copied."); }
      catch (_) { toast("Couldn't access clipboard in this browser.", "error"); }
    } else if (action === "regenerate") regenerateFrom(messageId);
  });
  $$(".suggestion").forEach(button => button.addEventListener("click", () => {
    els.messageInput.value = button.dataset.prompt || "";
    resizeInput();
    els.messageInput.focus();
  }));
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      createConversation();
    }
    if (event.key === "Escape") closeSidebar();
  });
  function closeSidebar() { els.sidebar.classList.remove("open"); }

  async function init() {
    await Promise.all([loadModels(), loadConversations(), loadProfile()]);
    // Start with a blank landing screen. The first message creates its conversation.
    els.messageInput.focus({ preventScroll: true });
  }
  init();
})();
