// One document: identifiers, quality summary, warnings, current job; tabs for Docling's HTML, the section tree,
// the chunks, the images, quality.json and the published files.

import { PATHS, fileUrl, get } from "./api.js";
import { showDialog } from "./dialog.js";
import {
  alertBox, badge, button, copyButton, emptyState, fmtBytes, fmtNumber, fmtPageRange, fmtPages, fmtPct, fmtSeconds, h,
  icon, kv, shortId, timeEl, truncate, uid,
} from "./dom.js";
import { FINAL, jobCard, onJobFinal, stateBadge, trackJob } from "./jobs.js";
import { errorCard, genericView } from "./renderers.js";
import { docLabel, findDoc, refreshDocs, setSelectedDoc, state } from "./state.js";
import { deleteFlow, reprocessFlow } from "./view_documents.js";

const TABS = [
  { id: "document", label: "Document" },
  { id: "sections", label: "Sections" },
  { id: "chunks", label: "Chunks" },
  { id: "images", label: "Images" },
  { id: "quality", label: "Quality" },
  { id: "files", label: "Files" },
];

let current = null; // {docId, select(tabId)}

export function update(route) {
  if (current && route.parts[1] === current.docId) {
    current.select(route.parts[2] || "document", false);
    return true;
  }
  return false;
}

