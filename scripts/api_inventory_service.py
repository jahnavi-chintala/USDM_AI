"""Inventory of the service side of docs/api_reference.md: every field the REST routes, the MCP results and the
published files really return (sections 3 REST API, 4 MCP and 6 Published files).

    python scripts/api_inventory_service.py [--json]      field names only, never values

inventory() -> {surface: {path}} in the notation of scripts/api_inventory_common.py. Surfaces:
  rest:<METHOD> <path template of /openapi.json>   the JSON body of a route (every status that answers one)
  rest-error                                       the JSON error body (REST, and the same body in MCP results)
  mcp:tools/list, mcp:envelope                     the tools/list result; a tools/call result without the body
  mcp:list_documents, mcp:get_document, mcp:ingest_document   their structuredContent
  file:<published name>                            one record (.jsonl) or the object (.json) of a published file;
                                                   file:protocol.decisions.json: its top-level keys only
The protocol tool route (POST /documents/{doc_id}/tools/{tool}) is not a surface here: its body is the tool's
result (tool:<name>, scripts/api_inventory_tools.py). It is checked to equal the MCP structuredContent of the same
call and the ProtocolDocument result (except document_info's out_dir, which the service redacts).

How the data is made: in process, offline (OPENROUTER_OFFLINE=1, HF_HUB_OFFLINE=1, no network, no port), on a
temporary DATA_DIR that is removed at the end:
  - the app of service.api.create_app over a ProtocolService with the real pipeline version and fingerprint, driven
    by Starlette's TestClient (REST and the MCP endpoint /mcp, JSON-RPC); the worker runs one pipeline function
    that dispatches by the PDF's SHA-256:
      * the public protocols LZZT_CDISC_Pilot and Lilly_NCT03421379: service.pipeline.process_document for real,
        except that the Docling conversion (minutes per document) is replaced by a copy of the converted files in
        output_public/<name> (protocol.json / .md / .html / .decisions.json, images, tables); chunking, embedding
        (offline: cached vectors only), quality report and identifiers run as in the service;
      * made-up one-page PDFs (generated here, no real study): a fake pipeline that finishes, waits (a running job),
        or fails inside the real pipeline (a conversion that produced nothing);
  - LZZT is first imported from output_public with service.import_existing (an "imported", stale result), then
    reprocessed; Lilly is uploaded as a new PDF;
  - for the file surfaces, also the chunk files and decisions log of every public output folder as they are (the
    files import_existing publishes unchanged).
Maps whose keys are data (DYNAMIC) give '*' path segments. A public output that is missing is skipped silently.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import api_inventory_common as common  # noqa: E402

PUBLIC = ("LZZT_CDISC_Pilot", "Lilly_NCT03421379")   # run through the pipeline (conversion copied)
PUBLIC_ALL = ("Alexion_NCT04573309", "Bayer_NCT04304508", "Lilly_NCT03421379", "LZZT_CDISC_Pilot",
              "PrECOG_NCT01797120", "Sanofi_NCT03637764")
CHUNK_FILES = ("chunks.jsonl", "parents.jsonl", "tables.jsonl", "sections.json", "glossary.json", "stats.json")
LOCAL = "http://127.0.0.1"
FINAL = ("done", "done_with_warnings", "failed", "cancelled")
PASSWORD = "made-up-inventory-password"
UNCACHED_QUERY = "zebra inventory probe 4711 unrelated words"

JOB_DYN = ("stages[].detail",)
DOC_DYN = ("quality.stages",)
DYNAMIC: dict[str, tuple[str, ...]] = {
    "rest:GET /health": ("jobs",),
    "rest:GET /version": ("packages", "fingerprint_inputs.settings", "fingerprint_inputs.packages",
                          "fingerprint_inputs.converter"),
    "rest:GET /documents": tuple(f"documents[].{p}" for p in DOC_DYN),
    "rest:GET /documents/{doc_id}": DOC_DYN + tuple(f"current_job.{p}" for p in JOB_DYN),
    "rest:GET /documents/{doc_id}/jobs": tuple(f"jobs[].{p}" for p in JOB_DYN),
    "rest:GET /jobs": tuple(f"jobs[].{p}" for p in JOB_DYN),
    "rest:GET /jobs/{job_id}": JOB_DYN,
    "rest:POST /jobs/{job_id}/cancel": JOB_DYN,
    "rest:GET /documents/{doc_id}/files": ("aliases",),
    "rest:GET /tools": ("tools[].input_schema.properties", "tools[].function.parameters.properties"),
    "mcp:tools/list": ("tools[].inputSchema.properties",),
    "mcp:list_documents": tuple(f"documents[].{p}" for p in DOC_DYN),
    "mcp:get_document": DOC_DYN + tuple(f"current_job.{p}" for p in JOB_DYN),
    "file:quality.json": ("tables.lattice_raw", "tables.lattice_final", "summary.stages", "stages[].detail"),
    "file:chunks/stats.json": ("token_histogram", "chunk_types", "chunks_containing_label", "parents.kinds",
                               "parents.skipped_items", "document.identifiers.sources",
                               "document.identifiers.others"),
    "file:chunks/sections.json": ("ref_section", "ref_map"),
    "file:chunks/glossary.json": ("entries",),
}

Sink = Callable[..., None]   # sink(surface, body, note="")


# ============================================================================ made-up PDFs


def _pdf_string(text: str) -> bytes:
    raw = text.encode("latin-1")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def made_up_pdf(*lines: str, extra: bytes = b"") -> bytes:
    """One 612 x 792 page of Helvetica text; `extra` (a comment) changes the bytes, not the text."""
    content = b"\n".join(b"BT /F1 12 Tf 72 %d Td %s Tj ET" % (720 - 16 * i, _pdf_string(line))
                         for i, line in enumerate(lines))
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
            b"/Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n" + extra)
    offsets = []
    for n, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _load_script(name: str):
    """A helper module of scripts/ by path (no static import: it stays out of other tests' imports)."""
    key = f"_api_inventory_{name}"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module   # dataclasses look their module up while the class is created
        spec.loader.exec_module(module)
    return sys.modules[key]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ============================================================================ the worker's pipeline function


@contextmanager
def _conversion(folder: Path | None):
    """convert_protocol.convert_document replaced by a copy of the converted files of `folder` (None: a
    conversion that produced nothing). process_document imports it at call time, so the patch applies."""
    import convert_protocol

    def convert_document(src, out, *, stem=None, images_dir=None, progress=None, **_):
        stem = stem or "protocol"
        out = Path(out)
        if progress:
            progress("convert", 0, 1)
        counts = {"pages": 1, "tables": 0, "pictures": 0}
        if folder is not None:
            for suffix in (".json", ".md", ".html", ".decisions.json"):
                if (folder / f"protocol{suffix}").is_file():
                    shutil.copy2(folder / f"protocol{suffix}", out / f"{stem}{suffix}")
            for name in ("protocol_images", "tables"):
                if (folder / name).is_dir():
                    shutil.copytree(folder / name, out / name, dirs_exist_ok=True)
            doc = json.loads((out / f"{stem}.json").read_text(encoding="utf-8"))
            counts = {"pages": len(doc.get("pages") or {}), "tables": len(doc.get("tables") or []),
                      "pictures": len(doc.get("pictures") or [])}
        if progress:
            progress("convert", 1, 1)
            for stage in ("postprocess", "figures", "save"):
                progress(stage)
        return {"stem": stem, "json": out / f"{stem}.json", **counts, "postprocess": {}, "figures": {}}

    original = convert_protocol.convert_document
    convert_protocol.convert_document = convert_document
    try:
        yield
    finally:
        convert_protocol.convert_document = original


class Pipelines:
    """The worker's pipeline_fn: dispatch by the PDF's SHA-256 (see the module docstring)."""

    def __init__(self) -> None:
        self.public: dict[str, Path] = {}         # sha -> output_public folder: the real pipeline
        self.kind: dict[str, str] = {}            # sha -> "gate" | "fail" | "related" (default: done)
        self.version = ("", "")
        self.gate_started = threading.Event()
        self.gate_release = threading.Event()
        self.lock = threading.Lock()

    def __call__(self, pdf_path, workdir, config, *, settings=None, progress=None, password=None):
        from service.contracts import STAGES, PipelineResult, StageResult

        pdf_path, workdir = Path(pdf_path), Path(workdir)
        digest = _sha(pdf_path.read_bytes())
        if digest in self.public or self.kind.get(digest) == "fail":
            from service.pipeline import process_document

            with self.lock, _conversion(self.public.get(digest)):
                return process_document(pdf_path, workdir, config, settings=settings, progress=progress,
                                        password=password)
        kind = self.kind.get(digest, "done")
        if kind == "gate":
            if progress:
                progress("convert", 2, 7)
            self.gate_started.set()
            if not self.gate_release.wait(120):
                raise TimeoutError("gate never released")
        for i, stage in enumerate(STAGES, 1):
            if progress:
                progress(stage, i, len(STAGES))
        (workdir / "protocol.md").write_text("# Made-up result\n", encoding="utf-8")
        (workdir / "protocol.json").write_text("{}", encoding="utf-8")
        ids = {"nct": "NCT99999999", "sponsor_protocol_id": None} if kind == "related" else {}
        return PipelineResult(status="done", pipeline_version=self.version[0], fingerprint=self.version[1],
                              sha256=digest, pages=1, identifiers=ids, quality={}, warnings=[],
                              stages=[StageResult(name=s, status="done") for s in STAGES])


# ============================================================================ the scenario


class Scenario:
    """Drives the app and reports every JSON body to sink(surface, body, note)."""

    def __init__(self, sink: Sink) -> None:
        self.sink = sink
        self.errors: dict[str, set[int]] = {}
        self.parity: dict[str, bool] = {}
        self.client = None
        self.mcp_id = 0

    # ---------------------------------------------------------------- REST helpers
    def call(self, method: str, url: str, surface: str | None, expect: int, note: str = "", **kw) -> Any:
        r = self.client.request(method, url, **kw)
        if r.status_code != expect:
            kind = r.json().get("type") if r.headers.get("content-type", "").startswith("application/json") else ""
            raise AssertionError(f"{surface or url.split('?')[0]}: HTTP {r.status_code} {kind}, expected {expect}")
        body = r.json()
        if surface:
            self.sink(surface, body, note or f"HTTP {expect}")
        return body

    def error(self, method: str, url: str, status: int, type_: str, note: str = "", **kw) -> dict:
        r = self.client.request(method, url, **kw)
        body = r.json()
        if r.status_code != status or body.get("type") != type_:
            raise AssertionError(f"expected {status} {type_}, got {r.status_code} {body.get('type')} ({note})")
        self.errors.setdefault(type_, set()).add(status)
        self.sink("rest-error", body, f"HTTP {status} {note}".strip())
        return body

    def upload(self, data: bytes, name: str, expect: int, note: str = "", **form) -> dict:
        fields = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in form.items()}
        return self.call("POST", "/documents", "rest:POST /documents", expect, note,
                         files={"file": (name, data, "application/pdf")}, data=fields)

    def job(self, job_id: str, note: str = "") -> dict:
        return self.call("GET", f"/jobs/{job_id}", "rest:GET /jobs/{job_id}", 200, note)

    def document(self, doc_id: str, note: str = "") -> dict:
        return self.call("GET", f"/documents/{doc_id}", "rest:GET /documents/{doc_id}", 200, note)

    def wait(self, job_id: str, doc_id: str | None = None, timeout: float = 240.0) -> dict:
        """Poll the job until it is final; while it runs, also sample the document once per stage."""
        deadline, stages = time.monotonic() + timeout, set()
        while True:
            job = self.job(job_id, "polled")
            if job["state"] in FINAL:
                return job
            if job["state"] == "running" and doc_id and job["stage"] not in stages:
                stages.add(job["stage"])
                self.document(doc_id, "while its job runs")
            if time.monotonic() > deadline:
                raise AssertionError(f"job still {job['state']} after {timeout:.0f}s")
            time.sleep(0.05)

    # ---------------------------------------------------------------- MCP helpers
    def rpc(self, method: str, params: dict | None = None) -> dict:
        from mcp_types.version import LATEST_HANDSHAKE_VERSION

        self.mcp_id += 1
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": self.mcp_id, "method": method,
                                           "params": params or {}},
                             headers={"Accept": "application/json, text/event-stream",
                                      "Content-Type": "application/json",
                                      "MCP-Protocol-Version": LATEST_HANDSHAKE_VERSION})
        if r.status_code != 200 or "result" not in r.json():
            raise AssertionError(f"MCP {method}: HTTP {r.status_code}")
        return r.json()["result"]

    def tool(self, name: str, args: dict, surface: str | None = None, error: str | None = None,
             note: str = "") -> dict:
        result = self.rpc("tools/call", {"name": name, "arguments": args})
        body = result.get("structuredContent")
        text = json.loads(result["content"][0]["text"]) if result.get("content") else None
        if text != body:
            raise AssertionError(f"MCP {name}: the text block differs from structuredContent")
        self.sink("mcp:envelope", {**result, "structuredContent": {}}, f"{name} {'error' if error else 'ok'}")
        if error is not None:
            if not result.get("isError") or body.get("type") != error:
                raise AssertionError(f"MCP {name}: expected error {error}, got {body.get('type')}")
            self.errors.setdefault(error, set()).add(0)   # 0: MCP only (no HTTP status)
            self.sink("rest-error", body, f"MCP {name} {note}".strip())
        else:
            if result.get("isError"):
                raise AssertionError(f"MCP {name}: unexpected error {body.get('type')}")
            if surface:
                self.sink(surface, body, note)
        return body


