"""REST API of the protocol service: a thin FastAPI layer over service.app_service.ProtocolService, which the MCP
server shares, so REST and MCP return the same results.

    python -m service.api                          # HOST (default 127.0.0.1), PORT (default 8000), one process
    uvicorn service.api:create_app --factory       # the same app (one worker: one process per DATA_DIR)

Routes (OpenAPI at /docs and /openapi.json):
  GET  /health                                   200 when the service is healthy, else 503 (Docker healthcheck)
  GET  /version                                  pipeline version, fingerprint, packages, models
  POST /documents                                multipart upload: file, reprocess, password (form field only)
  GET  /documents   GET|DELETE /documents/{doc_id}   POST /documents/{doc_id}/reprocess (force: query or JSON;
                                                   password: JSON body only)
  GET  /documents/{doc_id}/jobs   GET /jobs   GET /jobs/{job_id}   POST /jobs/{job_id}/cancel
  GET  /documents/{doc_id}/files  GET /documents/{doc_id}/files/{name}   (short names such as chunks.jsonl)
  GET  /tools                                    protocol tool definitions (style=anthropic|openai)
  POST /documents/{doc_id}/tools/{tool}          one typed route per protocol tool, generated from
                                                 protocol_api.TOOLS; a generic route (same path, registered after
                                                 them) answers any other name: 404 unknown_tool
  POST /mcp                                      the MCP server (Streamable HTTP, stateless, JSON responses; the
                                                 same service and results, see service.mcp_server)
  GET  /ui/                                      the demo UI (static files from service/ui; / redirects there)
Uploads and reprocess requests answer 202 when a job was queued, else 200.

Errors are JSON {"type", "message"[, "details"]}: a ServiceError with its own status; request validation 422
"validation_error" with the location, message and type of each error, never the input values (they can hold a
password or document text); unknown routes 404 "not_found", wrong methods 405 "method_not_allowed"; anything else
500 "internal_error" naming only the exception type, logged with the type and our own file:line.

Service methods block (SQLite, files, CPU-heavy tools): every route that calls them is a plain def, which FastAPI
runs in its threadpool. No authentication and no CORS by design: publish the port on 127.0.0.1 only. Requests
must address an allowed host (ALLOWED_HOSTS; 421 "invalid_host") and browser requests must come from a page of
one (403 "invalid_origin").
"""

from __future__ import annotations

import inspect
import logging
import os
import re
import sys
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from http import HTTPStatus
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Callable, Literal, Sequence

from fastapi import Body, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, StrictInt, StrictStr, create_model
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException

from service.app_service import BadRequest, PayloadTooLarge, ProtocolService, ServiceError, Unavailable
from service.config import ServiceConfig
from service.worker import describe_exception

log = logging.getLogger("service.api")

MIB = 1024 * 1024
MULTIPART_ALLOWANCE = 64 * 1024   # boundaries, part headers and the small form fields around the PDF
HTML_CSP = "default-src 'none'; img-src data: 'self'; style-src 'unsafe-inline'; sandbox"
HTTP_ERROR_TYPES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 413: "too_large"}

TAGS = [
    {"name": "documents", "description": "Upload protocol PDFs, list, inspect, reprocess and delete documents."},
    {"name": "jobs", "description": "Processing jobs: state, stage progress, warnings; cancel a queued job."},
    {"name": "files", "description": "Files of a document's published result (Markdown, JSON, HTML, chunks, "
                                     "images)."},
    {"name": "tools", "description": "Protocol tools (protocol_api) on a document's published result."},
    {"name": "service", "description": "Health and versions."},
]

# A FastAPI lifespan hook: called with the app once the service is ready; its context stays open while the app
# serves (e.g. the session manager of an MCP app mounted at /mcp).
LifespanHook = Callable[[FastAPI], AbstractAsyncContextManager[Any]]