export function render(main, route) {
  const docId = route.parts[1];
  const cleanups = [];
  const page = h("div", { class: "stack" });
  main.replaceChildren(page, h("div", { class: "page-loading" }, h("span", { class: "spinner" }), "Loading document…"));
  const cache = new Map(); // loader results per tab / file
  let doc = null;
  let disposed = false;

  const load = async () => {
    try {
      const res = await get(PATHS.document, { params: { doc_id: docId } });
      if (disposed) return;
      doc = res.data;
      paint();
    } catch (err) {
      if (disposed) return;
      main.replaceChildren(h("div", { class: "stack" },
        h("div", { class: "breadcrumb" }, h("a", { href: "#/documents" }, "Documents"), " / ", shortId(docId, 12)),
        h("h1", null, "Document"), errorCard(err)));
    }
  };

  let activeTab = route.parts[2] || "document";
  let tabButtons = [];
  let panel = null;

  function paint() {
    const { title, subtitle } = docLabel(doc);
    setSelectedDoc(doc.has_result ? doc.doc_id : state.selectedDoc);
    const head = h("section", { class: "card" },
      h("div", { class: "breadcrumb" }, h("a", { href: "#/documents" }, "Documents"), " / ", title),
      h("div", { class: "doc-header" },
        h("div", { class: "stack-sm grow" },
          h("h1", { tabindex: "-1" }, title),
          h("div", { class: "row" }, stateBadge(doc.status),
            doc.stale ? badge("stale: made by another pipeline version", "warn") : doc.has_result ? badge("current", "ok") : null,
            doc.pipeline_version ? badge(`pipeline ${doc.pipeline_version}`, "outline") : null,
            doc.has_result ? null : badge("no result yet", "outline")),
          h("div", { class: "small muted break" }, (doc.filenames || []).join(", ") || "–",
            subtitle && !(doc.filenames || []).includes(subtitle) ? ` · ${subtitle}` : "")),
        h("div", { class: "row" },
          doc.has_result ? h("a", { class: "btn btn-primary", href: "#/tools" }, icon("play"), "Tool explorer") : null,
          actionButton("Reprocess", "refresh", () => reprocessFlow(doc).then((r) => { if (r && r.job_id) { trackJob(r.job_id); load(); } })),
          actionButton("Delete", "trash", async () => { if (await deleteFlow(doc)) location.hash = "#/documents"; }, "btn-danger"),
          actionButton("Refresh", "refresh", () => load(), "btn-ghost"))),
      h("hr"),
      kv([
        ["doc_id", h("span", { class: "row" }, h("span", { class: "mono break" }, doc.doc_id), copyButton(doc.doc_id, "Copy", { class: "btn-xs" }))],
        ["Pages", doc.pages ? fmtNumber(doc.pages) : "–"],
        ["Size", fmtBytes(doc.size)],
        ["Published", doc.published_at ? timeEl(doc.published_at, "date") : "not yet"],
        ["Updated", timeEl(doc.updated_at)],
        ["Fingerprint", h("span", { class: "mono" }, doc.fingerprint || "–")],
      ], "kv-compact"));

    const ids = doc.identifiers || {};
    const idRows = Object.entries(ids).filter(([, v]) => v);
    const idCard = h("section", { class: "card" }, h("h2", null, "Identifiers"),
      idRows.length ? kv(idRows.map(([k, v]) => [k.replace(/_/g, " "), h("span", { class: "mono" }, String(v))]), "kv-compact")
        : h("div", { class: "muted small" }, "No identifier recorded for this document."),
      h("div", { class: "tiny muted" }, "Recorded by the pipeline; document_info shows each with the verbatim line it was found in."));

    const q = doc.quality || {};
    const qCard = h("section", { class: "card" }, h("h2", null, "Quality summary"), qualityTiles(q),
      Object.keys(q).length ? null : h("div", { class: "muted small" }, "No quality summary (results imported from an existing output folder have none)."));

    const extra = [];
    if (doc.error) extra.push(alertBox("danger", "The last job failed", h("span", { class: "mono" }, `${doc.error.type || "error"}${doc.error.where ? ` at ${doc.error.where}` : ""}`)));
    if ((doc.warnings || []).length) {
      extra.push(alertBox("warn", `${doc.warnings.length} warning${doc.warnings.length === 1 ? "" : "s"} on the served result`,
        h("ul", { class: "list-bullets small" }, doc.warnings.map((w) => h("li", null, String(w))))));
    }
    const rel = relatedCard(doc);
    if (rel) extra.push(rel);
    if (doc.current_job && !FINAL.has(doc.current_job.state)) {
      trackJob(doc.current_job.job_id, doc.current_job);
      extra.push(h("section", { class: "card" }, h("h2", null, "Current job"), jobCard(doc.current_job.job_id, { title })));
    }

    // tabs
    const tablist = h("div", { class: "tabs", role: "tablist", "aria-label": "Document views" });
    panel = h("div", { class: "tabpanel", role: "tabpanel", tabindex: "0" });
    tabButtons = TABS.map((t) => {
      const b = h("button", { type: "button", class: "tab", role: "tab", id: `tab-${t.id}`, "aria-controls": "doc-tabpanel",
        "aria-selected": "false", tabindex: "-1", disabled: !doc.has_result }, t.label);
      b.addEventListener("click", () => select(t.id, true));
      b.addEventListener("keydown", (e) => {
        const i = TABS.findIndex((x) => x.id === t.id);
        let j = null;
        if (e.key === "ArrowRight") j = (i + 1) % TABS.length;
        if (e.key === "ArrowLeft") j = (i - 1 + TABS.length) % TABS.length;
        if (e.key === "Home") j = 0;
        if (e.key === "End") j = TABS.length - 1;
        if (j !== null) { e.preventDefault(); select(TABS[j].id, true); tabButtons[j].focus(); }
      });
      tablist.append(b);
      return b;
    });
    panel.id = "doc-tabpanel";
    const tabsCard = h("section", { class: "card" }, tablist,
      doc.has_result ? panel : h("div", { class: "tabpanel" }, emptyState(`No published result yet (status ${doc.status}).`)));

    page.replaceChildren(head, h("div", { class: "grid-2" }, idCard, qCard), ...extra, tabsCard);
    main.replaceChildren(page);
    if (doc.has_result) select(activeTab, false);
  }

  function select(tabId, pushHash) {
    if (!TABS.some((t) => t.id === tabId)) tabId = "document";
    activeTab = tabId;
    if (pushHash) {
      const target = `#/doc/${docId}/${tabId}`;
      if (location.hash !== target) history.replaceState(null, "", target);
    }
    if (!panel || !doc) return;
    tabButtons.forEach((b, i) => {
      const on = TABS[i].id === tabId;
      b.setAttribute("aria-selected", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    panel.setAttribute("aria-labelledby", `tab-${tabId}`);
    const loaders = { document: tabDocument, sections: tabSections, chunks: tabChunks, images: tabImages, quality: tabQuality, files: tabFiles };
    panel.replaceChildren(h("div", { class: "page-loading" }, h("span", { class: "spinner" }), "Loading…"));
    loaders[tabId]().then((el) => { if (activeTab === tabId) panel.replaceChildren(el); })
      .catch((err) => { if (activeTab === tabId) panel.replaceChildren(errorCard(err)); });
  }

  // ---- data
  async function fetchOnce(key, fn) {
    if (!cache.has(key)) cache.set(key, fn().catch((err) => { cache.delete(key); throw err; }));
    return cache.get(key);
  }
  const fileList = () => fetchOnce("files", async () => (await get(PATHS.files, { params: { doc_id: docId } })).data);
  const chunks = () => fetchOnce("chunks", async () => {
    const res = await get(PATHS.file, { params: { doc_id: docId, name: "chunks/chunks.jsonl" }, text: true });
    return String(res.data || "").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l));
  });
  const sections = () => fetchOnce("sections", async () => (await get(PATHS.file, { params: { doc_id: docId, name: "chunks/sections.json" } })).data);

  // ---- tabs
  async function tabDocument() {
    const files = doc.files || [];
    if (!files.includes("protocol.html")) return emptyState("This result has no protocol.html.");
    const src = fileUrl(docId, "protocol.html");
    // Sandboxed without allow-scripts / forms / popups / top navigation: the frame never runs script. The file's own
    // response CSP ("sandbox") makes its origin opaque in any case; "allow-same-origin" here only keeps browsers that
    // refuse to load fully sandboxed frames (Electron-based panes) from blocking the request.
    const frame = h("iframe", { class: "doc-frame", src, title: `Docling HTML of ${docLabel(doc).title}`,
      sandbox: "allow-same-origin", referrerpolicy: "no-referrer" });
    return h("div", { class: "stack-sm" },
      h("div", { class: "row-between" }, h("div", { class: "small muted" }, "Docling's HTML export, shown in a sandboxed frame (no scripts)."),
        h("div", { class: "row" },
          files.includes("protocol.md") ? h("a", { class: "btn btn-sm", href: fileUrl(docId, "protocol.md"), target: "_blank", rel: "noopener" }, "Markdown") : null,
          h("a", { class: "btn btn-sm", href: src, target: "_blank", rel: "noopener" }, icon("external"), "Open in a new tab"))),
      h("div", { class: "frame-wrap" }, frame));
  }

  async function tabSections() {
    const data = await sections();
    const list = (data && data.sections) || [];
    if (!list.length) return emptyState("No sections.");
    const byId = new Map(list.map((s) => [s.section_id, s]));
    const kids = new Map();
    const roots = [];
    for (const s of list) {
      if (s.parent_id && byId.has(s.parent_id)) kids.set(s.parent_id, [...(kids.get(s.parent_id) || []), s]);
      else roots.push(s);
    }
    const filterId = uid("sec-filter");
    const input = h("input", { id: filterId, type: "search", class: "input", placeholder: "Filter by number, title, role or id" });
    const treeHost = h("div");
    const count = h("span", { class: "small muted" });

    const paintTree = () => {
      const q = input.value.trim().toLowerCase();
      let keep = null;
      if (q) {
        keep = new Set();
        for (const s of list) {
          const hay = `${s.number || ""} ${s.title || ""} ${s.role || ""} ${s.section_id}`.toLowerCase();
          if (hay.includes(q)) {
            let cur = s;
            while (cur && !keep.has(cur.section_id)) { keep.add(cur.section_id); cur = byId.get(cur.parent_id); }
          }
        }
      }
      const node = (s) => {
        if (keep && !keep.has(s.section_id)) return null;
        const children = (kids.get(s.section_id) || []).map(node).filter(Boolean);
        const read = h("a", { class: "chip", href: `#/tools/read_section?section=${encodeURIComponent(s.section_id)}&run=1`,
          title: `read_section ${s.section_id}` }, s.section_id);
        const titleText = s.number && (s.title || "").startsWith(s.number) ? s.title.slice(s.number.length).replace(/^\.?\s*/, "") : (s.title || "");
        const row = h("span", { class: "tree-row" },
          s.number ? h("span", { class: "tree-num" }, s.number) : null,
          h("span", { class: "tree-title" }, markText(titleText, q)),
          s.role ? h("span", { class: ["chip", "chip-role", s.role_score < 0.7 ? "weak" : null], title: `role score ${s.role_score}` }, s.role) : null,
          h("span", { class: "tree-meta" }, [fmtPages(s.pages), (s.item_refs || []).length ? `${s.item_refs.length} items` : null].filter(Boolean).join(" · ")),
          read);
        if (!children.length) return h("li", { class: "tree-node leaf" }, row);
        return h("li", { class: "tree-node" }, h("details", { open: Boolean(keep) || (s.level ?? 1) <= 1 }, h("summary", null, row), h("ul", null, children)));
      };
      const items = roots.map(node).filter(Boolean);
      count.textContent = keep ? `${[...keep].length} of ${list.length} sections` : `${list.length} sections`;
      treeHost.replaceChildren(items.length ? h("ul", { class: "tree" }, items) : emptyState("No section matches."));
    };
    input.addEventListener("input", paintTree);
    paintTree();
    return h("div", { class: "stack-sm" },
      h("div", { class: "row-between" }, h("div", { class: "field grow" }, h("label", { class: "visually-hidden", for: filterId }, "Filter sections"), input), count),
      h("div", { class: "tiny muted" }, "From chunks/sections.json. The id chip opens read_section for that section in the tool explorer; dashed role chips mark weak or inherited roles (score < 0.7)."),
      treeHost);
  }

  async function tabChunks() {
    const all = await chunks();
    const types = [...new Set(all.map((c) => c.chunk_type).filter(Boolean))].sort();
    const qId = uid("chunk-q");
    const tId = uid("chunk-t");
    const input = h("input", { id: qId, type: "search", class: "input", placeholder: "Search chunk text, section or id" });
    const typeSel = h("select", { id: tId, class: "input" }, h("option", { value: "" }, "All types"), types.map((t) => h("option", { value: t }, t)));
    const sizeSel = h("select", { class: "input input-inline", "aria-label": "Rows per page" }, [25, 50, 100].map((n) => h("option", { value: String(n) }, `${n} / page`)));
    const host = h("div");
    const pager = h("div", { class: "pager" });
    let pageNo = 0;
    const expanded = new Set();

    const paint = () => {
      const q = input.value.trim().toLowerCase();
      const t = typeSel.value;
      const size = Number(sizeSel.value);
      const rows = all.filter((c) => (!t || c.chunk_type === t) && (!q || `${c.chunk_id} ${c.section_number || ""} ${c.section_title || ""} ${c.text || ""}`.toLowerCase().includes(q)));
      const pages = Math.max(1, Math.ceil(rows.length / size));
      pageNo = Math.min(pageNo, pages - 1);
      const slice = rows.slice(pageNo * size, pageNo * size + size);
      const tbody = h("tbody");
      for (const c of slice) {
        const open = expanded.has(c.chunk_id);
        const toggle = h("button", { type: "button", class: "btn btn-ghost btn-xs mono", "aria-expanded": open ? "true" : "false",
          title: open ? "Collapse" : "Show the whole chunk" }, open ? "▾" : "▸", c.chunk_id);
        toggle.addEventListener("click", () => { if (expanded.has(c.chunk_id)) expanded.delete(c.chunk_id); else expanded.add(c.chunk_id); paint(); });
        tbody.append(h("tr", { class: open ? "row-expanded" : null },
          h("td", { "data-label": "Chunk", class: "nowrap" }, toggle),
          h("td", { "data-label": "Section", class: "small" }, c.section_number ? h("span", { class: "tree-num" }, `${c.section_number} `) : null,
            truncate(c.section_title || "", 60), c.role ? h("div", null, h("span", { class: "chip chip-role" }, c.role)) : null),
          h("td", { "data-label": "Pages", class: "nowrap small" }, fmtPages(c.pages) || fmtPageRange([c.page_start, c.page_end])),
          h("td", { "data-label": "Type", class: "small" }, c.chunk_type || "–"),
          h("td", { "data-label": "Tokens", class: "num" }, fmtNumber(c.n_tokens)),
          h("td", { "data-label": "Text" }, h("span", { class: "snippet" }, truncate((c.text || "").replace(/\s+/g, " "), 160)))));
        if (open) {
          tbody.append(h("tr", { class: "row-expanded" }, h("td", { colspan: "6" }, h("div", { class: "stack-sm" },
            h("pre", { class: "chunk-text" }, c.text || ""),
            kv([
              ["Headings", (c.headings || []).join(" › ") || "–"],
              ["Section id", c.section_id ? h("span", { class: "chip" }, c.section_id) : "–"],
              ["Elements", (c.element_types || []).join(", ") || "–"],
              ["Item refs", h("span", { class: "mono small break" }, (c.doc_item_refs || []).join(", ") || "–")],
              ["Parent", h("span", { class: "mono small" }, c.parent_id || "–")],
              ["Content hash", h("span", { class: "mono small" }, c.content_hash || "–")],
              ["Figures", Array.isArray(c.figures) && c.figures.length ? c.figures.map((f) => f.caption || f.figure_id).join("; ") : undefined],
            ], "kv-compact")))));
        }
      }
      host.replaceChildren(rows.length ? h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
        h("thead", null, h("tr", null, ["Chunk", "Section", "Pages", "Type", "Tokens", "Text"].map((x, i) => h("th", { class: i === 4 ? "num" : null }, x)))),
        tbody)) : emptyState("No chunk matches.", "search"));
      const prev = button("Previous", { class: "btn-sm", disabled: pageNo === 0 });
      const next = button("Next", { class: "btn-sm", disabled: pageNo >= pages - 1 });
      prev.addEventListener("click", () => { pageNo -= 1; paint(); });
      next.addEventListener("click", () => { pageNo += 1; paint(); });
      pager.replaceChildren(h("span", { class: "small muted" }, `${fmtNumber(rows.length)} of ${fmtNumber(all.length)} chunks · page ${pageNo + 1} of ${pages}`),
        h("div", { class: "row" }, sizeSel, prev, next));
    };
    input.addEventListener("input", () => { pageNo = 0; paint(); });
    typeSel.addEventListener("change", () => { pageNo = 0; paint(); });
    sizeSel.addEventListener("change", () => { pageNo = 0; paint(); });
    paint();
    return h("div", { class: "stack-sm" },
      h("div", { class: "filters" },
        h("div", { class: "field" }, h("label", { class: "field-label", for: qId }, "Search"), input),
        h("div", { class: "field" }, h("label", { class: "field-label", for: tId }, "Chunk type"), typeSel)),
      host, pager);
  }

  async function tabImages() {
    const listing = await fileList();
    const images = listing.images || [];
    if (!images.length) return emptyState("This result has no images.");
    let figures = new Map();
    try {
      const all = await chunks();
      for (const c of all) {
        for (const f of c.figures || []) {
          if (f.image_path && !figures.has(f.image_path)) figures.set(f.image_path, { ...f, pages: c.pages, section: c.section_title });
        }
      }
    } catch (_) { figures = new Map(); }
    const items = images.map((name) => {
      const f = figures.get(name);
      const src = fileUrl(docId, name);
      const cap = f && f.caption ? f.caption : name.split("/").pop();
      const b = h("button", { type: "button", class: "gallery-item", "aria-label": `View ${cap}` },
        h("span", { class: "gallery-thumb" }, h("img", { src, alt: cap, loading: "lazy", decoding: "async" })),
        h("span", { class: "gallery-cap" }, h("span", null, truncate(cap, 90)),
          f ? h("span", { class: "tiny muted" }, [fmtPages(f.pages), f.class ? f.class.replace(/_/g, " ") : null].filter(Boolean).join(" · ")) : null,
          h("span", { class: "mono tiny" }, name.split("/").pop())));
      b.addEventListener("click", () => showDialog({
        title: cap,
        body: h("div", { class: "stack-sm" }, h("img", { class: "lightbox-img", src, alt: cap }),
          f ? kv([["Figure", f.figure_id || "–"], ["Class", f.class || "–"], ["Pages", fmtPages(f.pages) || "–"], ["Section", f.section || "–"],
            ["AI description", f.has_description ? "yes (labelled AI-generated in read_section)" : "no"]], "kv-compact") : null,
          h("a", { href: src, target: "_blank", rel: "noopener" }, "Open the original image")),
      }));
      return b;
    });
    return h("div", { class: "stack-sm" }, h("div", { class: "small muted" }, `${images.length} image${images.length === 1 ? "" : "s"}; captions come from the chunks' figure records.`),
      h("div", { class: "gallery" }, items));
  }

  async function tabQuality() {
    const files = doc.files || [];
    const summary = doc.quality || {};
    const parts = [];
    if (Object.keys(summary).length) parts.push(h("section", { class: "stack-sm" }, h("h3", null, "Summary (registry)"), qualityTiles(summary)));
    if (!files.includes("quality.json")) {
      parts.push(alertBox("info", "No quality.json for this result",
        "Results imported from an existing output folder do not include one. Documents converted by the service have word "
        + "coverage per page, table checks, figure counts and stage results here."));
      return h("div", { class: "stack" }, parts);
    }
    const res = await get(PATHS.file, { params: { doc_id: docId, name: "quality.json" } });
    parts.push(qualityReport(res.data || {}));
    return h("div", { class: "stack" }, parts);
  }

  async function tabFiles() {
    const listing = await fileList();
    const files = listing.files || [];
    const images = listing.images || [];
    const aliases = listing.aliases || {};
    const back = new Map();
    for (const [alias, name] of Object.entries(aliases)) back.set(name, [...(back.get(name) || []), alias]);
    return h("div", { class: "stack" },
      h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
        h("thead", null, h("tr", null, h("th", null, "File"), h("th", null, "Short names"), h("th", null, ""))),
        h("tbody", null, files.map((name) => h("tr", null,
          h("td", { "data-label": "File", class: "mono" }, name),
          h("td", { "data-label": "Short names", class: "small muted" }, (back.get(name) || []).join(", ") || "–"),
          h("td", { "data-label": "" }, h("a", { class: "btn btn-xs", href: fileUrl(docId, name), target: "_blank", rel: "noopener" }, icon("external"), "Open"))))))),
      h("div", { class: "small muted" }, `${images.length} image file(s) are served under their folder name (see the Images tab). `
        + "GET /documents/{doc_id}/files lists every file the service exposes; the source PDF and the logs are not served."));
  }

  // ---- helpers
  cleanups.push(onJobFinal((job) => { if (job && job.doc_id === docId) load(); }));
  current = { docId, select };
  load();
  return () => {
    disposed = true;
    if (current && current.docId === docId) current = null;
    cleanups.forEach((fn) => fn());
  };
}

