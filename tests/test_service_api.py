"""Self-test for the REST API (service.api): `python tests/test_service_api.py` (no pytest, offline).

TestClient against create_app(ProtocolService(...)) with a temporary DATA_DIR per test, synthetic PDFs written by
hand (made-up text, tests/test_service_core.make_pdf) and fake pipeline functions (service.pipeline never runs).
The protocol tools run on a small synthetic Docling document (made-up text) chunked by chunk_protocol, with fake
embeddings, imported as a published result; search uses deterministic fake query embeddings and reranker. No
network: OPENROUTER_OFFLINE=1 and HF_HUB_OFFLINE=1 (the chunk tokenizer, Qwen/Qwen3-Embedding-8B, must be in the
local Hugging Face cache; without it the tool test is skipped with a note). Output: counts and booleans only.
"""

import hashlib
import json
import logging
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import traceback
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fastapi.testclient import TestClient  # noqa: E402

from protocol_api import TOOLS  # noqa: E402
from service import api  # noqa: E402
from service.app_service import ProtocolService  # noqa: E402
from service.config import ServiceConfig  # noqa: E402
from service.contracts import STAGES, PipelineResult, StageResult  # noqa: E402
from service.registry import Registry, inspect_pdf_in_process  # noqa: E402
from test_service_core import make_pdf, sha  # noqa: E402

VERSION = ("1.0.0", "fp-test")
LOCAL = "http://127.0.0.1"  # the app refuses Host headers outside ALLOWED_HOSTS (TestClient's default is "testserver")
FINAL = ("done", "done_with_warnings", "failed", "cancelled")
TOKENIZER = "Qwen/Qwen3-Embedding-8B"
DIM = 16  # dimension of the fake embeddings


# ---------------------------------------------------------------- fake pipelines and fixtures


RESULT_FILES = {  # what the fake pipeline publishes
    "protocol.md": b"# Synthetic result\n",
    "protocol.json": b'{"name": "synthetic"}',
    "protocol.html": b'<html><body><img src="images/image_000.png"></body></html>',
    "protocol.decisions.json": b'{"decisions": []}',
    "chunks/chunks.jsonl": b'{"chunk_id": "c1"}\n',
    "images/image_000.png": b"\x89PNG\r\n\x1a\n" + b"\0" * 16,
}


class Gate:
    """Holds a fake pipeline run until released (a job that stays "running")."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()


def make_pipeline(version=VERSION, gate: Gate | None = None, calls: list | None = None):
    def pipeline(pdf_path, workdir, config, *, settings=None, progress=None, password=None):
        data = Path(pdf_path).read_bytes()
        if calls is not None:
            calls.append({"sha": sha(data), "password": password})
        if gate is not None:
            gate.started.set()
            assert gate.release.wait(30), "gate never released"
        for i, stage in enumerate(STAGES, 1):
            if progress:
                progress(stage, i, len(STAGES))
        (workdir / "chunks").mkdir(exist_ok=True)
        (workdir / "images").mkdir(exist_ok=True)
        for name, content in RESULT_FILES.items():
            (workdir / name).write_bytes(content)
        return PipelineResult(status="done", pipeline_version=version[0], fingerprint=version[1], sha256=sha(data),
                              pages=1, identifiers={}, quality={"score": 1.0}, warnings=[],
                              stages=[StageResult(name=s, status="done") for s in STAGES])

    return pipeline


@contextmanager
def temp_dir():
    tmp = Path(tempfile.mkdtemp(prefix="svc_api_"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_service(data_dir: Path, *, pipeline=None, version=VERSION, max_upload_mb: int = 100,
                 **kwargs) -> ProtocolService:
    config = ServiceConfig(data_dir=data_dir, offline=True, max_upload_mb=max_upload_mb)
    return ProtocolService(config, version=version, pipeline_fn=pipeline or make_pipeline(version),
                           registry=Registry(config, inspector=inspect_pdf_in_process),
                           worker_options={"poll_interval": 0.05}, **kwargs)


@contextmanager
def client_for(service: ProtocolService):
    """A TestClient running the app's lifespan: the service is started, and stopped on exit."""
    with TestClient(api.create_app(service), base_url=LOCAL) as client:
        yield client


@contextmanager
def app_env(**kwargs):
    with temp_dir() as tmp:
        service = make_service(tmp / "data", **kwargs)
        with client_for(service) as client:
            yield client, service


def upload(client, data: bytes, name: str = "protocol.pdf", **form):
    fields = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in form.items()}
    return client.post("/documents", files={"file": (name, data, "application/pdf")}, data=fields)