class ErrorBody(BaseModel):
    """Every error response."""

    type: str = Field(description="Machine-readable error type, e.g. document_not_found, validation_error.")
    message: str
    details: dict[str, Any] | None = None


def _errors(*codes: int) -> dict:
    return {code: {"model": ErrorBody, "description": HTTPStatus(code).phrase} for code in codes}


COMMON_ERRORS = _errors(422, 500)


# ============================================================================ helpers

async def _service(request: Request) -> ProtocolService:
    """The app's ProtocolService (app.state.service), set by create_app or the lifespan."""
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise Unavailable("the service is not started", code="not_started")
    return service


ServiceDep = Annotated[ProtocolService, Depends(_service)]


def _ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status)


def _decision(decision: dict) -> JSONResponse:
    """An upload / reprocess decision: 202 when a job was queued, else 200."""
    return _ok(decision, 202 if decision.get("queued") else 200)


def _too_large(service: ProtocolService) -> str:
    return f"the request body is larger than MAX_UPLOAD_MB ({service.config.max_upload_mb} MiB) allows"


class _UploadRoute(APIRoute):
    """The upload route: before FastAPI reads the body, refuse a password in the URL and a body larger than
    MAX_UPLOAD_MB plus MULTIPART_ALLOWANCE by its Content-Length; without one (chunked), stop reading once the
    received bytes pass that limit. The service checks the PDF's own size again while storing it."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def guarded(request: Request) -> Response:
            if "password" in request.query_params:
                raise BadRequest("send the PDF password as a form field, never in the URL", code="password_in_query")
            service = await _service(request)
            limit = service.config.max_upload_mb * MIB + MULTIPART_ALLOWANCE
            length = request.headers.get("content-length", "").strip()
            if length.isdigit() and int(length) > limit:
                raise PayloadTooLarge(_too_large(service))
            receive, received = request.receive, 0

            async def counted() -> dict:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body") or b"")
                    if received > limit:  # an HTTPException passes FastAPI's body parsing unchanged
                        raise HTTPException(413, detail=_too_large(service))
                return message

            return await handler(Request(request.scope, counted))

        return guarded


class _CatchAll:
    """ASGI middleware: an unexpected exception becomes 500 {"type": "internal_error"} and is logged with its type
    and our own file:line only (the message and traceback can quote document text). Starlette's own handler would
    re-raise it to the server, which logs the full traceback."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:  # noqa: BLE001 - every unexpected failure is answered, never re-raised
            error_type, where = describe_exception(exc)
            log.error("%s %s failed: %s at %s", scope.get("method"), scope.get("path"), error_type, where or "?")
            if not started:  # else the response is cut short and the server closes the connection
                await _ok({"type": "internal_error", "message": f"internal error ({error_type})"}, 500)(
                    scope, receive, send)


