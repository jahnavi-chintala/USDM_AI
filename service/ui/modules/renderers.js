// Readable views of the protocol tool results (protocol_api), API errors and generic JSON.
// ctx: {onSection(id): read a section, onTool(tool, args): run another tool, onContinue(cursor)}.

import { alertBox, badge, button, fmtNumber, fmtPageRange, fmtPages, fmtPct, h, icon, truncate } from "./dom.js";
import { renderMarkdown, sectionChip } from "./markdown.js";

// ---------------------------------------------------------------- shared pieces

export function statusLine(res, extra) {
  if (!res) return null;
  const ok = res.status >= 200 && res.status < 300;
  return h("div", { class: "status-line" },
    h("span", { class: ok ? "http-ok" : "http-err" }, res.status ? String(res.status) : "—"),
    res.ms !== undefined ? h("span", null, `${res.ms} ms`) : null,
    res.method ? h("span", { class: "mono break" }, `${res.method} ${res.url}`) : null,
    extra || null);
}

const ERROR_HINTS = {
  offline: "The service runs with OPENROUTER_OFFLINE=1 and this query is not in the document's search cache, so it "
    + "cannot be embedded or reranked. read_section, find_sections and quote_check are local and work offline.",
  search_unavailable: "The document has no embeddings (it was processed offline). Reprocess it with the service "
    + "online to enable search.",
  not_ready: "The document has no published result yet: wait for its job to finish.",
  network_error: "The page could not reach the service. Check that it is running and reload.",
  document_busy: "A job of this document is running; try again when it has finished.",
  upstream_error: "OpenRouter failed or refused the request (the response body is not passed on).",
  no_api_key: "The service has no OPENROUTER_API_KEY: set it in .env, or run offline.",
  validation_error: "The request body did not match the tool's schema (checked by the typed REST route).",
  invalid_arguments: "The arguments did not match the tool's input schema.",
  invalid_origin: "The service refused this page's Origin (see ALLOWED_HOSTS).",
  invalid_host: "The service refused the Host header (see ALLOWED_HOSTS).",
};

/** An API error: its type and message (never a stack), plus the details the service gave. */
export function errorCard(err, { hint } = {}) {
  const type = err && err.type ? err.type : "error";
  const details = err && err.details;
  const list = details && Array.isArray(details.errors) ? h("ul", { class: "list-bullets small" },
    details.errors.map((e) => h("li", null, h("span", { class: "mono" }, (e.loc || []).join(".")), `: ${e.msg || ""}`))) : null;
  const tools = details && Array.isArray(details.tools) ? h("div", { class: "small muted" }, `Known tools: ${details.tools.join(", ")}`) : null;
  const more = hint || ERROR_HINTS[type];
  return h("div", { class: "alert alert-danger error-card", role: "alert" }, icon("alert"),
    h("div", { class: "alert-body" },
      h("div", { class: "row" }, h("span", { class: "err-type" }, type),
        err && err.status ? h("span", { class: "err-status" }, `HTTP ${err.status}`) : null),
      h("div", { class: "err-message" }, err && err.message ? err.message : "Unknown error"),
      list, tools, more ? h("div", { class: "err-hint" }, more) : null));
}

export function provLine(prov, ctx = {}, { label } = {}) {
  if (!prov || (!prov.section_id && !(prov.pages || []).length && !(prov.refs || []).length)) return null;
  const parts = [];
  if (label) parts.push(h("span", { class: "faint" }, label));
  if (prov.section_id) parts.push(sectionChip(prov.section_id, ctx));
  if (prov.title) parts.push(h("span", { class: "prov-title" }, truncate(prov.title, 90)));
  if (Array.isArray(prov.section_ids) && prov.section_ids.length > 1) {
    parts.push(h("span", { class: "faint" }, `+${prov.section_ids.length - 1} more section(s)`));
  }
  const pages = fmtPages(prov.pages);
  if (pages) parts.push(h("span", { class: "chip chip-page" }, pages));
  if (Array.isArray(prov.refs) && prov.refs.length) {
    parts.push(h("span", { class: "faint mono", title: prov.refs.join(", ") },
      prov.refs.length === 1 ? prov.refs[0] : `${prov.refs.length} refs`));
  }
  return h("div", { class: "prov" }, parts);
}

function roleChip(role, score) {
  if (!role) return null;
  const weak = typeof score === "number" && score < 0.7;
  return h("span", { class: ["chip", "chip-role", weak ? "weak" : null],
    title: typeof score === "number" ? `role score ${score}${weak ? " (inherited or weak)" : ""}` : "ICH M11 role" }, role);
}

function warnings(list) {
  if (!Array.isArray(list) || !list.length) return null;
  return alertBox("warn", `${list.length} warning${list.length === 1 ? "" : "s"}`,
    h("ul", { class: "list-bullets small" }, list.map((w) => h("li", null, typeof w === "string" ? w : JSON.stringify(w)))));
}

function section(title, ...children) {
  const body = children.flat().filter(Boolean);
  if (!body.length) return null;
  return h("section", { class: "stack-sm" }, h("h4", { class: "section-title" }, title), ...body);
}

function scalar(v, ctx) {
  if (v === null || v === undefined || v === "") return h("span", { class: "faint" }, "–");
  if (typeof v === "boolean") return badge(String(v), v ? "ok" : "outline");
  if (typeof v === "object") {
    if ("value" in v) return valueWithSource(v, ctx);
    return genericView(v, 2);
  }
  return String(v);
}

