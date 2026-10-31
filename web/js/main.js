import { $, $$, els } from "./state.js";
import { closeSidebar } from "./ui.js";
import { initProfile, loadProfile } from "./profile.js";
import { initMeter, loadConfig } from "./meter.js";
import { initModelSelect, loadModels } from "./models.js";
import { deletePending, loadConversations, newChat } from "./conversations.js";
import { initMessageActions } from "./messages.js";
import { sendMessage, stopGeneration } from "./chat.js";
import { initPrompts } from "./prompts.js";

function resizeInput() {
  els.messageInput.style.height = "auto";
  els.messageInput.style.height = `${Math.min(els.messageInput.scrollHeight, 190)}px`;
}

function bindEvents() {
  els.composerForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const content = els.messageInput.value;
    if (!content.trim()) return;
    els.messageInput.value = "";
    resizeInput();
    await sendMessage(content);
  });
  els.messageInput.addEventListener("input", resizeInput);
  els.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      els.composerForm.requestSubmit();
    }
  });
  els.newChat.addEventListener("click", newChat);
  els.stopBtn.addEventListener("click", () => stopGeneration(true));
  els.confirmDelete.addEventListener("click", (event) => { event.preventDefault(); deletePending(); });
  $("#openSidebar").addEventListener("click", () => els.sidebar.classList.add("open"));
  $("#closeSidebar").addEventListener("click", closeSidebar);
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); newChat(); }
    if (event.key === "Escape") closeSidebar();
  });
  for (const card of $$(".suggestion[data-prompt]")) {
    card.addEventListener("click", () => {
      els.messageInput.value = card.dataset.prompt;
      resizeInput();
      els.messageInput.focus();
    });
  }
}

async function init() {
  initPrompts();
  bindEvents();
  initProfile();
  initMeter();
  initModelSelect();
  initMessageActions();
  await Promise.all([loadModels(), loadConversations(), loadProfile(), loadConfig()]);
  els.messageInput.focus({ preventScroll: true });
}

init();
