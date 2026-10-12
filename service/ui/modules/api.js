// The service API, same origin, relative paths only. Every path the UI calls is written here as the OpenAPI path
// template (tests/test_service_ui.py checks each one against the app's OpenAPI schema; /mcp is the MCP endpoint).

export const PATHS = Object.freeze({
  health: "/health",
  version: "/version",
  documents: "/documents",
  document: "/documents/{doc_id}",
  reprocess: "/documents/{doc_id}/reprocess",
  documentJobs: "/documents/{doc_id}/jobs",
  jobs: "/jobs",
  job: "/jobs/{job_id}",
  cancelJob: "/jobs/{job_id}/cancel",
  files: "/documents/{doc_id}/files",
  file: "/documents/{doc_id}/files/{name}",
  tools: "/tools",
  tool: "/documents/{doc_id}/tools/{tool_name}",
  mcp: "/mcp",
});

export const MCP_PROTOCOL_VERSION = "2025-11-25";

/** Fill an OpenAPI path template; {name} (a file name) keeps its slashes, every other parameter is one segment. */
export function apiPath(template, params = {}, query = null) {
  let out = template.replace(/\{(\w+)\}/g, (_, key) => {
    const value = params[key];
    if (value === undefined || value === null || value === "") throw new Error(`missing path parameter ${key}`);
    if (key === "name") return String(value).split("/").map(encodeURIComponent).join("/");
    return encodeURIComponent(String(value));
  });
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== "") qs.set(k, String(v));
    const s = qs.toString();
    if (s) out += `?${s}`;
  }
  return out;
}

export function fileUrl(docId, name) {
  return apiPath(PATHS.file, { doc_id: docId, name });
}

/** An error answered by the API ({type, message, details}) or a transport failure (type network_error). */
export class ApiError extends Error {
  constructor(status, body, request) {
    const isObj = body && typeof body === "object" && !Array.isArray(body);
    const type = isObj && typeof body.type === "string" ? body.type : status ? "http_error" : "network_error";
    const message = isObj && typeof body.message === "string" ? body.message
      : status ? `HTTP ${status}` : "the service did not answer (is it running?)";
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.details = isObj ? body.details : undefined;
    this.body = body;
    this.request = request || null;
  }
}

async function readBody(res) {
  const text = await res.text();
  if (!text) return null;
  const type = res.headers.get("content-type") || "";
  if (type.includes("json")) {
    try { return JSON.parse(text); } catch (_) { return text; }
  }
  if (type.includes("text/event-stream")) return parseSse(text);
  try { return JSON.parse(text); } catch (_) { return text; }
}

/** The last JSON-RPC message of an SSE body (the service answers JSON, but a Streamable HTTP server may use SSE). */
function parseSse(text) {
  let last = null;
  for (const block of text.split(/\r?\n\r?\n/)) {
    const data = block.split(/\r?\n/).filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
    if (data) {
      try { last = JSON.parse(data); } catch (_) { last = data; }
    }
  }
  return last;
}

/**
 * One API request: resolves {status, data, ms, method, url}; rejects with ApiError for any non-2xx answer or a
 * transport failure. opts: params (path), query, json (body), form (FormData), signal, accept (array of statuses
 * that are not errors), text (resolve the body as text, e.g. JSON Lines).
 */
export async function request(method, template, opts = {}) {
  const url = apiPath(template, opts.params || {}, opts.query || null);
  const init = { method, headers: { Accept: "application/json" }, cache: "no-store", credentials: "same-origin",
    signal: opts.signal };
  if (opts.json !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.json);
  } else if (opts.form) {
    init.body = opts.form;
  }
  const req = { method, url, body: opts.json };
  const t0 = performance.now();
  let res;
  try {
    res = await fetch(url, init);
  } catch (err) {
    if (err && err.name === "AbortError") throw err;
    throw new ApiError(0, null, req);
  }
  const data = opts.text && res.ok ? await res.text() : await readBody(res);
  const ms = Math.round(performance.now() - t0);
  if (!res.ok && !(opts.accept || []).includes(res.status)) {
    const error = new ApiError(res.status, data, req);
    error.ms = ms;
    throw error;
  }
  return { status: res.status, data, ms, method, url };
}

