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
  jobs: [], maxConc: 1, openJob: null, config: null, lib: null, duplicates: { path: "", results: [], selected: new Set(), scanned: false },
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

/* ---------------- gallery (used by job detail and library) ---------------- */
function Gallery(dir, { editable = true } = {}) {
  const sel = new Set();
  let images = [], sig = "";
  const root = h("div");
  const actions = h("div", { class: "row" });
  const grid = h("div", { class: "grid" });
  root.append(actions, grid);

  async function del(names) {
    await api("DELETE", `/api/outputs/${enc(dir)}/images`, { names });
    names.forEach((n) => sel.delete(n));
    await refresh(true);
  }
  function draw() {
    actions.replaceChildren(
      h("span", { class: "muted" }, `${images.length} image${images.length === 1 ? "" : "s"}`),
      editable && h("button", { class: "btn small", onclick: () => { images.forEach((i) => sel.add(i.name)); draw(); } }, "Select all"),
      editable && h("button", { class: "btn small", onclick: () => { sel.clear(); draw(); } }, "Clear"),
      editable && h("button", {
        class: "btn danger small", disabled: !sel.size,
        onclick: guard(async () => { if (confirm(`Delete ${sel.size} image(s)? This cannot be undone.`)) await del([...sel]); }),
      }, `Delete selected (${sel.size})`));
    grid.replaceChildren(...images.map((img, idx) => h("div", { class: "card" + (sel.has(img.name) ? " sel" : "") },
      editable && h("input", { type: "checkbox", checked: sel.has(img.name), onchange: (e) => { e.target.checked ? sel.add(img.name) : sel.delete(img.name); draw(); } }),
      h("img", { loading: "lazy", src: `/api/outputs/${enc(dir)}/thumb/${enc(img.name)}?v=${img.mtime}`, alt: img.name,
        onclick: () => openLightbox(dir, images, idx, editable ? (names) => del(names) : null) }),
      h("div", { class: "cap" }, h("span", {}, img.name.replace(/^.*_frame_/, "frame ").replace(".png", "")), h("span", {}, fmtSize(img.size))))));
    if (!images.length) grid.replaceChildren(h("div", { class: "empty" }, "No images"));
  }
  async function refresh(force) {
    const { images: list } = await api("GET", `/api/outputs/${enc(dir)}/images`);
    const s = list.map((i) => i.name + i.mtime).join("|");
    images = list;
    for (const n of [...sel]) if (!list.some((i) => i.name === n)) sel.delete(n);
    if (force || s !== sig) { sig = s; draw(); }
  }
  refresh(true).catch((e) => toast(e.message, true));
  return { el: root, refresh: () => refresh(false).catch(() => {}) };
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

function cfgDefaults() {
  const c = state.config || {};
  return { review: true, auto_upload: c.confirm_before_upload === false, anonymous_upload: !!c.anonymous_upload, personal_release: !!c.personal_release, stream_optimized: !!c.stream_optimized };
}

function drawTray() {
  files.opts = files.opts || cfgDefaults();
  const o = files.opts;
  const opt = (key, label, hint) => h("label", { class: "chk", title: hint || "" }, h("input", { type: "checkbox", checked: o[key], onchange: (e) => { o[key] = e.target.checked; drawTray(); } }), label);
  const n = state.selected.size;
  files.tray.replaceChildren(
    h("h2", {}, `Selection (${n})`),
    n ? h("ul", {}, [...state.selected].map(([p]) => h("li", {},
      h("button", { class: "btn small", title: "Remove", onclick: () => { state.selected.delete(p); drawList(); drawTray(); } }, "✕"),
      h("span", { title: p }, "‎" + p)))) : h("p", { class: "muted" }, "Tick files or folders (a video file, or a release folder) to run the script on."),
    ...[...state.selected.keys()].filter((p) => files.inspect[p]).map((p) => h("div", { class: "insp" }, describe(p, files.inspect[p]))),
    h("div", { class: "opts" },
      opt("review", "Pause to review images before they are hosted", "Matches the CLI's ‘delete unwanted images’ pause"),
      opt("auto_upload", "Upload to tracker without asking", "Skips the final confirmation"),
      opt("anonymous_upload", "Anonymous upload"), opt("personal_release", "Personal release"), opt("stream_optimized", "Stream optimised")),
    (!o.review && o.auto_upload) && h("div", { class: "notice small" }, "Fully unattended: images and the release go out without any prompt."),
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
  const r = await api("POST", "/api/jobs", { paths: [...state.selected.keys()], options: files.opts });
  toast(`Queued ${r.created.length}` + (r.skipped.length ? `, skipped ${r.skipped.length} already active` : ""));
  state.selected.clear(); files.inspect = {}; drawList(); drawTray();
  switchTab("jobs");
}

/* ---------------- DUPLICATES tab ---------------- */
const duplicatesUI = { root: h("div"), path: null, results: h("div"), summary: h("div") };

function buildDuplicates() {
  const d = state.duplicates;
  duplicatesUI.path = h("input", { type: "text", class: "grow", placeholder: "/path/to/qBittorrent/downloads", value: d.path,
    oninput: (e) => { d.path = e.target.value; } });
  duplicatesUI.root.replaceChildren(
    h("div", { class: "panel" },
      h("h2", {}, "Find tracker duplicates"),
      h("p", { class: "muted small" }, "Select a qBittorrent-style download directory. The scanner checks each immediate subfolder as one release and each top-level MKV/MP4 file individually; it does not descend into loose files or treat files inside folders separately."),
      h("div", { class: "row" }, duplicatesUI.path,
        h("button", { class: "btn", onclick: guard(openDirectoryPicker) }, "Browse"),
        h("button", { class: "btn primary", onclick: guard(runDuplicateScan) }, "Scan tracker")),
      h("div", { class: "muted small", style: "margin-top:8px" }, "A search error stops the scan. Items are only marked unmatched after a successful tracker response.")),
    h("div", { class: "spacer" }), duplicatesUI.summary, duplicatesUI.results);
  $("#tab-duplicates").replaceChildren(duplicatesUI.root);
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
  const path = dpath(state.duplicates.path);
  if (!path) throw new Error("Choose a download directory first");
  duplicatesUI.summary.textContent = "Scanning tracker…";
  duplicatesUI.results.replaceChildren();
  const result = await api("POST", "/api/duplicates/scan", { path });
  const d = state.duplicates;
  d.results = result.results; d.selected = new Set(result.results.filter((x) => x.status === "unmatched").map((x) => x.path)); d.scanned = true;
  drawDuplicateResults(result);
}

function drawDuplicateResults(scan) {
  const d = state.duplicates;
  const unmatched = scan.results.filter((x) => x.status === "unmatched");
  const matched = scan.results.filter((x) => x.status === "matched");
  const skipped = scan.results.filter((x) => x.status === "skipped");
  duplicatesUI.summary.replaceChildren(h("div", { class: "row" },
    h("strong", {}, `Scanned ${scan.scanned} item(s)`),
    h("span", { class: "muted small" }, `${matched.length} with tracker matches · ${unmatched.length} unmatched · ${skipped.length} skipped`)));
  const unmatchedPanel = h("div", { class: "panel" },
    h("div", { class: "row" }, h("h2", { class: "grow" }, `No tracker matches (${unmatched.length})`),
      h("button", { class: "btn small", onclick: () => { d.selected = new Set(unmatched.map((x) => x.path)); drawDuplicateResults(scan); } }, "Select all"),
      h("button", { class: "btn small", onclick: () => { d.selected.clear(); drawDuplicateResults(scan); } }, "Clear selection")),
    unmatched.length ? h("div", { class: "dup-list" }, unmatched.map((item) => h("label", { class: "dup-item" },
      h("input", { type: "checkbox", checked: d.selected.has(item.path), onchange: (e) => { e.target.checked ? d.selected.add(item.path) : d.selected.delete(item.path); drawDuplicateResults(scan); } }),
      h("span", { class: "grow" }, h("strong", {}, item.name), h("span", { class: "muted small" }, `${item.kind} · search: ${item.query}`))))) : h("div", { class: "empty" }, "No unmatched items to add."),
    h("div", { class: "row" }, h("span", { class: "muted small grow" }, `${d.selected.size} selected`),
      h("button", { class: "btn primary", disabled: !d.selected.size, onclick: guard(async () => {
        const r = await api("POST", "/api/jobs", { paths: [...d.selected], options: cfgDefaults() });
        toast(`Queued ${r.created.length}` + (r.skipped.length ? `, skipped ${r.skipped.length} already active` : ""));
        d.selected.clear(); switchTab("jobs");
      }) }, `Add selected as batch (${d.selected.size})`)));
  const matchedRows = matched.map((item) => {
    const lines = item.matches.slice(0, 5).map((m) => {
      const meta = [m.resolution, m.type, m.seeders == null ? "" : `${m.seeders} seeders`].filter(Boolean);
      return h("span", { class: "match-line" }, m.name + (meta.length ? " · " + meta.join(" · ") : ""));
    });
    return h("div", { class: "dup-item matched" }, h("span", { class: "grow" },
      h("strong", {}, item.name), h("span", { class: "muted small" }, `Search: ${item.query}`), ...lines));
  });
  const matchedPanel = h("div", { class: "panel" }, h("h2", {}, `Possible duplicates (${matched.length})`),
    matched.length ? h("div", { class: "dup-list" }, matchedRows) : h("div", { class: "empty" }, "No tracker matches were found."));
  const skippedPanel = skipped.length ? h("div", { class: "panel" }, h("h2", {}, `Skipped (${skipped.length})`),
    h("p", { class: "muted small" }, "These folders contain no supported MKV/MP4 video, so they cannot be queued."),
    ...skipped.map((item) => h("div", { class: "muted small" }, item.name))) : null;
  duplicatesUI.results.replaceChildren(unmatchedPanel, h("div", { class: "spacer" }), matchedPanel, skippedPanel);
}

/* ---------------- JOBS tab ---------------- */
const jobsUI = { list: h("div"), detail: h("div"), toolbar: h("div", { class: "row" }), logEl: null, logCount: 0, logJob: null, gallery: null, galleryDir: null, sig: "" };
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

function drawJobDetail(force) {
  const j = state.jobs.find((x) => x.id === state.openJob);
  if (!j) { jobsUI.detail.replaceChildren(h("div", { class: "panel muted" }, "Select a job to see progress, images and the log.")); jobsUI.logJob = null; jobsUI.galleryDir = null; return; }
  const sig = JSON.stringify([j.id, j.status, j.stage, j.regenerating, j.plan, j.error, j.link, Math.floor(j.percent / 5)]);
  const hasImages = j.out_dir && ["frames", "review", "hosting", "finalize", "confirm", "uploading", "finished", "failed", "cancelled"].includes(j.stage) || (j.out_dir && !isActive(j));
  if (force || sig !== jobsUI.sig || jobsUI.logJob !== j.id) {
    jobsUI.sig = sig;
    const cur = STAGE_IDX[j.stage] ?? -1;
    const done = j.status === "finished";
    if (jobsUI.galleryDir !== j.out_dir || force) {
      jobsUI.gallery = hasImages ? Gallery(j.out_dir, { editable: j.status === "awaiting_review" || !isActive(j) }) : null;
      jobsUI.galleryDir = j.out_dir;
    }
    jobsUI.logEl = h("pre", { class: "log" });
    jobsUI.logCount = 0; jobsUI.logJob = j.id;
    jobsUI.detail.replaceChildren(h("div", { class: "panel" },
      h("div", { class: "row" }, h("h2", { class: "grow" }, j.name || j.target), h("span", { class: "pill " + j.status }, STATUS_TEXT[j.status])),
      h("div", { class: "muted small", style: "word-break:break-all" }, j.target),
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
  }
  if (jobsUI.gallery && (j.status === "awaiting_review" || j.stage === "frames")) jobsUI.gallery.refresh();
  pullLog(j);
}

function reviewControls(j) {
  if (j.status !== "awaiting_review") return null;
  return h("div", { class: "notice" },
    h("strong", {}, "Review the screenshots. "), "Delete any you don't want; whatever remains is uploaded to the image host.",
    h("div", { class: "spacer" }),
    h("div", { class: "row" },
      h("button", { class: "btn primary", disabled: j.regenerating, onclick: guard(async () => { await api("POST", `/api/jobs/${j.id}/proceed`, {}); await refreshJobs(); }) }, "Continue with remaining images"),
      h("button", { class: "btn", disabled: j.regenerating, onclick: guard(async () => { await api("POST", `/api/jobs/${j.id}/regenerate`, {}); toast("Regenerating frames…"); await refreshJobs(); }) }, j.regenerating ? "Regenerating…" : "Regenerate all frames")));
}

function confirmControls(j) {
  if (j.status !== "awaiting_confirm") return null;
  return h("div", { class: "notice" },
    h("strong", {}, "Ready to upload to the tracker"),
    h("div", { class: "plan" }, j.plan.join("\n")),
    j.error && h("div", { class: "err small" }, "Previous attempt: " + j.error),
    h("div", { class: "row" },
      h("button", { class: "btn primary", onclick: guard(async () => { await api("POST", `/api/jobs/${j.id}/confirm`, { go: true }); await refreshJobs(); }) }, "Upload now"),
      h("button", { class: "btn", onclick: guard(async () => { await api("POST", `/api/jobs/${j.id}/confirm`, { go: false }); await refreshJobs(); }) }, "Cancel upload")));
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
const settingsLoad = guard(async () => {
  const c = state.config = await api("GET", "/api/config");
  const draft = { secrets: {} };
  const bool = (k, label) => h("label", { class: "chk" }, h("input", { type: "checkbox", checked: !!c[k], onchange: (e) => (draft[k] = e.target.checked) }), label);
  const field = (k, label, type = "text", hint = "") => h("div", { class: "f" }, h("label", {}, label),
    h("input", { type, value: c[k] ?? "", ...(type === "number" ? { min: k === "screenshot_start_seconds" ? 0 : 1, max: k === "screenshot_count" ? 200 : 86400, step: 1 } : {}),
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
    await api("PUT", "/api/config", draft); toast("Saved"); files.opts = null; settingsLoad();
  };
  $("#tab-settings").replaceChildren(h("div", { class: "panel form" },
    h("h2", {}, "Tracker and uploader settings"),
    secret("announce_url", "Announce URL"), secret("tmdb_api_key", "TMDB API key"), secret("tracker_api_token", "Tracker API token"), secret("upload_endpoint", "Upload endpoint"),
    field("tracker_search_endpoint", "Tracker search endpoint (optional)", "text", "Leave blank to derive the UNIT3D /api/torrents/filter endpoint from the upload endpoint."),
    bool("confirm_before_upload", "Confirm before upload"), bool("anonymous_upload", "Anonymous upload"), bool("personal_release", "Personal release"), bool("stream_optimized", "Stream optimised"),
    field("group_separator", "Group separator"),
    h("h2", {}, "Screenshot generation"),
    field("screenshot_count", "Number of screenshots", "number", "Total screenshots generated per release. For TV packs, this is distributed across episodes."),
    field("screenshot_start_seconds", "First screenshot time (seconds)", "number", "Used for single-video releases; e.g. 60 means 1:00."),
    field("screenshot_interval_seconds", "Screenshot interval (seconds)", "number", "Used for single-video releases; e.g. 240 means every 4 minutes."),
    h("h2", {}, "Download client"),
    h("p", { class: "muted small" }, "After a successful tracker upload, send the returned torrent URL to a download client. Disable this to download the .torrent into the generated output folder."),
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
  $("#tab-library").append(lib.root);
  buildJobs();
  await api("GET", "/api/config").then((c) => (state.config = c)).catch(() => {});
  buildFiles();
  api("GET", "/api/tools").then((t) => {
    const missing = Object.entries(t).filter(([, ok]) => !ok).map(([n]) => n);
    if (missing.length) $("#tools").textContent = "Missing on server: " + missing.join(", ");
  }).catch(() => {});
  refreshJobs().catch(() => {});
  setInterval(() => refreshJobs().catch(() => {}), 1500);
})();
