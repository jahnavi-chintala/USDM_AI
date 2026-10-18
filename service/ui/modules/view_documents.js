// Documents: upload (drag and drop or file picker), live jobs, the document list with reprocess and delete.

import { PATHS, del, get, post, uploadPdf } from "./api.js";
import { confirmDialog, confirmOpenRouter, openRouterBadge } from "./dialog.js";
import {
  alertBox, badge, button, emptyState, fmtBytes, fmtNumber, h, icon, kv, shortId, timeEl, toast, copyButton,
} from "./dom.js";
import { FINAL, jobCard, onJobFinal, stateBadge, trackJob, trackedJobIds, jobState, forgetJob } from "./jobs.js";
import { errorCard } from "./renderers.js";
import { docLabel, isOffline, on, refreshDocs, setSelectedDoc, state } from "./state.js";

const shownJobs = new Set(); // job ids shown in the "Jobs" card during this page session

// ---------------------------------------------------------------- shared flows (also used by the document view)

export async function reprocessFlow(doc) {
  const { title } = docLabel(doc);
  const password = h("input", { type: "password", class: "input", id: "reprocess-password", autocomplete: "off" });
  const conf = await confirmOpenRouter("reprocess", {
    title: `Reprocess ${title}?`,
    action: "The service converts the stored source PDF again with the current pipeline. The current result stays "
      + "served until the new one is published.",
    extra: h("div", { class: "field" },
      h("label", { class: "field-label", for: "reprocess-password" }, "Password (only for an encrypted PDF)"),
      password,
      h("div", { class: "field-help" }, "Sent in the request body, held in the service's memory until the job starts; "
        + "never stored or logged, and dropped when nothing is queued.")),
    skipWhenOffline: false,
    confirmLabel: "Reprocess",
    checkboxes: [{
      name: "force",
      label: "Force: reprocess even if the result is current",
      checked: !doc.stale,
      help: doc.stale ? "This result is stale (another pipeline version or fingerprint): it is reprocessed without force."
        : "This result was made by the current pipeline; without force nothing is queued.",
    }],
  });
  const secret = password.value;
  password.value = ""; // read once, also when the dialog was cancelled: the field never keeps it
  if (!conf.ok) return null;
  const body = { force: Boolean(conf.checks.force) };
  if (secret) body.password = secret; // the JSON body only: the service refuses a password in the URL
  try {
    const res = await post(PATHS.reprocess, { params: { doc_id: doc.doc_id }, json: body });
    const d = res.data || {};
    if (d.queued && d.job_id) {
      shownJobs.add(d.job_id);
      trackJob(d.job_id);
      toast(`Reprocess queued (job ${shortId(d.job_id, 8)})`, "ok");
    } else if (d.job_id) {
      shownJobs.add(d.job_id);
      trackJob(d.job_id);
      toast("A job of this document is already queued or running", "info");
    } else {
      toast("Nothing queued: the result is current (tick Force to reprocess anyway)", "warn");
    }
    refreshDocs();
    return d;
  } catch (err) {
    toast(`${err.type}: ${err.message}`, "error", 7000);
    return null;
  } finally {
    delete body.password; // an ApiError keeps its request, and with it this body
  }
}

export async function deleteFlow(doc) {
  const { title, subtitle } = docLabel(doc);
  const conf = await confirmDialog({
    title: `Delete ${title}?`,
    danger: true,
    confirmLabel: "Delete document",
    iconName: "trash",
    body: [
      h("p", { class: "mt-0" }, "This removes the document record, its jobs and its stored files (the source PDF and the "
        + "published result). It cannot be undone."),
      h("div", { class: "small muted break" }, `${subtitle ? `${subtitle} · ` : ""}doc_id ${doc.doc_id}`),
    ],
    checkboxes: [{
      name: "purge",
      label: "Also purge caches",
      checked: false,
      help: "Also removes this PDF's cached Docling conversion batches and its figure descriptions (cache/batches, "
        + "cache/vlm). Without it they stay, as a speed-up for a later upload of the same PDF.",
    }],
  });
  if (!conf.ok) return false;
  try {
    const res = await del(PATHS.document, { params: { doc_id: doc.doc_id }, query: conf.checks.purge ? { purge_caches: "true" } : null });
    const purged = res.data && res.data.purged;
    const extra = purged && typeof purged === "object"
      ? ` · caches purged: ${Object.entries(purged).map(([k, v]) => `${k} ${v}`).join(", ")}` : conf.checks.purge ? " (caches: not reported)" : "";
    toast(`Deleted ${title}${extra}`, "ok", 6000);
    if (state.selectedDoc === doc.doc_id) setSelectedDoc(null);
    await refreshDocs();
    return true;
  } catch (err) {
    toast(`${err.type}: ${err.message}`, "error", 7000);
    return false;
  }
}

