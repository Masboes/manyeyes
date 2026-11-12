// manyeyes web UI. Plain ES module, no build step, no framework.
//
// Rules this file keeps:
// - Every URL is relative (the UI runs under an unknown path prefix).
// - Every piece of server data goes into the page as text (textContent or a
//   text node). Nothing is ever written with innerHTML.

import { renderMarkdown, renderInline } from "./md.js";

const TOKEN_KEY = "manyeyes.token";
const ACTIVE = new Set(["queued", "running"]);

const state = {
  config: null,
  me: null,
  cleanup: null,
  signedIn: false,
  jobsFilter: { kind: "", status: "" },
  statsDays: 30,
};

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

function storageGet(key) {
  try { return sessionStorage.getItem(key); } catch { return null; }
}
function storageSet(key, value) {
  try {
    if (value == null) sessionStorage.removeItem(key);
    else sessionStorage.setItem(key, value);
  } catch { /* storage can be blocked; the cookie session still works */ }
}

function append(node, kids) {
  for (const k of kids) {
    if (k == null || k === false) continue;
    if (Array.isArray(k)) append(node, k);
    else if (k instanceof Node) node.append(k);
    else node.append(document.createTextNode(String(k)));
  }
  return node;
}

/** Build an element. Strings become text nodes, never markup. */
function h(tag, props, ...kids) {
  const n = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k === "vars") for (const [name, val] of Object.entries(v)) n.style.setProperty(`--${name}`, String(val));
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
      else if (k.startsWith("aria-") || k.startsWith("data-") || k === "role" || k === "for" || k === "style") n.setAttribute(k, v === true ? "true" : String(v));
      else if (k in n) n[k] = v;
      else n.setAttribute(k, v === true ? "" : String(v));
    }
  }
  return append(n, kids);
}

const SVG_NS = "http://www.w3.org/2000/svg";
function icon(name) {
  const paths = {
    right: ["M3 8h10", "M9 4l4 4-4 4"],
    left: ["M13 8H3", "M7 4L3 8l4 4"],
    close: ["M4 4l8 8", "M12 4l-8 8"],
    out: ["M6 3H3v10h10v-3", "M9 3h4v4", "M13 3L7 9"],
    down: ["M8 3v9", "M4 8l4 4 4-4"],
    copy: ["M5 5h8v8H5z", "M3 11V3h8"],
  };
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 16 16");
  s.setAttribute("aria-hidden", "true");
  s.setAttribute("fill", "none");
  s.setAttribute("stroke", "currentColor");
  s.setAttribute("stroke-width", "1.75");
  s.setAttribute("stroke-linecap", "square");
  s.setAttribute("class", "ic");
  for (const d of paths[name] || []) {
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("d", d);
    s.append(p);
  }
  return s;
}

function markSvg() {
  const s = document.createElementNS(SVG_NS, "svg");
  s.setAttribute("viewBox", "0 0 32 32");
  s.setAttribute("aria-hidden", "true");
  const rect = (x, y, w, cls) => {
    const r = document.createElementNS(SVG_NS, "rect");
    r.setAttribute("x", x); r.setAttribute("y", y);
    r.setAttribute("width", w); r.setAttribute("height", w);
    r.setAttribute("class", cls);
    return r;
  };
  s.append(rect(0, 0, 32, "mark-bg"));
  const pos = [6.75, 13.75, 20.75];
  for (const y of pos) for (const x of pos) {
    s.append(rect(x, y, 4.5, x === 20.75 && y === 20.75 ? "mark-blue" : "mark-eye"));
  }
  return s;
}

// ---------------------------------------------------------------------------
// Formatting
// ---------------------------------------------------------------------------

function fmtDur(s) {
  if (s == null || !isFinite(s)) return "–";
  s = Math.max(0, s);
  if (s < 10) return `${s.toFixed(1)}s`;
  if (s < 59.5) return `${Math.round(s)}s`;
  s = Math.round(s);
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

/** A duration as a ledger figure: numbers large, units small. */
function durFigure(s) {
  const text = fmtDur(s);
  const frag = [];
  for (const m of text.matchAll(/([\d.]+)([a-z]+)|(\S+)/g)) {
    if (m[3]) { frag.push(m[3]); continue; }
    if (frag.length) frag.push(" ");
    frag.push(m[1], h("span", { class: "u" }, m[2]));
  }
  return frag;
}

function fmtTokens(n) {
  if (n == null) return "–";
  if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 1 : 2)}M`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(n >= 1e5 ? 0 : 1)}k`;
  return String(Math.round(n));
}

function fmtPct(r) {
  if (r == null) return "–";
  const p = r * 100;
  return `${p >= 99.95 || p === 0 ? p.toFixed(0) : p.toFixed(p >= 10 ? 0 : 1)}%`;
}

const nf = new Intl.NumberFormat("en-GB");
function fmtInt(n) { return n == null ? "–" : nf.format(n); }

const dtf = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
function absTime(ts) { return ts ? dtf.format(new Date(ts * 1000)) : "–"; }

function relTime(ts) {
  if (!ts) return "–";
  const d = Date.now() / 1000 - ts;
  if (d < 45) return "just now";
  if (d < 90) return "1 min ago";
  if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 2 * 3600) return "1 hour ago";
  if (d < 86400) return `${Math.round(d / 3600)} hours ago`;
  if (d < 2 * 86400) return "yesterday";
  if (d < 30 * 86400) return `${Math.round(d / 86400)} days ago`;
  return new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(new Date(ts * 1000));
}

function timeEl(ts) {
  return h("time", { class: "t-num", dateTime: ts ? new Date(ts * 1000).toISOString() : "", title: absTime(ts), "data-rel": ts || "" }, relTime(ts));
}

function shortSha(s) { return s ? s.slice(0, 7) : null; }

function rosterTotal(roster) {
  if (!roster) return 0;
  let n = 0;
  for (const part of String(roster).split(",")) {
    const m = /^\s*[\w-]+\s*(?::\s*(\d+))?\s*$/.exec(part);
    if (m) n += m[1] ? parseInt(m[1], 10) : 1;
  }
  return n;
}

function firstLine(text, max = 110) {
  const line = (text || "").trim().split("\n").find((l) => l.trim()) || "";
  const clean = line.replace(/^#+\s*/, "").trim();
  return clean.length > max ? `${clean.slice(0, max - 1).trimEnd()}…` : clean;
}

function jobTitle(job) {
  if (job.kind === "spar") return firstLine(job.prompt) || "Spar";
  let t = job.repo || "local";
  if (job.pr) t += ` #${job.pr}`;
  else if (job.head_ref) t += ` @ ${job.head_ref}`;
  return t;
}

function elapsed(job) {
  if (!job.started_at) return null;
  return (job.finished_at || Date.now() / 1000) - job.started_at;
}

const OUTCOMES = ["success", "rate_limited", "timeout", "straggler_killed", "auth_failed", "error", "cancelled"];
const OUTCOME_LABEL = {
  success: "Success", rate_limited: "Rate limited", timeout: "Timeout",
  straggler_killed: "Straggler killed", auth_failed: "Auth failed", error: "Error",
  cancelled: "Cancelled", pending: "Waiting", active: "Running",
};
const FAILISH = new Set(["error", "auth_failed"]);
function outcomeLabel(o) { return OUTCOME_LABEL[o] || String(o).replace(/_/g, " "); }

const STATUS_LABEL = { queued: "Queued", running: "Running", done: "Done", failed: "Failed", cancelled: "Cancelled" };
function statusEl(status, extra) {
  return h("span", { class: `status s-${status}` }, STATUS_LABEL[status] || status, extra ? h("span", { class: "faint", style: "font-weight:500" }, ` · ${extra}`) : null);
}

/** One cell per reviewer, in a fixed outcome order. Pending cells fill up to the roster size. */
function outcomeStrip(outcomes, total, large = false) {
  const cells = [];
  const counts = { ...(outcomes || {}) };
  const order = [...OUTCOMES, ...Object.keys(counts).filter((k) => !OUTCOMES.includes(k))];
  const parts = [];
  for (const o of order) {
    const n = counts[o] || 0;
    if (!n) continue;
    parts.push(`${n} ${outcomeLabel(o).toLowerCase()}`);
    for (let i = 0; i < n; i += 1) cells.push(h("span", { class: `cell o-${OUTCOMES.includes(o) ? o : "error"}` }));
  }
  const done = cells.length;
  const pending = Math.max(0, (total || 0) - done);
  if (pending) parts.push(`${pending} not finished`);
  for (let i = 0; i < pending; i += 1) cells.push(h("span", { class: "cell o-pending" }));
  return h("span", { class: `strip${large ? " lg" : ""}`, role: "img", "aria-label": parts.join(", ") || "no reviewers yet", title: parts.join(", ") }, cells);
}

function legend(keys) {
  return h("ul", { class: "legend", "aria-label": "Outcome colours" },
    keys.map((k) => h("li", null, h("span", { class: `cell o-${k}`, "aria-hidden": "true" }), outcomeLabel(k))));
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function detailText(data, fallback) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map((e) => {
      const loc = Array.isArray(e.loc) ? e.loc.filter((x) => x !== "body").join(".") : "";
      return loc ? `${loc}: ${e.msg}` : e.msg;
    }).join("; ");
  }
  return fallback;
}

async function api(path, { method = "GET", body, text = false } = {}) {
  const headers = { Accept: text ? "text/markdown, text/plain" : "application/json" };
  const token = storageGet(TOKEN_KEY);
  if (token) headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin", cache: "no-store" });
  } catch {
    throw new ApiError(0, "The server did not answer. Check the connection and try again.");
  }
  if (res.status === 401 && path.startsWith("api/")) {
    onUnauthorized();
    throw new ApiError(401, "Sign in again.");
  }
  if (!res.ok) {
    let data = null;
    try { data = await res.json(); } catch { /* not JSON */ }
    throw new ApiError(res.status, detailText(data, `${res.status} ${res.statusText || "error"}`));
  }
  return text ? res.text() : res.json();
}

let toastTimer = null;
function toast(msg, err = false) {
  document.querySelector(".toast")?.remove();
  clearTimeout(toastTimer);
  const t = h("div", { class: `toast${err ? " err" : ""}`, role: err ? "alert" : "status" }, msg);
  document.body.append(t);
  toastTimer = setTimeout(() => t.remove(), 3200);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    const ta = h("textarea", { value: text, style: "position:fixed;opacity:0;top:0;left:0" });
    document.body.append(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch { ok = false; }
    ta.remove();
    return ok;
  }
}

/** Open text from the API in a new tab. Works for cookie and token sign-in. */
async function openText(path) {
  const win = window.open("", "_blank");
  try {
    const txt = await api(path, { text: true });
    const url = URL.createObjectURL(new Blob([txt], { type: "text/plain;charset=utf-8" }));
    if (win) win.location.href = url;
    else window.location.href = url;
    setTimeout(() => URL.revokeObjectURL(url), 60_000);
  } catch (e) {
    win?.close();
    toast(e.message, true);
  }
}