def run(sink: Sink) -> dict:
    """Run the whole scenario; returns a summary (error types -> statuses, parity checks), values-free."""
    from fastapi.testclient import TestClient

    from service import api
    from service.app_service import ProtocolService
    from service.config import ServiceConfig
    from service.import_existing import import_folder
    from service.registry import Registry, inspect_pdf_in_process

    s = Scenario(sink)
    tmp = Path(tempfile.mkdtemp(prefix="api_inv_svc_"))
    root_logger = logging.getLogger()
    quiet = logging.NullHandler()
    root_logger.addHandler(quiet)   # no stderr output from the lastResort handler (job failures are expected)
    service = None
    try:
        config = ServiceConfig(data_dir=tmp / "data", offline=True, max_upload_mb=1)
        pipelines = Pipelines()
        service = ProtocolService(config, pipeline_fn=pipelines,
                                  registry=Registry(config, inspector=inspect_pdf_in_process),
                                  worker_options={"poll_interval": 0.05})
        pipelines.version = (service.pipeline_version, service.fingerprint)
        public = {name: (ROOT / "output_public" / name, ROOT / "public_protocols" / name / "protocol.pdf")
                  for name in PUBLIC}
        public = {n: (f, p) for n, (f, p) in public.items() if (f / "protocol.json").is_file() and p.is_file()}
        for folder, pdf in public.values():
            pipelines.public[_sha(pdf.read_bytes())] = folder

        lzzt = public.get("LZZT_CDISC_Pilot")
        lzzt_id = None
        if lzzt:  # an imported result: pipeline_version "imported", always stale
            lzzt_id = import_folder(config, lzzt[0], pdf=lzzt[1], registry=service.registry,
                                    storage=service.storage)["doc_id"]

        with TestClient(api.create_app(service), base_url=LOCAL) as client:
            s.client = client
            _service_routes(s)
            if lzzt_id:
                _public_lzzt(s, lzzt_id, lzzt[1])
            lilly_id = _public_lilly(s, public["Lilly_NCT03421379"][1]) if "Lilly_NCT03421379" in public else None
            made_up = _made_up_documents(s, pipelines, lzzt_id)
            _files(s, [d for d in (lzzt_id, lilly_id) if d])
            _tool_route(s, service, lzzt_id, lilly_id, made_up["done"])
            _mcp(s, service, lzzt_id or made_up["done"], protocol_tools=bool(lzzt_id),
                 more=[d for d in (lilly_id, made_up["failed"], made_up["related"]) if d])
            _deletes(s, made_up, lilly_id)
            _errors(s, lzzt_id or made_up["encrypted"])
            service.worker.stop(timeout=10)   # a stopped worker: /health answers 503
            s.call("GET", "/health", "rest:GET /health", 503, "worker stopped")
        # An app whose lifespan never ran (no `with`): no service exists, every route answers 503 not_started.
        # Never entered as a context: its lifespan would build a service on the environment's DATA_DIR.
        s.client = TestClient(api.create_app(), base_url=LOCAL)
        s.error("GET", "/health", 503, "not_started", "before startup")
    finally:
        if service is not None:
            service.stop()
        root_logger.removeHandler(quiet)
        gc.collect()
        for _ in range(5):
            shutil.rmtree(tmp, ignore_errors=True)
            if not tmp.exists():
                break
            time.sleep(0.5)
    _public_folders(sink)
    return {"errors": {k: sorted(v) for k, v in sorted(s.errors.items())}, "parity": s.parity}


