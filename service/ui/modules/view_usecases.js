// Use cases: guided walkthroughs that run the tools in order on the selected document and say what to take from
// each result. The USDM objects built here are illustrative (USDM 4 class and attribute names, local ids, not
// validated against the USDM schema); the criteria parsing is a heuristic of this page, not a service feature.

import { PATHS, callTool, get } from "./api.js";
import { alertBox, badge, button, fmtNumber, fmtPages, h, icon, truncate } from "./dom.js";
import { jsonBlock } from "./json_view.js";
import { renderMarkdown, sectionChip } from "./markdown.js";
import { errorCard, highlightQuote, provLine, quoteCheck, renderToolResult, soaGrid } from "./renderers.js";
import { docLabel, docPicker, findDoc, on, refreshDocs, state } from "./state.js";

const MADE_UP = "Purple zebras recite algebra beneath the frozen lighthouse every Thursday.";

// ---------------------------------------------------------------- helpers shared by the steps

/** Runs a tool, records the call for the step's call list. */
function toolCaller(ctx, calls) {
  return async (name, args = {}) => {
    calls.push(`POST /documents/{doc_id}/tools/${name} ${JSON.stringify(args)}`);
    const res = await callTool(ctx.docId, name, args);
    return res.data;
  };
}

async function readAll(tool, section, maxTokens) {
  const parts = [];
  let data = await tool("read_section", { section, max_tokens: maxTokens });
  parts.push(data);
  while (data.cursor && parts.length < 60) {
    data = await tool("read_section", { cursor: data.cursor, max_tokens: maxTokens });
    parts.push(data);
  }
  return { parts, markdown: parts.map((p) => p.markdown || "").join("\n\n"), first: parts[0], last: parts[parts.length - 1] };
}

const FAMILIES = [
  { kind: "bracket", re: /^\s*(?:[-*+]\s+)?\[(\d{1,3}[a-z]?)\]\s+(.+)$/ },
  { kind: "code", re: /^\s*(?:[-*+]\s+)?([A-Z]{1,3}\s?\d{1,3}[a-z]?)[.:)]\s+(.+)$/ },
  { kind: "number", re: /^\s*(?:[-*+]\s+)?(\d{1,3})[.)]\s+(.+)$/ },
  { kind: "bullet", re: /^\s*[-*+]\s+(.+)$/ },
];

/** Criteria from read_section Markdown: the marker family of the first marked line splits them (the protocol's own
 * markers become identifiers); other lines (sub-items, notes) belong to the criterion above them. */
export function parseCriteria(markdown) {
  const lines = String(markdown || "").split("\n");
  const skip = (l) => !l.trim() || /^\s*<!--/.test(l) || /^\s*#{1,6}\s/.test(l) || /^\s*\|/.test(l);
  let family = null;
  for (const l of lines) {
    if (skip(l)) continue;
    family = FAMILIES.find((f) => f.re.test(l));
    if (family) break;
  }
  if (!family) return [];
  const out = [];
  let cur = null;
  let n = 0;
  for (const l of lines) {
    if (/^\s*#{1,6}\s/.test(l)) { cur = null; continue; } // a heading ends the list it follows
    const m = l.match(family.re);
    if (m) {
      n += 1;
      const id = family.kind === "bullet" ? String(n) : m[1];
      const text = family.kind === "bullet" ? m[1] : m[2];
      cur = { id, marker: family.kind === "bracket" ? `[${id}]` : id, text: text.trim(), lines: [text.trim()] };
      out.push(cur);
    } else if (cur && l.trim() && !/^\s*<!--/.test(l)) {
      cur.lines.push(l.trim());
    }
  }
  return out;
}

/** A short verbatim span for quote_check: the criterion's first line without Markdown emphasis, at most 30 words. */
function quoteOf(text) {
  const clean = String(text || "").replace(/\*\*|__|`/g, "").replace(/\s+/g, " ").trim();
  const words = clean.split(" ");
  return words.length > 30 ? words.slice(0, 30).join(" ") : clean.replace(/[.;,:]$/, "");
}

/** The first sentence-like line of 6+ words of a read_section part (for the quote_check demo). */
function pickSentence(markdown) {
  const MARKER = /^\s*(?:[-*+]\s+)?(?:\[\d+[a-z]?\]|\(?\d{1,3}[.)]|[a-z][.)])\s+/;
  const lines = String(markdown || "").split("\n");
  for (const markedOnly of [true, false]) { // a criterion line first: that is what an agent extracts
    for (const raw of lines) {
      if (markedOnly && !MARKER.test(raw)) continue;
      const line = raw.replace(/<!--.*?-->|\{#[^}]*\}/g, " ").replace(MARKER, "").trim();
      if (/^#|^\|/.test(line)) continue;
      const words = line.replace(/[*_`]/g, "").split(/\s+/).filter(Boolean);
      if (words.length >= 6 && !/[|^[\]<>]/.test(line)) return words.slice(0, 14).join(" ").replace(/[.;,:]$/, "");
    }
  }
  return null;
}

function alterQuote(q) {
  if (/\d/.test(q)) return q.replace(/\d+/, (n) => String(Number(n) + 5));
  const words = q.split(" ");
  const i = words.findIndex((w, k) => k > 1 && w.length > 4);
  if (i >= 0) words[i] = words[i].endsWith("s") ? "participants" : "participant";
  else words.push("daily");
  return words.join(" ");
}