async function downloadText(path, filename) {
  try {
    const txt = await api(path, { text: true });
    const url = URL.createObjectURL(new Blob([txt], { type: "text/markdown;charset=utf-8" }));
    const a = h("a", { href: url, download: filename, style: "display:none" });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  } catch (e) {
    toast(e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Page parts
// ---------------------------------------------------------------------------

function pageHead({ rail, title, lead, actions, titleClass = "t-h1" }) {
  return h("header", { class: "grid-page head" },
    h("p", { class: "col-rail t-rail rise", vars: { i: 0 } }, rail),
    h("div", { class: "col-body" },
      h("h1", { class: `${titleClass} rise`, vars: { i: 1 } }, title),
      lead ? h("p", { class: "t-lead rise", vars: { i: 2 } }, lead) : null,
      actions ? h("div", { class: "head-actions rise", vars: { i: 3 }, style: "margin-top:2rem" }, actions) : null));
}

function block(rail, body, { note, id, label } = {}) {
  return h("section", { class: "block", id, "aria-label": label || (typeof rail === "string" ? rail : null) },
    h("div", { class: "grid-page" },
      h("div", { class: "col-rail" }, h("h2", { class: "t-rail" }, rail), note ? h("div", { class: "rail-note" }, note) : null),
      h("div", { class: "col-body" }, body)));
}

function codeBlock(label, code, { fresh = false } = {}) {
  const btn = h("button", { type: "button", "aria-label": `Copy: ${label}` }, "Copy");
  btn.addEventListener("click", async () => {
    const ok = await copyText(code);
    btn.textContent = ok ? "Copied" : "Copy failed";
    setTimeout(() => { btn.textContent = "Copy"; }, 1600);
  });
  return h("div", { class: `code${fresh ? " fresh" : ""}` },
    h("div", { class: "code-head" }, h("span", null, label), btn),
    h("pre", null, h("code", null, code)));
}

function errorBox(msg) {
  return h("div", { class: "error-box", role: "alert" }, msg);
}

function loading(text = "Loading") {
  return h("div", { class: "page" }, h("p", { class: "loading" }, text));
}

function failView(main, e, retry) {
  main.replaceChildren(h("div", { class: "page" },
    pageHead({ rail: "Error", title: e.status === 404 ? "Not found." : "Something went wrong.", lead: e.message }),
    h("div", { class: "grid-page", style: "margin-top:2rem" }, h("div", { class: "col-body head-actions" },
      retry ? h("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: retry }, "Try again") : null,
      h("a", { class: "btn btn-quiet btn-sm", href: "#/jobs" }, "All jobs")))));
}

// ---------------------------------------------------------------------------
// Header
// ---------------------------------------------------------------------------

const NAV = [
  ["#/jobs", "Jobs", "jobs"],
  ["#/new", "New", "new"],
  ["#/stats", "Stats", "stats"],
  ["#/settings", "Settings", "settings"],
  ["#/tokens", "Tokens", "tokens"],
];

async function signOut() {
  try { await fetch("auth/logout", { method: "POST", credentials: "same-origin" }); } catch { /* ignore */ }
  storageSet(TOKEN_KEY, null);
  location.hash = "#/jobs";
  location.reload();
}

function renderChrome(signedIn) {
  const chrome = document.getElementById("chrome");
  const brand = h("a", { class: "brand", href: "#/jobs", "aria-label": "manyeyes, all jobs" }, markSvg(), h("span", null, "manyeyes"));
  if (!signedIn) {
    chrome.replaceChildren(h("header", { class: "site-header" }, h("nav", { class: "page", "aria-label": "Main" }, brand)));
    return;
  }
  const links = NAV.map(([href, label, key]) => h("a", { href, "data-nav": key }, label));
  const who = h("span", { class: "who" }, "Signed in as ", h("b", null, state.me?.login || "?"));
  const out = h("button", { class: "btn btn-secondary btn-sm", type: "button", onclick: signOut }, "Sign out");

  const toggle = h("button", { class: "menu-toggle", type: "button", "aria-label": "Open menu", "aria-expanded": "false", "aria-controls": "mobile-menu" },
    h("span", { "aria-hidden": "true" }), h("span", { "aria-hidden": "true" }));
  const menuLinks = NAV.map(([href, label, key]) => h("a", { class: "big", href, "data-nav": key }, label));
  const menu = h("div", { class: "mobile-menu", id: "mobile-menu", inert: true },
    h("div", { class: "page" }, menuLinks,
      h("span", { class: "who" }, "Signed in as ", h("b", null, state.me?.login || "?")),
      h("button", { class: "btn btn-secondary", type: "button", onclick: signOut }, "Sign out")));

  const setOpen = (open) => {
    menu.classList.toggle("open", open);
    menu.inert = !open;
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    document.documentElement.style.overflow = open ? "hidden" : "";
    for (const el of [document.getElementById("main"), document.querySelector(".site-footer")]) el.inert = open;
  };
  toggle.addEventListener("click", () => setOpen(!menu.classList.contains("open")));
  menu.addEventListener("click", (e) => { if (e.target.closest("a")) setOpen(false); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && menu.classList.contains("open")) { setOpen(false); toggle.focus(); }
  });
  matchMedia("(min-width: 860px)").addEventListener("change", (e) => { if (e.matches) setOpen(false); });

  chrome.replaceChildren(h("header", { class: "site-header" },
    h("nav", { class: "page", "aria-label": "Main" },
      brand,
      h("div", { class: "nav-links" }, links, h("span", { class: "nav-sep", "aria-hidden": "true" }), who, out),
      toggle),
    menu));
}

function markNav(key) {
  for (const a of document.querySelectorAll("[data-nav]")) {
    if (a.dataset.nav === key) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
}

// ---------------------------------------------------------------------------
// Jobs list
// ---------------------------------------------------------------------------

function jobRow(job) {
  const total = rosterTotal(job.roster);
  const ok = job.outcomes?.success || 0;
  const finished = Object.values(job.outcomes || {}).reduce((a, b) => a + b, 0);
  const sub = [];
  sub.push(h("span", { class: "kind" }, job.kind === "spar" ? "Spar" : "Review"));
  if (job.kind === "review") {
    const refs = job.head_ref ? `${job.head_ref}${job.base_ref ? ` → ${job.base_ref}` : ""}` : (job.base_ref ? `into ${job.base_ref}` : null);
    if (refs) sub.push(h("span", { class: "sep", "aria-hidden": "true" }, "/"), refs);
  }
  sub.push(h("span", { class: "sep", "aria-hidden": "true" }, "/"), job.requested_by || "unknown");
  const dur = elapsed(job);
  const running = job.status === "running";
  return h("li", null,
    h("a", { class: "job-row group", href: `#/jobs/${encodeURIComponent(job.id)}`, "data-key": job.id },
      h("div", { class: "job-when" }, timeEl(job.created_at), h("span", { class: "mono faint" }, job.id)),
      h("div", { class: "job-main" },
        h("span", { class: "job-title" }, h("span", { class: "draw" }, jobTitle(job))),
        h("div", { class: "job-sub" }, sub)),
      h("div", { class: "job-strip" },
        outcomeStrip(job.outcomes, Math.max(total, finished)),
        h("span", { class: "t-num" }, finished ? `${ok} of ${Math.max(total, finished)} ok`
          : (ACTIVE.has(job.status) ? `${total} reviewers` : "no reviews"))),
      h("div", { class: "job-out" },
        h("span", { class: "t-figure", "data-since": running ? job.started_at : null }, dur == null ? "–" : durFigure(dur)),
        statusEl(job.status, job.status === "done" && job.partial ? "partial" : null))));
}

function viewJobs(main) {
  markNav("jobs");
  let timer = null;
  let ticker = null;
  let alive = true;
  const list = h("ul", { class: "ledger", "aria-live": "off" });
  const count = h("span", null, "");
  const status = h("div", { "aria-live": "polite", class: "sr-only" });

  const seg = (label, key, options) => h("div", { class: "seg", role: "group", "aria-label": label },
    options.map(([value, text]) => h("button", {
      type: "button", "aria-pressed": String(state.jobsFilter[key] === value),
      onclick: (e) => {
        state.jobsFilter[key] = value;
        for (const b of e.currentTarget.parentElement.children) b.setAttribute("aria-pressed", String(b === e.currentTarget));
        clearTimeout(timer);
        load();
      },
    }, text)));

  main.replaceChildren(h("div", { class: "page" },
    pageHead({
      rail: "Jobs",
      title: "What the swarm is reviewing.",
      lead: "Every review and spar on this server, newest first. The list refreshes while a job runs.",
      actions: h("a", { class: "btn btn-primary", href: "#/new" }, "New job", icon("right")),
    }),
    h("div", { class: "toolbar" },
      h("h2", null, "All jobs", count),
      h("div", { class: "filters" },
        seg("Kind", "kind", [["", "All"], ["review", "Reviews"], ["spar", "Spars"]]),
        seg("Status", "status", [["", "Any"], ["running", "Running"], ["done", "Done"], ["failed", "Failed"]]))),
    h("div", { class: "rule draw-in", "aria-hidden": "true" }),
    list, status));
  list.append(h("li", { style: "border:0" }, h("p", { class: "loading" }, "Loading jobs")));

  async function load() {
    let jobs;
    try {
      const q = new URLSearchParams({ limit: "50" });
      if (state.jobsFilter.kind) q.set("kind", state.jobsFilter.kind);
      if (state.jobsFilter.status) q.set("status", state.jobsFilter.status);
      jobs = await api(`api/jobs?${q}`);
    } catch (e) {
      if (!alive) return;
      list.replaceChildren(h("li", { class: "empty" }, errorBox(e.message)));
      timer = setTimeout(load, 10_000);
      return;
    }
    if (!alive) return;
    const focusKey = document.activeElement?.dataset?.key;
    if (!jobs.length) {
      const filtered = state.jobsFilter.kind || state.jobsFilter.status;
      list.replaceChildren(h("li", { class: "empty" },
        h("h3", { class: "t-h3" }, filtered ? "No jobs match this filter." : "No jobs yet."),
        h("p", null, filtered ? "Change the filter, or start a new job." : "Start a review of a pull request, or ask the swarm a question."),
        h("p", { style: "margin-top:1.5rem" }, h("a", { class: "btn btn-secondary btn-sm", href: "#/new" }, "New job"))));
    } else {
      list.replaceChildren(...jobs.map(jobRow));
    }
    count.textContent = `${jobs.length} shown`;
    if (focusKey) list.querySelector(`[data-key="${CSS.escape(focusKey)}"]`)?.focus();
    const busy = jobs.some((j) => ACTIVE.has(j.status));
    if (busy) timer = setTimeout(load, 3000);
  }

  ticker = setInterval(() => {
    for (const el of main.querySelectorAll("[data-since]")) {
      const since = parseFloat(el.dataset.since);
      if (since) el.replaceChildren(...durFigure(Date.now() / 1000 - since));
    }
    for (const el of main.querySelectorAll("time[data-rel]")) {
      const ts = parseFloat(el.dataset.rel);
      if (ts) el.textContent = relTime(ts);
    }
  }, 1000);

  load();
  return () => { alive = false; clearTimeout(timer); clearInterval(ticker); };
}

// ---------------------------------------------------------------------------
// Job detail
// ---------------------------------------------------------------------------

function progressModel(job, events) {
  const m = { reviewers: [], byName: new Map(), quorum: null, phase: "queued", quorumReached: false, synth: null, grace: null };
  const ensure = (name) => {
    if (!m.byName.has(name)) {
      const r = { name, state: "pending", model: null, effort: null, started: null, duration: null, reason: null };
      m.byName.set(name, r);
      m.reviewers.push(r);
    }
    return m.byName.get(name);
  };
  if (job.status === "running") m.phase = "starting";
  for (const ev of events) {
    const p = ev.payload || {};
    switch (ev.type) {
      case "clone_started": m.phase = "clone"; break;
      case "swarm_started":
        m.phase = "swarm";
        m.quorum = p.quorum ?? null;
        for (const n of p.reviewers || []) ensure(n);
        break;
      case "reviewer_started": {
        const r = ensure(p.reviewer);
        r.state = "active"; r.model = p.model; r.effort = p.effort; r.started = ev.ts;
        break;
      }
      case "reviewer_finished": {
        const r = ensure(p.reviewer);
        r.state = p.outcome || "error"; r.duration = p.duration_s; r.reason = p.reason;
        break;
      }
      case "quorum_reached": m.quorumReached = true; m.grace = p.grace_s ?? null; break;
      case "synth_started": m.phase = "synth"; break;
      case "synth_finished": m.phase = "synth_done"; m.synth = p; break;
      default: break;
    }
  }
  // Before swarm_started, show the roster size as waiting cells.
  if (!m.reviewers.length) {
    const n = rosterTotal(job.roster);
    for (let i = 1; i <= n; i += 1) ensure(`reviewer ${i}`);
  }
  return m;
}

function eventText(ev) {
  const p = ev.payload || {};
  switch (ev.type) {
    case "clone_started": return [`Cloning ${p.repo || "the repository"}`];
    case "swarm_started": return [`Started ${(p.reviewers || []).length} reviewers. Quorum is ${p.quorum}.`];
    case "reviewer_started": return [`${p.reviewer} started`, p.model ? h("span", { class: "faint" }, ` · ${p.model}${p.effort ? ` / ${p.effort}` : ""}`) : null];
    case "reviewer_finished": {
      const bad = p.outcome !== "success";
      return [h("span", { class: bad ? "ev-fail" : null }, `${p.reviewer} ${bad ? outcomeLabel(p.outcome).toLowerCase() : "finished"}`),
        h("span", { class: "faint" }, ` · ${fmtDur(p.duration_s)}${p.reason ? ` · ${p.reason}` : ""}`)];
    }
    case "quorum_reached": return [`Quorum reached with ${p.successes} reviews. The others get ${fmtDur(p.grace_s)}.`];
    case "synth_started": return ["Writing the synthesis"];
    case "synth_finished": return [`Synthesis ${p.outcome === "success" ? "done" : p.outcome}`, h("span", { class: "faint" }, ` · ${fmtDur(p.duration_s)}${p.note ? ` · ${p.note}` : ""}`)];
    default: return [ev.type.replace(/_/g, " ")];
  }
}

function phaseSentence(job, m) {
  if (job.status === "queued") return "The job waits for a free slot.";
  const done = m.reviewers.filter((r) => r.state !== "pending" && r.state !== "active").length;
  const ok = m.reviewers.filter((r) => r.state === "success").length;
  switch (m.phase) {
    case "starting": return "The job is starting.";
    case "clone": return "Cloning the repository.";
    case "swarm":
      if (m.quorumReached) return `Quorum reached: ${ok} reviews are in. The last reviewers get ${m.grace ? fmtDur(m.grace) : "a short grace period"}.`;
      return `${done} of ${m.reviewers.length} reviewers are done${m.quorum ? `. The synthesis starts after ${m.quorum} successful reviews and a short grace period` : ""}.`;
    case "synth": return "All reviews are in. The synthesis is being written.";
    case "synth_done": return "Saving the result.";
    default: return "Working.";
  }
}

function progressSection(job, events) {
  const m = progressModel(job, events);
  const tiles = h("div", { class: "tiles" }, m.reviewers.map((r) => {
    let sub;
    if (r.state === "pending") sub = "waiting";
    else if (r.state === "active") sub = h("span", { "data-tick": r.started || "" }, `running ${fmtDur(Date.now() / 1000 - (r.started || Date.now() / 1000))}`);
    else sub = `${outcomeLabel(r.state).toLowerCase()} · ${fmtDur(r.duration)}`;
    return h("div", { class: `tile o-${r.state}`, title: r.reason || "" }, h("b", null, r.name), h("span", null, sub));
  }));
  const evList = h("ol", { class: "events", reversed: true, "aria-label": "Event log, newest first" },
    [...events].reverse().map((ev) => h("li", null,
      h("span", { class: "t-num" }, job.started_at ? `+${fmtDur(Math.max(0, ev.ts - job.started_at))}` : absTime(ev.ts)),
      h("span", null, eventText(ev)))));
  return [
    h("p", { class: "t-lead", style: "margin:0 0 1.5rem", "aria-live": "polite" }, phaseSentence(job, m)),
    tiles,
    events.length ? evList : null,
  ];
}

function runsTable(job, onOpen) {
  const runs = [...(job.runs || [])].sort((a, b) => (a.kind === b.kind ? a.instance - b.instance : a.kind.localeCompare(b.kind)));
  const sum = (k) => runs.reduce((a, r) => a + (r[k] || 0), 0);
  const anyReasoning = runs.some((r) => r.reasoning_tokens != null);
  const rows = runs.map((r) => {
    const fail = r.outcome !== "success";
    const btn = h("button", { class: "rv-btn", type: "button", "aria-label": `Open the review of ${r.reviewer}` },
      h("span", { class: "rv-name" }, r.reviewer), icon("right"));
    const tr = h("tr", { class: "clickable" },
      h("td", null, btn, fail && r.failure_reason ? h("span", { class: "fail-reason" }, r.failure_reason) : null),
      h("td", { class: "rv-model hide-sm" }, `${r.model_resolved || r.model} / ${r.effort}`),
      h("td", null, h("span", { class: `oc${FAILISH.has(r.outcome) ? " fail" : ""}` }, h("span", { class: `cell o-${OUTCOMES.includes(r.outcome) ? r.outcome : "error"}`, "aria-hidden": "true" }), outcomeLabel(r.outcome))),
      h("td", { class: "num" }, fmtDur(r.duration_s)),
      h("td", { class: "num hide-xs" }, fmtTokens(r.input_tokens)),
      h("td", { class: "num dim hide-sm" }, fmtTokens(r.cached_input_tokens)),
      h("td", { class: "num hide-xs" }, fmtTokens(r.output_tokens)),
      anyReasoning ? h("td", { class: "num hide-sm" }, fmtTokens(r.reasoning_tokens)) : null);
    const open = () => onOpen(r, btn);
    btn.addEventListener("click", (e) => { e.stopPropagation(); open(); });
    tr.addEventListener("click", open);
    return tr;
  });
  const s = job.synth;
  const synthRow = s ? h("tr", null,
    h("td", null, h("span", { class: "rv-name" }, "synthesis"), s.outcome !== "success" && s.failure_detail ? h("span", { class: "fail-reason", style: s.outcome === "skipped" ? "color:var(--ink-4)" : null }, s.failure_detail) : null),
    h("td", { class: "rv-model hide-sm" }, s.model),
    h("td", null, h("span", { class: "oc" }, h("span", { class: `cell o-${s.outcome === "success" ? "success" : (s.outcome === "skipped" ? "cancelled" : "error")}`, "aria-hidden": "true" }), s.outcome === "success" ? "Success" : outcomeLabel(s.outcome).replace(/^./, (c) => c.toUpperCase()))),
    h("td", { class: "num" }, fmtDur(s.duration_s)),
    h("td", { class: "num hide-xs" }, fmtTokens(s.input_tokens)),
    h("td", { class: "num dim hide-sm" }, "–"),
    h("td", { class: "num hide-xs" }, fmtTokens(s.output_tokens)),
    anyReasoning ? h("td", { class: "num hide-sm" }, fmtTokens(s.reasoning_tokens)) : null) : null;
  return h("div", { class: "table-wrap" },
    h("table", { class: "data" },
      h("caption", { class: "sr-only" }, "Reviewers. Open a row to read that review."),
      h("thead", null, h("tr", null,
        h("th", { scope: "col" }, "Reviewer"),
        h("th", { scope: "col", class: "hide-sm" }, "Model / effort"),
        h("th", { scope: "col" }, "Outcome"),
        h("th", { scope: "col", class: "num" }, "Time"),
        h("th", { scope: "col", class: "num hide-xs" }, "In"),
        h("th", { scope: "col", class: "num hide-sm" }, "Cached"),
        h("th", { scope: "col", class: "num hide-xs" }, "Out"),
        anyReasoning ? h("th", { scope: "col", class: "num hide-sm" }, "Reasoning") : null)),
      h("tbody", null, rows, synthRow),
      runs.length > 1 ? h("tfoot", null, h("tr", null,
        h("td", null, "Total"),
        h("td", { class: "hide-sm" }, ""),
        h("td", null, `${runs.filter((r) => r.outcome === "success").length} of ${runs.length}`),
        h("td", { class: "num" }, ""),
        h("td", { class: "num hide-xs" }, fmtTokens(sum("input_tokens") + (s?.input_tokens || 0))),
        h("td", { class: "num dim hide-sm" }, fmtTokens(sum("cached_input_tokens"))),
        h("td", { class: "num hide-xs" }, fmtTokens(sum("output_tokens") + (s?.output_tokens || 0))),
        anyReasoning ? h("td", { class: "num hide-sm" }, fmtTokens(sum("reasoning_tokens") + (s?.reasoning_tokens || 0))) : null)) : null));
}

/** The review drawer. Focus moves in, Escape closes, focus returns to the opener. */
function openSheet({ title, meta, body, opener }) {
  const overlay = h("div", { class: "sheet-overlay" });
  const closeBtn = h("button", { class: "btn btn-secondary btn-sm sheet-close", type: "button" }, icon("close"), "Close");
  const panel = h("div", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-labelledby": "sheet-title" },
    h("div", { class: "sheet-head" },
      h("div", null, h("h2", { id: "sheet-title" }, title), meta ? h("p", { class: "t-meta" }, meta) : null),
      closeBtn),
    h("div", { class: "sheet-body", tabindex: "-1" }, body));
  const behind = [document.getElementById("chrome"), document.getElementById("main"), document.querySelector(".site-footer")];
  behind.forEach((el) => { el.inert = true; });
  document.documentElement.style.overflow = "hidden";
  document.body.append(overlay, panel);
  closeBtn.focus();
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener("keydown", onKey);
    behind.forEach((el) => { el.inert = false; });
    document.documentElement.style.overflow = "";
    const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
    panel.classList.add("closing");
    overlay.classList.add("closing");
    setTimeout(() => { panel.remove(); overlay.remove(); }, reduce ? 0 : 220);
    opener?.focus();
  };
  const onKey = (e) => {
    if (e.key === "Escape") close();
    if (e.key === "Tab") {
      const f = [...panel.querySelectorAll("button, a[href], [tabindex]:not([tabindex='-1'])")].filter((x) => !x.disabled);
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    }
  };
  document.addEventListener("keydown", onKey);
  overlay.addEventListener("click", close);
  closeBtn.addEventListener("click", close);
  return { close, panel, body: panel.querySelector(".sheet-body") };
}

function openReview(job, run, opener) {
  const ok = run.outcome === "success";
  const body = h("div", null,
    h("dl", { class: "figures small" },
      h("div", null, h("dt", { class: "t-meta" }, "Time"), h("dd", { class: "t-figure" }, durFigure(run.duration_s))),
      h("div", null, h("dt", { class: "t-meta" }, "Input tokens"), h("dd", { class: "t-figure" }, fmtTokens(run.input_tokens))),
      h("div", null, h("dt", { class: "t-meta" }, "Output tokens"), h("dd", { class: "t-figure" }, fmtTokens(run.output_tokens))),
      h("div", null, h("dt", { class: "t-meta" }, "Reasoning"), h("dd", { class: "t-figure" }, fmtTokens(run.reasoning_tokens)))),
    ok ? null : h("div", { style: "margin-bottom:2rem" },
      errorBox(`${outcomeLabel(run.outcome)}${run.failure_reason ? `: ${run.failure_reason}` : ""}${run.exit_code != null ? ` (exit code ${run.exit_code})` : ""}`),
      run.failure_detail ? h("div", { class: "prose", style: "margin-top:1rem" }, h("pre", null, h("code", null, run.failure_detail))) : null),
    h("div", { class: "review-text" }, h("p", { class: "loading" }, "Loading the review")));
  const sheet = openSheet({ title: run.reviewer, meta: `${run.model_resolved || run.model} / ${run.effort} · ${outcomeLabel(run.outcome)}`, body, opener });
  if (!run.has_review) {
    sheet.body.querySelector(".review-text").replaceChildren(h("p", { class: "notice" }, ok ? "This review has no text. The job was deleted, or the text was removed." : "This reviewer did not write a review."));
    return;
  }
  api(`api/jobs/${encodeURIComponent(job.id)}/runs/${encodeURIComponent(run.id)}`, { text: true })
    .then((txt) => sheet.body.querySelector(".review-text").replaceChildren(renderMarkdown(txt)))
    .catch((e) => sheet.body.querySelector(".review-text").replaceChildren(errorBox(e.message)));
}

function viewJob(main, id) {
  markNav("jobs");
  let alive = true;
  let timer = null;
  let ticker = null;
  let events = [];
  let lastEvent = 0;
  let job = null;
  const progressBox = h("div");
  const runsBox = h("div");
  const headStatus = h("span");
  const figuresBox = h("div");

  main.replaceChildren(loading("Loading the job"));

  function figures(j) {
    const total = rosterTotal(j.roster);
    const runs = j.runs || [];
    const ok = runs.filter((r) => r.outcome === "success").length;
    const sum = (k) => runs.reduce((a, r) => a + (r[k] || 0), 0) + ((j.synth && j.synth[k]) || 0);
    const live = ACTIVE.has(j.status);
    const wait = j.started_at ? j.started_at - j.created_at : (j.status === "queued" ? Date.now() / 1000 - j.created_at : null);
    const item = (label, value, sub, i, attrs = {}) => h("div", { vars: { i } },
      h("dt", { class: "t-meta" }, label), h("dd", { class: "t-figure", ...attrs }, value), sub ? h("span", { class: "t-meta sub" }, sub) : null);
    return h("dl", { class: "figures" },
      item(live ? "Running for" : "Run time", elapsed(j) == null ? "–" : durFigure(elapsed(j)), j.finished_at ? `finished ${relTime(j.finished_at)}` : null, 0, { "data-since": j.status === "running" ? j.started_at : null }),
      item("Reviews succeeded", runs.length || !live ? `${ok}/${Math.max(total, runs.length)}` : `0/${total}`, live ? `${runs.length} finished so far` : (runs.length - ok ? `${runs.length - ok} did not finish` : "every reviewer finished"), 1),
      item("Tokens in", fmtTokens(sum("input_tokens")), `${fmtTokens(sum("cached_input_tokens"))} cached`, 2),
      item("Waited in queue", wait == null ? "–" : durFigure(wait), `created ${relTime(j.created_at)}`, 3));
  }

  function detailsList(j) {
    const rows = [];
    const add = (k, v) => { if (v != null && v !== "") rows.push(h("div", null, h("dt", null, k), h("dd", null, v))); };
    add("Job", h("span", { class: "mono" }, j.id));
    add("Requested by", `${j.requested_by || "unknown"}${j.source ? ` (via ${j.source})` : ""}`);
    add("Reviewers", h("span", { class: "mono" }, j.roster));
    if (j.kind === "review") {
      add("Repository", j.repo);
      if (j.pr) add("Pull request", `#${j.pr}`);
      add("Head", j.head_ref || j.head_sha ? [j.head_ref || "", j.head_sha ? h("span", { class: "mono faint", title: j.head_sha }, ` ${shortSha(j.head_sha)}`) : null] : null);
      add("Base", j.base_ref || j.base_sha ? [j.base_ref || "", j.base_sha ? h("span", { class: "mono faint", title: j.base_sha }, ` ${shortSha(j.base_sha)}`) : null] : null);
    }
    add("Synthesis", j.options?.no_synth ? "skipped on request" : null);
    add("Mode", j.options?.mode && j.options.mode !== "full" ? j.options.mode : null);
    add("Related repos", (j.options?.context || []).length ? j.options.context.join(", ") : null);
    add("Created", absTime(j.created_at));
    add("Started", j.started_at ? absTime(j.started_at) : null);
    add("Finished", j.finished_at ? absTime(j.finished_at) : null);
    return h("dl", { class: "meta-list" }, rows);
  }

  function synthesisBlock(j) {
    const s = j.synth;
    const note = s && s.outcome === "success" ? h("p", { class: "t-meta synth-meta" }, h("span", null, s.model), h("span", null, fmtDur(s.duration_s)), h("span", null, `${fmtTokens(s.input_tokens)} in, ${fmtTokens(s.output_tokens)} out`)) : null;
    let body;
    if (j.synthesis) body = renderMarkdown(j.synthesis);
    else if (ACTIVE.has(j.status)) body = h("p", { class: "notice" }, "The synthesis appears here when the reviewers are done.");
    else if (j.options?.no_synth) body = h("p", { class: "notice" }, "The synthesis was skipped for this job. Read the separate reviews below.");
    else if (s && s.outcome !== "success") body = h("p", { class: "notice" }, `No synthesis: ${s.failure_detail || s.outcome}. Read the separate reviews below.`);
    else body = h("p", { class: "notice" }, "This job has no synthesis.");
    return block("Synthesis", body, { note, id: "synthesis" });
  }

  function paint(animate) {
    const j = job;
    const isMine = state.me && ((j.requested_by || '').toLowerCase() === (state.me.login || '').toLowerCase() || state.me.admin);
    const live = ACTIVE.has(j.status);
    const resultPath = `api/jobs/${encodeURIComponent(j.id)}/result`;
    const actions = [];
    if (j.status === "done" || (j.status === "failed" && j.runs?.length)) {
      const a = h("a", { class: "btn btn-secondary btn-sm", href: `${resultPath}?full=true`, target: "_blank", rel: "noopener" }, "Full markdown", icon("out"));
      a.addEventListener("click", (e) => {
        if (!storageGet(TOKEN_KEY)) return; // the cookie session opens the link itself
        e.preventDefault();
        openText(`${resultPath}?full=true`);
      });
      actions.push(a, h("button", { class: "btn btn-quiet btn-sm", type: "button", onclick: () => downloadText(`${resultPath}?full=true`, `${j.id}.md`) }, "Download", icon("down")));
    }
    if (isMine && live) {
      const cancel = h("button", { class: "btn btn-danger btn-sm", type: "button" }, "Cancel job");
      let armed = false;
      cancel.addEventListener("click", async () => {
        if (!armed) {
          armed = true;
          cancel.textContent = "Confirm cancel";
          setTimeout(() => { if (armed && cancel.isConnected) { armed = false; cancel.textContent = "Cancel job"; } }, 4000);
          return;
        }
        cancel.disabled = true;
        try {
          await api(`api/jobs/${encodeURIComponent(j.id)}/cancel`, { method: "POST" });
          toast("The job is cancelled.");
          refresh(true);
        } catch (e) {
          cancel.disabled = false;
          toast(e.message, true);
        }
      });
      actions.push(cancel);
    }

    const title = jobTitle(j);
    const long = title.length > 48;
    headStatus.replaceChildren(statusEl(j.status, j.status === "done" && j.partial ? "partial result" : null));
    const subParts = [];
    subParts.push(headStatus);
    if (j.kind === "review" && (j.head_ref || j.base_ref)) subParts.push(h("span", { class: "muted" }, `${j.head_ref || "head"} → ${j.base_ref || "default branch"}`));
    subParts.push(h("span", { class: "muted" }, `by ${j.requested_by || "unknown"}`));

    const promptText = (j.prompt || "").trim();
    const showPrompt = j.kind === "spar" ? promptText.length > title.length + 2 : !!promptText;

    const page = h("div", { class: "page" },
      h("a", { class: "back", href: "#/jobs" }, icon("left"), "All jobs"),
      h("header", { class: "grid-page head" },
        h("p", { class: "col-rail t-rail rise", vars: { i: 0 } }, j.kind === "spar" ? "Spar" : "Review"),
        h("div", { class: "col-body" },
          h("h1", { class: `${long ? "t-h2" : "t-h1"} rise`, vars: { i: 1 }, style: "overflow-wrap:anywhere" }, title),
          h("p", { class: "job-line rise", vars: { i: 2 } }, subParts),
          showPrompt ? h("div", { class: "rise", vars: { i: 3 } },
            j.kind === "review" ? h("p", { class: "t-meta", style: "margin-top:1.75rem" }, "Focus") : null,
            h("p", { class: "prompt-quote", style: j.kind === "review" ? "margin-top:0.5rem" : null }, promptText)) : null,
          actions.length ? h("div", { class: "head-actions rise", vars: { i: 3 }, style: "margin-top:2rem" }, actions) : null)),
      h("div", { style: "margin-top:3.5rem" }, figuresBox),
      j.error ? block(j.status === "cancelled" ? "Cancelled" : "Error", errorBox(j.error)) : null,
      live ? block("Progress", progressBox, { note: legend(["active", "success", "rate_limited", "error"]) }) : null,
      live ? null : synthesisBlock(j),
      block("Reviewers", runsBox, { note: h("p", { class: "t-meta" }, "Open a row to read that review.") }),
      block("Details", detailsList(j)));

    figuresBox.replaceChildren(figures(j));
    paintLive();
    main.classList.toggle("enter", !!animate);
    main.replaceChildren(page);
    if (animate) setTimeout(() => main.classList.remove("enter"), 1600);
  }

  function paintLive() {
    progressBox.replaceChildren(...progressSection(job, events));
    const runs = job.runs || [];
    runsBox.replaceChildren(runs.length
      ? runsTable(job, (run, btn) => openReview(job, run, btn))
      : h("p", { class: "notice" }, ACTIVE.has(job.status) ? "Reviewers show here as they finish." : "No reviewer ran for this job."));
  }

  async function refresh(full) {
    try {
      const [j, evs] = await Promise.all([
        api(`api/jobs/${encodeURIComponent(id)}`),
        api(`api/jobs/${encodeURIComponent(id)}/events?after=${lastEvent}`),
      ]);
      if (!alive) return;
      events = events.concat(evs);
      if (evs.length) lastEvent = evs[evs.length - 1].id;
      const changed = !job || job.status !== j.status;
      job = j;
      if (full || changed) paint(false);
      else {
        figuresBox.replaceChildren(figures(job));
        paintLive();
      }
    } catch (e) {
      if (!alive) return;
      if (e.status === 401) return;
      toast(e.message, true);
    }
    if (alive && ACTIVE.has(job?.status)) timer = setTimeout(() => refresh(false), 2000);
  }

  (async () => {
    try {
      const [j, evs] = await Promise.all([
        api(`api/jobs/${encodeURIComponent(id)}`),
        api(`api/jobs/${encodeURIComponent(id)}/events?after=0`),
      ]);
      if (!alive) return;
      job = j;
      events = evs;
      if (evs.length) lastEvent = evs[evs.length - 1].id;
      document.title = `${jobTitle(j)} · manyeyes`;
      paint(true);
      if (ACTIVE.has(j.status)) timer = setTimeout(() => refresh(false), 2000);
    } catch (e) {
      if (alive && e.status !== 401) failView(main, e, () => route());
    }
  })();

  ticker = setInterval(() => {
    for (const el of main.querySelectorAll("[data-since]")) {
      const since = parseFloat(el.dataset.since);
      if (since) el.replaceChildren(...durFigure(Date.now() / 1000 - since));
    }
    for (const el of main.querySelectorAll("[data-tick]")) {
      const since = parseFloat(el.dataset.tick);
      if (since) el.textContent = `running ${fmtDur(Date.now() / 1000 - since)}`;
    }
  }, 1000);

  return () => { alive = false; clearTimeout(timer); clearInterval(ticker); };
}

// ---------------------------------------------------------------------------
// New job
// ---------------------------------------------------------------------------

function field({ id, label, optional, hint, control }) {
  return h("div", { class: "field" },
    h("label", { for: id }, label, optional ? h("span", { class: "opt" }, "optional") : null),
    control,
    hint ? h("p", { class: "hint", id: `${id}-hint` }, hint) : null);
}

function checkField({ id, label, hint }) {
  const input = h("input", { type: "checkbox", id, name: id, "aria-describedby": hint ? `${id}-hint` : null });
  return [h("label", { class: "check", style: "margin-top:1.75rem" }, input,
    h("span", null, label, hint ? h("span", { class: "hint", id: `${id}-hint` }, hint) : null)), input];
}

function viewNew(main, params) {
  markNav("new");
  let alive = true;
  let kind = params.get("kind") === "spar" ? "spar" : "review";

  const err = h("div", { class: "form-error" });
  const rosterInputs = [];
  const mkRoster = (id) => {
    const i = h("input", { class: "input mono-in", id, name: "roster", autocomplete: "off", spellcheck: false, placeholder: "the default roster", "aria-describedby": `${id}-hint` });
    rosterInputs.push(i);
    return field({ id, label: "Reviewers", optional: true, hint: "For example codex:8,claude:2. Leave empty to use the default.", control: i });
  };

  // Review form
  const repo = h("input", { class: "input", id: "f-repo", name: "repo", required: true, autocomplete: "off", spellcheck: false, placeholder: "owner/name", "aria-describedby": "f-repo-hint" });
  const pr = h("input", { class: "input", id: "f-pr", name: "pr", type: "number", min: "1", inputMode: "numeric", placeholder: "123" });
  const branch = h("input", { class: "input", id: "f-branch", name: "branch", autocomplete: "off", spellcheck: false, placeholder: "feature/branch" });
  const base = h("input", { class: "input", id: "f-base", name: "base", autocomplete: "off", spellcheck: false, placeholder: "the default branch", "aria-describedby": "f-base-hint" });
  const context = h("input", { class: "input mono-in", id: "f-context", name: "context", autocomplete: "off", spellcheck: false, placeholder: "owner/name, owner/other@branch", "aria-describedby": "f-context-hint" });
  const focus = h("textarea", { class: "input", id: "f-focus", name: "focus", rows: 4, placeholder: "For example: look hard at the database migration.", "aria-describedby": "f-focus-hint" });
  const [rSkipRow, rSkip] = checkField({ id: "f-rnosynth", label: "Skip the synthesis", hint: "You get the separate reviews only." });
  const mkMode = (id) => {
    const sel = h("select", { class: "input", id, name: "mode", "aria-describedby": `${id}-hint` },
      h("option", { value: "full", selected: true }, "Full: the normal review, about 1–2 minutes"),
      h("option", { value: "quick" }, "Quick: fewer reviewers, about a minute"),
      h("option", { value: "deep" }, "Deep: thorough, 3–5 minutes, about 5× the tokens"));
    return [field({ id, label: "Mode", hint: "Deep reads every changed file in full and traces callers and dependencies; use it for risky changes. The Reviewers field, when filled in, replaces the mode's reviewers.", control: sel }), sel];
  };
  const [rQuickRow, rMode] = mkMode("f-rmode");
  const rSubmit = h("button", { class: "btn btn-primary", type: "submit" }, "Start review", icon("right"));
  const reviewForm = h("form", { class: "form", novalidate: true, "aria-labelledby": "tab-review" },
    field({ id: "f-repo", label: "Repository", hint: "The GitHub repository, as owner/name.", control: repo }),
    h("div", { class: "row2", style: "margin-top:1.75rem" },
      field({ id: "f-pr", label: "Pull request", control: pr }),
      h("span", { class: "or", "aria-hidden": "true" }, "or"),
      field({ id: "f-branch", label: "Head branch", control: branch })),
    h("p", { class: "hint", style: "margin-top:0.5rem" }, "Give a pull request number, or the branch to review."),
    h("div", { style: "height:1.75rem" }),
    field({ id: "f-base", label: "Base branch", optional: true, hint: "Leave empty to use the base of the pull request, or the default branch.", control: base }),
    mkRoster("f-rroster"),
    field({ id: "f-context", label: "Related repositories", optional: true, hint: "Up to 5, comma-separated. Reviewers get them read-only, to check whether the change breaks a caller, a shared contract or an event shape there.", control: context }),
    field({ id: "f-focus", label: "Focus", optional: true, hint: "Extra instructions that every reviewer gets.", control: focus }),
    rQuickRow,
    rSkipRow,
    h("div", { class: "form-actions" }, rSubmit, h("a", { class: "btn btn-quiet", href: "#/jobs" }, "Cancel")));

  // Spar form
  const prompt = h("textarea", { class: "input tall", id: "f-prompt", name: "prompt", required: true, placeholder: "Ask a question, or paste a design to pick apart.", "aria-describedby": "f-prompt-hint" });
  const [sSkipRow, sSkip] = checkField({ id: "f-snosynth", label: "Skip the synthesis", hint: "You get the separate answers only." });
  const [sQuickRow, sMode] = mkMode("f-smode");
  const sSubmit = h("button", { class: "btn btn-primary", type: "submit" }, "Start spar", icon("right"));
  const sparForm = h("form", { class: "form", novalidate: true, "aria-labelledby": "tab-spar" },
    field({ id: "f-prompt", label: "Question", hint: "Every reviewer gets this text on its own. The synthesis shows where they agree.", control: prompt }),
    mkRoster("f-sroster"),
    sQuickRow,
    sSkipRow,
    h("div", { class: "form-actions" }, sSubmit, h("a", { class: "btn btn-quiet", href: "#/jobs" }, "Cancel")));

  const tabReview = h("button", { role: "tab", id: "tab-review", type: "button", "aria-controls": "panel-review" }, "Review a change");
  const tabSpar = h("button", { role: "tab", id: "tab-spar", type: "button", "aria-controls": "panel-spar" }, "Spar on a question");
  const panelReview = h("div", { role: "tabpanel", id: "panel-review", "aria-labelledby": "tab-review" }, reviewForm);
  const panelSpar = h("div", { role: "tabpanel", id: "panel-spar", "aria-labelledby": "tab-spar" }, sparForm);

  const setKind = (k, focusTab = false) => {
    kind = k;
    const isR = k === "review";
    tabReview.setAttribute("aria-selected", String(isR));
    tabSpar.setAttribute("aria-selected", String(!isR));
    tabReview.tabIndex = isR ? 0 : -1;
    tabSpar.tabIndex = isR ? -1 : 0;
    panelReview.hidden = !isR;
    panelSpar.hidden = isR;
    err.replaceChildren();
    if (focusTab) (isR ? tabReview : tabSpar).focus();
  };
  tabReview.addEventListener("click", () => setKind("review"));
  tabSpar.addEventListener("click", () => setKind("spar"));
  for (const t of [tabReview, tabSpar]) {
    t.addEventListener("keydown", (e) => {
      if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
        e.preventDefault();
        setKind(kind === "review" ? "spar" : "review", true);
      }
    });
  }

  const showError = (msg, input) => {
    err.replaceChildren(errorBox(msg));
    for (const i of main.querySelectorAll("[aria-invalid]")) i.removeAttribute("aria-invalid");
    if (input) { input.setAttribute("aria-invalid", "true"); input.focus(); }
  };

  async function submit(path, body, btn) {
    const label = btn.firstChild.textContent;
    btn.disabled = true;
    btn.firstChild.textContent = "Starting";
    try {
      const job = await api(path, { method: "POST", body });
      location.hash = `#/jobs/${encodeURIComponent(job.id)}`;
    } catch (e) {
      if (!alive) return;
      showError(e.message);
      btn.disabled = false;
      btn.firstChild.textContent = label;
    }
  }

  reviewForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const r = repo.value.trim();
    if (!/^[\w.-]+\/[\w.-]+$/.test(r)) return showError("Give the repository as owner/name, for example octo/app.", repo);
    const prn = pr.value.trim();
    const br = branch.value.trim();
    if (!prn && !br) return showError("Give a pull request number or a head branch.", pr);
    if (prn && br) return showError("Give a pull request number or a head branch, not both.", pr);
    if (prn && !/^\d+$/.test(prn)) return showError("The pull request number must be a whole number.", pr);
    const body = { repo: r, no_synth: rSkip.checked, mode: rMode.value };
    if (prn) body.pr = parseInt(prn, 10);
    if (br) body.branch = br;
    if (base.value.trim()) body.base = base.value.trim();
    if (rosterInputs[0].value.trim()) body.roster = rosterInputs[0].value.trim();
    if (focus.value.trim()) body.focus = focus.value.trim();
    const ctx = context.value.split(",").map((x) => x.trim()).filter(Boolean);
    if (ctx.length) body.context_repos = ctx;
    submit("api/jobs/review", body, rSubmit);
  });
  sparForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const p = prompt.value.trim();
    if (!p) return showError("Write the question for the reviewers.", prompt);
    const body = { prompt: p, no_synth: sSkip.checked, mode: sMode.value };
    if (rosterInputs[1].value.trim()) body.roster = rosterInputs[1].value.trim();
    submit("api/jobs/spar", body, sSubmit);
  });

  main.replaceChildren(h("div", { class: "page" },
    pageHead({
      rail: "New job",
      title: "Ask the swarm.",
      lead: "A review clones the repository and reads the change. A spar gives every reviewer the same question. Both end in one synthesis.",
    }),
    h("div", { class: "grid-page", style: "margin-top:3.5rem" },
      h("div", { class: "col-body" },
        h("div", { class: "tabs", role: "tablist", "aria-label": "Kind of job" }, tabReview, tabSpar),
        panelReview, panelSpar, err))));
  setKind(kind);
  main.classList.add("enter");
  setTimeout(() => main.classList.remove("enter"), 1400);

  api("api/settings").then((s) => {
    const row = (s.settings || []).find((x) => x.name === "roster");
    if (row && alive) for (const i of rosterInputs) i.placeholder = `${row.value} (default)`;
  }).catch(() => { /* the placeholder stays generic */ });

  return () => { alive = false; };
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function displayValue(row) {
  if (row.secret) return row.value ? "set" : "not set";
  const v = row.value;
  if (v === true) return "on";
  if (v === false) return "off";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "–";
  if (v === "" || v == null) return "–";
  return String(v);
}

