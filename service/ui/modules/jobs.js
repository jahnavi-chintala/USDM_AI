// Live job progress: GET /jobs/{job_id} is polled while a tracked job is queued or running. The service reports the
// current stage and its done / total; the seconds of each stage arrive with the finished job, so while a job runs
// the elapsed time of a stage is measured here from the first poll that saw it.

import { PATHS, get, post } from "./api.js";
import { badge, button, fmtSeconds, h, icon, parseDate, shortId, toast } from "./dom.js";

export const STAGES = ["hash", "convert", "postprocess", "figures", "save", "chunk", "identity", "embed", "quality"];
export const FINAL = new Set(["done", "done_with_warnings", "failed", "cancelled"]);
const STAGE_HELP = {
  hash: "SHA-256 of the PDF",
  convert: "Docling conversion (layout, tables, OCR of bitmap regions)",
  postprocess: "headings, tables, footnote markers, reading-order repair",
  figures: "figure descriptions (OpenRouter vision model; skipped offline)",
  save: "Markdown, JSON, HTML, images, tables",
  chunk: "chunks, parents, section index, glossary",
  identity: "identity record: titles, sponsor, ids, version (optional model check on OpenRouter; skipped offline)",
  embed: "chunk embeddings (OpenRouter; skipped offline)",
  quality: "word coverage, table and figure checks",
};
const POLL_MS = 1500;

const tracked = new Map(); // job_id -> {job, seen: {stage: ms}, subs: Set, error, finalNotified}
const finalListeners = new Set();
let timer = null;

export function trackJob(jobId, initial) {
  if (!jobId) return null;
  let entry = tracked.get(jobId);
  if (!entry) {
    entry = { job: null, seen: {}, subs: new Set(), error: null, finalNotified: false, pollStarted: Date.now() };
    tracked.set(jobId, entry);
  }
  if (initial) update(entry, initial);
  schedule(0);
  return entry;
}

export function jobState(jobId) {
  const e = tracked.get(jobId);
  return e ? e.job : null;
}

export function activeJobIds() {
  return [...tracked.entries()].filter(([, e]) => !e.job || !FINAL.has(e.job.state)).map(([id]) => id);
}

export function trackedJobIds() {
  return [...tracked.keys()];
}

export function forgetJob(jobId) {
  tracked.delete(jobId);
}

/** fn(job) whenever a tracked job reaches a final state. */
export function onJobFinal(fn) {
  finalListeners.add(fn);
  return () => finalListeners.delete(fn);
}

function update(entry, job) {
  const now = Date.now();
  if (job.stage && !(job.stage in entry.seen)) entry.seen[job.stage] = now;
  entry.job = job;
}

function schedule(delay) {
  if (timer) return;
  timer = setTimeout(poll, delay);
}

async function poll() {
  timer = null;
  const ids = activeJobIds();
  await Promise.all(ids.map(async (id) => {
    const entry = tracked.get(id);
    try {
      const res = await get(PATHS.job, { params: { job_id: id } });
      entry.error = null;
      update(entry, res.data);
    } catch (err) {
      entry.error = err;
      if (err.status === 404) entry.job = { job_id: id, state: "cancelled", missing: true, stages: [], warnings: [] };
    }
    for (const fn of [...entry.subs]) fn(entry);
    if (entry.job && FINAL.has(entry.job.state) && !entry.finalNotified) {
      entry.finalNotified = true;
      for (const fn of [...finalListeners]) {
        try { fn(entry.job); } catch (e) { console.error(e); }
      }
    }
  }));
  if (activeJobIds().length) schedule(POLL_MS);
}

export function stateBadge(stateName) {
  const kinds = { done: "ok", done_with_warnings: "warn", failed: "danger", cancelled: "outline", queued: "info",
    running: "info", none: "outline" };
  const label = (stateName || "none").replace(/_/g, " ");
  return badge(label, kinds[stateName] || "outline", { class: "badge-dot" });
}

/** A self-updating card for one job (it stops listening once removed from the page). */
export function jobCard(jobId, { title, onOpen } = {}) {
  const entry = trackJob(jobId);
  const el = h("article", { class: "job", "aria-live": "polite", "aria-busy": "true" });
  let connectedOnce = false;
  const render = () => {
    if (el.isConnected) connectedOnce = true;
    else if (connectedOnce) { entry.subs.delete(render); return; }
    el.replaceChildren(...renderJob(entry, { title, onOpen }));
    el.setAttribute("aria-busy", entry.job && FINAL.has(entry.job.state) ? "false" : "true");
  };
  entry.subs.add(render);
  render();
  return el;
}

