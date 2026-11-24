"use strict";
/* Mod Manager page: one state poll, three screens (home, builds, settings), and an
 * action bar with the status line and Play on every screen. */

// ---- core ---------------------------------------------------------------------------
const BUSY = new Set(["applying", "building", "starting", "running", "downloading", "removing"]);
const SCREENS = ["home", "builds", "settings"];
let state = null;          // last GET /api/state
let pollTimer = null;
let screen = "home";

const $ = (id) => document.getElementById(id);

/** fetch helper: every POST is JSON (the server refuses anything else). */
async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  let resp;
  try {
    resp = await fetch(path, opts);
  } catch (err) {
    return { ok: false, status: 0, data: { error: "The manager is not answering." } };
  }
  let data = null;
  try { data = await resp.json(); } catch (err) { data = {}; }
  return { ok: resp.ok, status: resp.status, data: data || {} };
}

/** h("div", {class: "x", onclick: f}, child, "text", ...) */
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "style" && typeof value === "object") Object.assign(el.style, value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

/** Replace a container's children, keeping keyboard focus on the same data-key. */
function replaceKeyed(container, nodes) {
  const key = document.activeElement && container.contains(document.activeElement)
    ? document.activeElement.dataset.key : null;
  container.replaceChildren(...nodes);
  if (key) {
    const again = container.querySelector(`[data-key="${CSS.escape(key)}"]`);
    if (again) again.focus();
  }
}

let toastTimer = null;
function toast(text, bad) {
  const el = $("toast");
  el.textContent = text;
  el.className = "toast" + (bad ? " bad" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 7000 : 3500);
}

// A mod's picture: the server's art_url (its own image file, /modart/<id>, or game art, /art/<NAME>);
// an older server sends only the game art name.
const artSrc = (mod) => {
  const url = mod && mod.art_url;
  if (typeof url === "string" && /^\/(art|modart)\/[A-Za-z0-9_\-]+$/.test(url)) return url;
  const name = mod && mod.art;
  return name && /^[A-Z0-9_]+$/.test(name) ? `/art/${name}` : null;
};
const artUrl = (mod) => { const src = artSrc(mod); return src ? `url('${src}')` : "none"; };
/** A mod with no art gets its initials on the tile instead ("My Pitcher: Batter's Side" -> "MP", "Perks++" -> "P++"). */
const initialsOf = (mod) => {
  const words = String(mod.name).match(/[A-Za-z0-9]+/g) || ["?"];
  return words.slice(0, 2).map((w) => w[0].toUpperCase()).join("") + ((/\++$/.exec(mod.name) || [""])[0]);
};
const isBusy = () => !!state && BUSY.has(state.session.status);
/** [[branch, {head, files, mod}]] the last build left out (switched-off mods whose branch would not merge). */
const leftOut = () => Object.entries((state && state.build && state.build.left_out) || {});
const leftOutMods = (v) => (v && (v.mods || (v.mod ? [v.mod] : []))) || [];
const leftOutOf = (modId) => leftOut().find(([, v]) => leftOutMods(v).includes(modId));
/** Why a branch was left out, in a few words. */
function leftOutWhy(v) {
  if (v && v.files && v.files.length) return "conflicts in " + v.files.join(", ");
  if (v && v.reason === "branch not found") return "was not found";
  return "did not merge" + (v && v.reason ? " (" + v.reason + ")" : "");
}
const modNameOf = (id) => ((state.mods || []).find((m) => m.id === id) || {}).name || id;
const gamesText = (games) => games.map((g) => `PID ${g.pid}${g.exe ? ` (${g.exe})` : ""}`).join(", ");

async function poll() {
  clearTimeout(pollTimer);
  const res = await api("/api/state");
  if (res.ok) {
    state = res.data;
    render();
  } else {
    showStateError(res.status === 0 ? "The manager is not answering."
      : res.data.error || `Manager error ${res.status}`);
  }
  pollTimer = setTimeout(poll, !res.ok ? 2000 : isBusy() ? 1000 : 5000);
}

/** /api/state failed: say why, and never leave a stale "Ready" Play clickable. */
function showStateError(text) {
  setStatus(text, "error");
  $("progress").hidden = true;
  const btn = $("play");
  if (document.activeElement === btn) playHadFocus = true;
  btn.disabled = true;
  btn.classList.remove("ready", "busy");
  $("play-label").textContent = "PLAY";
  btn.title = "The manager cannot read its state: " + text;
  $("play-reason").textContent = btn.title;
}

function render() {
  watchSession();
  renderHome();
  renderPlay();
  renderStatus();
  if (screen === "builds") renderBuilds();
  if (screen === "settings") renderSettingsSide();
  $("hero-profile").textContent = state.profile ? "PROFILE · " + baseName(state.profile) : "";
  $("hero-profile").hidden = !state.profile;
  $("hero-profile").title = state.profile ? "Plays on " + state.profile : "";
}

const baseName = (p) => String(p).split(/[\\/]/).filter(Boolean).pop() || p;

// ---- router + keyboard -------------------------------------------------------------
function route() {
  const want = location.hash.replace("#", "");
  const was = screen;
  screen = SCREENS.includes(want) ? want : "home";
  if (screen !== was) $("main").scrollTop = 0;
  for (const name of SCREENS) $("screen-" + name).hidden = name !== screen;
  for (const pill of document.querySelectorAll("#nav .pill")) {
    pill.classList.toggle("active", pill.dataset.screen === screen);
  }
  if (screen === "settings") loadSettings();
  if (screen === "builds") pollLog(true); else stopLog();
  if (state) render();
}

/** Focusable things on the visible screen (+ the nav), for arrow-key movement. */
function focusables() {
  const root = $("screen-" + screen);
  // the mod list is one stop (its focused row); Up/Down inside it are handled by rowKey
  const sel = "button:not(:disabled):not([tabindex='-1']), a.pill, input, pre.log, .mod-row[tabindex='0']";
  return [...document.querySelectorAll("#nav .pill"), ...root.querySelectorAll(sel),
    ...document.querySelectorAll(".actionbar button:not(:disabled)")]
    .filter((el) => el.offsetParent !== null);
}

function moveFocus(dir) {
  const items = focusables();
  const cur = document.activeElement;
  if (!items.includes(cur)) {
    (items.find((el) => el.classList.contains("mod-row")) || items[0])?.focus();
    return;
  }
  const a = cur.getBoundingClientRect();
  const ax = a.left + a.width / 2, ay = a.top + a.height / 2;
  let best = null, bestScore = Infinity;
  for (const el of items) {
    if (el === cur) continue;
    const b = el.getBoundingClientRect();
    const dx = b.left + b.width / 2 - ax, dy = b.top + b.height / 2 - ay;
    const main = { left: -dx, right: dx, up: -dy, down: dy }[dir];
    if (main <= 4) continue;
    const cross = dir === "left" || dir === "right" ? Math.abs(dy) : Math.abs(dx);
    const score = main + cross * 2;
    if (score < bestScore) { bestScore = score; best = el; }
  }
  if (best) best.focus();
}

document.addEventListener("keydown", (ev) => {
  const typing = ev.target.matches("input[type=text], input[type=number]");
  if (ev.key === "Escape") {
    if (typing) ev.target.blur();   // first Escape leaves the field (keeps unsaved edits)
    else location.hash = "#home";
    return;
  }
  if (typing || ev.altKey || ev.ctrlKey || ev.metaKey) return;
  const dir = { ArrowLeft: "left", ArrowRight: "right", ArrowUp: "up", ArrowDown: "down" }[ev.key];
  if (dir && !(ev.target.matches("pre.log") && (dir === "up" || dir === "down"))) {
    ev.preventDefault();
    moveFocus(dir);
  }
});

window.addEventListener("hashchange", route);
$("refresh").addEventListener("click", () => location.reload());   // the F5 key, for keyboards without one

for (const btn of document.querySelectorAll("[data-open]")) {
  btn.addEventListener("click", () => openFolder(btn.dataset.open));
}

async function openFolder(what) {
  const res = await api("/api/open", { what });
  if (!res.ok) toast(res.data.error || "Cannot open that folder.", true);
}

// ---- HOME: the mod list (left) and the focused mod's card (right) --------------------
const FOCUS_KEY = "modmanager.focus";
let focusId = null;        // the mod shown in the detail card; kept across polls
try { focusId = localStorage.getItem(FOCUS_KEY); } catch (err) { focusId = null; }
let recheckBusy = false;   // RE-CHECK INSTALLED is running

const rowOf = (id) => $("mod-list").querySelector(`[data-key="${CSS.escape("row:" + id)}"]`);

/** Show mod `id` in the detail card; `moveDom` also moves keyboard focus to its row. */
function setFocus(id, moveDom) {
  focusId = id;
  try { localStorage.setItem(FOCUS_KEY, id); } catch (err) { /* storage blocked: memory only */ }
  if (state) renderHome();
  if (moveDom) {
    const row = rowOf(id);
    if (row) { row.focus(); row.scrollIntoView({ block: "nearest" }); }
  }
}

async function toggleMod(mod) {
  if (isBusy()) { toast("Wait for the current Play to finish before changing mods.", true); return; }
  const res = await api("/api/select", { id: mod.id, on: !mod.on });
  if (!res.ok) toast(res.data.error || "Could not change the selection.", true);
  poll();
}

/** Chip labels of a mod's parts, by the part names a release's list uses. */
const PART_LABELS = { files: "GAME FILES", runtime: "CODE", patches: "CODE PATCHES", server: "SERVER",
  profile: "OWN PROFILE" };
/** The parts a mod has, as chip labels. */
function partsOf(mod) {
  return [["files", mod.has_files], ["runtime", mod.has_runtime], ["patches", mod.has_patches],
    ["server", mod.has_server], ["profile", mod.own_profile]].filter(([, has]) => has).map(([p]) => PART_LABELS[p]);
}

/** Small state badges for a list row: [[text, kind, why]]. */
function badgesOf(mod) {
  const out = [];
  if (mod.has_files) {
    if (mod.redo) out.push(["INTERRUPTED", "warn", "The last Play stopped half-way: the next Play redoes it."]);
    else if (mod.status_error) out.push(["STATUS CHECK FAILED", "warn", mod.status_error]);
    else if (mod.installed === true) out.push(["INSTALLED ✓", "ok", "Its game files are installed."]);
    else if (mod.installed === false) {
      out.push(["NOT INSTALLED", "", mod.on ? "The next Play installs it." : "The game has its original files."]);
    }
  }
  const lo = leftOutOf(mod.id);
  if (lo) out.push(["LEFT OUT ⚠", "warn", "Left out of the last build: its branch " + leftOutWhy(lo[1])]);
  return out;
}

/** A list row's square picture, or the mod's initials when it has none. */
function thumbFor(mod) {
  return artUrl(mod) === "none"
    ? h("div", { class: "row-thumb initials", "aria-hidden": "true" }, h("span", {}, initialsOf(mod)))
    : h("div", { class: "row-thumb", style: { backgroundImage: artUrl(mod) } });
}

function rowFor(mod, focused) {
  const sw = h("button", {
    class: "switch" + (mod.on ? " on" : ""), type: "button", tabindex: "-1", role: "switch",
    "aria-checked": mod.on ? "true" : "false", "aria-label": (mod.on ? "Switch off " : "Switch on ") + mod.name,
    title: mod.on ? "On: click to switch it off" : "Off: click to switch it on",
    onmousedown: (ev) => ev.preventDefault(),   // a click toggles; it does not take focus
    onclick: (ev) => { ev.stopPropagation(); toggleMod(mod); },
  }, h("span", { class: "switch-text" }, mod.on ? "ON" : "OFF"), h("span", { class: "switch-knob" }));
  return h("div", {
    class: "mod-row" + (mod.on ? " on" : "") + (focused ? " focused" : ""),
    role: "option", "aria-selected": focused ? "true" : "false", tabindex: focused ? "0" : "-1",
    "data-key": "row:" + mod.id,
    onclick: () => setFocus(mod.id, true),
    onkeydown: (ev) => rowKey(ev, mod),
  },
  thumbFor(mod),
  h("div", { class: "row-main" },
    h("div", { class: "row-name" }, mod.name),
    h("div", { class: "row-desc", title: mod.description || null }, mod.description || ""),
    h("div", { class: "row-parts" },
      badgesOf(mod).map(([text, kind, why]) => h("span", { class: "badge-s " + kind, title: why || null }, text)),
      partsOf(mod).map((p) => h("span", { class: "part" }, p)))),
  sw);
}

/** Up/Down (Home/End) move between rows; Space/Enter switch the focused row on or off. */
function rowKey(ev, mod) {
  if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
  if (ev.key === " " || ev.key === "Enter") {
    ev.preventDefault();
    if (!ev.repeat) toggleMod(mod);
    return;
  }
  const ids = state.mods.map((m) => m.id);
  const at = ids.indexOf(mod.id);
  const to = { ArrowUp: at - 1, ArrowDown: at + 1, Home: 0, End: ids.length - 1 }[ev.key];
  if (to === undefined) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (to >= 0 && to < ids.length && to !== at) setFocus(ids[to], true);
}

function installedText(mod) {
  if (!mod.has_files) return ["no game files", ""];
  if (mod.redo) return ["interrupted — the next Play redoes it", "warn"];
  if (mod.status_error) return ["unknown — status check failed", "warn", mod.status_error];
  if (mod.installed === null) return ["checking…", ""];
  if (mod.installed) return mod.on ? ["installed (modded files)", "ok"]
    : ["installed (modded files) — the next Play restores the originals", "warn"];
  return ["original files" + (mod.on ? " — the next Play installs the mod" : ""), ""];
}

/** The code patch sets of one mod (from the build state), [] when unknown. */
const patchSetsOf = (modId) => ((state.build && state.build.patch_sets) || []).filter((p) => p.mod === modId);

/** The code line: [children, kind]. Whether the mod's branch is in the current exe. */
function codeLine(mod) {
  const b = state.build;
  const br = mod.branch;
  if (!br) return [["turns a game feature on or off; nothing to build"], ""];
  const name = [h("code", {}, br), " · "];
  const lo = leftOutOf(mod.id);
  if (lo) return [[...name, "left out of the current exe: its branch " + leftOutWhy(lo[1])], "warn"];
  if (!b || b.error) return [[...name, "build state unknown" + (b && b.error ? ": " + b.error : "")], "warn"];
  const built = b.built_from || {}, needed = b.needed || {};
  if (!b.exe || !br || !(br in built)) return [[...name, "not built yet — Play builds it"], ""];
  if (built[br] !== needed[br]) return [[...name, "changed since the last build — Play rebuilds"], "warn"];
  return [[...name, "in the current exe" + (mod.on ? "" : ", switched off while the mod is off")], "ok"];
}

/** The code patches line: every set with its switch, and whether the exe has the patch text. */
function patchLine(mod) {
  const sets = patchSetsOf(mod.id);
  const b = state.build || {};
  const stale = b.exe && b.built_from && b.needed && b.built_from.patches !== b.needed.patches;
  const items = sets.length
    ? sets.map((p) => h("span", { class: "set" }, p.name + " ", h("code", {}, `${p.env}=${mod.on ? 1 : 0}`)))
    : ["no switches"];
  return [[h("span", { class: "sets" }, items),
    stale ? h("span", { class: "dd-note warn" }, "The patch text changed since the build — Play rebuilds.") : null],
  ""];
}

function profileText(mod) {
  if (mod.own_profile) return mod.on && state.profile ? state.profile : "its own copy (profile-" + mod.id + ")";
  return (state.profile || "—") + (mod.on ? "" : " (when selected)");
}

/** "What it changes": one line per part, with its real state. */
function changeLines(mod) {
  const lines = [];
  const add = (label, children, kind, title) => lines.push(
    h("dt", {}, label), h("dd", { class: kind || null, title: title || null }, children));
  if (mod.has_files) {
    const [text, kind, why] = installedText(mod);
    add("Game files", [text, why ? h("pre", { class: "why" }, why) : null], kind);
  }
  if (mod.has_runtime) { const [c, k] = codeLine(mod); add("Code", c, k); }
  if (mod.has_patches) { const [c, k] = patchLine(mod); add("Code patches", c, k); }
  if (mod.has_server) add("Server", "starts its own local server for each Play, stopped when the game closes");
  add("Profile", profileText(mod), "", mod.own_profile
    ? "Plays on a copy of your normal profile, so your normal saves stay untouched." : "");
  return lines;
}

/** The note on a mod whose branch the last build left out, else null. */
function leftOutNote(mod) {
  const entry = leftOutOf(mod.id);
  if (!entry) return null;
  return h("p", { class: "mod-note warn" },
    "Left out of the last build: its branch " + leftOutWhy(entry[1]) +
    ". Switching it on rebuilds and shows the error until its branch is fixed.");
}

/** Text with its **bold** runs as <b> nodes (a feature file is text, never HTML). */
function richText(text) {
  return String(text).split(/\*\*(.+?)\*\*/).map((part, i) => (i % 2 ? h("b", {}, part) : part));
}

/** The mod's feature list (its features file, parsed by the manager): titled sections of bullets and notes. */
function featureList(mod) {
  if (!mod.features || !mod.features.length) return null;
  const out = [h("div", { class: "sub-head" }, "Features")];
  for (const section of mod.features) {
    if (section.title) out.push(h("div", { class: "feature-title" }, section.title));
    let list = null;
    for (const block of section.blocks) {
      if (block.para !== undefined) {
        list = null;
        out.push(h("p", { class: "feature-note" }, richText(block.para)));
        continue;
      }
      if (!list) out.push(list = h("ul", { class: "feature-items" }));
      list.append(h("li", {}, richText(block.bullet),
        block.sub.length ? h("ul", {}, block.sub.map((s) => h("li", {}, richText(s)))) : null));
    }
  }
  return h("div", { class: "features" }, out);
}

/* A title card drawn from the mod's card text ("card": a .json file), in the layout of tools/make_title_card.py:
 * every size and position below is that tool's, in pixels of its 1600x900 card, and the CSS turns one such pixel
 * (--u) into a share of the card's width. Text is measured on a canvas at those sizes, as the tool measures it, so a
 * name fits and wraps the same way at every window size. */
const CARD_FOOTER = "MLB The Show 26  |  Fastball 0.2  |  game 1.0.20";
const cardArtSizes = new Map();   // art path -> [width, height], so a redrawn card sizes its art at once
const cardArtFailed = new Set();  // art paths that did not load: their cards are drawn text-only at once
const cardU = (n) => `calc(var(--u) * ${n})`;
/* canvas fonts with the same family lists as the CSS of the parts they measure, so a missing font falls back alike */
const TC_NAME_FONTS = 'Bahnschrift, "Bahnschrift SemiBold Condensed", "Bahnschrift Condensed", "Arial Narrow", sans-serif';
const TC_TAG_FONTS = 'Bahnschrift, "Segoe UI", sans-serif';     // .tc-tagline, .tc-foot: Bahnschrift, var(--body)
const TC_TEXT_FONTS = '"Segoe UI", Bahnschrift, sans-serif';     // .tc-point, .tc-note: "Segoe UI", var(--body)
const nameFont = (size) => `700 ${size}px ${TC_NAME_FONTS}`;     // .tc-name: Bahnschrift, var(--head)
const tagFont = (size) => `600 ${size}px ${TC_TAG_FONTS}`;
const pointFont = (size) => `400 ${size}px ${TC_TEXT_FONTS}`;
let cardCanvas = null;
function textWidth(text, font) {
  cardCanvas = cardCanvas || document.createElement("canvas").getContext("2d");
  cardCanvas.font = font;
  return cardCanvas.measureText(text).width;
}

/** The largest size from `size` down to `floor` (steps of 2) at which `text` is at most `maxw` wide. */
function fitSize(text, fontAt, size, maxw, floor = 20) {
  for (let s = size; s >= floor; s -= 2) if (textWidth(text, fontAt(s)) <= maxw) return s;
  return floor;
}

/** How many lines `text` takes at `font` in `maxw`, broken as the CSS of .tc-name.wrap breaks it (at spaces, and
 *  inside a word only when the word alone is wider than the line). */
function wrappedLines(text, font, maxw) {
  let lines = 1, used = 0;
  const space = textWidth(" ", font);
  for (const word of String(text).split(/\s+/).filter(Boolean)) {
    const w = textWidth(word, font);
    if (used && used + space + w <= maxw) { used += space + w; continue; }
    if (used) { lines += 1; used = 0; }
    if (w <= maxw) { used = w; continue; }
    for (const ch of word) {   // a word wider than the line: broken anywhere
      const c = textWidth(ch, font);
      if (used + c > maxw && used) { lines += 1; used = 0; }
      used += c;
    }
  }
  return lines;
}

const NAME_FLOOR = 40;   // the smallest name size, for a single long word; below it the name wraps
/** The name in capitals as 1 line (from 150 down) or 2 evenly split lines (from 118 down) of [text, trailing + signs],
 * at the largest size that fits `maxw`; a name that fits neither way at 70 (one long word) goes on down to
 * NAME_FLOOR on one line, then wraps, with `count` the lines it takes. */
function cardTitle(name, maxw) {
  const upper = String(name).toUpperCase();
  const plus = (/\++$/.exec(upper) || [""])[0];
  const body = upper.slice(0, upper.length - plus.length);
  const words = body.split(/\s+/).filter(Boolean);
  const widthAt = (size) => (text, tail = "") =>
    textWidth(text, nameFont(size)) + (tail ? 8 + textWidth(tail, nameFont(size)) : 0);
  for (let size = 150; size >= 70; size -= 4) {
    const width = widthAt(size);
    if (width(body, plus) <= maxw) return { size, lines: [[body, plus]] };
    if (size > 118) continue;   // two lines only at a size that leaves room for the rest of the card
    let best = null;
    for (let cut = 1; cut < words.length; cut++) {
      const a = words.slice(0, cut).join(" "), b = words.slice(cut).join(" ");
      const widest = Math.max(width(a), width(b, plus));
      if (width(a) <= maxw && width(b, plus) <= maxw && (!best || widest < best[0])) best = [widest, a, b];
    }
    if (best) return { size, lines: [[best[1], ""], [best[2], plus]] };
  }
  for (let size = 66; size >= NAME_FLOOR; size -= 4) {
    if (widthAt(size)(body, plus) <= maxw) return { size, lines: [[body, plus]] };
  }
  return { size: NAME_FLOOR, lines: [[body, plus]], wrap: true,
    count: wrappedLines(body + (plus ? " " + plus : ""), nameFont(NAME_FLOOR), maxw) };
}

function titleCard(mod, withArt = true) {
  const text = mod.card_text;
  const wanted = withArt ? artSrc(mod) : null;
  const src = wanted && !cardArtFailed.has(wanted) ? wanted : null;
  const x = src ? 790 : 140, maxw = 1600 - x - 60;
  const at = (left, top, style) => ({ left: cardU(left), top: cardU(top), ...style });
  const card = h("div", { class: "title-card" + (src ? "" : " text-only") });
  const parts = [];
  if (src) {
    const art = h("img", {
      class: "tc-art-img", src, alt: "",
      onload: () => { cardArtSizes.set(src, [art.naturalWidth, art.naturalHeight]); sizeArt(); },
      onerror: () => {   // no picture in this install: the text alone, now and on every redraw
        cardArtFailed.add(src);
        card.replaceWith(titleCard(mod, false));
      },
    });
    const sizeArt = () => {   // shrunk to fit 600x600 at its own aspect ratio, never enlarged (the tool's thumbnail)
      const [w, h0] = cardArtSizes.get(src);
      const k = Math.min(1, 600 / w, 600 / h0);
      Object.assign(art.style, { width: cardU(w * k), height: cardU(h0 * k), visibility: "visible" });
    };
    if (cardArtSizes.has(src)) sizeArt();
    parts.push(h("div", { class: "tc-panel" }), h("div", { class: "tc-art" }, art));
  } else {
    parts.push(h("div", { class: "tc-bar" }));
  }

  const title = cardTitle(mod.name, maxw);
  let y = title.lines.length === 1 ? 150 : 100;
  for (const [line, plus] of title.lines) {
    parts.push(h("div", { class: "tc-name" + (title.wrap ? " wrap" : ""),
      style: at(x, y, { fontSize: cardU(title.size), maxWidth: title.wrap ? cardU(maxw) : null }) },
    line, plus ? h("span", { class: "tc-plus", style: { marginLeft: cardU(8) } }, plus) : null));
    y += Math.floor(title.size * 1.02);
  }
  if (title.wrap) y += (title.count - 1) * Math.ceil(title.size * 1.2);   // the lines a wrapped name takes after its first
  y += 20;
  parts.push(h("div", { class: "tc-tagline",
    style: at(x + 4, y, { fontSize: cardU(fitSize(text.tagline, tagFont, 44, maxw)) }) }, text.tagline));
  y += 100;
  for (const [head, rest] of text.highlights) {
    let size = 32;
    while (size > 22 && textWidth(`${head} - ${rest}`, pointFont(size)) + 40 > maxw) size -= 2;
    parts.push(h("div", { class: "tc-dot",
      style: at(x + 4, y + size * 0.45, { width: cardU(12), height: cardU(12) }) }));
    parts.push(h("div", { class: "tc-point", style: at(x + 36, y, { fontSize: cardU(size) }) },
      h("b", {}, head), ` - ${rest}`));
    y += 60;
  }
  if (text.note) {
    parts.push(h("div", { class: "tc-note",
      style: at(x + 4, Math.max(y + 40, 640), { fontSize: cardU(fitSize(text.note, pointFont, 28, maxw)) }) },
    text.note));
  }
  parts.push(h("div", { class: "tc-foot", style: at(x + 4, 800, { fontSize: cardU(28) }) }, CARD_FOOTER));
  card.append(...parts);
  return card;
}

/** The top of the detail card: the mod's title card (a picture, or drawn from its card text), else its art. */
function detailTop(mod) {
  if (mod.card_url) {
    return h("div", { class: "detail-card" },
      h("img", { class: "detail-card-img", src: mod.card_url, alt: mod.name + " title card" }));
  }
  if (mod.card_text) return h("div", { class: "detail-card" }, titleCard(mod));
  return h("div", { class: "detail-art", style: { backgroundImage: artUrl(mod) } },
    artUrl(mod) === "none"
      ? h("div", { class: "detail-initials", "aria-hidden": "true" }, h("span", {}, initialsOf(mod)))
      : h("div", { class: "detail-art-img", style: { backgroundImage: artUrl(mod) } }));
}

function detailFor(mod) {
  if (!mod) {
    return [h("div", { class: "detail-body" },
      h("h2", { class: "detail-name" }, "NO MODS FOUND"),
      h("p", { class: "detail-desc" }, state.error ||
        "Put a mod folder with a mod.json in the mods folder, then come back here."))];
  }
  return [
    detailTop(mod),
    h("div", { class: "detail-body" },
      h("div", { class: "detail-head" },
        h("h2", { class: "detail-name" }, mod.name),
        h("div", { class: "detail-actions" },
          h("span", { class: "detail-flag" + (mod.on ? " on" : "") }, mod.on ? "ON" : "OFF"),
          h("button", { class: "btn " + (mod.on ? "ghost" : "mint"), type: "button", "data-key": "toggle:" + mod.id,
            onclick: () => toggleMod(mod) }, mod.on ? "TURN OFF" : "TURN ON"))),
      h("p", { class: "detail-desc" }, mod.description || "No description."),
      leftOutNote(mod),
      featureList(mod),
      h("div", { class: "sub-head" }, "What it changes"),
      h("dl", { class: "kv changes" }, changeLines(mod)),
      h("div", { class: "row-actions" },
        h("button", { class: "btn ghost", type: "button", "data-key": "open:" + mod.id,
          onclick: () => openFolder("mod:" + mod.id) }, "OPEN FOLDER"),
        h("button", { class: "btn ghost", type: "button", "data-key": "recheck", disabled: recheckBusy,
          onclick: recheck }, recheckBusy ? "CHECKING …" : "RE-CHECK INSTALLED"))),
  ];
}

let lastListKey = "", lastDetailKey = "";
let homePicking = null;    // whether Home showed the picker at the last render
function renderHome() {
  const picking = pickerShown();
  const have = (state.mods || []).length > 0;
  const get = $("get-mods");
  get.hidden = !isPlayer() || (picking && !have);   // the picker with no mods yet has nothing to go back to
  get.textContent = picking ? "MY MODS" : "GET MODS";
  get.title = picking ? "Shows the mods you have." : "Shows this release's mods, so you can download more.";
  $("mods-title").textContent = picking ? "GET MODS" : "MODS";
  $("pick-foot").hidden = $("pick-hint").hidden = !picking;
  $("list-hint").hidden = picking;
  if (picking !== homePicking) {   // the list and the card are drawn afresh when the picker opens or closes
    homePicking = picking;
    lastListKey = lastDetailKey = lastPickKey = lastPickDetailKey = "";
  }
  if (picking) { renderPicker(); return; }
  const mods = state.mods || [];
  if (mods.length && !mods.some((m) => m.id === focusId)) focusId = mods[0].id;
  const on = mods.filter((m) => m.on).length;
  $("mods-count").textContent = mods.length ? `${on} OF ${mods.length} ON` : "";
  $("mod-list").classList.toggle("busy", isBusy());   // switches wait for the Play to end
  $("mods-error").hidden = !state.error;
  $("mods-error").textContent = state.error || "";
  const b = state.build || {};
  const listKey = JSON.stringify(["list", mods, focusId, b.left_out]);
  if (listKey !== lastListKey) {
    lastListKey = listKey;
    replaceKeyed($("mod-list"), mods.length ? mods.map((m) => rowFor(m, m.id === focusId))
      : [h("div", { class: "list-empty" }, "NO MODS FOUND")]);
  }
  const mod = mods.find((m) => m.id === focusId) || null;
  const detailKey = JSON.stringify(["list", mod, state.profile, state.error, b.error, b.exe, b.built_from, b.needed,
    b.patch_sets, b.left_out, recheckBusy]);
  if (detailKey !== lastDetailKey) {
    lastDetailKey = detailKey;
    replaceKeyed($("detail"), detailFor(mod));
  }
}

async function recheck() {
  recheckBusy = true;
  if (state) renderHome();
  toast("Checking what is installed …");
  const res = await api("/api/state?refresh=1");
  recheckBusy = false;
  if (res.ok) { state = res.data; render(); toast("Installed state checked."); }
  else { if (state) render(); toast(res.data.error || "Could not check what is installed.", true); }
  const again = $("detail").querySelector('[data-key="recheck"]');
  if (again && document.activeElement === document.body) again.focus();
}

// ---- HOME, the picker: this release's mods with tick boxes (player mode: first run, and GET MODS) ----
const pick = {
  open: false,          // GET MODS was pressed (with no mods here yet the picker shows anyway)
  catalog: null,        // last GET /api/catalog: {release, mods: [...]}
  loading: false,
  ticked: new Set(),    // ids to download
  focus: null,          // the mod shown in the detail card
};
let statusNote = null;  // a refusal or a failed list: shown in the status line while the picker is open, until
                        // the next action, a Play, download or removal starting or ending, or a settings save
let lastSession = null; // the session status at the last render, to see a download or removal end

const isPlayer = () => !!state && state.mode === "player";
const pickerShown = () => isPlayer() && (pick.open || !(state.mods || []).length);
const pickMods = () => (pick.catalog && pick.catalog.mods) || [];
const sizeText = (bytes) => (bytes < 1e5 ? `${Math.max(1, Math.round(bytes / 1e3))} KB` : `${(bytes / 1e6).toFixed(1)} MB`);

async function loadCatalog() {
  if (pick.loading) return;
  pick.loading = true;
  if (state) renderHome();
  const res = await api("/api/catalog");
  pick.loading = false;
  if (res.ok) {
    pick.catalog = res.data;
    statusNote = null;
    const offered = new Set(pickMods().filter((m) => !m.installed).map((m) => m.id));
    for (const id of [...pick.ticked]) if (!offered.has(id)) pick.ticked.delete(id);
  } else {
    pick.catalog = null;
    statusNote = res.data.error || "Cannot get the list of mods.";
  }
  if (state) render();
}

/** Called on every render: a session that starts or ends clears an old note; a download or removal that just
 *  ended changes what is installed, so the picker's list is fetched again. */
function watchSession() {
  const now = state.session.status, was = lastSession;
  lastSession = now;
  if (was === null || now === was) return;
  statusNote = null;   // what a refusal or a failed list said may no longer hold
  if ((was === "downloading" || was === "removing") && pickerShown()) loadCatalog();
}

function setPickFocus(id, moveDom) {
  pick.focus = id;
  if (state) renderHome();
  if (moveDom) {
    const row = $("mod-list").querySelector(`[data-key="${CSS.escape("pick:" + id)}"]`);
    if (row) { row.focus(); row.scrollIntoView({ block: "nearest" }); }
  }
}

function toggleTick(m) {
  if (m.installed) return;
  if (isBusy()) { toast("Wait for the current job to finish before changing the selection.", true); return; }
  if (pick.ticked.has(m.id)) pick.ticked.delete(m.id); else pick.ticked.add(m.id);
  renderHome();
}

/** A refused request: its plain reason in the status line (kept until the next action) and a toast. */
function refused(res, fallback) {
  statusNote = res.data.error || fallback;
  renderStatus();
  toast(statusNote, true);
}

async function getMods() {
  const ids = pickMods().filter((m) => pick.ticked.has(m.id) && !m.installed).map((m) => m.id);
  const res = await api("/api/get-mods", { ids });
  if (res.ok) { statusNote = null; pick.ticked.clear(); } else refused(res, "Cannot download mods now.");
  poll();
}

async function removeMod(m) {
  if (isBusy()) { toast("Wait for the current job to finish before removing a mod.", true); return; }
  const res = await api("/api/remove-mod", { id: m.id });
  if (res.ok) statusNote = null; else refused(res, "Cannot remove it now.");
  poll();
}

/** A check mark (drawn, so it sits in the middle of its box at every size). */
function tickMark() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M5 12.25l4.5 4.5L19 7.25");   // its ink box is centred on (12, 12)
  svg.append(path);
  return svg;
}