/** Same text (text fingerprint) and related (same NCT / sponsor protocol id, other text), from the list itself. */
export function relations(docs) {
  const out = new Map(docs.map((d) => [d.doc_id, { same: [], related: [] }]));
  const byFp = new Map();
  const byKey = new Map();
  for (const d of docs) {
    if (d.text_fingerprint) byFp.set(d.text_fingerprint, [...(byFp.get(d.text_fingerprint) || []), d]);
    for (const k of ["nct", "sponsor_protocol_id"]) {
      const v = (d.identifiers || {})[k];
      if (v) byKey.set(`${k}:${v}`, [...(byKey.get(`${k}:${v}`) || []), d]);
    }
  }
  for (const group of byFp.values()) {
    for (const d of group) out.get(d.doc_id).same.push(...group.filter((o) => o !== d));
  }
  for (const group of byKey.values()) {
    for (const d of group) {
      for (const o of group) {
        if (o === d || (d.text_fingerprint && d.text_fingerprint === o.text_fingerprint)) continue;
        const rel = out.get(d.doc_id).related;
        if (!rel.includes(o)) rel.push(o);
      }
    }
  }
  return out;
}

// ---------------------------------------------------------------- page

export function render(main) {
  const cleanups = [];
  const page = h("div", { class: "stack" });
  main.replaceChildren(page);

  page.append(h("div", { class: "page-head" }, h("div", null, h("h1", null, "Documents"),
    h("p", { class: "lede" }, "Upload clinical trial protocol PDFs, follow their conversion stage by stage, and open the "
      + "results. Each PDF is identified by its SHA-256 (doc_id): the same bytes are never converted twice."))));

  const upload = uploadCard();
  page.append(h("div", { class: "grid-main-side" }, upload.el, serviceCard(cleanups)));

  const jobsBody = h("div", { class: "stack-sm" });
  const clearBtn = button("Clear finished", { class: "btn-sm btn-ghost" });
  const jobsSection = h("section", { class: "card", "aria-labelledby": "jobs-h" },
    h("div", { class: "card-head" }, h("h2", { id: "jobs-h" }, "Jobs"), clearBtn), jobsBody);
  page.append(jobsSection);

  const listBody = h("div");
  const refreshBtn = button("Refresh", { class: "btn-sm" }, "refresh");
  const countEl = h("span", { class: "small muted" });
  page.append(h("section", { class: "card", "aria-labelledby": "docs-h" },
    h("div", { class: "card-head" }, h("div", { class: "row" }, h("h2", { id: "docs-h" }, "Processed and queued documents"), countEl), refreshBtn),
    listBody));

  // ---- jobs card
  const jobCards = new Map();
  function renderJobs() {
    const ids = [...shownJobs];
    jobsSection.hidden = !ids.length;
    for (const [id, el] of jobCards) {
      if (!shownJobs.has(id)) { el.remove(); jobCards.delete(id); }
    }
    for (const id of ids) { // oldest first: each new card is prepended, so the newest ends on top
      if (!jobCards.has(id)) {
        const doc = state.docs.find((d) => d.doc_id === (jobState(id) || {}).doc_id);
        const el = jobCard(id, { title: doc ? docLabel(doc).title : undefined, onOpen: true });
        jobCards.set(id, el);
        jobsBody.prepend(el);
      }
    }
    clearBtn.hidden = !ids.some((id) => FINAL.has((jobState(id) || {}).state));
  }
  clearBtn.addEventListener("click", () => {
    for (const id of [...shownJobs]) {
      if (FINAL.has((jobState(id) || {}).state)) { shownJobs.delete(id); forgetJob(id); }
    }
    renderJobs();
  });
  upload.onJob = (id) => { shownJobs.add(id); trackJob(id); renderJobs(); };

  // jobs already active in the service when the page opens
  (async () => {
    for (const st of ["running", "queued"]) {
      try {
        const res = await get(PATHS.jobs, { query: { state: st, limit: 50 } });
        for (const job of (res.data && res.data.jobs) || []) {
          shownJobs.add(job.job_id);
          trackJob(job.job_id, job);
        }
      } catch (_) { /* the list below shows the error */ }
    }
    for (const id of trackedJobIds()) if (!FINAL.has((jobState(id) || {}).state)) shownJobs.add(id);
    renderJobs();
  })();

  // ---- documents list
  function renderList() {
    if (!state.docsLoaded) { listBody.replaceChildren(h("div", { class: "page-loading" }, h("span", { class: "spinner" }), "Loading documents…")); return; }
    if (state.docsError) { listBody.replaceChildren(errorCard(state.docsError)); return; }
    const docs = state.docs;
    countEl.textContent = docs.length ? `${docs.length}` : "";
    if (!docs.length) {
      listBody.replaceChildren(emptyState("No documents yet. Upload a protocol PDF above.", "doc"));
      return;
    }
    const rel = relations(docs);
    const rows = docs.map((d) => docRow(d, rel.get(d.doc_id)));
    listBody.replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
      h("thead", null, h("tr", null, h("th", null, "Document"), h("th", null, "Status"), h("th", null, "Identifiers"),
        h("th", { class: "num" }, "Pages"), h("th", null, "Updated"), h("th", null, h("span", { class: "visually-hidden" }, "Actions")))),
      h("tbody", null, rows))));
  }

  function docRow(d, rel) {
    const { title, subtitle } = docLabel(d);
    const ids = d.identifiers || {};
    const flags = h("div", { class: "row" }, stateBadge(d.status));
    if (d.stale) flags.append(badge("stale", "warn", { title: "Made by another pipeline version or fingerprint: reprocess to update" }));
    if (d.has_result && d.status !== "done" && d.status !== "done_with_warnings") flags.append(badge("older result served", "outline"));
    if (rel && rel.same.length) flags.append(badge(`same text as ${rel.same.length}`, "info", { title: rel.same.map((o) => docLabel(o).title).join(", ") }));
    if (rel && rel.related.length) flags.append(badge(`related ${rel.related.length}`, "violet", { title: `Same NCT / sponsor protocol number, other text: ${rel.related.map((o) => docLabel(o).title).join(", ")}` }));
    const open = h("a", { class: "btn btn-sm", href: `#/doc/${d.doc_id}` }, "Open");
    const explore = button("Tools", { class: "btn-sm", disabled: !d.has_result, title: "Open the tool explorer on this document" });
    explore.addEventListener("click", () => { setSelectedDoc(d.doc_id); location.hash = "#/tools"; });
    const repro = button("Reprocess", { class: "btn-sm", disabled: d.status === "queued" || d.status === "running" }, "refresh");
    repro.addEventListener("click", () => reprocessFlow(d).then((r) => { if (r && r.job_id) upload.onJob(r.job_id); }));
    const delBtn = button("Delete", { class: "btn-sm btn-danger", disabled: d.status === "running", title: d.status === "running" ? "A job is running" : null }, "trash");
    delBtn.addEventListener("click", () => deleteFlow(d));
    return h("tr", null,
      h("td", { "data-label": "Document" }, h("a", { class: "doc-name", href: `#/doc/${d.doc_id}` }, title),
        h("div", { class: "doc-sub" }, subtitle ? `${subtitle} · ` : "", h("span", { class: "mono", title: d.doc_id }, shortId(d.doc_id, 12)))),
      h("td", { "data-label": "Status" }, flags, d.error ? h("div", { class: "tiny muted mono" }, `${d.error.type || "error"}`) : null),
      h("td", { "data-label": "Identifiers", class: "small" },
        ids.nct ? h("div", { class: "mono" }, ids.nct) : null,
        ids.sponsor_protocol_id ? h("div", { class: "mono" }, ids.sponsor_protocol_id) : null,
        ids.date || ids.version ? h("div", { class: "muted" }, [ids.version, ids.date].filter(Boolean).join(" · ")) : null,
        !ids.nct && !ids.sponsor_protocol_id ? h("span", { class: "faint" }, "–") : null),
      h("td", { "data-label": "Pages", class: "num" }, d.pages ? fmtNumber(d.pages) : "–", h("div", { class: "tiny faint" }, fmtBytes(d.size))),
      h("td", { "data-label": "Updated", class: "small nowrap" }, timeEl(d.updated_at)),
      h("td", { "data-label": "Actions" }, h("div", { class: "doc-actions" }, open, explore, repro, delBtn)));
  }

  refreshBtn.addEventListener("click", () => refreshDocs());
  cleanups.push(on("docs", () => { renderList(); renderJobs(); }));
  cleanups.push(on("version", () => upload.refreshNote()));
  cleanups.push(onJobFinal((job) => {
    renderJobs();
    refreshDocs();
    if (job && !job.missing) toast(`Job ${shortId(job.job_id, 8)} ${String(job.state).replace(/_/g, " ")}`, job.state === "failed" ? "error" : job.state === "done" ? "ok" : "warn");
  }));
  // keep the list fresh while something is queued or running
  const timer = setInterval(() => {
    if (state.docs.some((d) => d.status === "queued" || d.status === "running")) refreshDocs();
  }, 5000);
  cleanups.push(() => clearInterval(timer));

  renderList();
  renderJobs();
  refreshDocs();
  return () => cleanups.forEach((fn) => fn());
}