function valueWithSource(entry, ctx) {
  if (!entry || typeof entry !== "object") return scalar(entry, ctx);
  const src = entry.source || {};
  return h("div", { class: "stack-sm" },
    h("div", null, h("strong", null, String(entry.value ?? "–")), entry.method ? h("span", { class: "small muted" }, ` (${entry.method})`) : null),
    src.text ? h("div", { class: "id-source" }, "“", highlightQuote(src.text, String(entry.value ?? "")), "”",
      h("span", { class: "small faint" }, [src.page ? ` · p. ${src.page}` : "", src.layer && src.layer !== "body" ? ` · ${src.layer}` : ""].join(""))) : null,
    sourceTag(entry, ctx));
}

/** The item ref of a sourced value's line (tiny, monospace) and, when the tool explorer is there (ctx.onTool), a
 * "Verify" button that runs quote_check on that line's text, also without a ref (an older service) as long as the
 * line has 2 letters or digits. The " / " that joins the lines of a multi-ref source is not in the document: it
 * becomes an elision. Null when there is neither a ref nor a button. */
function sourceTag(entry, ctx) {
  const src = entry && typeof entry === "object" ? entry.source : null;
  if (!src || typeof src !== "object") return null;
  const refs = Array.isArray(src.refs) ? src.refs : [];
  const ref = src.ref || refs[0];
  let quote = String(src.text || "").replace(/^\s*…\s*/, "").replace(/\s*…\s*$/, "");
  if (refs.length > 1) quote = quote.split(" / ").join(" … ");
  const canVerify = Boolean(ctx && ctx.onTool) && (quote.match(/[\p{L}\p{N}]/gu) || []).length >= 2;
  if (!ref && !canVerify) return null;
  let verify = null;
  if (canVerify) {
    verify = toolButton("quote_check", { quote }, ctx, "Verify");
    verify.setAttribute("aria-label", `Verify "${truncate(String(entry.value ?? quote), 60)}" with quote_check`);
  }
  return h("span", { class: "source-tag" },
    ref ? h("span", { class: "tiny faint mono break", title: refs.length > 1 ? `Item refs of the source line: ${refs.join(", ")}` : "Item ref of the source line" },
      typeof src.row === "number" ? `${ref} row ${src.row}` : ref) : null,
    verify);
}

function tile(label, value, cls) {
  return h("div", { class: "tile" }, h("div", { class: "tile-label" }, label), h("div", { class: ["tile-value", cls] }, value));
}

// ---------------------------------------------------------------- quote highlighting

