"use strict";

const state = {me: null, csrf: "", lists: [], rules: [], tone: null};
let activeMutations = 0;
const byId = (id) => document.getElementById(id);

function feedback(message, error = false) {
  const node = byId("feedback");
  node.textContent = message;
  node.className = error ? "error" : "success";
}

function restoreButtonStates() {
  const hasOwnerContact = Boolean(byId("owner-contact").value.trim());
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = button.id === "copy-notice" && !hasOwnerContact;
  });
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const mutation = !['GET', 'HEAD'].includes(method);
  const headers = new Headers(options.headers || {});
  if (mutation) {
    headers.set("X-CSRF-Token", state.csrf);
    if (options.body) headers.set("Content-Type", "application/json");
    activeMutations += 1;
    document.querySelectorAll("button").forEach((button) => { button.disabled = true; });
    feedback("Working…");
  }
  try {
    const response = await fetch(path, {...options, method, headers, credentials: "same-origin"});
    let payload = {};
    try { payload = await response.json(); } catch (_error) { payload = {}; }
    if (response.status === 401) {
      byId("admin-panel").hidden = true;
      byId("login-panel").hidden = false;
      throw new Error("Your session expired or was revoked. Please log in again.");
    }
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  } finally {
    if (mutation) {
      activeMutations -= 1;
      if (activeMutations === 0) restoreButtonStates();
    }
  }
}

function textCard(title, details, actions = []) {
  const card = document.createElement("article");
  card.className = "card";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const body = document.createElement("pre");
  body.textContent = details;
  const actionBar = document.createElement("div");
  actionBar.className = "card-actions";
  actions.forEach(({label, run, danger}) => {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (danger) button.className = "danger";
    button.addEventListener("click", run);
    actionBar.append(button);
  });
  card.append(heading, body, actionBar);
  return card;
}

async function loadLists() {
  const data = await api("/api/admin/lists");
  state.lists = data.lists;
  const container = byId("list-items");
  container.replaceChildren();
  data.lists.forEach((item) => {
    const actions = [{label: "Edit", run: () => editList(item)}];
    if (item.slug !== "ignore") actions.push({label: "Delete", danger: true, run: async () => {
      if (!window.confirm(`Delete list ${item.slug}?`)) return;
      try { await api(`/api/admin/lists/${encodeURIComponent(item.slug)}`, {method: "DELETE"}); await loadLists(); feedback("List deleted."); } catch (error) { feedback(error.message, true); }
    }});
    (item.members || []).forEach((userId) => actions.push({label: `Remove member ${userId}`, run: async () => {
      if (!window.confirm(`Remove user ${userId} from ${item.slug}?`)) return;
      try { await api(`/api/admin/lists/${encodeURIComponent(item.slug)}/members/${userId}`, {method: "DELETE"}); await loadLists(); feedback("Member removed."); } catch (error) { feedback(error.message, true); }
    }}));
    container.append(textCard(item.slug, `${item.title}\nPriority: ${item.priority}\nScopes: ${item.applies_to.join(", ")}\nEnabled: ${item.enabled}\nMembers: ${(item.members || []).join(", ") || "none"}\nPrompt: ${item.injected_prompt || "(none)"}`, actions));
  });
}

function editList(item) {
  byId("list-original-slug").value = item.slug;
  byId("list-slug").value = item.slug;
  byId("list-title").value = item.title;
  byId("list-priority").value = item.priority;
  byId("list-enabled").checked = item.enabled;
  document.querySelectorAll('[name="list-scope"]').forEach((box) => { box.checked = item.applies_to.includes(box.value); });
  byId("list-prompt").value = item.injected_prompt;
  byId("list-form").scrollIntoView({behavior: "smooth"});
}

function resetList() {
  byId("list-form").reset();
  byId("list-original-slug").value = "";
  byId("list-enabled").checked = true;
}

async function loadRules() {
  const data = await api("/api/admin/rules");
  state.rules = data.rules;
  const container = byId("rule-items");
  container.replaceChildren();
  data.rules.forEach((item) => container.append(textCard(item.id, `Priority: ${item.priority}\nScope: ${item.scope}\nMatch: ${item.match.type} / ${item.match.value}\nEnabled: ${item.enabled}\nStop lower groups: ${item.stop_processing}\nInstruction: ${item.instruction}`, [
    {label: "Edit", run: () => editRule(item)},
    {label: "Delete", danger: true, run: async () => { if (!window.confirm(`Delete rule ${item.id}?`)) return; try { await api(`/api/admin/rules/${encodeURIComponent(item.id)}`, {method: "DELETE"}); await loadRules(); feedback("Rule deleted."); } catch (error) { feedback(error.message, true); }}},
  ])));
}