/** Badges of a release mod: installed here, and whether this release updates it. */
function pickBadges(m) {
  if (!m.installed) return [];
  const out = [["INSTALLED", "ok", "It is in your mods folder."]];
  if (m.update) out.push(["UPDATE", "warn", `You have release ${m.installed_release}; downloading updates it.`]);
  return out;
}

function pickRowFor(m, focused) {
  const ticked = pick.ticked.has(m.id);
  const quiet = (ev) => ev.preventDefault();   // a click acts; it does not take focus from the row
  const right = m.installed
    ? h("button", { class: "btn ghost small row-remove", type: "button", tabindex: "-1",
      title: "Deletes this mod from your mods folder.", onmousedown: quiet,
      onclick: (ev) => { ev.stopPropagation(); removeMod(m); } }, "REMOVE")
    : h("button", { class: "tick" + (ticked ? " on" : ""), type: "button", tabindex: "-1", role: "checkbox",
      "aria-checked": ticked ? "true" : "false", "aria-label": (ticked ? "Unselect " : "Select ") + m.name,
      title: ticked ? "Selected: click to leave it out" : "Click to select it", onmousedown: quiet,
      onclick: (ev) => { ev.stopPropagation(); toggleTick(m); } }, tickMark());
  return h("div", {
    class: "mod-row pick-row" + (ticked || m.installed ? " on" : "") + (focused ? " focused" : ""),
    role: "option", "aria-selected": focused ? "true" : "false", tabindex: focused ? "0" : "-1",
    "data-key": "pick:" + m.id,
    onclick: () => setPickFocus(m.id, true),
    onkeydown: (ev) => pickKey(ev, m),
  },
  thumbFor(m),
  h("div", { class: "row-main" },
    h("div", { class: "row-name" }, m.name),
    h("div", { class: "row-desc", title: m.description || null }, m.description || ""),
    h("div", { class: "row-parts" },
      pickBadges(m).map(([text, kind, why]) => h("span", { class: "badge-s " + kind, title: why }, text)),
      m.parts.map((p) => h("span", { class: "part" }, PART_LABELS[p])))),
  h("span", { class: "pick-size", title: "Download size" }, sizeText(m.size)),
  right);
}

