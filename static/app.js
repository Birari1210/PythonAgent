/* Research Synthesist — single-page UI. Talks only to the local backend API; holds no secrets. */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = (u) => (/^https?:\/\//i.test(u || "") ? u : "");
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—");
const fmtDay = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" }) : "—");

const SOURCE_TYPES = ["journal-article", "review", "preprint", "conference", "book", "dataset", "documentation", "web", "other"];
const CAT = { established: ["Well-established", "ok"], contested: ["Contested", "warn"], single_study: ["Single-study / limited", "info"] };
const STATUS_PILL = { completed: "ok", partial: "warn", running: "accent", failed: "bad", cancelled: "bad", draft: "" };
const WEIGHT_PILL = { High: "ok", Moderate: "info", Low: "warn", None: "bad" };

const state = { status: null, poll: null, draft: null, frame: null };

// ------------------------------------------------------------------ infrastructure
async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body instanceof FormData) init.body = opts.body;
  else if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  let res;
  try { res = await fetch(path, init); } catch (e) { throw new Error("Cannot reach the backend server. Is it running?"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.detail) || `Request failed (${res.status})`);
  return data;
}

function toast(msg, kind = "info", ms = 5000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function guard(btn, fn) {
  if (btn) btn.disabled = true;
  try { return await fn(); } catch (e) { toast(e.message, "error", 8000); } finally { if (btn) btn.disabled = false; }
}

function pill(text, kind = "") { return `<span class="pill ${kind}">${esc(text)}</span>`; }
function cites(labels, sources) {
  return (labels || []).map((l) => {
    const s = sources && sources.find((x) => x.label === l);
    const url = s ? safeUrl(s.url || (s.doi ? `https://doi.org/${s.doi}` : "")) : "";
    const title = s ? esc(s.title) : "";
    return url ? `<a class="cite" href="${esc(url)}" target="_blank" rel="noopener" title="${title}">${esc(l)}</a>` : `<span class="cite" title="${title}">${esc(l)}</span>`;
  }).join("");
}

// Minimal, escaping Markdown renderer for reports (headings, lists, tables, code, quotes, links, emphasis).
function inlineMd(t) {
  let s = esc(t);
  s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
  s = s.replace(/\[([^\]]+)\]\(([^)\s]*)\)/g, (m, txt, url) => {
    const u = safeUrl(url.replace(/&amp;/g, "&"));
    return u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${txt}</a>` : txt;
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>").replace(/(^|\W)_([^_]+)_(?=\W|$)/g, "$1<em>$2</em>");
  return s;
}
function renderMarkdown(md) {
  const lines = md.split("\n"); const out = []; let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      const buf = []; i++;
      while (i < lines.length && !lines[i].startsWith("```")) buf.push(lines[i++]);
      out.push(`<pre>${esc(buf.join("\n"))}</pre>`); i++; continue;
    }
    if (line.startsWith("|")) {
      const rows = [];
      while (i < lines.length && lines[i].startsWith("|")) rows.push(lines[i++]);
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
      const body = rows.filter((r) => !/^\|[\s\-|]+\|$/.test(r.trim()));
      const [head, ...rest] = body;
      out.push(`<div class="table-wrap"><table><thead><tr>${cells(head).map((c) => `<th>${inlineMd(c)}</th>`).join("")}</tr></thead><tbody>${rest.map((r) => `<tr>${cells(r).map((c) => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
      continue;
    }
    if (/^\s*- /.test(line)) {
      let html = "<ul>"; let depth = 0;
      while (i < lines.length && /^\s*- /.test(lines[i])) {
        const d = Math.floor((lines[i].match(/^\s*/)[0].length) / 2);
        while (d > depth) { html += "<ul>"; depth++; }
        while (d < depth) { html += "</ul>"; depth--; }
        html += `<li>${inlineMd(lines[i].replace(/^\s*- /, ""))}</li>`; i++;
      }
      while (depth-- > 0) html += "</ul>";
      out.push(html + "</ul>"); continue;
    }
    const h = line.match(/^(#{1,4}) (.*)/);
    if (h) out.push(`<h${h[1].length}>${inlineMd(h[2])}</h${h[1].length}>`);
    else if (line.startsWith("> ")) out.push(`<blockquote>${inlineMd(line.slice(2))}</blockquote>`);
    else if (line.trim()) out.push(`<p>${inlineMd(line)}</p>`);
    i++;
  }
  return out.join("\n");
}

function stopPoll() { if (state.poll) { clearTimeout(state.poll); state.poll = null; } }

// ------------------------------------------------------------------ router
const routes = [
  [/^#?\/?$/, () => viewDashboard()],
  [/^#\/new$/, () => viewNew()],
  [/^#\/history$/, () => viewHistory()],
  [/^#\/settings$/, () => viewSettings()],
  [/^#\/session\/([a-f0-9]+)(?:\/(\w+))?$/, (m) => viewSession(m[1], m[2] || "progress")],
];

async function route() {
  stopPoll();
  const hash = location.hash || "#/";
  const key = hash.startsWith("#/session") ? "history" : (hash.replace(/^#\/?/, "") || "dashboard");
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === key));
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) {
      try { await fn(m); } catch (e) { $("#view").innerHTML = `<div class="banner bad"><strong>Error:</strong> ${esc(e.message)}</div>`; }
      return;
    }
  }
  $("#view").innerHTML = `<div class="empty">Page not found. <a href="#/">Go to dashboard</a></div>`;
}

async function loadStatus() {
  try {
    state.status = await api("/api/status");
    const llm = state.status.llm;
    $("#foot-status").innerHTML = `${llm.configured ? "🟢" : "🟠"} Groq ${llm.configured ? esc(llm.model) : "not configured"}<br>` +
      `${state.status.providers.filter((p) => p.available && state.status.enabled_providers.includes(p.name)).length} providers enabled`;
  } catch (e) { $("#foot-status").textContent = "Backend unreachable"; }
}

function llmBanner() {
  const s = state.status;
  if (!s || s.llm.configured) return "";
  return `<div class="banner warn"><strong>Groq API key not configured.</strong> ${esc(s.llm.setup_help)} <a href="#/settings">Open Settings →</a></div>`;
}

// ------------------------------------------------------------------ dashboard
async function viewDashboard() {
  const sessions = await api("/api/sessions");
  const withReports = sessions.filter((s) => s.reports > 0);
  const running = sessions.filter((s) => s.running);
  $("#view").innerHTML = `
    <h1>Dashboard</h1>
    ${llmBanner()}
    <div class="card">
      <h2>Quick-start research</h2>
      <textarea id="qs-question" placeholder="e.g. Does retrieval-augmented generation reduce hallucination in LLM question answering compared with fine-tuning?"></textarea>
      <div class="row" style="margin-top:10px">
        <button class="primary" id="qs-start">▶ Start with defaults</button>
        <button id="qs-custom">Customize search…</button>
        <span class="muted small">Defaults: enabled providers, all dates, all source types.</span>
      </div>
    </div>
    <div class="grid g4">
      <div class="card tight"><div class="stat">${sessions.length}</div><div class="stat-label">Sessions</div></div>
      <div class="card tight"><div class="stat">${withReports.length}</div><div class="stat-label">With reports</div></div>
      <div class="card tight"><div class="stat">${sessions.reduce((a, s) => a + (s.included || 0), 0)}</div><div class="stat-label">Included sources</div></div>
      <div class="card tight"><div class="stat">${running.length}</div><div class="stat-label">Running now</div></div>
    </div>
    <div class="grid g2">
      <div><h2>Recent sessions</h2>${sessions.length ? sessions.slice(0, 6).map(sessionCard).join("") : `<div class="card empty">No research yet. Start one above.</div>`}</div>
      <div><h2>Saved reports</h2>${withReports.length ? `<div class="card">${withReports.slice(0, 10).map((s) => `
        <div class="row between" style="padding:6px 0;border-bottom:1px solid var(--border)">
          <a href="#/session/${s.id}/report">${esc(s.title)}</a>
          <span class="row">${s.confidence ? pill(s.confidence + " confidence", s.confidence === "High" ? "ok" : s.confidence === "Moderate" ? "warn" : "bad") : pill("not synthesized")}<span class="muted tiny">${fmtDay(s.updated_at)}</span></span>
        </div>`).join("")}</div>` : `<div class="card empty">Reports appear here after a run completes.</div>`}</div>
    </div>`;
  $("#qs-start").onclick = (e) => guard(e.currentTarget, async () => {
    const q = $("#qs-question").value.trim();
    if (q.length < 5) throw new Error("Enter a research question (at least 5 characters).");
    const s = await api("/api/sessions", { method: "POST", body: { question: q } });
    location.hash = `#/session/${s.id}/progress`;
  });
  $("#qs-custom").onclick = () => { state.draft = { question: $("#qs-question").value.trim() }; state.frame = null; location.hash = "#/new"; };
  $$(".session-card").forEach((el) => (el.onclick = () => (location.hash = `#/session/${el.dataset.id}/progress`)));
}

function sessionCard(s) {
  return `<div class="card tight session-card" data-id="${s.id}">
    <div class="row between"><h3>${esc(s.title)}</h3>${pill(s.running ? "running" : s.status, STATUS_PILL[s.running ? "running" : s.status])}</div>
    <div class="muted small">${esc(s.question.slice(0, 160))}</div>
    <div class="row small muted" style="margin-top:6px">
      <span>${s.included || 0} included</span>·<span>${s.reports} report(s)</span>·<span>${s.confidence ? esc(s.confidence) + " confidence" : "no confidence yet"}</span>·<span>updated ${fmtDay(s.updated_at)}</span>
    </div></div>`;
}

// ------------------------------------------------------------------ new research
async function viewNew() {
  await loadStatus();
  const st = state.status;
  const d = state.draft || (state.draft = {});
  d.providers = d.providers || st.enabled_providers.filter((p) => st.providers.find((x) => x.name === p && x.available));
  d.source_types = d.source_types || [];
  d.queries = d.queries || [];
  d.reuse = d.reuse || [];
  $("#view").innerHTML = `
    <h1>New research</h1>
    ${llmBanner()}
    <div class="card">
      <label class="field"><span>Research question</span>
        <textarea id="n-question" placeholder="State the subject, the comparison or intervention, and the outcome you care about.">${esc(d.question || "")}</textarea></label>
      <div class="row" style="margin-top:10px">
        <button id="n-frame">🧭 Analyze question & suggest queries</button>
        <span class="muted small">Optional — identifies subject/comparison/outcome and proposes search queries you can edit.</span>
      </div>
      <div id="n-framing"></div>
    </div>
    <div class="card">
      <h2>Search scope</h2>
      <div class="grid g3">
        <label class="field"><span>Published from (year)</span><input type="number" id="n-from" min="1800" max="2100" value="${esc(d.year_from || "")}" placeholder="any"></label>
        <label class="field"><span>Published to (year)</span><input type="number" id="n-to" min="1800" max="2100" value="${esc(d.year_to || "")}" placeholder="present"></label>
        <label class="field"><span>Max sources to include</span><input type="number" id="n-max" min="3" max="60" value="${esc(d.max_sources || "")}" placeholder="settings default"></label>
      </div>
      <div style="margin-top:14px"><strong class="small">Research providers</strong><div>${st.providers.map((p) => `
        <label class="check ${p.available ? "" : "disabled"}" title="${esc(p.usage_note)}${p.unavailable_reason ? " — " + esc(p.unavailable_reason) : ""}">
          <input type="checkbox" name="prov" value="${p.name}" ${d.providers.includes(p.name) ? "checked" : ""} ${p.available ? "" : "disabled"}> ${esc(p.label)}${p.available ? "" : ` <span class="tiny muted">(${esc(p.unavailable_reason)})</span>`}</label>`).join("")}</div></div>
      <div style="margin-top:10px"><strong class="small">Source types</strong> <span class="muted tiny">(none selected = all)</span><div>${SOURCE_TYPES.map((t) => `
        <label class="check"><input type="checkbox" name="stype" value="${t}" ${d.source_types.includes(t) ? "checked" : ""}> ${t}</label>`).join("")}</div></div>
      <div class="grid g2" style="margin-top:10px">
        <label class="field"><span>Inclusion terms (comma-separated; a source must mention at least one)</span><input type="text" id="n-inc" value="${esc((d.include_terms || []).join(", "))}"></label>
        <label class="field"><span>Exclusion terms (comma-separated; sources mentioning any are excluded)</span><input type="text" id="n-exc" value="${esc((d.exclude_terms || []).join(", "))}"></label>
      </div>
    </div>
    <div class="row"><button class="primary" id="n-start">▶ Start research</button><span class="muted small" id="n-hint"></span></div>`;
  if (state.frame) renderFraming();
  const collect = () => {
    d.question = $("#n-question").value.trim();
    d.year_from = $("#n-from").value ? +$("#n-from").value : null;
    d.year_to = $("#n-to").value ? +$("#n-to").value : null;
    d.max_sources = $("#n-max").value ? +$("#n-max").value : null;
    d.providers = $$("input[name=prov]:checked").map((x) => x.value);
    d.source_types = $$("input[name=stype]:checked").map((x) => x.value);
    const split = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
    d.include_terms = split($("#n-inc").value); d.exclude_terms = split($("#n-exc").value);
  };
  $("#n-frame").onclick = (e) => { const btn = e.currentTarget; return guard(btn, async () => {
    collect();
    if (d.question.length < 5) throw new Error("Enter a research question first.");
    btn.innerHTML = `<span class="spin">⟳</span> Analyzing…`;
    try {
      state.frame = await api("/api/frame", { method: "POST", body: { question: d.question } });
      d.queries = [...state.frame.framing.queries];
      renderFraming();
    } finally { btn.innerHTML = "🧭 Analyze question & suggest queries"; }
  }); };
  $("#n-start").onclick = (e) => guard(e.currentTarget, async () => {
    collect();
    if (d.question.length < 5) throw new Error("Enter a research question (at least 5 characters).");
    if (!d.providers.length) throw new Error("Select at least one research provider.");
    const body = { question: d.question, year_from: d.year_from, year_to: d.year_to, providers: d.providers, source_types: d.source_types,
      include_terms: d.include_terms, exclude_terms: d.exclude_terms, queries: d.queries, max_sources: d.max_sources, reuse_session_ids: d.reuse };
    if (state.frame && state.frame.framing.method && !state.frame.framing.method.startsWith("heuristic")) body.framing = { ...state.frame.framing, queries: d.queries };
    const s = await api("/api/sessions", { method: "POST", body });
    state.draft = null; state.frame = null;
    location.hash = `#/session/${s.id}/progress`;
  });
}

function renderFraming() {
  const d = state.draft, f = state.frame.framing, el = $("#n-framing");
  el.innerHTML = `
    ${state.frame.warning ? `<div class="banner warn" style="margin-top:12px">${esc(state.frame.warning)}</div>` : ""}
    <div class="grid g2" style="margin-top:14px">
      <div class="kv" style="margin:0"><dt>Subject</dt><dd>${esc(f.subject)}</dd><dt>Comparison</dt><dd>${esc(f.comparison || "none stated")}</dd>
        <dt>Outcome</dt><dd>${esc(f.outcome || "none stated")}</dd><dt>Scope</dt><dd>${esc(f.scope || "—")}</dd>
        <dt>Sufficient evidence</dt><dd>${esc(f.sufficient_evidence || "—")}</dd><dt>Method</dt><dd class="muted">${esc(f.method)}</dd></div>
      <div>${f.ambiguities && f.ambiguities.length ? `<strong class="small">Could be clearer:</strong><ul class="small">${f.ambiguities.map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : `<span class="muted small">No major ambiguities flagged.</span>`}</div>
    </div>
    <div style="margin-top:12px"><strong class="small">Search queries</strong> <span class="muted tiny">(each query is sent to every selected provider; max 5 used)</span>
      <div id="q-chips">${d.queries.map((q, i) => `<span class="chip">${esc(q)}<button data-i="${i}" title="Remove">✕</button></span>`).join("")}</div>
      <div class="row" style="margin-top:6px"><input type="text" id="q-add" placeholder="Add a query (keywords, not a sentence)" style="max-width:420px"><button id="q-add-btn">Add</button></div>
    </div>
    ${state.frame.related_sessions.length ? `<div style="margin-top:14px"><strong class="small">Related previous research</strong>
      <div class="muted tiny">Reusing copies their included sources into this session for re-screening. Their retrieval dates are kept — old findings are <em>not</em> treated as current.</div>
      ${state.frame.related_sessions.map((s) => `<label class="check" style="display:flex"><input type="checkbox" name="reuse" value="${s.id}" ${d.reuse.includes(s.id) ? "checked" : ""}>
        ${esc(s.title)} <span class="muted tiny">(last run ${fmtDay(s.last_run_at)})</span> <a href="#/session/${s.id}/report" target="_blank">view</a></label>`).join("")}</div>` : ""}`;
  $$("#q-chips button").forEach((b) => (b.onclick = () => { d.queries.splice(+b.dataset.i, 1); renderFraming(); }));
  const add = () => { const v = $("#q-add").value.trim(); if (v && d.queries.length < 6) { d.queries.push(v); renderFraming(); } else if (d.queries.length >= 6) toast("Up to 6 queries.", "error"); };
  $("#q-add-btn").onclick = add;
  $("#q-add").onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } };
  $$("input[name=reuse]").forEach((c) => (c.onchange = () => { d.reuse = $$("input[name=reuse]:checked").map((x) => x.value); }));
}

// ------------------------------------------------------------------ session
const TABS = [["progress", "Progress"], ["sources", "Source explorer"], ["evidence", "Evidence summary"], ["report", "Report"], ["followup", "Follow-up Q&A"]];

async function viewSession(id, tab) {
  const s = await api(`/api/sessions/${id}`);
  const status = s.running ? "running" : s.status;
  $("#view").innerHTML = `
    <div>
      <h1 id="s-title" style="margin-bottom:2px">${esc(s.title)}</h1>
      <div class="muted small">${esc(s.question)}</div>
      <div class="row no-print" style="margin-top:10px">
        ${pill(status, STATUS_PILL[status])}
        ${s.running ? `<button class="danger" id="s-cancel">■ Cancel</button>` : `
          <button id="s-refresh" title="${s.last_run_at ? "Search again for newly published sources" : "Run the search"}">${s.last_run_at ? "↻ Refresh (find new)" : "▶ Run"}</button>
          <button id="s-reanalyze" title="Re-run evaluation + synthesis with current sources">⚗ Re-analyze</button>
          <button id="s-rename">✎ Rename</button><button class="danger" id="s-delete">🗑 Delete</button>`}
      </div>
    </div>
    ${s.error && !s.running ? `<div class="banner bad" style="margin-top:12px"><strong>Last run ${esc(s.status)}:</strong> ${esc(s.error)}</div>` : ""}
    ${s.last_run_at ? `<div class="muted tiny" style="margin-top:6px">Last searched ${fmtDate(s.last_run_at)} — ${staleNote(s.last_run_at)}</div>` : ""}
    <nav class="tabs" style="margin-top:14px">${TABS.map(([k, l]) => `<a href="#/session/${id}/${k}" class="${k === tab ? "active" : ""}">${l}</a>`).join("")}</nav>
    <div id="tab"></div>`;
  wireSessionActions(s);
  const renderers = { progress: tabProgress, sources: tabSources, evidence: tabEvidence, report: tabReport, followup: tabFollowup };
  await (renderers[tab] || tabProgress)(s);
  if (s.running) schedulePoll(id, tab);
}

function staleNote(iso) {
  const days = Math.floor((Date.now() - new Date(iso)) / 864e5);
  return days > 30 ? `<strong>${days} days ago — consider refreshing; newer evidence may exist.</strong>` : `${days} day(s) ago`;
}

function schedulePoll(id, tab) {
  stopPoll();
  state.poll = setTimeout(async () => {
    if (!location.hash.startsWith(`#/session/${id}`)) return;
    try {
      const s = await api(`/api/sessions/${id}`);
      if (!s.running) { toast(`Research ${s.status}.`, s.status === "completed" ? "ok" : "error"); loadStatus(); return viewSession(id, tab); }
      if (tab === "progress") await tabProgress(s);
    } catch (e) { toast(e.message, "error"); }
    schedulePoll(id, tab);
  }, 1500);
}

function wireSessionActions(s) {
  const id = s.id;
  const go = (kind) => (e) => guard(e.currentTarget, async () => { await api(`/api/sessions/${id}/run?kind=${kind}`, { method: "POST" }); location.hash = `#/session/${id}/progress`; route(); });
  if ($("#s-cancel")) $("#s-cancel").onclick = (e) => guard(e.currentTarget, async () => { await api(`/api/sessions/${id}/cancel`, { method: "POST" }); toast("Cancelling after the current step…"); });
  if ($("#s-refresh")) $("#s-refresh").onclick = go("auto");
  if ($("#s-reanalyze")) $("#s-reanalyze").onclick = go("reanalysis");
  if ($("#s-rename")) $("#s-rename").onclick = (e) => guard(e.currentTarget, async () => {
    const t = prompt("New title", s.title); if (!t || !t.trim()) return;
    await api(`/api/sessions/${id}`, { method: "PATCH", body: { title: t.trim() } }); $("#s-title").textContent = t.trim(); toast("Renamed", "ok");
  });
  if ($("#s-delete")) $("#s-delete").onclick = (e) => guard(e.currentTarget, async () => {
    if (!confirm(`Delete "${s.title}" with all its sources and reports? This cannot be undone.`)) return;
    await api(`/api/sessions/${id}`, { method: "DELETE" }); toast("Session deleted", "ok"); location.hash = "#/history";
  });
}

const STAGE_ICON = { pending: "○", running: `<span class="spin">⟳</span>`, done: "✓", warning: "!", failed: "✕", skipped: "–", cancelled: "✕" };

async function tabProgress(s) {
  const run = s.runs[s.runs.length - 1];
  const el = $("#tab");
  if (!run) { el.innerHTML = `<div class="card empty">No runs yet. Click <strong>Run</strong> to start searching.</div>`; return; }
  const c = run.counts || {};
  const log = run.provider_log || [];
  el.innerHTML = `
    <div class="grid g2">
      <div class="card"><div class="row between"><h2>${run.kind === "refresh" ? "Refresh run" : run.kind === "reanalysis" ? "Re-analysis" : "Research run"} #${run.id}</h2>${pill(run.status, STATUS_PILL[run.status] || "")}</div>
        <div class="muted tiny">Started ${fmtDate(run.started_at)}${run.finished_at ? ` · finished ${fmtDate(run.finished_at)}` : ""}</div>
        ${run.stages.map((st) => `<div class="stage ${st.status}"><div class="icon">${STAGE_ICON[st.status] || "○"}</div>
          <div><strong>${esc(st.label)}</strong><div class="small muted">${esc(st.detail || (st.status === "pending" ? "Waiting" : ""))}</div></div>
          <div class="tiny muted">${st.finished_at && st.started_at ? Math.max(0, Math.round((new Date(st.finished_at) - new Date(st.started_at)) / 1000)) + "s" : ""}</div></div>`).join("")}
      </div>
      <div class="card"><h2>Search funnel</h2>
        ${run.kind === "reanalysis" ? `<div class="muted small">Re-analysis reuses the session's included sources.</div>` : `
        <div class="grid g4">${[["retrieved", "Retrieved"], ["unique", "After dedup"], ["screened_in", "Met criteria"], ["included", "Included"]].map(([k, l]) =>
          `<div><div class="stat">${c[k] ?? "–"}</div><div class="stat-label">${l}</div></div>`).join("")}</div>
        ${run.kind === "refresh" ? `<p class="small">${c.new ?? 0} new source(s); ${c.already_known ?? 0} result(s) were already in this session.</p>` : ""}`}
        <h3 style="margin-top:16px">Strategy</h3>
        <div class="kv" style="margin-top:0"><dt>Queries</dt><dd>${(run.strategy.queries || []).map((q) => `<div>“${esc(q)}”</div>`).join("") || "—"}</dd>
          <dt>Providers</dt><dd>${esc((run.strategy.providers || []).join(", ") || "—")}</dd>
          <dt>Date range</dt><dd>${esc(run.strategy.year_from || "any")} – ${esc(run.strategy.year_to || "present")}</dd>
          <dt>Types</dt><dd>${esc((run.strategy.source_types || []).join(", ") || "all")}</dd>
          <dt>Include / exclude</dt><dd>${esc((run.strategy.include_terms || []).join(", ") || "—")} / ${esc((run.strategy.exclude_terms || []).join(", ") || "—")}</dd></div>
      </div>
    </div>
    ${run.limitations && run.limitations.length ? `<div class="card"><h2>Limitations & partial results</h2><ul class="small">${run.limitations.map((l) => `<li>${esc(l)}</li>`).join("")}</ul></div>` : ""}
    ${log.length ? `<div class="card"><h2>Provider requests</h2><div class="table-wrap"><table><thead><tr><th>Provider</th><th>Query</th><th>Results</th><th>Status</th><th>Time</th></tr></thead><tbody>
      ${log.map((p) => `<tr><td>${esc(p.provider)}</td><td>${esc(p.query)}</td><td>${p.count}</td><td>${p.error ? pill(p.error, p.error.startsWith("skipped") ? "" : "bad") : pill("ok", "ok")}</td><td class="muted">${p.elapsed ? p.elapsed + "s" : ""}</td></tr>`).join("")}
    </tbody></table></div></div>` : ""}
    ${s.runs.length > 1 ? `<div class="card"><h2>Run history</h2>${s.runs.slice().reverse().map((r) => `<div class="row small" style="padding:4px 0">${pill(r.status, STATUS_PILL[r.status] || "")} #${r.id} ${esc(r.kind)} · ${fmtDate(r.started_at)} · ${r.counts.included ?? "–"} included${r.kind === "refresh" ? `, ${r.counts.new ?? 0} new` : ""}</div>`).join("")}</div>` : ""}`;
}

async function tabSources(s) {
  const sources = await api(`/api/sessions/${s.id}/sources`);
  const el = $("#tab");
  const f = state.srcFilter || (state.srcFilter = { status: "included", text: "", weight: "" });
  el.innerHTML = `
    <div class="card no-print"><h2>Add a source manually</h2>
      <div class="grid g2">
        <div class="row"><input type="url" id="add-url" placeholder="https://doi.org/10.… , arXiv link, or any article URL"><button id="add-url-btn">Add URL</button></div>
        <div class="row"><input type="file" id="add-file" accept=".pdf,.txt,.md,.markdown,.html,.htm"><button id="add-file-btn">Upload</button></div>
      </div>
      <div class="muted tiny" style="margin-top:6px">DOIs and arXiv links are resolved to scholarly metadata. Added sources are included but not evaluated until you click <strong>Re-analyze</strong>.</div>
    </div>
    <div class="row" style="margin-bottom:12px">
      <select id="f-status" style="width:auto">${["included", "excluded", "duplicate", "all"].map((x) => `<option ${f.status === x ? "selected" : ""}>${x}</option>`).join("")}</select>
      <select id="f-weight" style="width:auto"><option value="">any weight</option>${["High", "Moderate", "Low", "None"].map((x) => `<option ${f.weight === x ? "selected" : ""}>${x}</option>`).join("")}</select>
      <input type="text" id="f-text" placeholder="Filter by title, author, venue…" value="${esc(f.text)}" style="max-width:320px">
      <span class="muted small" id="f-count"></span>
    </div>
    <div id="src-list"></div>`;
  const draw = () => {
    const t = f.text.toLowerCase();
    const list = sources.filter((x) => (f.status === "all" || x.status === f.status) && (!f.weight || (x.evaluation && x.evaluation.weight === f.weight))
      && (!t || [x.title, (x.authors || []).join(" "), x.venue, x.label].join(" ").toLowerCase().includes(t)));
    $("#f-count").textContent = `${list.length} of ${sources.length} records`;
    $("#src-list").innerHTML = list.length ? list.map((x) => sourceCard(x, sources)).join("") : `<div class="card empty">No sources match.</div>`;
    $$("[data-toggle]").forEach((b) => (b.onclick = (e) => guard(e.currentTarget, async () => {
      await api(`/api/sessions/${s.id}/sources/${b.dataset.toggle}?status=${b.dataset.to}`, { method: "PATCH" });
      toast(`Source ${b.dataset.to}. Re-analyze to update the synthesis.`, "ok"); tabSources(s);
    })));
  };
  $("#f-status").onchange = (e) => { f.status = e.target.value; draw(); };
  $("#f-weight").onchange = (e) => { f.weight = e.target.value; draw(); };
  $("#f-text").oninput = (e) => { f.text = e.target.value; draw(); };
  $("#add-url-btn").onclick = (e) => guard(e.currentTarget, async () => {
    const url = $("#add-url").value.trim(); if (!url) throw new Error("Enter a URL.");
    const r = await api(`/api/sessions/${s.id}/sources/url`, { method: "POST", body: { url } });
    toast(`Added ${r.label}: ${r.title}`, "ok"); tabSources(s);
  });
  $("#add-file-btn").onclick = (e) => guard(e.currentTarget, async () => {
    const file = $("#add-file").files[0]; if (!file) throw new Error("Choose a file first.");
    const fd = new FormData(); fd.append("file", file);
    const r = await api(`/api/sessions/${s.id}/sources/upload`, { method: "POST", body: fd });
    toast(`Added ${r.label}: ${r.title}`, "ok"); tabSources(s);
  });
  draw();
}

function sourceCard(x, all) {
  const ev = x.evaluation;
  const url = safeUrl(x.url || (x.doi ? `https://doi.org/${x.doi}` : ""));
  const dupes = all.filter((d) => d.duplicate_of === x.id);
  const original = x.duplicate_of ? all.find((d) => d.id === x.duplicate_of) : null;
  const authors = (x.authors || []).slice(0, 5).join(", ") + ((x.authors || []).length > 5 ? " et al." : "");
  return `<div class="src ${x.status}">
    <div class="row between"><div style="min-width:0;flex:1"><span class="label">${esc(x.label)}</span>
      ${url ? `<a class="title" href="${esc(url)}" target="_blank" rel="noopener">${esc(x.title)}</a>` : `<span class="title">${esc(x.title)}</span>`}</div>
      <div class="row">${x.is_new ? pill("new", "accent") : ""}${x.origin && x.origin.startsWith("manual") ? pill("manual", "info") : ""}${x.reused_from ? pill("reused", "warn") : ""}
        ${ev ? pill("weight: " + ev.weight, WEIGHT_PILL[ev.weight]) : x.status === "included" ? pill("not evaluated") : ""}
        ${x.status !== "duplicate" ? `<button class="small" data-toggle="${x.id}" data-to="${x.status === "included" ? "excluded" : "included"}">${x.status === "included" ? "Exclude" : "Include"}</button>` : ""}</div></div>
    <div class="small muted">${esc(authors || "Unknown authors")} · ${esc(x.year || "n.d.")} · ${esc(x.venue || "venue unknown")} · ${esc(x.source_type)}</div>
    ${x.status !== "included" ? `<div class="small" style="margin-top:4px">${pill(x.status, x.status === "duplicate" ? "info" : "bad")} ${esc(x.reason || "")}${original ? ` — kept as <strong>${esc(original.label)}</strong>` : ""}</div>` : ""}
    ${ev ? `<dl class="kv">
      <dt>Evidence tier</dt><dd>${esc(ev.tier)}</dd>
      <dt>Peer review</dt><dd>${esc(ev.peer_review)}</dd>
      <dt>Method quality</dt><dd>${esc(ev.method_quality)}${ev.design ? ` — ${esc(ev.design)}` : ""}${ev.sample_size ? ` · ${esc(ev.sample_size)} <span class="tiny muted">(found in abstract)</span>` : ""}</dd>
      <dt>Independence</dt><dd>${esc(ev.independence)}</dd>
      ${ev.cited_by_in_set.length ? `<dt>Cited by (in set)</dt><dd>${cites(ev.cited_by_in_set, all)}</dd>` : ""}
      ${ev.conflicts || (ev.funders && ev.funders.length) ? `<dt>Funding / COI</dt><dd>${esc(ev.conflicts || "")}${ev.funders.length ? ` <span class="muted">Funders (metadata): ${esc(ev.funders.join(", "))}</span>` : ""}</dd>` : ""}
      <dt>Weight basis</dt><dd class="small">${ev.weight_basis.map(esc).join(" · ")}</dd>
    </dl>` : ""}
    ${dupes.length ? `<div class="small" style="margin-top:6px">↳ Merged duplicates: ${dupes.map((d) => `${esc(d.label)} (${esc(d.reason)}; via ${esc((d.providers || []).join(", "))})`).join("; ")}</div>` : ""}
    ${(x.versions || []).length ? `<div class="small">↳ Other versions: ${x.versions.map((v) => safeUrl(v.url) ? `<a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.type)} (${esc(v.venue || "")})</a>` : esc(v.type)).join(", ")}</div>` : ""}
    <details><summary>Abstract, extracted details & provenance</summary>
      <div class="provenance" style="margin-top:8px">Retrieved — abstract</div><div class="small">${esc(x.abstract || x.content_excerpt || "No abstract available.")}</div>
      ${ev && ev.key_findings.length ? `<div class="provenance" style="margin-top:8px">Model-extracted from abstract — verify against full text</div><ul class="small">${ev.key_findings.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}
      ${ev && ev.limitations.length ? `<div class="provenance">Stated limitations (model-extracted)</div><ul class="small">${ev.limitations.map((k) => `<li>${esc(k)}</li>`).join("")}</ul>` : ""}
      ${ev && ev.notes.length ? `<div class="small muted">Notes: ${ev.notes.map(esc).join("; ")}</div>` : ""}
      <div class="small muted" style="margin-top:6px">Found via ${esc((x.providers || []).join(", "))} on ${fmtDate(x.retrieved_at)}${(x.matched_queries || []).length ? ` · queries: ${esc(x.matched_queries.join(" | "))}` : ""}${x.doi ? ` · DOI ${esc(x.doi)}` : ""}${x.cited_by_count != null ? ` · cited by ${x.cited_by_count} (provider count)` : ""}${x.reused_from ? ` · reused from session ${esc(x.reused_from)}` : ""}</div>
    </details></div>`;
}

async function tabEvidence(s) {
  const el = $("#tab");
  let rep;
  try { rep = await api(`/api/sessions/${s.id}/report`); } catch (e) { el.innerHTML = `<div class="card empty">${esc(e.message)}</div>`; return; }
  const sources = await api(`/api/sessions/${s.id}/sources?status=included`);
  const syn = rep.synthesis;
  if (!syn) {
    const run = s.runs[s.runs.length - 1];
    const st = run && run.stages.find((x) => x.name === "synthesize");
    el.innerHTML = `<div class="banner warn"><strong>No evidence synthesis for this report.</strong> ${esc(st ? st.detail : "")}
      ${state.status && !state.status.llm.configured ? ` <a href="#/settings">Configure Groq</a>, then click <strong>Re-analyze</strong>.` : " Click <strong>Re-analyze</strong> to retry."}</div>
      <div class="card"><p>${sources.length} included source(s) are listed in the Source Explorer. No conclusions are shown without synthesis.</p></div>`;
    return;
  }
  const all = syn.themes.flatMap((t) => t.findings.map((f) => ({ ...f, theme: t.name })));
  const col = (cat) => {
    const items = all.filter((f) => f.category === cat);
    return `<div class="card evidence-col"><h3>${pill(CAT[cat][0], CAT[cat][1])} <span class="muted small">${items.length}</span></h3>
      ${items.length ? items.map((f) => `<div class="finding ${cat}"><div>${esc(f.statement)}</div>
        <div class="small" style="margin-top:4px">${cites(f.supporting, sources)}${f.contradicting.length ? ` <span class="muted">vs</span> ${cites(f.contradicting, sources)}` : ""}
          <span class="muted tiny"> · ${f.independent_lines} independent line(s) · ${esc(f.theme)}</span></div>
        ${f.explanation ? `<div class="small muted">${esc(f.explanation)}</div>` : ""}
        ${f.validator_note ? `<div class="tiny" style="color:var(--warn)">⚠ ${esc(f.validator_note)}</div>` : ""}</div>`).join("") : `<div class="muted small">None.</div>`}</div>`;
  };
  const c = syn.confidence;
  el.innerHTML = `
    <div class="card"><div class="row between"><div><div class="stat-label">Overall confidence</div><div class="conf ${esc(c.level)}">${esc(c.level)}</div>
      ${c.model_level !== c.level ? `<div class="small muted">Model proposed ${esc(c.model_level)} — capped by methodology rules.</div>` : ""}</div>
      <div class="provenance">Interpretation · ${esc(syn.model || "")}</div></div>
      <p class="small">${esc(c.rationale)}</p>
      ${c.caps_applied.length ? `<ul class="small">${c.caps_applied.map((x) => `<li><strong>Cap:</strong> ${esc(x)}</li>`).join("")}</ul>` : ""}
      <p>${esc(syn.summary)}</p></div>
    <div class="grid g3">${col("established")}${col("contested")}${col("single_study")}</div>
    <div class="grid g2">
      <div class="card"><h3>${pill("Evidence gaps", "bad")}</h3>${syn.gaps.length ? syn.gaps.map((g) => `<div class="finding gap"><strong>${esc(g.question)}</strong>${g.note ? `<div class="small muted">${esc(g.note)}</div>` : ""}</div>`).join("") : `<div class="muted small">None identified — absence of identified gaps is not proof the question is settled.</div>`}</div>
      <div class="card"><h3>Disagreements & dependencies</h3>
        ${syn.disagreements.map((d) => `<div class="finding contested"><strong>${esc(d.topic)}</strong> ${cites(d.sources, sources)}<div class="small">${d.positions.map(esc).join(" <em>vs</em> ")}</div>${d.significance ? `<div class="small muted">Why it matters: ${esc(d.significance)}</div>` : ""}</div>`).join("") || `<div class="muted small">No explicit disagreements recorded.</div>`}
        ${sources.filter((x) => x.evaluation && !x.evaluation.independent).map((x) => `<div class="small">• ${esc(x.label)} ${esc(x.evaluation.independence)} <span class="tiny muted">(retrieved citation trace)</span></div>`).join("")}
        ${syn.dependencies.map((d) => `<div class="small">• ${esc(d.source)} appears to derive from ${esc(d.depends_on)}: ${esc(d.reason)} <span class="tiny muted">(model-inferred)</span></div>`).join("")}
      </div>
    </div>
    ${syn.practical_conclusions.length ? `<div class="card"><h3>Practical conclusions <span class="provenance">Interpretation</span></h3><ul>${syn.practical_conclusions.map((p) => `<li>${esc(p)}</li>`).join("")}</ul></div>` : ""}
    ${syn.validator_notes.length ? `<div class="card"><h3>Validator corrections</h3><ul class="small">${syn.validator_notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></div>` : ""}`;
}

async function tabReport(s) {
  const el = $("#tab");
  const reports = await api(`/api/sessions/${s.id}/reports`);
  if (!reports.length) { el.innerHTML = `<div class="card empty">No report yet. ${s.running ? "The run is still in progress." : "Run the research first."}</div>`; return; }
  const sel = state.reportId && reports.find((r) => r.id === state.reportId) ? state.reportId : reports[0].id;
  const rep = await api(`/api/sessions/${s.id}/report?report_id=${sel}`);
  el.innerHTML = `
    <div class="row no-print" style="margin-bottom:12px">
      <select id="r-ver" style="width:auto">${reports.map((r, i) => `<option value="${r.id}" ${r.id === sel ? "selected" : ""}>v${reports.length - i} · ${fmtDate(r.created_at)} · ${r.confidence ? r.confidence + " confidence" : "not synthesized"}</option>`).join("")}</select>
      <a class="btn" href="/api/sessions/${s.id}/report/export?format=md&report_id=${sel}">⬇ Markdown</a>
      <a class="btn" href="/api/sessions/${s.id}/report/export?format=pdf&report_id=${sel}">⬇ PDF</a>
      <button id="r-print">🖨 Print</button>
    </div>
    <div class="card md">${renderMarkdown(rep.markdown)}</div>`;
  $("#r-ver").onchange = (e) => { state.reportId = +e.target.value; tabReport(s); };
  $("#r-print").onclick = () => window.print();
}

async function tabFollowup(s) {
  const el = $("#tab");
  const [items, sources] = await Promise.all([api(`/api/sessions/${s.id}/followups`), api(`/api/sessions/${s.id}/sources?status=included`)]);
  el.innerHTML = `
    <div class="card"><h2>Ask a follow-up</h2>
      <div class="muted small">Answers are grounded only in this session's ${sources.length} included source(s) and its latest synthesis. Uncited claims are removed; gaps are stated.</div>
      <textarea id="fu-q" style="margin-top:8px" placeholder="e.g. Which of the included studies had the largest samples, and do they agree?"></textarea>
      <div class="row" style="margin-top:8px"><button class="primary" id="fu-ask" ${s.running ? "disabled" : ""}>Ask</button></div></div>
    ${items.slice().reverse().map((f) => `<div class="card"><div class="muted tiny">${fmtDate(f.created_at)}</div><h3>${esc(f.question)}</h3>
      <div class="md">${renderMarkdown(f.answer.replace(/\[(S\d+)\]/g, "`$1`"))}</div>
      <div class="small" style="margin-top:6px">Cited: ${f.cited.length ? cites(f.cited, sources) : `<span class="muted">none</span>`}</div>
      ${f.not_covered ? `<div class="banner info small" style="margin-top:8px"><strong>Not covered by the evidence:</strong> ${esc(f.not_covered)}</div>` : ""}</div>`).join("")}`;
  $("#fu-ask").onclick = (e) => { const btn = e.currentTarget; return guard(btn, async () => {
    const q = $("#fu-q").value.trim(); if (q.length < 3) throw new Error("Enter a question.");
    btn.innerHTML = `<span class="spin">⟳</span> Thinking…`;
    try { await api(`/api/sessions/${s.id}/followups`, { method: "POST", body: { question: q } }); }
    finally { btn.innerHTML = "Ask"; }
    tabFollowup(s);
  }); };
}

// ------------------------------------------------------------------ history
async function viewHistory() {
  const sessions = await api("/api/sessions");
  $("#view").innerHTML = `
    <h1>Research history</h1>
    <div class="card"><div class="row"><input type="text" id="h-q" placeholder="Search sessions, reports and sources (title, abstract, DOI)…"><button id="h-go">Search</button></div><div id="h-results"></div></div>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Title</th><th>Status</th><th>Included</th><th>Confidence</th><th>Last searched</th><th></th></tr></thead><tbody>
    ${sessions.map((s) => `<tr><td><a href="#/session/${s.id}/progress">${esc(s.title)}</a><div class="tiny muted">${esc(s.question.slice(0, 120))}</div></td>
      <td>${pill(s.running ? "running" : s.status, STATUS_PILL[s.running ? "running" : s.status])}</td><td>${s.included}</td><td>${esc(s.confidence || "—")}</td><td class="small">${fmtDate(s.last_run_at)}</td>
      <td class="row"><a class="btn" href="#/session/${s.id}/report">Report</a><button data-del="${s.id}" data-title="${esc(s.title)}" class="danger" ${s.running ? "disabled" : ""}>Delete</button></td></tr>`).join("") || `<tr><td colspan="6" class="empty">No sessions yet.</td></tr>`}
    </tbody></table></div></div>`;
  const search = (e) => guard(e && e.currentTarget, async () => {
    const q = $("#h-q").value.trim(); if (q.length < 2) throw new Error("Enter at least 2 characters.");
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    $("#h-results").innerHTML = `<div class="grid g3" style="margin-top:12px">
      <div><h3>Sessions (${r.sessions.length})</h3>${r.sessions.map((s) => `<div class="small"><a href="#/session/${s.id}/progress">${esc(s.title)}</a></div>`).join("") || `<span class="muted small">none</span>`}</div>
      <div><h3>Reports (${r.reports.length})</h3>${r.reports.map((x) => `<div class="small"><a href="#/session/${x.session_id}/report">${esc(x.session_title)}</a> <span class="muted tiny">${fmtDay(x.created_at)}</span></div>`).join("") || `<span class="muted small">none</span>`}</div>
      <div><h3>Sources (${r.sources.length})</h3>${r.sources.map((x) => `<div class="small"><a href="#/session/${x.session_id}/sources">${esc(x.label)} ${esc(x.title.slice(0, 80))}</a> <span class="muted tiny">in ${esc(x.session_title)} · ${esc(x.status)}</span></div>`).join("") || `<span class="muted small">none</span>`}</div></div>`;
  });
  $("#h-go").onclick = search;
  $("#h-q").onkeydown = (e) => { if (e.key === "Enter") search(); };
  $$("[data-del]").forEach((b) => (b.onclick = (e) => guard(e.currentTarget, async () => {
    if (!confirm(`Delete "${b.dataset.title}" with all its sources and reports? This cannot be undone.`)) return;
    await api(`/api/sessions/${b.dataset.del}`, { method: "DELETE" }); toast("Deleted", "ok"); viewHistory();
  })));
}

// ------------------------------------------------------------------ settings
async function viewSettings() {
  const data = await api("/api/settings");
  const st = data.settings;
  $("#view").innerHTML = `
    <h1>Settings</h1>
    <div class="card"><h2>API keys</h2>
      <p class="muted small">Keys are saved to the server's <code>.env</code> file and never sent back to the browser. Leave a field empty to keep the current value.</p>
      ${Object.entries(data.secrets).map(([k, v]) => `<div class="grid g2" style="align-items:end;margin-bottom:10px">
        <label class="field"><span>${esc(v.label)} — <code>${k}</code> ${v.configured ? pill("configured", "ok") : pill("not set", "warn")}</span>
          <input type="password" autocomplete="off" data-secret="${k}" placeholder="${v.configured ? "•••••••• (set — type to replace)" : "paste key"}"></label>
        <div>${v.configured ? `<button data-clear="${k}" class="danger">Remove key</button>` : k === "GROQ_API_KEY" ? `<a href="https://console.groq.com/keys" target="_blank" rel="noopener">Get a free Groq key →</a>` : ""}</div></div>`).join("")}
    </div>
    <div class="card"><h2>Model</h2>
      <div class="grid g3">
        <label class="field"><span>Groq model</span><input type="text" id="s-model" list="model-list" value="${esc(st.groq_model)}"><datalist id="model-list"></datalist></label>
        <label class="field"><span>Temperature</span><input type="number" id="s-temp" step="0.05" min="0" max="1" value="${st.llm_temperature}"></label>
        <label class="field"><span>Timeout (seconds)</span><input type="number" id="s-timeout" min="10" max="300" value="${st.llm_timeout_seconds}"></label>
      </div>
      <div class="row" style="margin-top:10px"><button id="s-models">Load available models</button><button id="s-test">Test connection</button><span class="small muted" id="s-test-out"></span></div>
    </div>
    <div class="card"><h2>Research providers</h2>
      ${data.providers.map((p) => `<div style="padding:6px 0;border-bottom:1px solid var(--border)"><label class="check ${p.available ? "" : "disabled"}"><input type="checkbox" name="s-prov" value="${p.name}" ${st.providers.includes(p.name) ? "checked" : ""} ${p.available ? "" : "disabled"}> <strong>${esc(p.label)}</strong></label>
        ${p.available ? "" : pill(p.unavailable_reason, "warn")}<div class="small muted">${esc(p.usage_note)}${p.optional_key ? ` Optional key: <code>${esc(p.optional_key)}</code>.` : ""}</div></div>`).join("")}
      <div class="grid g3" style="margin-top:12px">
        <label class="field"><span>Results per query per provider</span><input type="number" id="s-rpq" min="3" max="50" value="${st.results_per_query}"></label>
        <label class="field"><span>Default max included sources</span><input type="number" id="s-max" min="3" max="60" value="${st.max_screened_sources}"></label>
        <label class="field"><span>Contact email (polite-pool for OpenAlex/Crossref)</span><input type="email" id="s-email" value="${esc(st.contact_email || "")}"></label>
      </div>
      <p class="tiny muted">Groq free-tier limits are token-per-minute based; keeping max sources ≈25 avoids rate-limit waits.</p>
    </div>
    <div class="card"><h2>Storage</h2><div class="small">SQLite database: <code>${esc(data.storage.database)}</code></div></div>
    <div class="row"><button class="primary" id="s-save">Save settings</button></div>`;
  const save = async (secrets = {}) => {
    $$("[data-secret]").forEach((i) => { if (i.value.trim()) secrets[i.dataset.secret] = i.value.trim(); });
    await api("/api/settings", { method: "PUT", body: {
      groq_model: $("#s-model").value.trim(), llm_temperature: +$("#s-temp").value, llm_timeout_seconds: +$("#s-timeout").value,
      providers: $$("input[name=s-prov]:checked").map((x) => x.value), results_per_query: +$("#s-rpq").value,
      max_screened_sources: +$("#s-max").value, contact_email: $("#s-email").value.trim(), secrets } });
    await loadStatus();
  };
  $("#s-save").onclick = (e) => guard(e.currentTarget, async () => { await save(); toast("Settings saved", "ok"); viewSettings(); });
  $$("[data-clear]").forEach((b) => (b.onclick = (e) => guard(e.currentTarget, async () => {
    if (!confirm(`Remove ${b.dataset.clear} from .env?`)) return;
    await save({ [b.dataset.clear]: "" }); toast("Key removed", "ok"); viewSettings();
  })));
  $("#s-models").onclick = (e) => guard(e.currentTarget, async () => {
    const r = await api("/api/settings/models");
    $("#model-list").innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
    toast(`${r.models.length} models available — pick one in the Model field.`, "ok");
  });
  $("#s-test").onclick = (e) => guard(e.currentTarget, async () => {
    $("#s-test-out").textContent = "Testing…";
    try { const r = await api("/api/settings/test-llm", { method: "POST" }); $("#s-test-out").textContent = `✓ ${r.model} responded`; }
    catch (err) { $("#s-test-out").textContent = "✕ " + err.message; throw err; }
  });
}

window.addEventListener("hashchange", route);
loadStatus().then(route);