function sourceBadge(src) {
  return h("span", { class: `badge b-${src}`, title: {
    default: "The built-in default", file: "From the config file", env: "From the environment", ui: "Set on this page", flag: "From a command-line flag",
  }[src] || src }, src);
}

function viewSettings(main) {
  markNav("settings");
  let alive = true;
  main.replaceChildren(loading("Loading the settings"));

  function render(data, animate, message) {
    const editable = new Set(data.editable || []);
    const rows = data.settings || [];
    const edit = rows.filter((r) => editable.has(r.name));
    const rest = rows.filter((r) => !editable.has(r.name));
    const changes = new Map();
    const err = h("div");
    const count = h("span", { class: "t-meta", "aria-live": "polite" }, "No unsaved changes");
    const saveBtn = h("button", { class: "btn btn-primary", type: "submit", disabled: true }, "Save changes");
    const discard = h("button", { class: "btn btn-quiet", type: "button", disabled: true }, "Discard");

    const bar = h("div", { class: "save-bar" }, count, h("div", { class: "head-actions" }, discard, saveBtn));
    const update = () => {
      const n = changes.size;
      count.textContent = n ? `${n} unsaved ${n === 1 ? "change" : "changes"}` : "No unsaved changes";
      saveBtn.disabled = !n;
      discard.disabled = !n;
      bar.classList.toggle("dirty", n > 0);
    };

    const rowEls = edit.map((r) => {
      const id = `s-${r.name.replace(/[^\w]/g, "-")}`;
      const changed = h("span", { class: "changed", hidden: true }, "Changed");
      let input;
      let read;
      if (r.type === "bool") {
        input = h("input", { type: "checkbox", id, checked: !!r.value });
        read = () => input.checked;
      } else if (r.choices && r.choices.length) {
        input = h("select", { class: "input", id }, r.choices.map((c) => h("option", { value: c, selected: c === r.value }, c)));
        read = () => input.value;
      } else if (r.type === "int" || r.type === "float") {
        input = h("input", { class: "input", id, type: "number", step: r.type === "int" ? "1" : "any", value: String(r.value ?? ""), inputMode: r.type === "int" ? "numeric" : "decimal" });
        read = () => (input.value.trim() === "" ? null : Number(input.value));
      } else {
        input = h("input", { class: "input mono-in", id, value: String(r.value ?? ""), autocomplete: "off", spellcheck: false });
        read = () => input.value.trim();
      }
      input.setAttribute("aria-describedby", `${id}-help`);
      const onChange = () => {
        const v = read();
        if (v === r.value || (v === null && r.value == null)) changes.delete(r.name);
        else changes.set(r.name, v);
        changed.hidden = !changes.has(r.name);
        update();
      };
      input.addEventListener("input", onChange);
      input.addEventListener("change", onChange);

      let reset = null;
      if (r.source === "ui") {
        reset = h("button", { class: "btn btn-quiet btn-xs", type: "button", title: "Remove the value set here. The file, environment or default value applies again." }, "Reset");
        reset.addEventListener("click", async () => {
          reset.disabled = true;
          try {
            const d = await api("api/settings", { method: "PUT", body: { [r.name]: null } });
            if (alive) render(d, false, `${r.name} is reset.`);
          } catch (e) {
            reset.disabled = false;
            err.replaceChildren(errorBox(e.message));
          }
        });
      }
      const ctl = r.type === "bool"
        ? h("label", { class: "check" }, input, h("span", null, "On"))
        : input;
      const defText = r.type === "bool" ? (r.default ? "on" : "off") : String(r.default ?? "");
      return h("div", { class: "set-row" },
        h("div", null,
          h("div", { class: "set-name" }, h("label", { for: id }, r.name), sourceBadge(r.source)),
          h("p", { class: "set-help", id: `${id}-help` }, renderInline(r.help || "")),
          h("p", { class: "t-meta set-def" }, `Default: ${defText || "–"}`)),
        h("div", { class: "set-ctl" }, h("div", { class: "line" }, h("div", { style: "flex:1" }, ctl)), h("div", { class: "line" }, changed, reset)));
    });

    const admin = state.me?.can_manage !== false;
    const form = h("form", { novalidate: true },
      admin ? null : h("p", { class: "notice" }, "Only admins can change these values. Ask an admin if a limit is in your way."),
      h("fieldset", { class: "set-fieldset", disabled: !admin }, h("div", { class: "set-list" }, rowEls)), err, admin ? bar : null);
    discard.addEventListener("click", () => render(data, false));
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!changes.size) return;
      saveBtn.disabled = true;
      err.replaceChildren();
      try {
        const d = await api("api/settings", { method: "PUT", body: Object.fromEntries(changes) });
        if (alive) render(d, false, "The settings are saved. They apply to the next job.");
      } catch (e2) {
        saveBtn.disabled = false;
        err.replaceChildren(h("div", { style: "margin-top:1.25rem" }, errorBox(e2.status === 422 ? `Not saved: ${e2.message}` : e2.message)));
      }
    });

    const table = h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("caption", { class: "sr-only" }, "Other settings, read only"),
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "Name"), h("th", { scope: "col" }, "Value"), h("th", { scope: "col" }, "Source"))),
      h("tbody", null, rest.map((r) => h("tr", null,
        h("td", null, h("span", { class: "mono", style: "color:var(--ink)" }, r.name), h("span", { class: "fail-reason", style: "color:var(--ink-4)" }, r.help ? renderInline(r.help) : null)),
        h("td", { style: "overflow-wrap:anywhere;max-width:22rem" }, r.secret
          ? h("span", { class: `oc${r.value ? "" : " faint"}` }, h("span", { class: `cell o-${r.value ? "success" : "pending"}`, "aria-hidden": "true" }), displayValue(r))
          : h("span", { class: "mono" }, displayValue(r))),
        h("td", null, sourceBadge(r.source)))))));

    const urls = data.public_urls && data.public_urls.length ? data.public_urls.join(", ") : null;
    main.classList.toggle("enter", !!animate);
    main.replaceChildren(h("div", { class: "page" },
      pageHead({
        rail: "Settings",
        title: "Review defaults.",
        lead: "These values apply to the next job. A job that runs now keeps the values it started with. A value set here wins over the config file and the environment.",
      }),
      block("Defaults", form, { note: h("p", { class: "t-meta" }, "The badge shows where each value comes from.") }),
      versionBlock(),
      block("All settings", table, { note: h("p", { class: "t-meta" }, urls ? `Set these in the config file or the environment. Public URL: ${urls}` : "Set these in the config file or the environment. Secrets show only whether they are set.") })));
    if (animate) setTimeout(() => main.classList.remove("enter"), 1600);
    if (message) toast(message);
  }

  api("api/settings").then((d) => { if (alive) render(d, true); })
    .catch((e) => { if (alive && e.status !== 401) failView(main, e, () => route()); });
  return () => { alive = false; };
}