function editRule(item) {
  byId("rule-original-id").value = item.id;
  byId("rule-id").value = item.id;
  byId("rule-id").disabled = true;
  byId("rule-enabled").checked = item.enabled;
  byId("rule-priority").value = item.priority;
  byId("rule-scope").value = item.scope;
  byId("rule-match-type").value = item.match.type;
  byId("rule-match-value").value = item.match.value;
  byId("rule-instruction").value = item.instruction;
  byId("rule-stop").checked = item.stop_processing;
  byId("rule-form").scrollIntoView({behavior: "smooth"});
}

function resetRule() {
  byId("rule-form").reset();
  byId("rule-original-id").value = "";
  byId("rule-id").disabled = false;
  byId("rule-enabled").checked = true;
}

async function loadTone() {
  state.tone = await api("/api/admin/tone");
  byId("tone-global").textContent = JSON.stringify(state.tone.global, null, 2);
  byId("tone-chat").textContent = JSON.stringify(state.tone.chat_override, null, 2);
  byId("tone-effective").textContent = JSON.stringify(state.tone.effective, null, 2);
  fillToneForm();
}

function fillToneForm() {
  if (!state.tone) return;
  const scope = byId("tone-scope").value;
  const value = scope === "global" ? state.tone.global : (state.tone.chat_override || state.tone.effective);
  byId("tone-preset").value = value.tone_preset;
}

async function loadLogs() {
  const data = await api("/api/admin/logs");
  const container = byId("log-items");
  container.replaceChildren();
  data.records.forEach((record) => {
    const markers = [record.is_bot ? "bot" : "participant", record.is_edited ? "edited" : null, record.reply_to ? `reply to ${record.reply_to.message_id}` : null].filter(Boolean).join(" · ");
    container.append(textCard(`${record.name || "Unknown"} · ${record.message_id}`, `${markers}\n${record.text || "(media/no caption)"}`));
  });
  if (!data.records.length) container.textContent = "No retained records.";
}

function renderPrivacyNotice() {
  const retention = state.me.retention;
  const historyDuration = `${retention.history_seconds} seconds`;
  const jobDuration = `${retention.job_seconds} seconds`;
  const contact = byId("owner-contact").value.trim();
  const copyButton = byId("copy-notice");
  copyButton.disabled = !contact;
  byId("privacy-notice").value = contact
    ? `Privacy notice for this group: Kulajaj retains up to ${retention.history_limit} recent messages for no longer than ${historyDuration}. Expired records are excluded and removed on the next access. The entire buffer expires after ${historyDuration} without activity. Observed profiles and list membership remain until the owner deletes them. Durable model-job snapshots are retained for up to ${jobDuration}. Selected context is processed by NVIDIA NIM. When a participant explicitly uses /google, Tavily receives that command's bounded search query. Recent group history is not sent to Tavily. QStash receives only an opaque job ID. Owner contact: ${contact}. Contact that owner to request profile, message, or full-chat deletion. Data already sent to an external provider cannot be recalled.`
    : "";
}

function configurePrivacy() {
  const retention = state.me.retention;
  const historyDuration = `${retention.history_seconds} seconds`;
  const jobDuration = `${retention.job_seconds} seconds`;
  byId("retention").textContent = `The bot retains at most ${retention.history_limit} recent messages. Expired records are excluded and removed on the next access. The entire history expires after ${historyDuration} without activity. Durable model-job snapshots expire after ${jobDuration}.`;
  byId("owner-contact").value = state.me.username ? `@${state.me.username}` : "";
  renderPrivacyNotice();
  byId("delete-user-form").hidden = !state.me.is_super_admin;
  byId("purge-form").hidden = !state.me.is_super_admin;
}

async function loadAll() {
  await Promise.all([loadLists(), loadRules(), loadTone(), loadLogs()]);
  configurePrivacy();
}

byId("user-search-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { const data = await api(`/api/admin/users?q=${encodeURIComponent(byId("user-query").value)}`); byId("user-result").textContent = JSON.stringify(data, null, 2); feedback("User lookup complete."); } catch (error) { byId("user-result").textContent = error.message; feedback(error.message, true); }
});

byId("list-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const original = byId("list-original-slug").value;
  const body = {slug: byId("list-slug").value, title: byId("list-title").value, enabled: byId("list-enabled").checked, priority: Number(byId("list-priority").value), applies_to: [...document.querySelectorAll('[name="list-scope"]:checked')].map((box) => box.value), injected_prompt: byId("list-prompt").value};
  try { await api(original ? `/api/admin/lists/${encodeURIComponent(original)}` : "/api/admin/lists", {method: original ? "PUT" : "POST", body: JSON.stringify(body)}); resetList(); await loadLists(); feedback("List saved."); } catch (error) { feedback(error.message, true); }
});
byId("list-reset").addEventListener("click", resetList);