function takeaway(...content) {
  return h("div", { class: "takeaway" }, h("strong", null, "What to take from this: "), ...content);
}

function miniTable(headers, rows) {
  return h("div", { class: "table-wrap" }, h("table", { class: "table responsive" },
    h("thead", null, h("tr", null, headers.map((x) => h("th", null, x)))),
    h("tbody", null, rows.map((r) => h("tr", null, r.map((c, i) => h("td", { "data-label": headers[i] }, c)))))));
}

function readableDetails(tool, data, ctx, label) {
  return h("details", null, h("summary", { class: "small" }, label || `Full ${tool} result`),
    h("div", { class: "stack-sm" }, renderToolResult(tool, data, ctx), jsonBlock(data)));
}

// ---------------------------------------------------------------- use case definitions

const USE_CASES = [
  {
    id: "eligibility",
    title: "Extract the eligibility criteria for USDM",
    summary: "Locate the inclusion and exclusion sections, read them completely by following cursors, split the criteria "
      + "by the protocol's own markers and verify each one with quote_check before it becomes an EligibilityCriterion.",
    flow: ["document_info", "find_sections", "read_section", "quote_check"],
    steps: [
      {
        title: "Identify the study and where its criteria are",
        explain: "document_info is the first call: the identity of the protocol and the outermost section of each ICH M11 role.",
        run: async (ctx, tool) => {
          const info = await tool("document_info");
          ctx.info = info;
          const roles = info.roles_found || {};
          const ids = info.identifiers || {};
          const title = ((info.titles || {}).official_title || (info.titles || {}).detected_title || {}).value;
          return [
            h("dl", { class: "kv kv-compact" },
              h("dt", null, "Title"), h("dd", null, title || "–"),
              h("dt", null, "Sponsor protocol id"), h("dd", { class: "mono" }, (ids.sponsor_protocol_id || {}).value || "–"),
              h("dt", null, "NCT"), h("dd", { class: "mono" }, (ids.nct || {}).value || "–"),
              h("dt", null, "Pages / sections"), h("dd", null, `${info.page_count} / ${info.section_count}`),
              ...["eligibility", "inclusion", "exclusion"].flatMap((r) => [h("dt", null, `Role ${r}`),
                h("dd", null, roles[r] ? h("div", { class: "chips" }, [].concat(roles[r]).map((s) => sectionChip(s, ctx))) : h("span", { class: "faint" }, "not found"))])),
            takeaway("roles_found tells where read_section('inclusion') and read_section('exclusion') will start. If a role is "
              + "missing, find the section by title with find_sections(query=...)."),
            readableDetails("document_info", info, ctx),
          ];
        },
      },
      {
        title: "Confirm the sections by role",
        explain: "find_sections(role) ranks the sections tagged inclusion / exclusion; root_of_role marks where a role read starts.",
        run: async (ctx, tool) => {
          const inc = await tool("find_sections", { role: "inclusion" });
          const exc = await tool("find_sections", { role: "exclusion" });
          ctx.sections = { inclusion: inc, exclusion: exc };
          const rows = [...(inc.sections || []).map((s) => ["inclusion", s]), ...(exc.sections || []).map((s) => ["exclusion", s])]
            .map(([role, s]) => [badge(role, role === "inclusion" ? "ok" : "danger"), sectionChip(s.section_id, ctx),
              h("span", { class: "break" }, s.title || ""), String(s.role_score ?? "–"),
              s.root_of_role ? badge("root", "info") : "", `${fmtNumber(s.tokens)} tok`]);
          return [
            rows.length ? miniTable(["Role", "Section", "Title", "Role score", "", "Size"], rows)
              : alertBox("warn", "No section tagged inclusion or exclusion", "Use find_sections(query='inclusion criteria') or the outline instead."),
            takeaway("A role score below 0.7 means the tag is inherited or weak: check the title. The size in tokens tells "
              + "how many read_section parts to expect."),
          ];
        },
      },
      {
        title: "Read both sections completely, following cursors",
        explain: "read_section(role) returns the complete Markdown; with a small max_tokens (500 here) the content comes in "
          + "parts and every part but the last has a cursor.",
        run: async (ctx, tool) => {
          ctx.read = {};
          const out = [];
          for (const role of ["inclusion", "exclusion"]) {
            try {
              const r = await readAll(tool, role, 500);
              ctx.read[role] = r;
              out.push(h("details", { class: "card", open: role === "inclusion" },
                h("summary", null, `${role}: ${r.parts.length} part${r.parts.length === 1 ? "" : "s"}, ${fmtNumber(r.parts.reduce((s, p) => s + (p.tokens || 0), 0))} tokens`,
                  " ", r.last.cursor ? badge("incomplete (stopped)", "warn") : badge("complete: no cursor left", "ok")),
                h("div", { class: "row small muted" }, "Cursors followed: ", r.parts.slice(0, -1).map((p) => h("code", null, p.cursor))),
                renderMarkdown(r.markdown, { onSection: ctx.onSection })));
            } catch (err) {
              if (err.status === 404) { out.push(alertBox("warn", `No ${role} section`, err.message)); continue; }
              throw err;
            }
          }
          out.push(takeaway("Follow cursor until it is absent: a part never ends on a bare heading and never cuts a block. "
            + "Criteria keep the protocol's own markers ([1], a), I 05.) and nesting, so identifiers come straight from the text."));
          return out;
        },
      },
      {
        title: "Split the criteria and verify each with quote_check",
        explain: "This page splits the Markdown by the first marker family it finds (a heuristic), then checks the first line "
          + "of every criterion with quote_check, restricted to the section it came from.",
        run: async (ctx, tool) => {
          ctx.criteria = [];
          const out = [];
          for (const role of ["inclusion", "exclusion"]) {
            const r = ctx.read && ctx.read[role];
            if (!r) continue;
            const crit = parseCriteria(r.markdown).slice(0, 60);
            const sectionId = r.first.section_id;
            const rows = [];
            for (const c of crit) {
              const quote = quoteOf(c.text);
              let qc;
              try {
                qc = await tool("quote_check", { quote, section_id: sectionId });
              } catch (err) {
                qc = { found: false, error: err };
              }
              ctx.criteria.push({ role, ...c, quote, check: qc, sectionId });
              rows.push([h("span", { class: "mono" }, c.marker),
                h("span", { class: "small break" }, truncate(c.lines.join(" "), 220)),
                qc.error ? badge(qc.error.type, "danger") : qc.found ? badge(`found · ${qc.match}`, "ok") : badge("not found", "danger"),
                qc.provenance ? fmtPages(qc.provenance.pages) : "–"]);
            }
            const verified = ctx.criteria.filter((c) => c.role === role && c.check.found).length;
            out.push(h("section", { class: "stack-sm" },
              h("h3", null, `${role[0].toUpperCase()}${role.slice(1)}: ${crit.length} criteria, ${verified} verified`),
              crit.length ? miniTable(["Id", "Criterion", "quote_check", "Pages"], rows)
                : alertBox("warn", "No marked criteria found", "The Markdown has no list markers this heuristic recognises.")));
          }
          const failed = ctx.criteria.filter((c) => !c.check.found);
          out.push(takeaway(failed.length
            ? `${failed.length} criterion line(s) were not found verbatim: open the quote_check result (closest) and fix the text before using it.`
            : "Every criterion was found verbatim in its own section: keep the returned provenance (section, pages, item refs) as evidence."));
          if (failed.length) {
            out.push(h("details", null, h("summary", { class: "small" }, "quote_check of the first criterion not found"),
              quoteCheck(failed[0].check.error ? { quote: failed[0].quote, found: false } : failed[0].check, ctx)));
          }
          return out;
        },
      },
      {
        title: "Map to USDM EligibilityCriterion",
        explain: "The verified criteria as USDM 4 EligibilityCriterion + EligibilityCriterionItem objects (illustrative shape).",
        run: async (ctx) => {
          const crit = (ctx.criteria || []).filter((c) => c.check.found);
          const criteria = [];
          const items = [];
          crit.forEach((c, i) => {
            const itemId = `EligibilityCriterionItem_${i + 1}`;
            items.push({ id: itemId, name: `${c.role.toUpperCase()}_${c.id}`, text: c.lines.join("\n"), instanceType: "EligibilityCriterionItem" });
            criteria.push({
              id: `EligibilityCriterion_${i + 1}`, name: `${c.role.toUpperCase()}_${c.id}`, label: c.marker,
              category: { code: c.role === "inclusion" ? "C25532" : "C25370", codeSystem: "CDISC Controlled Terminology",
                decode: c.role === "inclusion" ? "Inclusion Criteria" : "Exclusion Criteria" },
              identifier: c.id, criterionItemId: itemId,
              previousId: i > 0 && crit[i - 1].role === c.role ? `EligibilityCriterion_${i}` : null,
              nextId: i + 1 < crit.length && crit[i + 1].role === c.role ? `EligibilityCriterion_${i + 2}` : null,
              instanceType: "EligibilityCriterion",
              evidence: { quote: c.quote, match: c.check.match, provenance: c.check.provenance },
            });
          });
          return [
            h("div", { class: "row" }, badge(`${criteria.length} criteria`, "info"),
              badge(`${(ctx.criteria || []).length - criteria.length} left out (not verified)`, (ctx.criteria || []).length - criteria.length ? "warn" : "outline")),
            jsonBlock({ eligibilityCriteria: criteria, eligibilityCriterionItems: items }, { title: "USDM objects (illustrative)", open: true }),
            takeaway("Write only verified criteria; the evidence block (quote, match, provenance) is what a reviewer needs to "
              + "trace each value. The category codes are the CDISC terms for inclusion / exclusion; the ids are local to this page."),
          ];
        },
      },
    ],
  },
  {
    id: "soa",
    title: "Build SoA encounters and activities",
    summary: "Read the structured Schedule of Activities, cross-check a row against the table as printed, verify the visit "
      + "headers with quote_check, and map visits, activities and marks to USDM Encounter, Activity and "
      + "ScheduledActivityInstance.",
    flow: ["get_soa", "get_table", "quote_check", "USDM mapping"],
    steps: [
      {
        title: "Read the structured Schedule of Activities",
        explain: "get_soa returns visits (label, header path, window, timing), activities, the activity × visit cells with "
          + "marks and footnote markers, and the footnote texts.",
        run: async (ctx, tool) => {
          const soa = await tool("get_soa");
          ctx.soa = soa;
          if (!soa.available || !(soa.soas || []).length) {
            return [alertBox("warn", "No structured SoA", "Read it with read_section('soa') and get_table instead."),
              takeaway("soa.py found no Schedule of Activities table: fall back to the text.")];
          }
          const s = soa.soas[0];
          const marked = (s.cells || []).filter((c) => c.mark).length;
          return [
            h("div", { class: "row" }, badge(`${soa.soas.length} SoA table(s)`, "info"), badge(`${(s.visits || []).length} visits`, "outline"),
              badge(`${(s.activities || []).length} activities`, "outline"), badge(`${marked} marked cells`, "ok"),
              provLine(s.provenance, ctx)),
            soaGrid(s),
            Object.keys(s.footnotes || {}).length ? h("details", null, h("summary", { class: "small" }, `${Object.keys(s.footnotes).length} footnotes`),
              h("ol", { class: "footnotes" }, Object.entries(s.footnotes).map(([k, v]) => h("li", null, h("span", { class: "fn-mark" }, k), h("span", null, v))))) : null,
            takeaway("Visits become Encounters, activities become Activities, and each marked cell an activity scheduled at "
              + "that encounter. Visit names, windows and timings are what soa.py infers: check them against the table."),
          ];
        },
      },
      {
        title: "Cross-check a row against the printed table",
        explain: "get_table(format='rows') gives one 'Activity: column = value' line per row of the table as printed.",
        run: async (ctx, tool) => {
          const s = ((ctx.soa || {}).soas || [])[0];
          if (!s || !s.table_id) return [alertBox("info", "Skipped", "No SoA table to check.")];
          const tab = await tool("get_table", { table: s.table_id, format: "rows" });
          ctx.table = tab;
          const lines = (tab.fragments || []).flatMap((f) => f.row_lines || []);
          const act = (s.activities || []).find((a) => (s.cells || []).some((c) => c.activity === a.id && c.mark));
          const visitsOf = act ? (s.cells || []).filter((c) => c.activity === act.id && c.mark)
            .map((c) => (s.visits.find((v) => v.id === c.visit) || {}).label || c.visit) : [];
          const line = act ? lines.find((l) => l.toLowerCase().startsWith(act.label.toLowerCase().slice(0, 12))) : null;
          return [
            act ? h("div", { class: "card stack-sm" },
              h("div", null, h("strong", null, act.label), h("span", { class: "muted" }, " · get_soa marks it at: "), visitsOf.join(", ") || "–"),
              h("div", null, h("span", { class: "muted" }, "get_table row: "), line ? h("code", { class: "break" }, line) : h("span", { class: "faint" }, "row not matched by label"))) : null,
            h("details", null, h("summary", { class: "small" }, `All ${lines.length} row lines of ${s.table_id}`),
              h("ol", { class: "list-plain stack-sm" }, lines.map((l) => h("li", { class: "small" }, h("code", { class: "break" }, l))))),
            takeaway("The row line uses the printed column headers (visit numbers here), get_soa the inferred visit labels; "
              + "when they disagree, trust the printed table and quote_check the visit's header_text (labels can be composed)."),
          ];
        },
      },
      {
        title: "Verify the visit headers with quote_check",
        explain: "A visit label is cleaned and can be composed (a bare number under a 'Days' stub becomes 'Days -42 to -9'), "
          + "so quote the header cell as printed (header_text), not the label. Up to 6 visits are checked, without section_id "
          + "(a continued table can lie in another section): up to 3 composed ones, then visits whose header cell can be quoted.",
        run: async (ctx, tool) => {
          const s = ((ctx.soa || {}).soas || [])[0];
          if (!s) return [alertBox("info", "Skipped", "No SoA to check.")];
          const all = s.visits || [];
          if (!all.length) return [alertBox("info", "No visits", "This Schedule of Activities has no visits to check.")];
          if (!all.some((v) => "header_text" in v)) {
            return [alertBox("info", "Skipped", "get_soa of this service returns no header_text (an older version): quote_check the visit label instead.")];
          }
          const quotable = (v) => ((v.header_text || "").match(/[\p{L}\p{N}]/gu) || []).length >= 2;
          const picked = new Set(all.filter((v) => v.label_composed).slice(0, 3));
          for (const group of [all.filter(quotable), all]) {
            for (const v of group) if (picked.size < 6) picked.add(v);
          }
          // a short number occurs all over a protocol: only a listed location inside the cell's own table says more
          const inItsTable = (qc, cell) => Boolean(cell && cell.table_id)
            && (qc.locations || []).some((l) => ((l.provenance || {}).refs || []).includes(cell.table_id));
          const rows = [];
          let missed = 0;
          let failed = 0;
          for (const v of all.filter((x) => picked.has(x))) { // column order
            const cells = Array.isArray(v.header_cells) ? v.header_cells : [];
            const last = cells[cells.length - 1];
            const text = v.header_text;
            let result;
            if (!text) {
              result = h("span", { class: "faint" }, "no header cell");
            } else if (!quotable(v)) {
              result = h("span", { class: "small muted", title: last && last.table_id ? `table ${last.table_id}` : null },
                `too short to quote, located at ${last ? `row ${last.row}, column ${last.column}` : `column ${v.column}`}`);
            } else {
              let qc;
              try {
                qc = await tool("quote_check", { quote: text });
              } catch (err) {
                qc = { found: false, error: err };
              }
              const listed = (qc.locations || []).length;
              const places = `${qc.count ?? listed} place${(qc.count ?? listed) === 1 ? "" : "s"}`;
              if (qc.error) {
                failed += 1;
                result = badge(qc.error.type || "error", "danger");
              } else if (!qc.found) {
                missed += 1;
                result = badge("not found", "danger");
              } else if (inItsTable(qc, last)) {
                result = badge("found · in its table", "ok", { title: `a listed location is in ${last.table_id}` });
              } else {
                result = badge(last && last.table_id ? `found · ${places}, its table not among the ${listed} listed` : `found · ${places}`, "info");
              }
            }
            const units = cells.filter((c) => c && c.unit).map((c) => h("div", { class: "small" },
              `unit "${c.unit}"${c.unit_from ? ` from the row's "${c.unit_from.text}" cell (column ${c.unit_from.column})` : ""}`));
            rows.push([v.label || h("span", { class: "faint" }, "–"), v.label_composed ? badge("yes", "info") : "no",
              text ? h("code", { class: "break" }, text) : h("span", { class: "faint" }, "–"),
              units.length ? units : h("span", { class: "faint" }, "–"), result]);
          }
          return [
            h("div", { class: "row" }, badge(`${rows.length} of ${all.length} visits`, "outline"),
              badge(`${all.filter((v) => v.label_composed).length} composed labels`, "info")),
            miniTable(["Visit label", "Composed", "Header cell as printed", "Unit from", "quote_check"], rows),
            takeaway((missed ? `${missed} header cell(s) were not found: compare them with get_table(format='rows'). ` : "")
              + (failed ? `${failed} quote_check call(s) failed (error badge): run the step again. ` : "")
              + "A composed label was built by get_soa from the printed cell and the unit of its row: keep the header cell as the "
              + "evidence for the visit. A short number occurs all over a protocol, so a match only proves that the text exists "
              + "('in its table': a listed location lies in the cell's own table); the cell's table_id, row and column point at "
              + "the cell itself in get_table's grid. A cell with fewer than 2 letters or digits cannot be quoted, only located "
              + "that way."),
          ];
        },
      },
      {
        title: "Map to USDM Encounter, Activity and ScheduledActivityInstance",
        explain: "Each visit → Encounter, each activity → Activity, each visit's marked cells → one ScheduledActivityInstance.",
        run: async (ctx) => {
          const s = ((ctx.soa || {}).soas || [])[0];
          if (!s) return [alertBox("info", "Skipped", "No SoA to map.")];
          const visits = s.visits || [];
          const acts = s.activities || [];
          const encId = new Map(visits.map((v, i) => [v.id, `Encounter_${i + 1}`]));
          const actId = new Map(acts.map((a, i) => [a.id, `Activity_${i + 1}`]));
          const encounters = visits.map((v, i) => ({
            id: encId.get(v.id), name: v.label, label: v.label,
            description: Array.isArray(v.header_path) ? v.header_path.join(" / ") : null,
            previousId: i > 0 ? encId.get(visits[i - 1].id) : null, nextId: i + 1 < visits.length ? encId.get(visits[i + 1].id) : null,
            notes: [v.timing ? `timing: ${v.timing}` : null, v.window ? `window: ${v.window}` : null,
              ...(v.footnote_markers || []).map((m) => `footnote ${m}: ${(s.footnotes || {})[m] || ""}`)].filter(Boolean),
            instanceType: "Encounter",
          }));
          const activities = acts.map((a, i) => ({
            id: actId.get(a.id), name: a.label, label: a.label, description: a.notes || null,
            previousId: i > 0 ? actId.get(acts[i - 1].id) : null, nextId: i + 1 < acts.length ? actId.get(acts[i + 1].id) : null,
            notes: (a.footnote_markers || []).map((m) => `footnote ${m}: ${(s.footnotes || {})[m] || ""}`),
            instanceType: "Activity",
          }));
          const instances = visits.map((v, i) => {
            const cells = (s.cells || []).filter((c) => c.visit === v.id && c.mark);
            return {
              id: `ScheduledActivityInstance_${i + 1}`, name: `SAI_${v.id}`, encounterId: encId.get(v.id),
              activityIds: cells.map((c) => actId.get(c.activity)).filter(Boolean),
              notes: cells.filter((c) => (c.footnote_markers || []).length || c.qualifier)
                .map((c) => `${actId.get(c.activity)}: ${[c.qualifier, ...(c.footnote_markers || []).map((m) => `footnote ${m}`)].filter(Boolean).join(", ")}`),
              instanceType: "ScheduledActivityInstance",
            };
          }).filter((x) => x.activityIds.length);
          return [
            h("div", { class: "row" }, badge(`${encounters.length} encounters`, "info"), badge(`${activities.length} activities`, "info"),
              badge(`${instances.length} scheduled activity instances`, "info")),
            miniTable(["Encounter", "Visit label", "Timing / window", "Activities"], encounters.map((e, i) => [
              h("span", { class: "mono small" }, e.id), e.label, e.notes.filter((n) => !n.startsWith("footnote")).join(" · ") || "–",
              String((instances.find((x) => x.encounterId === e.id) || { activityIds: [] }).activityIds.length)])),
            jsonBlock({ encounters, activities, scheduledActivityInstances: instances }, { title: "USDM objects (illustrative)" }),
            takeaway("Timings and windows stay as notes here: turn them into USDM Timing objects only after checking them "
              + "against read_section('soa'). Footnotes qualify individual cells: keep them with the instance they apply to."),
          ];
        },
      },
    ],
  },
  {
    id: "verify",
    title: "Verify an extracted value",
    summary: "quote_check in its three outcomes: found (with the evidence to keep), not found with the closest passage "
      + "to correct the quote, and not found at all. Edit the quotes and re-run.",
    flow: ["read_section", "quote_check × 4"],
    steps: [
      {
        title: "Take a verbatim value",
        explain: "A sentence from read_section('inclusion') stands for a value an agent extracted with its quote.",
        run: async (ctx, tool) => {
          let data;
          try {
            data = await tool("read_section", { section: "inclusion", max_tokens: 600 });
          } catch (err) {
            if (err.status !== 404) throw err;
            data = await tool("read_section", { section: "S1", max_tokens: 600 });
          }
          ctx.sample = { section: data.section_id, sentence: pickSentence(data.markdown) || "" };
          const found = await tool("find_sections", { role: "exclusion" });
          ctx.otherSection = ((found.sections || [])[0] || {}).section_id || null;
          ctx.setInputs({
            found: ctx.sample.sentence,
            altered: alterQuote(ctx.sample.sentence),
            elsewhere: ctx.sample.sentence,
            madeup: MADE_UP,
          });
          return [
            h("div", { class: "card" }, h("div", { class: "small muted" }, "Extracted value (verbatim):"),
              h("div", { class: "context" }, `“${ctx.sample.sentence}”`), provLine(data.provenance, ctx)),
            takeaway("The next steps check this quote as is, with one number or word changed, restricted to another "
              + "section, and a made-up sentence. The quotes are filled in below each step and can be edited."),
          ];
        },
      },
      {
        title: "Found: keep the evidence",
        explain: "quote_check(quote) on the verbatim value.",
        input: "found",
        run: async (ctx, tool, input) => {
          const qc = await tool("quote_check", { quote: input });
          return [quoteCheck(qc, ctx), takeaway(qc.found
            ? `found: true, match ${qc.match}. Store the provenance (section ${qc.provenance.section_id}, ${fmtPages(qc.provenance.pages)}, item refs) as the value's evidence.`
            : "Not found: this quote is not verbatim. Correct it from the closest passage.")];
        },
      },
      {
        title: "Not found, with the closest passage",
        explain: "The same quote with one number or word changed, as an extraction error would.",
        input: "altered",
        run: async (ctx, tool, input) => {
          const qc = await tool("quote_check", { quote: input });
          return [quoteCheck(qc, ctx), takeaway(qc.closest
            ? `closest shows the passage the quote was meant to be (similarity ${Math.round(qc.closest.similarity * 100)}%): correct the value from it and check again. Never keep an unverified value.`
            : qc.found ? "This version was found: edit it to differ from the text." : "No passage is similar enough: the value is probably not in the document.")];
        },
      },
      {
        title: "Restricted to a section: found elsewhere",
        explain: "With section_id only that section and its subsections count; a match outside is listed under found_elsewhere.",
        input: "elsewhere",
        run: async (ctx, tool, input) => {
          if (!ctx.otherSection) return [alertBox("info", "Skipped", "The document has no exclusion section to restrict to.")];
          const qc = await tool("quote_check", { quote: input, section_id: ctx.otherSection });
          return [quoteCheck(qc, ctx), takeaway(`Restricted to ${ctx.otherSection}, the inclusion sentence does not count (found: ${qc.found}); `
            + `found_elsewhere shows where it really is. Pass section_id when you know where a value must come from.`)];
        },
      },
      {
        title: "Not in the document",
        explain: "A made-up sentence: not found, and no closest passage.",
        input: "madeup",
        run: async (ctx, tool, input) => {
          const qc = await tool("quote_check", { quote: input });
          return [quoteCheck(qc, ctx), takeaway(qc.found ? "Unexpectedly found." : qc.closest
            ? "Not found, but a passage shares most of its words: check it." : "Not found and nothing similar: drop the value.")];
        },
      },
    ],
  },
  {
    id: "identifiers",
    title: "Identifiers and version",
    summary: "Study identifiers with the verbatim lines they were read from, version and sponsor, verified with "
      + "quote_check, and the documents the service links to this one (other versions, same text).",
    flow: ["document_info", "quote_check", "GET /documents"],
    steps: [
      {
        title: "Identifiers, version and sponsor",
        explain: "document_info gives each identifier with the verbatim line it was found in, its page and item ref.",
        run: async (ctx, tool) => {
          const info = await tool("document_info");
          ctx.info = info;
          const ids = Object.entries(info.identifiers || {}).filter(([, v]) => v && v.value);
          const version = info.version || {};
          const sponsor = info.sponsor || {};
          return [
            ids.length ? miniTable(["Identifier", "Value", "Verbatim line", "Page"], ids.map(([k, v]) => [
              v.label || k, h("strong", { class: "mono" }, v.value),
              h("span", { class: "id-source" }, highlightQuote((v.source || {}).text || "", v.value)), (v.source || {}).page ? `p. ${v.source.page}` : "–"]))
              : alertBox("warn", "No identifier found", "Check the title page with read_section('title_page')."),
            h("dl", { class: "kv kv-compact" },
              ...Object.entries(version).flatMap(([k, v]) => [h("dt", null, k.replace(/_/g, " "), ""), h("dd", null, v === null ? h("span", { class: "faint" }, "–") : typeof v === "object" ? String(v.value ?? JSON.stringify(v)) : String(v))]),
              h("dt", null, "sponsor"), h("dd", null, sponsor.name || sponsor.value || "–", sponsor.method ? h("span", { class: "small muted" }, ` (${sponsor.method})`) : null)),
            takeaway("StudyIdentifier values come with their source line: cite the line, not just the value. A sponsor found "
              + "by the company-name pattern is marked unverified: confirm it with quote_check."),
          ];
        },
      },
      {
        title: "Verify the identifier values",
        explain: "quote_check of each identifier value and of the sponsor name.",
        run: async (ctx, tool) => {
          const ids = Object.entries((ctx.info || {}).identifiers || {}).filter(([, v]) => v && v.value);
          const sponsor = ((ctx.info || {}).sponsor || {}).name;
          const checks = ids.map(([k, v]) => [k, v.value]);
          if (sponsor) checks.push(["sponsor", sponsor]);
          const rows = [];
          for (const [k, value] of checks) {
            const qc = await tool("quote_check", { quote: value });
            rows.push([k, h("span", { class: "mono" }, value), qc.found ? badge(`found · ${qc.match}`, "ok") : badge("not found", "danger"),
              `${qc.count ?? 0}`, qc.found ? fmtPages(((qc.locations || [])[0] || {}).provenance?.pages) : "–"]);
          }
          return [rows.length ? miniTable(["Field", "Value", "quote_check", "Locations", "First at"], rows) : alertBox("info", "Nothing to check", "No identifier was found."),
            takeaway("An identifier usually occurs many times (headers, footers: layer 'furniture'); one body location is enough evidence.")];
        },
      },
      {
        title: "Versions and related documents",
        explain: "GET /documents/{doc_id} lists related documents (same NCT or sponsor protocol number, other text) and "
          + "documents with the same text; GET /documents shows every document of the service.",
        run: async (ctx, tool, input, calls) => {
          calls.push("GET /documents/{doc_id}");
          const doc = (await get(PATHS.document, { params: { doc_id: ctx.docId } })).data;
          calls.push("GET /documents?limit=1000");
          await refreshDocs();
          const ids = doc.identifiers || {};
          const keys = ["nct", "sponsor_protocol_id"].filter((k) => ids[k]);
          const sameKey = state.docs.filter((d) => d.doc_id !== doc.doc_id && keys.some((k) => (d.identifiers || {})[k] === ids[k]));
          const link = (id) => {
            const other = findDoc(id);
            return h("a", { href: `#/doc/${id}` }, other ? docLabel(other).title : id.slice(0, 12));
          };
          return [
            h("dl", { class: "kv kv-compact" },
              h("dt", null, "Registry identifiers"), h("dd", null, Object.entries(ids).filter(([, v]) => v).map(([k, v]) => `${k} ${v}`).join(" · ") || "–"),
              h("dt", null, "Pipeline version"), h("dd", null, `${doc.pipeline_version || "–"}${doc.stale ? " (stale)" : ""}`),
              h("dt", null, "Related"), h("dd", null, (doc.related || []).length ? h("ul", { class: "list-bullets" }, doc.related.map((r) => h("li", null, link(r.doc_id), ` (${(r.reasons || []).join(", ")})`))) : "none"),
              h("dt", null, "Same text as"), h("dd", null, (doc.same_text_as || []).length ? h("ul", { class: "list-bullets" }, doc.same_text_as.map((id) => h("li", null, link(id)))) : "none"),
              h("dt", null, "Same identifier"), h("dd", null, sameKey.length ? h("ul", { class: "list-bullets" }, sameKey.map((d) => h("li", null, link(d.doc_id)))) : "no other document")),
            takeaway("Amendments are near-identical but stay separate documents: the service links them as related (same "
              + "NCT / sponsor protocol number, different text) and flags identical text, never merging them. Pick the "
              + "version you extract from explicitly."),
          ];
        },
      },
    ],
  },
];

// ---------------------------------------------------------------- pages

export function render(main, route) {
  const id = route.parts[1];
  const uc = USE_CASES.find((u) => u.id === id);
  return uc ? renderCase(main, uc) : renderIndex(main);
}

function renderIndex(main) {
  main.replaceChildren(h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Use cases"),
      h("p", { class: "lede" }, "Guided walkthroughs of what an extraction agent does with the tools, step by step on a "
        + "document of your choice, with what to take from each result."))),
    h("div", { class: "uc-grid" }, USE_CASES.map((u) => h("article", { class: "card uc-card" },
      h("h2", null, h("a", { href: `#/usecases/${u.id}` }, u.title)),
      h("p", { class: "muted small" }, u.summary),
      h("div", { class: "uc-flow" }, u.flow.flatMap((f, i) => (i ? [icon("arrow-right"), h("span", null, f)] : [h("span", null, f)]))),
      h("div", { class: "row" }, h("a", { class: "btn btn-primary btn-sm", href: `#/usecases/${u.id}` }, "Open walkthrough"),
        h("span", { class: "tiny muted" }, `${u.steps.length} steps`)))))));
  return () => {};
}

