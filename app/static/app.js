// PermitPilot dashboard. Plain JavaScript: fetch the JSON API and re-render on every change.
"use strict";

const state = { ordinance: null, apps: [], current: null };
const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.add("hidden"), 4000);
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json();
  if (!res.ok) {
    const err = new Error(data?.message || data?.detail?.[0]?.msg || data?.detail || res.statusText);
    err.details = data?.details || [];
    throw err;
  }
  return data;
}

const fmt = (iso) => (iso ? new Date(iso + "Z").toLocaleString() : "");

// ---- applications list ---------------------------------------------------------------------

async function loadApps() {
  state.apps = await api("/api/applications");
  $("#app-list").innerHTML = state.apps.length
    ? state.apps.map((a) => `
      <li><button data-id="${a.id}" aria-current="${state.current?.id === a.id}">
        <strong>#${a.id} ${esc(a.permit_type)}</strong> · ${esc(a.project_address)}<br>
        <span class="badge ${esc(a.state)}">${esc(a.state.replace("_", " "))}</span>
      </button></li>`).join("")
    : `<li class="muted">No applications yet.</li>`;
}

$("#app-list").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-id]");
  if (btn) openApp(Number(btn.dataset.id));
});

$("#new-app-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = new FormData(e.target);
  try {
    const app = await api("/api/applications", {
      method: "POST",
      body: { ordinance_id: state.ordinance.id, ...Object.fromEntries(form) },
    });
    e.target.reset();
    $("#new-app").open = false;
    state.current = app;
    renderDetail();
    await loadApps();
  } catch (err) {
    toast(err.message);
  }
});

// ---- application detail --------------------------------------------------------------------

async function openApp(id) {
  try {
    state.current = await api(`/api/applications/${id}`);
    renderDetail();
    await loadApps();
  } catch (err) {
    toast(err.message);
  }
}

function renderDetail() {
  const a = state.current;
  if (!a) return;
  const pct = a.progress.total ? Math.round((100 * a.progress.done) / a.progress.total) : 0;
  const groups = {};
  for (const item of a.checklist) (groups[item.category] ||= []).push(item);
  const locked = ["closed", "rejected", "withdrawn"].includes(a.state);

  $("#detail").innerHTML = `
    <h2>#${a.id} ${esc(a.permit_type)} permit · ${esc(a.project_address)}
      <span class="badge ${esc(a.state)}">${esc(a.state.replace("_", " "))}</span></h2>
    <p class="muted">${esc(a.contractor_name)} · ${esc(a.contractor_email)}
      ${a.permit_number ? ` · Permit <strong>${esc(a.permit_number)}</strong>, expires ${fmt(a.expires_at)}` : ""}
      ${a.inspection_at ? ` · Inspection ${fmt(a.inspection_at)}` : ""}</p>
    <div class="progress" aria-label="Checklist progress"><div style="width:${pct}%"></div></div>
    <p class="muted">${a.progress.done} of ${a.progress.total} checklist items done</p>

    <div class="actions">
      ${a.actions.map((act) => `
        <button data-action="${esc(act.action)}" ${act.blocked_by.length ? "disabled" : ""}
          class="${act.action === "withdraw" ? "secondary" : ""}"
          title="${esc(act.blocked_by.join("\n"))}">${esc(act.action.replace("_", " "))}</button>`).join("")}
    </div>
    ${a.actions.filter((x) => x.blocked_by.length).map((x) => `
      <p class="muted">To <strong>${esc(x.action)}</strong>, finish:</p>
      <ul class="blocked">${x.blocked_by.map((b) => `<li>${esc(b)}</li>`).join("")}</ul>`).join("")}

    ${Object.entries(groups).map(([cat, items]) => `
      <h3>${esc(cat)}</h3>
      <ul class="checklist">${items.map((i) => `
        <li><input type="checkbox" data-item="${i.id}" ${i.done ? "checked" : ""} ${locked ? "disabled" : ""}
              id="item-${i.id}">
            <label for="item-${i.id}" style="margin:0">${esc(i.description)}
              <span class="cite">${esc(i.citation)}</span></label></li>`).join("")}
      </ul>`).join("")}

    <h3>Timeline</h3>
    <ul>${a.events.map((ev) => `<li>${fmt(ev.at)}: <strong>${esc(ev.action)}</strong>
        (${esc(ev.from_state)} → ${esc(ev.to_state)})${ev.note ? `: ${esc(ev.note)}` : ""}</li>`).join("")
      || `<li class="muted">No transitions yet.</li>`}</ul>

    <h3>Scheduled reminders</h3>
    <div class="scroll"><table>
      <tr><th>Job</th><th>Runs at</th><th>Status</th><th>Attempts</th></tr>
      ${a.jobs.map((j) => `<tr><td>${esc((j.dedupe_key || j.kind).split(":")[2] || j.kind)}</td>
        <td>${fmt(j.run_at)}</td><td>${esc(j.status)}</td><td>${j.attempts}/${j.max_attempts}</td></tr>`).join("")
      || `<tr><td colspan="4" class="muted">None</td></tr>`}
    </table></div>`;
}