class _HostCheck:
    """ASGI middleware: refuse a Host header outside ALLOWED_HOSTS (421) and a browser request from a page of
    another host (Origin, 403), for every route including /mcp. Without it any web page could reach this
    unauthenticated service on localhost (DNS rebinding, cross-site form posts); API and MCP clients send no
    Origin."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        service = getattr(getattr(scope.get("app"), "state", None), "service", None)
        config = service.config if service is not None else ServiceConfig.from_env()
        headers = Headers(scope=scope)
        if not config.host_allowed(headers.get("host")):
            body, status = {"type": "invalid_host", "message": "Host header not allowed (see ALLOWED_HOSTS)"}, 421
        elif not config.origin_allowed(headers.get("origin")):
            body, status = {"type": "invalid_origin", "message": "requests from this origin are not allowed"}, 403
        else:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            await _ok(body, status)(scope, receive, send)
        else:
            await send({"type": "websocket.close", "code": 1008})


# ============================================================================ error handlers

async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
    return _ok(exc.to_json(), exc.status)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [{"loc": list(e.get("loc") or ()), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
              for e in exc.errors()]  # no "input" / "ctx": they echo what the client sent
    first = errors[0] if errors else None
    where = ".".join(str(p) for p in first["loc"]) if first else ""
    message = f"invalid request: {where}: {first['msg']}" if first else "invalid request"
    return _ok({"type": "validation_error", "message": message, "details": {"errors": errors}}, 422)


async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    try:
        phrase = HTTPStatus(exc.status_code).phrase
    except ValueError:
        phrase = "HTTP error"
    message = exc.detail if isinstance(exc.detail, str) and exc.detail else phrase
    return JSONResponse({"type": HTTP_ERROR_TYPES.get(exc.status_code, "http_error"), "message": message},
                        status_code=exc.status_code, headers=getattr(exc, "headers", None))


# ============================================================================ typed protocol tool routes

_JSON_TYPES: dict[str, type] = {"string": StrictStr, "integer": StrictInt, "boolean": StrictBool}
_SCHEMA_KEYS = {"type", "properties", "required", "additionalProperties"}
_PROPERTY_KEYS = {"type", "enum", "minimum", "maximum", "default", "description"}


def _of_type(value: Any, kind: str) -> bool:
    if kind == "boolean" or isinstance(value, bool):
        return kind == "boolean" and isinstance(value, bool)
    return isinstance(value, {"string": str, "integer": int}[kind])


def _drop_default(schema: dict) -> None:
    schema.pop("default", None)  # optional without a default: "omit it", not "null"


def tool_model(name: str, schema: dict) -> type[BaseModel]:
    """The request body model of a protocol tool, built from its JSON schema (protocol_api.TOOLS input_schema):
    string / integer / boolean (strict, as JSON Schema), enum -> Literal, minimum / maximum -> ge / le, default and
    description carried over, required vs optional, extra properties forbidden. ValueError for anything else, so
    a schema the REST layer cannot express fails at app creation, not at request time."""
    def fail(what: str) -> ValueError:
        return ValueError(f"tool {name}: {what}: not mapped to a REST request model (extend service.api.tool_model)")

    if schema.get("type") != "object" or schema.get("additionalProperties", False) is not False:
        raise fail("input_schema must be an object with additionalProperties false")
    if set(schema) - _SCHEMA_KEYS:
        raise fail(f"input_schema keywords {sorted(set(schema) - _SCHEMA_KEYS)}")
    props, required = schema.get("properties") or {}, set(schema.get("required") or ())
    if required - set(props):
        raise fail(f"required {sorted(required - set(props))} without a property")
    fields: dict[str, Any] = {}
    for prop, spec in props.items():
        kind = spec.get("type") if isinstance(spec, dict) else None
        if kind not in _JSON_TYPES:
            raise fail(f"property {prop!r} of type {kind!r}")
        if set(spec) - _PROPERTY_KEYS:
            raise fail(f"property {prop!r} keywords {sorted(set(spec) - _PROPERTY_KEYS)}")
        annotation: Any = _JSON_TYPES[kind]
        if "enum" in spec:
            values = tuple(spec["enum"])
            if not values or not all(_of_type(v, kind) for v in values):
                raise fail(f"property {prop!r} enum values")
            annotation = Literal[values]
        limits = {k: spec[j] for j, k in (("minimum", "ge"), ("maximum", "le")) if j in spec}
        if limits and kind != "integer":
            raise fail(f"property {prop!r}: minimum / maximum on {kind}")
        options: dict[str, Any] = {"description": spec.get("description"), **limits}
        if prop in required:
            default = ...
        elif "default" in spec:
            default = spec["default"]
        else:
            default, options["json_schema_extra"] = None, _drop_default
        fields[prop] = (annotation, Field(default, **options))
    title = "".join(part.title() for part in name.split("_")) + "Args"
    return create_model(title, __config__=ConfigDict(extra="forbid"), __doc__=f"Arguments of the {name} tool.",
                        **fields)


def _summary(description: str) -> str:
    first = re.split(r"(?<=[.:])\s", description.strip(), maxsplit=1)[0]
    return first.rstrip(".:")[:120]


def _add_tool_route(app: FastAPI, tool: dict) -> None:
    """POST /documents/{doc_id}/tools/<name> with the tool's arguments as a typed JSON body. The arguments the
    client set (exclude_unset: the tool applies its own defaults) go to service.call_tool, so the result is the
    generic route's, byte for byte."""
    name, schema = tool["name"], tool["input_schema"]
    model = tool_model(name, schema)
    required = bool(schema.get("required"))

    def endpoint(doc_id: str, service: ProtocolService, args: BaseModel | None) -> JSONResponse:
        return _ok(service.call_tool(doc_id, name, args.model_dump(exclude_unset=True) if args is not None else {}))

    body = Body(..., description=f"Arguments of {name}.") if required else Body(
        None, description=f"Arguments of {name} (optional: an empty or missing body uses the defaults).")
    kind = inspect.Parameter.POSITIONAL_OR_KEYWORD
    endpoint.__signature__ = inspect.Signature([
        inspect.Parameter("doc_id", kind, annotation=str),
        inspect.Parameter("service", kind, annotation=ServiceDep),
        inspect.Parameter("args", kind, default=body, annotation=model if required else model | None),
    ], return_annotation=JSONResponse)
    endpoint.__name__ = endpoint.__qualname__ = f"tool_{name}"
    app.add_api_route(
        f"/documents/{{doc_id}}/tools/{name}", endpoint, methods=["POST"], tags=["tools"],
        operation_id=f"tool_{name}", summary=_summary(tool["description"]),
        description=f"{tool['description']}\n\nThe JSON body holds the tool's arguments, validated before the call "
                    "(422 validation_error); the result is service.call_tool's for the arguments given, as through "
                    "the generic route or the MCP tool of the same name.",
        responses=_errors(400, 404, 409, 502, 503))