/** Up/Down (Home/End) move between rows; Space/Enter select or unselect the focused row. */
function pickKey(ev, m) {
  if (ev.altKey || ev.ctrlKey || ev.metaKey) return;
  if (ev.key === " " || ev.key === "Enter") {
    ev.preventDefault();
    if (!ev.repeat) toggleTick(m);
    return;
  }
  const ids = pickMods().map((x) => x.id);
  const at = ids.indexOf(m.id);
  const to = { ArrowUp: at - 1, ArrowDown: at + 1, Home: 0, End: ids.length - 1 }[ev.key];
  if (to === undefined) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (to >= 0 && to < ids.length && to !== at) setPickFocus(ids[to], true);
}

/** The list area while there is no list: getting it, or why it could not be had. */
function pickEmpty() {
  if (pick.loading || (!pick.catalog && !statusNote)) return h("div", { class: "list-empty" }, "GETTING THE LIST OF MODS …");
  if (!pick.catalog) {
    return h("div", { class: "list-empty pick-error" },
      h("p", {}, statusNote),
      h("button", { class: "btn ghost small", type: "button", "data-key": "catalog-again", onclick: loadCatalog }, "TRY AGAIN"));
  }
  return h("div", { class: "list-empty" }, "THIS RELEASE HAS NO MODS");
}