# ---------------------------------------------------------------- service routes


def _service_routes(s: Scenario) -> None:
    s.call("GET", "/health", "rest:GET /health", 200)
    s.call("GET", "/version", "rest:GET /version", 200)
    spec = s.client.get("/openapi.json").json()
    for path in ("/health", "/version", "/documents", "/documents/{doc_id}", "/documents/{doc_id}/reprocess",
                 "/documents/{doc_id}/jobs", "/jobs", "/jobs/{job_id}", "/jobs/{job_id}/cancel",
                 "/documents/{doc_id}/files", "/documents/{doc_id}/files/{name}", "/tools",
                 "/documents/{doc_id}/tools/{tool_name}"):
        if path not in spec["paths"]:
            raise AssertionError(f"{path} is not in /openapi.json")
    s.call("GET", "/tools", "rest:GET /tools", 200, "style=anthropic")
    s.call("GET", "/tools?style=openai", "rest:GET /tools", 200, "style=openai")
    s.call("GET", "/documents", "rest:GET /documents", 200, "imported document only")


# ---------------------------------------------------------------- public protocols through the pipeline


def _public_lzzt(s: Scenario, doc_id: str, pdf: Path) -> None:
    s.document(doc_id, "imported, stale")
    s.tool("get_document", {"doc_id": doc_id}, "mcp:get_document", note="imported")
    s.tool("list_documents", {}, "mcp:list_documents", note="imported")
    s.call("GET", f"/documents/{doc_id}/files", "rest:GET /documents/{doc_id}/files", 200, "imported")
    _read_files(s, doc_id)   # the imported (output_public) files as served
    data = pdf.read_bytes()
    s.upload(data, "protocol.pdf", 200, "stale result: no job")
    d = s.call("POST", f"/documents/{doc_id}/reprocess", "rest:POST /documents/{doc_id}/reprocess", 202,
               "stale: queued")
    s.wait(d["job_id"], doc_id)
    s.job(d["job_id"], "reprocess finished")
    s.document(doc_id, "reprocessed, current")
    s.call("POST", f"/documents/{doc_id}/reprocess", "rest:POST /documents/{doc_id}/reprocess", 200,
           "current: no job")
    s.upload(data, "protocol.pdf", 200, "current result: duplicate, no job")
    s.call("GET", f"/documents/{doc_id}/jobs", "rest:GET /documents/{doc_id}/jobs", 200)