export const get = (template, opts) => request("GET", template, opts);
export const post = (template, opts) => request("POST", template, opts);
export const del = (template, opts) => request("DELETE", template, opts);

/** POST /documents/{doc_id}/tools/{tool_name} with the arguments as the JSON body. */
export function callTool(docId, tool, args, opts = {}) {
  return post(PATHS.tool, { ...opts, params: { doc_id: docId, tool_name: tool }, json: args || {} });
}

/**
 * Multipart upload (POST /documents) with upload progress (XMLHttpRequest: fetch has no upload progress).
 * Resolves {status, data}; rejects with ApiError.
 */
export function uploadPdf(file, { reprocess = false, password = "", onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("reprocess", reprocess ? "true" : "false");
    if (password) form.append("password", password); // a form field only: never in the URL
    const xhr = new XMLHttpRequest();
    const url = apiPath(PATHS.documents);
    xhr.open("POST", url);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.responseType = "text";
    if (onProgress) {
      xhr.upload.addEventListener("progress", (e) => { if (e.lengthComputable) onProgress(e.loaded, e.total); });
    }
    xhr.addEventListener("load", () => {
      let data = null;
      try { data = xhr.responseText ? JSON.parse(xhr.responseText) : null; } catch (_) { data = xhr.responseText; }
      if (xhr.status >= 200 && xhr.status < 300) resolve({ status: xhr.status, data });
      else reject(new ApiError(xhr.status, data, { method: "POST", url }));
    });
    xhr.addEventListener("error", () => reject(new ApiError(0, null, { method: "POST", url })));
    xhr.addEventListener("abort", () => reject(new ApiError(0, { type: "aborted", message: "upload cancelled" })));
    xhr.send(form);
  });
}

// ---------------------------------------------------------------- MCP (Streamable HTTP, stateless, JSON responses)

let rpcCounter = 0;

function mcpHeaders(withVersion) {
  const headers = { "Content-Type": "application/json", Accept: "application/json, text/event-stream" };
  if (withVersion) headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION;
  return headers;
}

/** POST one JSON-RPC message to /mcp; resolves {request: {method, url, headers, body}, status, body, ms}. */
export async function mcpPost(message, { withVersion = true } = {}) {
  const headers = mcpHeaders(withVersion);
  const url = apiPath(PATHS.mcp);
  const exchange = { request: { method: "POST", url, headers, body: message }, status: 0, body: null, ms: 0 };
  const t0 = performance.now();
  try {
    const res = await fetch(url, { method: "POST", headers, body: JSON.stringify(message), cache: "no-store",
      credentials: "same-origin" });
    exchange.status = res.status;
    exchange.body = await readBody(res);
  } catch (_) {
    exchange.status = 0;
    exchange.body = { error: "the service did not answer" };
  }
  exchange.ms = Math.round(performance.now() - t0);
  return exchange;
}

export function rpcMessage(method, params, notification = false) {
  const msg = { jsonrpc: "2.0" };
  if (!notification) {
    rpcCounter += 1;
    msg.id = rpcCounter;
  }
  msg.method = method;
  if (params !== undefined) msg.params = params;
  return msg;
}

/** The handshake era: initialize, then notifications/initialized (the stateless server keeps no session). */
export async function mcpHandshake() {
  const init = await mcpPost(rpcMessage("initialize", {
    protocolVersion: MCP_PROTOCOL_VERSION,
    capabilities: {},
    clientInfo: { name: "protocol-service-ui", version: "1" },
  }));
  const exchanges = [{ label: "initialize", ...init }];
  if (init.status === 200 && init.body && init.body.result) {
    const done = await mcpPost(rpcMessage("notifications/initialized", undefined, true));
    exchanges.push({ label: "notifications/initialized", ...done });
  }
  return exchanges;
}

export async function mcpListTools() {
  const exchanges = await mcpHandshake();
  const list = await mcpPost(rpcMessage("tools/list", {}));
  exchanges.push({ label: "tools/list", ...list });
  const tools = list.body && list.body.result && Array.isArray(list.body.result.tools) ? list.body.result.tools : null;
  return { exchanges, tools };
}

export async function mcpCallTool(name, args) {
  const exchanges = await mcpHandshake();
  const call = await mcpPost(rpcMessage("tools/call", { name, arguments: args }));
  exchanges.push({ label: "tools/call", ...call });
  return exchanges;
}
