"use strict";

const PALETTE = ["#d7352b", "#1f6fd1", "#0e9f5b", "#e9a100", "#8a4fbf", "#0097a7", "#d63a8c", "#8c5a33", "#5e8f1e", "#e5652a"];
// Short names on screen; the full meaning is the tooltip. The legend in index.html matches.
const KIND = {
  baseline: { who: "Pre-existing", tip: "In the file before the first recorded session", cls: "k-baseline" },
  external: { who: "External edit", tip: "Changed outside the agents, between two recorded edits", cls: "k-external" },
  drift: { who: "Changed since", tip: "Changed on disk after the last recorded edit", cls: "k-drift" },
  unknown: { who: "Unknown", tip: "Not enough evidence in the logs", cls: "k-unknown" },
};
// The same kinds, said of lines that are gone.
const GAP_TIP = {
  baseline: "Deleted before the first recorded session",
  external: "Deleted outside the agents, between two recorded edits",
  drift: "Deleted on disk after the last recorded edit",
  unknown: "Deleted, but the logs don't show by whom",
};
const MAX_LANES = 4;
// Deleted lines shown in place before a "show more" button.
const GAP_PREVIEW = 30;

const SVG_NS = "http://www.w3.org/2000/svg";
// 24px stroke icons.
const ICONS = {
  chat: "M4 5h16v11H10l-6 4z",
  pencil: "M4 20l1-4L16 5l3 3L8 19zM14 7l3 3",
  file: "M6 3h8l4 4v14H6zM14 3v4h4",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  external: "M14 4h6v6M20 4l-9 9M18 14v6H4V6h6",
  close: "M6 6l12 12M18 6L6 18",
  lock: "M6 11h12v9H6zM9 11V8a3 3 0 0 1 6 0v3",
  back: "M11 6l-6 6 6 6M5 12h14",
  branch: "M6 3v12M18 3a3 3 0 1 0 0 6a3 3 0 0 0 0-6zM6 15a3 3 0 1 0 0 6a3 3 0 0 0 0-6zM18 9a9 9 0 0 1-9 9",
  alert: "M12 4l9 16H3zM12 10v4M12 17v.5",
  sparkle: "M12 3l2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2z",
  sun: "M12 8a4 4 0 1 0 0 8a4 4 0 0 0 0-8zM12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M5 19l1.5-1.5M17.5 6.5L19 5",
  moon: "M20 14A8 8 0 1 1 10 4a6 6 0 0 0 10 10z",
  auto: "M3 5h18v11H3zM9 20h6M12 16v4",
  target: "M12 7a5 5 0 1 0 0 10a5 5 0 0 0 0-10zM12 2v3M12 19v3M2 12h3M19 12h3",
  terminal: "M3 5h18v14H3zM7 10l3 2-3 2M12 15h5",
  unfold: "M4 4h16v16H4zM8 12h8M12 8v8",
  fold: "M4 4h16v16H4zM8 12h8",
  search: "M11 4a7 7 0 1 0 0 14a7 7 0 0 0 0-14zM16 16l4.5 4.5",
  up: "M6 15l6-6l6 6",
  down: "M6 9l6 6l6-6",
  forward: "M13 6l6 6-6 6M19 12H5",
  outline: "M4 6h3M10 6h10M7 12h3M13 12h7M7 18h3M13 18h7",
};

const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const MOD = IS_MAC ? "⌘" : "Ctrl+";
const MAX_TABS = 12;
const PAGE_VERSION = document.querySelector('meta[name="papertrail-version"]')?.content;

const state = {
  workspace: null,
  overview: null,
  tree: null,
  entries: [],
  sessions: new Map(),
  root: "",
  tabs: [],
  expanded: new Set(),
  // The agent-only view starts with every folder open; these are the ones closed there.
  folded: new Set(),
  agentOnly: false,
  file: null,
  byLine: [],
  line: null,
  // Deletions by the number of lines above them, newest first at each spot.
  gapsAt: new Map(),
  gap: null,
  openGaps: new Set(),
  fullGaps: new Set(),
  showGaps: false,
  focus: null,
  panel: { view: "intro" },
  cypher: [],
};

const $ = (id) => document.getElementById(id);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

function icon(name) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICONS[name]);
  svg.append(path);
  return svg;
}

// An icon and a number. The words are the tooltip and what a screen reader hears.
function stat(name, n, word, visible) {
  return h("span", { class: "stat", title: plural(n, word) }, icon(name), n.toLocaleString(), h("span", { class: visible ? "word" : "sr-only" }, `${visible ? "" : " "}${word}${n === 1 ? "" : "s"}`));
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers);
  if (PAGE_VERSION) headers.set("X-Paper-Trail-Version", PAGE_VERSION);
  let res;
  try {
    res = await fetch(path, { ...options, headers });
  } catch {
    throw new Error("Can't reach the server. Is papertrail ui running?");
  }

  const version = res.headers.get("X-Paper-Trail-Version");
  if (version && version !== PAGE_VERSION) {
    const message = "Server restarted";
    closePalette();
    $("server-notice").hidden = false;
    $("server-notice-text").textContent = message;
    throw new Error(message);
  }

  const data = await res.json().catch(() => null);
  const error = typeof data?.error === "string" ? data.error : "";
  if (res.status === 405 || res.status === 501 ||
      (res.status === 404 && (!error || /^Unknown endpoint\.?$/i.test(error)))) {
    throw new Error("Server out of date. Restart papertrail ui.");
  }
  if (!res.ok && error) throw new Error(error);
  if (!res.ok || !data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("Unexpected response. Reload and retry.");
  }
  return data;
}

function api(name, params = {}) {
  return request(`/api/${name}?` + new URLSearchParams(params));
}

function post(name, body) {
  return request(`/api/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Paper-Trail": "1" },
    body: JSON.stringify(body),
  });
}

function stored(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v == null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}

function store(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {}
}

function rememberFolder(path) {
  store("papertrail-recent", [path, ...stored("papertrail-recent", []).filter((p) => p !== path)].slice(0, 8));
}

function record(label, statements, params) {
  state.cypher = state.cypher.filter((c) => c.label !== label);
  state.cypher.push({ label, statements, params });
  if (!$("cypher").hidden) renderCypher();
}

function renderCypher() {
  const body = $("cypher-body");
  body.replaceChildren(
    ...[...state.cypher].reverse().flatMap((c) => [
      h("h3", {}, c.label, c.params ? `  ${JSON.stringify(c.params)}` : ""),
      ...c.statements.map((s) => h("pre", {}, s)),
    ]),
  );
}

// ms = 0 keeps the message up until the next toast or hideToast().
function toast(text, ms = 2400) {
  const t = $("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  if (ms) toast.timer = setTimeout(() => (t.hidden = true), ms);
}

function hideToast() {
  clearTimeout(toast.timer);
  $("toast").hidden = true;
}

async function copy(text, done) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const area = h("textarea", { style: "position:fixed;opacity:0" }, text);
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  toast(done);
}

function rel(path) {
  if (!path) return "";
  if (state.root && path.startsWith(state.root)) return path.slice(state.root.length);
  if (state.home && path.startsWith(state.home + "/")) return "~" + path.slice(state.home.length);
  return path;
}
const base = (path) => (path || "").split("/").pop();
const inRoot = (path) => !state.root || path.startsWith(state.root);
const color = (id) => state.sessions.get(id)?.color || "var(--muted)";
const plural = (n, word, many = `${word}s`) => `${n} ${n === 1 ? word : many}`;
const oneLine = (text) => (text || "").replace(/\s+/g, " ").trim();

// The year shows only when it isn't this one.
function when(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  const year = d.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return d.toLocaleString(undefined, { year, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

// A title taken from a first prompt often starts as a Markdown heading and runs into the next one.
const tidyTitle = (title) => (title || "").replace(/^[#\s]+/, "").split(/\s#{1,6}\s/)[0].trim();

function shellQuote(s) {
  return /^[\w./-]+$/.test(s) ? s : `'${s.replace(/'/g, "'\\''")}'`;
}

function resumeCommand(row) {
  if (!row.resume) return null;
  return (row.resume_cwd || row.cwd ? `cd ${shellQuote(row.resume_cwd || row.cwd)} && ` : "") + row.resume;
}

function sessionLabel(id) {
  const s = state.sessions.get(id);
  return s?.title || (id ? `Session ${id.slice(0, 8)}` : "Unknown session");
}

/* ---------- URL state ---------- */

// A far jump (another file, or ten or more lines away) leaves a history entry, so
// Back returns to where the reader was. Nearer moves rewrite the current entry.
const FAR = 10;
const nav = { at: null, quiet: false, i: history.state?.i || 0, top: 0, restore: 0, fileRequest: 0 };
try {
  nav.top = Math.max(nav.i, Number(sessionStorage.getItem("papertrail-nav-top")) || 0);
} catch {
  nav.top = nav.i;
}

const whereNow = () => ({ file: state.file?.path || null, line: state.line, gap: state.line ? null : state.gap });
const spot = (w) => w.line ?? w.gap ?? 1;
const far = (a, b) => !!a?.file && !!b.file && (a.file !== b.file || Math.abs(spot(a) - spot(b)) >= FAR);

function writeHash() {
  const folder = base(state.workspace?.project || state.root.replace(/\/$/, ""));
  document.title = [state.file && base(state.file.path), folder, "Paper Trail"].filter(Boolean).join(" — ");
  const p = new URLSearchParams();
  if (state.file) p.set("file", state.file.path);
  if (state.line) p.set("line", state.line);
  else if (state.gap != null) p.set("gap", state.gap);
  if (state.focus) p.set("session", state.focus);
  const now = whereNow();
  if (!nav.quiet && far(nav.at, now)) {
    nav.top = ++nav.i;
    history.pushState({ i: nav.i, panel: state.panel }, "", "#" + p.toString());
    try {
      sessionStorage.setItem("papertrail-nav-top", nav.top);
    } catch {}
  } else history.replaceState({ i: nav.i, panel: state.panel }, "", "#" + p.toString());
  nav.at = now;
  renderNav();
}

function rememberPanel(details) {
  Object.assign(state.panel, details);
  const now = whereNow();
  if (!nav.quiet && nav.at && nav.at.file === now.file && nav.at.line === now.line && nav.at.gap === now.gap) {
    history.replaceState({ i: nav.i, panel: state.panel }, "");
  }
}

function renderNav() {
  $("nav-back").disabled = nav.i <= 0;
  $("nav-forward").disabled = nav.i >= nav.top;
}

// Back or Forward landed on another entry: show its location and panel.
async function restoreHash() {
  const want = readHash();
  const saved = history.state?.panel;
  const restoring = ++nav.restore;
  nav.fileRequest++;
  nav.i = history.state?.i ?? nav.i;
  nav.quiet = true;
  try {
    if ((!want.file && !saved) || (!state.overview && !state.workspace?.project)) return;
    const focus = want.session && state.sessions.has(want.session) ? want.session : null;
    if (want.file && state.file?.path !== want.file) {
      state.focus = focus;
      if (state.overview) renderSessions();
      await openFile(want.file, want.line, { gap: want.gap, panel: saved });
      return;
    }
    if (focus !== state.focus) setFocus(focus);
    if (!want.file && state.file) clearFile();
    if (want.gap != null && state.gapsAt.has(want.gap)) selectGap(want.gap, { open: true, scroll: true, panel: saved });
    else if (want.line) selectLine(want.line, { scroll: true, panel: saved });
    else {
      state.line = state.gap = null;
      markSelected();
      showPanel(saved || { view: "intro" });
      $("code-scroll").scrollTop = 0;
      writeHash();
    }
  } finally {
    if (restoring === nav.restore) {
      nav.quiet = false;
      nav.at = whereNow();
      renderNav();
    }
  }
}

function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const gap = p.get("gap");
  return { file: p.get("file"), line: Number(p.get("line")) || null, gap: gap == null ? null : Number(gap), session: p.get("session") };
}

/* ---------- Overview and sidebar ---------- */