def _public_lilly(s: Scenario, pdf: Path) -> str:
    d = s.upload(pdf.read_bytes(), "protocol.pdf", 202, "new PDF: queued")
    s.call("GET", "/documents?status=queued", "rest:GET /documents", 200, "status filter")
    s.wait(d["job_id"], d["doc_id"])
    s.job(d["job_id"], "pipeline finished offline (no embeddings)")
    s.document(d["doc_id"], "done_with_warnings")
    return d["doc_id"]


# ---------------------------------------------------------------- made-up documents: job states, rules


def _made_up_documents(s: Scenario, pipelines: Pipelines, lzzt_id: str | None) -> dict:
    e2e = _load_script("e2e_lib")
    pdfs = {
        "gate": made_up_pdf("Made-up inventory study: a job held running"),
        "queued": made_up_pdf("Made-up inventory study: a job that waits in the queue"),
        "fail": made_up_pdf("Made-up inventory study: a conversion that produced nothing"),
        "related_a": made_up_pdf("Made-up inventory study A with a made-up registry number"),
        "related_b": made_up_pdf("Made-up inventory study B with the same made-up registry number"),
        "same_a": made_up_pdf("Made-up inventory study: two files with the same text"),
        "same_b": made_up_pdf("Made-up inventory study: two files with the same text", extra=b"% other bytes\n"),
        "encrypted": e2e.make_pdf([e2e.Page(lines=[(72, 720, 12, "Made-up encrypted inventory study")])],
                                  user_password=PASSWORD),
    }
    sha = {k: _sha(v) for k, v in pdfs.items()}
    pipelines.kind.update({sha["gate"]: "gate", sha["fail"]: "fail", sha["related_a"]: "related",
                           sha["related_b"]: "related"})
    out: dict[str, Any] = {}
    gate = s.upload(pdfs["gate"], "gate.pdf", 202, "new PDF: queued")
    if not pipelines.gate_started.wait(60):
        raise AssertionError("the gate job never started")
    try:
        s.job(gate["job_id"], "running")
        s.document(gate["doc_id"], "running, no result yet")
        s.upload(pdfs["gate"], "gate-again.pdf", 200, "same bytes while running: that job")
        s.error("DELETE", f"/documents/{gate['doc_id']}", 409, "document_busy", "delete while its job runs")
        s.error("POST", f"/jobs/{gate['job_id']}/cancel", 409, "not_cancellable", "cancel a running job")
        s.error("GET", f"/documents/{gate['doc_id']}/files", 409, "not_ready", "files before a result")
        s.error("GET", f"/documents/{gate['doc_id']}/files/md", 409, "not_ready", "a file before a result")
        s.error("POST", f"/documents/{gate['doc_id']}/tools/outline", 409, "not_ready", "a tool before a result",
                json={})
        queued = s.upload(pdfs["queued"], "queued.pdf", 202, "new PDF: queued behind the running job")
        s.upload(pdfs["queued"], "queued.pdf", 200, "same bytes while queued: that job")
        s.job(queued["job_id"], "queued")
        s.call("GET", "/jobs?state=queued", "rest:GET /jobs", 200, "state filter")
        if lzzt_id:
            forced = s.call("POST", f"/documents/{lzzt_id}/reprocess?force=true",
                            "rest:POST /documents/{doc_id}/reprocess", 202, "force=true on a current result")
            s.call("POST", f"/documents/{lzzt_id}/reprocess", "rest:POST /documents/{doc_id}/reprocess", 200,
                   "JSON force while a job is queued: that job", json={"force": True})
            s.call("POST", f"/jobs/{forced['job_id']}/cancel", "rest:POST /jobs/{job_id}/cancel", 200,
                   "a queued reprocess cancelled")
            s.document(lzzt_id, "reprocess cancelled: status cancelled, the older result still served")
        s.call("POST", f"/jobs/{queued['job_id']}/cancel", "rest:POST /jobs/{job_id}/cancel", 200,
               "a queued job cancelled")
        s.error("POST", f"/jobs/{queued['job_id']}/cancel", 409, "not_cancellable", "cancel twice")
        s.document(queued["doc_id"], "cancelled, no result")
        fail = s.upload(pdfs["fail"], "fail.pdf", 202, "new PDF: queued")
        rel_a = s.upload(pdfs["related_a"], "related-a.pdf", 202, "new PDF: queued")
        rel_b = s.upload(pdfs["related_b"], "related-b.pdf", 202, "new PDF: queued")
        same_a = s.upload(pdfs["same_a"], "same-a.pdf", 202, "new PDF: queued")
        same_b = s.upload(pdfs["same_b"], "same-b.pdf", 202, "same text as another document: warning")
        s.error("POST", "/documents", 400, "password_required", "encrypted PDF without password",
                files={"file": ("locked.pdf", pdfs["encrypted"], "application/pdf")})
        s.error("POST", "/documents", 400, "bad_password", "encrypted PDF, wrong password",
                files={"file": ("locked.pdf", pdfs["encrypted"], "application/pdf")}, data={"password": "wrong"})
        enc = s.upload(pdfs["encrypted"], "locked.pdf", 202, "encrypted PDF with its password", password=PASSWORD)
        s.call("GET", "/documents", "rest:GET /documents", 200, "documents in every state")
    finally:
        pipelines.gate_release.set()
    for d in (gate, fail, rel_a, rel_b, same_a, same_b, enc):
        s.wait(d["job_id"])
    s.job(fail["job_id"], "failed")
    s.document(fail["doc_id"], "failed")
    s.call("GET", f"/documents/{fail['doc_id']}/jobs", "rest:GET /documents/{doc_id}/jobs", 200, "a failed job")
    s.document(rel_b["doc_id"], "related document")
    s.document(same_b["doc_id"], "same text as another document")
    again = s.upload(pdfs["queued"], "queued.pdf", 202, "a cancelled document uploaded again: a new job")
    s.wait(again["job_id"])
    s.upload(pdfs["gate"], "gate.pdf", 200, "current result: duplicate, no job")
    s.call("POST", f"/documents/{gate['doc_id']}/reprocess", "rest:POST /documents/{doc_id}/reprocess", 200,
           "current: no job")
    s.call("GET", "/jobs", "rest:GET /jobs", 200)
    s.call("GET", "/jobs?state=failed&limit=5", "rest:GET /jobs", 200, "state filter")
    s.call("GET", f"/documents/{gate['doc_id']}/jobs?state=done", "rest:GET /documents/{doc_id}/jobs", 200)
    s.call("GET", "/documents?status=done&limit=2&offset=1", "rest:GET /documents", 200, "paging")
    s.call("GET", "/documents", "rest:GET /documents", 200, "every document")
    out.update(done=gate["doc_id"], failed=fail["doc_id"], related=rel_b["doc_id"], same=same_b["doc_id"],
               encrypted=enc["doc_id"], queued=queued["doc_id"])
    return out