const DASH_RE = /[‐-―−﹘﹣－]/;
const SQUOTE_RE = /[‘’‚‛′`´]/;
const DQUOTE_RE = /[“-‟″]/;

function normChar(c) {
  if (/\s/.test(c)) return " ";
  if (DASH_RE.test(c)) return "-";
  if (SQUOTE_RE.test(c)) return "'";
  if (DQUOTE_RE.test(c)) return '"';
  if (c === "≥") return ">=";
  if (c === "≤") return "<=";
  if (c === "^") return "";
  return c.normalize("NFKC").toLowerCase();
}

function normalize(text, compact) {
  let out = "";
  const map = [];
  let lastSpace = true;
  for (let i = 0; i < text.length; i += 1) {
    let n = normChar(text[i]);
    if (compact) n = n.replace(/[^\p{L}\p{N}<>=%±]/gu, "");
    if (!n) continue;
    if (n === " ") {
      if (lastSpace) continue;
      lastSpace = true;
    } else {
      lastSpace = false;
    }
    for (const ch of n) { out += ch; map.push(i); }
  }
  return { out, map };
}

function cleanQuote(q) {
  return String(q || "").replace(/<!--[\s\S]*?-->/g, " ").replace(/\{#[^}]*\}/g, " ").replace(/^\s*#{1,6}\s+/gm, "")
    .replace(/\|/g, " ").trim();
}

function findRanges(text, quote) {
  const ranges = [];
  for (const part of cleanQuote(quote).split(/\.\.\.|…/)) {
    for (const compact of [false, true]) {
      const q = normalize(part, compact).out.trim();
      if (!q || (compact && q.length < 6)) continue;
      const t = normalize(text, compact);
      let from = 0;
      let found = false;
      while (from <= t.out.length) {
        const at = t.out.indexOf(q, from);
        if (at < 0) break;
        ranges.push([t.map[at], t.map[at + q.length - 1] + 1]);
        found = true;
        from = at + q.length;
      }
      if (found) break;
    }
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const merged = [];
  for (const r of ranges) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1]);
    else merged.push([...r]);
  }
  return merged;
}

/** Nodes of `text` with the quote marked (exact / normalized / compact), else the quote's words marked softly. */
export function highlightQuote(text, quote) {
  const s = String(text ?? "");
  const ranges = quote ? findRanges(s, quote) : [];
  const out = [];
  if (ranges.length) {
    let last = 0;
    for (const [a, b] of ranges) {
      if (a > last) out.push(s.slice(last, a));
      out.push(h("mark", null, s.slice(a, b)));
      last = b;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  }
  const words = new Set((cleanQuote(quote).normalize("NFKC").toLowerCase().match(/[\p{L}\p{N}]+/gu) || []).filter((w) => w.length > 2 || /\d/.test(w)));
  if (!words.size) return [s];
  let last = 0;
  for (const m of s.matchAll(/[\p{L}\p{N}]+/gu)) {
    if (!words.has(m[0].normalize("NFKC").toLowerCase())) continue;
    if (m.index > last) out.push(s.slice(last, m.index));
    out.push(h("span", { class: "hl-soft" }, m[0]));
    last = m.index + m[0].length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}

// ---------------------------------------------------------------- generic

export function genericView(value, depth = 0) {
  if (value === null || value === undefined) return h("span", { class: "faint" }, "null");
  if (typeof value === "boolean") return badge(String(value), value ? "ok" : "outline");
  if (typeof value === "number") return h("span", { class: "mono" }, String(value));
  if (typeof value === "string") {
    return value.length > 160 || value.includes("\n")
      ? h("div", { class: "chunk-text small" }, value) : h("span", { class: "break" }, value);
  }
  if (Array.isArray(value)) {
    if (!value.length) return h("span", { class: "faint" }, "[ ]");
    if (value.every((v) => v === null || typeof v !== "object")) {
      return h("div", { class: "chips" }, value.slice(0, 200).map((v) => h("span", { class: "chip chip-role" }, String(v))),
        value.length > 200 ? h("span", { class: "faint small" }, `+${value.length - 200}`) : null);
    }
    if (depth < 4 && value.every((v) => v && typeof v === "object" && !Array.isArray(v))) {
      const keys = [...new Set(value.flatMap((v) => Object.keys(v)))];
      if (keys.length <= 10) {
        return h("div", { class: "table-wrap" }, h("table", { class: "table" },
          h("thead", null, h("tr", null, keys.map((k) => h("th", null, k)))),
          h("tbody", null, value.slice(0, 300).map((row) => h("tr", null, keys.map((k) => h("td", null, compactValue(row[k]))))))));
      }
    }
    return h("ol", { class: "list-bullets" }, value.slice(0, 200).map((v) => h("li", null, genericView(v, depth + 1))));
  }
  if (depth > 4) return h("code", { class: "small break" }, truncate(JSON.stringify(value), 400));
  const entries = Object.entries(value);
  if (!entries.length) return h("span", { class: "faint" }, "{ }");
  const dl = h("dl", { class: "kv kv-compact" });
  for (const [k, v] of entries) dl.append(h("dt", { class: "mono" }, k), h("dd", null, genericView(v, depth + 1)));
  return dl;
}

function compactValue(v) {
  if (v === null || v === undefined) return h("span", { class: "faint" }, "–");
  if (typeof v === "object") return h("code", { class: "small break" }, truncate(JSON.stringify(v), 160));
  if (typeof v === "boolean") return String(v);
  return truncate(String(v), 300);
}

// ---------------------------------------------------------------- per tool

function documentInfo(d, ctx) {
  const titles = d.titles || {};
  const titleEntry = [titles.official_title, titles.detected_title].find((t) => t && t.value);
  const titleVal = titleEntry && titleEntry.value;
  const titleSrc = sourceTag(titleEntry, ctx);
  const withMethod = (t) => `${t.value ?? t}${t.method ? ` (${t.method})` : ""}`; // the style of valueWithSource
  const head = h("div", { class: "stack-sm" },
    h("h3", { class: "break" }, titleVal || "No title detected"),
    titleEntry && (titleEntry.method || titleSrc) ? h("div", { class: "row tiny muted" },
      titleEntry.method ? `(${titleEntry.method})` : null, titleSrc) : null,
    h("div", { class: "row small muted" },
      titles.brief_title ? h("span", null, `Brief title: ${withMethod(titles.brief_title)}`) : null,
      sourceTag(titles.brief_title, ctx),
      titles.acronym ? h("span", null, `Acronym: ${withMethod(titles.acronym)}`) : null,
      sourceTag(titles.acronym, ctx),
      !titles.official_title && titles.detected_title ? badge("detected title (no official title label)", "warn") : null));

  const ids = d.identifiers || {};
  const found = Object.entries(ids).filter(([, v]) => v && typeof v === "object");
  const missing = Object.entries(ids).filter(([, v]) => !v).map(([k]) => k);
  const idTable = found.length ? h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
    h("thead", null, h("tr", null, h("th", null, "Identifier"), h("th", null, "Value"), h("th", null, "Verbatim line (source)"), h("th", null, "Where"))),
    h("tbody", null, found.map(([key, v]) => {
      const src = v.source || {};
      return h("tr", null,
        h("td", { "data-label": "Identifier" }, v.label || key, h("div", { class: "tiny faint mono" }, key)),
        h("td", { "data-label": "Value" }, h("strong", { class: "mono" }, String(v.value ?? "–")),
          v.method ? h("div", { class: "tiny muted" }, v.method) : null),
        h("td", { "data-label": "Verbatim line" }, src.text ? h("span", { class: "id-source" }, highlightQuote(src.text, String(v.value ?? ""))) : h("span", { class: "faint" }, "–"),
          sourceTag(v, ctx)),
        h("td", { "data-label": "Where", class: "nowrap" }, src.page ? h("span", { class: "chip chip-page" }, `p. ${src.page}`) : null, " ",
          src.section_id ? sectionChip(src.section_id, ctx) : null, src.layer && src.layer !== "body" ? badge(src.layer, "outline") : null));
    })))) : h("div", { class: "muted small" }, "No registry or sponsor identifier found.");

  const version = d.version || {};
  const versionRows = Object.entries(version).map(([k, v]) => [k.replace(/_/g, " "), scalar(v, ctx)]);
  const sponsor = d.sponsor || null;
  const cover = d.cover_page && typeof d.cover_page === "object" ? Object.entries(d.cover_page) : [];
  const roles = d.roles_found || {};
  const soaTables = d.soa_tables || [];
  const other = d.other_identifiers_cited && Object.keys(d.other_identifiers_cited).length ? d.other_identifiers_cited : null;

  return h("div", { class: "stack" },
    head,
    h("div", { class: "tiles" }, tile("Pages", fmtNumber(d.page_count)), tile("Sections", fmtNumber(d.section_count)),
      tile("SoA tables", fmtNumber(d.soa_table_count)), tile("Identifiers", String(found.length))),
    section("Identifiers (StudyIdentifier)", idTable,
      missing.length ? h("div", { class: "small muted" }, `Not found: ${missing.join(", ")}`) : null),
    other ? section("Other identifiers cited", genericView(other, 1)) : null,
    h("div", { class: "grid-2" },
      section("Version (StudyVersion)", h("dl", { class: "kv kv-compact" }, versionRows.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, v)]))),
      sponsor ? section("Sponsor (Organization)", valueWithSource({ ...sponsor, value: sponsor.name || sponsor.value }, ctx),
        provLine(sponsor.provenance, ctx)) : null),
    cover.length ? section("Cover page label / value pairs", h("dl", { class: "kv kv-compact" },
      cover.flatMap(([k, v]) => [h("dt", null, k), h("dd", null, scalar(v, ctx))]))) : null,
    soaTables.length ? section("Schedule of Activities tables", h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, h("th", null, "Table"), h("th", null, "Section"), h("th", null, "Pages"), h("th", { class: "num" }, "Size"), h("th", null, ""))),
      h("tbody", null, soaTables.map((t) => h("tr", null,
        h("td", { class: "mono" }, t.table_id), h("td", null, t.section_id ? sectionChip(t.section_id, ctx) : "–", t.caption ? ` ${truncate(t.caption, 60)}` : ""),
        h("td", null, fmtPages(t.pages)), h("td", { class: "num" }, `${t.rows ?? "?"} × ${t.cols ?? "?"}`),
        h("td", null, ctx.onTool ? toolButton("get_table", { table: t.table_id, format: "all" }, ctx) : null))))))) : null,
    Object.keys(roles).length ? section("Sections by ICH M11 role", h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("tbody", null, Object.entries(roles).map(([role, sids]) => h("tr", null,
        h("td", { class: "nowrap" }, roleChip(role)),
        h("td", null, h("div", { class: "chips" }, (Array.isArray(sids) ? sids : [sids]).map((s) => sectionChip(s, ctx)))))))))) : null,
    Array.isArray(d.reading_order_repairs) && d.reading_order_repairs.length
      ? section(`Reading-order repairs made at read time (${d.reading_order_repairs.length})`, genericView(d.reading_order_repairs, 1)) : null,
    warnings(d.warnings),
    provLine(d.provenance, ctx, { label: "Provenance:" }));
}

function toolButton(tool, args, ctx, label) {
  const b = button(label || tool, { class: "btn-xs" }, "play");
  b.title = `Run ${tool} ${JSON.stringify(args)}`;
  b.addEventListener("click", () => ctx.onTool(tool, args));
  return b;
}

function outline(d, ctx) {
  const sections = Array.isArray(d.sections) ? d.sections : [];
  const idOf = (s) => s.section_id ?? s.id; // id / parent are the deprecated names of section_id / parent_id
  const parentOf = (s) => s.parent_id ?? s.parent;
  const byId = new Map(sections.map((s) => [idOf(s), s]));
  const roots = sections.filter((s) => !parentOf(s) || !byId.has(parentOf(s)));
  const node = (s) => {
    const kids = (s.children || []).map((c) => byId.get(c)).filter(Boolean);
    const row = h("span", { class: "tree-row" },
      s.number ? h("span", { class: "tree-num" }, s.number) : null,
      h("span", { class: "tree-title" }, s.number && (s.title || "").startsWith(s.number)
        ? s.title.slice(s.number.length).replace(/^\.?\s*/, "") : (s.title || idOf(s))),
      roleChip(s.role, s.role_score),
      h("span", { class: "tree-meta" }, [fmtPageRange(s.pages), `${fmtNumber(s.tokens)} tok`,
        s.tables ? `${s.tables} table${s.tables === 1 ? "" : "s"}` : null, s.figures ? `${s.figures} fig.` : null].filter(Boolean).join(" · ")),
      ctx.onSection ? sectionChip(idOf(s), ctx) : null);
    if (!kids.length) return h("li", { class: "tree-node leaf" }, row);
    const det = h("details", { open: (s.level ?? 1) <= 1 }, h("summary", null, row),
      h("ul", null, kids.map(node)));
    return h("li", { class: "tree-node" }, det);
  };
  return h("div", { class: "stack" },
    h("div", { class: "row small muted" }, `${fmtNumber(d.count)} of ${fmtNumber(d.total_sections)} sections`,
      d.total_tokens ? ` · ${fmtNumber(d.total_tokens)} tokens in total` : "", d.page_count ? ` · ${d.page_count} pages` : ""),
    roots.length ? h("ul", { class: "tree" }, roots.map(node)) : h("div", { class: "muted" }, "No sections."),
    d.roles ? h("details", null, h("summary", { class: "small" }, "What each role holds (USDM mapping)"),
      h("dl", { class: "kv kv-compact small" }, Object.entries(d.roles).flatMap(([k, v]) => [h("dt", null, roleChip(k)), h("dd", null, String(v))]))) : null,
    warnings(d.warnings));
}

function findSections(d, ctx) {
  const list = Array.isArray(d.sections) ? d.sections : [];
  return h("div", { class: "stack" },
    h("div", { class: "row small muted" }, `${list.length} section${list.length === 1 ? "" : "s"}`,
      d.role ? h("span", null, " for role ", roleChip(d.role)) : null, d.query ? ` matching “${d.query}”` : ""),
    list.length ? h("ol", { class: "list-plain stack-sm" }, list.map((s, i) => h("li", { class: "location" },
      h("div", { class: "row" }, h("span", { class: "rank" }, String(i + 1)),
        sectionChip(s.section_id, ctx), h("strong", { class: "break" }, s.title || s.number || s.section_id),
        roleChip(s.role, s.role_score), s.root_of_role ? badge("root of role", "info", { title: "read_section(role) starts here" }) : null,
        typeof s.score === "number" ? h("span", { class: "tiny muted" }, `score ${s.score}`) : null),
      s.path ? h("div", { class: "small muted break" }, s.path) : null,
      h("div", { class: "row tiny muted" }, s.tokens !== undefined ? `${fmtNumber(s.tokens)} tokens` : null,
        s.provenance ? ` · ${fmtPages(s.provenance.pages)}` : null)))) : h("div", { class: "muted" }, "No section found."),
    warnings(d.warnings));
}

function readSection(d, ctx) {
  const part = d.part || {};
  const head = h("div", { class: "row" },
    d.mode ? badge(`mode: ${d.mode}`, "outline") : null,
    d.role ? roleChip(d.role) : null,
    d.section_id ? sectionChip(d.section_id, ctx) : null,
    d.cursor ? badge("continues: follow cursor", "warn")
      : badge(d.complete ? "complete in one part" : "last part: section complete", "ok"),
    part.total_blocks !== undefined ? h("span", { class: "small muted" },
      `blocks ${part.first_block}–${Math.max(part.first_block, (part.end_block ?? 1) - 1)} of ${part.total_blocks}`) : null,
    d.tokens !== undefined ? h("span", { class: "small muted" }, `${fmtNumber(d.tokens)}${d.total_tokens ? ` / ${fmtNumber(d.total_tokens)}` : ""} tokens`) : null);
  const matched = Array.isArray(d.matched_sections) && d.matched_sections.length > 1
    ? h("div", { class: "row small" }, h("span", { class: "muted" }, "Sections with this role:"), d.matched_sections.map((s) => sectionChip(s, ctx))) : null;
  const excluded = Array.isArray(d.excluded_subsections) && d.excluded_subsections.length
    ? alertBox("info", `${d.excluded_subsections.length} subsection(s) of another role not included`,
      h("div", { class: "chips" }, d.excluded_subsections.map((s) => typeof s === "string" ? sectionChip(s, ctx)
        : h("span", { class: "row" }, sectionChip(s.section_id || s.id, ctx), h("span", { class: "small" }, truncate(s.title || "", 60)))))) : null;
  const blocks = Array.isArray(d.blocks) && d.blocks.length ? h("details", null,
    h("summary", { class: "small" }, `Per-block provenance (${d.blocks.length} blocks)`),
    h("div", { class: "table-wrap" }, h("table", { class: "table" },
      h("thead", null, h("tr", null, ["#", "kind", "section", "pages", "ref"].map((c) => h("th", null, c)))),
      h("tbody", null, d.blocks.map((b) => h("tr", null, h("td", { class: "num" }, String(b.block)), h("td", null, b.kind),
        h("td", null, b.section_id ? sectionChip(b.section_id, ctx) : "–"), h("td", { class: "nowrap" }, fmtPages(b.pages)),
        h("td", { class: "mono small" }, b.ref || (b.table_id || b.figure_id || "–")))))))) : null;
  return h("div", { class: "stack" }, head, matched, excluded, warnings(d.warnings),
    h("div", { class: "card" }, renderMarkdown(d.markdown || "", { onSection: ctx.onSection })),
    blocks,
    d.cursor ? h("div", { class: "small muted" }, "cursor: ", h("code", null, d.cursor)) : null);
}

function tableGrid(fr) {
  const grid = Array.isArray(fr.grid) ? fr.grid : [];
  const hr = fr.header_rows || 0;
  return h("div", { class: "table-wrap" }, h("table", { class: "table" },
    hr ? h("thead", null, grid.slice(0, hr).map((row) => h("tr", null, row.map((c) => h("th", null, String(c ?? "")))))) : null,
    h("tbody", null, grid.slice(hr).map((row) => h("tr", null, row.map((c) => h("td", null, String(c ?? ""))))))));
}

function getTable(d, ctx) {
  const frags = Array.isArray(d.fragments) ? d.fragments : [];
  return h("div", { class: "stack" },
    h("div", { class: "row" }, h("strong", null, d.caption || "Table without caption"),
      (d.table_ids || [d.table_id]).filter(Boolean).map((t) => h("span", { class: "chip" }, t)),
      d.role ? roleChip(d.role) : null, d.glossary ? badge("glossary table", "info") : null,
      h("span", { class: "small muted" }, `${d.rows ?? "?"} rows × ${d.cols ?? "?"} columns`)),
    d.section ? h("div", { class: "small muted break" }, d.section) : null,
    provLine(d.provenance, ctx),
    frags.length > 1 ? h("div", { class: "small muted" }, `${frags.length} page-split fragments joined`) : null,
    frags.map((fr, i) => h("section", { class: "stack-sm" },
      frags.length > 1 ? h("h4", { class: "section-title" }, `Fragment ${i + 1}: ${fr.table_id} · ${fmtPages(fr.pages)}`) : null,
      Array.isArray(fr.grid) ? tableGrid(fr) : null,
      Array.isArray(fr.row_lines) ? h("ol", { class: "list-plain stack-sm" }, fr.row_lines.map((line) => h("li", { class: "small" }, h("code", { class: "break" }, line)))) : null,
      Array.isArray(fr.merged_cells) && fr.merged_cells.length ? h("details", null, h("summary", { class: "small" }, `${fr.merged_cells.length} merged cell(s)`), genericView(fr.merged_cells, 1)) : null)),
    !frags.some((f) => Array.isArray(f.grid) || Array.isArray(f.row_lines)) && d.markdown ? renderMarkdown(d.markdown) : null,
    Array.isArray(d.footnotes) && d.footnotes.length ? section("Notes", h("ul", { class: "list-bullets small" }, d.footnotes.map((n) => h("li", null, String(n))))) : null);
}

function fnMarks(markers) {
  if (!Array.isArray(markers) || !markers.length) return null;
  return h("sup", { class: "fn", title: `footnote ${markers.join(", ")}` }, markers.join(","));
}

/** The header cells of a visit as printed, top to bottom: get_soa's header_cells, else just header_text. */
function printedCells(v) {
  const cells = (Array.isArray(v.header_cells) ? v.header_cells : []).filter((c) => c && typeof c === "object");
  return v.header_text ? (cells.length ? cells : [{ text: v.header_text }]) : [];
}

/** A visit header's tooltip: header path, window, epoch, and with get_soa's header_cells the cells as printed (a
 * unit get_soa put before a bare number says where it came from). */
function visitTitle(v) {
  const lines = [Array.isArray(v.header_path) ? v.header_path.join(" > ") : "", v.window ? `window ${v.window}` : "",
    v.epoch_hint ? `epoch ${v.epoch_hint}` : "", v.label_composed ? "label composed by get_soa" : ""];
  const printed = printedCells(v);
  if (printed.length) {
    lines.push("header cells as printed, top to bottom:", ...printed.map((c) => `  "${c.text ?? ""}"${c.unit_from
      ? ` (unit "${c.unit ?? ""}" from the row's "${c.unit_from.text ?? ""}" cell)` : ""}`));
  }
  return lines.filter(Boolean).join("\n") || v.label;
}

/** The same cells for screen readers (the tooltip is not read): visually hidden text, last in the header and set off
 * by a separator so that a reader does not run it into the label or a footnote marker. */
function printedForReaders(v) {
  const printed = printedCells(v);
  return printed.length ? h("span", { class: "visually-hidden" },
    `; ${printed.length > 1 ? "header cells" : "header cell"} as printed: ${printed.map((c) => c.text ?? "").join(", ")}`) : null;
}

export function soaGrid(soa) {
  const visits = soa.visits || [];
  const acts = soa.activities || [];
  const cells = new Map((soa.cells || []).map((c) => [`${c.activity}|${c.visit}`, c]));
  const thead = h("thead", null, h("tr", null, h("th", { class: "corner", scope: "col" }, "Activity \\ Visit"),
    visits.map((v) => h("th", { scope: "col", title: visitTitle(v) },
    v.label,
    v.label_composed ? h("span", { class: "composed-mark", role: "img", "aria-label": "label composed by get_soa" }, "≈") : null,
    fnMarks(v.footnote_markers),
    v.timing && v.timing !== v.label ? h("span", { class: "visit-sub" }, v.timing) : null,
    v.window ? h("span", { class: "visit-sub" }, v.window) : null,
    printedForReaders(v)))));
  const tbody = h("tbody", null, acts.map((a) => h("tr", null,
    h("th", { class: "act", scope: "row", title: a.notes || "" }, a.label, fnMarks(a.footnote_markers),
      a.section ? h("span", { class: "visit-sub" }, a.section) : null),
    visits.map((v) => {
      const c = cells.get(`${a.id}|${v.id}`);
      if (!c) return h("td", null, "");
      const text = c.text && c.text.trim() ? c.text.replace(/\^[^\s]*/g, "") : c.mark ? "✓" : "";
      return h("td", { class: c.mark ? "on" : null, title: [c.text, c.qualifier].filter(Boolean).join(" · ") },
        text, fnMarks(c.footnote_markers), c.qualifier ? h("span", { class: "qual" }, c.qualifier) : null);
    }))));
  const grid = h("div", { class: "soa-wrap", tabindex: "0", role: "region", "aria-label": "Schedule of Activities grid" },
    h("table", { class: "soa" }, thead, tbody));
  if (!visits.some((v) => v.label_composed)) return grid;
  return h("div", { class: "stack-sm" }, grid, h("div", { class: "tiny muted" },
    h("span", { class: "composed-mark", "aria-hidden": "true" }, "≈"), " label composed by get_soa: hover the column header for the header cells as printed"));
}

function footnoteList(notes) {
  const entries = notes && typeof notes === "object" ? Object.entries(notes) : [];
  if (!entries.length) return null;
  return h("ol", { class: "footnotes" }, entries.map(([k, v]) => h("li", null, h("span", { class: "fn-mark" }, k), h("span", null, String(v)))));
}

function getSoa(d, ctx) {
  if (!d.available) {
    return h("div", { class: "stack" }, alertBox("warn", "No structured Schedule of Activities",
      "soa.py found no SoA table in this document. Read it with read_section('soa') and get_table(...)."),
    ctx.onTool ? h("div", { class: "row" }, toolButton("read_section", { section: "soa" }, ctx, "read_section soa")) : null,
    warnings(d.warnings));
  }
  const soas = d.soas || [];
  return h("div", { class: "stack" },
    h("div", { class: "row small muted" }, `${soas.length} SoA table${soas.length === 1 ? "" : "s"}`),
    soas.map((s, i) => h("section", { class: "stack-sm" },
      h("div", { class: "row" }, h("strong", null, s.caption || `Schedule of Activities ${soas.length > 1 ? i + 1 : ""}`),
        (s.table_ids || [s.table_id]).filter(Boolean).map((t) => h("span", { class: "chip" }, t)),
        h("span", { class: "small muted" }, `${(s.visits || []).length} visits · ${(s.activities || []).length} activities · ${(s.cells || []).filter((c) => c.mark).length} marked cells`),
        ctx.onTool && s.table_id ? toolButton("get_table", { table: s.table_id, format: "rows" }, ctx, "get_table rows") : null),
      provLine(s.provenance, ctx),
      soaGrid(s),
      footnoteList(s.footnotes) ? section("Footnotes", footnoteList(s.footnotes)) : null,
      Array.isArray(s.header_notes) && s.header_notes.length ? section("Header notes", h("ul", { class: "list-bullets small" }, s.header_notes.map((n) => h("li", null, typeof n === "string" ? n : JSON.stringify(n))))) : null,
      Array.isArray(s.section_rows) && s.section_rows.length ? h("details", null, h("summary", { class: "small" }, `${s.section_rows.length} section row(s)`), genericView(s.section_rows, 1)) : null,
      warnings(s.warnings),
      s.markdown ? h("details", null, h("summary", { class: "small" }, "Markdown rendering"), renderMarkdown(s.markdown)) : null)),
    warnings(d.warnings));
}

function search(d, ctx) {
  const blocks = d.blocks || [];
  const hits = d.hits || [];
  const defs = d.definitions || [];
  const ident = d.identity;
  return h("div", { class: "stack" },
    h("div", { class: "row small muted" }, h("span", null, "Query: ", h("strong", { class: "break" }, d.query || "")),
      d.expanded_query && d.expanded_query !== d.query ? h("span", null, ` · expanded: ${d.expanded_query}`) : null,
      ` · k ${d.k ?? "?"} · ${fmtNumber(d.tokens)} / ${fmtNumber(d.budget)} tokens`),
    ident ? h("details", { class: "block-card" }, h("summary", { class: "small" }, "Identity block (always first in the context)"),
      h("div", { class: "block-body" }, h("div", { class: "chunk-text" }, ident.text || ""))) : null,
    blocks.length ? h("ol", { class: "list-plain stack-sm" }, blocks.map((b) => h("li", { class: "block-card" },
      h("header", null, h("span", { class: "rank" }, String(b.n ?? "")), badge(b.label || b.kind, "info"),
        h("span", { class: "break" }, b.section || ""), b.pages ? h("span", { class: "chip chip-page" }, fmtPages(b.pages)) : null,
        b.tokens !== undefined ? h("span", { class: "tiny muted" }, `${b.tokens} tok`) : null,
        b.note ? h("span", { class: "tiny muted" }, b.note) : null),
      h("div", { class: "block-body stack-sm" }, renderMarkdown(b.text || ""), provLine(b.provenance, ctx))))) : h("div", { class: "muted" }, "No context blocks."),
    hits.length ? h("details", null, h("summary", { class: "small" }, `${hits.length} retrieved passages (scores)`),
      h("div", { class: "table-wrap" }, h("table", { class: "table" },
        h("thead", null, h("tr", null, ["rank", "chunk", "score", "dense", "bm25", "section", "pages"].map((c) => h("th", null, c)))),
        h("tbody", null, hits.map((x) => h("tr", null, h("td", { class: "num" }, String(x.rank)), h("td", { class: "mono small" }, x.chunk_id),
          h("td", { class: "num" }, String(x.score)), h("td", { class: "num" }, String(x.dense_rank ?? "–")), h("td", { class: "num" }, String(x.bm25_rank ?? "–")),
          h("td", null, x.section_id ? sectionChip(x.section_id, ctx) : "–"), h("td", null, fmtPages(x.pages)))))))) : null,
    defs.length ? section("Definitions included", h("dl", { class: "kv kv-compact" }, defs.flatMap((x) => [
      h("dt", { class: "mono" }, x.abbreviation), h("dd", null, x.definition, x.inferred_from_text ? badge("inferred", "outline") : null)]))) : null,
    Array.isArray(d.degraded) && d.degraded.length ? alertBox("warn", "Degraded retrieval", genericView(d.degraded, 1)) : null);
}

function abbreviation(d, ctx) {
  const matches = d.matches || [];
  return h("div", { class: "stack" },
    h("div", { class: ["verdict", d.found ? "ok" : "bad"] }, icon(d.found ? "check" : "x"),
      d.found ? `“${d.term}”: ${matches.length} match${matches.length === 1 ? "" : "es"}` : `“${d.term}” is not in the glossary`),
    matches.length ? h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
      h("thead", null, h("tr", null, ["Abbreviation", "Definition", "Match", "Source", "Where"].map((c) => h("th", null, c)))),
      h("tbody", null, matches.map((m) => h("tr", null,
        h("td", { "data-label": "Abbreviation", class: "mono" }, h("strong", null, m.abbreviation)),
        h("td", { "data-label": "Definition" }, m.definition, Array.isArray(m.alternatives) && m.alternatives.length
          ? h("div", { class: "small muted" }, `also: ${m.alternatives.map((a) => (a && a.definition) || a).join("; ")}`) : null,
        m.note ? h("div", { class: "small muted" }, m.note) : null),
        h("td", { "data-label": "Match" }, badge(m.match, "outline"), typeof m.score === "number" && m.score < 1 ? h("span", { class: "tiny muted" }, ` ${m.score}`) : null),
        h("td", { "data-label": "Source" }, m.source || "–"),
        h("td", { "data-label": "Where" }, provLine(m.provenance, ctx) || "–")))))) : null,
    Array.isArray(d.did_you_mean) && d.did_you_mean.length ? h("div", { class: "row small" }, h("span", { class: "muted" }, "Did you mean:"),
      d.did_you_mean.map((t) => ctx.onTool ? toolButton("abbreviation", { term: t }, ctx, t) : h("span", { class: "chip" }, t))) : null,
    d.hint ? h("div", { class: "small muted" }, d.hint) : null);
}

function locationView(loc, quote, ctx, cls) {
  const prov = loc.provenance || {};
  return h("li", { class: ["location", cls] },
    h("div", { class: "row" }, prov.section_id ? sectionChip(prov.section_id, ctx) : null,
      prov.title ? h("span", { class: "small break" }, truncate(prov.title, 80)) : null,
      fmtPages(prov.pages) ? h("span", { class: "chip chip-page" }, fmtPages(prov.pages)) : null,
      loc.match ? badge(loc.match, loc.match === "exact" ? "ok" : "info") : null,
      loc.layer && loc.layer !== "body" ? badge(loc.layer, loc.layer === "ai_generated" ? "warn" : "outline") : null,
      loc.kind ? h("span", { class: "tiny muted" }, loc.kind) : null,
      Array.isArray(loc.table_rows) ? h("span", { class: "tiny muted" }, `table row ${loc.table_rows.join(", ")}`) : null),
    h("div", { class: "context" }, highlightQuote(loc.text || "", quote)),
    Array.isArray(prov.refs) && prov.refs.length ? h("div", { class: "tiny faint mono break" }, prov.refs.join(", ")) : null);
}

/** " · 9 in page text · 3 in headers/footers" from count_by_layer ({body, furniture}); a zero part is left out. */
function layerSplit(byLayer) {
  if (!byLayer || typeof byLayer !== "object") return "";
  return [byLayer.body > 0 ? `${byLayer.body} in page text` : null, byLayer.furniture > 0 ? `${byLayer.furniture} in headers/footers` : null]
    .filter(Boolean).map((part) => ` · ${part}`).join("");
}

export function quoteCheck(d, ctx) {
  const quote = d.quote || "";
  const locs = d.locations || [];
  const verdict = h("div", { class: ["verdict", d.found ? "ok" : "bad"] }, icon(d.found ? "check" : "x"),
    d.found ? "Found" : "Not found",
    d.match ? badge(`match: ${d.match}`, "info") : null,
    d.found ? h("span", { class: "small" }, `${d.count ?? locs.length} location${(d.count ?? locs.length) === 1 ? "" : "s"}${layerSplit(d.count_by_layer)}`) : null,
    d.section_id ? h("span", { class: "small" }, "within ", sectionChip(d.section_id, ctx)) : null);
  const parts = Array.isArray(d.parts) ? section("Elided quote: the parts must occur in order, each within 2,000 characters of the previous one", h("ol", { class: "list-plain stack-sm" },
    d.parts.map((p) => h("li", { class: "location" }, h("div", { class: "row" }, badge(p.found ? "found" : "not found", p.found ? "ok" : "danger"),
      p.match ? badge(p.match, "info") : null, h("span", { class: "small break" }, `“${p.part}”`)),
    (p.locations || []).slice(0, 1).map((l) => h("div", { class: "context" }, highlightQuote(l.text || "", p.part))))))) : null;
  const closest = d.closest ? section("Closest passage (fix the quote from it)", h("div", { class: "location" },
    h("div", { class: "row" }, badge(`similarity ${fmtPct(d.closest.similarity, 0)}`, "warn"), provLine(d.closest.provenance, ctx)),
    h("div", { class: "context" }, highlightQuote(d.closest.text || "", quote)),
    h("div", { class: "tiny muted" }, "Highlighted: the quote's words found in this passage."))) : null;
  return h("div", { class: "stack" },
    verdict,
    h("div", { class: "small muted break" }, "Quote: ", h("span", { class: "id-source" }, `“${quote}”`)),
    locs.length ? section("Locations", h("ol", { class: "list-plain stack-sm" }, locs.map((l) => locationView(l, quote, ctx)))) : null,
    d.more_locations ? h("div", { class: "small muted" }, `+${d.more_locations} more location(s) not listed`) : null,
    Array.isArray(d.found_elsewhere) && d.found_elsewhere.length ? section("Found outside the requested section (does not count)",
      h("ol", { class: "list-plain stack-sm" }, d.found_elsewhere.map((l) => locationView(l, quote, ctx, "elsewhere")))) : null,
    Array.isArray(d.found_in_ai_text) && d.found_in_ai_text.length ? section("Found only in AI-generated figure text (not document text)",
      d.warning ? alertBox("warn", null, d.warning) : null,
      h("ol", { class: "list-plain stack-sm" }, d.found_in_ai_text.map((l) => locationView(l, quote, ctx, "elsewhere")))) : null,
    parts,
    closest,
    !d.found && !d.closest && !(d.found_elsewhere || []).length ? h("div", { class: "small muted" },
      "No passage shares at least 60% of the quote's words: the value is probably not in the document.") : null,
    d.found ? provLine(d.provenance, ctx, { label: "Evidence:" }) : null);
}

const RENDERERS = {
  document_info: documentInfo,
  outline,
  find_sections: findSections,
  read_section: readSection,
  get_table: getTable,
  get_soa: getSoa,
  search,
  abbreviation,
  quote_check: quoteCheck,
};

/** The readable view of one tool result (falls back to the generic view, and to it on a rendering bug). */
export function renderToolResult(tool, data, ctx = {}) {
  const fn = RENDERERS[tool];
  if (!fn || !data || typeof data !== "object") return genericView(data);
  try {
    return fn(data, ctx);
  } catch (err) {
    console.error(err);
    return h("div", { class: "stack" }, alertBox("warn", "This result could not be shown in the readable view",
      `${err && err.name ? err.name : "Error"} in the ${tool} renderer; the generic view follows.`), genericView(data));
  }
}
