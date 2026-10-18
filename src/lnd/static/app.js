"use strict";

// Review UI. Everything that comes from the server is put on the page with textContent or
// input values, never innerHTML, because the page text is model output.

const $ = (id) => document.getElementById(id);

let tenant = "default";
try {
  tenant = localStorage.getItem("lnd-tenant") || "default";
} catch (_) { /* storage can be blocked; the default is fine */ }

let currentDoc = null;
let currentPageId = null;
let currentPageStatus = null; // status of the selected page when its record was last loaded
let events = null;
let refreshTimer = null;

function withTenant(path) {
  return path + (path.includes("?") ? "&" : "?") + "tenant=" + encodeURIComponent(tenant);
}

async function api(path, options = {}) {
  const headers = Object.assign({ "X-Tenant": tenant }, options.headers || {});
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

// ---- documents -----------------------------------------------------------------------------
async function refreshDocs() {
  const docs = await api("/api/documents");
  const list = $("docs");
  list.replaceChildren();
  if (docs.length === 0) list.append(el("li", { class: "muted", text: "Nothing uploaded yet." }));
  for (const d of docs) {
    const finished = Number(d.pages_done) + Number(d.pages_failed);
    const li = el("li", { class: d.id === currentDoc ? "active" : "" },
      el("div", { text: d.filename }),
      el("div", { class: "meta", text: `${finished}/${d.page_count} pages, ${d.status}` }));
    li.addEventListener("click", () => selectDoc(d.id));
    list.append(li);
  }
}

async function selectDoc(id) {
  currentDoc = id;
  currentPageId = null;
  $("workspace").hidden = false;
  $("review").hidden = true;
  $("export-json").href = withTenant(`/api/documents/${id}/export.json`);
  $("export-csv").href = withTenant(`/api/documents/${id}/export.csv`);
  await loadDoc();
  connectEvents(id);
  refreshDocs();
}

async function loadDoc() {
  if (!currentDoc) return;
  const doc = await api(`/api/documents/${currentDoc}`);
  $("doc-title").textContent = doc.filename;
  const by = doc.pages_by_status;
  const finished = by.done + by.failed;
  $("bar").style.width = `${(100 * finished) / doc.page_count}%`;
  $("doc-status").textContent =
    `${by.done} done, ${by.failed} failed, ${by.processing + by.queued} waiting of ${doc.page_count}` +
    (doc.status === "completed" ? " (finished)" : "");
  const chips = $("page-chips");
  chips.replaceChildren();
  for (const p of doc.pages) {
    const chip = el("div", {
      class: `chip ${p.status}${p.id === currentPageId ? " sel" : ""}`,
      title: p.error || p.title || p.status,
      text: String(p.page_no),
    });
    chip.addEventListener("click", () => selectPage(p.id));
    chips.append(chip);
  }
  if (!currentPageId && doc.pages.length === 1) selectPage(doc.pages[0].id);
  // The page on screen has finished (or failed, or was requeued) since its record was loaded:
  // show the new state without waiting for a click. A PDF page's image only exists once a
  // worker has rendered it, so retry the image too if it has not loaded yet.
  const current = doc.pages.find((p) => p.id === currentPageId);
  if (current && currentPageStatus !== null && current.status !== currentPageStatus) {
    currentPageStatus = current.status;
    const img = $("page-img");
    if (!img.naturalWidth) img.src = withTenant(`/api/pages/${current.id}/image`);
    loadRecord().catch(() => {});
  }
}

function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    loadDoc().catch(() => {});
    refreshUsage().catch(() => {});
  }, 300);
}

// ---- progress stream -----------------------------------------------------------------------
// EventSource reconnects by itself and sends Last-Event-ID, so after a dropped connection or an
// API restart the server replays exactly the events we have not seen.
function connectEvents(id) {
  if (events) events.close();
  events = new EventSource(withTenant(`/api/documents/${id}/events`));
  const names = ["document_created", "page_done", "page_failed", "page_requeued", "document_done"];
  for (const name of names) {
    events.addEventListener(name, () => {
      scheduleRefresh();
      if (name === "document_done") {
        events.close();
        refreshDocs().catch(() => {});
      }
    });
  }
  events.onerror = () => {
    $("doc-status").textContent += " (reconnecting...)";
  };
}

// ---- review pane ---------------------------------------------------------------------------
async function selectPage(id) {
  currentPageId = id;
  currentPageStatus = null;
  $("review").hidden = false;
  $("page-img").classList.remove("full");
  $("page-img").src = withTenant(`/api/pages/${id}/image`);
  scheduleRefresh();
  await loadRecord();
}

async function loadRecord() {
  const r = await api(`/api/pages/${currentPageId}/record`);
  currentPageStatus = r.page.status;
  const state = $("page-state");
  $("save-msg").textContent = "";
  if (!r.record) {
    $("record-form").hidden = true;
    state.textContent =
      r.page.status === "failed" ? `This page failed: ${r.page.error || "unknown error"}` : "Waiting to be read by the model...";
    if (r.page.status === "failed") {
      const btn = el("button", { type: "button", text: "Retry page" });
      btn.addEventListener("click", async () => {
        await api(`/api/pages/${currentPageId}/retry`, { method: "POST" });
        scheduleRefresh();
        loadRecord();
      });
      state.append(" ", btn);
    }
    return;
  }
  state.textContent = `Version ${r.record.version} (${r.record.source})`;
  $("record-form").hidden = false;
  renderForm(r.record.data);
  renderChecks(r.checks);
  await loadHistory();
}

function renderForm(data) {
  $("f-title").value = data.title || "";
  $("f-date").value = data.date || "";
  $("f-narrative").value = data.narrative || "";
  $("f-notes").value = data.notes || "";
  const body = $("reagents").querySelector("tbody");
  body.replaceChildren();
  for (const r of data.reagents || []) addReagentRow(r);
}