# ---------------------------------------------------------------- files


def _read_files(s: Scenario, doc_id: str) -> None:
    """Every published JSON file of a document, through the files route (canonical names and aliases)."""
    listing = s.call("GET", f"/documents/{doc_id}/files", "rest:GET /documents/{doc_id}/files", 200)
    names = set(listing["files"])
    for name, alias in (("chunks/chunks.jsonl", "chunks.jsonl"), ("chunks/parents.jsonl", "parents.jsonl"),
                        ("chunks/tables.jsonl", "tables.jsonl"), ("chunks/sections.json", "sections.json"),
                        ("chunks/glossary.json", "glossary.json"), ("chunks/stats.json", "stats.json"),
                        ("quality.json", "quality"), ("protocol.decisions.json", "decisions")):
        if name not in names:
            continue
        r = s.client.get(f"/documents/{doc_id}/files/{alias}")
        canonical = s.client.get(f"/documents/{doc_id}/files/{name}")
        if r.status_code != 200 or canonical.content != r.content:
            raise AssertionError(f"files/{alias}: HTTP {r.status_code}, alias and canonical name differ")
        _sink_file(s.sink, name, r.text, f"files route, {doc_id[:8]}")
    for name in ("protocol.md", "protocol.json", "protocol.html"):
        if name in names and s.client.get(f"/documents/{doc_id}/files/{name}").status_code != 200:
            raise AssertionError(f"files/{name} not served")
    for image in listing["images"][:1]:
        r = s.client.get(f"/documents/{doc_id}/files/{image}")
        if r.status_code != 200 or not r.headers["content-type"].startswith("image/"):
            raise AssertionError("an image of the listing is not served")