function pickDetailFor(m) {
  if (!m) {
    return [h("div", { class: "detail-body" },
      h("h2", { class: "detail-name" }, "GET MODS"),
      h("p", { class: "detail-desc" }, pick.catalog ? "This release has no mods."
        : "Pick the mods you want from this release's list, then download them."))];
  }
  const ticked = pick.ticked.has(m.id);
  const kv = [h("dt", {}, "Version"), h("dd", {}, m.version || "—"),
    h("dt", {}, "Size"), h("dd", {}, sizeText(m.size)),
    h("dt", {}, "Parts"), h("dd", {}, m.parts.map((p) => PART_LABELS[p].toLowerCase()).join(", ") || "—")];
  if (m.installed) {
    kv.push(h("dt", {}, "You have"), h("dd", { class: m.update ? "warn" : "ok" },
      `release ${m.installed_release}` + (m.update ? " — downloading updates it" : "")));
  }
  return [
    detailTop(m),
    h("div", { class: "detail-body" },
      h("div", { class: "detail-head" },
        h("h2", { class: "detail-name" }, m.name),
        h("div", { class: "detail-actions" },
          h("span", { class: "detail-flag" + (ticked || m.installed ? " on" : "") }, m.installed ? "INSTALLED" : sizeText(m.size)),
          m.installed
            ? h("button", { class: "btn ghost", type: "button", "data-key": "remove:" + m.id, disabled: isBusy(),
              title: "Deletes this mod from your mods folder.", onclick: () => removeMod(m) }, "REMOVE")
            : h("button", { class: "btn " + (ticked ? "ghost" : "mint"), type: "button", "data-key": "tick:" + m.id,
              disabled: isBusy(), onclick: () => toggleTick(m) }, ticked ? "UNSELECT" : "SELECT"))),
      h("p", { class: "detail-desc" }, m.description || "No description."),
      featureList(m),
      h("div", { class: "sub-head" }, "Download"),
      h("dl", { class: "kv changes" }, kv)),
  ];
}

