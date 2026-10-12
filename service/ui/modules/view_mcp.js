// MCP view: the same tool call sent as JSON-RPC over Streamable HTTP (/mcp: initialize, notifications/initialized,
// tools/call; stateless, JSON responses) and through REST, side by side, with a deep-equality check of the MCP
// structuredContent against the REST JSON.

import { MCP_PROTOCOL_VERSION, PATHS, apiPath, callTool, get, mcpCallTool, mcpListTools } from "./api.js";
import { confirmOpenRouter, openRouterBadge } from "./dialog.js";
import { badge, h, icon, toast, uid } from "./dom.js";
import { jsonBlock, jsonPre } from "./json_view.js";
import { errorCard } from "./renderers.js";
import { schemaForm } from "./schema_form.js";
import { docPicker, state } from "./state.js";

const NO_DOC = new Set(["list_documents", "ingest_document"]);

export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") {
    return typeof a === "number" && typeof b === "number" && Number.isNaN(a) && Number.isNaN(b);
  }
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  if (Array.isArray(a)) return a.length === b.length && a.every((v, i) => deepEqual(v, b[i]));
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && deepEqual(a[k], b[k]));
}

export function render(main) {
  const selId = uid("mcp-tool");
  const toolSel = h("select", { id: selId, class: "input" }, h("option", { value: "" }, "Loading tools/list…"));
  toolSel.disabled = true;
  const formHost = h("div");
  const descHost = h("div", { class: "small muted" });
  const badgeHost = h("span");
  const sendBtn = h("button", { type: "submit", class: "btn btn-primary" }, icon("play"), "Send over MCP and REST");
  const out = h("div", { class: "stack", "aria-live": "polite" });
  const handshakeHost = h("div");
  const picker = docPicker({});
  let tools = [];
  let form = null;

  const formEl = h("form", { novalidate: true, class: "card stack" },
    h("div", { class: "form-grid" },
      h("div", { class: "field" }, h("label", { class: "field-label", for: selId }, "MCP tool"), toolSel),
      picker),
    descHost, formHost,
    h("div", { class: "form-actions" }, sendBtn, badgeHost,
      h("span", { class: "tiny muted" }, `POST ${PATHS.mcp} · MCP-Protocol-Version ${MCP_PROTOCOL_VERSION}`)));

  main.replaceChildren(h("div", { class: "stack" },
    h("div", { class: "page-head" }, h("div", null, h("h1", null, "MCP"),
      h("p", { class: "lede" }, "The service's MCP server runs at /mcp (Streamable HTTP, stateless, JSON responses) over "
        + "the same service layer as REST, so a tool's structuredContent is exactly the REST JSON. This page sends the "
        + "handshake-era sequence (initialize, notifications/initialized, tools/call) and the REST call, and compares them."))),
    formEl, handshakeHost, out));

  function selectTool(name) {
    const tool = tools.find((t) => t.name === name);
    if (!tool) return;
    form = schemaForm(tool.inputSchema || tool.input_schema || {}, { omit: ["doc_id"], compact: true });
    formHost.replaceChildren(form.el);
    descHost.textContent = tool.description || "";
    picker.hidden = NO_DOC.has(name);
    badgeHost.replaceChildren(name === "search" ? openRouterBadge("search") : badge("local", "outline"));
  }
  toolSel.addEventListener("change", () => selectTool(toolSel.value));

  mcpListTools().then(({ exchanges, tools: list }) => {
    handshakeHost.replaceChildren(exchangeList(exchanges, "Discovery: initialize and tools/list"));
    if (!list) {
      const last = exchanges[exchanges.length - 1];
      toolSel.replaceChildren(h("option", { value: "" }, "tools/list failed"));
      out.replaceChildren(errorCard({ type: "mcp_error", status: last ? last.status : 0,
        message: "The MCP endpoint did not list its tools; see the exchange above." }));
      return;
    }
    tools = list;
    toolSel.replaceChildren(...list.map((t) => h("option", { value: t.name, disabled: t.name === "ingest_document" },
      t.name === "ingest_document" ? `${t.name} (use the Documents page to upload)` : t.name)));
    toolSel.disabled = false;
    toolSel.value = list.some((t) => t.name === "find_sections") ? "find_sections" : list[0].name;
    selectTool(toolSel.value);
    if (toolSel.value === "find_sections") form.setValues({ role: "inclusion" });
  });

  formEl.addEventListener("submit", async (e) => {
    e.preventDefault();
    const name = toolSel.value;
    if (!name || !form) return;
    const { args, errors } = form.getArgs();
    if (errors.length) return;
    const needsDoc = !NO_DOC.has(name);
    if (needsDoc && !state.selectedDoc) { toast("Select a document first", "warn"); return; }
    if (name === "search") {
      const conf = await confirmOpenRouter("search", {
        title: "Run search over MCP and REST?",
        action: "The query runs twice: over MCP, then through REST. The second call is answered from the cache the first one filled.",
        extra: h("div", { class: "small" }, "Query: ", h("strong", { class: "break" }, String(args.query || ""))),
      });
      if (!conf.ok) return;
    }
    const mcpArgs = needsDoc ? { doc_id: state.selectedDoc, ...args } : args;
    sendBtn.disabled = true;
    out.replaceChildren(h("div", { class: "page-loading" }, h("span", { class: "spinner" }), "Calling /mcp, then REST…"));
    try {
      const exchanges = await mcpCallTool(name, mcpArgs);
      const rest = await restEquivalent(name, args);
      paint(name, exchanges, rest);
    } finally {
      sendBtn.disabled = false;
    }
  });

  function paint(name, exchanges, rest) {
    const call = exchanges.find((x) => x.label === "tools/call");
    const handshake = exchanges.filter((x) => x.label !== "tools/call");
    const rpc = call && call.body;
    const result = rpc && rpc.result;
    const sc = result ? result.structuredContent : undefined;
    let verdict;
    if (!call) {
      verdict = h("div", { class: "verdict bad" }, icon("x"), "The handshake failed: no tools/call was sent");
    } else if (!result) {
      verdict = h("div", { class: "verdict bad" }, icon("x"), `JSON-RPC error (HTTP ${call.status})`);
    } else {
      const same = deepEqual(sc, rest.body);
      const bothErrors = Boolean(result.isError) && !rest.ok;
      const sameType = bothErrors && sc && rest.body && sc.type === rest.body.type;
      const textOk = Array.isArray(result.content) && result.content.length === 1 && result.content[0].type === "text"
        && (() => { try { return deepEqual(JSON.parse(result.content[0].text), sc); } catch (_) { return false; } })();
      verdict = h("div", { class: ["verdict", same ? "ok" : "bad"] }, icon(same ? "check" : "x"),
        same ? "structuredContent matches REST" : sameType ? "Same error type, different details" : "structuredContent differs from REST",
        badge(result.isError ? "isError: true" : "isError: false", result.isError ? "warn" : "ok"),
        badge(`REST HTTP ${rest.status || "—"}`, rest.ok ? "ok" : "warn"),
        badge(textOk ? "text block = structuredContent" : "text block differs", textOk ? "ok" : "warn"));
      if (!same && bothErrors && sc && rest.body && sc.type === "invalid_arguments" && rest.body.type === "validation_error") {
        verdict.append(h("span", { class: "small" }, "Expected: the typed REST route validates the body first (422)."));
      }
    }
    const req = call ? { ...call.request } : null;
    out.replaceChildren(
      h("section", { class: "card stack" },
        h("div", { class: "card-head" }, h("h2", null, `tools/call ${name}`),
          h("span", { class: "small muted" }, call ? `MCP ${call.ms} ms · REST ${rest.ms ?? "—"} ms` : "")),
        verdict,
        h("div", { class: "panes" },
          pane("JSON-RPC request", req ? { method: req.method, url: req.url, headers: req.headers, body: req.body } : null,
            req ? `${req.method} ${req.url}` : null),
          pane("JSON-RPC response", call ? call.body : null, call ? `HTTP ${call.status} · ${call.ms} ms` : null),
          pane("REST response", rest.body, `${rest.method} ${rest.url} · HTTP ${rest.status || "—"}`)),
        exchangeList(handshake, `Handshake before the call (${handshake.length} message${handshake.length === 1 ? "" : "s"})`)));
  }

  return () => {};
}

