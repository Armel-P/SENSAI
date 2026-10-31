export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

export const state = {
  conversations: [], conversationId: null, messages: [],
  model: "", defaultModel: "", models: [],
  profile: { name: "", preferences: [], instructions: "" },
  controller: null, isStreaming: false, pendingDeleteId: null,
  numCtx: 8192, budget: 0, contextWarned: false,   // context meter (M2)
};

const IDS = [
  "sidebar", "conversationList", "newChat", "topbarTitle", "modelSelect", "chatScroll", "welcome",
  "messages", "streamStatus", "streamStatusText", "composerForm", "messageInput", "sendBtn", "stopBtn",
  "thinkToggle", "statusDot", "statusText", "toastRegion", "profileModal", "profileForm", "profileName",
  "profilePreferences", "profileInstructions", "profileSaveStatus", "saveProfile", "confirmModal",
  "confirmTitle", "confirmText", "confirmDelete", "contextMeter", "contextText", "contextFill",
];
export const els = Object.fromEntries(IDS.map(id => [id, document.getElementById(id)]));