function actionButton(label, iconName, fn, cls) {
  const b = button(label, { class: ["btn", cls] }, iconName);
  b.addEventListener("click", async () => {
    b.disabled = true;
    try { await fn(); } finally { b.disabled = false; }
  });
  return b;
}

function markText(text, q) {
  if (!q) return text;
  const i = text.toLowerCase().indexOf(q);
  if (i < 0) return text;
  return [text.slice(0, i), h("mark", null, text.slice(i, i + q.length)), text.slice(i + q.length)];
}

function relatedCard(doc) {
  const related = doc.related || [];
  const same = doc.same_text_as || [];
  if (!related.length && !same.length) return null;
  const link = (id) => {
    const other = findDoc(id);
    return h("a", { href: `#/doc/${id}` }, other ? docLabel(other).title : shortId(id, 12), h("span", { class: "tiny faint mono" }, ` ${shortId(id, 8)}`));
  };
  if (!state.docsLoaded) refreshDocs();
  return h("section", { class: "card" }, h("h2", null, "Related documents"),
    related.length ? h("div", { class: "stack-sm" }, h("div", { class: "small muted" }, "Same NCT or sponsor protocol number, different text: likely another version or amendment."),
      h("ul", { class: "list-bullets" }, related.map((r) => h("li", null, link(r.doc_id), " ", (r.reasons || []).map((x) => badge(x, "violet")))))) : null,
    same.length ? h("div", { class: "stack-sm" }, h("div", { class: "small muted" }, "Same text (text fingerprint), different PDF bytes: kept separate, never merged."),
      h("ul", { class: "list-bullets" }, same.map((id) => h("li", null, link(id), " ", badge("same text", "info"))))) : null);
}