async function restEquivalent(name, args) {
  const docId = state.selectedDoc;
  const t0 = performance.now();
  try {
    let res;
    if (name === "list_documents") res = await get(PATHS.documents, { query: args });
    else if (name === "get_document") res = await get(PATHS.document, { params: { doc_id: docId } });
    else res = await callTool(docId, name, args);
    return { ok: true, status: res.status, body: res.data, ms: res.ms, method: res.method, url: res.url };
  } catch (err) {
    const url = name === "list_documents" ? apiPath(PATHS.documents, {}, args)
      : name === "get_document" ? apiPath(PATHS.document, { doc_id: docId })
        : apiPath(PATHS.tool, { doc_id: docId, tool_name: name });
    return { ok: false, status: err.status, body: err.body ?? { type: err.type, message: err.message },
      ms: Math.round(performance.now() - t0), method: name === "list_documents" || name === "get_document" ? "GET" : "POST", url };
  }
}

function pane(title, data, sub) {
  return h("section", { class: "pane" }, h("header", null, h("h3", { class: "break" }, title),
    sub ? h("div", { class: "tiny muted mono break" }, sub) : null),
  data === null || data === undefined ? h("div", { class: "empty" }, "Nothing") : jsonPre(data));
}

function exchangeList(exchanges, title) {
  if (!exchanges || !exchanges.length) return null;
  return h("details", { class: "card" }, h("summary", { class: "small" }, title),
    h("div", { class: "exchange-list" }, exchanges.map((x) => h("div", { class: "stack-sm" },
      h("div", { class: "row small" }, h("strong", { class: "mono" }, x.label), badge(`HTTP ${x.status}`, x.status >= 200 && x.status < 300 ? "ok" : "danger"),
        h("span", { class: "muted" }, `${x.ms} ms`)),
      h("div", { class: "panes two" }, pane("request", { method: x.request.method, url: x.request.url, headers: x.request.headers, body: x.request.body }),
        pane("response", x.body === null ? { status: x.status, body: null } : x.body))))),
    jsonBlock(exchanges.map((x) => ({ label: x.label, status: x.status, request: x.request.body, response: x.body })), { title: "All messages as JSON" }));
}