// ---------------------------------------------------------------------------
// Tokens
// ---------------------------------------------------------------------------

function viewTokens(main) {
  markNav("tokens");
  let alive = true;
  let fresh = null; // the token just created; shown once
  main.replaceChildren(loading("Loading your tokens"));

  async function load(animate) {
    let tokens;
    try { tokens = await api("api/tokens"); } catch (e) {
      if (alive && e.status !== 401) failView(main, e, () => route());
      return;
    }
    if (!alive) return;
    render(tokens, animate);
  }

  function render(tokens, animate) {
    const name = h("input", { class: "input", id: "t-name", required: true, maxLength: 80, autocomplete: "off", placeholder: "For example: laptop, CI, Claude Code" });
    const create = h("button", { class: "btn btn-primary", type: "submit" }, "Create token");
    const err = h("div");
    const form = h("form", { class: "inline-form", novalidate: true },
      field({ id: "t-name", label: "Name", control: name }), create);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!name.value.trim()) { err.replaceChildren(h("div", { style: "margin-top:1rem" }, errorBox("Give the token a name, so you know where it is used."))); name.focus(); return; }
      create.disabled = true;
      try {
        fresh = await api("api/tokens", { method: "POST", body: { name: name.value.trim() } });
        await load(false);
        document.getElementById("fresh-token")?.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
        document.querySelector("#fresh-token button")?.focus();
      } catch (e2) {
        create.disabled = false;
        err.replaceChildren(h("div", { style: "margin-top:1rem" }, errorBox(e2.message)));
      }
    });

    const freshBox = fresh ? h("div", { id: "fresh-token", style: "margin-top:2rem" },
      codeBlock(`New token: ${fresh.name}`, fresh.token, { fresh: true }),
      h("p", { class: "t-meta", style: "margin-top:0.75rem" }, "This is the only time the token shows. Copy it now and store it in a safe place.")) : null;

    const rows = tokens.map((t) => {
      const revoked = !!t.revoked_at;
      let action = null;
      if (!revoked) {
        action = h("button", { class: "btn btn-quiet btn-xs", type: "button", "aria-label": `Revoke ${t.name}` }, "Revoke");
        let armed = false;
        action.addEventListener("click", async () => {
          if (!armed) {
            armed = true;
            action.className = "btn btn-danger btn-xs";
            action.textContent = "Confirm revoke";
            setTimeout(() => { if (armed && action.isConnected) { armed = false; action.className = "btn btn-quiet btn-xs"; action.textContent = "Revoke"; } }, 4000);
            return;
          }
          action.disabled = true;
          try {
            await api(`api/tokens/${encodeURIComponent(t.id)}`, { method: "DELETE" });
            if (fresh && fresh.id === t.id) fresh = null;
            toast(`${t.name} is revoked.`);
            load(false);
          } catch (e) {
            action.disabled = false;
            toast(e.message, true);
          }
        });
      }
      return h("tr", null,
        h("td", null, h("span", { class: "rv-name" }, t.name), h("span", { class: "fail-reason mono", style: "color:var(--ink-4)" }, t.id)),
        h("td", { class: "hide-sm" }, h("span", { title: absTime(t.created_at) }, relTime(t.created_at))),
        h("td", null, t.last_used_at ? h("span", { title: absTime(t.last_used_at) }, relTime(t.last_used_at)) : h("span", { class: "faint" }, "never")),
        h("td", null, revoked ? h("span", { class: "status s-cancelled" }, "Revoked") : h("span", { class: "status s-done" }, "Active")),
        h("td", { style: "text-align:right" }, action));
    });
    const table = tokens.length ? h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("caption", { class: "sr-only" }, "Your API tokens"),
      h("thead", null, h("tr", null, h("th", { scope: "col" }, "Name"), h("th", { scope: "col", class: "hide-sm" }, "Created"), h("th", { scope: "col" }, "Last used"), h("th", { scope: "col" }, "Status"), h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Action")))),
      h("tbody", null, rows))) : h("p", { class: "notice" }, "You have no tokens yet.");

    const url = state.config?.mcp_url || "<server>/mcp";
    const tok = fresh ? fresh.token : "<token>";
    const mcp = h("div", null,
      h("p", { style: "max-width:40rem" }, "Add manyeyes to Claude Code, or to another MCP client. The client can then start reviews and spars, and read the results."),
      state.config?.github ? h("div", { style: "margin-top:1.5rem" },
        codeBlock("With GitHub sign-in", `claude mcp add --transport http manyeyes ${url}`),
        h("p", { class: "t-meta", style: "margin-top:0.75rem" }, "The client opens GitHub in the browser the first time.")) : null,
      h("div", { style: "margin-top:1.5rem" },
        codeBlock("With an API token", `claude mcp add --transport http manyeyes ${url} --header "Authorization: Bearer ${tok}"`),
        h("p", { class: "t-meta", style: "margin-top:0.75rem" }, fresh ? "This command has your new token in it. Do not share it." : "Put a token from this page in place of <token>.")));

    main.classList.toggle("enter", !!animate);
    main.replaceChildren(h("div", { class: "page" },
      pageHead({
        rail: "Access",
        title: "API tokens.",
        lead: "A token lets a script or an MCP client act as you. Each token has a name, so you can revoke one without the others.",
      }),
      block("New token", h("div", null, form, err, freshBox)),
      block("Your tokens", table, { note: h("p", { class: "t-meta" }, `${tokens.filter((t) => !t.revoked_at).length} active`) }),
      block("Connect an MCP client", mcp)));
    if (animate) setTimeout(() => main.classList.remove("enter"), 1600);
  }

  load(true);
  return () => { alive = false; };
}

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