$("#detail").addEventListener("change", async (e) => {
  const box = e.target.closest("input[data-item]");
  if (!box) return;
  try {
    state.current = await api(`/api/applications/${state.current.id}/checklist/${box.dataset.item}`, {
      method: "PATCH", body: { done: box.checked },
    });
    renderDetail();
  } catch (err) {
    box.checked = !box.checked;
    toast(err.message);
  }
});

$("#detail").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const action = btn.dataset.action;
  const body = { action, expected_version: state.current.version };
  if (["request_corrections", "reject", "fail_inspection"].includes(action)) {
    body.note = prompt(`Note for "${action.replace("_", " ")}":`);
    if (!body.note) return;
  }
  if (action === "schedule_inspection") {
    const when = prompt("Inspection date and time (YYYY-MM-DD HH:MM, UTC):");
    if (!when) return;
    body.inspection_at = when.replace(" ", "T");
  }
  try {
    state.current = await api(`/api/applications/${state.current.id}/transitions`, { method: "POST", body });
    renderDetail();
    await loadApps();
  } catch (err) {
    toast([err.message, ...err.details].join(" · "));
    if (err.message.includes("changed")) openApp(state.current.id);
  }
});

// ---- search ----------------------------------------------------------------------------------

$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const box = $("#search-results");
  try {
    const hits = await api(`/api/search?q=${encodeURIComponent($("#search-q").value)}&k=3`);
    box.innerHTML = `<h2>From the ordinance</h2>` + (hits.length
      ? hits.map((h) => `<div class="hit"><strong>§ ${esc(h.section)} ${esc(h.heading)}</strong>
          <span class="muted">(${esc(h.jurisdiction)}, score ${h.score.toFixed(2)})</span>
          ${h.excerpt.map((s) => `<q>${esc(s)}</q>`).join("")}</div>`).join("")
      : `<p class="muted">No matching sections.</p>`);
    box.classList.remove("hidden");
  } catch (err) {
    toast(err.message);
  }
});

// ---- reminder worker -------------------------------------------------------------------------

async function loadNotifications() {
  const notes = await api("/api/notifications");
  $("#notifications").innerHTML = notes.length
    ? `<div class="scroll"><table><tr><th>Sent</th><th>To</th><th>Subject</th></tr>${notes.map((n) => `
        <tr><td>${fmt(n.created_at)}</td><td>${esc(n.recipient)}</td>
        <td title="${esc(n.body)}">${esc(n.subject)}</td></tr>`).join("")}</table></div>`
    : `<p class="muted">No reminders sent yet. Issue a permit, then run the worker as of a date ~5 months out.</p>`;
}

$("#run-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const asOf = $("#as-of").value;
  try {
    const r = await api(`/api/jobs/run-due${asOf ? `?as_of=${encodeURIComponent(asOf)}` : ""}`, { method: "POST" });
    $("#run-result").textContent = `claimed ${r.claimed}, done ${r.done}, retried ${r.retried}, dead ${r.dead}`;
    await loadNotifications();
    if (state.current) openApp(state.current.id);
  } catch (err) {
    toast(err.message);
  }
});

// ---- boot ------------------------------------------------------------------------------------

(async function boot() {
  try {
    const ordinances = await api("/api/ordinances");
    state.ordinance = ordinances[0];
    $("#permit-type").innerHTML = (state.ordinance?.permit_types || [])
      .map((t) => `<option>${esc(t)}</option>`).join("");
    await Promise.all([loadApps(), loadNotifications()]);
  } catch (err) {
    toast(`Could not reach the API: ${err.message}`);
  }
})();
