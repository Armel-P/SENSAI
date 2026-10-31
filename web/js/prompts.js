import { $, els } from "./state.js";
import { api } from "./api.js";
import { toast } from "./ui.js";

const fail = (message) => toast(message, "error", 4500);
const input = $("#messageInput");
let prompts = [], shown = [], active = 0, picker = null, slash = false;
let modal = null, editingId = null;

const snippet = (t, n = 64) => (t.length > n ? t.slice(0, n) + "…" : t);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };

function use(p) {
  const cur = input.value;
  input.value = !cur.trim() || cur.startsWith("/") ? p.content : `${cur.trimEnd()}\n\n${p.content}`;
  input.dispatchEvent(new Event("input"));
  input.focus();
  closePicker();
}

function renderWelcome() {
  const host = $("#welcome .suggestions");
  if (!host) return;
  host.replaceChildren();
  const card = (icon, title, sub, onClick) => {
    const b = el("button", "suggestion"); b.type = "button";
    b.append(el("span", "", icon), el("strong", "", title), el("small", "", sub));
    b.addEventListener("click", onClick); host.append(b);
  };
  prompts.slice(0, 5).forEach(p => card("✧", p.title, snippet(p.content, 48), () => use(p)));
  card("＋", "Your prompts", "Add or edit saved prompts", () => openManager());
}

function closePicker() { if (picker) { picker.remove(); picker = null; } slash = false; }

function openPicker(filter = "", viaSlash = false) {
  const q = filter.trim().toLowerCase();
  shown = prompts.filter(p => p.title.toLowerCase().includes(q) || p.content.toLowerCase().includes(q));
  active = Math.min(active, Math.max(0, shown.length - 1));
  slash = viaSlash;
  if (!picker) { picker = el("div", "prompt-picker"); $(".composer-wrap").append(picker); }
  picker.replaceChildren();
  if (!shown.length) picker.append(el("div", "prompt-empty", "No saved prompt matches."));
  shown.forEach((p, i) => {
    const b = el("button", `prompt-item${i === active ? " active" : ""}`); b.type = "button";
    b.append(el("strong", "", p.title), el("small", "", snippet(p.content, 90)));
    b.addEventListener("mousedown", e => e.preventDefault());
    b.addEventListener("click", () => use(p));
    picker.append(b);
  });
  const foot = el("div", "prompt-footer");
  const manage = el("button", "", "Manage prompts…"); manage.type = "button";
  manage.addEventListener("click", () => { closePicker(); openManager(); });
  foot.append(manage);
  const text = input.value.trim();
  if (text && !text.startsWith("/")) {
    const save = el("button", "", "＋ Save current text"); save.type = "button";
    save.addEventListener("click", () => { closePicker(); openManager(text); });
    foot.append(save);
  }
  picker.append(foot);
}

function buildModal() {
  modal = el("dialog", "modal");
  modal.innerHTML = `<div class="modal-card">
    <div class="modal-header"><div><p class="eyebrow">YOUR LIBRARY</p><h2>Saved prompts</h2></div>
      <button type="button" class="icon-btn" data-close aria-label="Close">×</button></div>
    <p class="modal-intro">Pick one from the “Prompts” button, or type “/” in the message box.</p>
    <div class="prompt-list"></div>
    <label class="field-label">Title</label><input class="text-field" data-title maxlength="60">
    <label class="field-label">Prompt</label><textarea class="text-field field-textarea" data-content rows="4" maxlength="4000"></textarea>
    <div class="modal-actions"><span class="save-status" data-status></span>
      <button type="button" class="secondary-btn" data-new>New</button>
      <button type="button" class="primary-btn" data-save>Save prompt</button></div></div>`;
  document.body.append(modal);
  $("[data-close]", modal).addEventListener("click", () => modal.close());
  $("[data-new]", modal).addEventListener("click", () => edit(null));
  $("[data-save]", modal).addEventListener("click", savePrompt);
}

function edit(p, prefill = "") {
  editingId = p ? p.id : null;
  $("[data-title]", modal).value = p ? p.title : "";
  $("[data-content]", modal).value = p ? p.content : prefill;
  $("[data-status]", modal).textContent = p ? "Editing…" : "";
  $("[data-save]", modal).textContent = p ? "Update prompt" : "Save prompt";
}

function renderList() {
  if (!modal) return;
  const list = $(".prompt-list", modal);
  list.replaceChildren();
  if (!prompts.length) list.append(el("div", "prompt-empty", "No prompts yet. Create your first one below."));
  prompts.forEach(p => {
    const row = el("div", "prompt-row"), text = el("div", "prompt-row-text");
    text.append(el("strong", "", p.title), el("small", "", snippet(p.content, 80)));
    const editBtn = el("button", "small-btn", "Edit"); editBtn.type = "button";
    editBtn.addEventListener("click", () => edit(p));
    const delBtn = el("button", "small-btn", "Delete"); delBtn.type = "button";
    let armed = null;
    delBtn.addEventListener("click", async () => {
      if (!armed) {
        delBtn.textContent = "Sure?"; armed = setTimeout(() => { armed = null; delBtn.textContent = "Delete"; }, 3000); return;
      }
      clearTimeout(armed);
      try { await api(`/api/prompts/${p.id}`, { method: "DELETE" }); if (editingId === p.id) edit(null); await reload(); }
      catch (e) { fail(`Delete failed: ${e.message}`); }
    });
    row.append(text, editBtn, delBtn); list.append(row);
  });
}

async function savePrompt() {
  const title = $("[data-title]", modal).value.trim(), content = $("[data-content]", modal).value.trim();
  if (!title || !content) { fail("A prompt needs a title and some text."); return; }
  try {
    await api(editingId ? `/api/prompts/${editingId}` : "/api/prompts", {
      method: editingId ? "PUT" : "POST", body: JSON.stringify({ title, content }) });
    edit(null); await reload();
    $("[data-status]", modal).textContent = "Saved";
  } catch (e) { fail(`Couldn't save prompt: ${e.message}`); }
}

function openManager(prefill = "") {
  if (!modal) buildModal();
  edit(null, prefill); renderList(); modal.showModal();
}

async function reload() {
  try { prompts = await api("/api/prompts"); }
  catch (e) { fail(`Couldn't load saved prompts: ${e.message}`); return; }
  renderWelcome(); renderList();
  if (picker) openPicker(slash ? input.value.slice(1) : "", slash);
}


export function initPrompts() {
  const btn = el("button", "prompt-btn", "✧ Prompts"); btn.type = "button";
  btn.title = "Insert one of your saved prompts (or type “/”)";
  btn.addEventListener("click", () => { if (picker) closePicker(); else { active = 0; openPicker(); } });
  $(".composer-tools").insertBefore(btn, $(".composer-hint"));
  document.addEventListener("click", e => { if (picker && !picker.contains(e.target) && e.target !== btn) closePicker(); });

  input.addEventListener("input", () => {
    const v = input.value;
    if (v.startsWith("/") && !v.includes("\n")) { active = 0; openPicker(v.slice(1), true); }
    else if (slash) closePicker();
  });
  input.addEventListener("keydown", e => {
    if (!picker) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (shown.length) active = (active + (e.key === "ArrowDown" ? 1 : -1) + shown.length) % shown.length;
      openPicker(slash ? input.value.slice(1) : "", slash);
    } else if ((e.key === "Enter" || e.key === "Tab") && shown.length) {
      e.preventDefault(); e.stopImmediatePropagation(); use(shown[active]);
    } else if (e.key === "Escape") closePicker();
  }, true);

  reload();
}
