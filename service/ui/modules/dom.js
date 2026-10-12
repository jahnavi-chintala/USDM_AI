// DOM helpers. Every piece of text goes through textContent / text nodes, never innerHTML, so document text and
// API messages can never become markup.

export function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  append(el, ...children);
  return el;
}

const PROPS = new Set(["value", "checked", "selected", "disabled", "indeterminate", "open", "hidden", "multiple"]);

export function setAttrs(el, attrs) {
  if (!attrs) return el;
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null) continue;
    if (key === "class") {
      el.className = Array.isArray(value) ? value.filter(Boolean).join(" ") : String(value);
    } else if (key === "text") {
      el.textContent = String(value);
    } else if (key === "dataset") {
      for (const [k, v] of Object.entries(value)) if (v !== undefined && v !== null) el.dataset[k] = String(v);
    } else if (key === "style") {
      Object.assign(el.style, value); // CSSOM properties: allowed by style-src 'self' (no style attribute in markup)
    } else if (key.startsWith("on") && typeof value === "function") {
      el.addEventListener(key.slice(2), value);
    } else if (PROPS.has(key)) {
      el[key] = value;
    } else if (value === false) {
      continue;
    } else {
      el.setAttribute(key, value === true ? "" : String(value));
    }
  }
  return el;
}

export function append(el, ...children) {
  for (const child of children) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (Array.isArray(child)) append(el, ...child);
    else if (child instanceof Node) el.appendChild(child);
    else el.appendChild(document.createTextNode(String(child)));
  }
  return el;
}

export function clear(el) {
  el.replaceChildren();
  return el;
}

export function frag(...children) {
  return append(document.createDocumentFragment(), ...children);
}

/** An icon from the <template id="icon-set"> of index.html (no SVG namespace needed here). */
export function icon(name, extraClass) {
  const tpl = document.getElementById("icon-set");
  const svg = tpl && tpl.content.querySelector(`[data-icon="${name}"]`);
  if (!svg) return h("span", { class: "icon", "aria-hidden": "true" });
  const node = svg.cloneNode(true);
  if (extraClass) node.classList.add(extraClass);
  return node;
}

export function badge(text, kind, attrs = {}) {
  return h("span", { ...attrs, class: ["badge", kind ? `badge-${kind}` : null, attrs.class] }, text);
}

export function spinner(label) {
  return h("span", { class: "row small muted", role: "status" }, h("span", { class: "spinner", "aria-hidden": "true" }),
    label || "Loading…");
}

export function button(label, attrs = {}, iconName) {
  return h("button", { type: "button", ...attrs, class: ["btn", attrs.class] }, iconName ? icon(iconName) : null, label);
}

// ---------------------------------------------------------------- formatting

export function fmtBytes(n) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "–";
  const units = ["B", "KB", "MB", "GB"];
  let v = Number(n);
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
  return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}

export function fmtNumber(n) {
  if (n === null || n === undefined || n === "") return "–";
  const v = Number(n);
  return Number.isFinite(v) ? v.toLocaleString() : String(n);
}

export function fmtPct(v, digits = 1) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "–";
  return `${(Number(v) * 100).toFixed(digits)}%`;
}