// ---------------------------------------------------------------- upload card

function uploadCard() {
  const api = { onJob: null, refreshNote: null, el: null };
  const fileInput = h("input", { type: "file", accept: "application/pdf,.pdf", multiple: true, class: "visually-hidden", id: "pdf-input", tabindex: "-1" });
  const zone = h("div", { class: "dropzone", tabindex: "0", role: "button", "aria-describedby": "upload-note",
    "aria-label": "Choose PDF files to upload, or drop them here" },
  icon("upload"), h("div", { class: "dropzone-title" }, "Drop protocol PDFs here"),
  h("div", { class: "small muted" }, "or ", h("span", { class: "btn-link" }, "choose files"), ` · PDF only`));
  const reprocess = h("input", { type: "checkbox", id: "upload-reprocess" });
  const password = h("input", { type: "password", class: "input", id: "upload-password", autocomplete: "off",
    placeholder: "only for encrypted PDFs" });
  const note = h("div", { id: "upload-note", class: "small" });
  const items = h("div", { class: "stack-sm", "aria-live": "polite" });

  function refreshNote() {
    const offline = isOffline();
    const max = state.version && state.version.config ? state.version.config.max_upload_mb : null;
    note.replaceChildren(offline
      ? alertBox("ok", "Service offline", "Nothing is sent to OpenRouter: figures are not described and embeddings are "
        + "skipped, so new documents finish as done_with_warnings and cannot be searched until reprocessed online.")
      : alertBox("violet", "Uploads use OpenRouter",
        "After conversion, the service sends the figure images and the text of every chunk to OpenRouter (zero-data-"
        + "retention routing) for figure descriptions and embeddings. You confirm before anything is uploaded."));
    // replaceChildren would turn a null argument into the text "null"
    if (max) note.append(h("div", { class: "tiny muted" }, `Largest accepted PDF: ${max} MB.`));
  }
  api.refreshNote = refreshNote;

  zone.addEventListener("click", () => fileInput.click());
  zone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("is-over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("is-over");
    if (e.dataTransfer && e.dataTransfer.files.length) handle([...e.dataTransfer.files]);
  });
  fileInput.addEventListener("change", () => {
    const files = [...fileInput.files];
    fileInput.value = "";
    if (files.length) handle(files);
  });

  async function handle(files) {
    const pdfs = files.filter((f) => f.type === "application/pdf" || /\.pdf$/i.test(f.name));
    const skipped = files.length - pdfs.length;
    if (skipped) toast(`${skipped} file(s) skipped: not a PDF`, "warn");
    if (!pdfs.length) return;
    const list = h("ul", { class: "list-bullets small" }, pdfs.map((f) => h("li", null, `${f.name} (${fmtBytes(f.size)})`)));
    const conf = await confirmOpenRouter("upload", {
      title: pdfs.length === 1 ? `Upload ${pdfs[0].name}?` : `Upload ${pdfs.length} PDFs?`,
      action: "The PDF is stored by the service (doc_id = SHA-256 of its bytes) and converted by its worker: Docling "
        + "conversion and OCR run locally in the service.",
      extra: list,
      confirmLabel: "Upload",
    });
    if (!conf.ok) return;
    const options = { reprocess: reprocess.checked, password: password.value };
    password.value = "";
    for (const f of pdfs) await uploadOne(f, options);
    refreshDocs();
  }

  async function uploadOne(file, options) {
    const bar = h("progress", { class: "bar", max: "100", value: "0", "aria-label": `Upload of ${file.name}` });
    const status = h("div", { class: "small muted" }, "Uploading…");
    const item = h("div", { class: "upload-item" }, h("div", { class: "row-between" },
      h("strong", { class: "break" }, file.name), h("span", { class: "tiny muted" }, fmtBytes(file.size))), bar, status);
    items.prepend(item);
    try {
      const res = await uploadPdf(file, { ...options, onProgress: (a, b) => { bar.value = Math.round((100 * a) / b); } });
      bar.remove();
      const d = res.data || {};
      const open = d.doc_id ? h("a", { href: `#/doc/${d.doc_id}` }, "Open document") : null;
      const out = [];
      if (d.queued) {
        out.push(h("div", { class: "row" }, badge("queued", "info"), `Job ${shortId(d.job_id, 8)} queued (HTTP ${res.status}).`));
        if (api.onJob) api.onJob(d.job_id);
      } else if (d.job_id) {
        out.push(h("div", { class: "row" }, badge("already processing", "info"), "These bytes already have a queued or running job.", open));
        if (api.onJob) api.onJob(d.job_id);
      } else if (d.stale) {
        const btn = button("Reprocess now", { class: "btn-sm" }, "refresh");
        btn.addEventListener("click", () => reprocessFlow({ doc_id: d.doc_id, stale: true, identifiers: {}, filenames: [file.name] })
          .then((r) => { if (r && r.job_id && api.onJob) api.onJob(r.job_id); }));
        out.push(h("div", { class: "row" }, badge("stale", "warn"), "A result made by another pipeline version exists; nothing was queued.", btn, open));
      } else {
        out.push(h("div", { class: "row" }, badge("duplicate", "ok"), "Already processed: the existing result is reused (no job).", open));
      }
      for (const w of d.warnings || []) out.push(h("div", { class: "small" }, badge("note", "outline"), ` ${w}`));
      out.push(h("div", { class: "tiny faint mono break" }, `doc_id ${d.doc_id}`));
      status.replaceChildren(...out);
      status.className = "stack-sm small";
    } catch (err) {
      bar.remove();
      status.replaceChildren(errorCard(err));
    }
  }

  refreshNote();
  api.el = h("section", { class: "card", "aria-labelledby": "upload-h" },
    h("div", { class: "card-head" }, h("h2", { id: "upload-h" }, "Add a protocol PDF"), openRouterBadge("upload")),
    h("div", { class: "stack" },
      zone, fileInput,
      h("div", { class: "row" }, h("label", { class: "check", for: "upload-reprocess" }, reprocess,
        h("span", null, "Reprocess a stale result")), h("span", { class: "tiny muted" }, "(when these bytes have a result from another pipeline version)")),
      h("details", null, h("summary", { class: "small" }, "Encrypted PDF?"),
        h("div", { class: "field" }, h("label", { class: "field-label", for: "upload-password" }, "PDF password"), password,
          h("div", { class: "field-help" }, "Sent as a form field with the upload, held in the service's memory until the job starts; never stored or logged."))),
      note, items));
  return api;
}

