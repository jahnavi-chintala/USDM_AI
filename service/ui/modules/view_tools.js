// Tool explorer: one card per protocol tool (GET /tools), a form generated from its input schema, run through
// POST /documents/{doc_id}/tools/{tool_name}; readable result plus the raw JSON.

import { PATHS, apiPath, callTool } from "./api.js";
import { confirmOpenRouter, openRouterBadge } from "./dialog.js";
import { badge, button, copyButton, h, icon, toast } from "./dom.js";
import { jsonBlock } from "./json_view.js";
import { errorCard, renderToolResult, statusLine } from "./renderers.js";
import { schemaForm } from "./schema_form.js";
import { docPicker, isOffline, loadTools, on, state } from "./state.js";

let current = null; // {cards: Map, applyRoute(route)}
let searchConfirmed = false; // "don't ask again" for search, until the page is reloaded

export function update(route) {
  if (!current) return false;
  current.applyRoute(route);
  return true;
}

function smooth() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

export function render(main, route) {
  const cleanups = [];
  const cards = new Map();
  const grid = h("div", { class: "tool-grid" }, h("div", { class: "page-loading" }, h("span", { class: "spinner" }), "Loading tools…"));
  const page = h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "Tool explorer"),
      h("p", { class: "lede" }, "The nine protocol tools an extraction agent uses, run on the selected document through "
        + "the REST API. Every result carries provenance (section, pages, item refs). The same calls are available "
        + "over MCP: compare them on the ", h("a", { href: "#/mcp" }, "MCP"), " page."))),
    h("div", { class: "toolbar" }, docPicker({ onChange: () => clearAll() }),
      h("div", { class: "small muted grow" }, "Local tools run inside the service. ", h("strong", null, "search"),
        " embeds the query and reranks candidates with OpenRouter unless the query is cached.")),
    grid);
  main.replaceChildren(page);

  function clearAll() {
    for (const c of cards.values()) c.clear();
  }

  const ctxFor = () => ({
    onSection: (id) => runTool("read_section", { section: id }),
    onTool: (tool, args) => runTool(tool, args),
  });

  function runTool(name, args, { run = true } = {}) {
    const card = cards.get(name);
    if (!card) return;
    card.fill(args);
    card.el.scrollIntoView({ behavior: smooth(), block: "start" });
    if (run) card.run(args);
  }

  function applyRoute(r) {
    const name = r.parts[1];
    if (!name || !cards.size) return;
    const args = {};
    for (const [k, v] of r.query.entries()) if (k !== "run") args[k] = v;
    const card = cards.get(name);
    if (!card) { toast(`Unknown tool ${name}`, "warn"); return; }
    runTool(name, card.coerce(args), { run: r.query.get("run") === "1" });
  }

  loadTools().then((tools) => {
    grid.replaceChildren();
    for (const tool of tools) {
      const card = toolCard(tool, ctxFor);
      cards.set(tool.name, card);
      grid.append(card.el);
    }
    applyRoute(route);
  }).catch((err) => grid.replaceChildren(errorCard(err)));

  cleanups.push(on("version", () => { for (const c of cards.values()) c.refreshBadge(); }));
  current = { cards, applyRoute };
  return () => {
    current = null;
    cleanups.forEach((fn) => fn());
  };
}

function firstSentence(text) {
  const m = String(text || "").match(/^[\s\S]*?[.:](\s|$)/);
  return m ? m[0].trim() : String(text || "");
}