def wait_job(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while True:
        job = client.get(f"/jobs/{job_id}").json()
        if job["state"] in FINAL:
            return job
        if time.time() > deadline:
            raise AssertionError(f"job still {job['state']} after {timeout}s")
        time.sleep(0.03)


def error_of(response, status: int, type_: str) -> dict:
    """The JSON error body, checked for status, type and shape."""
    assert response.status_code == status, (response.status_code, response.text[:300])
    body = response.json()
    assert body["type"] == type_, body
    assert isinstance(body["message"], str) and body["message"], body
    assert set(body) <= {"type", "message", "details"}, body
    return body


def no_inputs(body: dict) -> None:
    for e in body["details"]["errors"]:
        assert set(e) == {"loc", "msg", "type"}, e


# ---------------------------------------------------------------- 1. service routes and OpenAPI


def test_health_version_openapi() -> None:
    with app_env() as (client, service):
        r = client.get("/health")
        assert r.status_code == 200 and r.json()["status"] == "ok", r.text
        assert set(r.json()) == {"status", "registry", "worker", "jobs", "offline"} and r.json()["worker"] == "running"
        v = client.get("/version").json()
        assert v["pipeline_version"] == "1.0.0" and v["fingerprint"] == "fp-test" and v["offline"] is True
        assert {"packages", "models", "config", "fingerprint_inputs"} <= set(v) and "fastapi" in v["packages"]
        spec = client.get("/openapi.json").json()
        from pipeline_version import PIPELINE_VERSION

        assert spec["info"]["title"] == "Protocol service" and spec["info"]["version"] == PIPELINE_VERSION
        paths = spec["paths"]
        for tool in TOOLS:
            op = paths[f"/documents/{{doc_id}}/tools/{tool['name']}"]["post"]
            assert op["operationId"] == f"tool_{tool['name']}" and op["tags"] == ["tools"]
            assert tool["description"] in op["description"] and op["summary"]
            if tool["input_schema"]["properties"]:
                ref = json.dumps(op["requestBody"])
                assert "#/components/schemas/" in ref, tool["name"]
        assert paths["/documents/{doc_id}/tools/{tool_name}"]["post"]["operationId"] == "call_tool"
        typed = [p for p in paths if p.startswith("/documents/{doc_id}/tools/")]
        assert len(typed) == len(TOOLS) + 1
        for p in ("/health", "/version", "/documents", "/documents/{doc_id}", "/documents/{doc_id}/reprocess",
                  "/documents/{doc_id}/jobs", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/cancel",
                  "/documents/{doc_id}/files", "/documents/{doc_id}/files/{name}", "/tools"):
            assert p in paths, p
        upload_body = json.dumps(paths["/documents"]["post"]["requestBody"])
        assert "multipart/form-data" in upload_body
        form = spec["components"]["schemas"][upload_body.split("#/components/schemas/")[1].split('"')[0]]
        assert set(form["properties"]) == {"file", "reprocess", "password"} and form["required"] == ["file"]
        assert '"format": "password"' in json.dumps(form["properties"]["password"])  # masked in /docs
        assert paths["/documents"]["post"]["responses"]["422"]["content"]["application/json"]["schema"]["$ref"] \
            .endswith("/ErrorBody")
        tags = {t["name"] for t in spec["tags"]}
        assert tags == {"documents", "jobs", "files", "tools", "service"}
        docs = client.get("/docs")
        assert docs.status_code == 200 and docs.headers["content-type"].startswith("text/html")
        tools = client.get("/tools").json()["tools"]
        assert [t["name"] for t in tools] == [t["name"] for t in TOOLS]
        assert client.get("/tools", params={"style": "openai"}).json()["tools"][0]["type"] == "function"
        error_of(client.get("/tools", params={"style": "xml"}), 400, "invalid_style")
        service.worker.stop(timeout=5)  # a stopped worker: unhealthy, 503 for the Docker healthcheck
        r = client.get("/health")
        assert r.status_code == 503 and r.json()["status"] == "unavailable" and r.json()["worker"] == "stopped"


def test_tool_models() -> None:
    from pydantic import ValidationError

    for tool in TOOLS:  # every tool schema maps (create_app would fail otherwise)
        schema = tool["input_schema"]
        model = api.tool_model(tool["name"], schema)
        js = model.model_json_schema()
        assert set(js.get("properties", {})) == set(schema["properties"]), tool["name"]
        assert set(js.get("required", [])) == set(schema.get("required", [])), tool["name"]
        assert js.get("additionalProperties") is False, tool["name"]
        for prop, spec in schema["properties"].items():
            got = js["properties"][prop]
            for key in ("default", "enum", "minimum", "maximum", "description"):
                assert got.get(key) == spec.get(key), (tool["name"], prop, key)
    m = api.tool_model("read_section", next(t for t in TOOLS if t["name"] == "read_section")["input_schema"])
    assert m(section="5.1").model_dump(exclude_unset=True) == {"section": "5.1"}
    for bad in ({"max_tokens": "300"}, {"max_tokens": 100}, {"include_subsections": "yes"}, {"section": 5},
                {"bogus": 1}, {"max_tokens": True}):
        try:
            m(**bad)
        except ValidationError:
            continue
        raise AssertionError(f"accepted {sorted(bad)}")
    f = api.tool_model("find_sections", next(t for t in TOOLS if t["name"] == "find_sections")["input_schema"])
    for bad in ({"role": "no_such_role"}, {"limit": 0}, {"limit": 51}):
        try:
            f(**bad)
        except ValidationError:
            continue
        raise AssertionError(f"accepted {sorted(bad)}")
    # a schema the REST layer cannot express fails loudly
    for schema in ({"type": "object", "properties": {"x": {"type": "array"}}, "additionalProperties": False},
                   {"type": "object", "properties": {"x": {"type": "string", "pattern": "a+"}},
                    "additionalProperties": False},
                   {"type": "object", "properties": {"x": {"type": "number"}}, "additionalProperties": False},
                   {"type": "object", "properties": {}, "additionalProperties": True},
                   {"type": "object", "properties": {}, "oneOf": []},
                   {"type": "object", "properties": {"x": {"type": "string", "enum": [1]}},
                    "additionalProperties": False},
                   {"type": "object", "properties": {"x": {"type": "string", "minimum": 1}},
                    "additionalProperties": False},
                   {"type": "object", "properties": {}, "required": ["y"], "additionalProperties": False}):
        try:
            api.tool_model("t", schema)
        except ValueError as exc:
            assert "tool t" in str(exc)
            continue
        raise AssertionError(f"mapped {schema}")


def test_lifespan_owns_service_and_hooks() -> None:
    with temp_dir() as tmp:
        data_dir = tmp / "data"
        old = os.environ.get("DATA_DIR")
        os.environ["DATA_DIR"] = str(data_dir)
        events: list[str] = []

        @asynccontextmanager
        async def hook(app):
            events.append(f"enter:{app.state.service.health()['worker']}")
            yield
            events.append("exit")

        try:
            app = api.create_app(lifespan_hooks=[hook])
            assert not data_dir.exists() and app.state.service is None  # nothing touched before startup
            app.state.lifespan_hooks.append(hook)
            with TestClient(app, base_url=LOCAL) as client:
                assert data_dir.is_dir() and app.state.service is not None
                assert client.get("/health").status_code == 200
                assert app.state.service.config.data_dir == data_dir.resolve()
            assert app.state.service is None and events == ["enter:running", "enter:running", "exit", "exit"]
            # a given service with manage_lifecycle=False is neither started nor stopped by the app
            service = make_service(tmp / "other")
            try:
                with TestClient(api.create_app(service, manage_lifecycle=False), base_url=LOCAL) as client:
                    assert client.get("/health").json()["worker"] == "not_started"
                    assert client.get("/health").status_code == 503
                assert client.app.state.service is service and service.health()["registry"] == "ok"
            finally:
                service.stop()
            # without the lifespan (no `with`), the routes answer 503 not_started
            error_of(TestClient(api.create_app(), base_url=LOCAL).get("/health"), 503, "not_started")
        finally:
            if old is None:
                os.environ.pop("DATA_DIR", None)
            else:
                os.environ["DATA_DIR"] = old


# ---------------------------------------------------------------- 2. upload lifecycle


def test_upload_lifecycle() -> None:
    gate = Gate()
    with temp_dir() as tmp:
        data_dir = tmp / "data"
        first, second = make_pdf("Lifecycle study one", "NCT00000011"), make_pdf("Lifecycle study two")
        service = make_service(data_dir, pipeline=make_pipeline(gate=gate))
        with client_for(service) as client:
            try:
                r = upload(client, first, "first.pdf")
                assert r.status_code == 202, r.text
                d1 = r.json()
                assert d1["queued"] and not d1["duplicate"] and not d1["stale"] and d1["status"] == "queued"
                assert d1["doc_id"] == sha(first) and len(d1["job_id"]) == 32 and d1["warnings"] == []
                assert set(d1) == {"doc_id", "job_id", "status", "duplicate", "stale", "queued", "warnings"}
                assert gate.started.wait(10)
                r = upload(client, first, "again.pdf")  # running: that job
                assert r.status_code == 200 and r.json()["job_id"] == d1["job_id"] and r.json()["duplicate"]
                assert r.json()["status"] == "running" and not r.json()["queued"]
                d2 = upload(client, second).json()  # queued behind the running job
                r = upload(client, second)
                assert r.status_code == 200 and r.json()["job_id"] == d2["job_id"] and r.json()["status"] == "queued"
                running = client.get(f"/documents/{d1['doc_id']}").json()
                assert running["status"] == "running" and not running["has_result"] and running["files"] == []
                assert running["current_job"]["state"] == "running"
            finally:
                gate.release.set()
            assert wait_job(client, d1["job_id"])["state"] == "done"
            job = wait_job(client, d2["job_id"])
            assert job["state"] == "done" and job["progress"] == {"done": len(STAGES), "total": len(STAGES)}
            doc = client.get(f"/documents/{d1['doc_id']}").json()
            assert doc["status"] == "done" and doc["has_result"] and doc["stale"] is False
            assert doc["filenames"] == ["first.pdf", "again.pdf"] and "protocol.md" in doc["files"]
            r = upload(client, first)
            assert r.status_code == 200 and r.json() == {"doc_id": d1["doc_id"], "job_id": None, "status": "done",
                                                         "duplicate": True, "stale": False, "queued": False,
                                                         "warnings": []}
            listed = client.get("/documents").json()
            assert {d["doc_id"] for d in listed["documents"]} == {d1["doc_id"], d2["doc_id"]}
            assert listed["limit"] == 100 and listed["offset"] == 0
            assert len(client.get("/documents", params={"status": "done", "limit": 1}).json()["documents"]) == 1
            assert client.get("/documents", params={"offset": 5}).json()["documents"] == []
            error_of(client.get("/documents", params={"status": "bogus"}), 400, "invalid_status")
            error_of(client.get("/documents", params={"limit": "many"}), 422, "validation_error")
            assert [j["job_id"] for j in client.get(f"/documents/{d1['doc_id']}/jobs").json()["jobs"]] == [d1["job_id"]]
            assert len(client.get("/jobs", params={"state": "done"}).json()["jobs"]) == 2
            error_of(client.get("/jobs", params={"state": "bogus"}), 400, "invalid_state")
        # a new pipeline fingerprint on the same DATA_DIR: the result is stale, reprocessed only on request
        other = ("1.0.0", "fp-other")
        with client_for(make_service(data_dir, version=other)) as client:
            r = upload(client, first)
            assert r.status_code == 200, r.text
            assert r.json()["stale"] and r.json()["duplicate"] and r.json()["job_id"] is None and not r.json()["queued"]
            assert client.get(f"/documents/{d1['doc_id']}").json()["stale"] is True
            r = upload(client, first, reprocess=True)
            assert r.status_code == 202 and r.json()["queued"] and r.json()["stale"], r.text
            job = wait_job(client, r.json()["job_id"])
            assert job["state"] == "done" and job["kind"] == "reprocess" and job["reprocess"]
            doc = client.get(f"/documents/{d1['doc_id']}").json()
            assert doc["stale"] is False and doc["fingerprint"] == "fp-other"
            # POST /documents/{id}/reprocess: nothing to do when current, unless forced (query or JSON body)
            r = client.post(f"/documents/{d1['doc_id']}/reprocess")
            assert r.status_code == 200 and r.json()["job_id"] is None and not r.json()["queued"]
            r = client.post(f"/documents/{d1['doc_id']}/reprocess", params={"force": "true"})
            assert r.status_code == 202 and r.json()["queued"]
            assert wait_job(client, r.json()["job_id"])["state"] == "done"
            r = client.post(f"/documents/{d1['doc_id']}/reprocess", json={"force": True})
            assert r.status_code == 202 and r.json()["queued"]
            assert wait_job(client, r.json()["job_id"])["state"] == "done"
            no_inputs(error_of(client.post(f"/documents/{d1['doc_id']}/reprocess", json={"force": "yes"}), 422,
                               "validation_error"))
            error_of(client.post(f"/documents/{d1['doc_id']}/reprocess", json={"other": 1}), 422, "validation_error")
            error_of(client.post(f"/documents/{'0' * 64}/reprocess"), 404, "document_not_found")
            assert len(client.get(f"/documents/{d1['doc_id']}/jobs").json()["jobs"]) == 4


# ---------------------------------------------------------------- 3. upload rejections


def multipart(boundary: str, data: bytes, name: str = "big.pdf") -> bytes:
    return (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()


def test_upload_rejections() -> None:
    with app_env(max_upload_mb=1) as (client, service):
        error_of(upload(client, b""), 400, "empty")
        error_of(upload(client, b"hello, not a pdf at all"), 415, "not_pdf")
        error_of(upload(client, b"x" * 5000), 415, "not_pdf")
        error_of(upload(client, b"%PDF-1.4\nnot really a pdf\n"), 400, "invalid_pdf")
        # Content-Length above MAX_UPLOAD_MB (+ multipart allowance): refused before the body is read
        big = b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024)
        body = error_of(upload(client, big), 413, "too_large")
        assert "request body" in body["message"]
        # within the allowance but the PDF itself is larger than 1 MiB: the service's own check
        body = error_of(upload(client, b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 1000)), 413, "too_large")
        assert "PDF" in body["message"]
        # no Content-Length (chunked): the bytes received are counted
        boundary = "test-boundary-7d3f"
        payload = multipart(boundary, big)

        def chunks():
            for i in range(0, len(payload), 256 * 1024):
                yield payload[i:i + 256 * 1024]

        r = client.post("/documents", content=chunks(),
                        headers={"content-type": f"multipart/form-data; boundary={boundary}"})
        assert "content-length" not in {k.lower() for k in r.request.headers}
        assert error_of(r, 413, "too_large")["message"] == api._too_large(service)
        # a hand-built multipart body under the limit is accepted (the parser path is the same)
        ok = make_pdf("Hand built multipart")
        r = client.post("/documents", content=multipart(boundary, ok, "hand.pdf"),
                        headers={"content-type": f"multipart/form-data; boundary={boundary}"})
        assert r.status_code == 202 and r.json()["doc_id"] == sha(ok), r.text
        # missing or malformed fields: 422 without the submitted values
        for resp in (client.post("/documents", data={"reprocess": "true"}),
                     client.post("/documents", data={"file": "not a file"}),
                     client.post("/documents", files={"file": ("a.pdf", ok, "application/pdf")},
                                 data={"reprocess": "maybe"}),
                     client.post("/documents", content=b"%PDF-1.4 raw", headers={"content-type": "application/pdf"})):
            body = error_of(resp, 422, "validation_error")
            no_inputs(body)
            assert "maybe" not in resp.text and "not a file" not in resp.text
        # a password in the URL is refused before anything is read
        r = client.post("/documents?password=pw-in-url-123", files={"file": ("a.pdf", ok, "application/pdf")})
        error_of(r, 400, "password_in_query")
        assert "pw-in-url-123" not in r.text
        docs = client.get("/documents").json()["documents"]
        assert [d["doc_id"] for d in docs] == [sha(ok)]  # nothing else registered
        work = service.config.work_dir
        assert all(p.name in {d["current_job_id"] for d in docs} for p in work.iterdir())  # no scratch left