def _sink_file(sink: Sink, name: str, text: str, note: str) -> None:
    surface = f"file:{name}"
    if name.endswith(".jsonl"):
        for line in text.splitlines():
            if line.strip():
                sink(surface, json.loads(line), note)
    elif name == "protocol.decisions.json":
        sink(surface, {k: None for k in json.loads(text)}, note)   # top-level keys only (internal log)
    else:
        sink(surface, json.loads(text), note)


def _files(s: Scenario, doc_ids: list[str]) -> None:
    for doc_id in doc_ids:
        _read_files(s, doc_id)


def _public_folders(sink: Sink) -> None:
    """The chunk files and decisions log of every public output folder, as import_existing publishes them."""
    for name in PUBLIC_ALL:
        folder = ROOT / "output_public" / name
        for f in CHUNK_FILES:
            path = folder / "chunks" / f
            if path.is_file():
                _sink_file(sink, f"chunks/{f}", path.read_text(encoding="utf-8"), f"output_public {name}")
        path = folder / "protocol.decisions.json"
        if path.is_file():
            _sink_file(sink, "protocol.decisions.json", path.read_text(encoding="utf-8"), f"output_public {name}")


# ---------------------------------------------------------------- the protocol tool route


def _tool_route(s: Scenario, service, lzzt_id: str | None, lilly_id: str | None, fake_id: str) -> None:
    from protocol_api import ProtocolDocument

    if lzzt_id:
        folder = service.storage.document_dir(lzzt_id)
        raw = ProtocolDocument.open(folder)
        for name, args in (("document_info", {}), ("outline", {"max_level": 1}),
                           ("read_section", {"section": "inclusion"})):
            rest = s.call("POST", f"/documents/{lzzt_id}/tools/{name}", None, 200, json=args)
            mcp = s.tool(name, {"doc_id": lzzt_id, **args})
            direct = json.loads(json.dumps(raw.call_tool(name, args)))
            differing = sorted(k for k in set(rest) | set(direct) if rest.get(k) != direct.get(k))
            s.parity[f"{name}: REST == MCP structuredContent"] = rest == mcp
            s.parity[f"{name}: REST == ProtocolDocument.call_tool except {differing}"] = set(differing) <= {"out_dir"}
            if rest != mcp or not set(differing) <= {"out_dir"}:
                raise AssertionError(f"tool route {name}: body differs ({differing})")
        s.error("POST", f"/documents/{lzzt_id}/tools/no_such_tool", 404, "unknown_tool", "unknown tool name",
                json={})
        s.error("POST", f"/documents/{lzzt_id}/tools/read_section", 404, "not_found", "no such section",
                json={"section": "S999"})
        s.error("POST", f"/documents/{lzzt_id}/tools/read_section", 400, "bad_request",
                "read_section without section or cursor", json={})
        s.error("POST", f"/documents/{lzzt_id}/tools/read_section", 422, "validation_error",
                "argument of the wrong type", json={"max_tokens": "many"})
        s.error("POST", f"/documents/{lzzt_id}/tools/search", 503, "offline", "query embedding not cached",
                json={"query": UNCACHED_QUERY})
    if lilly_id:
        s.error("POST", f"/documents/{lilly_id}/tools/search", 409, "search_unavailable",
                "document without embeddings", json={"query": UNCACHED_QUERY})
    s.error("POST", f"/documents/{fake_id}/tools/outline", 500, "document_unreadable",
            "a result protocol_api cannot open", json={})