export function qualityTiles(q) {
  if (!q || !Object.keys(q).length) return null;
  const tiles = [];
  const tile = (label, value, cls) => tiles.push(h("div", { class: "tile" }, h("div", { class: "tile-label" }, label), h("div", { class: ["tile-value", cls] }, value)));
  if (q.coverage !== undefined) tile("Word coverage", fmtPct(q.coverage), q.coverage >= 0.97 ? "ok" : "warn");
  if (q.pages_below_97 !== undefined) tile("Pages < 97%", fmtNumber(q.pages_below_97), q.pages_below_97 ? "warn" : "ok");
  if (q.tables !== undefined) tile("Tables", fmtNumber(q.tables));
  if (q.marks_missing !== undefined) tile("Marks missing", fmtNumber(q.marks_missing), q.marks_missing ? "warn" : "ok");
  if (q.figures && typeof q.figures === "object") {
    tile("Figures", `${fmtNumber(q.figures.described ?? 0)} / ${fmtNumber(q.figures.pictures ?? 0)}`);
  }
  if (q.chunks !== undefined) tile("Chunks", fmtNumber(q.chunks));
  for (const [k, v] of Object.entries(q)) {
    if (["coverage", "pages_below_97", "tables", "marks_missing", "figures", "chunks", "stages"].includes(k)) continue;
    if (v === null || typeof v !== "object") tile(k.replace(/_/g, " "), String(v));
  }
  const stages = q.stages && typeof q.stages === "object" ? h("div", { class: "row" }, Object.entries(q.stages).map(([k, v]) =>
    badge(`${k}: ${String(v).replace(/_/g, " ")}`, v === "done" ? "ok" : v === "failed" ? "danger" : "warn"))) : null;
  return h("div", { class: "stack-sm" }, h("div", { class: "tiles" }, tiles), stages,
    q.figures ? h("div", { class: "tiny muted" }, "Figures: described / pictures.") : null);
}