# ---------------------------------------------------------------- 4. files


TRAVERSALS = ("../registry.sqlite", "%2e%2e/registry.sqlite", "..%2fregistry.sqlite", "%2e%2e%2f%2e%2e%2fregistry.sqlite",
              "chunks/..%2f..%2f..%2fregistry.sqlite", "images%2f..%2f..%2f..%2fregistry.sqlite",
              "..%5c..%5cregistry.sqlite", "source.pdf", "publish.json", "logs/error.log", "/etc/passwd",
              "%2fetc%2fpasswd", "C:%2fWindows%2fwin.ini", "C:%5cWindows%5cwin.ini", "images/%2e%2e",
              "images%2f..%2fsource.pdf", "protocol.md%00.png", "PROTOCOL.MD", "chunks/embeddings.npy")


def test_files() -> None:
    gate = Gate()
    with app_env(pipeline=make_pipeline(gate=gate)) as (client, service):
        try:
            d = upload(client, make_pdf("File study")).json()
            assert gate.started.wait(10)
            error_of(client.get(f"/documents/{d['doc_id']}/files"), 409, "not_ready")
            error_of(client.get(f"/documents/{d['doc_id']}/files/md"), 409, "not_ready")
        finally:
            gate.release.set()
        wait_job(client, d["job_id"])
        doc = d["doc_id"]
        listing = client.get(f"/documents/{doc}/files").json()
        assert listing["files"] == ["chunks/chunks.jsonl", "protocol.decisions.json", "protocol.html", "protocol.json",
                                    "protocol.md"], listing["files"]
        assert listing["images"] == ["images/image_000.png"]
        assert listing["aliases"]["chunks.jsonl"] == "chunks/chunks.jsonl" and listing["aliases"]["md"] == "protocol.md"
        expect = {
            "md": ("text/markdown; charset=utf-8", RESULT_FILES["protocol.md"]),
            "protocol.md": ("text/markdown; charset=utf-8", RESULT_FILES["protocol.md"]),
            "chunks.jsonl": ("application/x-ndjson", RESULT_FILES["chunks/chunks.jsonl"]),
            "chunks/chunks.jsonl": ("application/x-ndjson", RESULT_FILES["chunks/chunks.jsonl"]),
            "decisions": ("application/json", RESULT_FILES["protocol.decisions.json"]),
            "json": ("application/json", RESULT_FILES["protocol.json"]),
            "images/image_000.png": ("image/png", RESULT_FILES["images/image_000.png"]),
        }
        folder = service.storage.document_dir(doc)
        for name, (media, content) in expect.items():
            r = client.get(f"/documents/{doc}/files/{name}")
            assert r.status_code == 200, (name, r.status_code)
            assert r.headers["content-type"] == media, (name, r.headers["content-type"])
            assert r.headers["x-content-type-options"] == "nosniff" and r.content == content, name
            assert "content-security-policy" not in r.headers, name
        r = client.get(f"/documents/{doc}/files/html")
        assert r.status_code == 200 and r.headers["content-type"] == "text/html; charset=utf-8"
        assert r.headers["content-security-policy"] == api.HTML_CSP and r.headers["x-content-type-options"] == "nosniff"
        assert "inline" in r.headers["content-disposition"] and "protocol.html" in r.headers["content-disposition"]
        error_of(client.get(f"/documents/{doc}/files/quality.json"), 404, "file_not_found")  # exposed, not written
        error_of(client.get(f"/documents/{doc}/files/nothing.txt"), 404, "file_not_found")
        secrets_on_disk = [service.config.registry_path.read_bytes()[:16], (folder / "source.pdf").read_bytes()[:8]]
        assert secrets_on_disk[0].startswith(b"SQLite format 3")
        names = list(TRAVERSALS) + [quote(str(service.config.registry_path), safe=""),
                                    quote(str(folder / "source.pdf"), safe="")]
        refused_by_service = 0
        for name in names:
            r = client.get(f"/documents/{doc}/files/{name}", follow_redirects=False)
            assert r.status_code in (400, 404), (name, r.status_code)
            assert r.json()["type"] in ("file_not_found", "not_found"), name
            assert not any(s in r.content for s in secrets_on_disk) and b"%PDF" not in r.content, name
            refused_by_service += r.json()["type"] == "file_not_found"
        assert refused_by_service >= len(names) - 1, refused_by_service  # "../x" is normalised by the client
        error_of(client.get(f"/documents/{'f' * 64}/files"), 404, "document_not_found")
        error_of(client.get(f"/documents/{'f' * 64}/files/md"), 404, "document_not_found")
        error_of(client.get("/documents/..%2f..%2fregistry.sqlite/files/md"), 404, "not_found")  # no route
        error_of(client.get("/documents/..%5c..%5cregistry.sqlite/files/md"), 400, "invalid_id")