# ---------------------------------------------------------------- MCP


def _mcp(s: Scenario, service, doc_id: str, protocol_tools: bool, more: list[str]) -> None:
    listing = s.rpc("tools/list")
    s.sink("mcp:tools/list", listing, "tools/list")
    rest_list = s.client.get("/documents").json()
    s.parity["list_documents == GET /documents"] = s.tool("list_documents", {}, "mcp:list_documents") == rest_list
    s.tool("list_documents", {"status": "done", "limit": 2, "offset": 0}, "mcp:list_documents", note="filtered")
    for d in [doc_id, *more]:   # current, done_with_warnings, failed, related
        doc = s.tool("get_document", {"doc_id": d}, "mcp:get_document")
        same = doc == s.client.get(f"/documents/{d}").json()
        s.parity["get_document == GET /documents/{doc_id}"] = s.parity.get("get_document == GET /documents/{doc_id}",
                                                                            True) and same
    data = made_up_pdf("Made-up inventory study sent as base64 over MCP")
    first = s.tool("ingest_document", {"data_base64": base64.b64encode(data).decode(), "filename": "mcp.pdf"},
                   "mcp:ingest_document", note="queued")
    s.wait(first["job_id"])
    again = s.tool("ingest_document", {"data_base64": "data:application/pdf;base64," + base64.b64encode(data).decode()},
                   "mcp:ingest_document", note="duplicate")
    rest_again = s.upload(data, "mcp.pdf", 200, "the same bytes over REST")
    s.parity["ingest_document == POST /documents"] = again == rest_again
    inbox = made_up_pdf("Made-up inventory study placed in the inbox")
    (service.config.inbox_dir / "inventory.pdf").write_bytes(inbox)
    queued = s.tool("ingest_document", {"path": "inbox/inventory.pdf"}, "mcp:ingest_document", note="inbox")
    s.wait(queued["job_id"])
    s.tool("get_document", {"doc_id": queued["doc_id"]}, "mcp:get_document", note="done")
    if protocol_tools:
        s.tool("outline", {"doc_id": doc_id, "max_level": 1})
        s.tool("read_section", {"doc_id": doc_id, "section": "S999"}, error="not_found")
    s.tool("get_document", {"doc_id": "f" * 64}, error="document_not_found")
    s.tool("get_document", {"doc_id": "xyz"}, error="invalid_id")
    s.tool("get_document", {}, error="invalid_arguments", note="schema")
    s.tool("no_such_tool", {}, error="unknown_tool")
    s.tool("ingest_document", {}, error="invalid_arguments", note="neither path nor data_base64")
    s.tool("ingest_document", {"path": "../registry.sqlite"}, error="invalid_name")
    s.tool("ingest_document", {"path": "missing.pdf"}, error="file_not_found")
    s.tool("ingest_document", {"data_base64": "@@@ not base64 @@@"}, error="invalid_base64")
    s.tool("ingest_document", {"data_base64": ""}, error="empty")