function viewStats(main) {
  markNav("stats");
  let alive = true;
  main.replaceChildren(loading("Loading the stats"));

  function periodSeg() {
    return h("div", { class: "seg", role: "group", "aria-label": "Period" },
      [7, 30, 90].map((d) => h("button", {
        type: "button", "aria-pressed": String(state.statsDays === d),
        onclick: () => { state.statsDays = d; load(false); },
      }, `${d} days`)));
  }

  function bars(obj, total, kindOf) {
    const entries = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
    if (!entries.length) return h("p", { class: "notice" }, "No data.");
    return h("ul", { class: "bars-list" }, entries.map(([k, n]) => h("li", null,
      h("span", null, k.charAt(0).toUpperCase() + k.slice(1).replace(/_/g, " ")),
      h("span", { class: "track", "aria-hidden": "true" }, h("span", { class: `fillbar ${kindOf ? kindOf(k) : ""}`, style: `display:block;width:${total ? (100 * n) / total : 0}%` })),
      h("span", { class: "t-num" }, fmtInt(n)))));
  }

  function modelBlock(m) {
    const outs = m.outcomes || {};
    const order = [...OUTCOMES.filter((o) => outs[o]), ...Object.keys(outs).filter((o) => !OUTCOMES.includes(o))];
    const fig = (label, value, i) => h("div", { vars: { i } }, h("dt", { class: "t-meta" }, label), h("dd", { class: "t-figure" }, value));
    const failures = (m.top_failures || []).length
      ? h("ul", { style: "margin:0;padding:0;list-style:none" }, m.top_failures.map(([reason, n]) => h("li", null, reason, h("span", { class: "faint t-num" }, ` × ${n}`))))
      : h("span", { class: "faint" }, "none");
    return h("article", { class: "model-block" },
      h("div", { class: "model-title" }, h("h3", null, m.model), h("span", { class: "badge" }, m.kind)),
      h("dl", { class: "figures small" },
        fig("Runs", fmtInt(m.runs), 0),
        fig("Succeeded", fmtPct(m.success_rate), 1),
        fig("Median time", durFigure(m.duration_s?.p50), 2),
        fig("p90 time", durFigure(m.duration_s?.p90), 3)),
      h("div", { style: "margin-top:2rem" },
        h("p", { class: "t-meta" }, "Outcomes of all runs"),
        h("div", { class: "bar", role: "img", "aria-label": order.map((o) => `${outs[o]} ${outcomeLabel(o).toLowerCase()}`).join(", ") },
          order.map((o) => h("span", { class: `o-${OUTCOMES.includes(o) ? o : "error"}`, style: `flex:${outs[o]} 1 0`, title: `${outcomeLabel(o)}: ${outs[o]}` }))),
        h("ul", { class: "legend" }, order.map((o) => h("li", null, h("span", { class: `cell o-${OUTCOMES.includes(o) ? o : "error"}`, "aria-hidden": "true" }), `${outcomeLabel(o)} `, h("span", { class: "t-num", style: "color:var(--ink)" }, fmtInt(outs[o])))))),
      h("table", { class: "kv" },
        h("caption", { class: "sr-only" }, `Details for ${m.model}`),
        h("tbody", null,
          h("tr", null, h("th", { scope: "row" }, "Average input tokens"), h("td", { class: "num" }, fmtTokens(m.avg_input_tokens))),
          h("tr", null, h("th", { scope: "row" }, "Average output tokens"), h("td", { class: "num" }, fmtTokens(m.avg_output_tokens))),
          h("tr", null, h("th", { scope: "row" }, "Average reasoning tokens"), h("td", { class: "num" }, fmtTokens(m.avg_reasoning_tokens))),
          h("tr", null, h("th", { scope: "row" }, "Average and longest time"), h("td", { class: "num" }, `${fmtDur(m.duration_s?.avg)}  ·  ${fmtDur(m.duration_s?.max)}`)),
          h("tr", null, h("th", { scope: "row" }, "Most common failures"), h("td", null, failures)))));
  }

  function resetsIn(ts) {
    if (!ts) return "";
    const m = Math.round((ts - Date.now() / 1000) / 60);
    if (m <= 0) return "reset; new reading soon";
    if (m < 60) return `resets in ${m} min`;
    if (m < 48 * 60) return `resets in ${Math.floor(m / 60)} h ${String(m % 60).padStart(2, "0")}`;
    return `resets in ${Math.round(m / 1440)} days`;
  }

  function usageBlock(u) {
    if (!u || !u.providers || (!u.providers.length && !u.interval_min)) return null;
    const body = u.providers.length ? u.providers.map((p) => {
      const plan = p.windows.find((w) => w.plan)?.plan;
      return h("div", { class: "usage-provider" },
        h("p", { class: "t-meta", style: "margin-bottom:0.5rem" }, p.provider === "codex" ? "Codex" : "Claude", plan ? ` · ${plan}` : "",
          p.read_at ? h("span", { class: "faint" }, " · read ", timeEl(p.read_at)) : null),
        p.windows.length ? h("ul", { class: "bars-list usage-list" }, p.windows.map((w) => h("li", null,
          h("span", null, w.window.charAt(0).toUpperCase() + w.window.slice(1)),
          h("span", { class: "track", role: "img", "aria-label": `${Math.round(w.used_percent)}% used` },
            h("span", { class: `fillbar ${w.used_percent >= 90 ? "bad" : "ok"}`, style: `display:block;width:${Math.min(100, w.used_percent)}%` })),
          h("span", { class: "t-num" }, `${Math.round(w.used_percent)}%`),
          h("span", { class: "t-meta hide-xs", title: w.resets_at ? absTime(w.resets_at) : "" }, resetsIn(w.resets_at))))) : null,
        p.error ? h("p", { class: "t-meta", style: "margin-top:0.5rem" }, `Last reading failed: ${p.error}`) : null);
    }) : h("p", { class: "notice" }, "No current reading. The server reads it for each logged-in reviewer kind; a reading older than three intervals is not shown.");
    return block("Plan usage", body, {
      note: h("p", { class: "t-meta" },
        "How much of the Codex and Claude plans the reviewer logins have used, for all their use, not only manyeyes. ",
        u.interval_min ? `Read every ${u.interval_min} min; reading spends no tokens.` : "Reading is off (usage.interval_min = 0)."),
    });
  }

  async function load(animate) {
    let s, u = null;
    try { u = await api("api/usage"); } catch { u = null; }
    try { s = await api(`api/stats?days=${state.statsDays}`); } catch (e) {
      if (alive && e.status !== 401) failView(main, e, () => route());
      return;
    }
    if (!alive) return;
    const j = s.jobs || {};
    const st = j.by_status || {};
    const runsTotal = (s.reviewers || []).reduce((a, m) => a + m.runs, 0);
    const runsOk = (s.reviewers || []).reduce((a, m) => a + (m.outcomes?.success || 0), 0);
    const fig = (label, value, sub, i) => h("div", { vars: { i } }, h("dt", { class: "t-meta" }, label), h("dd", { class: "t-figure" }, value), sub ? h("span", { class: "t-meta sub" }, sub) : null);

    main.classList.toggle("enter", !!animate);
    main.replaceChildren(h("div", { class: "page" },
      pageHead({
        rail: "Stats",
        title: "How the swarm performs.",
        lead: `Jobs and reviewer runs of the last ${state.statsDays} days. Times are wall-clock, from the start of the job to its end.`,
        actions: periodSeg(),
      }),
      h("dl", { class: "figures", style: "margin-top:3.5rem" },
        fig("Jobs", fmtInt(j.total || 0), `${fmtInt(st.done || 0)} done, ${fmtInt(st.failed || 0)} failed`, 0),
        fig("Jobs succeeded", fmtPct(j.success_rate), j.partial ? `${fmtInt(j.partial)} with a partial result` : "of the jobs that finished", 1),
        fig("Median run time", durFigure(j.run_s?.p50), j.run_s?.p90 != null ? `p90 ${fmtDur(j.run_s.p90)}, longest ${fmtDur(j.run_s.max)}` : null, 2),
        fig("Reviews succeeded", runsTotal ? fmtPct(runsOk / runsTotal) : "–", `${fmtInt(runsOk)} of ${fmtInt(runsTotal)} reviewer runs`, 3)),
      usageBlock(u),
      block("Jobs", h("div", { class: "split" },
        h("div", null, h("p", { class: "t-meta", style: "margin-bottom:0.5rem" }, "By status"), bars(st, j.total, (k) => (k === "done" ? "ok" : k === "failed" ? "bad" : ""))),
        h("div", null,
          h("p", { class: "t-meta", style: "margin-bottom:0.5rem" }, "By kind"), bars(j.by_kind, j.total),
          h("table", { class: "kv" }, h("tbody", null,
            h("tr", null, h("th", { scope: "row" }, "Median wait in the queue"), h("td", { class: "num" }, fmtDur(j.queue_s?.p50))),
            h("tr", null, h("th", { scope: "row" }, "p90 wait in the queue"), h("td", { class: "num" }, fmtDur(j.queue_s?.p90))),
            h("tr", null, h("th", { scope: "row" }, "Average run time"), h("td", { class: "num" }, fmtDur(j.run_s?.avg)))))))),
      block("Reviewers", (s.reviewers || []).length ? (s.reviewers || []).map(modelBlock) : h("p", { class: "notice" }, "No reviewer ran in this period."),
        { note: h("p", { class: "t-meta" }, "One block per model. Times and tokens count successful runs only.") }),
      (s.users || []).length ? block("By person", h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", null, h("tr", null, h("th", null, "Person"), h("th", { class: "num" }, "Jobs"),
          h("th", { class: "num" }, "Reviewer runs"), h("th", { class: "num" }, "Tokens in"), h("th", { class: "num hide-xs" }, "Tokens out"))),
        h("tbody", null, s.users.map((u) => h("tr", null, h("td", null, u.login || "—"), h("td", { class: "num" }, fmtInt(u.jobs)),
          h("td", { class: "num" }, fmtInt(u.reviewer_runs)), h("td", { class: "num" }, fmtTokens(u.input_tokens)),
          h("td", { class: "num hide-xs" }, fmtTokens(u.output_tokens))))))),
        { note: h("p", { class: "t-meta" }, "Server jobs per person in this period. Per-person limits are on the Settings page.") }) : null));
    if (animate) setTimeout(() => main.classList.remove("enter"), 1800);
  }

  load(true);
  return () => { alive = false; };
}