# ---------------------------------------------------------------- 5. errors, cancel, delete


def test_errors_cancel_delete() -> None:
    gate = Gate()
    with app_env(pipeline=make_pipeline(gate=gate)) as (client, service):
        missing = "a" * 64
        error_of(client.get(f"/documents/{missing}"), 404, "document_not_found")
        error_of(client.delete(f"/documents/{missing}"), 404, "document_not_found")
        for path in ("/documents/xyz", "/documents/XYZ/files", "/documents/xyz/jobs", f"/documents/{missing.upper()}"):
            error_of(client.get(path), 400, "invalid_id")
        error_of(client.post("/documents/xyz/tools/outline", json={}), 400, "invalid_id")
        error_of(client.get(f"/jobs/{'b' * 32}"), 404, "job_not_found")
        error_of(client.post(f"/jobs/{'b' * 32}/cancel"), 404, "job_not_found")
        error_of(client.get("/jobs/not-a-job"), 400, "invalid_id")
        error_of(client.get("/no/such/route"), 404, "not_found")
        r = client.put("/documents")
        error_of(r, 405, "method_not_allowed")
        assert "POST" in r.headers.get("allow", "")
        error_of(client.post(f"/documents/{missing}/tools/outline", content=b"{not json",
                             headers={"content-type": "application/json"}), 422, "validation_error")
        try:
            running = upload(client, make_pdf("Busy document")).json()
            assert gate.started.wait(10)
            queued = upload(client, make_pdf("Queued document")).json()
            error_of(client.delete(f"/documents/{running['doc_id']}"), 409, "document_busy")
            error_of(client.post(f"/jobs/{running['job_id']}/cancel"), 409, "not_cancellable")
            r = client.post(f"/jobs/{queued['job_id']}/cancel")
            assert r.status_code == 200 and r.json()["state"] == "cancelled" and r.json()["job_id"] == queued["job_id"]
            assert client.get(f"/documents/{queued['doc_id']}").json()["status"] == "cancelled"
            error_of(client.post(f"/jobs/{queued['job_id']}/cancel"), 409, "not_cancellable")
        finally:
            gate.release.set()
        assert wait_job(client, running["job_id"])["state"] == "done"
        assert client.get(f"/jobs/{queued['job_id']}").json()["state"] == "cancelled"  # never started
        error_of(client.post(f"/jobs/{running['job_id']}/cancel"), 409, "not_cancellable")
        r = client.delete(f"/documents/{running['doc_id']}")
        assert r.status_code == 200 and r.json() == {"doc_id": running["doc_id"], "deleted": True}
        error_of(client.get(f"/documents/{running['doc_id']}"), 404, "document_not_found")
        error_of(client.get(f"/documents/{running['doc_id']}/files/md"), 404, "document_not_found")
        error_of(client.get(f"/jobs/{running['job_id']}"), 404, "job_not_found")
        assert client.get(f"/documents/{running['doc_id']}/jobs").json()["jobs"] == []
        assert service.storage.document_dir(running["doc_id"]) is None
        assert client.delete(f"/documents/{queued['doc_id']}").status_code == 200
        assert service.storage.list_workdirs() == [] and service.storage.list_documents() == []
        assert client.get("/documents").json()["documents"] == []