# ---------------------------------------------------------------- deletes


def _deletes(s: Scenario, made_up: dict, lilly_id: str | None) -> None:
    s.call("DELETE", f"/documents/{made_up['done']}", "rest:DELETE /documents/{doc_id}", 200)
    s.error("GET", f"/documents/{made_up['done']}", 404, "document_not_found", "after delete")
    target = lilly_id or made_up["related"]
    s.call("DELETE", f"/documents/{target}?purge_caches=true", "rest:DELETE /documents/{doc_id}", 200,
           "purge_caches=true")


# ---------------------------------------------------------------- errors


def _errors(s: Scenario, doc_id: str) -> None:
    s.error("GET", "/documents?status=bogus", 400, "invalid_status")
    s.error("GET", "/jobs?state=bogus", 400, "invalid_state")
    s.error("GET", "/tools?style=xml", 400, "invalid_style")
    s.error("GET", "/documents/xyz", 400, "invalid_id", "doc_id")
    s.error("GET", "/jobs/xyz", 400, "invalid_id", "job_id")
    s.error("GET", f"/documents/{'f' * 64}", 404, "document_not_found")
    s.error("POST", f"/documents/{'f' * 64}/reprocess", 404, "document_not_found", "reprocess")
    s.error("GET", f"/jobs/{'b' * 32}", 404, "job_not_found")
    s.error("GET", f"/documents/{doc_id}/files/nothing.txt", 404, "file_not_found")
    s.error("GET", "/no/such/route", 404, "not_found", "unknown route")
    s.error("PUT", "/documents", 405, "method_not_allowed")
    ok = made_up_pdf("Made-up inventory study for upload errors")
    pdf_file = {"file": ("a.pdf", ok, "application/pdf")}
    s.error("POST", "/documents?password=x", 400, "password_in_query", files=pdf_file)
    s.error("POST", "/documents", 400, "empty", files={"file": ("a.pdf", b"", "application/pdf")})
    s.error("POST", "/documents", 415, "not_pdf", files={"file": ("a.pdf", b"plain text, no PDF header",
                                                                   "application/pdf")})
    s.error("POST", "/documents", 400, "invalid_pdf", files={"file": ("a.pdf", b"%PDF-1.4\nbroken\n",
                                                                       "application/pdf")})
    s.error("POST", "/documents", 413, "too_large", "Content-Length above the limit",
            files={"file": ("a.pdf", b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024), "application/pdf")})
    s.error("POST", "/documents", 413, "too_large", "the PDF above MAX_UPLOAD_MB",
            files={"file": ("a.pdf", b"%PDF-1.4\n" + b"0" * (1024 * 1024 + 1000), "application/pdf")})
    s.error("POST", "/documents", 422, "validation_error", "no file", data={"reprocess": "true"})
    s.error("POST", "/documents", 400, "bad_request", "a multipart body that does not parse (no boundary)",
            content=b"%PDF-1.4", headers={"content-type": "multipart/form-data"})
    s.error("POST", f"/documents/{doc_id}/reprocess", 422, "validation_error", "JSON body", json={"force": "yes"})
    s.error("GET", "/health", 421, "invalid_host", headers={"host": "evil.example"})
    s.error("GET", "/health", 403, "invalid_origin", headers={"origin": "http://evil.example"})


# ============================================================================ inventory


def inventory() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def sink(surface: str, body: Any, note: str = "") -> None:
        common.merge(found, surface, common.paths(body, DYNAMIC.get(surface, ())))

    run(sink)
    return found


if __name__ == "__main__":
    common.print_inventory(inventory(), "--json" in sys.argv)