function addReagentRow(r = {}) {
  const cell = (value, cls) => {
    const input = el("input", { class: cls });
    input.value = value === null || value === undefined ? "" : String(value);
    return el("td", {}, input);
  };
  const remove = el("button", { type: "button", text: "x", title: "Remove row" });
  const tr = el("tr", {},
    cell(r.name, "r-name"), cell(r.amount, "r-amount"), cell(r.unit, "r-unit"),
    cell(r.molar_mass_g_mol, "r-mm"), cell(r.equivalents, "r-eq"), el("td", {}, remove));
  remove.addEventListener("click", () => tr.remove());
  $("reagents").querySelector("tbody").append(tr);
}

function readForm() {
  const num = (text, label) => {
    const t = text.trim();
    if (t === "") return null;
    const n = Number(t);
    if (!Number.isFinite(n)) throw new Error(`${label} must be a number: "${t}"`);
    return n;
  };
  const date = $("f-date").value.trim();
  if (date && !/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new Error("Date must look like 2025-03-14");
  const reagents = [];
  for (const tr of $("reagents").querySelectorAll("tbody tr")) {
    const get = (cls) => tr.querySelector("." + cls).value;
    if (get("r-name").trim() === "") throw new Error("Every reagent row needs a name");
    reagents.push({
      name: get("r-name").trim(),
      amount: num(get("r-amount"), "Amount"),
      unit: get("r-unit").trim() || null,
      molar_mass_g_mol: num(get("r-mm"), "Molar mass"),
      equivalents: num(get("r-eq"), "Equivalents"),
    });
  }
  return {
    title: $("f-title").value,
    date: date || null,
    narrative: $("f-narrative").value,
    reagents,
    notes: $("f-notes").value,
  };
}

async function save() {
  const msg = $("save-msg");
  try {
    const data = readForm();
    const body = JSON.stringify({
      data,
      editor: $("f-editor").value.trim() || "reviewer",
      note: $("f-note").value.trim(),
    });
    const res = await api(`/api/pages/${currentPageId}/record`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body,
    });
    msg.textContent = `Saved as version ${res.version}.`;
    $("f-note").value = "";
    await loadRecord();
    $("save-msg").textContent = `Saved as version ${res.version}.`;
  } catch (err) {
    msg.textContent = `Not saved: ${err.message}`;
  }
}

function renderChecks(checks) {
  const box = $("checks");
  box.replaceChildren();
  if (!checks) {
    box.textContent = "No checks for this page.";
    return;
  }
  const r = checks.result;
  if (r.status === "ok" && r.passed) {
    box.append(el("div", { class: "check-ok", text: "All checks passed." }));
  } else if (r.status === "ok") {
    box.append(el("div", { class: "check-issue", text: "The check found problems:" }));
    const ul = el("ul");
    for (const issue of r.issues) ul.append(el("li", { text: issue }));
    box.append(ul);
  } else {
    box.append(el("div", { class: "check-err", text: `Check did not run: ${r.error || r.status}` }));
  }
  if (checks.code) {
    box.append(el("details", {}, el("summary", { text: "Show the model-written code" }),
      el("pre", { text: checks.code })));
  }
}

async function loadHistory() {
  const versions = await api(`/api/pages/${currentPageId}/versions`);
  const ul = $("history");
  ul.replaceChildren();
  for (const v of versions) {
    const when = new Date(v.created_at).toLocaleString();
    const who = v.source === "model" ? "model" : v.editor || "reviewer";
    const load = el("button", { type: "button", text: "Load into form" });
    load.addEventListener("click", async () => {
      const full = await api(`/api/pages/${currentPageId}/versions/${v.version}`);
      renderForm(full.data);
      $("save-msg").textContent =
        `Loaded version ${v.version} into the form. Save to make it the current version.`;
    });
    ul.append(el("li", {},
      el("span", { text: `v${v.version} · ${who} · ${when}${v.note ? " · " + v.note : ""}` }), load));
  }
}

// ---- usage and upload ----------------------------------------------------------------------
async function refreshUsage() {
  const u = await fetch(`/api/tenants/${encodeURIComponent(tenant)}/usage`).then((r) => r.json());
  const budget = u.token_budget === null ? "no limit" : u.token_budget.toLocaleString();
  $("usage").textContent = `${u.tokens_used.toLocaleString()} tokens used (budget: ${budget})`;
}

$("upload-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const file = $("file").files[0];
  if (!file) return;
  const msg = $("upload-msg");
  msg.textContent = "Uploading...";
  try {
    const form = new FormData();
    form.append("file", file);
    const doc = await api("/api/documents", { method: "POST", body: form });
    msg.textContent = `Queued ${doc.page_count} page${doc.page_count === 1 ? "" : "s"}.`;
    $("file").value = "";
    await selectDoc(doc.id);
  } catch (err) {
    msg.textContent = `Upload failed: ${err.message}`;
  }
});

$("tenant").value = tenant;
$("tenant").addEventListener("change", () => {
  tenant = $("tenant").value.trim() || "default";
  try { localStorage.setItem("lnd-tenant", tenant); } catch (_) { /* ignore */ }
  if (events) events.close();
  currentDoc = null;
  currentPageId = null;
  $("workspace").hidden = true;
  refreshDocs().catch(() => {});
  refreshUsage().catch(() => {});
});
$("add-row").addEventListener("click", () => addReagentRow());
$("save").addEventListener("click", save);
$("page-img").addEventListener("click", (e) => e.target.classList.toggle("full"));

refreshDocs().catch(() => {});
refreshUsage().catch(() => {});