export function fmtSeconds(s) {
  if (s === null || s === undefined || !Number.isFinite(Number(s))) return "–";
  const v = Number(s);
  if (v < 1) return `${v.toFixed(1)} s`;
  if (v < 90) return `${v < 10 ? v.toFixed(1) : Math.round(v)} s`;
  const m = Math.floor(v / 60);
  const r = Math.round(v % 60);
  if (m < 60) return `${m} min ${r ? `${r} s` : ""}`.trim();
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function parseDate(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function fmtDate(iso) {
  const d = parseDate(iso);
  if (!d) return "–";
  return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function fmtAgo(iso) {
  const d = parseDate(iso);
  if (!d) return "–";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return fmtDate(iso);
}

export function timeEl(iso, mode = "ago") {
  const d = parseDate(iso);
  if (!d) return h("span", { class: "faint" }, "–");
  return h("time", { datetime: iso, title: d.toLocaleString() }, mode === "ago" ? fmtAgo(iso) : fmtDate(iso));
}

export function shortId(id, n = 10) {
  return id ? String(id).slice(0, n) : "–";
}

/** "p. 11", "pp. 11–13", "pp. 11, 14, 20". */
export function fmtPages(pages) {
  if (!Array.isArray(pages) || !pages.length) return "";
  const nums = [...new Set(pages.filter((p) => Number.isFinite(Number(p))).map(Number))].sort((a, b) => a - b);
  if (!nums.length) return "";
  if (nums.length === 1) return `p. ${nums[0]}`;
  const consecutive = nums.every((p, i) => i === 0 || p === nums[i - 1] + 1);
  if (consecutive) return `pp. ${nums[0]}–${nums[nums.length - 1]}`;
  if (nums.length > 8) return `pp. ${nums.slice(0, 6).join(", ")}, … ${nums[nums.length - 1]}`;
  return `pp. ${nums.join(", ")}`;
}

/** outline / sections give [first, last]. */
export function fmtPageRange(pages) {
  if (!Array.isArray(pages) || !pages.length) return "";
  const a = pages[0];
  const b = pages[pages.length - 1];
  return a === b ? `p. ${a}` : `pp. ${a}–${b}`;
}

export function truncate(text, n) {
  const s = String(text ?? "");
  return s.length > n ? `${s.slice(0, n - 1).trimEnd()}…` : s;
}

export function plural(n, one, many) {
  return `${fmtNumber(n)} ${n === 1 ? one : (many || `${one}s`)}`;
}

// ---------------------------------------------------------------- clipboard, toasts, storage

export async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall back below */ }
  const ta = h("textarea", { class: "visually-hidden", "aria-hidden": "true" });
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  ta.remove();
  return ok;
}

export function copyButton(getText, label = "Copy", attrs = {}) {
  const btn = button(label, { ...attrs, class: ["btn-sm", attrs.class] }, "copy");
  btn.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const ok = await copyText(typeof getText === "function" ? getText() : String(getText));
    toast(ok ? "Copied to the clipboard" : "Copy failed: select the text and copy it", ok ? "ok" : "warn");
  });
  return btn;
}

export function toast(message, kind = "info", timeout = 4200) {
  const host = document.getElementById("toasts");
  if (!host) return;
  const glyph = kind === "error" || kind === "warn" ? "alert" : kind === "ok" ? "check" : "doc";
  const close = h("button", { type: "button", class: "btn btn-ghost btn-xs", "aria-label": "Dismiss" }, icon("x"));
  const el = h("div", { class: ["toast", kind] }, icon(glyph), h("div", { class: "grow" }, message), close);
  close.addEventListener("click", () => el.remove());
  host.appendChild(el);
  if (timeout) setTimeout(() => el.remove(), timeout);
}

export function storageGet(key, fallback = null) {
  try {
    const v = window.localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch (_) {
    return fallback;
  }
}

export function storageSet(key, value) {
  try {
    if (value === null || value === undefined) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, String(value));
  } catch (_) { /* private mode or blocked storage: a convenience only */ }
}

export function uid(prefix = "id") {
  uid.n = (uid.n || 0) + 1;
  return `${prefix}-${uid.n}`;
}

/** A labelled key/value list; rows with an undefined value are skipped. */
export function kv(rows, cls) {
  const dl = h("dl", { class: ["kv", cls] });
  for (const [key, value] of rows) {
    if (value === undefined) continue;
    dl.append(h("dt", null, key), h("dd", null, value === null || value === "" ? h("span", { class: "faint" }, "–") : value));
  }
  return dl;
}

export function alertBox(kind, title, body, iconName) {
  const glyph = iconName || (kind === "danger" || kind === "warn" ? "alert" : kind === "ok" ? "check" : kind === "violet" ? "cloud" : "doc");
  return h("div", { class: ["alert", `alert-${kind}`], role: kind === "danger" ? "alert" : null }, icon(glyph),
    h("div", { class: "alert-body" }, title ? h("div", { class: "alert-title" }, title) : null, body));
}

export function emptyState(text, iconName = "doc") {
  return h("div", { class: "empty" }, icon(iconName), h("div", null, text));
}