function qualityReport(r) {
  const cov = r.coverage || {};
  const pages = Array.isArray(r.pages) ? r.pages : [];
  const chart = pages.length ? h("div", { class: "stack-sm" },
    h("div", { class: "coverage-bars", role: "img", "aria-label": `Word coverage per page: ${pages.filter((p) => !p.expected && p.coverage < 0.97).length} of ${pages.length} pages below 97%` },
      pages.map((p) => {
        const bar = h("span", { class: ["coverage-bar", p.expected ? "toc" : p.coverage < 0.97 ? "low" : null],
          title: `p. ${p.page}: ${fmtPct(p.coverage)} of ${p.pdf_words} words${p.expected ? " (table of contents, skipped)" : ""}` });
        bar.style.height = `${Math.max(3, Math.round((p.coverage || 0) * 100))}%`;
        return bar;
      })),
    h("div", { class: "tiny muted" }, "Word coverage per page (hover for numbers). Amber: below 97%; grey: table-of-contents pages, rebuilt from the headings and not counted.")) : null;
  const low = pages.filter((p) => !p.expected && p.coverage < 0.97);
  const stages = Array.isArray(r.stages) ? r.stages : [];
  return h("div", { class: "stack" },
    h("section", { class: "stack-sm" }, h("h3", null, "Word coverage against the PDF text layer"),
      kv([
        ["Coverage", fmtPct(cov.coverage, 2)],
        ["PDF words checked", fmtNumber(cov.pdf_words)],
        ["Pages checked", fmtNumber(cov.pages_checked)],
        ["TOC pages skipped", fmtNumber(cov.toc_pages_skipped)],
        ["Pages below threshold", fmtNumber(cov.pages_below_threshold)],
        ["Lowest page", fmtPct(cov.lowest_page_coverage)],
        ["Header / footer share", fmtPct(cov.furniture_share)],
      ], "kv-compact"), chart,
      low.length ? h("details", null, h("summary", { class: "small" }, `${low.length} page(s) below 97%`),
        h("ul", { class: "list-bullets small" }, low.map((p) => h("li", null, `p. ${p.page}: ${fmtPct(p.coverage)} of ${p.pdf_words} words`)))) : null),
    r.tables ? h("section", { class: "stack-sm" }, h("h3", null, "Tables"), genericView(r.tables, 1),
      Array.isArray(r.tables_per_table) && r.tables_per_table.length ? h("details", null, h("summary", { class: "small" }, `Per table (${r.tables_per_table.length})`), genericView(r.tables_per_table, 1)) : null) : null,
    r.figures ? h("section", { class: "stack-sm" }, h("h3", null, "Figures"), genericView(r.figures, 1)) : null,
    stages.length ? h("section", { class: "stack-sm" }, h("h3", null, "Stages"), h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, h("th", null, "Stage"), h("th", null, "Status"), h("th", { class: "num" }, "Seconds"), h("th", null, "Detail"))),
      h("tbody", null, stages.map((s) => h("tr", null, h("td", null, s.name), h("td", null, badge(String(s.status).replace(/_/g, " "), s.status === "done" ? "ok" : s.status === "failed" ? "danger" : "warn")),
        h("td", { class: "num" }, fmtSeconds(s.seconds)),
        h("td", { class: "small" }, s.detail && Object.keys(s.detail).length ? Object.entries(s.detail).map(([k, v]) => `${k} ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · ") : "–"))))))) : null,
    Array.isArray(r.warnings) && r.warnings.length ? alertBox("warn", "Warnings", h("ul", { class: "list-bullets small" }, r.warnings.map((w) => h("li", null, String(w))))) : null);
}