function renderJob(entry, { title, onOpen }) {
  const job = entry.job;
  if (!job) return [h("div", { class: "row small muted" }, h("span", { class: "spinner", "aria-hidden": "true" }), "Loading job…")];
  const final = FINAL.has(job.state);
  const out = [];
  const head = h("div", { class: "job-head" },
    stateBadge(job.state),
    h("span", { class: "job-title" }, title || job.filename || "Job"),
    h("span", { class: "badge badge-outline" }, job.kind || "job"),
    h("span", { class: "tiny muted mono", title: job.job_id }, `job ${shortId(job.job_id, 8)}`));
  const timing = [];
  const created = parseDate(job.created_at);
  const started = parseDate(job.started_at);
  const finished = parseDate(job.finished_at);
  if (job.state === "queued" && created) timing.push(`queued ${fmtSeconds((Date.now() - created) / 1000)}`);
  if (started && created && started - created > 1500) timing.push(`waited ${fmtSeconds((started - created) / 1000)}`);
  if (started) timing.push(`${final ? "ran" : "running"} ${fmtSeconds(((finished || new Date()) - started) / 1000)}`);
  if (timing.length) head.append(h("span", { class: "tiny muted" }, timing.join(" · ")));
  out.push(head);

  if (job.missing) {
    out.push(h("div", { class: "small muted" }, "The job no longer exists (its document was deleted)."));
    return out;
  }
  if (job.kind === "import") {
    out.push(h("div", { class: "small muted" }, "Imported from an existing output folder: no pipeline stages ran."));
  } else {
    out.push(stageBar(job, entry));
  }
  if (job.error) {
    out.push(h("div", { class: "alert alert-danger" }, icon("alert"), h("div", { class: "alert-body" },
      h("div", { class: "alert-title" }, "The job failed"),
      h("div", { class: "mono small" }, `${job.error.type || "error"}${job.error.where ? ` at ${job.error.where}` : ""}`))));
  }
  if (Array.isArray(job.warnings) && job.warnings.length) {
    out.push(h("details", null, h("summary", { class: "small" }, `${job.warnings.length} warning${job.warnings.length === 1 ? "" : "s"}`),
      h("ul", { class: "list-bullets small" }, job.warnings.map((w) => h("li", null, String(w))))));
  }
  if (entry.error && !final) {
    out.push(h("div", { class: "small muted" }, `Polling failed: ${entry.error.type}: ${entry.error.message}. Retrying…`));
  }
  const actions = h("div", { class: "row" });
  if (job.state === "queued") {
    const cancel = button("Cancel job", { class: "btn-sm btn-danger" }, "x");
    cancel.addEventListener("click", async () => {
      cancel.disabled = true;
      try {
        const res = await post(PATHS.cancelJob, { params: { job_id: job.job_id } });
        update(entry, res.data);
        toast("Job cancelled", "ok");
        schedule(0);
      } catch (err) {
        toast(`${err.type}: ${err.message}`, "error");
        cancel.disabled = false;
      }
    });
    actions.append(cancel);
  }
  if (final && onOpen && job.doc_id && (job.state === "done" || job.state === "done_with_warnings")) {
    actions.append(h("a", { class: "btn btn-sm", href: `#/doc/${job.doc_id}` }, icon("doc"), "Open document"));
  }
  if (actions.childNodes.length) out.push(actions);
  return out;
}

function stageBar(job, entry) {
  const done = new Map((job.stages || []).map((s) => [s.name, s]));
  // running without a stage yet: the pipeline's child process is starting (loading the converter)
  const starting = job.state === "running" && !job.stage;
  const current = FINAL.has(job.state) ? null : (job.stage || (starting ? STAGES[0] : null));
  const currentIdx = current ? STAGES.indexOf(current) : -1;
  const list = h("ol", { class: "stagebar", "aria-label": "Pipeline stages" });
  STAGES.forEach((name, idx) => {
    const rec = done.get(name);
    let stateName = "pending";
    let meta = "";
    const li = h("li", { class: "stage", title: `${name}: ${STAGE_HELP[name]}` });
    const fill = h("span", { class: "stage-fill" });
    if (rec) {
      stateName = rec.status === "done" ? "done" : rec.status;
      meta = `${fmtSeconds(rec.seconds)}${rec.status !== "done" ? ` · ${rec.status.replace(/_/g, " ")}` : ""}`;
    } else if (current && name === current) {
      stateName = "current";
      const p = job.progress || {};
      const since = entry.seen[name];
      const elapsed = since ? fmtSeconds((Date.now() - since) / 1000) : "";
      if (p.total) {
        const pct = Math.max(0, Math.min(100, Math.round((100 * (p.done || 0)) / p.total)));
        fill.style.width = `${pct}%`;
        meta = `${p.done ?? 0}/${p.total}${elapsed ? ` · ${elapsed}` : ""}`;
      } else {
        li.classList.add("indeterminate");
        meta = starting ? "starting…" : (elapsed || "running");
      }
    } else if (currentIdx > -1 && idx < currentIdx) {
      stateName = "done";
      const since = entry.seen[name];
      const next = STAGES.slice(idx + 1).map((s) => entry.seen[s]).find(Boolean);
      meta = since && next ? `≈ ${fmtSeconds((next - since) / 1000)}` : "done";
    } else if (FINAL.has(job.state) && !rec) {
      stateName = job.state === "failed" && job.stage === name ? "failed" : "pending";
      meta = stateName === "failed" ? "failed" : "–";
    } else {
      meta = job.state === "queued" ? "queued" : "–";
    }
    li.dataset.state = stateName;
    li.append(h("span", { class: "stage-name" }, name), h("span", { class: "stage-bar" }, fill),
      h("span", { class: "stage-meta" }, meta));
    li.setAttribute("aria-label", `${name}: ${stateName.replace(/_/g, " ")}${meta ? `, ${meta}` : ""}`);
    list.append(li);
  });
  const wrap = h("div", { class: "stack-sm" }, list);
  const total = (job.stages || []).reduce((s, st) => s + (Number(st.seconds) || 0), 0);
  if (FINAL.has(job.state) && total) {
    wrap.append(h("div", { class: "tiny muted" }, `Stage seconds from the service: ${fmtSeconds(total)} in total.`));
  } else if (!FINAL.has(job.state) && job.state === "running") {
    wrap.append(h("div", { class: "tiny muted" }, "Elapsed times are measured by this page from the first poll that "
      + "saw each stage (≈); the service reports exact stage seconds when the job ends."));
  }
  return wrap;
}
