// Shared state: service version / offline flag, the document list, the selected document, tool definitions.

import { PATHS, get } from "./api.js";
import { h, shortId, storageGet, storageSet, uid } from "./dom.js";

const SELECTED_KEY = "protocol-ui.selected-doc";

export const state = {
  version: null,        // GET /version
  versionError: null,
  health: null,         // GET /health
  docs: [],             // GET /documents (documents)
  docsLoaded: false,
  docsError: null,
  tools: null,          // GET /tools (anthropic style)
  selectedDoc: storageGet(SELECTED_KEY, null),
};

const listeners = new Map();

export function on(event, fn) {
  if (!listeners.has(event)) listeners.set(event, new Set());
  listeners.get(event).add(fn);
  return () => listeners.get(event).delete(fn);
}

export function emit(event, data) {
  for (const fn of [...(listeners.get(event) || [])]) {
    try { fn(data); } catch (err) { console.error(err); }
  }
}

/** true / false from /version; null while unknown (treated as online: confirm before OpenRouter calls). */
export function isOffline() {
  return state.version ? Boolean(state.version.offline) : null;
}

export async function loadVersion() {
  try {
    const res = await get(PATHS.version);
    state.version = res.data;
    state.versionError = null;
  } catch (err) {
    state.versionError = err;
  }
  emit("version", state.version);
  return state.version;
}

export async function loadHealth() {
  try {
    const res = await get(PATHS.health, { accept: [503] });
    state.health = res.data;
  } catch (err) {
    state.health = { status: "unreachable", error: err };
  }
  emit("health", state.health);
  return state.health;
}

let docsPromise = null;
export function refreshDocs() {
  if (docsPromise) return docsPromise;
  docsPromise = (async () => {
    try {
      const res = await get(PATHS.documents, { query: { limit: 1000 } });
      state.docs = (res.data && res.data.documents) || [];
      state.docsError = null;
    } catch (err) {
      state.docsError = err;
    } finally {
      state.docsLoaded = true;
      docsPromise = null;
    }
    if (state.selectedDoc && state.docsLoaded && !state.docsError && !state.docs.some((d) => d.doc_id === state.selectedDoc)) {
      state.selectedDoc = null;
    }
    if (!state.selectedDoc) {
      const ready = state.docs.find((d) => d.has_result);
      if (ready) state.selectedDoc = ready.doc_id;
    }
    emit("docs", state.docs);
    return state.docs;
  })();
  return docsPromise;
}

export async function ensureDocs() {
  if (!state.docsLoaded) await refreshDocs();
  return state.docs;
}

export async function loadTools() {
  if (state.tools) return state.tools;
  const res = await get(PATHS.tools, { query: { style: "anthropic" } });
  state.tools = (res.data && res.data.tools) || [];
  return state.tools;
}

export function setSelectedDoc(docId) {
  if (state.selectedDoc === docId) return;
  state.selectedDoc = docId || null;
  storageSet(SELECTED_KEY, docId || null);
  emit("selected", state.selectedDoc);
}

export function findDoc(docId) {
  return state.docs.find((d) => d.doc_id === docId) || null;
}

/** {title, subtitle} for a document: the sponsor protocol id or NCT first, then the file name. */
export function docLabel(doc) {
  if (!doc) return { title: "Unknown document", subtitle: "" };
  const ids = doc.identifiers || {};
  const file = (doc.filenames || [])[0] || "";
  const main = ids.sponsor_protocol_id || ids.nct || file || shortId(doc.doc_id, 12);
  const extra = [ids.nct && ids.nct !== main ? ids.nct : null, file && file !== main ? file : null].filter(Boolean);
  return { title: main, subtitle: extra.join(" · ") };
}

/**
 * A document picker (select) bound to the selected document. onlyReady: documents with a published result only.
 * Returns the wrapping element; it keeps itself in sync with the document list while connected.
 */
export function docPicker({ onlyReady = true, label = "Document", onChange } = {}) {
  const id = uid("doc-picker");
  const select = h("select", { id, class: "input" });
  const hint = h("div", { class: "field-help" });
  const wrap = h("div", { class: "field" }, h("label", { class: "field-label", for: id }, label), select, hint);

  function fill() {
    const docs = state.docs.filter((d) => !onlyReady || d.has_result);
    select.replaceChildren();
    if (!state.docsLoaded) {
      select.append(h("option", { value: "" }, "Loading documents…"));
      select.disabled = true;
      return;
    }
    if (state.docsError) {
      select.append(h("option", { value: "" }, "Documents unavailable"));
      select.disabled = true;
      hint.textContent = `${state.docsError.type}: ${state.docsError.message}`;
      return;
    }
    if (!docs.length) {
      select.append(h("option", { value: "" }, "No processed document yet"));
      select.disabled = true;
      hint.replaceChildren("Upload a protocol on the ", h("a", { href: "#/documents" }, "Documents"), " page first.");
      return;
    }
    select.disabled = false;
    hint.textContent = "";
    for (const d of docs) {
      const { title, subtitle } = docLabel(d);
      select.append(h("option", { value: d.doc_id }, `${title}${subtitle ? ` — ${subtitle}` : ""} (${shortId(d.doc_id, 8)})`));
    }
    if (!docs.some((d) => d.doc_id === state.selectedDoc)) setSelectedDoc(docs[0].doc_id);
    select.value = state.selectedDoc;
  }

  select.addEventListener("change", () => {
    setSelectedDoc(select.value);
    if (onChange) onChange(select.value);
  });
  const offDocs = on("docs", () => (wrap.isConnected ? fill() : offDocs()));
  const offSel = on("selected", () => {
    if (!wrap.isConnected) { offSel(); return; }
    if (select.value !== state.selectedDoc && state.selectedDoc) select.value = state.selectedDoc;
  });
  fill();
  if (!state.docsLoaded) refreshDocs();
  return wrap;
}
