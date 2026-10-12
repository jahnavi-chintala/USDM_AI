"""MCP server of the protocol service: a thin layer over service.app_service.ProtocolService, which the REST API
(service.api) shares, so a tool returns exactly the JSON of the corresponding REST route.

    attach(app)                        # Streamable HTTP at http://<host>:<port>/mcp of a service.api.create_app app
    python -m service.mcp_stdio        # stdio (one client, see service.mcp_stdio)

Tools (12): list_documents, get_document (status, versions, warnings, current job with stage and progress: clients
poll it), ingest_document (a PDF in DATA_DIR/inbox or base64 data), and the 9 protocol tools of protocol_api.TOOLS
with their own descriptions and input schemas plus a required doc_id. Arguments are checked against the advertised
input schema before anything runs; the message names the argument and the rule, never the value sent (it can be
a password or document text).

Results: the service's dict as structuredContent and the same JSON as a text block, for clients without structured
content. A ServiceError is a tool result with isError true carrying the REST error body {"type", "message"
[, "details"]} (text and structuredContent); anything else is {"type": "internal_error"}, logged with the exception
type and our own file:line only. Every service call runs in a worker thread (anyio.to_thread): ProtocolService
blocks (SQLite, files, CPU-heavy tools) and the event loop is shared with the REST API.

HTTP transport (the SDK's StreamableHTTPSessionManager, built at app startup from the service's configuration):
  - stateless: no Mcp-Session-Id, every POST stands alone. Nothing is lost when the server restarts, no session
    expires on an idle timeout, no session table grows; stateful mode would only add server-to-client requests and
    notifications, which these tools never send. Handshake-era clients (Claude Code, Claude Desktop through
    mcp-remote, the SDK in legacy mode) still initialize first; 2026-07-28 clients send self-contained requests,
    which the SDK serves statelessly in any case.
  - JSON responses, not SSE: one application/json answer per POST (the tools emit no progress notifications);
    plain JSON passes any proxy and is easy to debug. GET (the optional SSE stream) and DELETE answer 405.
  - a Route at exactly `path` (no Mount: no /mcp -> /mcp/ redirect, no /mcp/mcp).
  - DNS-rebinding protection: Host and Origin must name one of ServiceConfig.allowed_hosts (any port, http or
    https); "*" turns the check off.
  - request body limit: MAX_UPLOAD_MB as base64 plus 1 MiB of JSON-RPC envelope (the SDK default is 4 MiB), so a
    data_base64 upload reaches ingest_document, where the service checks the PDF's size again.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable, Iterable

import anyio
import mcp_types as types
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.routing import Route

from service.app_service import MAX_LIST, BadRequest, NotFound, ProtocolService, ServiceError, Unavailable
from service.config import ServiceConfig
from service.contracts import DOC_STATES
from service.worker import describe_exception

if TYPE_CHECKING:
    from fastapi import FastAPI

log = logging.getLogger("service.mcp")

SERVER_NAME = "protocol-service"
MIB = 1024 * 1024
BODY_MARGIN = MIB                  # JSON-RPC envelope and the other arguments around a base64 PDF
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")

INSTRUCTIONS = """\
Clinical trial protocol PDFs, converted into structured documents, and tools to read them.
Suggested flow:
1. list_documents to find a document, or ingest_document to add a PDF (a file in the server's inbox folder, or \
base64 data); both give the doc_id (the SHA-256 of the PDF) that every other tool takes.
2. Poll get_document until status is done or done_with_warnings (queued / running: current_job shows the stage \
and progress; failed: see error).
3. document_info first, then outline / find_sections to locate sections.
4. read_section for anything that must be complete (inclusion / exclusion criteria, objectives, endpoints); \
follow 'cursor' until it is absent. get_soa / get_table for tables, search for facts scattered in the text.
5. quote_check every verbatim quote you attach to an extracted value; if it is not found, fix it from 'closest'.
Results are JSON. Errors are tool results with isError and a JSON body {type, message, details?}."""

DOC_ID = {"type": "string", "description": "The document id (SHA-256 of the PDF) from list_documents or "
                                           "ingest_document."}

SERVICE_TOOLS: list[dict] = [
    {"name": "list_documents",
     "description": "Documents of the service, most recently updated first: doc_id, file names, status (none, "
                    "queued, running, done, done_with_warnings, failed, cancelled), stale (made by another pipeline "
                    "version), identifiers (NCT, sponsor protocol number, version, ...), quality summary. Filter "
                    "by status; page with limit / offset.",
     "input_schema": {"type": "object", "properties": {
         "status": {"type": "string", "enum": list(DOC_STATES), "description": "Only documents in this status."},
         "limit": {"type": "integer", "minimum": 0, "maximum": MAX_LIST, "default": 100},
         "offset": {"type": "integer", "minimum": 0, "default": 0}},
         "additionalProperties": False}},
    {"name": "get_document",
     "description": "One document: status, pipeline version and stale flag, identifiers, quality summary, warnings "
                    "of the served result, its files, related documents (same NCT / sponsor protocol number) and "
                    "documents with the same text, and the current job with its stage and progress. Poll it after "
                    "ingest_document until status is done or done_with_warnings.",
     "input_schema": {"type": "object", "properties": {"doc_id": DOC_ID}, "required": ["doc_id"],
                      "additionalProperties": False}},
    {"name": "ingest_document",
     "description": "Add a protocol PDF: exactly one of 'path' (a PDF placed in the server's inbox folder, "
                    "DATA_DIR/inbox) or 'data_base64' (the PDF itself). The same bytes give the same doc_id: an "
                    "existing result is returned without a new job (duplicate), a result of another pipeline "
                    "version is reported stale and reprocessed only with reprocess=true, anything else is queued. "
                    "Returns {doc_id, job_id, status, duplicate, stale, queued, warnings}; then poll get_document.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "A PDF in the inbox folder: its file name, 'inbox/<name>' or its "
                                                   "absolute path (no subfolders)."},
         "data_base64": {"type": "string", "description": "The PDF, base64-encoded (a 'data:application/pdf;base64,' "
                                                          "prefix is accepted); at most MAX_UPLOAD_MB decoded."},
         "filename": {"type": "string", "description": "File name recorded for a data_base64 upload (an inbox file "
                                                       "keeps its own name)."},
         "reprocess": {"type": "boolean", "default": False,
                       "description": "Reprocess a stale result (made by another pipeline version)."},
         "password": {"type": "string", "description": "Password of an encrypted PDF: held in memory until its job "
                                                       "starts, never stored or logged."}},
         "additionalProperties": False}},
]
READ_ONLY = types.ToolAnnotations(read_only_hint=True, open_world_hint=False)
INGEST = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True,
                               open_world_hint=False)

ServiceGetter = Callable[[], "ProtocolService | None"]


# ============================================================================ tools

def tool_definitions() -> list[dict]:
    """[{name, description, input_schema}] of the 12 tools, in listing order: the service tools, then the protocol
    tools with doc_id (required, first) added to their protocol_api input schema."""
    from protocol_api import tool_schemas

    tools = [dict(t) for t in SERVICE_TOOLS]
    for tool in tool_schemas("anthropic"):
        schema = tool["input_schema"]
        tools.append({"name": tool["name"], "description": tool["description"], "input_schema": {
            **schema, "properties": {"doc_id": DOC_ID, **(schema.get("properties") or {})},
            "required": ["doc_id", *(schema.get("required") or [])]}})
    return json.loads(json.dumps(tools))  # no aliasing of the module-level schemas


_COMPARISONS = {"minimum": ">=", "maximum": "<=", "exclusiveMinimum": ">", "exclusiveMaximum": "<"}


def _describe(error) -> str:
    """A jsonschema error without the value it was about."""
    where = ".".join(str(p) for p in error.absolute_path)
    rule, expected = error.validator, error.validator_value
    if rule in ("required", "additionalProperties"):
        text = error.message  # names the properties only
    elif rule == "type":
        text = f"must be of type {' or '.join(expected) if isinstance(expected, list) else expected}"
    elif rule == "enum":
        text = f"must be one of {', '.join(json.dumps(v) for v in expected)}"
    elif rule in _COMPARISONS:
        text = f"must be {_COMPARISONS[rule]} {expected}"
    else:
        text = f"does not satisfy '{rule}'"
    return f"{where}: {text}" if where else text


def _invalid_arguments(name: str, validator: Draft202012Validator, args: dict) -> BadRequest | None:
    error = best_match(validator.iter_errors(args))
    if error is None:
        return None
    return BadRequest(f"invalid arguments for {name}: {_describe(error)}", code="invalid_arguments",
                      details={"tool": name})


def inbox_name(path: str, inbox_dir: Path) -> str:
    """The inbox file name that ingest_document's `path` names: a bare file name, "inbox/<name>" or an absolute
    path whose folder is the inbox itself. BadRequest invalid_name for anything else (subfolders, traversal, other
    folders); storage.inbox_file checks the name again (one component, .pdf, inside the inbox after symlinks)."""
    invalid = BadRequest("not a PDF file name in the inbox", code="invalid_name")
    if not path or "\x00" in path:
        raise invalid
    if os.path.isabs(path):
        full = Path(os.path.abspath(path))  # ".." resolved by name; symlinks are storage's to resolve
        try:
            inside = os.path.normcase(full.parent.resolve()) == os.path.normcase(Path(inbox_dir).resolve())
        except OSError:
            inside = False
        if not inside:
            raise invalid
        return full.name
    parts = PurePosixPath(path.replace("\\", "/")).parts
    if len(parts) == 2 and parts[0] == "inbox":
        return parts[1]
    if len(parts) == 1:
        return parts[0]
    raise invalid


def _ingest(service: ProtocolService, name: str, args: dict) -> dict:
    path, data = args.get("path"), args.get("data_base64")
    if (path is None) == (data is None):
        raise BadRequest("pass exactly one of path (a PDF in the inbox) or data_base64", code="invalid_arguments",
                         details={"tool": name})
    # a password without a worker in this process (stdio) is refused by service.ingest: password_unsupported_here
    options = {"reprocess": bool(args.get("reprocess", False)), "password": args.get("password") or None}
    if path is not None:
        return service.ingest_inbox(inbox_name(path, service.config.inbox_dir), **options)
    return service.ingest_base64(data, args.get("filename"), **options)


def _protocol_tool(service: ProtocolService, name: str, args: dict) -> dict:
    args = dict(args)
    return service.call_tool(args.pop("doc_id"), name, args)


SERVICE_CALLS: dict[str, Callable[[ProtocolService, str, dict], dict]] = {
    "list_documents": lambda service, name, args: service.list_documents(**args),
    "get_document": lambda service, name, args: service.get_document(args["doc_id"]),
    "ingest_document": _ingest,
}


def _call(get_service: ServiceGetter, name: str, args: dict) -> dict:
    """One tool call, in a worker thread."""
    service = get_service()
    if service is None:
        raise Unavailable("the service is not started", code="not_started")
    return SERVICE_CALLS.get(name, _protocol_tool)(service, name, args)


def _result(body: dict, *, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
                                structured_content=body, is_error=is_error)


def build_server(get_service: ServiceGetter) -> Server:
    """The MCP server over the service that get_service() returns at call time (None: not started yet). The
    getter runs in the worker thread of each call, so it may block (service.mcp_stdio waits for its startup)."""
    from pipeline_version import PIPELINE_VERSION

    definitions = tool_definitions()
    names = [t["name"] for t in definitions]
    schemas = {t["name"]: t["input_schema"] for t in definitions}
    validators = {name: Draft202012Validator(schema) for name, schema in schemas.items()}
    listing = types.ListToolsResult(tools=[
        types.Tool(name=t["name"], description=t["description"], input_schema=t["input_schema"],
                   annotations=INGEST if t["name"] == "ingest_document" else READ_ONLY) for t in definitions])

    async def list_tools(ctx, params) -> types.ListToolsResult:
        return listing

    async def call_tool(ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        name, args = params.name, dict(params.arguments or {})
        if name not in validators:
            return _result(NotFound("unknown tool", code="unknown_tool", details={"tools": names}).to_json(),
                           is_error=True)
        invalid = _invalid_arguments(name, validators[name], args)
        if invalid is not None:
            return _result(invalid.to_json(), is_error=True)
        try:
            body = await anyio.to_thread.run_sync(partial(_call, get_service, name, args))
        except ServiceError as exc:
            return _result(exc.to_json(), is_error=True)
        except Exception as exc:  # noqa: BLE001 - the message can quote document text: type and file:line only
            error_type, where = describe_exception(exc)
            log.error("MCP tool %s failed: %s at %s", name, error_type, where or "?")
            return _result({"type": "internal_error", "message": f"internal error ({error_type})"}, is_error=True)
        return _result(body)

    return Server(SERVER_NAME, version=PIPELINE_VERSION, instructions=INSTRUCTIONS, on_list_tools=list_tools,
                  on_call_tool=call_tool, get_tool_input_schema=schemas.get)


# ============================================================================ Streamable HTTP

def transport_security(allowed_hosts: Iterable[str]) -> TransportSecuritySettings:
    """The SDK's DNS-rebinding protection for these host names: Host h or h:<port>, Origin http(s)://h[:<port>]
    (an IPv6 address in brackets); "*" disables the check."""
    hosts = [h.strip() for h in allowed_hosts if h and h.strip()]
    if "*" in hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    allowed, origins = [], []
    for host in hosts:
        try:
            if ipaddress.ip_address(host).version == 6:
                host = f"[{host}]"
        except ValueError:
            pass  # a name (or an address already in brackets)
        allowed += [host, f"{host}:*"]
        origins += [f"{scheme}://{host}{port}" for scheme in ("http", "https") for port in ("", ":*")]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=allowed,
                                     allowed_origins=origins)


def request_body_limit(max_upload_mb: int) -> int:
    """Largest /mcp request body: a base64 PDF of MAX_UPLOAD_MB plus BODY_MARGIN."""
    return 4 * math.ceil(max_upload_mb * MIB / 3) + BODY_MARGIN


def _jsonrpc_error(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": message}},
                        status_code=status, headers=headers)


class McpEndpoint:
    """The ASGI app at the MCP path: the session manager of the running app, 405 for anything but POST, 503 outside
    the app's lifespan. lifespan(app) is the service.api lifespan hook that builds and runs the session manager."""

    def __init__(self, server: Server) -> None:
        self.server = server
        self.manager: StreamableHTTPSessionManager | None = None

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("method") != "POST":  # stateless: no SSE stream (GET) and no session to end (DELETE)
            await _jsonrpc_error(405, "Method Not Allowed: this MCP endpoint answers POST only",
                                 {"Allow": "POST"})(scope, receive, send)
            return
        manager = self.manager
        if manager is None:
            await _jsonrpc_error(503, "the MCP endpoint is not running")(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self, app: FastAPI):
        service = getattr(app.state, "service", None)
        config = service.config if service is not None else ServiceConfig.from_env()
        manager = StreamableHTTPSessionManager(
            self.server, json_response=True, stateless=True,
            security_settings=transport_security(getattr(config, "allowed_hosts", DEFAULT_ALLOWED_HOSTS)),
            max_request_body_size=request_body_limit(config.max_upload_mb))
        async with manager.run():  # a session manager runs once: a new one per app startup
            self.manager = manager
            try:
                yield
            finally:
                self.manager = None


def attach(app: FastAPI, *, path: str = "/mcp") -> McpEndpoint:
    """Serve the MCP server over Streamable HTTP at exactly `path` of an app made by service.api.create_app (before
    its startup): a Route (not a Mount) and a lifespan hook that runs the session manager while the app serves. The
    tools call app.state.service at call time. Idempotent: an endpoint already at `path` is returned."""
    if not path.startswith("/") or path.endswith("/"):
        raise ValueError("path must start with '/' and not end with '/'")
    for route in app.router.routes:  # attached already (create_app does it): that endpoint
        if isinstance(route, Route) and route.path == path and isinstance(route.endpoint, McpEndpoint):
            return route.endpoint
    endpoint = McpEndpoint(build_server(lambda: getattr(app.state, "service", None)))
    app.router.routes.append(Route(path, endpoint=endpoint, include_in_schema=False, name="mcp"))
    app.state.lifespan_hooks.append(endpoint.lifespan)
    return endpoint