function toolCard(tool, ctxFor) {
  const isSearch = tool.name === "search";
  const form = schemaForm(tool.input_schema, { compact: true });
  const result = h("div", { class: "result", hidden: true, "aria-live": "polite" });
  const runBtn = h("button", { type: "submit", class: "btn btn-primary" }, icon("play"), "Run");
  const clearBtn = button("Clear result", { class: "btn-ghost btn-sm", hidden: true });
  const badgeHost = h("span");
  const formEl = h("form", { novalidate: true, "aria-label": `${tool.name} arguments` }, form.el,
    h("div", { class: "form-actions" }, runBtn, clearBtn, h("span", { class: "tiny muted mono" }, `POST ${PATHS.tool.replace("{tool_name}", tool.name)}`)));
  const rest = firstSentence(tool.description);
  const more = tool.description.length > rest.length + 2 ? h("details", null, h("summary", null, "More"), h("p", { class: "small" }, tool.description)) : null;
  const el = h("article", { class: "card tool-card", id: `tool-${tool.name}`, "aria-labelledby": `tool-h-${tool.name}` },
    h("div", { class: "card-head" }, h("h2", { class: "tool-name", id: `tool-h-${tool.name}` }, tool.name), badgeHost),
    h("div", { class: "tool-desc" }, rest, " ", more),
    formEl, result);

  const refreshBadge = () => badgeHost.replaceChildren(isSearch ? openRouterBadge("search")
    : badge("local", "outline", { title: "Runs inside the service on the published result; nothing is sent anywhere" }));
  refreshBadge();

  let lastArgs = {};
  clearBtn.addEventListener("click", () => clear());

  function clear() {
    result.replaceChildren();
    result.hidden = true;
    clearBtn.hidden = true;
    el.classList.remove("has-result");
  }

  formEl.addEventListener("submit", (e) => {
    e.preventDefault();
    const { args, errors } = form.getArgs();
    if (errors.length) return;
    run(args);
  });

  async function run(args, { append = false } = {}) {
    const docId = state.selectedDoc;
    if (!docId) { toast("Select a document first (none is processed yet?)", "warn"); return; }
    if (isSearch && !searchConfirmed) {
      const offline = isOffline();
      const conf = await confirmOpenRouter("search", {
        title: "Run search?",
        action: "search embeds the query and reranks candidate passages. A query asked before on this document is "
          + "answered from its cache without any request.",
        extra: h("div", { class: "small" }, "Query: ", h("strong", { class: "break" }, String(args.query || ""))),
        checkboxes: offline ? [] : [{ name: "remember", label: "Don't ask again for search until this page is reloaded" }],
      });
      if (!conf.ok) return;
      if (conf.checks.remember) searchConfirmed = true;
    }
    lastArgs = args;
    el.classList.add("has-result");
    result.hidden = false;
    clearBtn.hidden = false;
    const part = h("div", { class: "result-part stack-sm" }, h("div", { class: "page-loading" }, h("span", { class: "spinner" }),
      `Running ${tool.name}…`, h("span", { class: "tiny muted" }, "(the first call on a document opens it: up to about 15 s)")));
    if (append) result.append(part);
    else result.replaceChildren(part);
    runBtn.disabled = true;
    const curl = () => {
      const url = `${location.origin}${apiPath(PATHS.tool, { doc_id: docId, tool_name: tool.name })}`;
      return `curl.exe -X POST "${url}" -H "Content-Type: application/json" -d "${JSON.stringify(args).replace(/"/g, '\\"')}"`;
    };
    let outcome;
    try {
      const res = await callTool(docId, tool.name, args);
      outcome = { res };
      const body = [statusLine(res, copyButton(curl, "Copy curl", { class: "btn-xs" })),
        renderToolResult(tool.name, res.data, ctxFor())];
      if (tool.name === "read_section" && res.data && res.data.cursor) body.push(continueBar(res.data.cursor, args));
      body.push(jsonBlock(res.data));
      part.replaceChildren(...body);
    } catch (err) {
      outcome = { err };
      part.replaceChildren(statusLine({ status: err.status, ms: err.ms, method: "POST", url: err.request ? err.request.url : "" }),
        errorCard(err), err.body ? jsonBlock(err.body, { title: "Raw error JSON" }) : null);
    } finally {
      runBtn.disabled = false;
    }
    return outcome;
  }

  /** "Continue" follows one cursor; "Read to the end" follows them until the section is complete. */
  function continueBar(cursor, args) {
    const next = button("Continue", { class: "btn-primary btn-sm" }, "arrow-right");
    const all = button("Read to the end", { class: "btn-sm" });
    const bar = h("div", { class: "alert alert-info", dataset: { cursor } }, icon("arrow-right"), h("div", { class: "alert-body row-between" },
      h("span", null, "The section continues: call read_section again with ", h("code", null, "cursor"), "."), h("div", { class: "row" }, next, all)));
    const withSize = (c) => ({ cursor: c, ...(args.max_tokens ? { max_tokens: args.max_tokens } : {}) });
    next.addEventListener("click", () => { bar.remove(); run(withSize(cursor), { append: true }); });
    all.addEventListener("click", async () => {
      bar.remove();
      let cur = withSize(cursor);
      for (let n = 0; n < 100 && cur; n += 1) {
        const outcome = await run(cur, { append: true });
        const nextCursor = outcome && outcome.res && outcome.res.data ? outcome.res.data.cursor : null;
        if (!nextCursor) break;
        const pending = result.lastElementChild && result.lastElementChild.querySelector("[data-cursor]");
        if (pending) pending.remove();
        cur = withSize(nextCursor);
      }
    });
    return bar;
  }

  function fill(args) {
    form.setValues(args || {});
  }

  /** URL query values (strings) to the schema's types. */
  function coerce(args) {
    const out = {};
    const props = (tool.input_schema && tool.input_schema.properties) || {};
    for (const [k, v] of Object.entries(args)) {
      const t = (props[k] || {}).type;
      if (t === "integer" || t === "number") out[k] = Number(v);
      else if (t === "boolean") out[k] = v === "true" || v === "1";
      else out[k] = v;
    }
    return out;
  }

  return { el, run: (args) => run(args), fill, clear, coerce, refreshBadge, lastArgs: () => lastArgs };
}