# ============================================================================ the app

def create_app(service: ProtocolService | None = None, *, manage_lifecycle: bool = True,
               lifespan_hooks: Sequence[LifespanHook] = ()) -> FastAPI:
    """The REST app. service=None: ProtocolService(ServiceConfig.from_env()) is built at startup (importing this
    module and calling create_app touch nothing under DATA_DIR) and always started / stopped by the app. A given
    service is started at startup and stopped at shutdown when manage_lifecycle. lifespan_hooks (also
    app.state.lifespan_hooks, appendable until startup) are entered after the service started and exited before
    it stops."""
    from pipeline_version import PIPELINE_VERSION
    from protocol_api import TOOLS

    owned = service is None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = app.state.service
        if svc is None:
            svc = app.state.service = await run_in_threadpool(lambda: ProtocolService(ServiceConfig.from_env()))
        managed = owned or manage_lifecycle
        if managed:
            try:
                report = await run_in_threadpool(svc.start)
            except BaseException:
                await run_in_threadpool(svc.stop)  # close the registry; the server reports the failed startup
                raise
            log.info("protocol service started: %s; recovery %s", svc.config.public(),
                     {k: len(v) if isinstance(v, list) else v for k, v in (report or {}).items()})
        try:
            async with AsyncExitStack() as stack:
                for hook in list(app.state.lifespan_hooks):
                    await stack.enter_async_context(hook(app))
                yield
        finally:
            if managed:
                await run_in_threadpool(svc.stop)
                log.info("protocol service stopped")
            if owned:
                app.state.service = None

    app = FastAPI(
        title="Protocol service", version=PIPELINE_VERSION, lifespan=lifespan, openapi_tags=TAGS,
        description="Converts clinical trial protocol PDFs (Docling) into Markdown / JSON / chunks and answers "
                    "protocol tool calls on the results. Errors are JSON {type, message, details?}.",
        responses=COMMON_ERRORS,
        # No implicit export of telemetry: OTLP exporters are never configured from the environment, and exception
        # messages / request bodies are never recorded as OpenTelemetry logs (they can quote document text).
        telemetry={"auto_configure": False, "logs": False},
    )
    app.state.service = service
    app.state.lifespan_hooks = list(lifespan_hooks)
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(HTTPException, _http_error)
    app.add_middleware(_CatchAll)
    app.add_middleware(_HostCheck)  # added last: outermost, before anything else runs

    # ---------------------------------------------------------------- service
    @app.get("/health", tags=["service"], summary="Service health (503 when unavailable)",
             responses=_errors(503))
    def health(service: ServiceDep) -> JSONResponse:
        """{"status": "ok" | "unavailable", "registry", "worker", "jobs", "offline"}: 503 when the registry cannot
        be read or the worker has stopped."""
        info = service.health()
        return _ok(info, 200 if info.get("status") == "ok" else 503)

    @app.get("/version", tags=["service"], summary="Pipeline version, fingerprint, packages and models")
    def version(service: ServiceDep) -> JSONResponse:
        return _ok(service.version())

    # ---------------------------------------------------------------- documents
    app.router.add_api_route(
        "/documents", upload, methods=["POST"], tags=["documents"], route_class_override=_UploadRoute,
        summary="Upload a protocol PDF", status_code=202, responses={200: {"description": "Not queued: an "
        "existing result (duplicate, or stale without reprocess) or the job already queued / running for "
        "these bytes"}, 202: {"description": "A processing job was queued"}, **_errors(400, 413, 415)})

    @app.get("/documents", tags=["documents"], summary="List documents (most recently updated first)",
             responses=_errors(400))
    def list_documents(service: ServiceDep,
                       status: Annotated[str | None, Query(description="Only documents in this status: none, "
                                         "queued, running, done, done_with_warnings, failed, cancelled.")] = None,
                       limit: Annotated[int, Query(description="At most 1000.")] = 100,
                       offset: int = 0) -> JSONResponse:
        return _ok(service.list_documents(status=status, limit=limit, offset=offset))

    @app.get("/documents/{doc_id}", tags=["documents"], summary="A document: status, versions, warnings, files",
             responses=_errors(400, 404))
    def get_document(doc_id: str, service: ServiceDep) -> JSONResponse:
        return _ok(service.get_document(doc_id))

    @app.delete("/documents/{doc_id}", tags=["documents"], summary="Delete a document, its jobs and files",
                responses=_errors(400, 404, 409))
    def delete_document(doc_id: str, service: ServiceDep,
                        purge_caches: Annotated[bool, Query(description="Also remove this PDF's conversion batches "
                                                            "and figure descriptions from the shared caches: no "
                                                            "trace of it stays under DATA_DIR.")] = False
                        ) -> JSONResponse:
        """{"doc_id", "deleted": true[, "purged": {"batches", "figures"}]}: 409 while a job of it runs."""
        return _ok(service.delete_document(doc_id, purge_caches=purge_caches))

    @app.post("/documents/{doc_id}/reprocess", tags=["documents"], status_code=202,
              summary="Reprocess a stored document from its source PDF",
              responses={200: {"description": "Not queued: the result is current (pass force) or a job is "
                                              "already queued / running"},
                         202: {"description": "A reprocess job was queued"}, **_errors(400, 404)})
    def reprocess(doc_id: str, request: Request, service: ServiceDep,
                  force: Annotated[bool | None, Query(description="Reprocess even when the result is "
                                                                  "current.")] = None,
                  options: Annotated[ReprocessOptions | None, Body(description="Alternative to the query "
                                                                               "parameter, and the password of "
                                                                               "an encrypted PDF.")] = None
                  ) -> JSONResponse:
        """Queues a job when the published result is stale (made by another pipeline version or fingerprint) or
        force is set; force may come as a query parameter or as a JSON body {"force": true}. The password of an
        encrypted source PDF comes only in the JSON body {"password": "..."}, never in the URL."""
        if "password" in request.query_params:
            raise BadRequest("send the PDF password in the JSON body, never in the URL", code="password_in_query")
        secret = options.password.get_secret_value() if options and options.password is not None else None
        return _decision(service.reprocess(doc_id, force=bool(force) or bool(options and options.force),
                                           password=secret or None))

    # ---------------------------------------------------------------- jobs
    @app.get("/documents/{doc_id}/jobs", tags=["jobs"], summary="Jobs of a document (newest first)",
             responses=_errors(400, 404))
    def document_jobs(doc_id: str, service: ServiceDep, state: str | None = None,
                      limit: int = 100) -> JSONResponse:
        return _ok(service.list_jobs(doc_id=doc_id, state=state, limit=limit))

    @app.get("/jobs", tags=["jobs"], summary="Jobs (newest first)", responses=_errors(400))
    def list_jobs(service: ServiceDep,
                  state: Annotated[str | None, Query(description="queued, running, done, done_with_warnings, "
                                                                 "failed or cancelled.")] = None,
                  limit: int = 100) -> JSONResponse:
        return _ok(service.list_jobs(state=state, limit=limit))

    @app.get("/jobs/{job_id}", tags=["jobs"], summary="A job: state, stage, progress, warnings, error",
             responses=_errors(400, 404))
    def get_job(job_id: str, service: ServiceDep) -> JSONResponse:
        return _ok(service.get_job(job_id))

    @app.post("/jobs/{job_id}/cancel", tags=["jobs"], summary="Cancel a queued job (running jobs cannot be "
              "interrupted)", responses=_errors(400, 404, 409))
    def cancel_job(job_id: str, service: ServiceDep) -> JSONResponse:
        return _ok(service.cancel_job(job_id))

    # ---------------------------------------------------------------- files
    @app.get("/documents/{doc_id}/files", tags=["files"], summary="Files and images of the published result",
             responses=_errors(400, 404, 409))
    def list_files(doc_id: str, service: ServiceDep) -> JSONResponse:
        return _ok(service.list_files(doc_id))

    @app.get("/documents/{doc_id}/files/{name:path}", tags=["files"], summary="One file of the published result",
             response_class=FileResponse, responses={200: {"description": "The file", "content": {
                 "application/octet-stream": {}}}, **_errors(400, 404, 409)})
    def get_file(doc_id: str, name: str, service: ServiceDep) -> FileResponse:
        """protocol.md / .json / .html, protocol.decisions.json, quality.json, chunks/*.jsonl|json, images (images/
        <file> or <stem>_images/<file>); short names such as md, json, html, decisions, chunks.jsonl,
        sections.json are accepted (GET /documents/{doc_id}/files lists them)."""
        path, media_type, canonical = service.file(doc_id, name)
        headers = {"X-Content-Type-Options": "nosniff"}
        if media_type.startswith("text/html"):
            headers["Content-Security-Policy"] = HTML_CSP
        return FileResponse(path, media_type=media_type, headers=headers, filename=PurePosixPath(canonical).name,
                            content_disposition_type="inline")

    # ---------------------------------------------------------------- tools
    @app.get("/tools", tags=["tools"], summary="Protocol tool definitions", responses=_errors(400))
    def list_tools(service: ServiceDep,
                   style: Annotated[str, Query(description="anthropic or openai.")] = "anthropic") -> JSONResponse:
        return _ok(service.tools(style))

    for tool in TOOLS:  # before the generic route, which would match their paths too
        _add_tool_route(app, tool)

    @app.post("/documents/{doc_id}/tools/{tool_name}", tags=["tools"], operation_id="call_tool",
              summary="Run a protocol tool by name", responses=_errors(400, 404, 409, 502, 503))
    def call_tool(doc_id: str, tool_name: str, service: ServiceDep,
                  args: Annotated[dict[str, Any] | None, Body(description="The tool's arguments (a JSON object; "
                                                                          "empty or missing: none).")] = None
                  ) -> JSONResponse:
        """Validates the arguments against the tool's JSON schema (400 invalid_arguments) and runs it on the
        published result: 404 unknown tool, section or table; 409 not ready or search without embeddings; 503
        offline cache miss; 502 OpenRouter failure. Over HTTP the typed route of a known tool (same path,
        registered first) answers instead, with the same result; its invalid arguments are 422."""
        return _ok(service.call_tool(doc_id, tool_name, args))

    # ---------------------------------------------------------------- MCP (Streamable HTTP), same service
    from service.mcp_server import attach

    attach(app)

    # ---------------------------------------------------------------- the demo UI (static, same origin)
    app.mount("/ui", _UiFiles(directory=UI_DIR, html=True, check_dir=False), name="ui")

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/")

    return app