// ---------------------------------------------------------------- service card

function serviceCard(cleanups) {
  const body = h("div", { class: "stack-sm" });
  const el = h("section", { class: "card", "aria-labelledby": "svc-h" },
    h("div", { class: "card-head" }, h("h2", { id: "svc-h" }, "Service"), h("a", { class: "small", href: "../docs", target: "_blank", rel: "noopener" }, "API reference")),
    body);
  const paint = () => {
    const v = state.version;
    const hl = state.health || {};
    if (!v) {
      body.replaceChildren(state.versionError ? errorCard(state.versionError) : h("div", { class: "page-loading" }, h("span", { class: "spinner" }), "Loading…"));
      return;
    }
    const cfg = v.config || {};
    const jobs = hl.jobs || {};
    body.replaceChildren(
      h("div", { class: "row" }, hl.status === "ok" ? badge("healthy", "ok", { class: "badge-dot" }) : badge(hl.status || "unknown", "danger", { class: "badge-dot" }),
        v.offline ? badge("offline", "outline") : badge("online", "violet"), badge(`pipeline ${v.pipeline_version}`, "outline")),
      kv([
        ["Fingerprint", h("span", { class: "mono" }, v.fingerprint || "–")],
        ["Worker", hl.worker || "–"],
        ["Jobs", Object.keys(jobs).length ? Object.entries(jobs).map(([k, n]) => `${k} ${n}`).join(" · ") : "none"],
        ["Figure model", h("span", { class: "mono small break" }, cfg.figure_model || "(off)")],
        ["Embedding", h("span", { class: "mono small break" }, cfg.embed_model || "–")],
        ["Reranker", h("span", { class: "mono small break" }, cfg.rerank_model || "–")],
        ["API key set", cfg.api_key_set === undefined ? undefined : String(cfg.api_key_set)],
      ], "kv-compact"),
      v.packages ? h("details", null, h("summary", { class: "small" }, "Packages"),
        kv(Object.entries(v.packages).map(([k, x]) => [k, h("span", { class: "mono" }, x || "–")]), "kv-compact")) : null,
      h("div", { class: "row" }, copyButton(() => JSON.stringify(v, null, 2), "Copy /version")));
  };
  cleanups.push(on("version", paint));
  cleanups.push(on("health", paint));
  paint();
  return el;
}