// ---------------------------------------------------------------------------
// Sign-in
// ---------------------------------------------------------------------------

function viewSignin(main, { error } = {}) {
  renderChrome(false);
  const github = !!state.config?.github;
  const tokenInput = h("input", { class: "input mono-in", id: "token", type: "password", autocomplete: "off", spellcheck: false, required: true, placeholder: "me_…", "aria-describedby": "token-hint" });
  const err = h("div", { style: "margin-top:1rem" }, error ? errorBox(error) : null);
  const go = h("button", { class: "btn btn-primary", type: "submit" }, "Sign in");
  const form = h("form", { class: "token-form", hidden: github && !error, novalidate: true },
    field({ id: "token", label: "API token", hint: "The token stays in this browser tab only, until you close it or sign out.", control: tokenInput }),
    h("div", { style: "margin-top:1.25rem" }, go), err);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const t = tokenInput.value.trim();
    if (!t) { err.replaceChildren(errorBox("Paste an API token.")); tokenInput.focus(); return; }
    go.disabled = true;
    storageSet(TOKEN_KEY, t);
    try {
      const res = await fetch("api/me", { headers: { Authorization: `Bearer ${t}`, Accept: "application/json" }, credentials: "same-origin" });
      if (res.status === 401) throw new Error("This token is not valid, or its owner is not on the allowlist.");
      if (!res.ok) throw new Error(`The server answered ${res.status}.`);
      await boot();
    } catch (e2) {
      storageSet(TOKEN_KEY, null);
      go.disabled = false;
      err.replaceChildren(errorBox(e2.message));
      tokenInput.setAttribute("aria-invalid", "true");
      tokenInput.focus();
    }
  });
  const useToken = h("button", { class: "btn btn-secondary", type: "button", "aria-expanded": String(!form.hidden), "aria-controls": "token-form" }, "Use an API token");
  form.id = "token-form";
  useToken.addEventListener("click", () => {
    form.hidden = false;
    useToken.setAttribute("aria-expanded", "true");
    tokenInput.focus();
  });

  const cells = [];
  const outcomes = ["success", "success", "success", "success", "rate_limited", "success", "success", "success", "success", "straggler_killed"];
  outcomes.forEach((o, i) => cells.push(h("span", { class: `cell o-${o} rise`, vars: { i: 4 + i } })));
  const motif = h("figure", { class: "signin-motif", style: "margin-left:0;margin-right:0", "aria-label": "Ten reviewers feed one synthesis" },
    h("div", { class: "rule draw-in", vars: { i: 3 }, "aria-hidden": "true", style: "margin-bottom:1.5rem" }),
    h("div", { class: "row", "aria-hidden": "true" },
      h("div", { class: "strip", style: "flex:1" }, cells),
      h("span", { class: "arrow" }, icon("right")),
      h("span", { class: "synth rise", vars: { i: 15 } })),
    h("figcaption", { class: "caps" },
      h("span", { class: "t-meta" }, "Each cell is one reviewer. It reads the change on its own."),
      h("span", { class: "t-meta", style: "text-align:right" }, "One synthesis groups what they agree on.")));

  main.classList.add("enter");
  main.replaceChildren(h("div", { class: "page signin" },
    h("div", { class: "grid-page head" },
      h("p", { class: "col-rail t-rail rise", vars: { i: 0 } }, "Sign in"),
      h("div", { class: "col-body" },
        h("h1", { class: "t-display rise", vars: { i: 1 } }, "manyeyes", h("span", { class: "dot" }, ".")),
        h("p", { class: "t-lead rise", vars: { i: 2 }, style: "margin-top:1.75rem;max-width:36rem" },
          "A self-hosted swarm of AI code reviewers. Many reviewers read the same change on their own; one synthesis says where they agree."),
        h("div", { class: "actions rise", vars: { i: 3 } },
          github ? h("a", { class: "btn btn-primary", href: "auth/login" }, "Sign in with GitHub", icon("right")) : null,
          github ? useToken : null),
        form,
        motif))));
  if (!github) setTimeout(() => tokenInput.focus(), 50);
  document.title = "Sign in · manyeyes";
}