let lastPickKey = "", lastPickDetailKey = "";
function renderPicker() {
  if (!pick.catalog && !pick.loading && !statusNote) loadCatalog();
  const list = pickMods();
  if (list.length && !list.some((m) => m.id === pick.focus)) pick.focus = list[0].id;
  const offered = list.filter((m) => !m.installed).length;
  const updates = list.some((m) => m.update);
  $("mods-count").textContent = !pick.catalog ? "" : offered ? `${pick.ticked.size} SELECTED` : "ALL INSTALLED";
  $("mod-list").classList.toggle("busy", isBusy());
  $("mods-error").hidden = !state.error;
  $("mods-error").textContent = state.error || "";
  const dl = $("download");
  dl.disabled = isBusy() || !pick.catalog || !(pick.ticked.size || updates);
  $("pick-note").textContent = !pick.catalog ? ""
    : updates ? "Downloads the ticked mods and updates your installed ones to this release."
      : "Downloads the ticked mods from this release.";
  $("pick-note").title = pick.catalog ? "Release " + pick.catalog.release : "";
  const busy = isBusy();
  const listKey = JSON.stringify(["pick", pick.catalog, [...pick.ticked], pick.focus, pick.loading, statusNote, busy]);
  if (listKey !== lastPickKey) {
    lastPickKey = listKey;
    replaceKeyed($("mod-list"), list.length ? list.map((m) => pickRowFor(m, m.id === pick.focus)) : [pickEmpty()]);
  }
  const mod = list.find((m) => m.id === pick.focus) || null;
  const detailKey = JSON.stringify(["pick", mod, pick.ticked.has(mod && mod.id), pick.catalog && pick.catalog.release, busy]);
  if (detailKey !== lastPickDetailKey) {
    lastPickDetailKey = detailKey;
    replaceKeyed($("detail"), pickDetailFor(mod));
  }
}