UI_DIR = Path(__file__).resolve().parent / "ui"
# The UI is plain files from this origin: no inline script or style, no other host, framed only by itself.
UI_CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
          "frame-src 'self'; frame-ancestors 'self'; base-uri 'none'; form-action 'self'; object-src 'none'")


class _UiFiles(StaticFiles):
    """service/ui/ at /ui (index.html for /ui/), with UI_CSP and nosniff on every response."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Content-Security-Policy"] = UI_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-cache"  # revalidate (ETag -> 304): no stale modules after an update
        return response


class ReprocessOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    force: StrictBool = Field(False, description="Reprocess even when the result is current.")
    password: SecretStr | None = Field(None, description="Password of an encrypted source PDF: held in memory until "
                                                         "its job starts, never stored or logged; dropped when no "
                                                         "job is queued.")


def upload(service: ServiceDep,
           file: Annotated[UploadFile, File(description="The protocol PDF.")],
           reprocess: Annotated[bool, Form(description="Reprocess a stale result (made by another pipeline "
                                                       "version or fingerprint).")] = False,
           password: Annotated[SecretStr | None, Form(description="Password of an encrypted PDF: held in memory "
                                                                  "until its job starts, never stored or "
                                                                  "logged.")] = None) -> JSONResponse:
    """Stores the PDF (doc_id = SHA-256 of its bytes) and applies the duplicate / version rules: the same bytes
    with a queued or running job return that job; a current result returns it without a job (duplicate); a
    result of another pipeline version or fingerprint is stale and reprocessed only with reprocess=true; a new
    PDF (or a failed / cancelled one) is queued. Returns {doc_id, job_id, status, duplicate, stale, queued,
    warnings}."""
    secret = password.get_secret_value() if password is not None else None
    return _decision(service.ingest(file.file, file.filename, reprocess=reprocess, password=secret or None))


# ============================================================================ server

_PASSWORD_PARAM = re.compile(r"(?i)(\bpassword=)[^&\s\"]*")


class RedactPasswordFilter(logging.Filter):
    """Masks password=... in log records (uvicorn's access log prints the request line as the client sent it;
    the upload route refuses a password in the URL, but the line is logged anyway)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(_PASSWORD_PARAM.sub(r"\1***", a) if isinstance(a, str) else a for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _PASSWORD_PARAM.sub(r"\1***", record.msg)
        return True


def main(argv: list[str] | None = None) -> int:
    """Serve the app with uvicorn: HOST (default 127.0.0.1), PORT (default 8000), one worker (the job worker
    thread and SQLite assume one process per DATA_DIR); LOG_LEVEL from the service configuration."""
    import uvicorn

    config = ServiceConfig.from_env()
    level = logging.getLevelName(config.log_level)  # "INFO" -> 20 ("WARN" too); an unknown name -> INFO
    level = level if isinstance(level, int) else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server_config = uvicorn.Config(create_app(), host=os.environ.get("HOST") or "127.0.0.1",
                                   port=int(os.environ.get("PORT") or 8000), workers=1, lifespan="on",
                                   log_level=level, server_header=False)
    logging.getLogger("uvicorn.access").addFilter(RedactPasswordFilter())  # after uvicorn configured its loggers
    uvicorn.Server(server_config).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