# ---------------------------------------------------------------- 6. passwords


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if record.exc_info:
            text += "".join(traceback.format_exception(*record.exc_info))
        self.lines.append(f"{record.name} {text} {record.args!r}")


def test_password_never_stored_logged_or_echoed() -> None:
    password = "pw-" + secrets.token_hex(12)
    calls: list[dict] = []
    root, capture = logging.getLogger(), Capture()
    old_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    responses: list[bytes] = []
    try:
        with temp_dir() as tmp:
            gate = Gate()
            service = make_service(tmp / "data", pipeline=make_pipeline(calls=calls, gate=gate))
            with client_for(service) as client:
                data = make_pdf("Password study")
                r = upload(client, data, password=password)
                responses.append(r.content)
                assert r.status_code == 202, r.text
                assert gate.started.wait(10)
                # while it runs: a second document with the password is queued, then cancelled
                queued = upload(client, make_pdf("Password study, cancelled"), password=password)
                assert queued.json()["queued"] and len(service.registry.pdf_passwords) == 1
                cancelled = client.post(f"/jobs/{queued.json()['job_id']}/cancel")
                assert cancelled.json()["state"] == "cancelled" and len(service.registry.pdf_passwords) == 0
                responses += [queued.content, cancelled.content]
                gate.release.set()
                assert wait_job(client, r.json()["job_id"])["state"] == "done"
                assert calls and calls[-1]["password"] == password  # the pipeline received it
                for resp in (upload(client, data, password=password),  # duplicate
                             client.post("/documents", data={"password": password}),  # 422: no file
                             client.post("/documents", files={"file": ("x.pdf", b"", "application/pdf")},
                                         data={"password": password}),  # 400 empty
                             client.get(f"/documents/{r.json()['doc_id']}"), client.get(f"/jobs/{r.json()['job_id']}"),
                             client.get("/jobs"), client.get("/documents"),
                             client.post(f"/documents/{r.json()['doc_id']}/tools/quote_check",
                                         json={"quote": password, "bogus": password})):
                    responses.append(resp.content)
                assert len(service.registry.pdf_passwords) == 0
            # after shutdown: the registry is closed, nothing moves any more
            files = [p for p in service.config.data_dir.rglob("*") if p.is_file()]
            assert files and not any(password.encode() in p.read_bytes() for p in files)
        for body in responses:
            assert password.encode() not in body
        assert capture.lines and not any(password in line for line in capture.lines)
    finally:
        root.removeHandler(capture)
        root.setLevel(old_level)
    # uvicorn's access log prints the request line: the filter masks a password the client put in the URL
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:5000", "POST", f"/documents?reprocess=1&password={password}", "1.1", 400),
                               None)
    assert api.RedactPasswordFilter().filter(record) and password not in record.getMessage()
    assert "reprocess=1&password=***" in record.getMessage()


# ---------------------------------------------------------------- 7. unexpected exceptions


def test_unexpected_exception_is_opaque() -> None:
    root, capture = logging.getLogger(), Capture()
    root.addHandler(capture)
    try:
        with app_env() as (client, service):
            def broken(doc_id):
                raise ValueError("SECRET TEXT quoted from a document")

            service.get_document = broken
            r = client.get(f"/documents/{'c' * 64}")
            assert r.status_code == 500 and r.json() == {"type": "internal_error",
                                                         "message": "internal error (ValueError)"}, r.text
            assert "SECRET" not in r.text
            assert client.get("/health").status_code == 200  # the app keeps serving
        logged = [line for line in capture.lines if line.startswith("service.api ")]
        assert logged and "ValueError" in logged[0] and "tests/test_service_api.py:" in logged[0], logged
        assert not any("SECRET" in line for line in capture.lines)
    finally:
        root.removeHandler(capture)