$("get-mods").addEventListener("click", () => {
  pick.open = !pickerShown();
  statusNote = null;
  if (pick.open) pick.catalog = null;   // a fresh list each time it opens
  renderHome();
  renderStatus();
});
$("download").addEventListener("click", getMods);

// ---- action bar: status line + Play (on every screen) ----------------------------------
let playHadFocus = false;  // give focus back to Play when it is enabled again
/** Play: disabled with the reason while the server's play_block says so (the user's own
 *  game, an unknown copy, or more than 3 copies running) or a session is busy; a note beside up to 3 test copies. */
function renderPlay() {
  const btn = $("play"), label = $("play-label"), reason = $("play-reason");
  const status = state.session.status;
  let disabled = false, why = "", text = "PLAY";
  if (BUSY.has(status)) {
    disabled = true;
    text = { applying: "APPLYING", building: "BUILDING", starting: "STARTING", running: "IN GAME",
      downloading: "DOWNLOADING", removing: "REMOVING" }[status];
  } else if (state.play_block) {
    disabled = true;
    why = state.play_block;
  } else if (state.error) {
    disabled = true;
    why = "Cannot play: " + state.error;
  } else if (state.play_note) {
    why = state.play_note;
  }
  if (disabled && document.activeElement === btn) playHadFocus = true;
  btn.disabled = disabled;
  if (!disabled && playHadFocus) {
    playHadFocus = false;
    if (document.activeElement === document.body) btn.focus();
  }
  btn.classList.toggle("busy", BUSY.has(status));
  btn.classList.toggle("long", text.length > 9);   // DOWNLOADING: a smaller label, so it stays inside the button
  btn.classList.toggle("ready", !disabled);
  label.textContent = text;
  btn.title = why;
  reason.textContent = why;
  reason.title = why;
}