// ---------------------------------------------------------------------------
// Router and boot
// ---------------------------------------------------------------------------

function parseRoute() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  const parts = path.split("/").filter(Boolean).map(decodeURIComponent);
  return { parts, params: new URLSearchParams(query) };
}

const TITLES = { jobs: "Jobs", new: "New job", stats: "Stats", settings: "Settings", tokens: "Tokens" };

function route() {
  if (!state.signedIn) return;
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } }
  state.cleanup = null;
  document.querySelectorAll(".sheet, .sheet-overlay").forEach((n) => n.remove());
  for (const el of [document.getElementById("chrome"), document.getElementById("main"), document.querySelector(".site-footer")]) el.inert = false;
  document.documentElement.style.overflow = "";
  const main = document.getElementById("main");
  main.classList.add("enter");
  const { parts, params } = parseRoute();
  const top = parts[0] || "jobs";
  document.title = `${TITLES[top] || "manyeyes"} · manyeyes`;
  window.scrollTo(0, 0);
  let cleanup;
  if (top === "jobs" && parts[1]) cleanup = viewJob(main, parts[1]);
  else if (top === "new") cleanup = viewNew(main, params);
  else if (top === "settings") cleanup = viewSettings(main);
  else if (top === "tokens") cleanup = viewTokens(main);
  else if (top === "stats") cleanup = viewStats(main);
  else {
    if (top !== "jobs") { history.replaceState(null, "", "#/jobs"); }
    cleanup = viewJobs(main);
    setTimeout(() => main.classList.remove("enter"), 1600);
  }
  state.cleanup = cleanup;
}