def test_ui_mount() -> None:
    """The demo UI: static files from service/ui at /ui (index for /ui/), the UI's CSP on every file, JSON 404s,
    / redirecting to /ui/, the Host check applying as everywhere."""
    with temp_dir() as tmp:
        ui = tmp / "ui"
        (ui / "modules").mkdir(parents=True)
        (ui / "index.html").write_text("<!doctype html><title>t</title><script type=module src=app.js></script>",
                                       encoding="utf-8")
        (ui / "modules" / "app.js").write_text("export const x = 1;\n", encoding="utf-8")
        old = api.UI_DIR
        api.UI_DIR = ui
        try:
            service = make_service(tmp / "data")
            with TestClient(api.create_app(service), base_url=LOCAL) as client:
                r = client.get("/", follow_redirects=False)
                assert r.status_code in (302, 307) and r.headers["location"] == "/ui/", r.status_code
                r = client.get("/ui/")
                assert r.status_code == 200 and r.headers["content-type"].startswith("text/html"), r.status_code
                assert r.headers["content-security-policy"] == api.UI_CSP and "'unsafe-inline'" not in api.UI_CSP
                assert r.headers["x-content-type-options"] == "nosniff"
                r = client.get("/ui/modules/app.js")
                assert r.status_code == 200 and "javascript" in r.headers["content-type"], r.headers["content-type"]
                assert r.headers["content-security-policy"] == api.UI_CSP
                error_of(client.get("/ui/missing.js"), 404, "not_found")
                error_of(client.get("/ui/../registry.sqlite"), 404, "not_found")
                error_of(client.get("/ui/", headers={"host": "evil.example"}), 421, "invalid_host")
                assert "/ui" not in client.get("/openapi.json").json()["paths"]
        finally:
            api.UI_DIR = old


def test_host_and_origin_checks() -> None:
    """Only ALLOWED_HOSTS may be addressed, and browser pages of other hosts are refused (DNS rebinding,
    cross-site form posts); clients without an Origin header and pages of an allowed host pass."""
    with app_env() as (client, service):
        assert service.config.allowed_hosts == ("localhost", "127.0.0.1", "::1")
        for host in ("127.0.0.1:8000", "localhost", "LOCALHOST:1", "[::1]:8000"):
            assert client.get("/health", headers={"host": host}).status_code == 200, host
        for host in ("evil.example", "evil.example:8000", "127.0.0.1.evil.example", "localhost.evil.example:80"):
            error_of(client.get("/health", headers={"host": host}), 421, "invalid_host")
        for origin in ("http://127.0.0.1:8000", "http://localhost:5173", "https://[::1]"):
            assert client.get("/health", headers={"origin": origin}).status_code == 200, origin
        for origin in ("http://evil.example", "https://127.0.0.1.evil.example", "null", "file://", "ftp://localhost"):
            error_of(client.get("/health", headers={"origin": origin}), 403, "invalid_origin")
        # a cross-site form post never reaches the upload route
        r = client.post("/documents", headers={"origin": "http://evil.example"},
                        files={"file": ("a.pdf", make_pdf("Made-up protocol text"), "application/pdf")})
        error_of(r, 403, "invalid_origin")
        assert service.list_documents()["documents"] == []
    open_config = ServiceConfig(data_dir=Path("unused"), allowed_hosts=("*",))
    assert open_config.host_allowed("anything.example") and open_config.origin_allowed("http://evil.example")
    assert ServiceConfig(data_dir=Path("unused")).host_allowed(None) is False


# ---------------------------------------------------------------- 8. protocol tools on a synthetic document


def fake_vector(text: str):
    import numpy as np

    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def fake_rerank(query: str, chunks: list[dict], model: str) -> list[tuple[int, float]]:
    return [(i, 1.0 / (i + 1)) for i in range(len(chunks))]


def synthetic_protocol():
    """A small made-up protocol: title page, numbered sections, criteria lists, a captioned table, an inline
    abbreviation."""
    from docling_core.types.doc import (BoundingBox, ContentLayer, CoordOrigin, DocItemLabel, DoclingDocument,
                                        ProvenanceItem, Size, TableCell, TableData)

    def pv(page):
        return ProvenanceItem(page_no=page, bbox=BoundingBox(l=72, t=700, r=540, b=690,
                                                             coord_origin=CoordOrigin.BOTTOMLEFT), charspan=(0, 1))

    d = DoclingDocument(name="quillmore")
    for p in range(1, 7):
        d.add_page(page_no=p, size=Size(width=612, height=792))
        d.add_text(DocItemLabel.PAGE_HEADER, "Protocol QM-2024-07 Quillmore Study", prov=pv(p),
                   content_layer=ContentLayer.FURNITURE)
    d.add_title("A Randomized Study of Quillmorex in Adults with Fictional Fatigue Syndrome", prov=pv(1))
    d.add_text(DocItemLabel.TEXT, "Sponsor protocol number: QM-2024-07", prov=pv(1))
    d.add_text(DocItemLabel.TEXT, "Protocol version 2.0, 3 March 2024", prov=pv(1))
    d.add_heading("1 Introduction", level=1, prov=pv(2))
    d.add_text(DocItemLabel.TEXT, "Quillmorex is an investigational tablet studied in fictional fatigue syndrome. "
               "Every adverse event (AE) is recorded from the first dose until the end of follow up.", prov=pv(2))
    d.add_heading("2 Objectives", level=1, prov=pv(2))
    d.add_text(DocItemLabel.TEXT, "The primary objective is to compare the change in fatigue score after twelve "
               "weeks between quillmorex and placebo.", prov=pv(2))
    d.add_heading("3 Study Design", level=1, prov=pv(3))
    d.add_text(DocItemLabel.TEXT, "This is a double blind placebo controlled parallel group study with two arms.",
               prov=pv(3))
    rows = [["Arm", "Treatment", "Dose"], ["A", "Quillmorex", "40 mg daily"], ["B", "Placebo", "matching tablet"]]
    cells = [TableCell(text=x, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                       end_col_offset_idx=c + 1, column_header=r == 0)
             for r, row in enumerate(rows) for c, x in enumerate(row)]
    tab = d.add_table(data=TableData(num_rows=3, num_cols=3, table_cells=cells), prov=pv(3))
    cap = d.add_text(DocItemLabel.CAPTION, "Table 1. Study treatments", prov=pv(3), parent=tab)
    tab.captions.append(cap.get_ref())
    d.add_heading("5 Study Population", level=1, prov=pv(4))
    d.add_text(DocItemLabel.TEXT, "Adults with fictional fatigue syndrome are enrolled at ten made up sites.",
               prov=pv(4))
    d.add_heading("5.1 Inclusion Criteria", level=2, prov=pv(4))
    group = d.add_list_group()
    for i, text in enumerate(["Aged 18 to 65 years at screening.",
                              "Diagnosed with fictional fatigue syndrome for at least six months.",
                              "Able to swallow tablets whole."], 1):
        d.add_list_item(text, marker=f"{i}.", parent=group, prov=pv(4))
    d.add_heading("5.2 Exclusion Criteria", level=2, prov=pv(5))
    group = d.add_list_group()
    for i, text in enumerate(["Any serious AE during a previous quillmorex study.", "Pregnant or breastfeeding."], 1):
        d.add_list_item(text, marker=f"{i}.", parent=group, prov=pv(5))
    d.add_heading("6 Statistics", level=1, prov=pv(6))
    d.add_text(DocItemLabel.TEXT, "A sample size of 120 participants gives 80 percent power.", prov=pv(6))
    return d