/** The session as one status line: {text, kind: ""|"busy"|"error", progress: 0..1|null}. */
function statusInfo() {
  const s = state.session;
  let text = s.message || "", kind = "", progress = null, title = null;
  if (s.status === "building") {
    const line = s.last_line || "";
    const m = /\[(\d+)\/(\d+)\]/.exec(line);
    const gen = /^\[gen\] /.test(line) || line.startsWith("Translating game code");
    const chunks = /emitted (\d+)\/(\d+) chunks/.exec(line);
    if (gen && chunks) {
      text = `Translating game code ${chunks[1]}/${chunks[2]}…`; progress = +chunks[1] / +chunks[2];
    } else if (gen) text = "Translating game code…";
    else if (m) { text = `Building ${m[1]}/${m[2]}…`; progress = +m[1] / +m[2]; }
    else text = s.message || "Building…";
    kind = "busy";
  } else if (s.status === "running") {
    text = s.message || "Game running"; kind = "busy";
  } else if (BUSY.has(s.status)) {
    kind = "busy";
  } else if (s.status === "error") {
    kind = "error";
  } else if (!text) {
    // the count fits the bar; which mods is the list above, and the line's tooltip
    const on = state.mods.filter((m) => m.on).map((m) => m.name);
    text = on.length ? `Ready — ${on.length} mod${on.length === 1 ? "" : "s"} on`
      : "Ready — no mods: plays the normal game";
    if (on.length) title = "Plays with " + on.join(", ");
  }
  if (!kind && statusNote && pickerShown()) return { text: statusNote, kind: "error", progress: null, title: null };
  if (!kind && leftOut().length) {
    text += " · Left out of the build: " + leftOut().map(([b, v]) => (leftOutMods(v).length
      ? leftOutMods(v).map(modNameOf).join(", ") : b)).join(", ");
  }
  return { text, kind, progress, title };
}

function renderStatus() {
  const { text, kind, progress, title } = statusInfo();
  setStatus(text, kind, $("status"), title);
  $("progress").hidden = progress === null;
  if (progress !== null) $("progress").firstElementChild.style.width = (progress * 100).toFixed(1) + "%";
}

function setStatus(text, kind, el = $("status"), title = null) {
  el.className = "status" + (kind ? " " + kind : "");
  el.replaceChildren(h("span", { class: "dot" }), h("span", { class: "status-text", title: title || text }, text));
}

$("play").addEventListener("click", async () => {
  const res = await api("/api/play", {});
  if (!res.ok) {
    setStatus(res.data.error || "Play failed.", "error");
    toast(res.data.error || "Play failed.", true);
  }
  poll();
});


// ---- BUILDS & LOGS ---------------------------------------------------------------------
const short = (sha) => (sha ? String(sha).slice(0, 10) : "—");

let lastBuildKey = "";
function renderBuilds() {
  const b = state.build;
  $("rebuild").disabled = isBusy();
  const info = statusInfo();
  setStatus(info.text, info.kind, $("build-status"));
  const key = JSON.stringify(b);
  if (key === lastBuildKey) return;
  lastBuildKey = key;
  const badge = $("build-badge");
  if (!b || b.error) {
    badge.className = "chip warn"; badge.textContent = "UNKNOWN";
    $("build-body").replaceChildren(h("pre", { class: "build-error" }, (b && b.error) || "No build state."));
    return;
  }
  badge.className = "chip " + (b.up_to_date ? "lit" : "warn");
  badge.textContent = b.up_to_date ? "UP TO DATE" : (b.exe ? "NEEDS REBUILD" : "NOT BUILT");
  const built = b.built_from || {};
  const names = [...new Set([...Object.keys(b.needed || {}), ...Object.keys(built)])]
    .filter((name) => name !== "patches" && name !== "exefs");
  const exefsStale = (b.needed || {}).exefs !== built.exefs;
  const rows = names.map((name) => {
    const stale = (b.needed || {})[name] !== built[name];
    return h("tr", {},
      h("td", {}, name),
      h("td", { class: stale ? "stale" : "" }, short(built[name])),
      h("td", {}, short((b.needed || {})[name])));
  });
  $("build-body").replaceChildren(...[
    h("div", { class: "sub-head" }, "Branch heads"),
    h("table", { class: "heads" },
      h("thead", {}, h("tr", {}, h("th", {}, "Branch"), h("th", {}, "Built"), h("th", {}, "Needed"))),
      h("tbody", {}, rows)),
    ...leftOutRows(b),
    ...patchRows(b, built),
    h("dl", { class: "kv" },
      h("dt", {}, "Game ExeFS"),
      h("dd", { class: exefsStale ? "stale" : "", title: "size:mtime of game\\exefs\\main" },
        exefsStale ? "changed since the build (a game update): rebuild" : "as built"),
      h("dt", {}, "Exe"), h("dd", {}, b.exe || "not built yet")),
    b.last_error ? h("div", { class: "sub-head warn" }, "Last build error") : null,
    b.last_error ? h("pre", { class: "build-error" }, b.last_error) : null].filter(Boolean));
}

/** The mods the build left out (off, their branch would not merge), with the files. */
function leftOutRows(b) {
  const entries = Object.entries(b.left_out || {});
  if (!entries.length) return [];
  return [
    h("div", { class: "sub-head warn" }, "Left out of this build"),
    h("table", { class: "heads" },
      h("thead", {}, h("tr", {}, h("th", {}, "Mod"), h("th", {}, "Branch"), h("th", {}, "Why"))),
      h("tbody", {}, entries.map(([branch, v]) => h("tr", {},
        h("td", {}, leftOutMods(v).length ? leftOutMods(v).map(modNameOf).join(", ") : "—"),
        h("td", {}, branch), h("td", { class: "stale" }, leftOutWhy(v)))))),
    h("p", { class: "mod-note" }, "These mods are off, so the build went on without them; "
      + "switching one on rebuilds and shows the error until its branch is fixed."),
  ];
}

/** The code patches part of the build card: the patch text's hash (built / needed) and
 *  every patch set with its switch; nothing when no mod has code patches. */