// The project is the working directory where most of the recorded edits happened.
function projectRoot(sessions, files) {
  const weight = new Map();
  for (const s of sessions) if (s.cwd && s.edits) weight.set(s.cwd, (weight.get(s.cwd) || 0) + s.edits);
  const best = [...weight.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
  return best ? best.replace(/\/?$/, "/") : commonRoot(files.map((f) => f.path));
}

function commonRoot(paths) {
  if (!paths.length) return "";
  const parts = paths.map((p) => p.split("/").slice(0, -1));
  const first = parts[0];
  let n = first.length;
  for (const p of parts) {
    let i = 0;
    while (i < n && p[i] === first[i]) i++;
    n = i;
  }
  return n ? first.slice(0, n).join("/") + "/" : "";
}

function renderSummary() {
  const s = state.overview.stats;
  const pct = s.agent_fraction == null ? null : Math.round(s.agent_fraction * 100);
  $("summary").replaceChildren(
    stat("chat", s.sessions, "session", true),
    stat("pencil", s.edits, "edit", true),
    stat("file", s.files, "file", true),
    pct == null
      ? ""
      : h("span", { class: "stat", title: `${pct}% of current lines were written by agents` }, h("span", { class: "share" }, h("i", { style: `width:${pct}%` })), `${pct}% agent-written`),
  );
}

// Every file the explorer can show: files in the open folder on disk, plus every
// file in the graph. Files outside the folder go in their own group.
function buildEntries() {
  const map = new Map();
  for (const f of state.overview?.files || []) map.set(f.path, { path: f.path, graph: f });
  if (state.tree?.root) {
    const root = state.tree.root.replace(/\/?$/, "/");
    for (const r of state.tree.files) if (!map.has(root + r)) map.set(root + r, { path: root + r, graph: null });
  }
  return [...map.values()].sort((a, b) => a.path.localeCompare(b.path));
}

const agentWrote = (e) => e.graph && e.graph.edits > 0;

function fileTree(entries) {
  const top = { dirs: new Map(), files: [], path: "", agent: 0 };
  for (const e of entries) {
    const parts = rel(e.path).split("/");
    let node = top;
    for (const part of parts.slice(0, -1)) {
      if (!node.dirs.has(part)) node.dirs.set(part, { name: part, dirs: new Map(), files: [], path: node.path + part + "/", agent: 0 });
      node = node.dirs.get(part);
      if (agentWrote(e)) node.agent++;
    }
    node.files.push(e);
  }
  return top;
}

function ensureExpanded(path) {
  if (!inRoot(path)) return;
  const parts = rel(path).split("/").slice(0, -1);
  let acc = "";
  for (const part of parts) {
    state.expanded.add((acc += part + "/"));
    state.folded.delete(acc);
  }
}

const folderOpen = (path) => (state.agentOnly ? !state.folded.has(path) : state.expanded.has(path));

function toggleFolder(path) {
  const set = state.agentOnly ? state.folded : state.expanded;
  if (set.has(path)) set.delete(path);
  else set.add(path);
  renderFiles();
}

function foldAll(tree) {
  state.expanded.clear();
  if (state.agentOnly) {
    const walk = (node) => {
      for (const d of node.dirs.values()) {
        state.folded.add(d.path);
        walk(d);
      }
    };
    walk(tree);
  }
  renderFiles();
}

function fileRow(e, depth, label = base(e.path)) {
  const f = e.graph;
  const share = f?.lines ? f.agent_lines / f.lines : 0;
  const gone = f?.status === "missing" ? "deleted" : f?.status === "unreadable" ? "unreadable" : null;
  const tip = f
    ? `${rel(e.path)}\n${plural(f.lines || 0, "line")} · ${plural(f.sessions, "session")} · ${plural(f.edits, "edit")} · ${Math.round(share * 100)}% agent-written`
    : rel(e.path);
  return h(
    "button",
    {
      type: "button",
      class: agentWrote(e) ? "item tree-row" : "item tree-row plain",
      style: `--depth:${depth}`,
      "data-path": e.path,
      "aria-current": state.file?.path === e.path ? "true" : "false",
      title: tip,
      onclick: () => openFile(e.path),
    },
    h("span", { class: "name" }, label, gone ? h("span", { class: "tag" }, gone) : null),
    f?.lines ? h("span", { class: "share" }, h("i", { style: `width:${share * 100}%` })) : null,
  );
}

function treeRows(node, depth, out) {
  const dirs = [...node.dirs.values()].sort((a, b) => a.name.localeCompare(b.name));
  for (let d of dirs) {
    // Compact single-child chains like an editor does: src/app/lib.
    let label = d.name;
    while (!d.files.length && d.dirs.size === 1) {
      d = [...d.dirs.values()][0];
      label += "/" + d.name;
    }
    const open = folderOpen(d.path);
    const folder = d;
    out.push(
      h(
        "button",
        {
          type: "button",
          class: folder.agent ? "item tree-row dir" : "item tree-row dir plain",
          style: `--depth:${depth}`,
          "aria-expanded": String(open),
          title: folder.agent ? `${plural(folder.agent, "file")} edited by agents` : null,
          onclick: () => toggleFolder(folder.path),
        },
        h("span", { class: "chev", "aria-hidden": "true" }),
        h("span", { class: "name" }, label),
      ),
    );
    if (open) treeRows(d, depth + 1, out);
  }
  for (const e of node.files.sort((a, b) => base(a.path).localeCompare(base(b.path)))) out.push(fileRow(e, depth));
}

function renderFiles() {
  const box = $("tab-files");
  if (!state.root) return renderNoFolder(box);
  const scroll = box.scrollTop;
  const here = state.entries.filter((e) => inRoot(e.path) && (!state.agentOnly || agentWrote(e)));
  const away = state.entries.filter((e) => !inRoot(e.path));
  const name = base(state.root.replace(/\/$/, "")) || state.root || "Files";
  const filter = h(
    "button",
    {
      type: "button",
      class: "icon-btn",
      "aria-pressed": String(state.agentOnly),
      "aria-label": "Agent-edited files only",
      title: "Agent-edited files only",
      onclick: () => {
        state.agentOnly = !state.agentOnly;
        store("papertrail-agent-only", state.agentOnly);
        renderFiles();
      },
    },
    icon("sparkle"),
  );
  const rows = [];
  const tree = fileTree(here);
  treeRows(tree, 0, rows);
  const anyOpen = rows.some((r) => r.classList?.contains("dir") && r.getAttribute("aria-expanded") === "true");
  const fold = h(
    "button",
    { type: "button", class: "icon-btn", "aria-label": "Collapse folders", title: "Collapse folders", disabled: !anyOpen, onclick: () => foldAll(tree) },
    icon("fold"),
  );
  box.replaceChildren(
    ...[
      h(
        "div",
        { class: "explorer-head" },
        h("button", { type: "button", class: "folder-name", title: `${state.root}\nOpen another folder (${MOD}O)`, onclick: pickFolder }, name),
        rows.length ? fold : null,
        state.overview ? filter : null,
      ),
      state.overview
        ? null
        : state.building
          ? h(
              "div",
              { class: "unbuilt", id: "side-build" },
              h("p", {}, sideBuildText()),
              h("span", { class: "meter" }, h("i", { style: `width:${buildPercent(state.buildStatus)}%` })),
            )
          : h(
              "div",
              { class: "unbuilt" },
              h("p", {}, "No history yet"),
              h("button", { type: "button", class: "primary", onclick: () => buildHistory() }, "Build History"),
            ),
      ...(rows.length ? rows : [h("p", { class: "none" }, state.agentOnly ? "No agent-edited files" : "Empty folder")]),
      state.tree?.truncated ? h("p", { class: "none" }, `First ${state.tree.files.length} files shown`) : null,
      away.length
        ? h(
            "details",
            { class: "past", open: away.some((e) => e.path === state.file?.path) },
            h("summary", { title: "Edited by the same sessions" }, "Outside this folder", h("span", { class: "count" }, away.length)),
            away.map((e) => fileRow(e, 0, rel(e.path))),
          )
        : null,
    ].filter(Boolean),
  );
  box.scrollTop = scroll;
}

function renderNoFolder(box) {
  const project = state.workspace?.project;
  box.replaceChildren(
    h(
      "div",
      { class: "none" },
      h("p", {}, project ? "No history yet" : "No folder open"),
      h("button", { type: "button", class: project ? null : "primary", onclick: pickFolder }, "Open Folder…"),
    ),
  );
}

function dots(ids) {
  return h("span", { class: "dots" }, ids.slice(0, 6).map((id) => h("i", { class: "dot", style: `background:${color(id)}`, title: sessionLabel(id) })));
}

function renderCrossings() {
  const box = $("tab-crossings");
  if (!state.overview) {
    return box.replaceChildren(h("p", { class: "none" }, state.building ? "Building…" : "No history yet"));
  }
  const rows = state.overview.crossings;
  const live = rows.filter((r) => r.start != null);
  const past = rows.filter((r) => r.start == null);
  if (!rows.length) {
    return box.replaceChildren(h("p", { class: "none" }, "No crossings yet"));
  }
  // One entry per file; each crossing inside it is a small line chip.
  const byFile = new Map();
  for (const r of live) {
    if (!byFile.has(r.path)) byFile.set(r.path, []);
    byFile.get(r.path).push(r);
  }
  const fileItem = ([path, spots]) =>
    h(
      "div",
      { class: "item cross-file", "data-path": path, "aria-current": state.file?.path === path ? "true" : "false" },
      h("button", { type: "button", class: "name", title: rel(path), onclick: () => openFile(path, spots[0].start) }, dots([...new Set(spots.flatMap((s) => s.sessions))]), base(path)),
      h(
        "span",
        { class: "chips" },
        spots.map((r) =>
          h(
            "button",
            {
              type: "button",
              class: r.session_count > 2 ? "chip many" : "chip",
              title: `${r.session_count} sessions rewrote line ${r.start}${r.end > r.start ? "-" + r.end : ""}`,
              onclick: () => openFile(path, r.start),
            },
            r.end > r.start ? `${r.start}-${r.end}` : r.start,
          ),
        ),
      ),
    );
  const files = [...byFile.entries()];
  const here = files.filter(([p]) => inRoot(p));
  const away = files.filter(([p]) => !inRoot(p));
  const pastList = past.length
    ? h(
        "details",
        { class: "past" },
        h("summary", {}, "Replaced or deleted", h("span", { class: "count" }, past.length)),
        past.map((r) => h("button", { type: "button", class: "item", title: rel(r.path), onclick: () => openFile(r.path) }, h("span", { class: "name" }, dots(r.sessions), base(r.path)))),
      )
    : null;
  box.replaceChildren(
    ...[
      ...(here.length ? here.map(fileItem) : away.length ? [] : [h("p", { class: "none" }, "None in current files")]),
      away.length ? h("p", { class: "group-label" }, "Outside this folder") : null,
      ...away.map(fileItem),
      pastList,
      state.overview.crossings_truncated ? h("p", { class: "none" }, "First 200 shown") : null,
    ].filter(Boolean),
  );
}

// How much of what a session wrote is still in the files.
const remainPct = (s) => Math.round((s.alive / s.written) * 100);

function remainStat(s) {
  return h(
    "span",
    { class: "stat remain", title: `${s.alive.toLocaleString()} of the ${plural(s.written, "line").replace(/^\d+/, s.written.toLocaleString())} it wrote are still in the files` },
    h("span", { class: "share" }, h("i", { style: `width:${remainPct(s)}%;background:${s.color}` })),
    `${remainPct(s)}%`,
  );
}

function renderSessions() {
  const box = $("tab-sessions");
  // Top-level conversations only; helpers are reached from their parent's view.
  const all = [...state.sessions.values()];
  const helpers = new Map();
  for (const s of all) if (s.parent && state.sessions.has(s.parent)) helpers.set(s.parent, (helpers.get(s.parent) || 0) + 1);
  const list = all.filter((s) => (s.edits > 0 || helpers.has(s.id)) && !(s.parent && state.sessions.has(s.parent))).reverse();
  if (!list.length) return box.replaceChildren(h("p", { class: "none" }, "No sessions"));
  box.replaceChildren(
    ...list.map((s) =>
      h(
        "button",
        {
          class: "item session",
          "aria-current": state.focus === s.id ? "true" : "false",
          title: [SOURCE_NAMES[s.agent] || s.agent, s.model].filter(Boolean).join(" · ") || null,
          onclick: () => openSession(s.id),
        },
        h("span", { class: "name" }, h("span", { class: "dots" }, h("i", { class: "dot", style: `background:${s.color}` })), sessionLabel(s.id)),
        h(
          "span",
          { class: "meta" },
          when(s.started_at),
          stat("pencil", s.edits, "edit"),
          s.written ? remainStat(s) : null,
          helpers.has(s.id) ? stat("branch", helpers.get(s.id), "helper session") : null,
        ),
      ),
    ),
  );
}

function selectTab(name) {
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  for (const t of ["files", "crossings", "sessions", "outline", "search"]) $("tab-" + t).hidden = t !== name;
}

/* ---------- Search ---------- */

// Three things can hold the words you remember: a line still in a file, an
// instruction you gave, or a line since deleted. One box searches all three, and
// each current line comes back marked with who wrote it.
const search = { case: false, token: 0, timer: null, result: null, refs: null };
// Instructions are long; the first few leave room for the line results below.
const PROMPT_HITS = 3;

function openSearch() {
  closeMenu();
  selectTab("search");
  const input = $("search-input");
  const picked = String(getSelection()).trim();
  if (picked && picked.length <= 80 && !picked.includes("\n")) {
    input.value = picked;
    runSearch();
  }
  input.focus();
  input.select();
}

function queueSearch() {
  clearTimeout(search.timer);
  search.timer = setTimeout(() => runSearch(), 200);
}

// With `refs`, the search is for the uses of one name, as a whole word.
async function runSearch(refs = null) {
  clearTimeout(search.timer);
  search.refs = refs;
  const q = $("search-input").value;
  const token = ++search.token;
  const box = $("search-results");
  if (q.trim().length < 2) {
    search.result = null;
    return renderSearch();
  }
  let result;
  try {
    result = await api("search", refs ? { q, case: 1, word: 1 } : { q, case: search.case ? 1 : 0 });
  } catch (err) {
    if (token === search.token) box.replaceChildren(h("p", { class: "none" }, err.message));
    return;
  }
  if (token !== search.token) return;
  search.result = result;
  if (result.cypher.length) record(refs ? "Uses of a name" : "Search", result.cypher, { q: result.query });
  renderSearch();
  return result;
}

/* ---------- Uses of a name ---------- */

// The name under a point in the code, with where it sits in its line.
function nameAt(x, y) {
  const pos = document.caretPositionFromPoint?.(x, y);
  const range = pos ? null : document.caretRangeFromPoint?.(x, y);
  const node = pos ? pos.offsetNode : range?.startContainer;
  const offset = pos ? pos.offset : range?.startOffset;
  if (node?.nodeType !== Node.TEXT_NODE || !node.parentElement.matches(".ln > .src")) return null;
  const text = node.data;
  let start = offset;
  let end = offset;
  while (start > 0 && /[\w$]/.test(text[start - 1])) start--;
  while (end < text.length && /[\w$]/.test(text[end])) end++;
  const name = text.slice(start, end);
  if (!name || /^\d/.test(name)) return null;
  return { name, node, start, end, line: Number(node.parentElement.parentElement.dataset.line) };
}

// Lists every use of the name. From a use, it also jumps to the definition when
// the folder has exactly one, the way an editor's go-to-definition does.
async function showUses(name, from = null) {
  selectTab("search");
  $("search-input").value = name;
  const result = await runSearch({ name });
  if (!result || !from) return;
  const defs = result.files.flatMap((f) => f.hits.filter((hit) => hit.def).map((hit) => ({ path: f.path, line: hit.line })));
  if (defs.length !== 1 || (defs[0].path === from.path && defs[0].line === from.line)) return;
  setFind(name, true);
  await openFile(defs[0].path, defs[0].line);
  toast(`Definition of ${name}`, 1600);
}

function markLink(hit) {
  if (!window.Highlight || !CSS.highlights) return;
  if (!hit) return CSS.highlights.delete("link");
  const mark = new Highlight(new StaticRange({ startContainer: hit.node, startOffset: hit.start, endContainer: hit.node, endOffset: hit.end }));
  mark.priority = 3;
  CSS.highlights.set("link", mark);
}

// A result's text with the matched words marked.
function marked(hit) {
  const end = hit.at + hit.size;
  return [hit.cut ? "…" : "", hit.text.slice(0, hit.at), hit.size ? h("mark", {}, hit.text.slice(hit.at, end)) : "", hit.text.slice(end)];
}

// Who wrote a result's line: a session's dot, or the mark for its kind.
function ownerMark(hit) {
  if (hit.session) return h("i", { class: "dot", style: `background:${color(hit.session)}`, title: sessionLabel(hit.session) });
  const kind = KIND[hit.kind];
  return kind ? h("i", { class: `key ${kind.cls}`, title: kind.who }) : h("i", { class: "key" });
}

function searchGroup(head, count, rows) {
  return h("details", { class: "sgroup", open: true }, h("summary", {}, head, h("span", { class: "count" }, count)), rows);
}

function renderSearch() {
  const box = $("search-results");
  const r = search.result;
  if (!r) {
    return box.replaceChildren(
      h("p", { class: "none" }, state.overview ? "Current lines with who wrote them, the instructions you gave, and lines since deleted." : "Lines in this folder's files."),
      h("p", { class: "none" }, h("kbd", {}, `${MOD}Click`), " a name in the code to list where it's used."),
    );
  }
  const lines = r.files.reduce((n, f) => n + f.count, 0);
  if (!lines && !r.prompts.length && !r.deleted.length) return box.replaceChildren(h("p", { class: "none" }, "No results"));
  const openHit = (path, line) => {
    setFind(r.query, r.case);
    openFile(path, line);
  };
  const prompts = r.prompts.length
    ? searchGroup(
        "Instructions",
        r.prompts.length,
        [
          ...r.prompts.map((p, i) =>
            h(
              "button",
              { type: "button", class: "item hit-prompt", hidden: i >= PROMPT_HITS, title: sessionLabel(p.session), onclick: () => openSession(p.session, { prompt: p.ts }) },
              h("span", { class: "name" }, marked(p)),
              h("span", { class: "meta" }, h("i", { class: "dot", style: `background:${color(p.session)}` }), when(p.ts), p.edits ? stat("pencil", p.edits, "edit") : null),
            ),
          ),
          r.prompts.length > PROMPT_HITS
            ? h(
                "button",
                {
                  type: "button",
                  class: "link snote",
                  onclick: (e) => {
                    for (const row of e.target.parentElement.querySelectorAll(".hit-prompt[hidden]")) row.hidden = false;
                    e.target.remove();
                  },
                },
                `Show all ${r.prompts.length}`,
              )
            : null,
        ],
      )
    : null;
  const files = r.files.map((f) => {
    const dir = rel(f.path).slice(0, -base(f.path).length).replace(/\/$/, "");
    return searchGroup(
      [h("span", { class: "sfile", title: rel(f.path) }, base(f.path)), dir ? h("span", { class: "sdir" }, dir) : null],
      f.count,
      [
        f.stale ? h("p", { class: "snote" }, "Changed since the build. Rebuild to see who wrote these.") : null,
        ...f.hits.map((hit) =>
          h(
            "button",
            { type: "button", class: "hit", onclick: () => openHit(f.path, hit.line) },
            ownerMark(hit),
            h("span", { class: "hit-num" }, hit.line),
            h("span", { class: "hit-text" }, marked(hit)),
            hit.def ? h("span", { class: "hit-def", title: "Defined here" }, "def") : null,
          ),
        ),
        f.count > f.hits.length ? h("p", { class: "snote" }, `${f.count - f.hits.length} more in this file`) : null,
      ],
    );
  });
  const deleted = r.deleted.length
    ? searchGroup(
        "Deleted lines",
        r.deleted.length,
        r.deleted.map((d) =>
          h(
            "button",
            {
              type: "button",
              class: "hit gone",
              title: `${rel(d.path)}, ${gapWhere(d.after)}${d.session ? `\nDeleted by ${sessionLabel(d.session)}` : ""}`,
              onclick: () => openFile(d.path, null, { gap: d.after }),
            },
            ownerMark(d),
            h("span", { class: "hit-num" }, base(d.path)),
            h("span", { class: "hit-text" }, marked(d)),
          ),
        ),
      )
    : null;
  const total = lines ? `${plural(lines, r.word ? "use" : "line")}${r.word ? ` of ${r.query}` : ""} in ${plural(r.files.length, "file")}` : "No current lines";
  box.replaceChildren(...[h("p", { class: "group-label" }, r.truncated ? `${total} · first results only` : total), prompts, ...files, deleted].filter(Boolean));
  box.parentElement.scrollTop = 0;
}

/* ---------- Outline ---------- */

// The file's functions and classes, each with the sessions that wrote its lines:
// the line-by-line answer, summed up to the unit people actually talk about.
const CONTROL = new Set("if else for while do switch case catch return throw new delete typeof await yield with try finally function using goto sizeof".split(" "));
const TYPED = /^\s*(?:[\w<>\[\],.*&:~?]+\s+)+([\w~]+)\s*\([^;{]*\)\s*(?:const\s*|throws\s+[\w., ]+)?\{\s*$/d;
const indentOf = (text) => /^\s*/.exec(text)[0].replace(/\t/g, "    ").length;
const JS_TYPE = /(?:(?:export|default|abstract|declare)\s+)*(class|interface|enum)\s+([\w$]+)/y;
const JS_FUNCTION = /((?:export\s+)?(?:default\s+)?)(?:async\s+)?function\b\s*\*?\s*([\w$]+)?\s*(?:<[^>{}]+>\s*)?(?=\()/y;
const JS_BINDING = /(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::[^=\n]+)?=(?!=|>)\s*(?:async\s+(?!=>))?/y;
const JS_EXPRESSION = /function\b\s*\*?\s*(?:[\w$]+\s*)?(?:<[^>{}]+>\s*)?(?=\()/y;
const JS_METHOD = /(?:(?:async|static|get|set|public|private|protected|override|readonly|abstract)\s+)*\*?\s*([#\w$]+)\s*(?:<[^>{}]+>\s*)?(?=\()/y;
const JS_RESULT = /\s*(?::\s*(?:\{[^{}]*\}(?=\s*(?:\{|=>|;))|[^={};\n]+))?\s*/y;
const GO_TYPE = /type\s+(\w+)(?:\[[^\]\n]*\])?\s*(?:=\s*)?(?:struct|interface)\s*/y;
const GO_FUNC = /func\b[ \t]*/y;
const GO_NAME = /\s*(\w+)\s*(?:\[[^\]\n]*\]\s*)?(?=\()/y;
const GO_RESULT = /[ \t]*(?:[\w[\].*,<>-]+[ \t]*)*/y;
const JAVA_TYPE = /(?:(?:public|private|protected|static|abstract|final|strictfp|sealed|non-sealed)\s+)*(class|interface|enum|record)\s+([\w$]+)/y;
const JAVA_METHOD = /(?:(?:public|private|protected|static|abstract|final|native|strictfp|synchronized|default)\s+)*(?:<[^{};]+?>\s*)?([\w$][\w$.\[\]<>?, \t]*?)\s+([\w$]+)\s*(?=\()/y;
const JAVA_CONSTRUCTOR = /(?:(?:public|private|protected)\s+)*(?:<[^{};]+?>\s*)?([\w$]+)\s*(?=\()/y;
const JAVA_RESULT = /\s*(?:throws\s+[\w$.,\s]+)?/y;

function outlineMatch(pattern, code, at) {
  pattern.lastIndex = at;
  return pattern.exec(code);
}

// Index pairs of brackets and the enclosing brace at each line. Mask comments
// and literals first so braces in text cannot change a declaration's extent.
function outlineBrackets(lines, tokens) {
  const starts = [], parents = [], pairs = new Map(), objects = new Set();
  const stack = [], braces = [];
  let offset = 0, previous = "";
  const code = lines.map((text, i) => {
    const parts = [];
    let from = 0;
    for (const [start, end, kind] of tokens[i]) {
      if (kind === "comment" || kind === "string") {
        parts.push(text.slice(from, start), (kind === "string" ? "~" : " ") + " ".repeat(end - start - 1));
        from = end;
      }
    }
    parts.push(text.slice(from));
    const clean = parts.join("");
    starts.push(offset + clean.search(/\S|$/));
    parents.push(braces.at(-1));
    for (const token of clean.matchAll(/=>|[\w$]+|\S/g)) {
      const value = token[0], at = offset + token.index;
      if ("{([".includes(value)) {
        stack.push({ value, at });
        if (value === "{") {
          if (["=", "(", "[", ",", "return"].includes(previous) ||
              (previous === ":" && objects.has(braces.at(-1)))) objects.add(at);
          braces.push(at);
        }
      } else if ("})]".includes(value) && stack.at(-1)?.value === { "}": "{", ")": "(", "]": "[" }[value]) {
        pairs.set(stack.pop().at, { at, line: i + 1 });
        if (value === "}") braces.pop();
      }
      previous = value;
    }
    offset += text.length + 1;
    return clean;
  }).join("\n");
  return { code, starts, parents, pairs, objects };
}

function braceOutline(lines, tokens, lang) {
  const { code, starts, parents, pairs, objects } = outlineBrackets(lines, tokens);
  const containers = new Map();
  const items = [];
  const match = (pattern, at) => outlineMatch(pattern, code, at);
  const after = (m) => m.index + m[0].length;
  const skip = (at) => after(match(/\s*/y, at));
  const closeOf = (at) => pairs.has(at) ? pairs.get(at).at + 1 : null;
  const typeBody = (at) => after(match(/[^\n;{]*\s*/y, at));
  const jsBody = (at) => after(match(JS_RESULT, at));

  lines.forEach((text, i) => {
    const at = starts[i], parent = parents[i], owner = containers.get(parent);
    if (code[at] === "\n" || at === code.length) return;
    const add = (name, kind, body, last = at) => {
      const line = i + 1;
      const end = pairs.get(body)?.line ?? line + (code.slice(at, last).match(/\n/g) || []).length;
      items.push({ line, name, kind, indent: indentOf(text), end });
    };
    const type = lang === "javascript" ? match(JS_TYPE, at) : lang === "java" ? match(JAVA_TYPE, at) : null;
    if (type) {
      const body = typeBody(after(type));
      if (code[body] === "{") containers.set(body, { name: type[2], kind: type[1] });
      return add(type[2], type[1], body);
    }

    if (lang === "javascript") {
      const decl = match(JS_FUNCTION, at);
      if (decl && (decl[2] || /\bdefault\b/.test(decl[1]))) {
        const last = closeOf(after(decl));
        return add(decl[2] || "default", "function", last == null ? null : jsBody(last), last ?? at);
      }
      const binding = match(JS_BINDING, at);
      if (binding) {
        const value = after(binding), expression = match(JS_EXPRESSION, value);
        if (expression) {
          const last = closeOf(after(expression));
          return add(binding[1], "function", last == null ? null : jsBody(last), last ?? at);
        }
        const params = match(/(?:<[^>{}]+>\s*)?(?=\()/y, value);
        const single = match(/[\w$]+\s*(?==>)/y, value);
        const last = params ? closeOf(after(params)) : single ? after(single) : null;
        if (last != null && code.startsWith("=>", jsBody(last))) {
          const body = skip(jsBody(last) + 2);
          return add(binding[1], "function", code[body] === "{" ? body : null, last);
        }
      }
      if (!owner && !objects.has(parent)) return;
      const method = match(JS_METHOD, at);
      if (!method || CONTROL.has(method[1])) return;
      const last = closeOf(after(method));
      if (last == null) return;
      const body = jsBody(last);
      if (code[body] === "{" || (owner?.kind === "interface" && code[body] === ";")) {
        const kind = owner?.kind === "class" && method[1] === "constructor" ? "constructor" : "method";
        add(method[1], kind, code[body] === "{" ? body : null, last);
      }
    } else if (lang === "go") {
      const type = match(GO_TYPE, at);
      if (type) return add(type[1], "type", after(type));
      const alias = match(/type[ \t]+(\w+)/y, at);
      if (alias) return add(alias[1], "type");
      const func = match(GO_FUNC, at);
      if (!func) return;
      const start = after(func);
      const nameAt = code[start] === "(" ? closeOf(start) : start;
      const name = nameAt == null ? null : match(GO_NAME, nameAt);
      if (!name) return;
      let last = closeOf(after(name));
      if (last == null) return add(name[1], "func");
      const result = skip(last);
      if (code[result] === "(") last = closeOf(result) ?? last;
      const body = after(match(GO_RESULT, last));
      return add(name[1], "func", code[body] === "{" ? body : null, last);
    } else if (owner) {
      const method = match(JAVA_METHOD, at);
      const constructor = match(JAVA_CONSTRUCTOR, at);
      const isConstructor = constructor?.[1] === owner.name;
      const signature = isConstructor ? constructor : method;
      const name = isConstructor ? owner.name : method?.[2];
      if (!signature || CONTROL.has(name) || CONTROL.has(method?.[1].split(/\s/)[0])) return;
      const last = closeOf(after(signature));
      if (last == null) return;
      const body = after(match(JAVA_RESULT, last));
      if (code[body] === "{" || (!isConstructor && code[body] === ";")) {
        add(name, isConstructor ? "constructor" : "method", code[body] === "{" ? body : null, last);
      }
    }
  });
  return items;
}

function syntaxTokens(file) {
  if (!file.lines || file.lines.length > MAX_SYNTAX_LINES) return null;
  if (file.tokens === undefined) file.tokens = SYNTAX.tokens(file.lines, file.path);
  return file.tokens;
}

function outlineOf(file) {
  const lines = file?.lines;
  const tokens = lines && syntaxTokens(file);
  if (!tokens) return [];
  const lang = SYNTAX.language(file.path, lines[0]);
  const braces = lang === "javascript" || lang === "go" || (lang === "java" && /\.java$/i.test(file.path));
  const extra = ["c", "java", "csharp", "php"].includes(lang) ? [TYPED] : [];
  const items = braces ? braceOutline(lines, tokens, lang) : [];
  if (!braces) lines.forEach((text, i) => {
    if (lang === "markdown") {
      const m = tokens[i][0]?.[2] === "keyword" && /^(#{1,6})\s+(.*)/.exec(text);
      if (m) items.push({ line: i + 1, name: m[2].trim(), kind: "", indent: m[1].length, heading: true });
      return;
    }
    const def = tokens[i].find((t) => t[2] === "def");
    if (def) {
      const kind = /([\w$]+)\W*$/.exec(text.slice(0, def[0]))?.[1] || "";
      return items.push({ line: i + 1, name: text.slice(def[0], def[1]), kind, indent: indentOf(text) });
    }
    for (const pattern of extra) {
      const m = pattern.exec(text);
      if (!m || CONTROL.has(m[1]) || CONTROL.has(text.trim().split(/\W/)[0])) continue;
      // A match inside a comment or a string is only words.
      const at = m.indices[1][0];
      if (tokens[i].some((t) => (t[2] === "comment" || t[2] === "string") && t[0] <= at && at < t[1])) continue;
      return items.push({ line: i + 1, name: m[1], kind: "", indent: indentOf(text) });
    }
  });
  const open = [];
  items.forEach((item, k) => {
    while (open.length && (braces ? open.at(-1).end < item.line : open.at(-1).indent >= item.indent)) open.pop();
    item.depth = open.length;
    open.push(item);
    // The body runs while lines stay indented deeper, through its closing bracket.
    let end = item.line;
    if (braces) {
      end = item.end;
    } else if (item.heading) {
      const next = items.slice(k + 1).find((other) => other.indent <= item.indent);
      end = next ? next.line - 1 : lines.length;
      while (end > item.line && !lines[end - 1].trim()) end--;
    } else {
      for (let n = item.line + 1; n <= lines.length; n++) {
        const text = lines[n - 1];
        if (!text.trim()) continue;
        if (indentOf(text) <= item.indent && !/^\s*(?:[)\]}]|end\b)/.test(text)) break;
        end = n;
      }
    }
    item.end = end;
    // Who owns its lines now, most lines first, then who wrote there before.
    const owned = new Map();
    const kinds = new Map();
    const past = new Set();
    for (let n = item.line; n <= end; n++) {
      const range = state.byLine[n];
      if (!range) continue;
      const ids = laneIds(range);
      if (range.kind === "agent" && ids.length) owned.set(ids[0], (owned.get(ids[0]) || 0) + 1);
      else kinds.set(range.kind, (kinds.get(range.kind) || 0) + 1);
      for (const id of ids) past.add(id);
    }
    item.owners = [...owned].sort((a, b) => b[1] - a[1]);
    item.past = [...past].filter((id) => !owned.has(id));
    item.kindOf = [...kinds].sort((a, b) => b[1] - a[1])[0]?.[0] || null;
  });
  return items;
}

function outlineRow(item) {
  const ids = [...item.owners.map(([id]) => id), ...item.past];
  const shown = ids.slice(0, 5);
  const tip = [
    `Lines ${item.line}–${item.end}`,
    ...item.owners.map(([id, n]) => `${sessionLabel(id)}: ${plural(n, "line")}`),
    ...item.past.map((id) => `${sessionLabel(id)}: rewritten since`),
  ].join("\n");
  return h(
    "button",
    {
      type: "button",
      class: state.focus && !ids.includes(state.focus) ? "oitem dim" : "oitem",
      style: `--depth:${item.depth}`,
      title: tip,
      "data-line": item.line,
      onclick: () => selectLine(item.line, { scroll: true, span: item }),
    },
    item.kind ? h("span", { class: "okind" }, item.kind) : null,
    h("span", { class: "oname" }, item.name),
    ids.length > shown.length ? h("span", { class: "okind" }, `+${ids.length - shown.length}`) : null,
    shown.length
      ? h("span", { class: "dots" }, shown.map((id, k) => h("i", { class: k < item.owners.length ? "dot" : "dot past", style: `background:${color(id)}` })))
      : item.kindOf && KIND[item.kindOf]
        ? h("i", { class: `key ${KIND[item.kindOf].cls}` })
        : null,
  );
}

function renderOutline() {
  const box = $("tab-outline");
  const file = state.file;
  state.outline = file?.lines ? outlineOf(file) : [];
  if (!file?.lines) return box.replaceChildren(h("p", { class: "none" }, "Open a file to see its functions and who wrote them."));
  if (!state.outline.length) return box.replaceChildren(h("p", { class: "none" }, `No functions or classes found in ${base(file.path)}.`));
  const sessions = new Set(state.outline.flatMap((item) => item.owners.map(([id]) => id)));
  box.replaceChildren(
    h("p", { class: "ohead" }, h("b", {}, base(file.path)), h("span", {}, [plural(state.outline.length, "item"), sessions.size ? plural(sessions.size, "session") : ""].filter(Boolean).join(" · "))),
    ...state.outline.map(outlineRow),
  );
  markOutline();
}

// The innermost item around the selected line.
function markOutline() {
  const box = $("tab-outline");
  box.querySelector('.oitem[aria-current="true"]')?.removeAttribute("aria-current");
  if (!state.line || !state.outline?.length) return;
  const at = state.outline.findLast((item) => item.line <= state.line && state.line <= item.end);
  const row = at && box.querySelector(`.oitem[data-line="${at.line}"]`);
  if (!row) return;
  row.setAttribute("aria-current", "true");
  if (!box.hidden) row.scrollIntoView({ block: "nearest" });
}

/* ---------- Find in file ---------- */

// Matches are painted with the CSS highlight API, so the line rows stay untouched.
const find = { query: "", case: false, hits: [], at: -1 };
const MAX_FIND = 5000;

function setFind(query, matchCase = find.case) {
  Object.assign(find, { query, case: matchCase, at: -1 });
  $("find-case").setAttribute("aria-pressed", String(find.case));
  paintFind();
}

function paintFind() {
  find.hits = [];
  const lines = state.file?.lines;
  if (find.query && lines) {
    const needle = find.case ? find.query : find.query.toLowerCase();
    for (let i = 0; i < lines.length && find.hits.length < MAX_FIND; i++) {
      const text = find.case ? lines[i] : lines[i].toLowerCase();
      for (let at = text.indexOf(needle); at >= 0 && find.hits.length < MAX_FIND; at = text.indexOf(needle, at + needle.length)) {
        find.hits.push({ line: i + 1, at });
      }
    }
  }
  if (find.at >= find.hits.length) find.at = -1;
  drawFind();
}

function drawFind() {
  const n = find.hits.length;
  if (window.Highlight && CSS.highlights) {
    const range = (hit) => {
      const node = $(`L${hit.line}`)?.querySelector(".src")?.firstChild;
      if (!node) return null;
      try {
        const r = new Range();
        r.setStart(node, hit.at);
        r.setEnd(node, hit.at + find.query.length);
        return r;
      } catch {
        return null;
      }
    };
    const all = find.hits.map(range).filter(Boolean);
    if (all.length) {
      const mark = new Highlight(...all);
      mark.priority = 1; // above the syntax colors
      CSS.highlights.set("find", mark);
    } else CSS.highlights.delete("find");
    const current = find.hits[find.at] && range(find.hits[find.at]);
    if (current) {
      const mark = new Highlight(current);
      mark.priority = 2;
      CSS.highlights.set("find-current", mark);
    } else CSS.highlights.delete("find-current");
  }
  $("find-count").textContent = !find.query ? "" : !n ? "No results" : find.at >= 0 ? `${find.at + 1} of ${n}` : n >= MAX_FIND ? `${n}+` : String(n);
  $("find-prev").disabled = $("find-next").disabled = !n;
}

// The first match at or after the selected line, or the last one at or before it.
function nearestHit(step) {
  const line = state.line || 1;
  const i = step > 0 ? find.hits.findIndex((hit) => hit.line >= line) : find.hits.findLastIndex((hit) => hit.line <= line);
  return i >= 0 ? i : step > 0 ? 0 : find.hits.length - 1;
}

// Stepping to a match selects its line, so the panel shows who wrote it.
function stepFind(step) {
  const n = find.hits.length;
  if (!n) return;
  find.at = find.at < 0 ? nearestHit(step) : (find.at + step + n) % n;
  drawFind();
  selectLine(find.hits[find.at].line, { scroll: true });
}

function openFind() {
  if (!state.file?.lines) return toast("Open a file first.");
  closeMenu();
  const input = $("find-input");
  const picked = String(getSelection()).trim();
  if (picked && picked.length <= 80 && !picked.includes("\n")) input.value = picked;
  else if (find.query) input.value = find.query;
  $("find").hidden = false;
  input.focus();
  input.select();
  if (input.value !== find.query) typeFind();
  else drawFind();
}

function typeFind() {
  setFind($("find-input").value);
  if (find.hits.length) $(`L${find.hits[nearestHit(1)].line}`)?.scrollIntoView({ block: "center" });
}

function closeFind() {
  const open = !$("find").hidden;
  $("find").hidden = true;
  setFind("");
  if (open) $("code-scroll").focus({ preventScroll: true });
}

/* ---------- File view ---------- */

async function openFile(path, line = null, { keepFocus = true, keepPanel = false, gap = null, panel } = {}) {
  const request = ++nav.fileRequest;
  let file;
  try {
    file = await api("file", { path });
  } catch (err) {
    if (request === nav.fileRequest) toast(err.message);
    return;
  }
  if (request !== nav.fileRequest) return;
  state.file = file;
  if (!keepFocus) state.focus = null;
  addTab(file.path);
  ensureExpanded(file.path);
  record("File tracks", file.cypher, { path: rel(path) });
  state.byLine = [];
  for (const r of file.tracks) for (let n = r.start; n <= r.end; n++) state.byLine[n] = r;
  // A file edited after the build can be shorter than the graph remembers.
  const last = file.lines?.length ?? 0;
  state.gapsAt = new Map();
  for (const g of file.gaps || []) {
    const at = Math.min(g.after, last);
    if (!state.gapsAt.has(at)) state.gapsAt.set(at, []);
    state.gapsAt.get(at).unshift(g);
  }
  state.openGaps = new Set();
  state.fullGaps = new Set();
  state.line = null;
  state.gap = null;
  renderFile();
  renderTabs();
  renderFiles();
  renderCrossings();
  $("tab-files").querySelector('.tree-row[aria-current="true"]')?.scrollIntoView({ block: "nearest" });
  if (gap != null && state.gapsAt.has(gap)) selectGap(gap, { open: true, scroll: true, panel });
  else if (line && file.lines?.length) selectLine(line, { scroll: true, panel });
  else if (panel || !keepPanel) showPanel(panel || { view: "intro" });
  else if (state.backTo && $("back-label")) $("back-label").textContent = backLabel();
  writeHash();
}

/* ---------- Editor tabs ---------- */

function addTab(path) {
  if (state.tabs.includes(path)) return;
  state.tabs.push(path);
  while (state.tabs.length > MAX_TABS) state.tabs.splice(state.tabs.findIndex((p) => p !== path), 1);
}

function renderTabs() {
  const bar = $("editor-tabs");
  bar.hidden = !state.tabs.length;
  bar.replaceChildren(
    ...state.tabs.map((p) => {
      const active = state.file?.path === p;
      const tab = h(
        "div",
        { class: active ? "etab active" : "etab", title: rel(p) },
        h("button", { type: "button", class: "etab-name", role: "tab", "aria-selected": String(active), onclick: () => !active && openFile(p) }, base(p)),
        h("button", { type: "button", class: "etab-close", "aria-label": `Close ${base(p)}`, title: `Close ${base(p)}`, onclick: () => closeTab(p) }, "×"),
      );
      tab.addEventListener("auxclick", (e) => e.button === 1 && closeTab(p));
      return tab;
    }),
  );
  bar.querySelector(".etab.active")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function closeTab(path) {
  const i = state.tabs.indexOf(path);
  if (i < 0) return;
  state.tabs.splice(i, 1);
  if (state.file?.path !== path) return renderTabs();
  const next = state.tabs[i] || state.tabs[i - 1];
  if (next) return openFile(next);
  clearFile();
  if (!state.overview && state.workspace?.project) showBuild();
}

function clearFile() {
  state.file = null;
  state.line = null;
  state.byLine = [];
  state.gapsAt = new Map();
  state.gap = null;
  $("gaps-toggle").hidden = true;
  $("file-title").textContent = base(state.root.replace(/\/$/, ""));
  $("file-note").hidden = true;
  $("hover").replaceChildren();
  $("lines").replaceChildren();
  $("find").hidden = true;
  paintSyntax();
  paintFind();
  renderOutline();
  $("code-empty").hidden = false;
  $("code-empty").replaceChildren(...(state.overview ? ["Pick a file ", h("kbd", {}, `${MOD}P`), ", then click a line."] : []));
  renderFocus();
  renderTabs();
  if (state.overview) {
    renderFiles();
    renderCrossings();
  }
  showIntro();
  writeHash();
}

function laneIds(range) {
  const seen = [];
  for (const lane of range?.lanes || []) if (!seen.includes(lane.session)) seen.push(lane.session);
  return seen;
}

function renderFile() {
  const f = state.file;
  const dir = rel(f.path).split("/");
  const name = dir.pop();
  $("file-title").replaceChildren(dir.length ? h("span", { class: "dir" }, dir.join("/") + "/") : "", name);
  const untracked = f.status === "untracked" && !f.note;
  const note = $("file-note");
  note.hidden = !f.note && !untracked;
  note.classList.toggle("quiet", untracked);
  // A file that changed after the build lines up again once the history is rebuilt.
  const stale = f.note && f.lines && f.status !== "untracked" && state.workspace?.project;
  note.replaceChildren(
    ...(f.note ? [icon("alert"), h("span", {}, f.note)] : [untracked ? (state.overview ? "No agent edits" : "No history yet") : ""]),
    stale ? h("button", { type: "button", onclick: () => buildHistory() }, "Rebuild") : "",
  );
  $("code-empty").hidden = !!f.lines;
  if (!f.lines) $("code-empty").textContent = f.note || "Empty file";
  renderFocus();

  let width = 1;
  for (const r of f.tracks) width = Math.max(width, Math.min(MAX_LANES, laneIds(r).length + (r.kind === "agent" ? 0 : 1)));
  $("lines").style.setProperty("--tracks-w", `${12 + width * 8}px`);

  const lines = f.lines || [];
  const rows = [];
  for (let i = 0; i <= lines.length; i++) {
    if (state.gapsAt.has(i)) rows.push(gapRow(i));
    if (i < lines.length) rows.push(lineRow(lines[i], i + 1));
  }
  $("lines").replaceChildren(...rows);
  renderGapsToggle();
  paintSyntax();
  paintFind();
  renderOutline();
}

// Syntax colors are highlight ranges over each line's one text node, like the
// find marks, so the rows hold plain text and offsets into a line stay true.
const MAX_SYNTAX_LINES = 20000;

function paintSyntax() {
  if (!window.Highlight || !CSS.highlights) return;
  for (const type of SYNTAX.TYPES) CSS.highlights.delete(`syn-${type}`);
  const tokens = state.file?.lines && syntaxTokens(state.file);
  if (!tokens) return;
  const rows = document.querySelectorAll("#lines > .ln > .src");
  const marks = new Map();
  tokens.forEach((list, i) => {
    const node = rows[i]?.firstChild;
    if (!node) return;
    for (const [start, end, type] of list) {
      if (!marks.has(type)) marks.set(type, new Highlight());
      marks.get(type).add(new StaticRange({ startContainer: node, startOffset: start, endContainer: node, endOffset: end }));
    }
  });
  for (const [type, mark] of marks) CSS.highlights.set(`syn-${type}`, mark);
}

function lineRow(text, n) {
  const range = state.byLine[n];
  const ids = laneIds(range);
  const tracks = [];
  if (range && range.kind !== "agent") tracks.push(h("i", { class: KIND[range.kind]?.cls || "k-unknown" }));
  ids.slice(0, MAX_LANES - tracks.length).forEach((id, k) => tracks.push(h("i", { class: k || tracks.length ? "older" : "", style: `background:${color(id)}` })));
  const cls = ["ln"];
  if (range && range.start === n) cls.push("top");
  if (ids.length >= 2) cls.push("cross");
  if (state.focus && !ids.includes(state.focus)) cls.push("dim");
  return h(
    "li",
    { class: cls.join(" "), "data-line": n, id: `L${n}`, title: ids.length >= 2 ? `${ids.length} sessions rewrote this spot` : null },
    h("span", { class: "tracks", "aria-hidden": "true" }, tracks),
    h("span", { class: "num" }, n),
    h("span", { class: "src" }, text || " "),
  );
}

/* ---------- Deleted lines ---------- */

// A deletion has no line of its own. It sits between two lines as a small mark
// in the deleter's color; opened, it shows the lines that used to be there.
const gapColor = (g) => (g.session ? color(g.session) : "var(--muted)");
const gapWhere = (after) => (after ? `after line ${after}` : "at the top");
const gapLines = (list) => list.reduce((n, g) => n + (g.removed || 0), 0);

function gapRow(after) {
  const list = state.gapsAt.get(after);
  const open = state.showGaps || state.openGaps.has(after);
  const cls = ["gap"];
  if (open) cls.push("open");
  if (state.gap === after && !state.line) cls.push("sel");
  if (state.focus && !list.some((g) => g.session === state.focus)) cls.push("dim");
  const label = `${plural(gapLines(list), "line")} deleted ${gapWhere(after)}`;
  return h(
    "li",
    { class: cls.join(" "), "data-after": after, id: `G${after}`, style: `--gap:${gapColor(list[0])}` },
    h("button", { type: "button", class: "gap-mark", "aria-expanded": String(open), "aria-label": open ? `Hide the ${label}` : `Show the ${label}`, title: label }),
    open ? list.map((g) => gapBlock(g, after)) : null,
  );
}

function gapBlock(g, after) {
  const kind = g.kind === "agent" && g.session ? null : KIND[g.kind] || KIND.unknown;
  const who = kind
    ? [h("i", { class: `key ${kind.cls}` }), h("span", { class: "who" }, kind.who)]
    : [h("i", { class: "dot", style: `background:${color(g.session)}` }), h("span", { class: "who" }, sessionLabel(g.session))];
  const text = g.text ? g.text.replace(/\n$/, "").split("\n") : [];
  const full = state.fullGaps.has(g.id);
  const shown = full ? text : text.slice(0, GAP_PREVIEW);
  const ghost = (src) =>
    h("div", { class: "gap-ln" }, h("span", { class: "tracks", "aria-hidden": "true" }, h("i")), h("span", { class: "num", "aria-hidden": "true" }, "−"), h("span", { class: "src" }, src || " "));
  return h(
    "div",
    { class: "gap-block", style: `--gap:${gapColor(g)}`, "data-gap": g.id },
    h(
      "div",
      { class: "gap-head", title: kind ? GAP_TIP[g.kind] || kind.tip : null },
      h("span", { class: "tracks", "aria-hidden": "true" }),
      h("span", { class: "num" }),
      h("span", { class: "gap-what" }, h("b", {}, `Deleted ${plural(g.removed, "line")}`), ...who, g.ts ? h("span", { class: "when" }, shortWhen(g.ts)) : null),
    ),
    text.length ? shown.map(ghost) : h("div", { class: "gap-ln none" }, h("span", { class: "tracks" }), h("span", { class: "num" }), h("span", { class: "src" }, "The logs don't show these lines' text")),
    text.length > shown.length
      ? h(
          "button",
          {
            type: "button",
            class: "gap-more",
            onclick: (e) => {
              e.stopPropagation();
              state.fullGaps.add(g.id);
              redrawGap(after);
            },
          },
          `Show ${plural(text.length - shown.length, "more line")}`,
        )
      : null,
  );
}

function redrawGap(after) {
  $(`G${after}`)?.replaceWith(gapRow(after));
}

function renderGapsToggle() {
  const button = $("gaps-toggle");
  const spots = state.gapsAt.size;
  button.hidden = !spots;
  if (!spots) return;
  const lines = [...state.gapsAt.values()].reduce((n, list) => n + gapLines(list), 0);
  button.setAttribute("aria-pressed", String(state.showGaps));
  button.title = state.showGaps ? "Hide the deleted lines" : `Show ${plural(lines, "deleted line")} in place, in ${plural(spots, "spot")}`;
  button.replaceChildren(h("i", { class: "gap-key", "aria-hidden": "true" }), "Deleted", h("span", { class: "count" }, spots));
}

function toggleAllGaps() {
  state.showGaps = !state.showGaps;
  store("papertrail-show-deletions", state.showGaps);
  state.openGaps.clear();
  const keep = state.line ? $(`L${state.line}`) : state.gap != null ? $(`G${state.gap}`) : null;
  const top = keep?.getBoundingClientRect().top;
  renderFile();
  // Keep the selected line where it was while blocks open and close above it.
  const again = state.line ? $(`L${state.line}`) : state.gap != null ? $(`G${state.gap}`) : null;
  if (again && top != null) $("code-scroll").scrollTop += again.getBoundingClientRect().top - top;
}

function toggleGap(after) {
  if (state.showGaps) return;
  if (state.openGaps.has(after)) state.openGaps.delete(after);
  else state.openGaps.add(after);
  redrawGap(after);
}

function describeGap(after) {
  const list = state.gapsAt.get(after) || [];
  const g = list[0];
  if (!g) return [];
  const ids = [...new Set(list.map((x) => x.session).filter(Boolean))];
  const kind = g.kind === "agent" && g.session ? null : KIND[g.kind] || KIND.unknown;
  return [
    h("b", {}, after ? `Ln ${after}–${after + 1}` : "Top"),
    `${plural(gapLines(list), "line")} deleted`,
    ids.length ? dots(ids) : h("i", { class: `key ${kind.cls}` }),
    h("span", { class: "owner" }, kind ? kind.who : sessionLabel(g.session)),
    g.ts ? h("span", { class: "when" }, when(g.ts)) : "",
  ];
}

function selectGap(after, { open = false, scroll = false, panel } = {}) {
  const list = state.gapsAt.get(after);
  if (!list) return;
  state.line = null;
  state.gap = after;
  if (open && !state.showGaps && !state.openGaps.has(after)) {
    state.openGaps.add(after);
    redrawGap(after);
  }
  markSelected();
  if (scroll) $(`G${after}`)?.scrollIntoView({ block: "center" });
  showPanel(panel || { view: "trace", path: state.file.path, gap: after });
  writeHash();
}

function describeOwner(n) {
  const range = state.byLine[n];
  const num = h("b", {}, `Ln ${n}`);
  if (!range) return [num];
  // One dot per session that wrote this spot, newest first.
  const ids = laneIds(range);
  if (range.kind !== "agent") {
    const kind = KIND[range.kind] || KIND.unknown;
    return [num, h("i", { class: `key ${kind.cls}` }), kind.who, ids.length ? dots(ids) : ""];
  }
  const s = state.sessions.get(ids[0]);
  return [num, dots(ids), h("span", { class: "owner" }, sessionLabel(ids[0])), s ? h("span", { class: "when" }, when(s.started_at)) : ""];
}

// Runs of lines the focused session wrote or rewrote, top to bottom.
function focusBlocks() {
  const blocks = [];
  const n = state.file?.lines?.length || 0;
  for (let i = 1; i <= n; i++) {
    if (!laneIds(state.byLine[i]).includes(state.focus)) continue;
    const last = blocks[blocks.length - 1];
    if (last && last.end === i - 1) last.end = i;
    else blocks.push({ start: i, end: i });
  }
  return blocks;
}

// Scrolls without selecting, so a conversation open in the panel stays open.
function stepFocus(step) {
  const blocks = focusBlocks();
  if (!blocks.length) return;
  const n = blocks.length;
  const i = state.focusAt == null ? (step > 0 ? 0 : n - 1) : (state.focusAt + step + n) % n;
  state.focusAt = i;
  const row = $(`L${blocks[i].start}`);
  row?.scrollIntoView({ block: "center" });
  for (let k = blocks[i].start; k <= blocks[i].end; k++) {
    const el = $(`L${k}`);
    el.classList.remove("flash");
    void el.offsetWidth;
    el.classList.add("flash");
  }
  $("focus-at").textContent = `${i + 1} of ${n}`;
}

function renderFocus() {
  const box = $("focus");
  if (!state.focus) return (box.hidden = true);
  box.hidden = false;
  state.focusAt = null;
  const blocks = focusBlocks();
  const lines = blocks.reduce((n, b) => n + b.end - b.start + 1, 0);
  box.replaceChildren(
    ...[
      h("i", { class: "dot", style: `background:${color(state.focus)}` }),
      h("span", { class: "focus-name", title: "Only this session's lines are highlighted" }, sessionLabel(state.focus)),
      state.file?.lines
        ? h("span", { class: "focus-count", id: "focus-at", title: blocks.length ? `${plural(lines, "line")} in ${plural(blocks.length, "place")} in this file` : null }, blocks.length ? plural(lines, "line") : "No lines here")
        : null,
      blocks.length > 1 || (blocks.length && lines < state.file.lines.length)
        ? [
            h("button", { type: "button", class: "icon-btn bare", title: "Previous place this session wrote", "aria-label": "Previous place this session wrote", onclick: () => stepFocus(-1) }, icon("up")),
            h("button", { type: "button", class: "icon-btn bare", title: "Next place this session wrote", "aria-label": "Next place this session wrote", onclick: () => stepFocus(1) }, icon("down")),
          ]
        : null,
      h("button", { type: "button", class: "icon-btn bare", title: "Show all lines (Esc)", "aria-label": "Show all lines", onclick: () => setFocus(null) }, icon("close")),
    ].flat().filter(Boolean),
  );
}

function setFocus(id) {
  state.focus = id;
  if (state.file) renderFile();
  if (state.line) markSelected();
  renderSessions();
  writeHash();
}

function markSelected() {
  for (const el of document.querySelectorAll(".ln.sel, .gap.sel")) el.classList.remove("sel");
  if (state.line) $(`L${state.line}`)?.classList.add("sel");
  else if (state.gap != null) $(`G${state.gap}`)?.classList.add("sel");
  markOutline();
}

function selectLine(n, { scroll = false, span = null, panel } = {}) {
  if (!state.file?.lines?.length) return;
  n = Math.max(1, Math.min(state.file.lines.length, n));
  state.line = n;
  state.gap = null;
  markSelected();
  if (scroll) $(`L${n}`)?.scrollIntoView({ block: "center" });
  if (!panel) {
    const path = state.file.path;
    if (span && state.file.status !== "untracked") {
      const { line, end, name, kind } = span;
      panel = { view: "span", path, span: { line, end, name, kind } };
    } else {
      panel = { view: "trace", path, line: n, text: state.file.lines[n - 1] };
    }
  }
  showPanel(panel);
  writeHash();
}

/* ---------- History panel ---------- */

// The panel shows either a plain message or the conversation view. The
// conversation view keeps its line header fixed and scrolls only the chat.
function setPanel(nodes, chat = false) {
  const panel = $("panel");
  panel.classList.toggle("chat-mode", chat);
  panel.replaceChildren(...[nodes].flat().filter(Boolean));
}

function showIntro() {
  showPanel({ view: "intro" });
}

function showPanel(panel) {
  state.panel = { ...panel };
  state.trace = null;
  if (panel.view === "conversation") return renderSession(panel);
  if (panel.view === "span" || panel.view === "trace") {
    setPanel(h("div", { class: "empty" }, "Loading history…"));
    if (panel.view === "span") return loadSpan(state.panel);
    return panel.gap == null ? loadTrace(state.panel) : loadGapTrace(state.panel);
  }
  setPanel(h("div", { class: "empty" }, "Click a line to see who wrote it, and the conversation behind it."));
}

// The clicked line, quoted under the heading. A blank line has nothing to quote.
function snippet(n) {
  const text = state.file.lines?.[n - 1] || "";
  return text.trim() ? h("pre", { class: "snippet" }, text) : null;
}

function panelError(text) {
  setPanel(h("div", { class: "empty" }, text));
}

async function loadTrace(panel) {
  const { path, line: n, text } = panel;
  if (state.file?.path === path && state.file.status === "untracked") {
    return setPanel([
      h("div", { class: "panel-head" }, h("h2", {}, `Line ${n}`), snippet(n)),
      h("div", { class: "empty" }, state.overview ? "No agent edits in this file" : "No history yet"),
    ]);
  }
  let result;
  try {
    result = await api("trace", { path, line: n });
  } catch (err) {
    if (state.panel === panel) panelError(err.message);
    return;
  }
  if (state.panel !== panel) return;
  record("Line history", result.cypher, { path: rel(path), line: n });
  // One stop per change to this line, newest first.
  const seen = new Set();
  const stops = result.rows.filter((r) => {
    const key = r.chain[r.chain.length - 1];
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const first = stops.findIndex((r) => r.kind === "agent" && r.session);
  state.trace = { n, path, text: text || "", stops, pick: Math.max(0, first) };
  renderTraceView();
}

// A whole function's history, from the outline: one stop per instruction that
// shaped its lines, shown the way a single line's changes are.
async function loadSpan(panel) {
  const { path, span: item } = panel;
  let result;
  try {
    result = await api("span", { path, start: item.line, end: item.end });
  } catch (err) {
    if (state.panel === panel) panelError(err.message);
    return;
  }
  if (state.panel !== panel) return;
  record("Function history", result.cypher, { path: rel(path), start: result.start, end: result.end });
  const first = result.rows.findIndex((r) => r.kind === "agent" && r.session);
  state.trace = { n: item.line, path, text: "", stops: result.rows, pick: Math.max(0, first), span: { ...item, end: result.end } };
  renderTraceView();
}

async function loadGapTrace(panel) {
  const { path, gap } = panel;
  let stops = state.file?.path === path && state.gapsAt.get(gap);
  if (!stops) {
    try {
      const file = await api("file", { path });
      const last = file.lines?.length ?? 0;
      stops = (file.gaps || []).filter((g) => Math.min(g.after, last) === gap).reverse();
    } catch (err) {
      if (state.panel === panel) panelError(err.message);
      return;
    }
  }
  if (state.panel !== panel) return;
  const pick = Math.max(0, stops.findIndex((g) => g.session));
  state.trace = { gap, n: gap, path, text: "", stops, pick };
  renderTraceView();
}

const who = (r) => {
  if (r.kind && (r.kind !== "agent" || !r.session)) return (KIND[r.kind] || KIND.unknown).who;
  const s = state.sessions.get(r.session);
  const meta = [SOURCE_NAMES[r.agent || s?.agent] || r.agent || s?.agent, r.model || s?.model].filter(Boolean).join(", ");
  const name = sessionLabel(r.session);
  return (name.length > 80 ? name.slice(0, 80) + "…" : name) + (meta ? ` (${meta})` : "");
};

// The panel's answer as a few lines of Markdown, to paste into a review or a commit.
function originText() {
  const t = state.trace;
  const r = t.stops[t.pick];
  const gap = t.gap != null;
  const where = gap
    ? `${rel(t.path)}, deleted ${gapWhere(t.n)}`
    : t.span
      ? `${rel(t.path)}:${t.span.line}-${t.span.end} (${[t.span.kind, t.span.name].filter(Boolean).join(" ")})`
      : `${rel(t.path)}:${t.n}`;
  const out = [`**${where}**`];
  if (!gap && !t.span && t.text.trim()) out.push("`" + t.text.trim() + "`");
  if (!r) return out.join("\n");
  out.push(`- ${gap ? "Deleted by" : t.span && !r.lines ? "Written earlier by" : "Written by"}: ${who(r)}${r.ts ? `, ${when(r.ts)}` : ""}`);
  const prompt = oneLine(r.prompt);
  if (prompt) out.push(`- Instruction: ${prompt.slice(0, 400)}${prompt.length > 400 ? "…" : ""}`);
  const resume = r.session && resumeCommand(r);
  if (resume) out.push("- Resume: `" + resume + "`");
  const others = t.stops.filter((_, i) => i !== t.pick);
  if (others.length) out.push(`- Other changes here: ${others.map((x) => who(x) + (x.ts ? `, ${shortWhen(x.ts)}` : "")).join("; ")}`);
  return out.join("\n");
}

function renderTraceView() {
  const t = state.trace;
  const { stop, writer } = state.panel;
  if (stop) {
    const same = (r) => r && r.kind === stop.kind && r.session === stop.session && r.edit === stop.edit;
    const pick = same(t.stops[stop.index]) ? stop.index : t.stops.findIndex(same);
    if (pick >= 0) t.pick = pick;
  }
  const gap = t.gap != null;
  const sessions = new Set(t.stops.map((r) => r.session).filter(Boolean));
  const count = gap ? `${t.stops.length} deletions here, newest first` : t.span ? `Lines ${t.span.line}–${t.span.end} · ${plural(t.stops.length, "change")}, newest first` : `${t.stops.length} changes, newest first`;
  const head = h(
    "div",
    { class: "panel-head line-head" },
    // The editor already shows the line itself; the head only says whose it is.
    h(
      "div",
      { class: "head-row" },
      h("h2", {}, gap ? `Deleted ${gapWhere(t.n)}` : t.span ? [t.span.kind ? h("span", { class: "okind" }, t.span.kind + " ") : null, h("code", {}, t.span.name)] : `Line ${t.n}`, sessions.size > 1 ? dots([...sessions]) : null),
      t.stops.length > 1 || t.span ? h("span", { class: "head-count" }, count) : null,
      h("button", { type: "button", class: "icon-btn bare", title: "Copy where this came from, as Markdown", "aria-label": "Copy where this came from", onclick: () => copy(originText(), "Copied") }, icon("copy")),
      h("a", { class: "icon-btn bare", href: `vscode://file${encodeURI(t.path)}:${Math.max(1, t.n)}`, title: "Open in VS Code", "aria-label": "Open in VS Code" }, icon("external")),
    ),
    t.stops.length ? h("div", { class: "chips", role: "tablist", "aria-label": gap ? "Deletions at this spot, newest first" : "Changes to this line, newest first" }, t.stops.map((r, i) => historyChip(r, i))) : null,
    gap ? h("p", { class: "gap-origin", id: "gap-origin" }) : null,
  );
  const body = h("div", { class: "chat-body", id: "chat-body" });
  setPanel([head, body], true);
  if (!t.stops.length) return body.replaceChildren(h("div", { class: "empty" }, "No recorded history"));
  showStop(t.pick, writer);
}

function shortWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const year = d.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return d.toLocaleDateString(undefined, { year, month: "short", day: "numeric" });
}

function historyChip(r, i) {
  const kind = r.kind === "agent" && r.session ? null : KIND[r.kind] || KIND.unknown;
  const label = kind ? kind.who : sessionLabel(r.session);
  return h(
    "button",
    {
      type: "button",
      role: "tab",
      class: kind ? "hchip quiet" : "hchip",
      "aria-selected": String(i === state.trace.pick),
      title: kind ? kind.tip : [label, when(r.ts), state.trace.span ? (r.lines ? `${plural(r.lines, "line")} here now` : "Rewritten since") : null, oneLine(r.prompt).slice(0, 200)].filter(Boolean).join("\n"),
      onclick: () => showStop(i),
    },
    kind ? h("i", { class: `key ${kind.cls}` }) : h("i", { class: "dot", style: `background:${color(r.session)}` }),
    h("span", { class: "hchip-name" }, label),
    h("span", { class: "hchip-when" }, shortWhen(r.ts)),
    state.trace.span && !kind && r.lines ? h("span", { class: "count" }, r.lines) : null,
  );
}

function showStop(i, writer = null) {
  const t = state.trace;
  if (!t) return;
  t.pick = i;
  document.querySelectorAll(".hchip").forEach((el, k) => el.setAttribute("aria-selected", String(k === i)));
  const r = t.stops[i];
  rememberPanel({ stop: { index: i, kind: r.kind, session: r.session, edit: r.edit }, writer: null });
  const body = $("chat-body");
  const gap = t.gap != null;
  if (gap && gapOrigin(r, writer)) return;
  if (r.kind !== "agent" || !r.session) {
    const kind = KIND[r.kind] || KIND.unknown;
    return body.replaceChildren(
      h(
        "div",
        { class: "kind-note" },
        h("p", { class: "kind-who" }, h("i", { class: `key ${kind.cls}` }), kind.who),
        h("p", {}, (gap && GAP_TIP[r.kind]) || kind.tip),
        r.ts ? h("p", { class: "when" }, r.kind === "external" ? `≈ ${when(r.ts)}, between two recorded edits` : when(r.ts)) : null,
      ),
    );
  }
  if (gap) return showConversation(body, r.session, { edit: r.edit, stop: r, tag: `deleted ${plural(r.removed, "line")}`, del: textSet(r.text) });
  if (t.span) return showConversation(body, r.session, { edit: r.edit, stop: r, tag: r.lines ? `wrote ${plural(r.lines, "line")} here` : "rewritten since" });
  return showConversation(body, r.session, { edit: r.edit, stop: r, line: t.n, text: t.text });
}

const textSet = (text) => new Set((text || "").split("\n").map((s) => s.trim()).filter(Boolean));

// Who wrote the lines a deletion took out. Each writer opens at the step that wrote them.
function gapOrigin(r, restoreWriter) {
  const box = $("gap-origin");
  if (!box) return;
  const writers = new Map();
  for (const w of r.wrote || []) {
    const key = w.session || w.kind;
    const prev = writers.get(key);
    writers.set(key, { ...w, lines: (prev?.lines || 0) + (w.lines || 0) });
  }
  if (!writers.size) return box.replaceChildren();
  const pick = (w) => {
    if (!w.session) return;
    rememberPanel({ writer: { session: w.session, edit: w.edit } });
    for (const el of document.querySelectorAll(".hchip, .writer")) el.setAttribute("aria-selected", "false");
    box.querySelector(`[data-session="${CSS.escape(w.session)}"]`)?.setAttribute("aria-selected", "true");
    showConversation($("chat-body"), w.session, { edit: w.edit, stop: { ...w, depth: 1 }, tag: "wrote the deleted lines", add: textSet(r.text) });
    return true;
  };
  box.replaceChildren(
    h("span", { class: "label" }, "Written by"),
    ...[...writers.values()].map((w) => {
      const kind = w.session ? null : KIND[w.kind] || KIND.unknown;
      return h(
        w.session ? "button" : "span",
        {
          type: w.session ? "button" : null,
          class: "writer",
          "data-session": w.session || null,
          title: kind ? kind.tip : `Show where ${sessionLabel(w.session)} wrote these lines`,
          onclick: w.session ? () => pick(w) : null,
        },
        kind ? h("i", { class: `key ${kind.cls}` }) : h("i", { class: "dot", style: `background:${color(w.session)}` }),
        h("span", { class: "writer-name" }, kind ? kind.who : sessionLabel(w.session)),
        r.wrote.length > 1 ? h("span", { class: "count" }, w.lines) : null,
      );
    }),
  );
  const writer = restoreWriter && writers.get(restoreWriter.session);
  return writer && writer.edit === restoreWriter.edit && pick(writer);
}

// Where "Back to line" returns: the file, line and focus the reader showed before
// the first session was followed. Moving between sessions keeps it.
async function openSession(id, target = {}) {
  if (state.file && state.line) state.backTo = { path: state.file.path, line: state.line, focus: state.focus };
  const panel = { view: "conversation", id, target, backTo: state.backTo || null };
  const shown = showPanel(panel);
  setFocus(id);
  await shown;
}

function renderSession({ id, target, backTo }) {
  state.backTo = backTo;
  const body = h("div", { class: "chat-body", id: "chat-body" });
  setPanel(body, true);
  return showConversation(body, id, target);
}

const backLabel = () => (state.backTo.path === state.file?.path ? `Line ${state.backTo.line}` : `${base(state.backTo.path)}:${state.backTo.line}`);

async function backToLine() {
  const { path, line, focus } = state.backTo;
  state.focus = focus;
  if (state.file?.path !== path) return openFile(path, line);
  renderFile();
  renderSessions();
  selectLine(line, { scroll: true });
}

/* ---------- Conversation ---------- */

const conversations = new Map();

async function loadConversation(id) {
  if (conversations.has(id)) {
    const hit = conversations.get(id);
    conversations.delete(id);
    conversations.set(id, hit);
    return hit;
  }
  const conv = await api("conversation", { id });
  conversations.set(id, conv);
  while (conversations.size > 6) conversations.delete(conversations.keys().next().value);
  return conv;
}

async function showConversation(box, id, target) {
  const token = (showConversation.token = (showConversation.token || 0) + 1);
  if (!conversations.has(id)) box.replaceChildren(h("div", { class: "empty" }, "Loading the conversation…"));
  let conv;
  try {
    conv = await loadConversation(id);
  } catch (err) {
    if (token === showConversation.token) box.replaceChildren(h("div", { class: "empty" }, err.message));
    return;
  }
  if (token !== showConversation.token || !box.isConnected) return;
  record("Conversation", conv.cypher, { id });
  const step = target.edit ? conv.items.find((i) => i.kind === "tool" && i.edits.some((e) => e.id === target.edit)) : null;
  // `show` opens whatever holds an item and returns its element, for the find bar.
  const view = { conv, target, step, color: color(id), show: new Map(), rows: new Map() };
  Object.assign(chatFind, { query: "", hits: [], at: -1, view, el: null });
  const bar = sessionBar(conv, box);
  box.replaceChildren(bar, conv.items.length ? chatView(view) : missingLog(conv, target));
  paintChatFind();
  // A group drawn on opening may hold matches the last pass could not see.
  if (!box.findBound) {
    box.findBound = true;
    box.addEventListener("toggle", () => chatFind.query && requestAnimationFrame(() => paintChatFind()), true);
  }
  box.scrollTop = 0;
  const spot = box.querySelector(".act.target");
  if (spot) {
    spot.scrollIntoView({ block: "center" });
    spot.classList.add("flash");
  }
  pinPrompt(box, bar);
  const turn = target.prompt && [...box.querySelectorAll(".turn")].find((li) => li.userItem && Date.parse(li.userItem.ts) === Date.parse(target.prompt));
  if (turn) {
    turn.scrollIntoView({ block: "start" });
    turn.classList.add("flash");
  }
}

// A message as one plain line: no Markdown marks, which read as noise when squeezed.
const plainText = (text) =>
  oneLine(
    (text || "")
      .replace(/^```.*$/gm, "")
      .replace(/^\s{0,3}(?:#{1,6}|>|[-*+]|\d+[.)])\s+/gm, "")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/(\*\*|__|`)/g, ""),
  );

// The bar under the session pins the prompt of the turn being read, but only
// once that turn's own message has scrolled away, so it never shows twice.
function pinPrompt(box, bar) {
  const pin = bar.querySelector(".prompt-pin");
  if (!pin) return;
  let turn = null;
  const update = () => {
    const edge = bar.getBoundingClientRect().bottom;
    let current = null;
    for (const t of box.querySelectorAll(".chat > .turn")) {
      if (t.getBoundingClientRect().top >= edge) break;
      current = t;
    }
    const bubble = current?.querySelector(":scope > .msg.user");
    if (!bubble || bubble.getBoundingClientRect().bottom > edge + 4) {
      pin.hidden = true;
      turn = null;
      return;
    }
    if (current !== turn) {
      turn = current;
      const user = current.userItem;
      pin.querySelector(".who").textContent = user.origin === "task" ? "Task" : "You";
      pin.querySelector(".pin-text").textContent = plainText(user.text);
      pin.title = "Go to this message";
    }
    pin.hidden = false;
  };
  pin.onclick = () => {
    if (!turn) return;
    if (turn.classList.contains("folded")) turn.querySelector(".fold").click();
    turn.querySelector(":scope > .msg.user").scrollIntoView({ block: "start" });
  };
  // Scroll events already arrive at most once a frame.
  box.onscroll = update;
  update();
}

function sessionBar(conv, box) {
  const s = conv.session;
  const resume = resumeCommand(s);
  const meta = [SOURCE_NAMES[s.agent] || s.agent, s.model, when(s.started_at)].filter(Boolean).join(" · ");
  const tool = (name, label, onclick, pressed) =>
    h("button", { type: "button", class: "icon-btn", title: label, "aria-label": label, "aria-pressed": pressed == null ? null : String(pressed), onclick }, icon(name));
  const highlight = tool("target", "Focus on this session: highlight only its lines in the file", () => {
    setFocus(state.focus === s.id ? null : s.id);
    highlight.setAttribute("aria-pressed", String(state.focus === s.id));
  }, state.focus === s.id);
  const expand = tool("unfold", "Open every group of steps", () => {
    const groups = [...box.querySelectorAll("details.work")];
    const open = !groups.every((d) => d.open);
    for (const d of groups) d.open = open;
    const label = open ? "Close every group of steps" : "Open every group of steps";
    expand.replaceChildren(icon(open ? "fold" : "unfold"));
    expand.title = label;
    expand.setAttribute("aria-label", label);
  });
  return h(
    "div",
    { class: "chat-bar" },
    h(
      "div",
      { class: "bar-row" },
      h("i", { class: "dot", style: `background:${color(s.id)}` }),
      h(
        "div",
        { class: "bar-text" },
        h("span", { class: "name", title: sessionLabel(s.id) }, sessionLabel(s.id)),
        h("span", { class: "chat-meta" }, meta),
        s.parent ? h("span", { class: "chat-meta" }, "Helper of ", h("button", { type: "button", class: "link", onclick: () => openSession(s.parent) }, sessionLabel(s.parent))) : null,
      ),
      !state.trace && state.backTo
        ? h("button", { type: "button", class: "back", title: "Back to this line", onclick: backToLine }, icon("back"), h("span", { id: "back-label" }, backLabel()))
        : null,
      h(
        "div",
        { class: "bar-tools" },
        highlight,
        conv.items.length ? expand : null,
        conv.items.length ? tool("search", `Find in conversation (${MOD}F)`, openChatFind) : null,
        resume ? tool("terminal", `Copy the command that resumes this session: ${resume}`, () => copy(resume, "Copied")) : null,
      ),
    ),
    sessionFate(conv),
    conv.items.length ? chatFindBar() : null,
    conv.items.length ? h("button", { type: "button", class: "prompt-pin", hidden: true }, h("span", { class: "who" }), h("span", { class: "pin-text" })) : null,
  );
}

// What became of the session's lines, including those with no recorded outcome.
function sessionFate(conv) {
  const s = state.sessions.get(conv.session.id);
  if (!s?.written) return null;
  const mark = (r) => (r.session ? h("i", { class: "dot", style: `background:${color(r.session)}` }) : h("i", { class: `key ${(KIND[r.kind] || KIND.unknown).cls}` }));
  const replaced = (conv.replaced || []).map((r) => {
    const name = r.session === s.id ? "This session, later on" : r.session ? sessionLabel(r.session) : (KIND[r.kind] || KIND.unknown).who;
    const other = r.session && r.session !== s.id;
    return h(
      other ? "button" : "div",
      { class: "fate-row", type: other ? "button" : null, title: other ? "Open this session" : r.session ? null : (KIND[r.kind] || KIND.unknown).tip, onclick: other ? () => openSession(r.session) : null },
      mark(r),
      h("span", { class: "fate-name" }, name),
      h("span", { class: "count" }, r.lines.toLocaleString()),
    );
  });
  const unaccountedLabels = {
    missing: "Original file was missing at build",
    unreadable: "Original file could not be read at build",
    unknown: "No linked change",
  };
  const unaccounted = (conv.unaccounted || []).map((r) =>
    h(
      "div",
      { class: "fate-row" },
      h("i", { class: `key ${KIND.unknown.cls}` }),
      h("span", { class: "fate-name" }, unaccountedLabels[r.kind] || unaccountedLabels.unknown),
      h("span", { class: "count" }, r.lines.toLocaleString()),
    ),
  );
  const files = (conv.alive_in || []).slice(0, 6).map((f) =>
    h(
      "button",
      { class: "fate-row", type: "button", title: rel(f.path), onclick: () => openFile(f.path, f.line, { keepPanel: true }).then(() => setFocus(s.id)) },
      icon("file"),
      h("span", { class: "fate-name" }, base(f.path)),
      h("span", { class: "count" }, f.lines.toLocaleString()),
    ),
  );
  const more = (conv.alive_in || []).length - files.length;
  return h(
    "details",
    { class: "fate" },
    h(
      "summary",
      { title: "What became of the lines this session wrote" },
      h("span", { class: "chev", "aria-hidden": "true" }),
      h("span", { class: "share" }, h("i", { style: `width:${remainPct(s)}%;background:${s.color}` })),
      h("span", {}, `${s.alive.toLocaleString()} of ${s.written.toLocaleString()} lines remain`),
      h("span", { class: "fate-pct" }, `${remainPct(s)}%`),
    ),
    h(
      "div",
      { class: "fate-body" },
      ...(replaced.length ? [h("p", { class: "label" }, "Replaced or deleted by"), ...replaced] : []),
      ...(unaccounted.length ? [h("p", { class: "label" }, "No recorded outcome"), ...unaccounted] : []),
      ...(files.length ? [h("p", { class: "label" }, "Still standing in"), ...files, more > 0 ? h("p", { class: "fate-more" }, `and ${plural(more, "more file")}`) : null] : []),
    ),
  );
}

// The log is gone: fall back to what the graph kept, the instruction behind the change.
function missingLog(conv, target) {
  const s = target.stop;
  return h(
    "div",
    { class: "chat-missing" },
    h("p", { class: "when" }, conv.note || "Nothing recorded in this session's log."),
    s?.prompt ? [h("p", { class: "label" }, "The instruction behind this change"), prompt(s.prompt, 12)] : null,
  );
}

/* ---------- Find in the conversation ---------- */

// Steps are only drawn once opened, so the search reads the conversation itself
// and opens whatever holds the match it steps to.
const chatFind = { query: "", hits: [], at: -1, view: null, el: null };
const MAX_CHAT_MARKS = 3000;
// Only what a step shows when opened: its diff stands in for its input.
const itemText = (item) => [item.text, item.summary, item.diff, !item.diff || item.diff_from_graph ? item.input : "", item.output].filter(Boolean).join("\n");

function chatFindBar() {
  const input = h("input", { id: "chat-find-input", type: "text", autocomplete: "off", spellcheck: "false", placeholder: "Find in conversation", "aria-label": "Find in conversation" });
  input.addEventListener("input", () => setChatFind(input.value));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") stepChatFind(e.shiftKey ? -1 : 1);
    else if (e.key === "Escape") closeChatFind();
    else return;
    e.preventDefault();
  });
  const button = (name, label, run) => h("button", { type: "button", class: "icon-btn bare", title: label, "aria-label": label, onclick: run }, icon(name));
  return h(
    "div",
    { class: "find chat-find", id: "chat-find", role: "search", hidden: true },
    input,
    h("span", { class: "find-count", id: "chat-find-count", "aria-live": "polite", title: "Messages and steps that hold these words" }),
    button("up", "Previous match (Shift+Enter)", () => stepChatFind(-1)),
    button("down", "Next match (Enter)", () => stepChatFind(1)),
    button("close", "Close (Esc)", closeChatFind),
  );
}

function setChatFind(query) {
  const view = chatFind.view;
  const needle = query.toLowerCase();
  Object.assign(chatFind, { query, at: -1, el: null, hits: needle && view ? view.conv.items.filter((item) => view.show.has(item) && itemText(item).toLowerCase().includes(needle)) : [] });
  paintChatFind();
}

// Marks every match now on screen; returns the first one inside `within`.
function paintChatFind(within = chatFind.el) {
  const box = $("chat-body");
  const n = chatFind.hits.length;
  const count = $("chat-find-count");
  if (count) count.textContent = !chatFind.query ? "" : !n ? "No results" : chatFind.at >= 0 ? `${chatFind.at + 1} of ${n}` : String(n);
  if (!window.Highlight || !CSS.highlights) return null;
  CSS.highlights.delete("chat-find");
  CSS.highlights.delete("chat-find-current");
  const list = box?.querySelector(".chat");
  const needle = chatFind.query.toLowerCase();
  if (!list || !needle) return null;
  const marks = new Highlight();
  let first = null;
  const walk = document.createTreeWalker(list, NodeFilter.SHOW_TEXT);
  for (let node = walk.nextNode(); node && marks.size < MAX_CHAT_MARKS; node = walk.nextNode()) {
    const text = node.data.toLowerCase();
    for (let at = text.indexOf(needle); at >= 0; at = text.indexOf(needle, at + needle.length)) {
      const range = new StaticRange({ startContainer: node, startOffset: at, endContainer: node, endOffset: at + needle.length });
      marks.add(range);
      if (!first && within?.contains(node)) first = range;
    }
  }
  marks.priority = 1;
  CSS.highlights.set("chat-find", marks);
  if (first) {
    const current = new Highlight(first);
    current.priority = 2;
    CSS.highlights.set("chat-find-current", current);
  }
  return first;
}

function stepChatFind(step) {
  const n = chatFind.hits.length;
  const view = chatFind.view;
  if (!n || !view) return;
  chatFind.at = chatFind.at < 0 ? (step > 0 ? 0 : n - 1) : (chatFind.at + step + n) % n;
  const el = view.show.get(chatFind.hits[chatFind.at])();
  const turn = el.closest(".turn.folded");
  if (turn) turn.querySelector(".fold").click();
  for (const more of el.querySelectorAll(".md.clamp + .more")) more.click();
  chatFind.el = el;
  const hit = paintChatFind(el);
  if (!hit) return el.scrollIntoView({ block: "center" });
  for (let up = hit.startContainer.parentElement; up && up !== el; up = up.parentElement) if (up.tagName === "DETAILS") up.open = true;
  // Long output scrolls inside its own box, so every scrolling box on the way up moves.
  const live = new Range();
  live.setStart(hit.startContainer, hit.startOffset);
  live.setEnd(hit.endContainer, hit.endOffset);
  const box = $("chat-body");
  for (let up = hit.startContainer.parentElement; up; up = up.parentElement) {
    if (up.scrollHeight > up.clientHeight + 1 && getComputedStyle(up).overflowY !== "visible") {
      up.scrollTop += live.getBoundingClientRect().top - up.getBoundingClientRect().top - up.clientHeight / 2;
    }
    if (up === box) break;
  }
}

function openChatFind() {
  const bar = $("chat-find");
  if (!bar) return false;
  closeMenu();
  const input = $("chat-find-input");
  const picked = String(getSelection()).trim();
  if (picked && picked.length <= 80 && !picked.includes("\n") && $("panel").contains(getSelection().anchorNode)) input.value = picked;
  bar.hidden = false;
  input.focus();
  input.select();
  if (input.value !== chatFind.query) setChatFind(input.value);
  return true;
}

function closeChatFind() {
  if ($("chat-find")) $("chat-find").hidden = true;
  setChatFind("");
}

function splitTurns(items) {
  const turns = [];
  for (const item of items) {
    if (item.kind === "user" || !turns.length) turns.push({ user: item.kind === "user" ? item : null, items: [] });
    if (item.kind !== "user") turns[turns.length - 1].items.push(item);
  }
  return turns;
}

function chatView(view) {
  const list = h("ol", { class: "chat", "aria-label": "Conversation" });
  for (const turn of splitTurns(view.conv.items)) list.append(renderTurn(turn, view));
  if (view.conv.note) list.append(h("li", { class: "chat-note" }, view.conv.note));
  if (view.target.edit && !view.step) list.prepend(h("li", { class: "chat-note" }, "This change's step isn't in the log, so the whole conversation is shown."));
  return list;
}

function renderTurn(turn, view) {
  const li = h("li", { class: "turn" });
  const blocks = [];
  for (const item of turn.items) {
    if (item.kind === "tool" || item.kind === "thinking") {
      const last = blocks[blocks.length - 1];
      if (last?.work) last.work.push(item);
      else blocks.push({ work: [item] });
    } else blocks.push({ item });
  }
  const body = h(
    "div",
    { class: "turn-body" },
    blocks.map((b) => {
      if (b.work) return workGroup(b.work, view);
      const el = message(b.item);
      view.show.set(b.item, () => el);
      return el;
    }),
  );
  if (turn.user) {
    const steps = turn.items.filter((i) => i.kind === "tool").length;
    const replies = turn.items.filter((i) => i.kind === "assistant").length;
    const folded = h("p", { class: "folded-note" }, [steps && plural(steps, "step"), replies && plural(replies, "reply", "replies")].filter(Boolean).join(" · ") || "No reply");
    const toggle = h("button", { type: "button", class: "fold", "aria-expanded": "true", title: "Fold this turn" }, h("span", { class: "chev", "aria-hidden": "true" }));
    toggle.addEventListener("click", () => {
      const fold = !li.classList.contains("folded");
      li.classList.toggle("folded", fold);
      toggle.setAttribute("aria-expanded", String(!fold));
      toggle.title = fold ? "Unfold this turn" : "Fold this turn";
    });
    const bubble = userBubble(turn.user, toggle);
    li.append(bubble, folded);
    li.userItem = turn.user;
    view.show.set(turn.user, () => bubble);
  }
  li.append(body);
  return li;
}

function userBubble(item, toggle) {
  const task = item.origin === "task";
  return h(
    "div",
    { class: task ? "msg user task" : "msg user" },
    h("div", { class: "msg-head" }, h("span", { class: "who" }, task ? "Task from the parent session" : "You"), h("span", { class: "when" }, when(item.ts)), toggle),
    clampText(md(item.text), item.text, 10),
    item.more ? h("p", { class: "cut" }, "Message cut short") : null,
  );
}

function message(item) {
  if (item.kind === "note") return h("div", { class: "chat-note" }, item.text);
  return h("div", { class: "msg ai" }, clampText(md(item.text), item.text, 24), item.more ? h("p", { class: "cut" }, "Message cut short") : null);
}

function clampText(nodes, text, lines) {
  const box = h("div", { class: "md" }, nodes);
  const long = text.length > lines * 70 || text.split("\n").length > lines;
  if (!long) return box;
  box.classList.add("clamp");
  box.style.setProperty("--clamp", `${lines * 1.5}em`);
  const more = h("button", { type: "button", class: "more" }, "Show all");
  more.addEventListener("click", () => {
    const open = box.classList.toggle("clamp");
    more.textContent = open ? "Show all" : "Show less";
  });
  return [box, more];
}

/* ---------- Steps ---------- */

const TOOL_VERBS = {
  Read: "Read",
  Write: "Wrote",
  Edit: "Edited",
  MultiEdit: "Edited",
  NotebookEdit: "Edited",
  Bash: "Ran",
  exec_command: "Ran",
  exec: "Ran",
  shell: "Ran",
  write_stdin: "Typed into",
  Grep: "Searched",
  Glob: "Listed",
  WebFetch: "Fetched",
  WebSearch: "Searched the web",
  Agent: "Delegated",
  Task: "Delegated",
  spawn_agent: "Delegated",
  apply_patch: "Patched",
  TodoWrite: "Updated plan",
  update_plan: "Updated plan",
  Skill: "Used skill",
  ToolSearch: "Loaded tools",
};

function toolName(name) {
  const parts = name.split("__");
  return parts.length > 2 ? parts.slice(2).join("__") : name;
}

const verb = (item) => TOOL_VERBS[item.name] || (/update_plan$/.test(item.name) ? "Updated plan" : toolName(item.name));

function stepTarget(item) {
  if (["Read", "Write", "Edit", "MultiEdit", "NotebookEdit"].includes(item.name)) return rel(item.summary);
  return item.summary;
}

const KIND_OF = { Read: "read", Bash: "run", exec_command: "run", exec: "run", shell: "run", Grep: "search", Glob: "search", WebSearch: "search", WebFetch: "search" };

function workSummary(items) {
  const counts = { read: 0, run: 0, search: 0, other: 0, think: 0 };
  const edited = [];
  for (const i of items) {
    if (i.kind === "thinking") counts.think++;
    else if (i.edits.length) for (const e of i.edits) !edited.includes(e.path) && edited.push(e.path);
    else counts[KIND_OF[i.name] || "other"]++;
  }
  const parts = [];
  if (edited.length) parts.push(`edited ${edited.slice(0, 2).map(base).join(", ")}${edited.length > 2 ? ` +${edited.length - 2}` : ""}`);
  if (counts.read) parts.push(`read ${plural(counts.read, "file")}`);
  if (counts.run) parts.push(`ran ${plural(counts.run, "command")}`);
  if (counts.search) parts.push(`${counts.search} ${counts.search === 1 ? "search" : "searches"}`);
  if (counts.other) parts.push(plural(counts.other, "other step"));
  if (counts.think && !parts.length) parts.push("thought");
  const text = parts.join(", ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function workGroup(items, view) {
  const steps = items.filter((i) => i.kind === "tool").length;
  const hasTarget = view.step && items.includes(view.step);
  const wrote = items.some((i) => i.edits?.length);
  const det = h("details", { class: ["work", hasTarget && "has-target", wrote && "wrote"].filter(Boolean).join(" "), style: `--line:${view.color}` });
  det.append(
    h(
      "summary",
      {},
      h("span", { class: "chev", "aria-hidden": "true" }),
      h("span", { class: "work-count" }, steps ? plural(steps, "step") : "Thinking"),
      h("span", { class: "work-what" }, workSummary(items)),
    ),
  );
  let filled = false;
  const fill = () => {
    if (filled) return;
    filled = true;
    det.append(h("ol", { class: "acts" }, items.map((i) => actRow(i, view))));
  };
  det.addEventListener("toggle", () => det.open && fill());
  for (const i of items) {
    view.show.set(i, () => {
      fill();
      det.open = true;
      return view.rows.get(i)();
    });
  }
  if (hasTarget) {
    fill();
    det.open = true;
  }
  return det;
}

function actRow(item, view) {
  if (item.kind === "thinking") {
    const d = h("details", {}, h("summary", {}, h("span", { class: "verb" }, "Thought"), h("span", { class: "what" }, oneLine(item.text).slice(0, 120))));
    let filled = false;
    const fill = () => {
      if (filled) return;
      filled = true;
      d.append(h("div", { class: "act-body think" }, h("div", { class: "md" }, md(item.text))));
    };
    d.addEventListener("toggle", () => d.open && fill());
    const li = h("li", { class: "act think" }, d);
    view.rows.set(item, () => {
      fill();
      d.open = true;
      return li;
    });
    return li;
  }
  const target = item === view.step;
  const cls = ["act"];
  if (target) cls.push("target");
  if (item.edits.length) cls.push("wrote");
  if (item.error) cls.push("failed");
  const d = h(
    "details",
    {},
    h(
      "summary",
      {},
      h("span", { class: "verb" }, verb(item)),
      h("span", { class: "what", title: item.summary }, stepTarget(item) || ""),
      item.error ? h("span", { class: "tag bad" }, "failed") : null,
      target && view.target.tag
        ? h("span", { class: view.target.del ? "tag here cut" : "tag here" }, view.target.tag)
        : target
        ? h("span", { class: "tag here", title: view.target.stop?.depth ? "Later changes replaced what this step wrote on the line" : null }, view.target.stop?.depth ? `earlier line ${view.target.line}` : `wrote line ${view.target.line}`)
        : item.edits.length ? h("span", { class: "tag pen", title: "Changed files still traced in the graph" }, icon("pencil")) : null,
    ),
  );
  let filled = false;
  const fill = () => {
    if (filled) return;
    filled = true;
    d.append(stepBody(item, view, target));
  };
  // The target is only marked, not opened: the editor already shows the code it wrote.
  d.addEventListener("toggle", () => d.open && fill());
  const li = h("li", { class: cls.join(" "), style: `--line:${view.color}` }, d);
  view.rows.set(item, () => {
    fill();
    d.open = true;
    return li;
  });
  return li;
}

function inputText(item) {
  try {
    const v = JSON.parse(item.input);
    const cmd = v && typeof v === "object" ? v.command ?? v.cmd : null;
    if (typeof cmd === "string") return "$ " + cmd;
    if (Array.isArray(cmd)) return "$ " + cmd.join(" ");
  } catch {}
  return item.input;
}

function stepBody(item, view, target) {
  const parts = [];
  if (item.diff) {
    parts.push(
      item.diff_from_graph ? h("p", { class: "cut" }, "The log has no diff for this step; these are the lines it left in the file.") : null,
      diffView(item.diff, target ? view.target : {}),
      item.diff_more ? h("p", { class: "cut" }, "Diff cut short") : null,
    );
    if (item.diff_from_graph && item.input) parts.push(h("details", { class: "act-input" }, h("summary", {}, "Command"), h("pre", { class: "io" }, inputText(item))));
  }
  else if (item.input) parts.push(h("pre", { class: "io" }, inputText(item)), item.input_more ? h("p", { class: "cut" }, "Input cut short") : null);
  if (item.output) parts.push(h("pre", { class: item.error ? "io out bad" : "io out" }, item.output), item.output_more ? h("p", { class: "cut" }, "Output cut short") : null);
  if (item.edits.length) {
    const files = [...new Set(item.edits.map((e) => e.path))];
    parts.push(
      h(
        "p",
        { class: "act-files" },
        icon("pencil"),
        files.flatMap((p, i) => [
          i ? ", " : "",
          h("button", { type: "button", class: "link", title: rel(p), onclick: () => openFile(p, null, { keepPanel: true }).then(() => setFocus(view.conv.session.id)) }, base(p)),
        ]),
      ),
    );
  }
  if (item.child) parts.push(h("button", { type: "button", class: "link child", onclick: () => openSession(item.child) }, "Open the helper's conversation"));
  return h("div", { class: "act-body" }, parts);
}

// `target.text` marks the traced line among the added ones; `add` and `del`
// mark the lines a deletion took out, where they were written and removed.
function diffView(text, target) {
  const want = (target.text || "").trim();
  const added = (s) => (want && s === want) || target.add?.has(s);
  return h(
    "pre",
    { class: "io diff" },
    text.split("\n").map((line) => {
      const c = line[0];
      let cls = "";
      if (line.startsWith("***") || line.startsWith("@@")) cls = "hunk";
      else if (c === "+") cls = added(line.slice(1).trim()) ? "add hit" : "add";
      else if (c === "-") cls = target.del?.has(line.slice(1).trim()) ? "del hit" : "del";
      return h("span", { class: cls }, line || " ");
    }),
  );
}

/* ---------- A little Markdown, built as DOM nodes (no HTML from the log) ---------- */

function md(text) {
  const out = [];
  const parts = (text || "").split(/^```[^\n]*\n?/m);
  parts.forEach((part, i) => {
    if (i % 2) return out.push(h("pre", { class: "md-code" }, part.replace(/\n$/, "")));
    for (const block of part.split(/\n{2,}/)) {
      if (block.trim()) out.push(mdBlock(block));
    }
  });
  return out;
}

function mdBlock(block) {
  const lines = block.replace(/^\n+|\n+$/g, "").split("\n");
  if (lines.every((l) => /^\s*([-*+]|\d+[.)])\s+/.test(l) || /^\s{2,}\S/.test(l))) {
    const ordered = /^\s*\d/.test(lines[0]);
    const items = [];
    for (const l of lines) {
      const m = l.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
      if (m) items.push([m[1]]);
      else items[items.length - 1]?.push(l.trim());
    }
    return h(ordered ? "ol" : "ul", {}, items.map((parts) => h("li", {}, inline(parts.join(" ")))));
  }
  const heading = lines[0].match(/^#{1,6}\s+(.*)$/);
  if (heading && lines.length === 1) return h("p", { class: "md-h" }, inline(heading[1]));
  if (lines.every((l) => /^\s*>/.test(l))) return h("blockquote", {}, inline(lines.map((l) => l.replace(/^\s*>\s?/, "")).join("\n")));
  if (lines.length > 1 && lines.every((l) => /^\s*\|/.test(l))) return h("pre", { class: "md-table" }, block);
  return h("p", {}, lines.flatMap((l, i) => [i ? h("br") : null, ...(heading && i === 0 ? [h("strong", {}, inline(heading[1]))] : inline(l))]));
}

function inline(text) {
  const out = [];
  const re = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1] != null) out.push(h("code", {}, m[1]));
    else if (m[2] != null) out.push(h("strong", {}, m[2]));
    else out.push(h("span", { class: "md-link", title: m[4] }, m[3]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// Kept for logs that are gone: the instruction text the graph stored.
const NO_PROMPT = "No instruction recorded";

function prompt(text, lines = 6) {
  if (!text) return h("p", { class: "when" }, NO_PROMPT);
  const long = text.length > lines * 60 || text.split("\n").length > lines;
  const quote = h("blockquote", { class: long ? "clamp" : "", style: `--clamp:${lines}` }, text);
  if (!long) return quote;
  const more = h("button", { type: "button", class: "more" }, "Show all");
  more.addEventListener("click", () => {
    quote.classList.toggle("clamp");
    more.textContent = quote.classList.contains("clamp") ? "Show all" : "Show less";
  });
  return [quote, more];
}

/* ---------- Wiring ---------- */

/* ---------- Tooltips ---------- */
// The browser's own title tooltip waits about a second, looks like a system hint and never
// shows on keyboard focus. This one appears at once, and for icon-only buttons falls back to
// the aria-label, so every control says what it does. The title is lifted off the element
// while the tip is up (so the native one stays quiet) and put back afterwards.
function bindTooltips() {
  const tip = h("div", { class: "tip", role: "tooltip", hidden: true });
  document.body.append(tip);
  const SHOW_DELAY = 180;
  const WARM_MS = 600;
  let owner = null;
  let held = null;
  let timer = 0;
  let lastShown = 0;

  const label = (el) => (el.dataset.tip || el.getAttribute("title") || el.getAttribute("aria-label") || "").trim();
  const find = (node) => {
    const el = node instanceof Element ? node.closest("[title], [data-tip], button[aria-label], a[aria-label], [role=tab][aria-label]") : null;
    return el && label(el) ? el : null;
  };

  const hide = () => {
    clearTimeout(timer);
    timer = 0;
    if (!tip.hidden) lastShown = Date.now();
    tip.hidden = true;
    if (owner && held != null && !owner.hasAttribute("title") && owner.isConnected) owner.setAttribute("title", held);
    owner = null;
    held = null;
  };

  const place = (el, at) => {
    tip.style.left = "0px";
    tip.style.top = "0px";
    const box = el.getBoundingClientRect();
    const w = tip.offsetWidth;
    const hgt = tip.offsetHeight;
    // A wide row (a code line, a file name) is pointed at, not boxed: follow where the pointer came in.
    const x = box.width > 220 && at ? at.x : box.left + box.width / 2;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, x - w / 2));
    const below = box.bottom + 8 + hgt <= window.innerHeight - 4;
    tip.style.left = `${Math.round(left)}px`;
    tip.style.top = `${Math.round(below ? box.bottom + 8 : Math.max(4, box.top - 8 - hgt))}px`;
  };

  const arm = (el, at, instant) => {
    hide();
    const words = el && label(el);
    if (!words) return;
    // The native tip is silenced at once, even while ours waits to appear.
    owner = el;
    held = el.hasAttribute("title") ? el.getAttribute("title") : null;
    if (held != null) el.removeAttribute("title");
    const go = () => {
      timer = 0;
      if (owner !== el || !el.isConnected) return hide();
      tip.textContent = words;
      tip.hidden = false;
      place(el, at);
    };
    if (instant || Date.now() - lastShown < WARM_MS) go();
    else timer = setTimeout(go, SHOW_DELAY);
  };

  document.addEventListener("mouseover", (e) => {
    // Moving between children of the same element keeps its tip (its title is off the element meanwhile).
    if (owner?.contains(e.target)) return;
    arm(find(e.target), { x: e.clientX, y: e.clientY });
  });
  document.addEventListener("mouseout", (e) => {
    if (owner && !owner.contains(e.relatedTarget)) hide();
  });
  document.addEventListener("focusin", (e) => {
    // Only keyboard focus: a click already shows the tip by hovering.
    if (e.target instanceof Element && e.target.matches(":focus-visible")) arm(find(e.target), null, true);
  });
  document.addEventListener("focusout", hide);
  document.addEventListener("pointerdown", hide, true);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !tip.hidden) hide();
  });
  window.addEventListener("scroll", hide, true);
  window.addEventListener("blur", hide);
  window.addEventListener("resize", hide);
}

function bindEvents() {
  bindTooltips();
  $("lines").addEventListener("click", (e) => {
    const link = (e.metaKey || e.ctrlKey) && nameAt(e.clientX, e.clientY);
    if (link) {
      markLink(null);
      // The line clicked from becomes the place Back returns to.
      selectLine(link.line);
      return showUses(link.name, { path: state.file.path, line: link.line });
    }
    const row = e.target.closest(".ln");
    if (row) return selectLine(Number(row.dataset.line));
    const gap = e.target.closest(".gap");
    if (!gap) return;
    const after = Number(gap.dataset.after);
    if (e.target.closest(".gap-mark")) toggleGap(after);
    selectGap(after);
  });
  $("lines").addEventListener("mouseover", (e) => {
    const row = e.target.closest(".ln");
    if (row) return $("hover").replaceChildren(...describeOwner(Number(row.dataset.line)));
    const gap = e.target.closest(".gap");
    if (gap) $("hover").replaceChildren(...describeGap(Number(gap.dataset.after)));
  });
  $("gaps-toggle").addEventListener("click", toggleAllGaps);
  // Holding the modifier turns names into links.
  $("lines").addEventListener("mousemove", (e) => {
    const hit = (e.metaKey || e.ctrlKey) && nameAt(e.clientX, e.clientY);
    $("lines").classList.toggle("linking", !!hit);
    markLink(hit || null);
  });
  const unlink = () => {
    $("lines").classList.remove("linking");
    markLink(null);
  };
  $("lines").addEventListener("mouseleave", unlink);
  document.addEventListener("keyup", (e) => (e.key === "Meta" || e.key === "Control") && unlink());
  window.addEventListener("blur", unlink);
  $("lines").addEventListener("mouseleave", () => $("hover").replaceChildren());
  $("code-scroll").addEventListener("keydown", (e) => {
    if (!state.file?.lines) return;
    // From a deletion, the arrows step to the line below or above it.
    const at = state.line || (state.gap != null ? state.gap + 0.5 : null);
    if (e.key === "ArrowDown" || e.key === "j") selectLine(Math.floor((at || 0) + 1), { scroll: true });
    else if (e.key === "ArrowUp" || e.key === "k") selectLine(Math.ceil((at || 2) - 1), { scroll: true });
    else if (e.key === "Escape") setFocus(null);
    else return;
    e.preventDefault();
  });
  for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => selectTab(b.dataset.tab));

  $("nav-back").append(icon("back"));
  $("nav-forward").append(icon("forward"));
  $("nav-back").title = `Back (${IS_MAC ? "⌃-" : "Alt+←"})`;
  $("nav-forward").title = `Forward (${IS_MAC ? "⌃⇧-" : "Alt+→"})`;
  $("nav-back").setAttribute("aria-label", "Back");
  $("nav-forward").setAttribute("aria-label", "Forward");
  $("nav-back").addEventListener("click", () => history.back());
  $("nav-forward").addEventListener("click", () => history.forward());
  window.addEventListener("popstate", restoreHash);
  renderNav();

  $("outline-tab").append(icon("outline"));
  $("outline-tab").title = "Outline: this file's functions and who wrote them";
  $("search-tab").append(icon("search"));
  $("search-tab").title = `Search (${IS_MAC ? "⇧⌘F" : "Ctrl+Shift+F"})`;
  $("search-tab").addEventListener("click", () => $("search-input").focus());
  $("search-input").addEventListener("input", queueSearch);
  $("search-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch(search.refs);
    else if (e.key === "ArrowDown") $("search-results").querySelector("button")?.focus();
    else return;
    e.preventDefault();
  });
  $("search-results").addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    const rows = [...$("search-results").querySelectorAll("details[open] button")];
    const i = rows.indexOf(document.activeElement) + (e.key === "ArrowDown" ? 1 : -1);
    (rows[i] || (i < 0 ? $("search-input") : null))?.focus();
    e.preventDefault();
  });
  $("search-case").addEventListener("click", () => {
    search.case = !search.case;
    $("search-case").setAttribute("aria-pressed", String(search.case));
    runSearch();
  });
  renderSearch();

  $("find-prev").append(icon("up"));
  $("find-next").append(icon("down"));
  $("find-close").append(icon("close"));
  $("find-input").addEventListener("input", typeFind);
  $("find-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") stepFind(e.shiftKey ? -1 : 1);
    else if (e.key === "Escape") closeFind();
    else return;
    e.preventDefault();
  });
  $("find-prev").addEventListener("click", () => stepFind(-1));
  $("find-next").addEventListener("click", () => stepFind(1));
  $("find-close").addEventListener("click", closeFind);
  $("find-case").addEventListener("click", () => {
    setFind(find.query, !find.case);
    if (find.hits.length) $(`L${find.hits[nearestHit(1)].line}`)?.scrollIntoView({ block: "center" });
  });

  const drawer = $("cypher");
  const toggle = $("cypher-toggle");
  const setDrawer = (open) => {
    drawer.hidden = !open;
    toggle.setAttribute("aria-pressed", String(open));
    if (open) renderCypher();
  };
  toggle.addEventListener("click", () => setDrawer(drawer.hidden));
  $("cypher-close").append(icon("close"));
  $("cypher-close").addEventListener("click", () => setDrawer(false));

  const themes = ["auto", "light", "dark"];
  const themeIcons = { auto: "auto", light: "sun", dark: "moon" };
  const theme = $("theme-toggle");
  const apply = (t) => {
    if (t === "auto") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = t;
    theme.replaceChildren(icon(themeIcons[t] || "auto"));
    const names = { auto: "Auto (follows your system)", light: "Light", dark: "Dark" };
    const next = themes[(themes.indexOf(t) + 1) % themes.length];
    theme.title = `Theme: ${names[t] || names.auto}\nClick to switch to ${names[next].replace(/ \(.*/, "")}`;
    theme.setAttribute("aria-label", `Theme: ${names[t] || names.auto}. Switch to ${names[next].replace(/ \(.*/, "")}`);
  };
  let current = "auto";
  try {
    current = localStorage.getItem("papertrail-theme") || "auto";
  } catch {}
  apply(current);
  theme.addEventListener("click", () => {
    current = themes[(themes.indexOf(current) + 1) % themes.length];
    apply(current);
    try {
      localStorage.setItem("papertrail-theme", current);
    } catch {}
  });
}

/* ---------- Quick open (files, folders, line) ---------- */

const palette = { items: [], active: 0, token: 0, source: null, pick: null, browse: new Map() };

// `label` names the dialog for screen readers; the placeholder is all a sighted reader needs.
function openPalette({ placeholder, label, value = "", source, pick }) {
  closeMenu();
  Object.assign(palette, { source, pick, items: [], active: 0 });
  const input = $("palette-input");
  input.placeholder = placeholder;
  input.value = value;
  input.setAttribute("aria-label", label);
  $("palette").firstElementChild.setAttribute("aria-label", label);
  $("palette-list").replaceChildren(); // no rows left over from the last palette while this one loads
  $("palette").hidden = false;
  input.focus();
  refreshPalette();
}

function closePalette() {
  if ($("palette").hidden) return;
  $("palette").hidden = true;
  palette.token++;
}

async function refreshPalette() {
  const token = ++palette.token;
  let items;
  try {
    items = await palette.source($("palette-input").value);
  } catch (err) {
    items = [{ label: err.message, disabled: true }];
  }
  if (token !== palette.token) return;
  palette.items = items;
  palette.active = Math.max(0, items.findIndex((i) => !i.disabled));
  renderPalette();
}

function renderPalette() {
  const list = $("palette-list");
  list.replaceChildren(
    ...palette.items.map((item, i) =>
      h(
        "li",
        {
          role: "option",
          id: `pal-${i}`,
          class: [item.disabled && "disabled", item.group && "group"].filter(Boolean).join(" ") || null,
          "aria-selected": String(i === palette.active),
          "aria-disabled": item.disabled ? "true" : null,
          onmousedown: (e) => e.preventDefault(),
          onclick: () => !item.disabled && choose(item),
        },
        item.mark ? h("i", { class: "pal-mark", "aria-hidden": "true" }) : h("i", { class: "pal-mark none", "aria-hidden": "true" }),
        h("span", { class: "pal-label" }, item.label),
        item.detail ? h("span", { class: "pal-detail" }, item.detail) : null,
      ),
    ),
  );
  $("palette-input").setAttribute("aria-activedescendant", palette.items.length ? `pal-${palette.active}` : "");
  $(`pal-${palette.active}`)?.scrollIntoView({ block: "nearest" });
}

function movePalette(step) {
  const n = palette.items.length;
  for (let k = 1; k <= n; k++) {
    const i = (palette.active + step * k + n * k) % n;
    if (!palette.items[i].disabled) {
      palette.active = i;
      return renderPalette();
    }
  }
}

async function choose(item) {
  const keep = await palette.pick(item);
  if (keep) {
    $("palette-input").value = keep;
    refreshPalette();
  } else closePalette();
}

// Characters of the query in order, with bonuses for runs and word starts.
function fuzzy(query, text) {
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  let i = 0;
  let score = 0;
  let last = -2;
  for (let j = 0; j < t.length && i < q.length; j++) {
    if (t[j] !== q[i]) continue;
    score += 1 + (j === last + 1 ? 3 : 0) + (j === 0 || "/_-. ".includes(t[j - 1]) ? 2 : 0);
    last = j;
    i++;
  }
  if (i < q.length) return -1;
  return score + (base(t).includes(q) ? 10 : 0) - t.length * 0.01;
}

function quickOpenFile() {
  if (!state.overview) return quickOpenFolder();
  openPalette({
    placeholder: "File name, or name:42 for a line",
    label: "Open a file",
    source: (q) => {
      const [, name, line] = q.trim().match(/^(.*?)(?::(\d+))?$/);
      const files = state.entries.filter((e) => state.agentOnly ? agentWrote(e) : true);
      let ranked;
      if (!name) {
        const recent = [...state.tabs].reverse();
        ranked = [...recent.map((p) => files.find((e) => e.path === p)).filter(Boolean), ...files.filter((e) => agentWrote(e) && !recent.includes(e.path))];
      } else {
        ranked = files
          .map((e) => [fuzzy(name, rel(e.path)), e])
          .filter(([score]) => score >= 0)
          .sort((a, b) => b[0] - a[0])
          .map(([, e]) => e);
      }
      return ranked.slice(0, 60).map((e) => ({
        label: base(e.path),
        detail: rel(e.path).slice(0, -base(e.path).length).replace(/\/$/, "") || null,
        mark: agentWrote(e),
        path: e.path,
        line: line ? Number(line) : null,
      }));
    },
    pick: (item) => openFile(item.path, item.line),
  });
}

function goToLine() {
  const f = state.file;
  if (!f?.lines) return toast("Open a file first.");
  openPalette({
    placeholder: `Line 1–${f.lines.length}`,
    label: `Go to a line in ${base(f.path)}`,
    value: state.line ? String(state.line) : "",
    source: (q) => {
      const n = Number(q.trim());
      if (!q.trim()) return [];
      if (!Number.isInteger(n) || n < 1 || n > f.lines.length) return [{ label: `1–${f.lines.length} only`, disabled: true }];
      return [{ label: `Go to line ${n}`, detail: oneLine(f.lines[n - 1]).slice(0, 80) || null, line: n }];
    },
    pick: (item) => {
      selectLine(item.line, { scroll: true });
      $("code-scroll").focus({ preventScroll: true });
    },
  });
}

async function browseDir(path) {
  if (!palette.browse.has(path)) palette.browse.set(path, api("browse", { path }));
  try {
    return await palette.browse.get(path);
  } catch (err) {
    palette.browse.delete(path);
    throw err;
  }
}

async function knownFolders() {
  if (!state.agentFolders) {
    state.agentFolders = api("folders").then((r) => r.folders, () => []);
  }
  const recent = stored("papertrail-recent", []);
  const agents = await state.agentFolders;
  const rows = recent.map((path) => ({ path, recent: true }));
  for (const f of agents) if (!recent.includes(f.path)) rows.push(f);
  return rows;
}

function folderItem(f) {
  const home = state.workspace?.home;
  const shown = home && f.path.startsWith(home + "/") ? "~" + f.path.slice(home.length) : f.path;
  return {
    label: base(f.path),
    // The dot already says the folder has history.
    detail: f.path === state.workspace?.project ? `${shown}  · open` : shown,
    mark: f.graph,
    open: f.path,
  };
}

function quickOpenFolder() {
  if (state.building) return toast(BUILD_BUSY);
  const home = state.workspace?.home || "";
  openPalette({
    placeholder: "Folder name, or a path like ~/projects/app",
    label: "Open a folder",
    source: async (q) => {
      const typed = q.trim();
      if (!typed) {
        const rows = await knownFolders();
        return [
          { label: "System dialog…", detail: `${MOD}O`, system: true },
          { label: "Home", detail: "~/", into: home + "/" },
          ...(rows.length ? [{ label: "Recent", disabled: true, group: true }, ...rows.map(folderItem)] : []),
        ];
      }
      if (!/^[~/]/.test(typed)) {
        const rows = await knownFolders();
        const hits = rows.map((f) => [fuzzy(typed, f.path), f]).filter(([s]) => s >= 0).sort((a, b) => b[0] - a[0]);
        return hits.length ? hits.slice(0, 40).map(([, f]) => folderItem(f)) : [{ label: "No match. Start with / or ~ for a path.", disabled: true }];
      }
      const full = typed.replace(/^~(?=\/|$)/, home);
      const cut = full.lastIndexOf("/");
      const dir = full.slice(0, cut) || "/";
      const part = full.slice(cut + 1).toLowerCase();
      const listing = await browseDir(dir);
      const dirs = listing.dirs.filter((d) => d.name.toLowerCase().startsWith(part)).concat(listing.dirs.filter((d) => !d.name.toLowerCase().startsWith(part) && part && d.name.toLowerCase().includes(part)));
      // With nothing after the last slash, the first row opens this folder. With a
      // partial name, the first row is the best matching subfolder.
      const exact = part && listing.dirs.find((d) => d.name.toLowerCase() === part);
      const target = part ? exact : { path: listing.path, graph: listing.graph };
      const rows = [
        ...(target ? [{ label: `Open ${base(target.path) || target.path}`, mark: target.graph, open: target.path }] : []),
        ...(listing.parent && !part ? [{ label: "..", into: listing.parent.replace(/\/?$/, "/") }] : []),
        ...dirs.slice(0, 200).map((d) => ({ label: d.name + "/", mark: d.graph, into: d.path + "/" })),
      ];
      return rows.length ? rows : [{ label: "No match", disabled: true }];
    },
    pick: (item) => {
      if (item.into) return item.into;
      if (item.system) return void pickFolder();
      openFolder(item.open);
    },
  });
}

/* ---------- File menu ---------- */

function menuItems() {
  const recent = stored("papertrail-recent", []).filter((p) => p !== state.workspace?.project).slice(0, 5);
  return [
    { label: "Open Folder…", key: `${MOD}O`, run: pickFolder, disabled: state.building },
    { label: "Open Recent…", key: IS_MAC ? "⌃R" : "Ctrl+R", run: quickOpenFolder, disabled: state.building },
    { label: "Open File…", key: `${MOD}P`, run: quickOpenFile, disabled: !state.overview },
    { label: "Go to Line…", key: IS_MAC ? "⌃G" : "Ctrl+G", run: goToLine, disabled: !state.file?.lines },
    { label: "Find in File…", key: `${MOD}F`, run: openFind, disabled: !state.file?.lines },
    { label: "Search…", key: IS_MAC ? "⇧⌘F" : "Ctrl+Shift+F", run: openSearch, disabled: !state.workspace?.project && !state.overview },
    null,
    { label: "Rebuild History", run: () => buildHistory(), disabled: !state.workspace?.project || state.building, title: "Read the agent logs again" },
    ...(recent.length ? [null, { label: "Recent folders", group: true }, ...recent.map((p) => ({ label: base(p), detail: rel(p), title: p, run: () => openFolder(p), disabled: state.building }))] : []),
  ];
}

function openMenu() {
  const list = $("file-menu");
  list.replaceChildren(
    ...menuItems().map((item) =>
      !item
        ? h("hr", { role: "separator" })
        : item.group
          ? h("p", { class: "menu-group" }, item.label)
          : h(
              "button",
              {
                type: "button",
                role: "menuitem",
                disabled: item.disabled,
                title: item.title || null,
                onclick: () => {
                  closeMenu();
                  item.run();
                },
              },
              h("span", {}, item.label),
              item.key ? h("kbd", {}, item.key) : item.detail ? h("span", { class: "menu-detail" }, item.detail) : null,
            ),
    ),
  );
  list.hidden = false;
  $("file-menu-button").setAttribute("aria-expanded", "true");
  list.querySelector("button:not([disabled])")?.focus();
}

function closeMenu() {
  if ($("file-menu").hidden) return;
  $("file-menu").hidden = true;
  $("file-menu-button").setAttribute("aria-expanded", "false");
}

/* ---------- Folders ---------- */

function resetWorkspace() {
  Object.assign(state, { overview: null, tree: null, entries: [], root: "", tabs: [], file: null, line: null, focus: null, byLine: [], cypher: [] });
  state.sessions.clear();
  state.expanded.clear();
  state.folded.clear();
  history.replaceState({ i: nav.i }, "", location.pathname);
  nav.at = null;
  for (const t of ["crossings", "sessions"]) $("tab-" + t).replaceChildren();
  search.token++;
  search.result = null;
  $("search-input").value = "";
  renderSearch();
  $("summary").textContent = "";
  selectTab("files");
  clearFile();
  renderFiles();
  if (!$("cypher").hidden) renderCypher();
}

// The browser can't hand a page a real folder path, so the local server shows the
// system's own folder dialog and returns the choice.
async function pickFolder() {
  closeMenu();
  closePalette();
  if (state.building) return toast(BUILD_BUSY);
  if (pickFolder.busy) return;
  pickFolder.busy = true;
  document.body.classList.add("picking");
  toast("Waiting for the system folder dialog…", 0);
  let result;
  try {
    result = await post("pick", {});
  } catch (err) {
    toast(err.message);
    if ($("server-notice").hidden) quickOpenFolder();
    return;
  } finally {
    pickFolder.busy = false;
    document.body.classList.remove("picking");
  }
  hideToast();
  if (result.path) await openFolder(result.path);
}

async function openFolder(path) {
  closePalette();
  if (state.building) return toast(BUILD_BUSY);
  let ws;
  try {
    ws = await post("open", { path });
  } catch (err) {
    return toast(err.message);
  }
  state.workspace = ws;
  resetWorkspace();
  rememberFolder(ws.project);
  if (ws.ready) await loadOverview({ restore: false });
  else await showUnbuilt();
}

// A folder with no graph still opens like an editor: its files, as plain text.
async function showUnbuilt() {
  const ws = state.workspace;
  if (ws?.project) {
    state.home = ws.home;
    state.root = ws.project.replace(/\/?$/, "/");
    try {
      state.tree = await api("tree");
    } catch (err) {
      toast(err.message);
    }
    state.entries = buildEntries();
    document.title = `${base(ws.project)} — Paper Trail`;
  }
  renderFiles();
  renderCrossings();
  showBuild();
}

function showBuild() {
  const ws = state.workspace;
  const box = $("code-empty");
  box.hidden = false;
  $("file-title").textContent = ws?.project ? base(ws.project) : "";
  if (!ws?.project) {
    box.replaceChildren(
      h("h2", {}, "Open a folder"),
      h("p", {}, "See which agent session wrote each line."),
      h("div", { class: "actions" }, h("button", { type: "button", class: "primary", onclick: pickFolder }, "Open Folder…"),
        h("button", { type: "button", onclick: quickOpenFolder }, "Open Recent…")),
    );
    return;
  }
  if (state.building) return renderBuildScreen();
  box.replaceChildren(
    h("h2", {}, `No history for ${base(ws.project)} yet`),
    h("p", { class: "fine", title: `The graph is saved to ${rel(ws.store)}` }, icon("lock"), "Reads your local Claude Code and Codex logs. Nothing leaves this computer."),
    h(
      "div",
      { class: "actions" },
      h("button", { type: "button", class: "primary", onclick: () => buildHistory() }, "Build History"),
      h("button", { type: "button", onclick: pickFolder }, "Open Another Folder…"),
    ),
  );
}

/* ---------- Building history ---------- */

// The server builds on a worker thread and reports counts; the page polls them.
const BUILD_POLL_MS = 300;
const BUILD_BUSY = "A build is running. Wait for it to finish.";
const BUILD_STAGES = ["find", "read", "select", "replay", "write", "done"];
const SOURCE_NAMES = { "claude-code": "Claude Code", codex: "Codex" };
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
let buildView = null;

async function buildHistory() {
  const ws = state.workspace;
  if (!ws?.project || state.building) return;
  closeMenu();
  let status;
  try {
    status = await post("build", { path: ws.project });
  } catch (err) {
    return toast(err.message);
  }
  await followBuild(status);
}

// Also picks up a build that was already running when this page loaded.
async function followBuild(status) {
  const ws = state.workspace;
  state.building = true;
  state.buildStatus = status;
  state.buildEta = null;
  if (state.overview) resetWorkspace();
  state.workspace = { ...ws, ready: false, building: true };
  if (state.file) clearFile();
  await showUnbuilt();
  while (status.running) {
    updateBuild(status);
    await sleep(BUILD_POLL_MS);
    try {
      status = await api("build");
    } catch (err) {
      state.building = false;
      showBuild();
      renderFiles();
      return toast(err.message);
    }
  }
  updateBuild(status);
  state.building = false;
  buildView = null;
  if (status.stage !== "done") {
    toast(status.error || "The build stopped early.", 6000);
    try {
      state.workspace = await api("workspace");
    } catch {
      /* Keep the last known workspace. */
    }
    if (state.workspace.ready) return loadOverview({ restore: false });
    return showUnbuilt();
  }
  const result = status.result;
  state.workspace = result;
  state.agentFolders = null;
  rememberFolder(result.project);
  toast(`Built: ${result.sessions} of ${plural(result.sessions_read, "session")}, ${result.seconds}s`, 4000);
  // A file opened while the build ran reopens with its tracks.
  await loadOverview({ restore: true });
}

function buildPercent(st) {
  if (!st) return 0;
  const at = BUILD_STAGES.indexOf(st.stage);
  if (st.stage === "done") return 100;
  if (at >= 2) return 95 + at;
  return Math.min(96, Math.floor((st.fraction || 0) * 96));
}

function sideBuildText() {
  return `Building… ${buildPercent(state.buildStatus)}%`;
}

function bytes(n) {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.ceil(n / 1e3)} KB`;
}

function clock(seconds) {
  const s = Math.floor(seconds || 0);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// Time left from the share of log bytes read so far, smoothed between polls.
function timeLeft(st) {
  // Outside the read stage the step list already says what is happening.
  if (st.stage !== "read" || st.elapsed < 3 || st.fraction < 0.02) return "";
  const raw = (st.elapsed * (1 - st.fraction)) / st.fraction;
  state.buildEta = state.buildEta == null ? raw : 0.8 * state.buildEta + 0.2 * raw;
  const s = state.buildEta;
  if (s < 10) return "a few seconds left";
  if (s < 60) return `~${Math.ceil(s / 5) * 5} s left`;
  return `~${Math.round(s / 60)} min left`;
}

function buildSteps(st) {
  const at = Math.max(0, BUILD_STAGES.indexOf(st.stage));
  const phase = (i) => (at > i ? "done" : at === i ? "active" : "todo");
  const sources = st.sources?.length ? st.sources : Object.keys(SOURCE_NAMES).map((name) => ({ name, pending: true }));
  const found = sources.filter((s) => !s.pending && !s.missing);
  const total = found.reduce((n, s) => n + s.files, 0);
  const size = found.reduce((n, s) => n + s.bytes, 0);
  const steps = [
    {
      label: "Find agent logs",
      state: phase(0),
      detail: at > 0 ? `${plural(total, "log file")}, ${bytes(size)}` : "~/.claude/projects · ~/.codex/sessions",
    },
  ];
  let active = false;
  for (const s of sources) {
    const label = `Read ${SOURCE_NAMES[s.name] || s.name} logs`;
    if (s.missing) {
      steps.push({ label, state: "skipped", detail: `No logs in ${rel(s.root)}` });
      continue;
    }
    if (s.pending) {
      steps.push({ label, state: "todo", detail: "" });
      continue;
    }
    const done = s.sessions != null;
    const state = done ? "done" : at >= 1 && !active ? "active" : "todo";
    active ||= state === "active";
    steps.push({
      label,
      state,
      detail: done
        ? `${plural(s.files, "file")}, ${plural(s.sessions, "session")}`
        : state === "active"
          ? `${s.read_files} of ${s.files} files · ${bytes(s.read_bytes)} of ${bytes(s.bytes)}`
          : `${plural(s.files, "file")}, ${bytes(s.bytes)}`,
      fraction: state === "active" ? (s.bytes ? s.read_bytes / s.bytes : 1) : null,
    });
  }
  steps.push(
    {
      label: "Keep this folder's sessions",
      state: phase(2),
      detail: st.sessions_kept != null ? `${st.sessions_kept} of ${plural(st.sessions_read, "session")}` : "",
    },
    { label: "Replay edits", state: phase(3), detail: st.files != null ? plural(st.files, "file") : "" },
    { label: "Write the graph", state: phase(4), detail: at >= 4 ? rel(st.store) : "" },
  );
  return steps;
}

function renderBuildScreen() {
  const ws = state.workspace;
  const view = {
    steps: h("ol", { class: "steps" }),
    clock: h("span", { class: "build-clock", title: "Time since the build started" }),
    fill: h("i"),
    left: h("span", { class: "build-left" }),
    said: h("p", { class: "sr-only", role: "status" }),
  };
  view.meter = h("span", { class: "meter", role: "progressbar", "aria-label": "History build", "aria-valuemin": "0", "aria-valuemax": "100" }, view.fill);
  buildView = view;
  $("code-empty").replaceChildren(
    h(
      "div",
      { class: "build" },
      h("div", { class: "build-head" }, h("h2", {}, `Building history for ${base(ws.project)}`), view.clock),
      h("div", { class: "build-body" }, loom(), view.steps),
      h("div", { class: "overall" }, view.meter, view.left),
      view.said,
    ),
  );
  if (state.buildStatus) updateBuild(state.buildStatus);
}

function stepRow() {
  return h(
    "li",
    { class: "step" },
    h("i", { class: "step-mark", "aria-hidden": "true" }),
    h("div", {}, h("b", {}), h("span", { class: "detail" }), h("span", { class: "meter", hidden: true }, h("i"))),
  );
}

function updateBuild(st) {
  state.buildStatus = st;
  const pct = buildPercent(st);
  $("summary").replaceChildren();
  document.title = `${pct}% · ${base(st.project)} — Paper Trail`;
  const side = $("side-build");
  if (side) {
    side.querySelector("p").textContent = sideBuildText();
    side.querySelector(".meter i").style.width = `${pct}%`;
  }
  const view = buildView;
  if (!view?.steps.isConnected) return;
  view.clock.textContent = clock(st.elapsed);
  view.fill.style.width = `${pct}%`;
  view.meter.setAttribute("aria-valuenow", String(pct));
  view.meter.classList.toggle("moving", st.running);
  view.left.textContent = [`${pct}%`, timeLeft(st)].filter(Boolean).join(" · ");
  const steps = buildSteps(st);
  // Update rows in place so the spinner keeps turning between polls.
  while (view.steps.children.length < steps.length) view.steps.append(stepRow());
  while (view.steps.children.length > steps.length) view.steps.lastChild.remove();
  steps.forEach((step, i) => {
    const li = view.steps.children[i];
    li.className = `step ${step.state}`;
    li.querySelector("b").textContent = step.label;
    li.querySelector(".detail").textContent = step.detail;
    const meter = li.querySelector(".meter");
    meter.hidden = step.fraction == null;
    if (step.fraction != null) meter.firstChild.style.width = `${Math.round(step.fraction * 100)}%`;
  });
  const now = steps.find((s) => s.state === "active");
  const said = now ? `${now.label}…` : "";
  if (view.said.textContent !== said) view.said.textContent = said;
}

// A few lines of code with session tracks drawing down the gutter beside them,
// the way the reader shows them once the history is built.
const LOOM = {
  period: 5600,
  hold: 4700,
  fade: 5200,
  row: 16,
  top: 4,
  perRow: 170,
  lines: [[0, 84], [10, 128], [10, 100], [20, 64], [20, 136], [10, 112], [20, 72], [10, 120], [0, 90]],
  // Older sessions sit next to the line numbers; each newer one runs outside it.
  tracks: [
    { color: PALETTE[0], from: 0, to: 6, x: 18, start: 200 },
    { color: PALETTE[1], from: 2, to: 5, x: 11, start: 1600 },
    { color: PALETTE[2], from: 4, to: 8, x: 4, start: 2500 },
  ],
};

function loom() {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs) => {
    const node = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    return node;
  };
  const { row, top, lines, tracks, period, hold, fade, perRow } = LOOM;
  const height = top * 2 + lines.length * row;
  const svg = el("svg", { class: "loom", viewBox: `0 0 220 ${height}`, "aria-hidden": "true" });
  const still = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const at = (ms) => Math.min(1, ms / period);
  const play = (node, frames) => !still && node.animate?.(frames, { duration: period, iterations: Infinity });
  lines.forEach(([indent, width], i) => {
    const y = top + i * row;
    const num = el("text", { class: "num", x: 40, y: y + 11.5, "text-anchor": "end" });
    num.textContent = String(i + 1);
    svg.append(num, el("rect", { class: "code-bar", x: 50 + indent, y: y + 5, width, height: 6, rx: 3 }));
  });
  // When a track passes a line, that line takes its color until a newer track passes.
  const hits = (t, r) => t.start + (r - t.from + 0.5) * perRow;
  for (const t of tracks) {
    const end = t.start + (t.to - t.from + 1) * perRow;
    const y0 = top + t.from * row;
    const y1 = top + (t.to + 1) * row;
    for (let r = t.from; r <= t.to; r++) {
      const [indent, width] = lines[r];
      const hit = hits(t, r);
      const next = tracks.filter((n) => n.start > t.start && n.from <= r && r <= n.to).map((n) => hits(n, r));
      const until = next.length ? Math.max(Math.min(...next), hit + 500) : null;
      const tint = el("rect", { x: 50 + indent, y: top + r * row + 5, width, height: 6, rx: 3, fill: t.color, opacity: still && until == null ? 0.45 : 0 });
      play(tint, [
        { offset: 0, opacity: 0 },
        { offset: at(hit), opacity: 0 },
        { offset: at(hit + 120), opacity: 0.9 },
        { offset: at(hit + 500), opacity: 0.45 },
        ...(until == null
          ? [{ offset: at(hold), opacity: 0.45 }, { offset: at(fade), opacity: 0 }]
          : [{ offset: at(until), opacity: 0.45 }, { offset: at(until + 160), opacity: 0 }]),
        { offset: 1, opacity: 0 },
      ]);
      svg.append(tint);
    }
    const bar = el("rect", { class: "lane", x: t.x, y: y0, width: 5, height: y1 - y0, fill: t.color });
    play(bar, [
      { offset: 0, transform: "scaleY(0)", opacity: 1 },
      { offset: at(t.start), transform: "scaleY(0)", opacity: 1 },
      { offset: at(end), transform: "scaleY(1)", opacity: 1 },
      { offset: at(hold), transform: "scaleY(1)", opacity: 1 },
      { offset: at(fade), transform: "scaleY(1)", opacity: 0 },
      { offset: 1, transform: "scaleY(0)", opacity: 0 },
    ]);
    svg.append(bar);
    if (still) continue;
    const hub = el("circle", { class: "hub", cx: t.x + 2.5, cy: 0, r: 3.4, opacity: 0 });
    const move = (y) => `translateY(${y}px)`;
    play(hub, [
      { offset: 0, opacity: 0, transform: move(y0) },
      { offset: at(t.start), opacity: 0, transform: move(y0) },
      { offset: at(t.start + 60), opacity: 1, transform: move(y0 + (60 / perRow) * row) },
      { offset: at(end), opacity: 1, transform: move(y1) },
      { offset: at(end + 220), opacity: 0, transform: move(y1) },
      { offset: 1, opacity: 0, transform: move(y1) },
    ]);
    svg.append(hub);
  }
  return svg;
}

async function loadOverview({ restore = true } = {}) {
  let tree;
  try {
    [state.overview, tree] = await Promise.all([api("overview"), state.workspace?.project ? api("tree") : null]);
  } catch (err) {
    $("summary").textContent = err.message;
    return;
  }
  state.tree = tree;
  record("Overview", state.overview.cypher);
  state.sessions.clear();
  state.overview.sessions.forEach((s, i) => state.sessions.set(s.id, { ...s, title: tidyTitle(s.title), color: PALETTE[i % PALETTE.length] }));
  state.home = state.overview.home;
  const project = state.overview.project;
  state.root = project ? project.replace(/\/?$/, "/") : projectRoot(state.overview.sessions, state.overview.files);
  state.entries = buildEntries();
  renderSummary();
  renderFiles();
  renderCrossings();
  renderSessions();
  const want = restore ? readHash() : {};
  clearFile();

  if (want.session && state.sessions.has(want.session)) state.focus = want.session;
  if (want.file) return openFile(want.file, want.line, { gap: want.gap });
  const best = state.overview.crossings.find((c) => c.start != null && inRoot(c.path));
  if (best) {
    selectTab("crossings");
    return openFile(best.path, best.start);
  }
  const busiest = [...state.overview.files].filter((f) => f.lines).sort((a, b) => inRoot(b.path) - inRoot(a.path) || b.sessions - a.sessions || b.edits - a.edits)[0];
  if (busiest) openFile(busiest.path);
}

// Which side the reader last touched decides what Find searches.
let lastArea = "code";

// The sidebar and the history panel are as wide as the reader drags them.
function bindGrips() {
  const layout = document.querySelector(".layout");
  const sizes = stored("papertrail-widths", {});
  const apply = () => {
    for (const key of ["side", "panel"]) {
      if (sizes[key]) layout.style.setProperty(`--${key}-w`, `${sizes[key]}px`);
      else layout.style.removeProperty(`--${key}-w`);
    }
  };
  const bind = (grip, key, pane, other, sign, min) => {
    grip.addEventListener("pointerdown", (e) => {
      if (e.button) return;
      e.preventDefault();
      grip.setPointerCapture(e.pointerId);
      const from = e.clientX;
      const width = pane.getBoundingClientRect().width;
      const max = Math.max(min, layout.clientWidth - other.getBoundingClientRect().width - 320);
      document.body.classList.add("resizing");
      const move = (ev) => {
        sizes[key] = Math.round(Math.max(min, Math.min(max, width + sign * (ev.clientX - from))));
        apply();
      };
      const done = () => {
        grip.removeEventListener("pointermove", move);
        document.body.classList.remove("resizing");
        store("papertrail-widths", sizes);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", done, { once: true });
      grip.addEventListener("pointercancel", done, { once: true });
    });
    grip.addEventListener("dblclick", () => {
      delete sizes[key];
      apply();
      store("papertrail-widths", sizes);
    });
  };
  const side = document.querySelector(".side");
  bind($("grip-side"), "side", side, $("panel"), 1, 180);
  bind($("grip-panel"), "panel", $("panel"), side, -1, 300);
  apply();
}

function bindWorkspaceEvents() {
  bindGrips();
  for (const type of ["pointerdown", "focusin"]) {
    document.addEventListener(type, (e) => (lastArea = e.target.closest?.("#panel") ? "chat" : "code"), true);
  }
  $("server-reload").addEventListener("click", () => location.reload());
  $("file-menu-button").addEventListener("click", () => ($("file-menu").hidden ? openMenu() : closeMenu()));
  $("file-menu").addEventListener("keydown", (e) => {
    const items = [...$("file-menu").querySelectorAll("button:not([disabled])")];
    const i = items.indexOf(document.activeElement);
    if (e.key === "ArrowDown") items[(i + 1) % items.length]?.focus();
    else if (e.key === "ArrowUp") items[(i - 1 + items.length) % items.length]?.focus();
    else if (e.key === "Escape") {
      closeMenu();
      $("file-menu-button").focus();
    } else return;
    e.preventDefault();
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".menu")) closeMenu();
  });

  const input = $("palette-input");
  input.addEventListener("input", refreshPalette);
  input.addEventListener("keydown", (e) => {
    const item = palette.items[palette.active];
    if (e.key === "ArrowDown") movePalette(1);
    else if (e.key === "ArrowUp") movePalette(-1);
    else if (e.key === "Enter") item && !item.disabled && choose(item);
    else if (e.key === "Tab" && item?.into) {
      input.value = item.into;
      refreshPalette();
    } else if (e.key === "Escape") closePalette();
    else return;
    e.preventDefault();
  });
  $("palette").addEventListener("mousedown", (e) => {
    if (e.target === $("palette")) closePalette();
  });

  document.addEventListener("keydown", (e) => {
    const mod = e.metaKey || e.ctrlKey;
    const key = e.key.toLowerCase();
    if (mod && !e.shiftKey && !e.altKey && key === "p") quickOpenFile();
    else if (mod && !e.shiftKey && !e.altKey && key === "o") pickFolder();
    else if (e.ctrlKey && !e.metaKey && !e.shiftKey && key === "r") quickOpenFolder();
    else if (e.ctrlKey && !e.metaKey && key === "g") goToLine();
    else if (e.ctrlKey && !e.metaKey && !e.altKey && (key === "-" || key === "_")) e.shiftKey ? !$("nav-forward").disabled && history.forward() : !$("nav-back").disabled && history.back();
    else if (mod && e.shiftKey && !e.altKey && key === "f") openSearch();
    else if (mod && !e.shiftKey && !e.altKey && key === "f" && (state.file?.lines || $("chat-find"))) ((lastArea === "chat" || !state.file?.lines) && openChatFind()) || openFind();
    else if (e.metaKey && !e.ctrlKey && key === "g" && find.hits.length) stepFind(e.shiftKey ? -1 : 1);
    else if (e.key === "Escape" && !$("file-menu").hidden) closeMenu();
    else return;
    e.preventDefault();
  });
}

async function start() {
  bindEvents();
  bindWorkspaceEvents();
  state.agentOnly = stored("papertrail-agent-only", false);
  state.showGaps = stored("papertrail-show-deletions", false) === true;
  try {
    state.workspace = await api("workspace");
  } catch (err) {
    $("summary").textContent = err.message;
    return;
  }
  if (state.workspace.building) {
    try {
      return await followBuild(await api("build"));
    } catch (err) {
      return toast(err.message);
    }
  }
  if (!state.workspace.ready) return showUnbuilt();
  if (state.workspace.project) rememberFolder(state.workspace.project);
  await loadOverview();
}

start();