def build_converted_folder(folder: Path) -> None:
    """protocol.json + chunks/ (chunk_protocol) + fake embeddings in embed_chunks.py's format + an image."""
    import numpy as np

    from chunk_protocol import chunk_document

    folder.mkdir(parents=True)
    synthetic_protocol().save_as_json(folder / "protocol.json")
    logging.getLogger("chunk_protocol").setLevel(logging.WARNING)
    chunk_document(folder / "protocol.json", folder / "chunks", quiet_text=True)
    chunks = [json.loads(line) for line in (folder / "chunks" / "chunks.jsonl").read_text(encoding="utf-8")
              .splitlines() if line.strip()]
    np.save(folder / "chunks" / "embeddings.npy", np.asarray([fake_vector(c["embed_text"]) for c in chunks]))
    (folder / "chunks" / "embeddings_meta.json").write_text(json.dumps({
        "model": "qwen/qwen3-embedding-8b", "dimensions": DIM, "normalized": True,
        "query_instruction": "Given a question about a clinical trial protocol, retrieve the protocol passages",
        "chunk_ids": [c["chunk_id"] for c in chunks]}, indent=1), encoding="utf-8")
    (folder / "protocol.md").write_text("# Synthetic protocol\n", encoding="utf-8")
    (folder / "protocol_images").mkdir()
    (folder / "protocol_images" / "image_000000.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\1" * 8)


def tokenizer_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache(TOKENIZER, "tokenizer.json"), str)
    except Exception:  # noqa: BLE001 - no hub client: treat as not cached
        return False


TOOL_CALLS = {
    "document_info": {}, "outline": {"max_level": 1}, "find_sections": {"role": "exclusion"},
    "read_section": {"section": "inclusion"}, "get_table": {"table": "#/tables/0", "format": "markdown"},
    "get_soa": {}, "abbreviation": {"term": "AE"}, "quote_check": {"quote": "Able to swallow tablets whole."},
    "search": {"query": "What dose of quillmorex is given?", "k": 4},
}


def test_delete_purges_caches() -> None:
    """DELETE ?purge_caches=true also removes the PDF's conversion batches and its figure descriptions from the
    shared caches; other documents' entries stay; without the flag the caches are left alone."""
    if not tokenizer_cached():
        print(f"   (skipped: tokenizer {TOKENIZER} not in the local Hugging Face cache)")
        return
    from docling_core.types.doc import DoclingDocument

    from figure_vlm import document_key
    from service.import_existing import import_folder

    with temp_dir() as tmp:
        folder, pdf = tmp / "src" / "Quillmore", tmp / "src" / "quillmore.pdf"
        build_converted_folder(folder)
        pdf.write_bytes(make_pdf("Quillmore study protocol", "Made-up text for a purge test"))
        service = make_service(tmp / "data")
        doc = import_folder(service.config, folder, pdf=pdf, version=VERSION, registry=service.registry,
                            storage=service.storage)["doc_id"]
        key = document_key(DoclingDocument.load_from_json(folder / "protocol.json"))
        vlm, batches = service.config.vlm_cache_dir, service.config.batch_cache_dir
        for name, doc_key in (("mine", key), ("other", "0123456789abcdef")):
            (vlm / f"{name}.json").write_text(json.dumps({"doc_key": doc_key, "description": "made up"}),
                                              encoding="utf-8")
        for owner in (doc[:16], "f" * 16):
            run = batches / owner / "layout-cfg"
            run.mkdir(parents=True)
            for f in ("0001-0015.json", "0016-0030.json", "manifest.json"):
                (run / f).write_text("{}", encoding="utf-8")
        (vlm / "broken.json").write_text("{not json", encoding="utf-8")
        with client_for(service) as client:
            r = client.delete(f"/documents/{doc}", params={"purge_caches": "true"})
            assert r.status_code == 200 and r.json() == {"doc_id": doc, "deleted": True,
                                                         "purged": {"batches": 2, "figures": 1}}, r.text
            assert not (vlm / "mine.json").exists() and (vlm / "other.json").exists() and (vlm / "broken.json").exists()
            assert not (batches / doc[:16]).exists() and (batches / ("f" * 16)).is_dir()
            error_of(client.get(f"/documents/{doc}"), 404, "document_not_found")
            # a document without a Docling JSON (fake pipeline): its batches go, no figure key, nothing else
            other = upload(client, make_pdf("Purge study without figures")).json()
            wait_job(client, other["job_id"])
            (batches / other["doc_id"][:16]).mkdir()
            (batches / other["doc_id"][:16] / "0001-0015.json").write_text("{}", encoding="utf-8")
            r = client.delete(f"/documents/{other['doc_id']}?purge_caches=true")
            assert r.json()["purged"] == {"batches": 1, "figures": 0}, r.text
            assert (vlm / "other.json").exists()
            # without the flag: nothing purged, no "purged" key
            third = upload(client, make_pdf("Kept caches study")).json()
            wait_job(client, third["job_id"])
            (batches / third["doc_id"][:16]).mkdir()
            assert client.delete(f"/documents/{third['doc_id']}").json() == {"doc_id": third["doc_id"], "deleted": True}
            assert (batches / third["doc_id"][:16]).is_dir()