function patchRows(b, built) {
  const sets = b.patch_sets || [];
  const needed = (b.needed || {}).patches, have = built.patches;
  if (!sets.length && !needed && !have) return [];
  const modName = (id) => (state.mods.find((m) => m.id === id) || {}).name || id;
  return [
    h("div", { class: "sub-head" }, "Code patches"),
    h("table", { class: "heads" },
      h("thead", {}, h("tr", {}, h("th", {}, "Mod"), h("th", {}, "Set"), h("th", {}, "Switch"))),
      h("tbody", {}, sets.map((p) => h("tr", {},
        h("td", { class: leftOutOf(p.mod) ? "stale" : "" },
          modName(p.mod) + (leftOutOf(p.mod) ? " (left out)" : "")),
        h("td", {}, p.name), h("td", {}, p.env))))),
    h("dl", { class: "kv" },
      h("dt", {}, "Patch text"),
      h("dd", { class: needed !== have ? "stale" : "" },
        `built ${short(have)} · needed ${short(needed)}` + (needed !== have ? " (changed)" : ""))),
  ];
}

$("rebuild").addEventListener("click", async () => {
  const res = await api("/api/build", {});
  if (!res.ok) toast(res.data.error || "Cannot build now.", true);
  else { toast("Rebuilding the combined exe …"); showLog("build"); }
  poll();
});

let logWhich = "build";
let logTimer = null;
function showLog(which) { logWhich = which; pollLog(true); }

async function pollLog(reset) {
  clearTimeout(logTimer);
  for (const tab of document.querySelectorAll("#log-tabs [data-log]")) {
    tab.classList.toggle("active", tab.dataset.log === logWhich);
  }
  const pre = $("log");
  const atBottom = reset || pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
  const res = await api(`/api/logs?which=${logWhich}&lines=400`);
  if (res.ok) {
    $("log-path").textContent = res.data.path;
    const text = res.data.lines.length ? res.data.lines.join("\n") : "(empty — nothing logged yet)";
    if (pre.textContent !== text) pre.textContent = text;
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }
  if (screen === "builds") logTimer = setTimeout(() => pollLog(false), 2000);
}
function stopLog() { clearTimeout(logTimer); }

for (const tab of document.querySelectorAll("#log-tabs [data-log]")) {
  tab.addEventListener("click", () => showLog(tab.dataset.log));
}

// ---- SETTINGS ----------------------------------------------------------------------
// "optional" fields may be left empty (saved as null).
const FIELDS = [
  ["game", "Game folder", "path", "The folder Fastball installed the game into."],
  ["setup", "Setup folder", "optional", "The Fastball setup folder you installed the game from."],
  ["mods", "Mods folder", "path"],
  ["theshow", "TheShow folder", "optional"], ["recomp", "Recomp source", "optional"],
  ["build_wt", "Build worktree", "optional"],
  ["build_out", "Build output", "path"],
  ["state", "State folder", "path", "Selection, journal and logs are kept here; a change applies right away."],
  ["port", "Manager port", "port", "Takes effect after the manager restarts."],
  ["packages_port", "Packages server port", "port"],
  ["profile_override", "Profile override", "optional",
    "Leave empty to let the switched-on mods choose the profile."],
  ["always_combined_exe", "Always use the combined exe", "bool"],
];
/** Fields only a developer with a Fastball source clone uses (state.mode "dev"). */
const DEV_FIELDS = new Set(["theshow", "recomp", "build_wt"]);
const isDev = () => !!state && state.mode === "dev";
let loadedSettings = null;
/** A developer field is shown in dev mode; Recomp source and Build worktree also whenever a Recomp source is set,
 *  so a mistyped path (which makes the mode "player") can be put right here. */
const devFieldShown = (key) => isDev() || (key !== "theshow" && !!(loadedSettings && loadedSettings.recomp));

async function loadSettings() {
  const res = await api("/api/settings");
  if (!res.ok) { $("settings-msg").textContent = res.data.error || "Cannot load settings."; return; }
  loadedSettings = res.data;
  $("fields").replaceChildren(...FIELDS.map(([key, label, kind, hint]) => {
    const value = loadedSettings[key];
    const id = "f-" + key;
    if (kind === "bool") {
      return h("div", { class: "field check", "data-field": key },
        h("input", { type: "checkbox", id, checked: !!value }), h("label", { for: id }, label));
    }
    return h("div", { class: "field", "data-field": key, hidden: DEV_FIELDS.has(key) && !devFieldShown(key) },
      h("label", { for: id }, label),
      h("input", { type: kind === "port" ? "number" : "text", id, spellcheck: "false",
        "aria-describedby": hint ? id + "-hint" : null,
        value: value === null || value === undefined ? "" : String(value) }),
      hint ? h("p", { class: "field-hint", id: id + "-hint" }, hint) : null);
  }));
  setMsg("settings-msg", "", "");
  renderSettingsSide();
}

function settingsChanges() {
  const out = {};
  for (const [key, , kind] of FIELDS) {
    const input = $("f-" + key);
    let value;
    if (kind === "bool") value = input.checked;
    else if (kind === "port") value = input.value.trim() === "" ? null : Number(input.value);
    else if (kind === "optional") value = input.value.trim() === "" ? null : input.value.trim();
    else value = input.value.trim();
    if (value !== loadedSettings[key]) out[key] = value;
  }
  return out;
}

function setMsg(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = "form-msg" + (kind ? " " + kind : "");
}

$("settings-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  for (const el of document.querySelectorAll(".field.bad")) el.classList.remove("bad");
  const changes = settingsChanges();
  if (!Object.keys(changes).length) { setMsg("settings-msg", "Nothing changed.", ""); return; }
  const res = await api("/api/settings", changes);
  if (!res.ok) {
    const err = res.data.error || "Could not save.";
    setMsg("settings-msg", err, "bad");
    const key = Object.keys(changes).find((k) => err.includes(k));
    if (key) document.querySelector(`[data-field="${key}"]`).classList.add("bad");
    return;
  }
  await loadSettings();
  setMsg("settings-msg", "Saved.", "ok");
  statusNote = null;    // the picker fetches its list again on its next draw: Release source may have changed
  pick.catalog = null;
  poll();
});

function renderSettingsSide() {
  for (const key of DEV_FIELDS) {
    const field = document.querySelector(`[data-field="${key}"]`);
    if (field) field.hidden = !devFieldShown(key);
  }
  $("profile-path").textContent = state ? state.profile || "—" : "…";
  const games = (state && state.games_running) || [];
  const busy = isBusy();
  $("backup").disabled = busy || games.length > 0;
  $("backup").title = games.length ? "Close the running game first — " + gamesText(games)
    : busy ? "Wait for the current Play to finish." : "";
  $("quit").disabled = busy;
}

$("backup").addEventListener("click", async () => {
  setMsg("backup-msg", "Backing up …", "");
  $("backup").disabled = true;
  const res = await api("/api/backup-profile", {});
  $("backup").disabled = false;
  if (res.ok) setMsg("backup-msg", "Backed up to " + res.data.path, "ok");
  else setMsg("backup-msg", res.data.error || "Backup failed.", "bad");
});

$("quit").addEventListener("click", async () => {
  const res = await api("/api/quit", {});
  if (!res.ok) { toast(res.data.error || "Cannot quit now.", true); return; }
  clearTimeout(pollTimer);
  stopLog();
  $("gone").hidden = false;
  setTimeout(() => window.close(), 400);
});

// ---- start ---------------------------------------------------------------------------
route();
poll();
