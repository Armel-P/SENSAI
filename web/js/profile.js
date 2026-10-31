import { $, els, state } from "./state.js";
import { api } from "./api.js";
import { toast } from "./ui.js";

function applyProfile(profile) {
  state.profile = {
    name: profile.name || "",
    preferences: Array.isArray(profile.preferences) ? profile.preferences : [],
    instructions: profile.instructions || "",
  };
  const name = state.profile.name.trim();
  $(".avatar").textContent = name ? name[0].toUpperCase() : "S";
  $(".profile-shortcut-copy strong").textContent = name || "Set up your profile";
  $(".profile-shortcut-copy small").textContent = name ? "Profile & preferences" : "Name, preferences & instructions";
  els.profileName.value = state.profile.name;
  els.profilePreferences.value = state.profile.preferences.join("\n");
  els.profileInstructions.value = state.profile.instructions;
}

export async function loadProfile() {
  try { applyProfile(await api("/api/profile")); }
  catch (error) { toast(`Couldn't load profile: ${error.message}`, "error"); }
}

export function openProfile() {
  els.profileSaveStatus.textContent = "";
  els.profileModal.showModal();
}

async function saveProfile(event) {
  event.preventDefault();
  els.saveProfile.disabled = true;
  els.profileSaveStatus.textContent = "Saving…";
  try {
    applyProfile(await api("/api/profile", { method: "PUT", body: JSON.stringify({
      name: els.profileName.value.trim() || null,
      preferences: els.profilePreferences.value.split("\n").map(x => x.trim()).filter(Boolean),
      instructions: els.profileInstructions.value.trim() || null,
    }) }));
    els.profileSaveStatus.textContent = "Saved";
    toast("Profile saved.");
    setTimeout(() => { if (els.profileModal.open) els.profileModal.close(); }, 500);
  } catch (error) {
    els.profileSaveStatus.textContent = "";
    toast(`Couldn't save profile: ${error.message}`, "error");
  } finally { els.saveProfile.disabled = false; }
}

export function initProfile() {
  $("#openProfile").addEventListener("click", openProfile);
  $("#headerProfile").addEventListener("click", openProfile);
  $("#cancelProfile").addEventListener("click", () => els.profileModal.close());
  els.profileForm.addEventListener("submit", saveProfile);
}
