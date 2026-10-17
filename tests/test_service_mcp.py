"""Self-test for the MCP server (service.mcp_server, service.mcp_stdio): `python tests/test_service_mcp.py` (no pytest,
offline).

The SDK's clients against build_server over a test ProtocolService (fake pipeline, temporary DATA_DIR, the fixtures
of tests/test_service_api): in process in both protocol eras (2026-07-28 per-request envelope and the initialize
handshake); the Streamable HTTP endpoint of create_app + attach served by uvicorn on 127.0.0.1 and reached at exactly
http://127.0.0.1:<port>/mcp; `python -m service.mcp_stdio` spawned by the SDK's stdio client. The protocol tools are
compared with the REST API on the synthetic document of tests/test_service_api (fake embeddings and reranker); they
need the chunk tokenizer in the local Hugging Face cache and are skipped with a note without it. No network beyond
127.0.0.1: OPENROUTER_OFFLINE=1, HF_HUB_OFFLINE=1. Output: counts, timings and booleans only.
"""

import base64
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import anyio  # noqa: E402
import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp import Client, StdioServerParameters, stdio_client  # noqa: E402
from mcp.client._memory import InMemoryTransport  # noqa: E402
from mcp.server.transport_security import TransportSecurityMiddleware  # noqa: E402
from mcp_types.version import LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION  # noqa: E402

from pipeline_version import PIPELINE_VERSION  # noqa: E402
from protocol_api import TOOLS  # noqa: E402
from service import api  # noqa: E402
from service.app_service import ProtocolService  # noqa: E402
from service.config import ServiceConfig  # noqa: E402
from service.mcp_server import (DOC_ID, attach, build_server, inbox_name, request_body_limit,  # noqa: E402
                                tool_definitions, transport_security)
from service.registry import Registry  # noqa: E402
from test_service_api import (TOOL_CALLS, VERSION, Capture, build_converted_folder, fake_rerank,  # noqa: E402
                              fake_vector, make_pipeline, make_service, temp_dir, tokenizer_cached)
from test_service_core import make_pdf, sha  # noqa: E402

logging.getLogger("mcp.server.transport_security").setLevel(logging.ERROR)  # the refusals tested here are expected

