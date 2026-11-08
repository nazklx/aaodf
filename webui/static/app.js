"use strict";
// All DOM is built with h() + textContent, so file names can never inject markup.

const $ = (s) => document.querySelector(s);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k in el) el[k] = v;
    else el.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// Let `cond && h(...)` work in replaceChildren() too: drop false/null and flatten arrays.
const _replaceChildren = Element.prototype.replaceChildren;
Element.prototype.replaceChildren = function (...kids) {
  _replaceChildren.apply(this, kids.flat().filter((k) => k != null && k !== false));
};

async function api(method, url, body) {
  const r = await fetch(url, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : { "Accept": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await r.json(); } catch (_) { /* non-JSON */ }
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
}

let toastTimer;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false; t.style.background = bad ? "var(--bad)" : "";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 3500);
}
const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message, true); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const fmtSize = (n) => {
  if (n == null) return "";
  const u = ["B", "KiB", "MiB", "GiB", "TiB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
};
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" }) : "";
const enc = encodeURIComponent;

const state = {
  tab: "files", fs: null, hidden: false, filter: "", selected: new Map(),
  jobs: [], maxConc: 1, openJob: null, config: null, lib: null, dupOpts: null,
  duplicates: { path: "", results: [], selected: new Set(), scanId: "", meta: null, polling: false },
};

/* ---------------- lightbox ---------------- */
const lb = { dir: null, list: [], i: 0, onDelete: null };
function openLightbox(dir, list, i, onDelete) {
  Object.assign(lb, { dir, list, i, onDelete });
  drawLightbox();
}
function drawLightbox() {
  const box = $("#lightbox");
  const img = lb.list[lb.i];
  if (!img) { box.hidden = true; return; }
  box.hidden = false; box.replaceChildren(
    h("img", { src: `/api/outputs/${enc(lb.dir)}/image/${enc(img.name)}?v=${img.mtime}`, alt: img.name }),
    h("div", { class: "bar2" },
      h("button", { class: "btn", onclick: (e) => { e.stopPropagation(); step(-1); } }, "‹ Prev"),
      h("span", {}, `${lb.i + 1} / ${lb.list.length} · ${img.name} · ${fmtSize(img.size)}`),
      h("button", { class: "btn", onclick: (e) => { e.stopPropagation(); step(1); } }, "Next ›"),
      lb.onDelete && h("button", { class: "btn danger fill", onclick: (e) => { e.stopPropagation(); delCurrent(); } }, "Delete"),
      h("button", { class: "btn", onclick: closeLightbox }, "Close")));
}
const step = (d) => { lb.i = (lb.i + d + lb.list.length) % lb.list.length; drawLightbox(); };
const closeLightbox = () => { $("#lightbox").hidden = true; };
const delCurrent = guard(async () => {
  const img = lb.list[lb.i];
  await lb.onDelete([img.name]);
  lb.list.splice(lb.i, 1);
  if (lb.i >= lb.list.length) lb.i = 0;
  drawLightbox();
});
$("#lightbox").addEventListener("click", (e) => { if (e.target.id === "lightbox") closeLightbox(); });
document.addEventListener("keydown", (e) => {
  if ($("#lightbox").hidden) return;
  if (e.key === "Escape") closeLightbox();
  else if (e.key === "ArrowRight") step(1);
  else if (e.key === "ArrowLeft") step(-1);
  else if (e.key === "Delete" && lb.onDelete) delCurrent();
});

/* ---------------- gallery (used by job detail and library) ----------------
 * configure({ editable, onKeep, busy }) can be called at any time (the jobs page calls it on
 * every poll) so checkboxes appear/disappear as the job's state changes.
 * With onKeep set, a "Pick to keep" mode is offered next to the normal "Pick to delete" one:
 * the ticked images are uploaded and everything else in the folder is deleted. */
function Gallery(dir, { editable = true } = {}) {
  const sel = new Set();
  let images = [], sig = "", loaded = false, keepMode = false;
  let mode = { editable, onKeep: null, busy: false };
  const root = h("div");
  const actions = h("div", { class: "row" });
  const grid = h("div", { class: "grid" });
  root.append(actions, grid);

  async function del(names) {
    await api("DELETE", `/api/outputs/${enc(dir)}/images`, { names });
    names.forEach((n) => sel.delete(n));
    await refresh(true);
  }
  function setKeepMode(on) { keepMode = on; sel.clear(); draw(); }
  function draw() {
    const canKeep = mode.editable && !!mode.onKeep;
    if (!canKeep) keepMode = false;
    actions.replaceChildren(
      h("span", { class: "muted" }, `${images.length} image${images.length === 1 ? "" : "s"}`),
      canKeep && h("span", { class: "seg" },
        h("button", { class: "btn small" + (keepMode ? "" : " on"), title: "Tick the images you don't want, then delete them", onclick: () => setKeepMode(false) }, "Pick to delete"),
        h("button", { class: "btn small" + (keepMode ? " on" : ""), title: "Tick the images you want; the rest are deleted automatically", onclick: () => setKeepMode(true) }, "Pick to keep")),
      mode.editable && h("button", { class: "btn small", onclick: () => { images.forEach((i) => sel.add(i.name)); draw(); } }, "Select all"),
      mode.editable && h("button", { class: "btn small", onclick: () => { sel.clear(); draw(); } }, "Clear"),
      mode.editable && !keepMode && h("button", {
        class: "btn danger small", disabled: !sel.size,
        onclick: guard(async () => { if (confirm(`Delete ${sel.size} image(s)? This cannot be undone.`)) await del([...sel]); }),
      }, `Delete selected (${sel.size})`),
      canKeep && keepMode && h("button", {
        class: "btn primary small", disabled: !sel.size || mode.busy,
        onclick: guard(async () => {
          const drop = images.length - sel.size;
          if (confirm(`Upload the ${sel.size} selected image(s) and permanently delete the other ${drop}?`)) await mode.onKeep([...sel]);
        }),
      }, `Upload ${sel.size} selected, delete the rest`));
    grid.className = "grid" + (keepMode ? " keeping" : "");
    if (!loaded) { grid.replaceChildren(h("div", { class: "empty" }, "Loading…")); return; }
    grid.replaceChildren(...images.map((img, idx) => h("div", { class: "card" + (sel.has(img.name) ? (keepMode ? " sel keep" : " sel") : "") },
      mode.editable && h("input", { type: "checkbox", checked: sel.has(img.name), onchange: (e) => { e.target.checked ? sel.add(img.name) : sel.delete(img.name); draw(); } }),
      h("img", { loading: "lazy", src: `/api/outputs/${enc(dir)}/thumb/${enc(img.name)}?v=${img.mtime}`, alt: img.name,
        onclick: () => openLightbox(dir, images, idx, mode.editable && !keepMode ? (names) => del(names) : null) }),
      h("div", { class: "cap" }, h("span", {}, img.name.replace(/^.*_frame_/, "frame ").replace(".png", "")), h("span", {}, fmtSize(img.size))))));
    if (!images.length) grid.replaceChildren(h("div", { class: "empty" }, "No images"));
  }
  async function refresh(force) {
    const { images: list } = await api("GET", `/api/outputs/${enc(dir)}/images`);
    const s = list.map((i) => i.name + i.mtime).join("|");
    images = list;
    for (const n of [...sel]) if (!list.some((i) => i.name === n)) sel.delete(n);
    if (force || s !== sig || !loaded) { sig = s; loaded = true; draw(); }
  }
  function configure(next) {
    const changed = next.editable !== mode.editable || !!next.onKeep !== !!mode.onKeep || !!next.busy !== mode.busy;
    mode = { editable: next.editable, onKeep: next.onKeep || null, busy: !!next.busy };
    if (changed) { if (!mode.editable) sel.clear(); draw(); }
  }
  draw();
  refresh(true).catch((e) => toast(e.message, true));
  return { el: root, configure, refresh: () => refresh(false).catch(() => {}) };
}

/* ---------------- shared job options (Files tray and Duplicates tab) ---------------- */
function cfgDefaults() {
  const c = state.config || {};
  return {
    review: true, auto_upload: c.confirm_before_upload === false, anonymous_upload: !!c.anonymous_upload,
    personal_release: !!c.personal_release, stream_optimized: !!c.stream_optimized,
    free: Number(c.free) || 0, free_days: Number(c.free_days) || 0,
    doubleup: c.doubleup ? 1 : 0, doubleup_days: Number(c.doubleup_days) || 0, featured: c.featured ? 1 : 0,
    screenshot_count: "", category: "", save_path: "",
  };
}

/* Free / double upload / featured controls, shared by the options panel and the pre-upload editor.
 * `m` is edited in place: free (0 = off, otherwise 1-100 %), free_days, doubleup (0/1),
 * doubleup_days (0 = never expires) and featured (0/1). */
function promoControls(m) {
  const box = (checked, onchange) => h("input", { type: "checkbox", checked: !!checked, onchange });
  const num = (value, min, max, oninput) => h("input", { type: "number", min, max, step: 1, value: value || "", oninput });
  const freeOn = box(m.free > 0, () => apply());
  const freePct = num(m.free > 0 ? m.free : 100, 1, 100, () => apply());
  const freeDays = num(m.free_days, 1, 3650, () => apply());
  const dblOn = box(m.doubleup, () => apply());
  const dblDays = num(m.doubleup_days, 1, 3650, () => apply());
  const featOn = box(m.featured, () => apply());
  const days = (el) => (el.value === "" ? 0 : Number(el.value));
  function apply() {
    m.free = freeOn.checked ? (freePct.value === "" ? NaN : Number(freePct.value)) : 0;
    m.free_days = freeOn.checked ? days(freeDays) : 0;
    m.doubleup = dblOn.checked ? 1 : 0;
    m.doubleup_days = dblOn.checked ? days(dblDays) : 0;
    m.featured = featOn.checked ? 1 : 0;
    freePct.disabled = freeDays.disabled = !freeOn.checked;
    dblDays.disabled = !dblOn.checked;
  }
  apply();
  return h("div", { class: "promo" },
    h("div", { class: "line" }, h("label", { class: "chk" }, freeOn, "Free"), freePct, "% for", freeDays, "days"),
    h("div", { class: "line" }, h("label", { class: "chk" }, dblOn, "Double upload"), "for", dblDays, "days"),
    h("div", { class: "line" }, h("label", { class: "chk" }, featOn, "Featured (sticky)")),
    h("div", { class: "muted small" }, "Leave days blank for no expiry. The tracker ignores these unless your account is allowed to set them."));
}

// Throws a user-facing Error when the promotion values are out of range.
function checkPromo(m) {
  const whole = (v, lo, hi) => Number.isInteger(v) && v >= lo && v <= hi;
  if (m.free !== 0 && !whole(m.free, 1, 100)) throw new Error("Free percentage must be a whole number from 1 to 100");
  if (!whole(m.free_days, 0, 3650)) throw new Error("Free days must be a whole number from 0 to 3650");
  if (!whole(m.doubleup_days, 0, 3650)) throw new Error("Double upload days must be a whole number from 0 to 3650");
}

// Blank screenshot count / category / target directory mean "use the Settings value".
function optionsPanel(o) {
  const c = state.config || {};
  const client = c.download_client;
  const defaultDir = client ? (client.type === "transmission" ? client.download_dir : client.save_path) : "";
  const note = h("div", { class: "notice small" }, "Fully unattended: images and the release go out without any prompt.");
  const sync = () => { note.hidden = !(!o.review && o.auto_upload); };
  const chk = (key, label, hint) => h("label", { class: "chk", title: hint || "" },
    h("input", { type: "checkbox", checked: o[key], onchange: (e) => { o[key] = e.target.checked; sync(); } }), label);
  const field = (key, label, placeholder, hint, type = "text", extra = {}) => h("div", { class: "f" },
    h("label", {}, label),
    h("input", { type, value: o[key] ?? "", placeholder, disabled: !!extra.disabled, ...(type === "number" ? { min: 1, max: 200, step: 1 } : {}),
      oninput: (e) => { o[key] = e.target.value; } }),
    hint && h("div", { class: "muted small" }, hint));
  sync();
  return h("div", { class: "opts" },
    chk("review", "Pause to review images before they are hosted", "Matches the CLI's ‘delete unwanted images’ pause"),
    chk("auto_upload", "Upload to tracker without asking", "Skips the final confirmation"),
    chk("anonymous_upload", "Anonymous upload"), chk("personal_release", "Personal release"), chk("stream_optimized", "Stream optimised"),
    promoControls(o),
    note,
    h("div", { class: "optfields" },
      field("screenshot_count", "Screenshots per job", `${c.screenshot_count ?? 18} (from Settings)`, null, "number"),
      field("category", "qBittorrent category", client && client.category ? `${client.category} (from Settings)` : "none", client ? "qBittorrent only." : null, "text", { disabled: !client }),
      field("save_path", "Target directory", defaultDir ? `${defaultDir} (from Settings)` : "client default", client ? "As seen by the download client." : null, "text", { disabled: !client }),
      !client && h("div", { class: "muted small" }, "No download client is set in Settings, so category and target directory are ignored.")));
}

function jobPayload(o) {
  checkPromo(o);
  const p = {
    review: o.review, auto_upload: o.auto_upload, anonymous_upload: o.anonymous_upload, personal_release: o.personal_release, stream_optimized: o.stream_optimized,
    free: o.free, free_days: o.free_days, doubleup: !!o.doubleup, doubleup_days: o.doubleup_days, featured: !!o.featured,
  };
  const n = String(o.screenshot_count ?? "").trim();
  if (n) {
    if (!/^\d+$/.test(n) || +n < 1 || +n > 200) throw new Error("Screenshots per job must be a whole number from 1 to 200");
    p.screenshot_count = +n;
  }
  for (const k of ["category", "save_path"]) {
    const v = String(o[k] ?? "").trim();
    if (v) p[k] = v;
  }
  return p;
}

async function queueJobs(paths, o) {
  const r = await api("POST", "/api/jobs", { paths, options: jobPayload(o) });
  toast(`Queued ${r.created.length}` + (r.skipped.length ? `, skipped ${r.skipped.length} already active` : ""));
  switchTab("jobs");
  return r;
}

/* ---------------- FILES tab ---------------- */
const files = { list: h("div"), crumbs: h("div", { class: "crumbs" }), tray: h("div", { class: "panel tray" }), path: null, inspect: {}, opts: null };

function buildFiles() {
  const root = $("#tab-files");
  files.path = h("input", { type: "text", class: "grow", onkeydown: (e) => { if (e.key === "Enter") goto(e.target.value); } });
  const filter = h("input", { type: "text", placeholder: "Filter…", oninput: (e) => { state.filter = e.target.value.toLowerCase(); drawList(); } });
  root.append(
    h("div", { class: "files-layout" },
      h("div", { class: "panel" },
        h("div", { class: "row" },
          h("button", { class: "btn", title: "Up", onclick: () => state.fs && state.fs.parent && goto(state.fs.parent) }, "↑ Up"),
          files.path,
          h("button", { class: "btn", onclick: () => goto(state.fs.path) }, "Refresh"),
          h("label", { class: "chk small" }, h("input", { type: "checkbox", onchange: (e) => { state.hidden = e.target.checked; goto(state.fs.path); } }), "Hidden"),
          filter),
        h("div", { class: "spacer" }), files.crumbs,
        h("div", { class: "fs-scroll" }, files.list)),
      files.tray));
  goto(null);
}

const goto = guard(async (path) => {
  const q = new URLSearchParams({ hidden: state.hidden ? "1" : "0" });
  if (path) q.set("path", path);
  state.fs = await api("GET", "/api/fs?" + q);
  files.path.value = state.fs.path;
  drawCrumbs(); drawList(); drawTray();
});

function drawCrumbs() {
  const { path, root } = state.fs;
  const rel = path.startsWith(root) ? path.slice(root.length) : path;
  const parts = rel.split("/").filter(Boolean);
  let acc = root.replace(/\/$/, "");
  files.crumbs.replaceChildren(h("button", { onclick: () => goto(root) }, root === "/" ? "/" : root),
    ...parts.flatMap((p) => { acc += "/" + p; const target = acc; return [h("span", { class: "muted" }, "/"), h("button", { onclick: () => goto(target) }, p)]; }));
}

function visibleEntries() {
  return state.fs.entries.filter((e) => !state.filter || e.name.toLowerCase().includes(state.filter));
}

function toggleSel(e, on) {
  const want = on ?? !state.selected.has(e.path);
  want ? state.selected.set(e.path, e.name) : state.selected.delete(e.path);
}

function drawList() {
  const rows = visibleEntries();
  const allOn = rows.length && rows.every((e) => state.selected.has(e.path));
  files.list.replaceChildren(h("table", { class: "fs" },
    h("thead", {}, h("tr", {},
      h("th", {}, h("input", { type: "checkbox", checked: !!allOn, onchange: (ev) => { rows.forEach((e) => toggleSel(e, ev.target.checked)); drawList(); drawTray(); } })),
      h("th", {}, "Name"), h("th", {}, "Size"), h("th", {}, "Modified"))),
    h("tbody", {}, rows.map((e) => h("tr", { class: state.selected.has(e.path) ? "sel" : "" },
      h("td", {}, h("input", { type: "checkbox", checked: state.selected.has(e.path), onchange: () => { toggleSel(e); drawList(); drawTray(); } })),
      h("td", { class: "name" }, e.dir
        ? h("button", { class: "link dir", onclick: () => goto(e.path) }, "📁 " + e.name)
        : h("button", { class: "link", onclick: () => { toggleSel(e); drawList(); drawTray(); } }, (e.video ? "🎞 " : "📄 ") + e.name),
        e.video && h("span", { class: "tag" }, "video")),
      h("td", { class: "num" }, fmtSize(e.size)), h("td", { class: "num" }, fmtTime(e.mtime))))),
    ),
    !rows.length && h("div", { class: "empty" }, "Empty"),
    state.fs.truncated && h("div", { class: "muted small" }, "Showing the first 5000 entries only."));
}

function drawTray() {
  files.opts = files.opts || cfgDefaults();
  const n = state.selected.size;
  files.tray.replaceChildren(
    h("h2", {}, `Selection (${n})`),
    n ? h("ul", {}, [...state.selected].map(([p]) => h("li", {},
      h("button", { class: "btn small", title: "Remove", onclick: () => { state.selected.delete(p); drawList(); drawTray(); } }, "✕"),
      h("span", { title: p }, "‎" + p)))) : h("p", { class: "muted" }, "Tick files or folders (a video file, or a release folder) to run the script on."),
    ...[...state.selected.keys()].filter((p) => files.inspect[p]).map((p) => h("div", { class: "insp" }, describe(p, files.inspect[p]))),
    optionsPanel(files.opts),
    h("div", { class: "row" },
      h("button", { class: "btn", disabled: !n, onclick: guard(inspectAll) }, "Inspect"),
      h("button", { class: "btn", disabled: !n, onclick: () => { state.selected.clear(); drawList(); drawTray(); } }, "Clear"),
      h("button", { class: "btn primary", disabled: !n, onclick: guard(queueSelected) }, n > 1 ? `Queue ${n} as batch` : "Run")));
}

function describe(p, i) {
  if (!i.ok) return `${p.split("/").pop()}: ${i.error}`;
  return `${i.name} → ${i.sources} source video(s) of ${i.videos}${i.tv ? ", TV" : ""}; output ${i.out_dir}${i.output_exists ? " (exists, will be overwritten)" : ""}`;
}

async function inspectAll() {
  for (const p of state.selected.keys()) {
    files.inspect[p] = await api("GET", "/api/inspect?path=" + enc(p)).catch((e) => ({ ok: false, error: e.message }));
    drawTray();
  }
}

async function queueSelected() {
  await queueJobs([...state.selected.keys()], files.opts);
  state.selected.clear(); files.inspect = {}; drawList(); drawTray();
}

/* ---------------- DUPLICATES tab ---------------- */
const duplicatesUI = { root: h("div"), path: null, results: h("div"), summary: h("div"), scanBtn: null, cancelBtn: null };
const getDupOpts = () => (state.dupOpts = state.dupOpts || cfgDefaults());

function buildDuplicates() {
  const d = state.duplicates;
  const limit = (state.config && state.config.tracker_search_rate_limit) || 30;
  duplicatesUI.path = h("input", { type: "text", class: "grow", placeholder: "/path/to/qBittorrent/downloads", value: d.path,
    oninput: (e) => { d.path = e.target.value; } });
  duplicatesUI.scanBtn = h("button", { class: "btn primary", onclick: guard(runDuplicateScan) }, "Scan tracker");
  duplicatesUI.cancelBtn = h("button", { class: "btn danger", hidden: true, onclick: guard(async () => { await api("POST", "/api/duplicates/scan/cancel", {}); }) }, "Cancel scan");
  duplicatesUI.root.replaceChildren(
    h("div", { class: "panel" },
      h("h2", {}, "Find tracker duplicates"),
      h("p", { class: "muted small" }, "Select a qBittorrent-style download directory. The scanner checks each immediate subfolder as one release and each top-level MKV/MP4 file individually; it does not descend into loose files or treat files inside folders separately."),
      h("div", { class: "row" }, duplicatesUI.path,
        h("button", { class: "btn", onclick: guard(openDirectoryPicker) }, "Browse"),
        duplicatesUI.scanBtn, duplicatesUI.cancelBtn),
      h("div", { class: "muted small", style: "margin-top:8px" },
        `Tracker searches are limited to ${limit} per minute (Settings → Tracker search rate limit), so big directories take a while; the scan keeps running if you switch tabs. A search error stops the scan, and items are only marked unmatched after a successful tracker response.`)),
    h("div", { class: "spacer" }),
    h("div", { class: "panel" }, h("h2", {}, "Options for jobs added from this scan"), optionsPanel(getDupOpts())),
    h("div", { class: "spacer" }), duplicatesUI.summary, duplicatesUI.results);
  $("#tab-duplicates").replaceChildren(duplicatesUI.root);
  drawDuplicates();
}

async function openDirectoryPicker() {
  const first = await api("GET", "/api/fs");
  let current = dpath(state.duplicates.path) || first.path;
  const root = first.root;
  const overlay = h("div", { class: "picker-backdrop" });
  const panel = h("div", { class: "picker panel" });
  const location = h("div", { class: "muted small" });
  const listing = h("div", { class: "picker-list" });
  async function load(path) {
    const data = await api("GET", "/api/fs?path=" + enc(path));
    current = data.path;
    location.textContent = current;
    listing.replaceChildren(...data.entries.filter((e) => e.dir).map((e) => h("button", { class: "picker-entry", onclick: guard(() => load(e.path)) }, "📁 " + e.name)));
    if (!listing.childNodes.length) listing.append(h("div", { class: "muted small" }, "No subfolders"));
  }
  panel.append(h("div", { class: "row" }, h("h2", { class: "grow" }, "Choose download directory"),
    h("button", { class: "btn", onclick: () => overlay.remove() }, "Close")),
    h("div", { class: "row" }, h("button", { class: "btn", onclick: guard(async () => { const data = await api("GET", "/api/fs?path=" + enc(current)); if (data.parent) await load(data.parent); }) }, "↑ Up"), location),
    listing,
    h("div", { class: "row" }, h("button", { class: "btn", onclick: () => load(root) }, "Go to allowed root"),
      h("button", { class: "btn primary", onclick: () => { state.duplicates.path = current; duplicatesUI.path.value = current; overlay.remove(); } }, "Use this directory")));
  overlay.append(panel); document.body.append(overlay);
  await load(current);
}
function dpath(value) { return String(value || "").trim(); }

async function runDuplicateScan() {
  const d = state.duplicates;
  const path = dpath(d.path);
  if (!path) throw new Error("Choose a download directory first");
  const { scan } = await api("POST", "/api/duplicates/scan", { path });
  applyScan(scan);
  pollScan();
}

// Merge a (possibly partial) scan snapshot from the server into the client state.
function applyScan(scan) {
  const d = state.duplicates;
  if (scan.id !== d.scanId) { d.scanId = scan.id; d.results = []; d.selected = new Set(); }
  else d.results.length = Math.min(d.results.length, scan.since);
  for (const item of scan.results) {
    d.results.push(item);
    if (item.status === "unmatched") d.selected.add(item.path);
  }
  const { results, ...meta } = scan;
  d.meta = meta;
  drawDuplicates();
}

async function pollScan() {
  const d = state.duplicates;
  if (d.polling) return;
  d.polling = true;
  let failures = 0;
  try {
    for (;;) {
      await sleep(1000);
      let scan;
      try {
        ({ scan } = await api("GET", `/api/duplicates/scan?id=${enc(d.scanId)}&since=${d.results.length}`));
        failures = 0;
      } catch (e) {
        if (++failures >= 5) { toast("Lost contact with the server: " + e.message, true); break; }
        continue;
      }
      if (!scan) {
        d.meta = { ...d.meta, status: "failed", error: "The scan is no longer available (the server may have restarted)." };
        drawDuplicates();
        break;
      }
      applyScan(scan);
      if (scan.status !== "running") break;
    }
  } finally { d.polling = false; }
}

async function resumeDuplicateScan() {
  const { scan } = await api("GET", "/api/duplicates/scan?since=0");
  if (!scan) return;
  if (!state.duplicates.path) state.duplicates.path = scan.path;
  applyScan(scan);
  if (scan.status === "running") pollScan();
}

function drawDuplicates() {
  const d = state.duplicates, m = d.meta;
  const running = !!m && m.status === "running";
  if (duplicatesUI.scanBtn) { duplicatesUI.scanBtn.disabled = running; duplicatesUI.cancelBtn.hidden = !running; }
  if (!m) { duplicatesUI.summary.replaceChildren(); duplicatesUI.results.replaceChildren(); return; }

  const by = (s) => d.results.filter((x) => x.status === s);
  const unmatched = by("unmatched"), matched = by("matched"), skipped = by("skipped");
  const complete = m.status === "done";
  const headline = {
    running: `Scanning ${m.done}/${m.total}`, done: `Scanned ${m.done} item(s)`,
    cancelled: `Scan cancelled after ${m.done}/${m.total}`, failed: `Scan stopped after ${m.done}/${m.total}`,
  }[m.status] || m.status;
  duplicatesUI.summary.replaceChildren(
    h("div", { class: "row" }, h("strong", {}, headline),
      h("span", { class: "muted small" }, `${matched.length} with tracker matches · ${unmatched.length} unmatched · ${skipped.length} skipped`)),
    running && h("div", { class: "bar" }, h("div", { style: `width:${m.total ? Math.round(100 * m.done / m.total) : 0}%` })),
    running && m.current && h("div", { class: "muted small" }, "Searching: " + m.current),
    m.error && h("div", { class: "err" }, m.error),
    !complete && !running && h("div", { class: "muted small" }, "The scan is incomplete, so adding to a batch is disabled. Results so far are shown below."));

  const addPaths = (paths) => guard(async () => {
    await queueJobs(paths, getDupOpts());
    paths.forEach((p) => d.selected.delete(p));
  });
  const chosen = unmatched.filter((x) => d.selected.has(x.path)).map((x) => x.path);
  const unmatchedPanel = h("div", { class: "panel" },
    h("div", { class: "row" }, h("h2", { class: "grow" }, `No tracker matches (${unmatched.length})`),
      h("button", { class: "btn small", onclick: () => { d.selected = new Set(unmatched.map((x) => x.path)); drawDuplicates(); } }, "Select all"),
      h("button", { class: "btn small", onclick: () => { d.selected.clear(); drawDuplicates(); } }, "Clear selection")),
    unmatched.length ? h("div", { class: "dup-list" }, unmatched.map((item) => h("label", { class: "dup-item" },
      h("input", { type: "checkbox", checked: d.selected.has(item.path), onchange: (e) => { e.target.checked ? d.selected.add(item.path) : d.selected.delete(item.path); drawDuplicates(); } }),
      h("span", { class: "grow" }, h("strong", {}, item.name), h("span", { class: "muted small" }, `${item.kind} · search: ${item.query}`))))) : h("div", { class: "empty" }, running ? "Nothing yet…" : "No unmatched items to add."),
    h("div", { class: "row" }, h("span", { class: "muted small grow" }, `${chosen.length} selected`),
      h("button", { class: "btn", disabled: !complete || !chosen.length, onclick: addPaths(chosen) }, `Add selected as batch (${chosen.length})`),
      h("button", { class: "btn primary", disabled: !complete || !unmatched.length, onclick: guard(async () => {
        const all = unmatched.map((x) => x.path);
        if (!confirm(`Queue all ${all.length} item(s) with no tracker match as jobs?`)) return;
        await addPaths(all)();
      }) }, `Add all unmatched as batch (${unmatched.length})`)));
  const matchedRows = matched.map((item) => {
    const lines = item.matches.slice(0, 5).map((mt) => {
      const meta = [mt.resolution, mt.type, mt.seeders == null ? "" : `${mt.seeders} seeders`].filter(Boolean);
      return h("span", { class: "match-line" }, mt.name + (meta.length ? " · " + meta.join(" · ") : ""));
    });
    return h("div", { class: "dup-item matched" }, h("span", { class: "grow" },
      h("strong", {}, item.name), h("span", { class: "muted small" }, `Search: ${item.query}`), ...lines));
  });
  const matchedPanel = h("div", { class: "panel" }, h("h2", {}, `Possible duplicates (${matched.length})`),
    matched.length ? h("div", { class: "dup-list" }, matchedRows) : h("div", { class: "empty" }, running ? "Nothing yet…" : "No tracker matches were found."));
  const skippedPanel = skipped.length ? h("div", { class: "panel" }, h("h2", {}, `Skipped (${skipped.length})`),
    h("p", { class: "muted small" }, "These folders contain no supported MKV/MP4 video, so they were not searched and cannot be queued."),
    ...skipped.map((item) => h("div", { class: "muted small" }, item.name))) : null;
  duplicatesUI.results.replaceChildren(unmatchedPanel, h("div", { class: "spacer" }), matchedPanel, skippedPanel);
}

/* ---------------- JOBS tab ---------------- */
// The detail panel is sticky (see .jobs-detail in style.css) so the open job stays in view while the job list scrolls.
const jobsUI = { list: h("div"), detail: h("div", { class: "jobs-detail" }), toolbar: h("div", { class: "row" }), logEl: null, logCount: 0, logJob: null, gallery: null, galleryDir: null, sig: "", lastDetail: null };
const planDrafts = new Map();   // job id -> edited upload form; survives re-renders and failed attempts
const STEPS = ["Frames", "Review", "Images", "Finalize", "Upload"];
const STAGE_IDX = { starting: 0, frames: 0, review: 1, hosting: 2, finalize: 3, confirm: 4, uploading: 4 };
const STATUS_TEXT = { queued: "Queued", running: "Running", awaiting_review: "Needs review", awaiting_confirm: "Confirm upload", finished: "Finished", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted" };
const isActive = (j) => ["queued", "running", "awaiting_review", "awaiting_confirm"].includes(j.status);

function buildJobs() {
  $("#tab-jobs").append(jobsUI.toolbar, h("div", { class: "spacer" }),
    h("div", { class: "jobs-layout" }, jobsUI.list, jobsUI.detail));
}

function drawJobsToolbar() {
  jobsUI.toolbar.replaceChildren(
    h("label", { class: "chk" }, "Run up to ",
      h("input", { type: "number", min: 1, max: 8, value: state.maxConc, onchange: guard(async (e) => { await api("POST", "/api/queue", { max_concurrent: +e.target.value }); }) }), " at once"),
    h("span", { class: "muted small" }, "Jobs waiting for you do not use a slot."),
    h("span", { class: "grow" }),
    h("button", { class: "btn", onclick: guard(async () => { await api("POST", "/api/jobs/clear", {}); await refreshJobs(); }) }, "Clear finished"));
}

function drawJobsList() {
  const jobs = state.jobs;
  jobsUI.list.replaceChildren(...(jobs.length ? jobs.map((j) => h("div", { class: "job " + j.status + (state.openJob === j.id ? " open" : ""), onclick: () => { state.openJob = j.id; drawJobsList(); drawJobDetail(true); } },
    h("div", { class: "row" }, h("div", { class: "title grow", title: j.target }, j.name || j.target.split("/").pop()), h("span", { class: "pill " + j.status }, STATUS_TEXT[j.status] || j.status)),
    h("div", { class: "bar" }, h("div", { style: `width:${j.status === "finished" && j.uploaded ? 100 : j.percent}%` })),
    h("div", { class: "muted small" }, jobLine(j)))) : [h("div", { class: "empty" }, "No jobs yet. Pick files on the Files tab.")]));
}

function jobLine(j) {
  if (j.status === "failed" || j.status === "interrupted") return j.error || "";
  if (j.status === "finished") return j.uploaded ? "Uploaded" + (j.link ? " · " + j.link : "") : j.error || "Ended without upload";
  if (j.stage === "frames") return `Extracting frames ${j.frames_done}/${j.frames_total || "?"}`;
  if (j.stage === "hosting") return `Hosting images ${j.hosted}/${j.hosted_total || "?"}`;
  return j.status === "queued" ? "Waiting for a free slot" : (j.detail || j.stage);
}

function jobOverrides(j) {
  const o = j.options || {}, bits = [];
  if (o.screenshot_count) bits.push(`${o.screenshot_count} screenshots`);
  if (o.category) bits.push(`category ${o.category}`);
  if (o.save_path) bits.push(`save to ${o.save_path}`);
  if (o.free) bits.push(`free ${o.free}%` + (o.free_days ? ` for ${o.free_days}d` : ""));
  if (o.doubleup) bits.push("double upload" + (o.doubleup_days ? ` for ${o.doubleup_days}d` : ""));
  if (o.featured) bits.push("featured");
  return bits.join(" · ");
}

// Review shortcut: upload only the ticked images and delete every other one.
const keepSelected = (id) => async (names) => {
  await api("POST", `/api/jobs/${id}/proceed`, { keep: names });
  toast(`Uploading ${names.length} selected image(s)`);
  await refreshJobs();
};

function drawJobDetail(force) {
  const j = state.jobs.find((x) => x.id === state.openJob);
  if (!j) {
    jobsUI.detail.replaceChildren(h("div", { class: "panel muted" }, "Select a job to see progress, images and the log."));
    Object.assign(jobsUI, { logJob: null, gallery: null, galleryDir: null, sig: "", lastDetail: null });
    return;
  }

  // The gallery is re-evaluated from the job's current state on every poll (it used to be
  // decided once at first render, so images/checkboxes only appeared after leaving the tab).
  const wantGallery = !!j.out_dir && !["queued", "starting"].includes(j.stage);
  const reviewing = j.status === "awaiting_review";
  let galleryChanged = false;
  if (!wantGallery) {
    galleryChanged = !!jobsUI.gallery;
    jobsUI.gallery = null; jobsUI.galleryDir = null;
  } else if (!jobsUI.gallery || jobsUI.galleryDir !== j.out_dir) {
    jobsUI.gallery = Gallery(j.out_dir); jobsUI.galleryDir = j.out_dir; galleryChanged = true;
  }
  if (jobsUI.gallery) jobsUI.gallery.configure({ editable: reviewing || !isActive(j), onKeep: reviewing ? keepSelected(j.id) : null, busy: j.regenerating });

  const sig = JSON.stringify([j.id, j.status, j.stage, j.regenerating, j.plan, j.error, j.link, Math.floor(j.percent / 5)]);
  const rerender = force || galleryChanged || sig !== jobsUI.sig || jobsUI.logJob !== j.id;
  if (rerender) {
    jobsUI.sig = sig;
    const cur = STAGE_IDX[j.stage] ?? -1;
    const done = j.status === "finished";
    if (jobsUI.logJob !== j.id) {   // keep the same <pre> (and its scroll position) while the same job stays open
      jobsUI.logEl = h("pre", { class: "log" });
      jobsUI.logCount = 0; jobsUI.logJob = j.id;
    }
    const overrides = jobOverrides(j);
    // The panel scrolls on its own (it is sticky), so keep its position across re-renders of the same job.
    const keepTop = jobsUI.lastDetail === j.id ? jobsUI.detail.scrollTop : 0;
    jobsUI.detail.replaceChildren(h("div", { class: "panel" },
      h("div", { class: "row" }, h("h2", { class: "grow" }, j.name || j.target), h("span", { class: "pill " + j.status }, STATUS_TEXT[j.status])),
      h("div", { class: "muted small", style: "word-break:break-all" }, j.target),
      overrides && h("div", { class: "muted small" }, "Overrides: " + overrides),
      h("div", { class: "bar" }, h("div", { style: `width:${done && j.uploaded ? 100 : j.percent}%` })),
      h("div", { class: "steps" }, STEPS.map((s, i) => h("span", { class: done ? "past" : i < cur ? "past" : i === cur && isActive(j) ? "now" : "" }, s))),
      j.error && h("div", { class: "err" }, j.error),
      j.link && h("div", {}, "Link: ", h("a", { href: j.link, target: "_blank", rel: "noreferrer noopener" }, j.link)),
      reviewControls(j), confirmControls(j),
      h("div", { class: "row" },
        isActive(j) && h("button", { class: "btn danger", onclick: guard(async () => { if (confirm("Cancel this job?")) { await api("POST", `/api/jobs/${j.id}/cancel`, {}); await refreshJobs(); } }) }, "Cancel job"),
        !isActive(j) && h("button", { class: "btn", onclick: guard(async () => { await api("POST", `/api/jobs/${j.id}/retry`, {}); await refreshJobs(); }) }, "Run again"),
        !isActive(j) && h("button", { class: "btn", onclick: guard(async () => { await api("DELETE", `/api/jobs/${j.id}`); state.openJob = null; await refreshJobs(); }) }, "Remove from list")),
      jobsUI.gallery && h("div", {}, h("div", { class: "spacer" }), jobsUI.gallery.el),
      h("h2", { style: "margin-top:14px" }, "Log"), jobsUI.logEl));
    jobsUI.detail.scrollTop = keepTop;
    jobsUI.lastDetail = j.id;
  }
  if (jobsUI.gallery && (rerender || j.regenerating || ["frames", "review"].includes(j.stage))) jobsUI.gallery.refresh();
  pullLog(j);
}

function reviewControls(j) {
  if (j.status !== "awaiting_review") return null;
  return h("div", { class: "notice" },
    h("strong", {}, "Review the screenshots. "),
    "Either delete the ones you don't want and continue with what remains, or switch the gallery below to ‘Pick to keep’, tick the ones you want and the rest are deleted automatically.",
    h("div", { class: "spacer" }),
    h("div", { class: "row" },
      h("button", { class: "btn primary", disabled: j.regenerating, onclick: guard(async () => { await api("POST", `/api/jobs/${j.id}/proceed`, {}); await refreshJobs(); }) }, "Continue with remaining images"),
      h("button", { class: "btn", disabled: j.regenerating, onclick: guard(async () => { await api("POST", `/api/jobs/${j.id}/regenerate`, {}); toast("Regenerating frames…"); await refreshJobs(); }) }, j.regenerating ? "Regenerating…" : "Regenerate all frames")));
}

const WHOLE_FIELDS = {
  category_id: "Category", type_id: "Type", resolution_id: "Resolution", tmdb: "TMDB id", imdb: "IMDb id", tvdb: "TVDB id",
  season_number: "Season", episode_number: "Episode",
};

// Pre-upload check: the summary the script printed plus a form to change anything before it is sent.
// Edits live in planDrafts so polling, a failed upload or an invalid edit never throws them away.
function confirmControls(j) {
  if (j.status !== "awaiting_confirm") return null;
  const pf = j.plan_fields || {};
  const summary = h("div", { class: "plan" }, j.plan.join("\n"));
  const post = (body) => guard(async () => { await api("POST", `/api/jobs/${j.id}/confirm`, body); await refreshJobs(); });
  const cancel = h("button", { class: "btn", onclick: post({ go: false }) }, "Cancel upload");
  if (!pf.fields) {   // no editable data (should not happen): fall back to a plain confirmation
    return h("div", { class: "notice" }, h("strong", {}, "Ready to upload to the tracker"), summary,
      j.error && h("div", { class: "err small" }, "Previous attempt: " + j.error),
      h("div", { class: "row" }, h("button", { class: "btn primary", onclick: post({ go: true }) }, "Upload now"), cancel));
  }
  if (!planDrafts.has(j.id)) planDrafts.set(j.id, { ...pf.fields });
  const d = planDrafts.get(j.id), base = pf.fields, ch = pf.choices || {};
  const edited = () => Object.keys(d).filter((k) => d[k] !== base[k]);

  const text = (key, label, cls = "") => h("div", { class: "f " + cls }, h("label", {}, label),
    h("input", { type: "text", value: d[key] ?? "", oninput: (e) => { d[key] = e.target.value; } }));
  const whole = (key, label, blankZero) => h("div", { class: "f" }, h("label", {}, label),
    h("input", { type: "number", min: 0, step: 1, value: blankZero && !d[key] ? "" : String(d[key] ?? ""),
      oninput: (e) => { d[key] = e.target.value === "" ? 0 : Number(e.target.value); } }));
  const pick = (key, label, options) => {
    const opts = options.some(([id]) => id === d[key]) ? options : [...options, [d[key], "custom"]];
    const sel = h("select", { onchange: (e) => { d[key] = Number(e.target.value); } },
      opts.map(([id, name]) => h("option", { value: String(id) }, `${name} (id ${id})`)));
    sel.value = String(d[key]);
    return h("div", { class: "f" }, h("label", {}, label), sel);
  };
  const flag = (key, label) => h("label", { class: "chk" },
    h("input", { type: "checkbox", checked: !!d[key], onchange: (e) => { d[key] = e.target.checked ? 1 : 0; } }), label);

  const form = h("div", { class: "editform" },
    text("name", "Name", "wide"),
    pick("category_id", "Category", ch.categories || []), pick("type_id", "Type", ch.types || []), pick("resolution_id", "Resolution", ch.resolutions || []),
    whole("tmdb", "TMDB id"), whole("imdb", "IMDb id (blank = none)", true), whole("tvdb", "TVDB id (blank = none)", true),
    "season_number" in d && whole("season_number", "Season"), "episode_number" in d && whole("episode_number", "Episode"),
    h("div", { class: "wide row" }, flag("anonymous", "Anonymous"), flag("personal_release", "Personal release"), flag("stream", "Stream optimised"), flag("sd", "SD")),
    h("div", { class: "wide" }, promoControls(d)),
    h("div", { class: "f wide" }, h("label", {}, "Description"),
      h("textarea", { rows: 8, value: d.description ?? "", oninput: (e) => { d.description = e.target.value; } })));

  const upload = guard(async () => {
    checkPromo(d);
    for (const [k, label] of Object.entries(WHOLE_FIELDS)) {
      if (k in d && !Number.isInteger(d[k])) throw new Error(`${label} must be a whole number`);
    }
    const fields = Object.fromEntries(edited().map((k) => [k, d[k]]));
    await api("POST", `/api/jobs/${j.id}/confirm`, { go: true, fields });
    await refreshJobs();
  });
  return h("div", { class: "notice" },
    h("strong", {}, "Ready to upload to the tracker"),
    summary,
    h("div", { class: "muted small" }, "Change anything below before uploading; only the fields you edit are changed."),
    form,
    j.error && h("div", { class: "err small" }, "Previous attempt: " + j.error),
    h("div", { class: "row" },
      h("button", { class: "btn primary", onclick: upload }, "Upload now"),
      h("button", { class: "btn", title: "Throw away your edits and go back to the detected values", onclick: () => { planDrafts.delete(j.id); drawJobDetail(true); } }, "Reset edits"),
      cancel));
}

async function pullLog(j) {
  if (jobsUI.logJob !== j.id || j.log_count === jobsUI.logCount) return;
  const { lines } = await api("GET", `/api/jobs/${j.id}/log?since=${jobsUI.logCount}`).catch(() => ({ lines: [] }));
  if (!lines.length || jobsUI.logJob !== j.id) return;
  const el = jobsUI.logEl, atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
  el.append(lines.join("\n") + "\n");
  jobsUI.logCount += lines.length;
  if (atEnd) el.scrollTop = el.scrollHeight;
}

async function refreshJobs() {
  const r = await api("GET", "/api/jobs");
  state.jobs = r.jobs; state.maxConc = r.max_concurrent;
  for (const id of [...planDrafts.keys()]) {   // drafts only matter while the job can still ask for confirmation
    const job = r.jobs.find((x) => x.id === id);
    if (!job || !isActive(job)) planDrafts.delete(id);
  }
  const n = r.jobs.filter(isActive).length, w = r.jobs.filter((j) => j.status.startsWith("awaiting")).length;
  const b = $("#jobs-badge"); b.hidden = !n; b.textContent = w ? `${w} waiting` : n;
  if (state.tab === "jobs") {
    if (!document.activeElement || document.activeElement.type !== "number") drawJobsToolbar();
    drawJobsList(); drawJobDetail(false);
  }
}

/* ---------------- LIBRARY tab ---------------- */
const lib = { root: h("div") };
const libLoad = guard(async () => {
  if (state.lib) return libOpen(state.lib);
  const { outputs } = await api("GET", "/api/outputs");
  lib.root.replaceChildren(h("div", { class: "row" }, h("h2", { class: "grow" }, "Generated screenshots"), h("button", { class: "btn", onclick: libLoad }, "Refresh")),
    outputs.length ? h("div", { class: "grid outputs" }, outputs.map((o) => h("div", { class: "card", onclick: () => { state.lib = o.name; libLoad(); } },
      o.cover ? h("img", { loading: "lazy", src: `/api/outputs/${enc(o.name)}/thumb/${enc(o.cover)}`, alt: "" }) : h("div", { class: "empty" }, "no images"),
      h("div", { class: "title", title: o.name }, o.name.replace(/^UPLOAD\./, "")),
      h("div", { class: "cap" }, h("span", {}, `${o.images} image(s)${o.job ? " · active" : ""}`), h("span", {}, fmtTime(o.mtime)))))) : h("div", { class: "empty" }, "Nothing generated yet."));
});

function libOpen(name) {
  const g = Gallery(name);
  lib.root.replaceChildren(h("div", { class: "row" },
    h("button", { class: "btn", onclick: () => { state.lib = null; libLoad(); } }, "← All outputs"),
    h("h2", { class: "grow", style: "margin:0" }, name.replace(/^UPLOAD\./, "")),
    h("button", { class: "btn danger", onclick: guard(async () => {
      if (!confirm(`Delete the whole folder ${name} (images, .torrent, .nfo)?`)) return;
      await api("DELETE", `/api/outputs/${enc(name)}`); state.lib = null; libLoad();
    }) }, "Delete folder")), g.el);
}

/* ---------------- SETTINGS tab ---------------- */
const NUM_RANGE = {
  screenshot_count: [1, 200], screenshot_start_seconds: [0, 86400],
  screenshot_interval_seconds: [1, 86400], tracker_search_rate_limit: [1, 600],
};

const settingsLoad = guard(async () => {
  const c = state.config = await api("GET", "/api/config");
  const draft = { secrets: {} };
  const bool = (k, label) => h("label", { class: "chk" }, h("input", { type: "checkbox", checked: !!c[k], onchange: (e) => (draft[k] = e.target.checked) }), label);
  const field = (k, label, type = "text", hint = "") => h("div", { class: "f" }, h("label", {}, label),
    h("input", { type, value: c[k] ?? "", ...(type === "number" ? { min: NUM_RANGE[k][0], max: NUM_RANGE[k][1], step: 1 } : {}),
      oninput: (e) => { if (type === "number") { if (e.target.value !== "") draft[k] = Number(e.target.value); } else draft[k] = e.target.value; } }),
    hint && h("div", { class: "muted small" }, hint));
  const secret = (k, label) => h("div", { class: "f" }, h("label", {}, label, " ", h("span", { class: "muted small" }, c.secrets[k] ? "(set – leave blank to keep)" : "(not set)")),
    h("input", { type: "password", autocomplete: "off", placeholder: c.secrets[k] ? "••••••••" : "", oninput: (e) => (draft.secrets[k] = e.target.value) }));
  const client = c.download_client || { type: "", url: "", username: "", category: "", tags: "", save_path: "", download_dir: "", password_set: false };
  const clientDraft = { ...client, password: "" };
  const clientField = (k, label, placeholder = "") => h("div", { class: "f" }, h("label", {}, label), h("input", { type: k === "password" ? "password" : "text", autocomplete: "off", value: k === "password" ? "" : (clientDraft[k] || ""), placeholder: k === "password" && client.password_set ? "(set – leave blank to keep)" : placeholder,
    oninput: (e) => (clientDraft[k] = e.target.value) }));
  const clientType = h("select", { onchange: (e) => { clientDraft.type = e.target.value; } },
    h("option", { value: "" }, "Disabled (download torrent to output folder)"), h("option", { value: "qbittorrent" }, "qBittorrent"), h("option", { value: "transmission" }, "Transmission"));
  clientType.value = clientDraft.type || "";
  const clientFields = h("div", { class: "subform" }, clientType,
    clientField("url", "Web/API URL", "http://localhost:8080"), clientField("username", "Username"), clientField("password", "Password"),
    clientField("category", "qBittorrent category (optional)"), clientField("tags", "qBittorrent tags (optional)"),
    clientField("save_path", "qBittorrent save path (optional)"), clientField("download_dir", "Transmission download directory (optional)"));
  const save = async () => {
    if (clientDraft.type) draft.download_client = clientDraft;
    else draft.download_client = null;
    await api("PUT", "/api/config", draft); toast("Saved"); files.opts = null; state.dupOpts = null; settingsLoad();
  };
  $("#tab-settings").replaceChildren(h("div", { class: "panel form" },
    h("h2", {}, "Tracker and uploader settings"),
    secret("announce_url", "Announce URL"), secret("tmdb_api_key", "TMDB API key"), secret("tracker_api_token", "Tracker API token"), secret("upload_endpoint", "Upload endpoint"),
    field("tracker_search_endpoint", "Tracker search endpoint (optional)", "text", "Leave blank to derive the UNIT3D /api/torrents/filter endpoint from the upload endpoint."),
    field("tracker_search_rate_limit", "Tracker search rate limit (requests per minute)", "number", "Used by duplicate scans; every page of results counts as one request. Default 30. Lower it if the tracker still answers 429 (the scan also backs off and retries by itself). Applies immediately, even to a scan in progress."),
    bool("confirm_before_upload", "Confirm before upload"), bool("anonymous_upload", "Anonymous upload"), bool("personal_release", "Personal release"), bool("stream_optimized", "Stream optimised"),
    field("group_separator", "Group separator"),
    h("h2", {}, "Screenshot generation"),
    field("screenshot_count", "Number of screenshots", "number", "Total screenshots generated per release. For TV packs, this is distributed across episodes. Can be overridden per batch when adding jobs."),
    field("screenshot_start_seconds", "First screenshot time (seconds)", "number", "Used for single-video releases; e.g. 60 means 1:00."),
    field("screenshot_interval_seconds", "Screenshot interval (seconds)", "number", "Used for single-video releases; e.g. 240 means every 4 minutes."),
    h("h2", {}, "Download client"),
    h("p", { class: "muted small" }, "After a successful tracker upload, send the returned torrent URL to a download client. Disable this to download the .torrent into the generated output folder. Category and save path can be overridden per batch when adding jobs."),
    clientFields,
    h("div", { class: "muted small" }, "Secrets are never sent back to the browser. Run-as-user is not used by the web UI; jobs run as the user running the server."),
    h("div", {}, h("button", { class: "btn primary", onclick: guard(save) }, "Save settings"))));
});

/* ---------------- tabs / boot ---------------- */
function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  for (const t of ["files", "jobs", "duplicates", "library", "settings"]) $("#tab-" + t).hidden = t !== tab;
  if (tab === "jobs") { drawJobsToolbar(); drawJobsList(); drawJobDetail(true); refreshJobs(); }
  if (tab === "duplicates") buildDuplicates();
  if (tab === "library") libLoad();
  if (tab === "settings") settingsLoad();
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));

(async function boot() {
  // The sticky job panel sits just below the sticky header, whose height changes when it wraps.
  const header = document.querySelector("header");
  const syncHeader = () => document.documentElement.style.setProperty("--header-h", header.offsetHeight + "px");
  syncHeader();
  if (window.ResizeObserver) new ResizeObserver(syncHeader).observe(header);
  else window.addEventListener("resize", syncHeader);
  $("#tab-library").append(lib.root);
  buildJobs();
  await api("GET", "/api/config").then((c) => (state.config = c)).catch(() => {});
  buildFiles();
  api("GET", "/api/tools").then((t) => {
    const missing = Object.entries(t).filter(([, ok]) => !ok).map(([n]) => n);
    if (missing.length) $("#tools").textContent = "Missing on server: " + missing.join(", ");
  }).catch(() => {});
  resumeDuplicateScan().catch(() => {});
  refreshJobs().catch(() => {});
  setInterval(() => refreshJobs().catch(() => {}), 1500);
})();
