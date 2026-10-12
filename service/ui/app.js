// Protocol service demo console: hash router, top bar (service status, OpenRouter flag, theme) and the screens.
// Plain ES modules, no build step, no external resources; everything runs under the /ui Content-Security-Policy.

import { badge, h, icon, storageGet, storageSet, toast } from "./modules/dom.js";
import { onJobFinal } from "./modules/jobs.js";
import { loadHealth, loadVersion, on, refreshDocs, state } from "./modules/state.js";
import * as documentView from "./modules/view_document.js";
import * as documentsView from "./modules/view_documents.js";
import * as mcpView from "./modules/view_mcp.js";
import * as toolsView from "./modules/view_tools.js";
import * as usecasesView from "./modules/view_usecases.js";

const VIEWS = {
  documents: { module: documentsView, nav: "documents", title: "Documents" },
  doc: { module: documentView, nav: "documents", title: "Document" },
  tools: { module: toolsView, nav: "tools", title: "Tool explorer" },
  mcp: { module: mcpView, nav: "mcp", title: "MCP" },
  usecases: { module: usecasesView, nav: "usecases", title: "Use cases" },
};

const main = document.getElementById("main");
let cleanup = null;
let currentName = null;
let firstRoute = true;

export function parseHash(hash = location.hash) {
  const raw = hash.replace(/^#\/?/, "");
  const q = raw.indexOf("?");
  const path = q >= 0 ? raw.slice(0, q) : raw;
  const parts = path.split("/").filter(Boolean).map((p) => {
    try { return decodeURIComponent(p); } catch (_) { return p; }
  });
  return { parts, name: parts[0] || "documents", query: new URLSearchParams(q >= 0 ? raw.slice(q + 1) : "") };
}

function route() {
  const r = parseHash();
  const view = VIEWS[r.name];
  if (!view) {
    location.replace("#/documents");
    return;
  }
  document.querySelectorAll(".mainnav a").forEach((a) => {
    if (a.dataset.nav === view.nav) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  document.title = `${view.title} · Protocol service`;
  if (currentName === r.name && typeof view.module.update === "function" && view.module.update(r)) return;
  if (cleanup) {
    try { cleanup(); } catch (err) { console.error(err); }
    cleanup = null;
  }
  currentName = r.name;
  try {
    cleanup = view.module.render(main, r) || null;
  } catch (err) {
    console.error(err);
    main.replaceChildren(h("div", { class: "alert alert-danger" }, icon("alert"),
      h("div", { class: "alert-body" }, h("div", { class: "alert-title" }, "This screen failed to render"),
        `${err && err.name ? err.name : "Error"}: see the browser console.`)));
  }
  if (!firstRoute) {
    window.scrollTo(0, 0);
    const heading = main.querySelector("h1");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus({ preventScroll: true });
    }
  }
  firstRoute = false;
}

// ---------------------------------------------------------------- top bar

function paintStatus() {
  const host = document.getElementById("service-status");
  if (!host) return;
  const v = state.version;
  const hl = state.health;
  const parts = [];
  if (hl && hl.status !== "ok") {
    parts.push(badge(hl.status === "unreachable" ? "service unreachable" : `service ${hl.status}`, "danger",
      { class: "badge-dot", title: hl.error ? `${hl.error.type}: ${hl.error.message}` : `worker ${hl.worker}` }));
  }
  if (v) {
    parts.push(v.offline
      ? badge("offline", "outline", { title: "OPENROUTER_OFFLINE is set: the service sends nothing to OpenRouter", class: "badge-dot" })
      : badge("OpenRouter on", "violet", { title: "Upload, reprocess and search may send data to OpenRouter (zero-data-retention routing); you confirm each time", class: "badge-dot" }));
    parts.push(h("span", { class: "badge badge-outline status-pill-text", title: `fingerprint ${v.fingerprint}` }, `v${v.pipeline_version}`));
  } else if (state.versionError) {
    parts.push(badge("version unknown", "warn", { title: `${state.versionError.type}: ${state.versionError.message}` }));
  }
  host.replaceChildren(...parts);
}

const THEMES = ["auto", "light", "dark"];
const THEME_KEY = "protocol-ui.theme";

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "light" || mode === "dark") root.dataset.theme = mode;
  else delete root.dataset.theme;
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const label = `Colour theme: ${mode === "auto" ? "automatic (follows the system)" : mode}`;
    btn.setAttribute("aria-label", `${label}. Activate to change.`);
    btn.title = label;
  }
}

function setupTheme() {
  let mode = storageGet(THEME_KEY, "auto");
  if (!THEMES.includes(mode)) mode = "auto";
  applyTheme(mode);
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.addEventListener("click", () => {
      mode = THEMES[(THEMES.indexOf(mode) + 1) % THEMES.length];
      storageSet(THEME_KEY, mode);
      applyTheme(mode);
      toast(`Theme: ${mode === "auto" ? "automatic" : mode}`, "info", 1600);
    });
  }
}

function setupSkipLink() {
  const link = document.getElementById("skip-link");
  if (!link) return;
  link.addEventListener("click", (e) => {
    e.preventDefault(); // the hash is the router's: focus the content instead of navigating
    main.focus();
  });
}

function start() {
  setupTheme();
  setupSkipLink();
  on("version", paintStatus);
  on("health", paintStatus);
  onJobFinal(() => loadHealth());
  window.addEventListener("hashchange", route);
  loadVersion();
  loadHealth();
  refreshDocs();
  setInterval(loadHealth, 30000);
  if (!location.hash || location.hash === "#" || location.hash === "#/") history.replaceState(null, "", "#/documents");
  route();
}

start();