def test_protocol_tools() -> None:
    if not tokenizer_cached():
        print(f"   (skipped: tokenizer {TOKENIZER} not in the local Hugging Face cache)")
        return
    from service.import_existing import import_folder

    assert set(TOOL_CALLS) == {t["name"] for t in TOOLS}
    with temp_dir() as tmp:
        folder, pdf = tmp / "src" / "Quillmore", tmp / "src" / "quillmore.pdf"
        build_converted_folder(folder)
        pdf.write_bytes(make_pdf("Quillmore study protocol", "Made-up text for a test"))
        service = make_service(tmp / "data", embed_fn=fake_vector, rerank_fn=fake_rerank, run_worker=False)
        out = import_folder(service.config, folder, pdf=pdf, version=VERSION, registry=service.registry,
                            storage=service.storage)
        doc = out["doc_id"]
        with client_for(service) as client:
            info = client.get(f"/documents/{doc}").json()
            assert info["has_result"] and info["stale"] is False and info["status"] == "done"
            listing = client.get(f"/documents/{doc}/files").json()
            assert "chunks/sections.json" in listing["files"] and listing["images"] == ["protocol_images/image_000000.png"]
            r = client.get(f"/documents/{doc}/files/protocol_images/image_000000.png")
            assert r.status_code == 200 and r.headers["content-type"] == "image/png"
            assert client.get(f"/documents/{doc}/files/sections.json").headers["content-type"] == "application/json"
            # Over HTTP the typed route of a tool name shadows the generic route (registered first, same path), so
            # the generic handler is called directly here, with the same service, to compare the two.
            generic_route = next(r for r in client.app.routes if getattr(r, "operation_id", None) == "call_tool")
            for name, args in TOOL_CALLS.items():
                typed = client.post(f"/documents/{doc}/tools/{name}", json=args)
                assert typed.status_code == 200, (name, typed.status_code, typed.json().get("type"))
                assert "error" not in typed.json(), name
                assert generic_call(generic_route, service, doc, name, args) == (200, typed.content), name
            search = client.post(f"/documents/{doc}/tools/search", json=TOOL_CALLS["search"]).json()
            assert search["blocks"] and len(search["hits"]) == 4 and search["blocks"][0]["provenance"]
            assert client.post(f"/documents/{doc}/tools/abbreviation", json={"term": "AE"}).json()["found"]
            assert client.post(f"/documents/{doc}/tools/quote_check", json=TOOL_CALLS["quote_check"]).json()["found"]
            # no body / empty body: the tool's defaults, identical on both routes
            for name in ("document_info", "outline", "get_soa"):
                for kwargs in ({}, {"json": {}}):
                    empty = client.post(f"/documents/{doc}/tools/{name}", **kwargs)
                    assert empty.status_code == 200, (name, empty.status_code)
                    assert generic_call(generic_route, service, doc, name, None) == (200, empty.content), name
            # invalid arguments: 400 invalid_arguments from the tool's JSON schema (generic), 422 from the typed model
            for name, args in (("quote_check", {"quote": 5}), ("quote_check", {"quote": "x", "bogus": 1}),
                               ("find_sections", {"role": "no_such_role"}), ("read_section", {"max_tokens": 10}),
                               ("search", {"query": "dose", "k": "4"}), ("get_table", {}),
                               ("outline", {"with_roles": "yes"})):
                status, body = generic_call(generic_route, service, doc, name, args)
                assert status == 400 and body["type"] == "invalid_arguments" and body["details"] == {"tool": name}, \
                    (name, status)
                no_inputs(error_of(client.post(f"/documents/{doc}/tools/{name}", json=args), 422, "validation_error"))
            error_of(client.post(f"/documents/{doc}/tools/search"), 422, "validation_error")  # body required
            error_of(client.post(f"/documents/{doc}/tools/outline", json=[1, 2]), 422, "validation_error")
            # over HTTP the generic route serves the names without a typed route
            body = error_of(client.post(f"/documents/{doc}/tools/no_such_tool", json={}), 404, "unknown_tool")
            assert body["details"]["tools"] == [t["name"] for t in TOOLS]
            error_of(client.post(f"/documents/{doc}/tools/no_such_tool"), 404, "unknown_tool")
            error_of(client.post(f"/documents/{doc}/tools/no_such_tool", json=[1]), 422, "validation_error")
            # lookups that find nothing: 404 not_found on both routes
            error_of(client.post(f"/documents/{doc}/tools/read_section", json={"section": "S99"}), 404, "not_found")
            status, body = generic_call(generic_route, service, doc, "read_section", {"section": "S99"})
            assert status == 404 and body["type"] == "not_found"
            error_of(client.post(f"/documents/{doc}/tools/get_table", json={"table": "#/tables/99"}), 404, "not_found")
            error_of(client.post(f"/documents/{'d' * 64}/tools/outline", json={}), 404, "document_not_found")
            # a document without a result yet (no worker here: the upload stays queued)
            queued = upload(client, make_pdf("Queued forever")).json()
            assert queued["queued"]
            error_of(client.post(f"/documents/{queued['doc_id']}/tools/document_info"), 409, "not_ready")
            error_of(client.get(f"/documents/{queued['doc_id']}/files"), 409, "not_ready")
            # paths under DATA_DIR never appear in tool results (JSON escapes backslashes); document_info's
            # "out_dir" (the folder's path in protocol_api) is redacted by the service
            forms = {str(service.config.data_dir), service.config.data_dir.as_posix(),
                     json.dumps(str(service.config.data_dir))[1:-1]}
            for name, args in TOOL_CALLS.items():
                result = client.post(f"/documents/{doc}/tools/{name}", json=args).json()
                assert not any(f in json.dumps(result) for f in forms), name
            info = client.post(f"/documents/{doc}/tools/document_info").json()
            assert info.get("out_dir") == "<document>", "document_info out_dir is redacted"


def generic_call(route, service, doc: str, name: str, args) -> tuple[int, bytes | dict]:
    """The generic tool route's handler called directly: (200, body bytes) or (error status, error JSON)."""
    from service.app_service import ServiceError

    try:
        response = route.endpoint(doc_id=doc, tool_name=name, service=service, args=args)
    except ServiceError as exc:
        return exc.status, exc.to_json()
    return response.status_code, bytes(response.body)


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    start_all = time.time()
    for t in tests:
        start = time.time()
        try:
            t()
            print(f"ok {t.__name__} ({time.time() - start:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed} passed in {time.time() - start_all:.1f}s" if not failed
          else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