byId("member-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const slug = encodeURIComponent(byId("member-slug").value);
  const identifier = byId("member-user-id").value.trim();
  let userId;
  try {
    if (/^[0-9]+$/.test(identifier)) userId = Number(identifier);
    else userId = (await api(`/api/admin/users?q=${encodeURIComponent(identifier)}`)).user_id;
  } catch (error) { feedback(error.message, true); return; }
  try { await api(`/api/admin/lists/${slug}/members/${userId}`, {method: "POST"}); event.target.reset(); await loadLists(); feedback("List member added."); } catch (error) { feedback(error.message, true); }
});

byId("rule-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const original = byId("rule-original-id").value;
  const body = {id: byId("rule-id").value, enabled: byId("rule-enabled").checked, priority: Number(byId("rule-priority").value), scope: byId("rule-scope").value, match: {type: byId("rule-match-type").value, value: byId("rule-match-value").value}, instruction: byId("rule-instruction").value, stop_processing: byId("rule-stop").checked};
  try { await api(original ? `/api/admin/rules/${encodeURIComponent(original)}` : "/api/admin/rules", {method: original ? "PUT" : "POST", body: JSON.stringify(body)}); resetRule(); await loadRules(); feedback("Rule saved."); } catch (error) { feedback(error.message, true); }
});
byId("rule-reset").addEventListener("click", resetRule);

byId("tone-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = {scope: byId("tone-scope").value, tone_preset: byId("tone-preset").value};
  try { await api("/api/admin/tone", {method: "PUT", body: JSON.stringify(body)}); await loadTone(); feedback("Tone configuration saved."); } catch (error) { feedback(error.message, true); }
});
byId("tone-scope").addEventListener("change", fillToneForm);
byId("tone-clear").addEventListener("click", async () => { if (!window.confirm("Clear the current-chat override?")) return; try { await api("/api/admin/tone/override", {method: "DELETE"}); await loadTone(); feedback("Chat override cleared."); } catch (error) { feedback(error.message, true); }});
byId("logs-refresh").addEventListener("click", () => loadLogs().catch((error) => feedback(error.message, true)));

byId("delete-user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const userId = Number(byId("delete-user-id").value);
  if (!window.confirm(`Delete retained data for user ${userId}?`)) return;
  try { await api(`/api/admin/users/${userId}?purge_messages=${byId("delete-user-messages").checked}`, {method: "DELETE"}); event.target.reset(); await Promise.all([loadLists(), loadLogs()]); feedback("User data deleted."); } catch (error) { feedback(error.message, true); }
});

byId("purge-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const confirmation = byId("purge-confirmation").value;
  if (confirmation !== "PURGE ALL CHAT DATA" || !window.confirm("Permanently purge retained chat history and jobs?")) return;
  try { await api("/api/admin/logs", {method: "DELETE", body: JSON.stringify({confirmation})}); event.target.reset(); await loadLogs(); feedback("Chat history and indexed jobs purged."); } catch (error) { feedback(error.message, true); }
});

byId("owner-contact").addEventListener("input", renderPrivacyNotice);
byId("copy-notice").addEventListener("click", async () => {
  const contact = byId("owner-contact").value.trim();
  if (!contact) { feedback("Enter a monitored owner contact before copying.", true); return; }
  try { await navigator.clipboard.writeText(byId("privacy-notice").value); feedback("Privacy notice copied."); } catch (_error) { feedback("Copy failed; select the notice manually.", true); }
});
byId("logout").addEventListener("click", async () => { try { await api("/api/auth/logout", {method: "POST"}); window.location.assign("/"); } catch (error) { feedback(error.message, true); }});

async function boot() {
  try {
    const config = await fetch("/api/public/config", {credentials: "same-origin"}).then((response) => response.json());
    byId("status").textContent = config.telegram_bot_username ? `Managing @${config.telegram_bot_username}` : "Bot configuration is incomplete.";
    const response = await fetch("/api/admin/me", {credentials: "same-origin"});
    if (response.status === 401) return;
    if (!response.ok) throw new Error("Admin service is unavailable.");
    state.me = await response.json();
    state.csrf = state.me.csrf_token;
    byId("role").textContent = `Signed in as ${state.me.name || state.me.user_id} (${state.me.role})`;
    byId("login-panel").hidden = true;
    byId("admin-panel").hidden = false;
    await loadAll();
  } catch (error) { byId("status").textContent = error.message; }
}

boot();