let unauthorizedShown = false;
function onUnauthorized() {
  if (unauthorizedShown) return;
  unauthorizedShown = true;
  const hadToken = !!storageGet(TOKEN_KEY);
  storageSet(TOKEN_KEY, null);
  state.signedIn = false;
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } }
  state.cleanup = null;
  viewSignin(document.getElementById("main"), { error: hadToken ? "The token stopped working. Sign in again." : "Your session ended. Sign in again." });
}

async function boot() {
  unauthorizedShown = false;
  const main = document.getElementById("main");
  let config;
  try {
    const headers = { Accept: "application/json" };
    const token = storageGet(TOKEN_KEY);
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch("auth/config", { headers, credentials: "same-origin", cache: "no-store" });
    if (!res.ok) throw new Error(`The server answered ${res.status}.`);
    config = await res.json();
  } catch (e) {
    main.replaceChildren(h("div", { class: "page" }, pageHead({ rail: "Error", title: "The server did not answer.", lead: e.message }),
      h("div", { class: "grid-page", style: "margin-top:2rem" }, h("div", { class: "col-body" }, h("button", { class: "btn btn-secondary", type: "button", onclick: boot }, "Try again")))));
    return;
  }
  state.config = config;
  if (!config.login) {
    const hadToken = !!storageGet(TOKEN_KEY);
    storageSet(TOKEN_KEY, null);
    state.signedIn = false;
    viewSignin(main, { error: hadToken ? "The saved token is not valid any more. Sign in again." : null });
    return;
  }
  try {
    state.me = await api("api/me");
  } catch {
    return; // onUnauthorized showed the sign-in
  }
  state.signedIn = true;
  renderChrome(true);
  route();
  loadVersion();
}

// ---- version and updates ----------------------------------------------------
async function loadVersion() {
  try {
    state.version = await api("api/version");
  } catch {
    return;
  }
  if (!state.version.update_available) return;
  const pill = () => h("a", { class: "update-pill", href: "#/settings",
    title: `manyeyes ${state.version.latest} is available` }, "Update available");
  document.querySelector(".nav-links .who")?.before(pill());
  document.querySelector(".mobile-menu .who")?.before(pill());
}

function versionBlock() {
  const v = state.version;
  const body = h("div", { class: "version-body" });
  if (!v) {
    body.append(h("p", { class: "t-meta" }, "Version information is not available."));
    return block("Version", body);
  }
  body.append(h("p", { class: "version-line" },
    h("span", { class: "t-num" }, `manyeyes ${v.current}`),
    v.update_available ? h("span", { class: "badge b-ui" }, `${v.latest} available`) : null));
  if (v.update_available) {
    body.append(
      h("p", null, "A newer release is out. ",
        v.url ? h("a", { class: "link", href: v.url, target: "_blank", rel: "noopener" }, "Read the release notes") : null,
        "."),
      codeBlock("Update the server (systemd unit with PULL=1)", "systemctl --user restart manyeyes"),
      codeBlock("Or with Docker Compose", "docker compose pull && docker compose up -d"),
      codeBlock("Update the CLI", v.url && v.url.includes("/releases/tag/")
        ? `uv tool install --force git+${v.url.replace("/releases/tag/", "@")}`
        : "uv tool install --force git+https://github.com/<owner>/manyeyes@<tag>"));
  } else {
    const when = v.checked_at ? relTime(v.checked_at) : null;
    body.append(h("p", { class: "t-meta" },
      v.error ? `No newer release found (${v.error}).` : "This is the newest release.",
      when ? ` Checked ${when}.` : ""));
  }
  return block("Version", body, { note: h("p", { class: "t-meta" }, "The server checks for a new release twice a day.") });
}

window.addEventListener("hashchange", () => {
  if (location.hash === "#main") return;
  route();
});

// The skip link must not change the route.
document.addEventListener("click", (e) => {
  const a = e.target.closest?.("a.skip");
  if (!a) return;
  e.preventDefault();
  document.getElementById("main").focus();
});

boot();