MIB = 1024 * 1024
FINAL = ("done", "done_with_warnings", "failed", "cancelled")
SERVICE_TOOLS = ["list_documents", "get_document", "ingest_document"]
ALL_TOOLS = SERVICE_TOOLS + [t["name"] for t in TOOLS]
DECISION_KEYS = {"doc_id", "job_id", "status", "duplicate", "stale", "queued", "warnings"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": LATEST_HANDSHAKE_VERSION, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}}
SLOW = 2.0  # seconds the slow tool of the event-loop test takes


# ---------------------------------------------------------------- helpers


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def body_of(result) -> dict:
    """A tool result's JSON, checked: one text block that parses to the structured content."""
    assert len(result.content) == 1 and result.content[0].type == "text", result.content
    parsed = json.loads(result.content[0].text)
    assert parsed == result.structured_content, "text block and structured content differ"
    return parsed


def ok(result) -> dict:
    body = body_of(result)
    assert not result.is_error, body
    return body


def err(result, type_: str) -> dict:
    body = body_of(result)
    assert result.is_error and body["type"] == type_, body
    assert isinstance(body["message"], str) and body["message"] and set(body) <= {"type", "message", "details"}, body
    return body


_SOURCE: dict = {}


def synthetic_source() -> tuple[Path, Path] | None:
    """(converted folder, PDF) of the synthetic protocol of tests/test_service_api, built once; None without the
    chunk tokenizer in the local cache."""
    if "src" not in _SOURCE:
        if not tokenizer_cached():
            _SOURCE["src"] = None
        else:
            tmp = _SOURCE["tmp"] = Path(tempfile.mkdtemp(prefix="svc_mcp_src_"))
            folder, pdf = tmp / "Quillmore", tmp / "quillmore.pdf"
            build_converted_folder(folder)
            pdf.write_bytes(make_pdf("Quillmore study protocol", "Made-up text for a test"))
            _SOURCE["src"] = (folder, pdf)
    return _SOURCE["src"]


def import_synthetic(service: ProtocolService) -> str:
    from service.import_existing import import_folder

    folder, pdf = synthetic_source()
    return import_folder(service.config, folder, pdf=pdf, version=VERSION, registry=service.registry,
                         storage=service.storage)["doc_id"]


def skip_note(what: str) -> None:
    print(f"   (skipped {what}: the chunk tokenizer is not in the local Hugging Face cache)")


async def wait_done(client, doc_id: str, timeout: float = 20.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        doc = ok(await client.call_tool("get_document", {"doc_id": doc_id}))
        if doc["status"] in FINAL:
            return doc
        if time.monotonic() > deadline:
            raise AssertionError(f"document still {doc['status']} after {timeout}s")
        await anyio.sleep(0.05)


@contextmanager
def serve_http(app):
    """uvicorn serving `app` (lifespan on) on 127.0.0.1 and a free port in a thread; yields the base URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="warning", server_header=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="uvicorn", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(30)
        sock.close()
        assert not thread.is_alive(), "uvicorn did not stop"


def process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True,
                             text=True, errors="replace").stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------- 1. tool listing


def test_tool_listing() -> None:
    definitions = tool_definitions()
    assert [t["name"] for t in definitions] == ALL_TOOLS and len(definitions) == 12
    by_name = {t["name"]: t for t in definitions}
    for tool in TOOLS:
        schema, mine = tool["input_schema"], by_name[tool["name"]]
        assert mine["description"] == tool["description"], tool["name"]
        assert mine["input_schema"] == {**schema, "properties": {"doc_id": DOC_ID, **schema["properties"]},
                                        "required": ["doc_id", *schema.get("required", [])]}, tool["name"]
        assert next(iter(mine["input_schema"]["properties"])) == "doc_id"
    for t in definitions:  # the Anthropic API refuses top-level combinators in a tool's input schema
        schema = t["input_schema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False, t["name"]
        assert not {"oneOf", "anyOf", "allOf"} & set(schema), t["name"]
    assert "doc_id" in by_name["get_document"]["input_schema"]["required"]
    server = build_server(lambda: None)

    async def check(client) -> str:
        async with client:
            tools = (await client.list_tools()).tools
            assert [t.name for t in tools] == ALL_TOOLS
            for t in tools:
                assert t.input_schema == by_name[t.name]["input_schema"], t.name
                assert t.description == by_name[t.name]["description"], t.name
                assert t.annotations.read_only_hint is (t.name != "ingest_document"), t.name
                # the tools that call OpenRouter (search: query embedding and rerank; ingest_document: processing)
                assert t.annotations.open_world_hint is (t.name in ("search", "ingest_document")), t.name
            ingest = next(t for t in tools if t.name == "ingest_document").annotations
            assert ingest.destructive_hint is False and ingest.idempotent_hint is True
            assert client.server_info.name == "protocol-service" and client.server_info.version == PIPELINE_VERSION
            assert "quote_check" in client.instructions and "get_document" in client.instructions
            err(await client.call_tool("list_documents", {}), "not_started")  # the getter returns no service
            return client.protocol_version

    eras = [anyio.run(check, client) for client in (Client(server), Client(server, mode="legacy"),
                                                     Client(InMemoryTransport(server)))]
    assert eras == [LATEST_MODERN_VERSION, LATEST_HANDSHAKE_VERSION, LATEST_MODERN_VERSION], eras


def test_settings_helpers() -> None:
    settings = transport_security(("localhost", "127.0.0.1", "::1"))
    assert settings.enable_dns_rebinding_protection
    assert {"localhost", "localhost:*", "127.0.0.1:*", "[::1]", "[::1]:*"} <= set(settings.allowed_hosts)
    assert {"http://localhost:*", "https://127.0.0.1", "http://[::1]:*"} <= set(settings.allowed_origins)
    check = TransportSecurityMiddleware(settings)
    assert check._validate_host("localhost:8000") and check._validate_host("[::1]:8000")
    assert check._validate_origin("http://127.0.0.1:3000") and check._validate_origin(None)
    for host in ("evil.example", "evil.example:8000", "localhost.evil.example", "127.0.0.2:8000"):
        assert not check._validate_host(host), host
    for origin in ("http://evil.example", "http://localhost.evil.example", "null", "file://"):
        assert not check._validate_origin(origin), origin
    assert not transport_security(["*"]).enable_dns_rebinding_protection
    assert transport_security(["example.org"]).allowed_hosts == ["example.org", "example.org:*"]
    for mb in (1, 100):  # a base64 PDF of MAX_UPLOAD_MB plus the envelope fits
        assert request_body_limit(mb) >= len(b64(b"\0" * (mb * MIB))) + MIB
    with temp_dir() as tmp:
        inbox = tmp / "inbox"
        (inbox / "sub").mkdir(parents=True)
        assert inbox_name("a.pdf", inbox) == "a.pdf" and inbox_name("inbox/a.pdf", inbox) == "a.pdf"
        assert inbox_name("inbox\\a.pdf", inbox) == "a.pdf" and inbox_name(str(inbox / "a.pdf"), inbox) == "a.pdf"
        assert inbox_name(str(inbox / "sub" / ".." / "a.pdf"), inbox) == "a.pdf"  # still the inbox itself
        for bad in ("", "../a.pdf", "inbox/../a.pdf", "sub/a.pdf", "inbox/sub/a.pdf", str(inbox / "sub" / "a.pdf"),
                    str(tmp / "a.pdf"), str(inbox / ".." / "a.pdf"), "a\x00.pdf", "x/inbox/a.pdf"):
            try:
                inbox_name(bad, inbox)
            except Exception as exc:  # noqa: BLE001
                assert getattr(exc, "code", None) == "invalid_name", bad
                continue
            raise AssertionError(f"accepted {bad!r}")


# ---------------------------------------------------------------- 2. documents and ingest, in process


def test_documents_and_ingest() -> None:
    calls: list[dict] = []
    with temp_dir() as tmp:
        service = make_service(tmp / "data", pipeline=make_pipeline(calls=calls), max_upload_mb=1)
        service.start()
        try:
            anyio.run(documents_and_ingest, service, tmp, calls)
        finally:
            service.stop()


async def documents_and_ingest(service: ProtocolService, tmp: Path, calls: list) -> None:
    inbox = service.config.inbox_dir
    pdfs = {name: make_pdf(f"Inbox study {name}") for name in ("a.pdf", "b.pdf", "c.pdf")}
    for name, data in pdfs.items():
        (inbox / name).write_bytes(data)
    (inbox / "sub").mkdir()
    (inbox / "sub" / "d.pdf").write_bytes(make_pdf("Inbox subfolder study"))
    (tmp / "outside.pdf").write_bytes(make_pdf("Outside study"))
    async with Client(build_server(lambda: service)) as client:
        listed = ok(await client.call_tool("list_documents", {}))
        assert listed == {"documents": [], "limit": 100, "offset": 0} == service.list_documents()
        # base64: a queued job (a data URL prefix is accepted)
        first, second = make_pdf("MCP base64 study"), make_pdf("MCP data URL study")
        d1 = ok(await client.call_tool("ingest_document", {"data_base64": b64(first), "filename": "first.pdf"}))
        assert set(d1) == DECISION_KEYS and d1["doc_id"] == sha(first) and d1["queued"] and len(d1["job_id"]) == 32
        assert d1["status"] in ("queued", "running") and not d1["duplicate"] and not d1["stale"]
        d2 = ok(await client.call_tool("ingest_document",
                                       {"data_base64": "data:application/pdf;base64," + b64(second)}))
        assert d2["doc_id"] == sha(second) and d2["queued"]
        # the inbox: a bare name, inbox/<name>, the absolute path
        inboxed = [ok(await client.call_tool("ingest_document", {"path": p}))
                   for p in ("a.pdf", "inbox/b.pdf", str(inbox / "c.pdf"))]
        assert [d["doc_id"] for d in inboxed] == [sha(pdfs[n]) for n in ("a.pdf", "b.pdf", "c.pdf")]
        assert all(d["queued"] for d in inboxed)
        for bad in ("../registry.sqlite", "inbox/../registry.sqlite", "..\\registry.sqlite", "sub/d.pdf",
                    "inbox/sub/d.pdf", str(inbox / "sub" / "d.pdf"), str(tmp / "outside.pdf"),
                    str(inbox / ".." / "registry.sqlite"), str(service.config.registry_path), "inbox", "",
                    "notes.txt"):
            err(await client.call_tool("ingest_document", {"path": bad}), "invalid_name")
        err(await client.call_tool("ingest_document", {"path": "missing.pdf"}), "file_not_found")
        for args in ({}, {"path": "a.pdf", "data_base64": b64(first)}, {"filename": "x.pdf", "reprocess": True}):
            body = err(await client.call_tool("ingest_document", args), "invalid_arguments")
            assert body["details"] == {"tool": "ingest_document"}
        big = b"%PDF-1.4\n" + b"0" * (MIB + 100)  # MAX_UPLOAD_MB is 1 here
        err(await client.call_tool("ingest_document", {"data_base64": b64(big)}), "too_large")
        err(await client.call_tool("ingest_document", {"data_base64": "@@@ not base64 @@@"}), "invalid_base64")
        err(await client.call_tool("ingest_document", {"data_base64": ""}), "empty")
        err(await client.call_tool("ingest_document", {"data_base64": b64(b"hello, not a pdf")}), "not_pdf")
        # get_document: how clients poll; the same dict as the service's
        for d in (d1, d2, *inboxed):
            assert (await wait_done(client, d["doc_id"]))["status"] == "done"
        doc = ok(await client.call_tool("get_document", {"doc_id": d1["doc_id"]}))
        assert doc == service.get_document(d1["doc_id"])
        assert doc["filenames"] == ["first.pdf"] and doc["current_job"]["state"] == "done" and doc["has_result"]
        again = ok(await client.call_tool("ingest_document", {"data_base64": b64(first)}))
        assert again["duplicate"] and again["job_id"] is None and not again["queued"] and again["status"] == "done"
        page = ok(await client.call_tool("list_documents", {"status": "done", "limit": 2, "offset": 1}))
        assert page == service.list_documents(status="done", limit=2, offset=1) and len(page["documents"]) == 2
        err(await client.call_tool("get_document", {"doc_id": "f" * 64}), "document_not_found")
        err(await client.call_tool("get_document", {"doc_id": "xyz"}), "invalid_id")
        # a password reaches the pipeline (this process runs the worker) and is never echoed
        password = "pw-" + secrets.token_hex(8)
        d = ok(await client.call_tool("ingest_document", {"data_base64": b64(make_pdf("Password study")),
                                                          "password": password}))
        assert (await wait_done(client, d["doc_id"]))["status"] == "done" and calls[-1]["password"] == password
        assert password not in json.dumps(d) and len(service.registry.pdf_passwords) == 0
        # arguments against the input schema: invalid_arguments naming the tool, never the value sent
        secret = "SECRET-" + secrets.token_hex(6)
        for name, args in (("get_document", {}), ("get_document", {"doc_id": 5}),
                           ("list_documents", {"limit": "many"}), ("list_documents", {"status": secret}),
                           ("list_documents", {"limit": 5000}), ("list_documents", {"bogus": 1}),
                           ("ingest_document", {"path": "a.pdf", "reprocess": secret}),
                           ("ingest_document", {"path": "a.pdf", "password": 987654321987}),
                           ("ingest_document", {"path": "a.pdf", "filename": 123456789123}),
                           ("read_section", {"doc_id": d1["doc_id"], "max_tokens": 10}),
                           ("read_section", {"doc_id": d1["doc_id"], "section": 5}),
                           ("quote_check", {"doc_id": d1["doc_id"]}), ("search", {"query": "dose"}),
                           ("find_sections", {"doc_id": d1["doc_id"], "role": secret}),
                           ("outline", {"doc_id": d1["doc_id"], "with_roles": "yes"})):
            body = err(await client.call_tool(name, args), "invalid_arguments")
            assert body["details"] == {"tool": name} and body["message"].startswith(f"invalid arguments for {name}")
            text = json.dumps(body)
            assert secret not in text and "987654321987" not in text and "123456789123" not in text, body
        body = err(await client.call_tool("no_such_tool", {}), "unknown_tool")
        assert body["details"]["tools"] == ALL_TOOLS
        root, capture = logging.getLogger(), Capture()
        root.addHandler(capture)
        original = service.list_documents

        def broken(**kwargs):
            raise ValueError("SECRET TEXT quoted from a document")

        try:
            # a protocol tool on a document whose stored result cannot be opened (the fake pipeline's): the
            # service's own error
            err(await client.call_tool("document_info", {"doc_id": d1["doc_id"]}), "document_unreadable")
            # an unexpected exception: internal_error naming the type; the log has the type and our file:line only
            service.list_documents = broken
            body = err(await client.call_tool("list_documents", {}), "internal_error")
            assert body == {"type": "internal_error", "message": "internal error (ValueError)"}, body
        finally:
            service.list_documents = original
            root.removeHandler(capture)
        logged = [line for line in capture.lines if line.startswith("service.mcp ")]
        assert logged and "ValueError" in logged[0] and "tests/test_service_mcp.py:" in logged[0], logged
        assert not any("SECRET" in line for line in capture.lines)
        assert ok(await client.call_tool("list_documents", {"limit": 1}))["limit"] == 1  # still serving

    # without a worker in the process a password is refused; a plain upload is queued for another worker
    no_worker = make_service(tmp / "data2", run_worker=False)
    try:
        async with Client(build_server(lambda: no_worker), mode="legacy") as client:
            body = err(await client.call_tool("ingest_document", {"data_base64": b64(make_pdf("Locked study")),
                                                                  "password": "pw-123"}), "password_unsupported_here")
            assert "pw-123" not in json.dumps(body) and no_worker.list_documents()["documents"] == []
            queued = ok(await client.call_tool("ingest_document", {"data_base64": b64(make_pdf("Queued study"))}))
            assert queued["queued"] and no_worker.get_job(queued["job_id"])["state"] == "queued"
    finally:
        no_worker.stop()


# ---------------------------------------------------------------- 3. parity with the REST API


def test_parity_with_rest() -> None:
    if synthetic_source() is None:
        skip_note("REST parity")
        return
    with temp_dir() as tmp:
        service = make_service(tmp / "data", embed_fn=fake_vector, rerank_fn=fake_rerank, run_worker=False)
        doc = import_synthetic(service)
        with TestClient(api.create_app(service), base_url="http://127.0.0.1") as rest:
            for client in (Client(build_server(lambda: service)), Client(build_server(lambda: service),
                                                                          mode="legacy")):
                anyio.run(parity, client, rest, doc)


async def parity(client, rest, doc: str) -> None:
    async with client:
        for name, args in TOOL_CALLS.items():
            r = rest.post(f"/documents/{doc}/tools/{name}", json=args)
            assert r.status_code == 200, (name, r.status_code)
            assert ok(await client.call_tool(name, {"doc_id": doc, **args})) == r.json(), name
        for name in ("document_info", "outline", "get_soa"):  # the tool's own defaults on both sides
            assert ok(await client.call_tool(name, {"doc_id": doc})) == rest.post(
                f"/documents/{doc}/tools/{name}").json(), name
        assert ok(await client.call_tool("get_document", {"doc_id": doc})) == rest.get(f"/documents/{doc}").json()
        assert ok(await client.call_tool("list_documents", {})) == rest.get("/documents").json()
        for name, args, status, type_ in (("read_section", {"section": "S99"}, 404, "not_found"),
                                          ("get_table", {"table": "#/tables/99"}, 404, "not_found"),
                                          ("abbreviation", {"term": "ZZQ"}, 404, "not_found")):
            r = rest.post(f"/documents/{doc}/tools/{name}", json=args)
            if r.status_code == 200:  # a lookup that answers "not found" in its result: same result
                assert ok(await client.call_tool(name, {"doc_id": doc, **args})) == r.json(), name
                continue
            assert r.status_code == status and r.json()["type"] == type_, (name, r.status_code)
            assert err(await client.call_tool(name, {"doc_id": doc, **args}), type_) == r.json(), name
        for other, type_ in (("d" * 64, "document_not_found"), ("xyz", "invalid_id")):
            r = rest.post(f"/documents/{other}/tools/outline", json={})
            assert err(await client.call_tool("outline", {"doc_id": other}), type_) == r.json()


# ---------------------------------------------------------------- 4. Streamable HTTP


def test_streamable_http() -> None:
    with temp_dir() as tmp:
        service = make_service(tmp / "data", embed_fn=fake_vector, rerank_fn=fake_rerank, max_upload_mb=1)
        if synthetic_source() is not None:
            doc, protocol_tools = import_synthetic(service), True
        else:
            skip_note("protocol tools over HTTP")
            doc, protocol_tools = service.ingest(make_pdf("HTTP study"), "http.pdf")["doc_id"], False
        app = api.create_app(service)
        endpoint = attach(app)
        assert attach(app) is endpoint and [r.path for r in app.routes].count("/mcp") == 1
        assert endpoint.manager is None  # runs with the app only
        with serve_http(app) as base:
            manager = endpoint.manager
            assert manager is not None and manager.stateless and manager.json_response
            assert manager.max_request_body_size == request_body_limit(1)
            assert manager.security_settings == transport_security(service.config.allowed_hosts)
            assert manager.security_settings.enable_dns_rebinding_protection
            anyio.run(http_checks, base, doc, protocol_tools)
        assert endpoint.manager is None


async def http_checks(base: str, doc: str, protocol_tools: bool) -> None:
    url, port = f"{base}/mcp", base.rsplit(":", 1)[1]
    with httpx.Client(base_url=base, timeout=30) as rest:
        deadline = time.monotonic() + 20
        while rest.get(f"/documents/{doc}").json()["status"] not in FINAL:  # the fake pipeline, without a tokenizer
            assert time.monotonic() < deadline
            time.sleep(0.05)
        for client, era in ((Client(url), LATEST_MODERN_VERSION), (Client(url, mode="legacy"),
                                                                    LATEST_HANDSHAKE_VERSION)):
            async with client:
                assert client.protocol_version == era and client.server_info.name == "protocol-service"
                assert [t.name for t in (await client.list_tools()).tools] == ALL_TOOLS
                assert ok(await client.call_tool("get_document", {"doc_id": doc})) == rest.get(
                    f"/documents/{doc}").json()
                if protocol_tools:  # every protocol tool over real JSON: the REST result
                    for name, args in TOOL_CALLS.items():
                        r = rest.post(f"/documents/{doc}/tools/{name}", json=args)
                        assert ok(await client.call_tool(name, {"doc_id": doc, **args})) == r.json(), name
                    r = rest.post(f"/documents/{doc}/tools/read_section", json={"section": "S99"})
                    assert err(await client.call_tool("read_section", {"doc_id": doc, "section": "S99"}),
                               "not_found") == r.json()
        # exactly /mcp: answered there, no redirect, no session
        r = rest.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json"), r.status_code
        assert "mcp-session-id" not in r.headers and r.json()["result"]["serverInfo"]["name"] == "protocol-service"
        for method in ("GET", "DELETE"):
            r = rest.request(method, "/mcp", headers=MCP_HEADERS)
            assert r.status_code == 405 and r.headers["allow"] == "POST", (method, r.status_code)
        assert rest.post("/mcp/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code == 404
        # DNS rebinding: a foreign Host or Origin is refused (by the SDK or by the app), localhost names pass
        modern = {**MCP_HEADERS, "MCP-Protocol-Version": LATEST_MODERN_VERSION}
        for headers in ({"Host": "evil.example"}, {"Host": f"evil.example:{port}"}, {"Origin": "http://evil.example"},
                        {"Origin": f"http://evil.example:{port}"}, {"Origin": "null"}):
            for base_headers, payload in ((MCP_HEADERS, INITIALIZE),
                                          (modern, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})):
                r = rest.post("/mcp", json=payload, headers={**base_headers, **headers})
                assert r.status_code in (400, 403, 421), (headers, r.status_code)
        for headers in ({"Origin": base}, {"Origin": f"http://localhost:{port}"}, {"Host": f"localhost:{port}"}):
            r = rest.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **headers})
            assert r.status_code == 200, (headers, r.status_code)
        # body limit: a base64 PDF above MAX_UPLOAD_MB but within the envelope margin reaches the service
        def ingest(data: bytes) -> httpx.Response:
            return rest.post("/mcp", headers={**MCP_HEADERS, "MCP-Protocol-Version": LATEST_HANDSHAKE_VERSION}, json={
                "jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "ingest_document", "arguments": {"data_base64": b64(data)}}})

        r = ingest(b"%PDF-1.4\n" + b"0" * (MIB + 100 * 1024))
        assert r.status_code == 200 and r.json()["result"]["isError"], r.status_code
        assert r.json()["result"]["structuredContent"]["type"] == "too_large"
        assert ingest(b"%PDF-1.4\n" + b"0" * (2 * MIB)).status_code == 413  # beyond it: the transport refuses
        # arguments that are not a JSON object never reach the tool: the SDK answers INVALID_PARAMS
        r = rest.post("/mcp", headers={**MCP_HEADERS, "MCP-Protocol-Version": LATEST_HANDSHAKE_VERSION}, json={
            "jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "outline", "arguments": [1, 2]}})
        assert r.json()["error"]["code"] == -32602, (r.status_code, r.text[:200])
        # the REST API on the same server
        assert rest.get("/health").status_code == 200


# ---------------------------------------------------------------- 5. the event loop is not blocked


def test_event_loop_not_blocked() -> None:
    with temp_dir() as tmp:
        service = make_service(tmp / "data")
        started, finished = threading.Event(), threading.Event()

        def slow_get_document(doc_id: str) -> dict:
            started.set()
            time.sleep(SLOW)
            finished.set()
            return {"doc_id": doc_id, "slow": True}

        service.get_document = slow_get_document
        app = api.create_app(service)
        attach(app)
        with serve_http(app) as base:
            anyio.run(not_blocked, base, started, finished)


async def not_blocked(base: str, started: threading.Event, finished: threading.Event) -> None:
    out: dict = {}
    async with Client(f"{base}/mcp") as client:
        async def slow_call() -> None:
            out["slow"] = await client.call_tool("get_document", {"doc_id": "a" * 64})

        async with anyio.create_task_group() as tg:
            tg.start_soon(slow_call)
            assert await anyio.to_thread.run_sync(started.wait, 10)
            t0 = time.perf_counter()
            health = await anyio.to_thread.run_sync(lambda: httpx.get(f"{base}/health", timeout=10))
            health_s = time.perf_counter() - t0
            t0 = time.perf_counter()
            listed = await client.call_tool("list_documents", {})
            mcp_s = time.perf_counter() - t0
            busy = not finished.is_set()
    assert health.status_code == 200 and ok(listed)["documents"] == []
    assert busy and health_s < SLOW / 2 and mcp_s < SLOW / 2, (busy, round(health_s, 2), round(mcp_s, 2))
    assert ok(out["slow"]) == {"doc_id": "a" * 64, "slow": True}


# ---------------------------------------------------------------- 6. stdio


def test_stdio() -> None:
    with temp_dir() as tmp:
        data_dir = tmp / "data"
        service = make_service(data_dir, run_worker=False)
        doc = expected = None
        if synthetic_source() is not None:
            doc = import_synthetic(service)
            expected = service.call_tool(doc, "read_section", {"section": "inclusion"})
        else:
            skip_note("read_section over stdio")
        service.stop()
        env = {**os.environ, "DATA_DIR": str(data_dir), "OPENROUTER_OFFLINE": "1", "HF_HUB_OFFLINE": "1",
               "HF_HUB_DISABLE_SYMLINKS_WARNING": "1", "PYTHONIOENCODING": "utf-8", "LOG_LEVEL": "INFO"}
        params = StdioServerParameters(command=sys.executable, args=["-m", "service.mcp_stdio"], env=env,
                                       cwd=str(ROOT))
        errlog_path = tmp / "stderr.log"
        root, capture = logging.getLogger(), Capture()
        root.addHandler(capture)
        try:
            with open(errlog_path, "w", encoding="utf-8") as errlog:
                handshake_s, first_s, job_id = anyio.run(stdio_session, params, errlog, doc, expected)
        finally:
            root.removeHandler(capture)
        # every stdout line was JSON-RPC: the client logs a line it cannot parse
        assert not [line for line in capture.lines if line.startswith("mcp.")], capture.lines[:3]
        log_text = errlog_path.read_text(encoding="utf-8", errors="replace")
        found = re.search(r"pid=(\d+)", log_text)
        assert found, "no pid in the server log"
        pid, deadline = int(found.group(1)), time.monotonic() + 10
        while process_alive(pid):
            assert time.monotonic() < deadline, "the stdio server is still running"
            time.sleep(0.1)
        assert "MCP stdio server stopped" in log_text  # it exited on its own when stdin closed
        assert "Able to swallow" not in log_text and "Traceback" not in log_text
        with Registry(ServiceConfig(data_dir=data_dir)) as registry:  # no worker ran the job it queued
            assert registry.get_job(job_id)["state"] == "queued"
        print(f"   (stdio: handshake {handshake_s:.1f}s, first tool result {first_s:.1f}s)")


async def stdio_session(params, errlog, doc: str | None, expected: dict | None) -> tuple[float, float, str]:
    t0 = time.perf_counter()
    async with Client(stdio_client(params, errlog=errlog), mode="legacy") as client:
        handshake_s = time.perf_counter() - t0
        assert client.server_info.name == "protocol-service"
        assert [t.name for t in (await client.list_tools()).tools] == ALL_TOOLS
        if doc is not None:
            info = ok(await client.call_tool("get_document", {"doc_id": doc}))
            first_s = time.perf_counter() - t0
            assert info["doc_id"] == doc and info["status"] == "done" and info["has_result"]
            assert ok(await client.call_tool("read_section", {"doc_id": doc, "section": "inclusion"})) == expected
        else:
            assert ok(await client.call_tool("list_documents", {}))["documents"] == []
            first_s = time.perf_counter() - t0
        password = "pw-" + secrets.token_hex(8)
        body = err(await client.call_tool("ingest_document", {"data_base64": b64(make_pdf("Stdio locked study")),
                                                              "password": password}), "password_unsupported_here")
        assert password not in json.dumps(body)
        queued = ok(await client.call_tool("ingest_document", {"data_base64": b64(make_pdf("Stdio queued study"))}))
        assert queued["queued"] and queued["status"] == "queued"
    return handshake_s, first_s, queued["job_id"]


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    start_all = time.time()
    try:
        for t in tests:
            start = time.time()
            try:
                t()
                print(f"ok {t.__name__} ({time.time() - start:.1f}s)")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {t.__name__}")
                traceback.print_exc()
    finally:
        if _SOURCE.get("tmp"):
            shutil.rmtree(_SOURCE["tmp"], ignore_errors=True)
    print(f"{len(tests) - failed} passed in {time.time() - start_all:.1f}s" if not failed
          else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