function renderCase(main, uc) {
  const cleanups = [];
  const stepEls = [];
  const inputs = {};
  let ctx = null;
  let busy = false;

  const runAll = button("Run all steps", { class: "btn-primary" }, "play");
  const reset = button("Reset", { class: "btn-ghost" });
  const list = h("ol", { class: "steps" });

  main.replaceChildren(h("div", { class: "stack" },
    h("div", { class: "breadcrumb" }, h("a", { href: "#/usecases" }, "Use cases"), " / ", uc.title),
    h("div", { class: "page-head" }, h("div", null, h("h1", null, uc.title), h("p", { class: "lede" }, uc.summary))),
    h("div", { class: "toolbar" }, docPicker({ onChange: () => resetAll() }), h("div", { class: "row" }, runAll, reset),
      h("div", { class: "small muted grow" }, "All steps are local tool calls (nothing is sent to OpenRouter).")),
    list));

  function newCtx() {
    return {
      docId: state.selectedDoc,
      onSection: (sid) => { location.hash = `#/tools/read_section?section=${encodeURIComponent(sid)}&run=1`; },
      onTool: (tool, args) => {
        const qs = new URLSearchParams({ ...args, run: "1" });
        location.hash = `#/tools/${tool}?${qs.toString()}`;
      },
      setInputs: (vals) => {
        for (const [k, v] of Object.entries(vals)) {
          if (inputs[k] && !inputs[k].dataset.edited) inputs[k].value = v;
        }
      },
    };
  }

  uc.steps.forEach((step, i) => {
    const num = h("span", { class: "step-num", "aria-hidden": "true" }, String(i + 1));
    const calls = h("div", { class: "step-calls" });
    const runBtn = button("Run", { class: "btn-sm" }, "play");
    const body = h("div", { class: "step-body", hidden: true, "aria-live": "polite" });
    let inputEl = null;
    if (step.input) {
      inputEl = h("textarea", { class: "input", rows: "2", "aria-label": `Quote for step ${i + 1}`, placeholder: "Filled in by step 1" });
      inputEl.addEventListener("input", () => { inputEl.dataset.edited = "1"; });
      inputs[step.input] = inputEl;
    }
    const li = h("li", { class: "step", dataset: { state: "idle" } },
      h("header", null, num, h("div", { class: "grow stack-sm" },
        h("div", { class: "row-between" }, h("h2", { class: "step-title" }, `${i + 1}. ${step.title}`), runBtn),
        h("div", { class: "step-explain" }, step.explain),
        inputEl, calls)),
      body);
    runBtn.addEventListener("click", () => runUpTo(i));
    list.append(li);
    stepEls.push({ li, body, calls, runBtn, done: false, step });
  });

  function resetAll() {
    ctx = null;
    for (const s of stepEls) {
      s.done = false;
      s.li.dataset.state = "idle";
      s.body.hidden = true;
      s.body.replaceChildren();
      s.calls.replaceChildren();
    }
    for (const el of Object.values(inputs)) { el.value = ""; delete el.dataset.edited; }
  }

  async function runStep(i) {
    const s = stepEls[i];
    if (!ctx || ctx.docId !== state.selectedDoc) ctx = newCtx();
    if (!ctx.docId) throw new Error("no document selected");
    const callLog = [];
    s.li.dataset.state = "running";
    s.body.hidden = false;
    s.body.replaceChildren(h("div", { class: "page-loading" }, h("span", { class: "spinner" }), "Running…"));
    s.calls.replaceChildren();
    const tool = toolCaller(ctx, callLog);
    try {
      const nodes = await s.step.run(ctx, tool, s.step.input ? inputs[s.step.input].value.trim() : undefined, callLog);
      s.body.replaceChildren(...[].concat(nodes).filter(Boolean));
      s.li.dataset.state = "done";
      s.done = true;
    } catch (err) {
      s.body.replaceChildren(err && err.type ? errorCard(err) : alertBox("danger", "The step failed", String(err && err.message ? err.message : err)));
      s.li.dataset.state = "error";
      s.done = false;
      throw err;
    } finally {
      const codes = callLog.map((c) => h("code", null, c));
      s.calls.replaceChildren(...(codes.length > 3
        ? [h("details", null, h("summary", { class: "tiny muted" }, `${codes.length} API calls`), h("div", { class: "step-calls" }, codes))]
        : codes));
    }
  }

  async function runUpTo(i) {
    if (busy) return;
    if (!state.selectedDoc) return;
    busy = true;
    setBusy(true);
    try {
      for (let k = 0; k <= i; k += 1) {
        if (k < i && stepEls[k].done) continue;
        await runStep(k);
      }
    } catch (_) { /* shown in the step */ } finally {
      busy = false;
      setBusy(false);
    }
  }

  function setBusy(on) {
    runAll.disabled = on;
    for (const s of stepEls) s.runBtn.disabled = on;
  }

  runAll.addEventListener("click", async () => {
    resetAll();
    await runUpTo(stepEls.length - 1);
  });
  reset.addEventListener("click", resetAll);
  cleanups.push(on("selected", () => resetAll()));
  return () => cleanups.forEach((fn) => fn());
}
