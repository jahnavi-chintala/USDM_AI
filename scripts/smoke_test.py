"""End-to-end smoke test of the protocol service: REST, MCP over Streamable HTTP and MCP over stdio, run against a
service that is already running (by default the Docker container at http://127.0.0.1:8000).

    python scripts/smoke_test.py [--base-url http://127.0.0.1:8000]
        [--stdio-cmd "docker exec -i protocol-service python -m service.mcp_stdio"]
        [--protocols LZZT_CDISC_Pilot Lilly_NCT03421379] [--timeout-min 120]
        [--allow-openrouter] [--skip-reprocess]

Checks (each a named check; a PASS / FAIL / SKIP table at the end; exit code 1 on any FAIL, 2 when refused):
  1 service     GET /health ok; GET /version: pipeline version, fingerprint, offline flag, API key set. The
                expectations below follow the offline flag: online, the figures and embed stages run and search
                answers; offline (OPENROUTER_OFFLINE=1) they are skipped_offline, a new result is
                done_with_warnings and search answers 409 search_unavailable.
  2 upload      POST /documents for each protocol: 202 with a queued job (or 200 duplicate: the existing result is
                reused; a stale one is re-uploaded with reprocess=true; online, one made while the service was
                offline is reprocessed with force); the job is polled to its end (stage and progress changes
                printed) and its per-stage seconds are shown.
  3 document    GET /documents/{id}: status, stale false, has_result, identifiers (LZZT H2Q-MC-LZZT, Lilly
                NCT03421379); quality.json coverage >= 0.97, tables and chunks > 0; stage statuses.
  4 files       every listed file and one image downloadable; chunks.jsonl parses; every figure image_path is
                relative and served by the files route; no response names a server path (DATA_DIR, /data/, /tmp/).
  5 rest tools  the 9 typed tool routes: document_info, outline, find_sections, read_section ('inclusion', whole
                and paged through 'cursor'), get_soa, get_table (a real SoA table id), search, abbreviation,
                quote_check (a verbatim sentence of read_section: found; a made-up one: not found).
  6 mcp http    <base>/mcp with the MCP SDK client: initialize, 12 tools, list_documents, get_document and every
                call of step 5 with structuredContent equal to the REST JSON (errors: the same error type);
                ingest_document (base64) of a stored PDF is a duplicate without a job.
  7 mcp stdio   --stdio-cmd (split with shlex, POSIX rules: write Windows paths with / or in single quotes):
                initialize, 12 tools, get_document, read_section, quote_check equal to REST; the child exits.
  8 facts       Lilly NCT03421379 and LZZT H2Q-MC-LZZT in document_info; every inclusion criterion of the public
                USDM answer key (usdm_gold) present in read_section('inclusion') (token match, criterion ids
                printed, never texts); get_soa available with visits, at least as many as the key's encounters.
  9 versions    a duplicate re-upload answers 200 without a job; reprocess=true on a current result queues
                nothing; POST /documents/{id}/reprocess?force=true (skipped with --skip-reprocess) queues a job
                that publishes a new result (result_job_id and published_at change) with the same chunk texts and,
                online, sends nothing new to OpenRouter (figures described_now 0, embeddings embedded 0: cached).
 10 summary     the table, job stage timings per document, what was sent to OpenRouter (counts from the jobs).

What is sent where. The PDFs are uploaded to the service only, and only the public protocols of
public_protocols/<name>/protocol.pdf (default LZZT_CDISC_Pilot and Lilly_NCT03421379; --protocols takes folder
names under public_protocols/, never paths; anything else is refused). A service that is ONLINE then sends, with
zero-data-retention routing and no fallbacks: the figure images not yet in its figure cache (FIGURE_MODEL), the
text of every chunk not yet in its embedding cache (EMBED_MODEL), and for the search checks the query
"What is the dose of the study drug?" (EMBED_MODEL) with the candidate chunk texts (RERANK_MODEL). The forced
reprocess finds everything cached and sends nothing new.

Guard: nothing is uploaded or reprocessed unless GET /version reports offline=true, or --allow-openrouter is given
(it prints what will be sent first). Without either the script stops after step 1 with exit code 2.

Output: ids, counts, booleans, timings and short labels only; errors as HTTP status + error type, or exception
type + our file:line; never document text.

Real run against the container (PowerShell, from the project root):
    .venv\\Scripts\\python.exe scripts\\smoke_test.py --allow-openrouter
Against the hosted service behind its proxy (docs/hosting_gcp.md): set PROTOCOL_SERVICE_API_KEY and every REST and
MCP HTTP request carries "Authorization: Bearer <key>" (the key is never printed); --stdio-cmd= (empty) skips stdio:
    $s = Read-Host 'API key' -AsSecureString   # hidden input; then copy it into the environment:
    $env:PROTOCOL_SERVICE_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))
    .venv\\Scripts\\python.exe scripts\\smoke_test.py --base-url https://<host> --stdio-cmd= --allow-openrouter
Offline run against a local service (python -m service.api with OPENROUTER_OFFLINE=1, PORT=8790; the stdio child
inherits this environment, so set DATA_DIR to the service's):
    .venv\\Scripts\\python.exe scripts\\smoke_test.py --base-url http://127.0.0.1:8790 \\
        --stdio-cmd "C:/path/to/.venv/Scripts/python.exe -m service.mcp_stdio"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import traceback
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path, PurePath
from typing import Any, Callable
from urllib.parse import quote as urlquote

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public_protocols"
OUTPUT_PUBLIC = ROOT / "output_public"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import anyio  # noqa: E402
import httpx  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8000"
DEFAULT_STDIO = "docker exec -i protocol-service python -m service.mcp_stdio"
DEFAULT_PROTOCOLS = ("LZZT_CDISC_Pilot", "Lilly_NCT03421379")
KNOWN_IDS = {"LZZT_CDISC_Pilot": ("sponsor_protocol_id", "H2Q-MC-LZZT"),
             "Lilly_NCT03421379": ("nct", "NCT03421379")}
VERSION_DOC = "LZZT_CDISC_Pilot"       # the document of the duplicate / version checks (else the first one)
SEARCH_QUERY = "What is the dose of the study drug?"
MADE_UP = "Purple zebras recite algebra beneath the frozen lighthouse every Thursday."
PROTOCOL_TOOLS = ("document_info", "outline", "find_sections", "read_section", "get_table", "get_soa", "search",
                  "abbreviation", "quote_check")
SERVICE_TOOLS = ("list_documents", "get_document", "ingest_document")
FINAL = ("done", "done_with_warnings", "failed", "cancelled")
RESULT_STATES = ("done", "done_with_warnings")
MIN_COVERAGE = 0.97
CRITERION_MATCH = 0.8      # share of a key criterion's content tokens found in one window of read_section
PAGE_TOKENS = 250          # read_section part size for the cursor check
TOOL_TIMEOUT = 900.0       # seconds: the first tool call opens the document (tokenizer, Docling JSON)
POLL_SECONDS = 2.0
PROGRESS_EVERY = 30.0
EMBED_BATCH = 32           # embed_chunks.embed_texts: chunks per embedding request
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_JOB_ID = re.compile(r"[0-9a-f]{32}")
_QUOTE_UNSAFE = re.compile(r"[&|\[\]\\<>=%±≥≤^_*#`]")


# ============================================================================ plumbing


class Refused(Exception):
    """The guard refuses an upload / reprocess (the service is online and --allow-openrouter was not given)."""


class HttpError(Exception):
    def __init__(self, method: str, path: str, status: int, error_type: str | None) -> None:
        super().__init__(f"{method} {path} -> HTTP {status} {error_type or ''}".strip())
        self.status, self.error_type = status, error_type


def where(exc: BaseException) -> str:
    """Exception type and the innermost frame in this project (file:line), never the message."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    if isinstance(exc, HttpError):
        return str(exc)
    if isinstance(exc, Refused):
        return f"refused by the guard: {exc.args[0] if exc.args else ''}"
    loc = None
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            rel = Path(frame.filename).resolve().relative_to(ROOT)
        except (OSError, ValueError):
            continue
        if rel.parts and rel.parts[0] not in (".venv", "venv"):
            loc = f"{rel.as_posix()}:{frame.lineno}"
    return type(exc).__name__ + (f" at {loc}" if loc else "")


class Report:
    """Named checks, printed as they happen and tabled at the end."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, step: str, name: str, status: str, detail: str = "") -> None:
        self.rows.append((step, name, status, detail))
        print(f"  {status:<4} {name}" + (f"  [{detail}]" if detail else ""), flush=True)

    def check(self, step: str, name: str, ok: bool, detail: str = "") -> bool:
        self.add(step, name, PASS if ok else FAIL, detail)
        return bool(ok)

    def error(self, step: str, name: str, exc: BaseException) -> None:
        self.add(step, name, FAIL, where(exc))

    @property
    def failed(self) -> int:
        return sum(r[2] == FAIL for r in self.rows)


class PathScanner:
    """Looks for absolute server paths in every response body (except /version, which names DATA_DIR)."""

    def __init__(self) -> None:
        self.markers: list[bytes] = [b"/data/", b"/tmp/"]
        self.bodies = 0
        self.hits: list[str] = []

    def add_data_dir(self, data_dir: str | None) -> None:
        if not data_dir:
            return
        if len(PurePath(data_dir.replace("\\", "/")).parts) < 3:  # "/data": the "/data/" marker covers it
            return
        for form in {data_dir, data_dir.replace("\\", "/"), json.dumps(data_dir)[1:-1]}:
            self.markers.append(form.encode("utf-8"))

    def scan(self, label: str, content: bytes) -> None:
        self.bodies += 1
        for marker in self.markers:
            if marker in content:
                self.hits.append(label)
                return


def auth_headers() -> dict[str, str]:
    """The proxy's API key header when PROTOCOL_SERVICE_API_KEY is set (a hosted service), else none."""
    key = os.environ.get("PROTOCOL_SERVICE_API_KEY", "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


class Rest:
    def __init__(self, base: str, scanner: PathScanner) -> None:
        self.http = httpx.Client(base_url=base, timeout=httpx.Timeout(TOOL_TIMEOUT, connect=15.0), trust_env=False,
                                 headers=auth_headers())
        self.scanner = scanner

    def close(self) -> None:
        self.http.close()

    def request(self, method: str, path: str, *, scan: bool = True, **kwargs) -> httpx.Response:
        response = self.http.request(method, path, **kwargs)
        if scan:
            self.scanner.scan(f"{method} {path[:80]}", response.content)
        return response

    def json(self, method: str, path: str, *, ok: tuple[int, ...] = (200,), scan: bool = True,
             **kwargs) -> tuple[int, Any]:
        response = self.request(method, path, scan=scan, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code not in ok:
            raise HttpError(method, path[:80], response.status_code,
                            body.get("type") if isinstance(body, dict) else None)
        return response.status_code, body

    def tool(self, doc_id: str, name: str, args: dict) -> tuple[int, Any]:
        """A typed tool route; never raises on an HTTP error status (the caller checks it)."""
        response = self.request("POST", f"/documents/{doc_id}/tools/{name}", json=args)
        try:
            return response.status_code, response.json()
        except ValueError:
            return response.status_code, None


@dataclass
class Doc:
    name: str
    pdf: Path
    data: bytes
    sha: str
    doc_id: str | None = None
    job_id: str | None = None                      # the processing job of this run (None: existing result)
    job: dict | None = None                        # the job of the served result
    ready: bool = False                            # a published result to test
    summary: dict | None = None                    # quality.json summary
    chunk_texts: str | None = None                 # SHA-256 of the chunk texts (step 4)
    calls: list[tuple[str, str, dict]] = field(default_factory=list)   # (label, tool, args) of step 5
    results: dict[str, tuple[int, Any]] = field(default_factory=dict)  # label -> REST (status, body)
    inclusion_md: str | None = None
    quote: str | None = None
    jobs: list[tuple[str, dict]] = field(default_factory=list)         # (label, finished job) for the summary


class Run:
    def __init__(self, args: argparse.Namespace, docs: list[Doc]) -> None:
        self.args = args
        self.base = args.base_url.rstrip("/")
        self.docs = docs
        self.report = Report()
        self.scanner = PathScanner()
        self.rest = Rest(self.base, self.scanner)
        self.version: dict = {}
        self.offline: bool | None = None
        self.allowed = False
        self.timings: list[tuple[str, float]] = []
        self.searches = 0

    # ---- guard
    def require(self, action: str) -> None:
        if not (self.offline is True or self.allowed):
            raise Refused(action)

    @property
    def figure_model(self) -> str:
        return str((self.version.get("config") or {}).get("figure_model") or "")

    def ready_docs(self) -> list[Doc]:
        return [d for d in self.docs if d.ready]

    def version_doc(self) -> Doc | None:
        ready = self.ready_docs()
        return next((d for d in ready if d.name == VERSION_DOC), ready[0] if ready else None)

    def guarded(self, step: str, label: str, fn: Callable, *args) -> Any:
        """fn(*args); an exception becomes a FAIL row (type and file:line) and None."""
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - every failure is reported, the run goes on
            self.report.error(step, f"{label}: error", exc)
            return None


class NotPublic(ValueError):
    """A --protocols value that is not a folder directly under public_protocols/ with a protocol.pdf."""


def public_protocol(name: str) -> Path:
    """public_protocols/<name>/protocol.pdf of a folder directly under public_protocols/, else NotPublic."""
    def refuse(why: str) -> NotPublic:
        return NotPublic(f"refused --protocols {name!r}: {why} (only folder names directly under "
                         f"public_protocols/ with a protocol.pdf)")

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}", name or "") or ".." in name:
        raise refuse("not a plain folder name")
    public = PUBLIC.resolve()
    folder = (PUBLIC / name).resolve()
    if folder.parent != public or not folder.is_dir():
        raise refuse("no such folder")
    pdf = folder / "protocol.pdf"
    if not pdf.is_file() or pdf.is_symlink() or not pdf.resolve().is_relative_to(public):
        raise refuse("no protocol.pdf in it")
    return pdf


def is_job_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_JOB_ID.fullmatch(value))


def stage_map(job: dict | None) -> dict[str, dict]:
    return {s.get("name"): s for s in (job or {}).get("stages") or []}


def _seconds_between(start: str | None, end: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None


def stage_line(job: dict | None) -> str:
    """Per-stage seconds and statuses; the job's queue wait and run time (the run includes the start of the
    pipeline's child process, which no stage counts)."""
    job = job or {}
    stages = job.get("stages") or []
    total = sum(float(s.get("seconds") or 0) for s in stages)
    queued = _seconds_between(job.get("created_at"), job.get("started_at"))
    ran = _seconds_between(job.get("started_at"), job.get("finished_at"))
    extra = "".join([f", job ran {ran:.0f}s" if ran is not None else "",
                     f" after {queued:.0f}s queued" if queued else ""])
    return " | ".join(f"{s.get('name')} {float(s.get('seconds') or 0):.1f}s {s.get('status')}" for s in stages) \
        + f"  (stages {total:.1f}s{extra})"


def detail_counts(stage: dict | None) -> str:
    detail = (stage or {}).get("detail") or {}
    return ", ".join(f"{k} {v}" for k, v in detail.items() if isinstance(v, (int, float)) and not isinstance(v, bool))


def texts_hash(chunks: list[dict]) -> str:
    return hashlib.sha256(json.dumps([c.get("text") for c in chunks], ensure_ascii=False).encode()).hexdigest()


def parse_jsonl(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def wait_job(run: Run, job_id: str, label: str) -> dict:
    """Poll GET /jobs/{job_id} until it is final; prints state / stage / progress changes (and every 30 s)."""
    deadline = time.monotonic() + run.args.timeout_min * 60
    t0, last, last_t = time.monotonic(), None, 0.0
    while True:
        _, job = run.rest.json("GET", f"/jobs/{job_id}")
        progress = job.get("progress") or {}
        key = (job.get("state"), job.get("stage"))
        now = time.monotonic()
        if key != last or now - last_t >= PROGRESS_EVERY:
            p = f" {progress.get('done')}/{progress.get('total')}" if progress.get("total") else ""
            print(f"    {label} job {job_id[:8]}: {job.get('state')} {job.get('stage') or '-'}{p} "
                  f"[{now - t0:.0f}s]", flush=True)
            last, last_t = key, now
        if job.get("state") in FINAL:
            return job
        if now > deadline:
            raise TimeoutError(f"job {job_id[:8]} not final after {run.args.timeout_min} min")
        time.sleep(POLL_SECONDS)


def upload(run: Run, doc: Doc, *, reprocess: bool = False) -> tuple[int, dict]:
    run.require(f"upload {doc.name}")
    return run.rest.json("POST", "/documents", ok=(200, 202),
                         files={"file": (f"{doc.name}.pdf", doc.data, "application/pdf")},
                         data={"reprocess": "true" if reprocess else "false"})


def job_count(run: Run, doc_id: str) -> int:
    _, body = run.rest.json("GET", f"/documents/{doc_id}/jobs")
    return len(body.get("jobs") or [])


# ============================================================================ step 1: service


STARTUP_WAIT = 180.0  # seconds: a (re)started container takes ~15-30 s; Docker's port proxy accepts and drops before


def wait_for_service(run: Run) -> None:
    """Wait until /health answers 200 (the service may still be starting after `docker compose up -d`): connection
    errors and 503 are retried until STARTUP_WAIT; the last attempt's error is left to step_service."""
    deadline, said = time.monotonic() + STARTUP_WAIT, False
    while time.monotonic() < deadline:
        try:
            if run.rest.request("GET", "/health", scan=False).status_code == 200:
                return
        except httpx.TransportError:
            pass
        if not said:
            print(f"  waiting up to {STARTUP_WAIT:.0f} s for the service to start ...", flush=True)
            said = True
        time.sleep(2.0)


def step_service(run: Run) -> None:
    r = run.report
    wait_for_service(run)
    status, health = run.rest.json("GET", "/health", ok=(200, 503), scan=False)
    r.check("1", "GET /health ok", status == 200 and (health or {}).get("status") == "ok",
            f"HTTP {status}, worker {(health or {}).get('worker')}, offline {(health or {}).get('offline')}")
    _, version = run.rest.json("GET", "/version", scan=False)
    run.version = version or {}
    config = run.version.get("config") or {}
    run.offline = bool(run.version.get("offline"))
    run.scanner.add_data_dir(config.get("data_dir"))
    r.check("1", "GET /version", bool(run.version.get("pipeline_version")) and bool(run.version.get("fingerprint")),
            f"pipeline {run.version.get('pipeline_version')}, fingerprint {run.version.get('fingerprint')}, "
            f"offline {run.offline}, api_key_set {config.get('api_key_set')}")
    print(f"    models: figure {config.get('figure_model')!r}, embed {config.get('embed_model')!r}, "
          f"rerank {config.get('rerank_model')!r}")
    if not run.offline:
        r.check("1", "online service has an OpenRouter API key", bool(config.get("api_key_set")),
                f"api_key_set {config.get('api_key_set')}")
    _, tools = run.rest.json("GET", "/tools")
    names = [t.get("name") for t in (tools or {}).get("tools") or []]
    r.check("1", "GET /tools lists the 9 protocol tools", sorted(names) == sorted(PROTOCOL_TOOLS), f"{len(names)}")


def openrouter_notice(run: Run) -> str:
    config = run.version.get("config") or {}
    lines = [f"The service at {run.base} reports offline=false. Uploading the public protocols "
             f"{', '.join(d.name for d in run.docs)} makes it send to OpenRouter (zero-data-retention routing, "
             "no fallbacks):",
             f"  - every figure image not yet in its figure cache -> {config.get('figure_model') or '(none)'}",
             *([f"  - the first three pages' text and the running headers/footers of every document not yet in its "
                f"identity cache (one request each) -> {config['identity_model']}"]
               if config.get("identity_model") else []),
             f"  - the text of every chunk not yet in its embedding cache -> {config.get('embed_model')}",
             f"  - search checks: the query {SEARCH_QUERY!r} -> {config.get('embed_model')}, and the query with the "
             f"candidate chunk texts -> {config.get('rerank_model')}",
             "  - the forced reprocess finds everything cached and sends nothing new"]
    estimate = []
    for d in run.docs:
        chunks_file = OUTPUT_PUBLIC / d.name / "chunks" / "chunks.jsonl"
        if chunks_file.is_file():
            chunks = parse_jsonl(chunks_file.read_bytes())
            figures = {f.get("image_path") for c in chunks for f in c.get("figures") or []}
            estimate.append(f"{d.name}: <= {len(figures)} figure requests, about "
                            f"{math.ceil(len(chunks) / EMBED_BATCH)} embedding requests ({len(chunks)} chunks)")
    lines.append("  roughly (from output_public, when nothing is cached yet): " + ("; ".join(estimate) or "unknown")
                 + f"; plus {len(run.docs)} query embedding(s) and {len(run.docs)} rerank(s)")
    return "\n".join(lines)


# ============================================================================ step 2: upload and jobs


def step_upload(run: Run) -> None:
    for d in run.docs:
        run.guarded("2", d.name, upload_one, run, d)
    for d in run.docs:
        run.guarded("2", f"{d.name} job", finish_one, run, d)


def upload_one(run: Run, d: Doc) -> None:
    r = run.report
    status, decision = upload(run, d)
    d.doc_id = decision.get("doc_id")
    r.check("2", f"{d.name}: doc_id is the SHA-256 of the PDF", d.doc_id == d.sha, f"{(d.doc_id or '')[:12]}")
    if status == 202:
        ok = decision.get("queued") is True and is_job_id(decision.get("job_id")) and not decision.get("duplicate")
        r.check("2", f"{d.name}: upload -> 202 queued", ok, f"job {str(decision.get('job_id'))[:8]}")
        d.job_id = decision.get("job_id")
    elif decision.get("job_id"):
        r.check("2", f"{d.name}: upload -> 200, a job of these bytes is already active", True,
                f"job {decision['job_id'][:8]}, status {decision.get('status')}")
        d.job_id = decision["job_id"]
    elif decision.get("stale"):
        print(f"    {d.name}: the stored result is stale (another pipeline version / fingerprint): "
              "uploading again with reprocess=true")
        status2, again = upload(run, d, reprocess=True)
        r.check("2", f"{d.name}: stale result -> reprocess queued (202)",
                status2 == 202 and is_job_id(again.get("job_id")), f"HTTP {status2}")
        d.job_id = again.get("job_id")
    else:
        ok = decision.get("duplicate") is True and decision.get("job_id") is None and not decision.get("queued")
        r.check("2", f"{d.name}: upload -> 200 duplicate (existing result reused)", ok,
                f"status {decision.get('status')}")


def force_reprocess(run: Run, d: Doc) -> tuple[int, dict]:
    run.require(f"reprocess {d.name}")
    return run.rest.json("POST", f"/documents/{d.doc_id}/reprocess", ok=(200, 202), params={"force": "true"})


def await_job(run: Run, d: Doc, job_id: str, label: str) -> dict:
    job = wait_job(run, job_id, d.name)
    error = job.get("error") or {}
    run.report.check("2", f"{d.name}: {label} job finished", job.get("state") in RESULT_STATES,
                     f"{job.get('state')}" + (f", error {error.get('type')} at {error.get('where')}" if error else ""))
    d.jobs.append((label, job))
    return job


def served_result(run: Run, d: Doc) -> tuple[dict, dict | None]:
    """(GET /documents/{id}, the job that made the served result or None)."""
    _, record = run.rest.json("GET", f"/documents/{d.doc_id}")
    job = run.rest.json("GET", f"/jobs/{record['result_job_id']}")[1] if record.get("result_job_id") else None
    return record, job


def finish_one(run: Run, d: Doc) -> None:
    r = run.report
    if not d.doc_id:
        r.add("2", f"{d.name}: processing", SKIP, "no upload")
        return
    if d.job_id:
        await_job(run, d, d.job_id, "process")
    record, d.job = served_result(run, d)
    if d.job_id is None and run.offline is False and d.job is not None:
        # an existing result made while the service was offline: the online checks need an online result
        made_offline = [n for n in ("figures", "embed") if (stage_map(d.job).get(n) or {}).get("status")
                        == "skipped_offline"]
        if made_offline:
            print(f"    {d.name}: the existing result was made offline ({', '.join(made_offline)} skipped_offline) "
                  "and the service is online now: forcing a reprocess so that the online checks apply")
            status, decision = force_reprocess(run, d)
            if r.check("2", f"{d.name}: reprocess of the offline-made result -> 202 job",
                       status == 202 and is_job_id(decision.get("job_id")), f"HTTP {status}"):
                d.job_id = decision["job_id"]
                await_job(run, d, d.job_id, "online reprocess")
                record, d.job = served_result(run, d)
    d.ready = bool(record.get("has_result")) and d.job is not None
    if d.job is not None:
        print(f"    {d.name} stages: {stage_line(d.job)}")


# ============================================================================ step 3: document


def step_document(run: Run) -> None:
    for d in run.docs:
        if not d.ready:
            run.report.add("3", f"{d.name}: document checks", SKIP, "no result")
            continue
        run.guarded("3", d.name, document_one, run, d)


def document_one(run: Run, d: Doc) -> None:
    r = run.report
    _, rec = run.rest.json("GET", f"/documents/{d.doc_id}")
    expected = "done_with_warnings" if run.offline else "done"
    warnings = rec.get("warnings") or []
    r.check("3", f"{d.name}: status {expected}", rec.get("status") == expected,
            f"status {rec.get('status')}, {len(warnings)} warning(s)")
    if rec.get("status") != expected:
        for w in warnings[:5]:  # generated by the pipeline (counts and reasons), never document text
            print(f"      warning: {str(w)[:120]}")
    r.check("3", f"{d.name}: stale false, has_result true", rec.get("stale") is False and rec.get("has_result") is True,
            f"stale {rec.get('stale')}, has_result {rec.get('has_result')}")
    ids = rec.get("identifiers") or {}
    if d.name in KNOWN_IDS:
        key, value = KNOWN_IDS[d.name]
        r.check("3", f"{d.name}: identifier {key} = {value}", ids.get(key) == value, f"got {ids.get(key)}")
    else:
        print(f"    {d.name} identifiers present: {sorted(k for k, v in ids.items() if v)}")
    _, quality = run.rest.json("GET", f"/documents/{d.doc_id}/files/quality.json")
    coverage = ((quality or {}).get("coverage") or {}).get("coverage")
    d.summary = (quality or {}).get("summary") or {}
    r.check("3", f"{d.name}: word coverage >= {MIN_COVERAGE}", isinstance(coverage, (int, float))
            and coverage >= MIN_COVERAGE, f"coverage {coverage}")
    tables, chunks = d.summary.get("tables"), d.summary.get("chunks")
    r.check("3", f"{d.name}: quality summary tables > 0 and chunks > 0",
            isinstance(tables, int) and tables > 0 and isinstance(chunks, int) and chunks > 0,
            f"tables {tables}, chunks {chunks}, pages {rec.get('pages')}")
    stages = stage_map(d.job)
    fig, emb = stages.get("figures") or {}, stages.get("embed") or {}
    fig_detail, emb_detail = fig.get("detail") or {}, emb.get("detail") or {}
    print(f"    {d.name} figures: {fig.get('status')} ({detail_counts(fig) or 'no counts'}); "
          f"embed: {emb.get('status')} ({detail_counts(emb) or 'no counts'})")
    if run.offline:
        r.check("3", f"{d.name}: figures not described offline",
                fig.get("status") in ("skipped_offline", "done", "skipped") and not fig_detail.get("described_now")
                and not fig_detail.get("failed"), f"figures {fig.get('status')}")
        r.check("3", f"{d.name}: embed skipped_offline (or all cached)",
                emb.get("status") == "skipped_offline" or (emb.get("status") == "done"
                                                           and emb_detail.get("embedded") == 0),
                f"embed {emb.get('status')}")
    else:
        if run.figure_model:
            r.check("3", f"{d.name}: figures stage done, none failed",
                    fig.get("status") == "done" and not fig_detail.get("failed") and not fig_detail.get("offline"),
                    f"figures {fig.get('status')}, failed {fig_detail.get('failed', 0)}")
        else:
            r.check("3", f"{d.name}: figures skipped (FIGURE_MODEL is empty)", fig.get("status") == "skipped",
                    f"figures {fig.get('status')}")
        r.check("3", f"{d.name}: embed stage done", emb.get("status") == "done", f"embed {emb.get('status')}")


# ============================================================================ step 4: files


def step_files(run: Run) -> None:
    for d in run.docs:
        if not d.ready:
            run.report.add("4", f"{d.name}: files", SKIP, "no result")
            continue
        run.guarded("4", d.name, files_one, run, d)
    run.report.check("4", "no response body names a server path", not run.scanner.hits,
                     f"{run.scanner.bodies} bodies scanned" + (f"; hits in {run.scanner.hits[:3]}"
                                                               if run.scanner.hits else ""))


def files_one(run: Run, d: Doc) -> None:
    r = run.report
    base = f"/documents/{d.doc_id}/files"
    _, listing = run.rest.json("GET", base)
    files, images = listing.get("files") or [], listing.get("images") or []
    r.check("4", f"{d.name}: files listed", "chunks/chunks.jsonl" in files and "quality.json" in files,
            f"{len(files)} files, {len(images)} images")
    bad, size, chunk_bytes = [], 0, None
    for name in files:
        resp = run.rest.request("GET", f"{base}/{urlquote(name, safe='/')}")
        if resp.status_code != 200 or not resp.content:
            bad.append(f"{name} HTTP {resp.status_code}")
            continue
        size += len(resp.content)
        if name == "chunks/chunks.jsonl":
            chunk_bytes = resp.content
    r.check("4", f"{d.name}: every listed file downloads", not bad,
            f"{len(files) - len(bad)}/{len(files)}, {size // 1024} KiB" + (f"; failed {bad[:3]}" if bad else ""))
    if images:
        resp = run.rest.request("GET", f"{base}/{urlquote(images[0], safe='/')}", scan=False)
        r.check("4", f"{d.name}: an image downloads", resp.status_code == 200 and len(resp.content) > 0
                and resp.headers.get("content-type", "").startswith("image/"),
                f"HTTP {resp.status_code}, {resp.headers.get('content-type')}, {len(resp.content)} bytes")
    else:
        r.add("4", f"{d.name}: an image downloads", SKIP, "no images")
    if chunk_bytes is None:
        r.add("4", f"{d.name}: chunks.jsonl", FAIL, "not downloaded")
        return
    chunks = parse_jsonl(chunk_bytes)
    d.chunk_texts = texts_hash(chunks)
    expected = (d.summary or {}).get("chunks")
    r.check("4", f"{d.name}: chunks.jsonl parses", len(chunks) > 0 and (expected is None or len(chunks) == expected),
            f"{len(chunks)} chunks (quality summary {expected})")
    alias = run.rest.request("GET", f"{base}/chunks.jsonl", scan=False)
    r.check("4", f"{d.name}: short name chunks.jsonl serves the same file",
            alias.status_code == 200 and alias.content == chunk_bytes, f"HTTP {alias.status_code}")
    paths = [f.get("image_path") for c in chunks for f in c.get("figures") or []]
    named = sorted({p for p in paths if p})
    relative = all(not p.startswith(("/", "\\")) and ":" not in p and ".." not in p.split("/") for p in named)
    unserved = []
    for p in named:
        resp = run.rest.request("GET", f"{base}/{urlquote(p, safe='/')}", scan=False)
        if resp.status_code != 200 or not resp.content:
            unserved.append(f"HTTP {resp.status_code}")
    r.check("4", f"{d.name}: figure image_paths relative and served", relative and not unserved,
            f"{len(paths)} figure refs, {len(named)} images, {len(paths) - len([p for p in paths if p])} without "
            f"a path, {len(unserved)} not served, relative {relative}")


# ============================================================================ step 5: REST tools


def step_rest_tools(run: Run) -> None:
    for d in run.docs:
        if not d.ready:
            run.report.add("5", f"{d.name}: REST tools", SKIP, "no result")
            continue
        run.guarded("5", d.name, rest_tools_one, run, d)


def call(run: Run, d: Doc, label: str, tool: str, args: dict) -> tuple[int, Any]:
    if tool == "search":
        run.require(f"search on {d.name}")  # online: the query and candidate chunks go to OpenRouter
        run.searches += 1
    status, body = run.rest.tool(d.doc_id, tool, args)
    d.calls.append((label, tool, args))
    d.results[label] = (status, body)
    return status, body


def err_type(body: Any) -> str | None:
    return body.get("type") if isinstance(body, dict) else None


def pick_quote(result: dict) -> str | None:
    """A short verbatim piece of read_section's Markdown: the first text / list line of 6+ words without markup
    or symbols the normalisations of quote_check treat specially; at most 12 words."""
    md = result.get("markdown") or ""
    for block in result.get("blocks") or []:
        if block.get("kind") not in ("text", "list"):
            continue
        a, z = (block.get("chars") or [0, 0])[:2]
        for line in md[a:z].splitlines():
            line = re.sub(r"<!--.*?-->|\{#[^}]*\}", " ", line)
            line = re.sub(r"^\s*(?:[-*+]|\(?(?:\d{1,3}|[ivx]{1,4}|[a-z])[.)])\s+", "", line)
            words = line.split()[:12]
            text = " ".join(words)
            if len(words) >= 6 and not _QUOTE_UNSAFE.search(text):
                return text
    return None


def rest_tools_one(run: Run, d: Doc) -> None:
    r = run.report
    n = d.name
    st, info = call(run, d, "document_info", "document_info", {})
    r.check("5", f"{n}: document_info", st == 200 and isinstance((info or {}).get("identifiers"), dict),
            f"HTTP {st}, sections {(info or {}).get('section_count')}, SoA tables "
            f"{(info or {}).get('soa_table_count')}")
    st, outline = call(run, d, "outline", "outline", {"max_level": 1})
    r.check("5", f"{n}: outline max_level 1", st == 200 and (outline or {}).get("count", 0) > 0,
            f"HTTP {st}, {(outline or {}).get('count')} of {(outline or {}).get('total_sections')} sections")
    st, found = call(run, d, "find_sections", "find_sections", {"role": "inclusion"})
    r.check("5", f"{n}: find_sections role inclusion", st == 200 and len((found or {}).get("sections") or []) > 0,
            f"HTTP {st}, {len((found or {}).get('sections') or [])} section(s)")
    # read_section('inclusion') with the default part size, following 'cursor' until it is absent
    st, first = call(run, d, "read_section", "read_section", {"section": "inclusion"})
    parts, part = [], first
    while st == 200 and part is not None:
        parts.append(part)
        cursor = part.get("cursor")
        if not cursor or len(parts) > 100:
            break
        st, part = run.rest.tool(d.doc_id, "read_section", {"cursor": cursor})
    complete = st == 200 and bool(parts) and not parts[-1].get("cursor")
    d.inclusion_md = "\n\n".join(p.get("markdown") or "" for p in parts) if complete else None
    r.check("5", f"{n}: read_section inclusion complete", complete,
            f"HTTP {st}, {len(parts)} part(s), {sum(p.get('tokens') or 0 for p in parts)} tokens, "
            f"mode {(first or {}).get('mode')}")
    # the same, in parts of PAGE_TOKENS: contiguous blocks up to the last one
    st, paged = call(run, d, "read_section paged", "read_section", {"section": "inclusion", "max_tokens": PAGE_TOKENS})
    chain, ok, part = [], st == 200, paged
    while ok and part is not None and len(chain) <= 200:
        chain.append(part)
        cursor = part.get("cursor")
        if not cursor:
            break
        args = {"cursor": cursor, "max_tokens": PAGE_TOKENS}
        if len(chain) == 1:
            st, part = call(run, d, "read_section cursor", "read_section", args)
        else:
            st, part = run.rest.tool(d.doc_id, "read_section", args)
        ok = st == 200
    spans = [(p.get("part") or {}) for p in chain]
    contiguous = ok and bool(spans) and spans[0].get("first_block") == 0 and all(
        a.get("end_block") == b.get("first_block") for a, b in zip(spans, spans[1:])) and \
        spans[-1].get("end_block") == spans[-1].get("total_blocks") and not chain[-1].get("cursor")
    r.check("5", f"{n}: read_section paged by cursor ({PAGE_TOKENS} tokens)", contiguous and len(chain) >= 2,
            f"HTTP {st}, {len(chain)} parts, {spans[-1].get('total_blocks') if spans else '?'} blocks")
    # get_soa, then get_table on a real SoA table
    st, soa = call(run, d, "get_soa", "get_soa", {})
    soas = (soa or {}).get("soas") or []
    r.check("5", f"{n}: get_soa", st == 200 and (soa or {}).get("available") is True,
            f"HTTP {st}, available {(soa or {}).get('available')}, {len(soas)} SoA(s), visits "
            f"{[len(s.get('visits') or []) for s in soas]}")
    table = next((s.get("table_id") for s in soas if s.get("table_id")), None) or next(
        (t.get("table_id") for t in (info or {}).get("soa_tables") or []), None) or "#/tables/0"
    st, tab = call(run, d, "get_table", "get_table", {"table": table, "format": "markdown"})
    r.check("5", f"{n}: get_table {table}", st == 200 and (tab or {}).get("rows", 0) > 0
            and bool((tab or {}).get("markdown")),
            f"HTTP {st}, {(tab or {}).get('rows')} rows x {(tab or {}).get('cols')} cols, "
            f"{len((tab or {}).get('table_ids') or [])} fragment(s)")
    st, found = call(run, d, "search", "search", {"query": SEARCH_QUERY})
    if run.offline:
        r.check("5", f"{n}: search offline -> 409 search_unavailable",
                st == 409 and err_type(found) == "search_unavailable", f"HTTP {st} {err_type(found)}")
    else:
        r.check("5", f"{n}: search", st == 200 and len((found or {}).get("blocks") or []) > 0,
                f"HTTP {st} {err_type(found) or ''}, {len((found or {}).get('blocks') or [])} blocks, "
                f"{len((found or {}).get('hits') or [])} hits")
    st, abbr = call(run, d, "abbreviation", "abbreviation", {"term": "AE"})
    r.check("5", f"{n}: abbreviation AE", st == 200 and (abbr or {}).get("term") == "AE",
            f"HTTP {st}, found {(abbr or {}).get('found')}, {len((abbr or {}).get('matches') or [])} match(es)")
    d.quote = pick_quote(first or {}) if first else None
    if d.quote:
        st, qc = call(run, d, "quote_check found", "quote_check", {"quote": d.quote})
        r.check("5", f"{n}: quote_check of a read_section sentence -> found", st == 200 and (qc or {}).get("found"),
                f"HTTP {st}, {len(d.quote.split())} words, match {(qc or {}).get('match')}, "
                f"{(qc or {}).get('count')} location(s)")
    else:
        r.add("5", f"{n}: quote_check of a read_section sentence -> found", FAIL, "no usable sentence")
    st, qc = call(run, d, "quote_check made-up", "quote_check", {"quote": MADE_UP})
    r.check("5", f"{n}: quote_check of a made-up sentence -> not found",
            st == 200 and (qc or {}).get("found") is False, f"HTTP {st}, found {(qc or {}).get('found')}")


# ============================================================================ steps 6 and 7: MCP


def mcp_body(result) -> tuple[Any, bool]:
    """(structuredContent, the text block parses to the same JSON)."""
    sc = result.structured_content
    content = result.content or []
    text_ok = len(content) == 1 and getattr(content[0], "type", None) == "text" and json.loads(content[0].text) == sc
    return sc, text_ok


def same_as_rest(run: Run, result, status: int, body: Any) -> bool:
    sc, text_ok = mcp_body(result)
    run.scanner.scan("mcp result", json.dumps(sc, ensure_ascii=False).encode("utf-8"))
    if status == 200:
        return not result.is_error and text_ok and sc == body
    return bool(result.is_error) and text_ok and isinstance(sc, dict) and sc.get("type") == err_type(body)


async def rest_async(fn: Callable, *args) -> Any:
    return await anyio.to_thread.run_sync(partial(fn, *args))


async def doc_parity(run: Run, client, d: Doc, calls: list[tuple[str, str, dict]]) -> tuple[int, list[str]]:
    """Each (label, tool, args) through MCP and REST (fresh); returns (count, labels that differ)."""
    differ = []
    for label, tool, args in calls:
        result = await client.call_tool(tool, {"doc_id": d.doc_id, **args})
        status, body = await rest_async(run.rest.tool, d.doc_id, tool, args)
        if not same_as_rest(run, result, status, body):
            differ.append(label)
    return len(calls), differ


def mcp_target(url: str):
    """What mcp.Client connects to: the URL, or with PROTOCOL_SERVICE_API_KEY set, a Streamable HTTP transport whose
    HTTP client sends the key (that client lives until the script ends)."""
    if not auth_headers():
        return url
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(headers=auth_headers(), timeout=httpx2.Timeout(30.0, read=TOOL_TIMEOUT))
    return streamable_http_client(url, http_client=http)


async def mcp_http(run: Run) -> None:
    from mcp import Client

    r = run.report
    url = f"{run.base}/mcp"
    async with Client(mcp_target(url), mode="legacy", read_timeout_seconds=TOOL_TIMEOUT) as client:
        r.check("6", "initialize (handshake) at /mcp", client.server_info.name == "protocol-service",
                f"protocol {client.protocol_version}, server {client.server_info.name} {client.server_info.version}")
        names = [t.name for t in (await client.list_tools()).tools]
        r.check("6", "tools/list: 12 tools", sorted(names) == sorted(SERVICE_TOOLS + PROTOCOL_TOOLS),
                f"{len(names)} tools")
        result = await client.call_tool("list_documents", {})
        status, body = await rest_async(run.rest.json, "GET", "/documents")
        r.check("6", "list_documents = GET /documents", same_as_rest(run, result, status, body),
                f"{len((body or {}).get('documents') or [])} document(s)")
        for d in run.ready_docs():
            result = await client.call_tool("get_document", {"doc_id": d.doc_id})
            status, body = await rest_async(run.rest.json, "GET", f"/documents/{d.doc_id}")
            r.check("6", f"{d.name}: get_document = GET /documents/{{id}}", same_as_rest(run, result, status, body),
                    f"status {(body or {}).get('status')}")
            count, differ = await doc_parity(run, client, d, d.calls)
            r.check("6", f"{d.name}: protocol tools = REST (structuredContent)", count > 0 and not differ,
                    f"{count - len(differ)}/{count} equal" + (f"; differ: {differ}" if differ else ""))
        d = run.version_doc()
        if d is None:
            r.add("6", "ingest_document (base64) of a stored PDF", SKIP, "no result")
        else:
            run.require(f"MCP ingest_document {d.name}")
            before = await rest_async(job_count, run, d.doc_id)
            result = await client.call_tool("ingest_document", {"data_base64": base64.b64encode(d.data).decode(),
                                                                "filename": f"{d.name}.pdf"})
            sc, text_ok = mcp_body(result)
            sc = sc or {}
            after = await rest_async(job_count, run, d.doc_id)
            r.check("6", f"{d.name}: ingest_document (base64) -> duplicate, no job",
                    not result.is_error and text_ok and sc.get("duplicate") is True and sc.get("job_id") is None
                    and sc.get("queued") is False and sc.get("doc_id") == d.doc_id and after == before,
                    f"duplicate {sc.get('duplicate')}, job_id {sc.get('job_id')}, jobs {before} -> {after}")
    async with Client(url, read_timeout_seconds=TOOL_TIMEOUT) as client:  # the SDK's default era
        names = [t.name for t in (await client.list_tools()).tools]
        r.check("6", "default-era client: 12 tools", len(names) == 12, f"protocol {client.protocol_version}")


def step_mcp_http(run: Run) -> None:
    try:
        anyio.run(mcp_http, run)
    except Exception as exc:  # noqa: BLE001
        run.report.error("6", "MCP over HTTP", exc)


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


async def mcp_stdio(run: Run, argv: list[str], errlog) -> None:
    from mcp import Client, StdioServerParameters, stdio_client

    r = run.report
    params = StdioServerParameters(command=argv[0], args=argv[1:], env=dict(os.environ), cwd=str(ROOT))
    t0 = time.monotonic()
    async with Client(stdio_client(params, errlog=errlog), mode="legacy", read_timeout_seconds=TOOL_TIMEOUT) as client:
        r.check("7", "initialize over stdio", client.server_info.name == "protocol-service",
                f"handshake {time.monotonic() - t0:.1f}s, protocol {client.protocol_version}")
        names = [t.name for t in (await client.list_tools()).tools]
        r.check("7", "tools/list: 12 tools", sorted(names) == sorted(SERVICE_TOOLS + PROTOCOL_TOOLS),
                f"{len(names)} tools")
        for d in run.ready_docs():
            t1 = time.monotonic()
            result = await client.call_tool("get_document", {"doc_id": d.doc_id})
            status, body = await rest_async(run.rest.json, "GET", f"/documents/{d.doc_id}")
            r.check("7", f"{d.name}: get_document = REST", same_as_rest(run, result, status, body),
                    f"{time.monotonic() - t1:.1f}s")
            calls = [c for c in d.calls if c[0] in ("read_section", "quote_check found")]
            t1 = time.monotonic()
            count, differ = await doc_parity(run, client, d, calls)
            r.check("7", f"{d.name}: read_section, quote_check = REST", count == 2 and not differ,
                    f"{count - len(differ)}/{count} equal, {time.monotonic() - t1:.1f}s"
                    + (f"; differ: {differ}" if differ else ""))


def step_mcp_stdio(run: Run) -> None:
    r = run.report
    command = (run.args.stdio_cmd or "").strip()
    if not command or command.lower() == "none":
        r.add("7", "MCP over stdio", SKIP, "no --stdio-cmd")
        return
    argv = shlex.split(command, posix=True)
    is_docker = Path(argv[0]).name.lower().startswith("docker")
    fd, log_path = tempfile.mkstemp(prefix="smoke_stdio_", suffix=".log")
    os.close(fd)
    log_file = Path(log_path)
    try:
        with open(log_file, "w", encoding="utf-8", errors="replace") as errlog:
            try:
                anyio.run(mcp_stdio, run, argv, errlog)
            except Exception as exc:  # noqa: BLE001
                r.error("7", "MCP over stdio", exc)
                errlog.flush()
                lines = [line for line in log_file.read_text(encoding="utf-8", errors="replace").splitlines()
                         if re.search(r"(?i)\berror\b", line)]
                for line in lines[-5:]:  # the server logs exception types and file:line only
                    print(f"      stderr: {line[:160]}")
        text = log_file.read_text(encoding="utf-8", errors="replace")
        started = re.search(r"MCP stdio server started \(pid=(\d+)", text)
        stopped = "MCP stdio server stopped" in text
        if started is None:
            r.add("7", "stdio child exited", SKIP, "no INFO log from the child (LOG_LEVEL above INFO?)")
            return
        alive = None
        if not is_docker:  # a pid inside a container cannot be checked from here
            pid, deadline = int(started.group(1)), time.monotonic() + 10
            alive = process_alive(pid)
            while alive and time.monotonic() < deadline:
                time.sleep(0.2)
                alive = process_alive(pid)
        r.check("7", "stdio child exited when stdin closed", stopped and not alive,
                f"'stopped' logged {stopped}" + ("" if alive is None else f", process alive {alive}"))
    finally:
        try:
            log_file.unlink()
        except OSError:
            pass


# ============================================================================ step 8: known facts


def window_cover(needle: list[str], hay: list[str], slack: float = 1.5) -> float:
    """The largest share of `needle`'s tokens (with multiplicity) found in one window of `hay` of about
    slack x len(needle) tokens."""
    if not needle:
        return 1.0
    need = Counter(needle)
    width = max(len(needle), int(len(needle) * slack) + 2)
    window: Counter = Counter()
    have = best = 0
    for i, tok in enumerate(hay):
        window[tok] += 1
        if window[tok] <= need.get(tok, 0):
            have += 1
        if i >= width:
            old = hay[i - width]
            if window[old] <= need.get(old, 0):
                have -= 1
            window[old] -= 1
        best = max(best, have)
    return best / len(needle)


def step_facts(run: Run) -> None:
    for d in run.docs:
        if not d.ready:
            run.report.add("8", f"{d.name}: known facts", SKIP, "no result")
            continue
        run.guarded("8", d.name, facts_one, run, d)


def facts_one(run: Run, d: Doc) -> None:
    r = run.report
    status, info = d.results.get("document_info", (0, None))
    if d.name in KNOWN_IDS:
        key, value = KNOWN_IDS[d.name]
        got = (((info or {}).get("identifiers") or {}).get(key) or {}).get("value") if status == 200 else None
        r.check("8", f"{d.name}: document_info {key} = {value}", got == value, f"got {got}")
    try:
        import usdm_gold
        has_key = d.name in usdm_gold.key_paths()
    except Exception as exc:  # noqa: BLE001 - the key checks are optional
        r.add("8", f"{d.name}: USDM answer key", SKIP, f"usdm_gold unavailable ({where(exc)})")
        has_key = False
    study = usdm_gold.load_study(d.name) if has_key else None
    if study is None:
        r.add("8", f"{d.name}: inclusion criteria of the USDM key", SKIP, "no answer key")
    elif d.inclusion_md is None:
        r.add("8", f"{d.name}: inclusion criteria of the USDM key", FAIL, "read_section('inclusion') incomplete")
    else:
        hay = usdm_gold.content_tokens(d.inclusion_md)
        criteria = [c for c in study.of("EligibilityCriterion")
                    if "inclusion" in (c.attrs.get("category") or "").lower()]
        scores = [(c.attrs.get("identifier") or c.id, window_cover(usdm_gold.content_tokens(c.text), hay))
                  for c in criteria]
        missing = [ident for ident, s in scores if s < CRITERION_MATCH]
        r.check("8", f"{d.name}: every inclusion criterion of the USDM key in read_section('inclusion')",
                bool(scores) and not missing,
                f"{len(scores) - len(missing)}/{len(scores)} found (threshold {CRITERION_MATCH}, lowest "
                f"{min((s for _, s in scores), default=0):.2f}); ids {[i for i, _ in scores]}"
                + (f"; missing {missing}" if missing else ""))
    status, soa = d.results.get("get_soa", (0, None))
    soas = (soa or {}).get("soas") or []
    visits = max((len(s.get("visits") or []) for s in soas), default=0)
    activities = max((len(s.get("activities") or []) for s in soas), default=0)
    r.check("8", f"{d.name}: get_soa available with visits", status == 200 and (soa or {}).get("available") is True
            and visits > 0, f"{len(soas)} SoA(s), {visits} visits, {activities} activities")
    if study is not None:
        encounters = len(study.of("Encounter"))
        r.check("8", f"{d.name}: SoA visits >= encounters of the USDM key", encounters > 0 and visits >= encounters,
                f"{visits} visits vs {encounters} key encounters")


# ============================================================================ step 9: duplicates and versions


RULE = ("same bytes + current result -> duplicate, no job; reprocess=true queues only when the result is stale "
        "(another pipeline version or fingerprint); POST /documents/{id}/reprocess?force=true forces a job")


def step_versions(run: Run) -> None:
    d = run.version_doc()
    if d is None:
        run.report.add("9", "duplicate / version rules", SKIP, "no result")
        return
    run.guarded("9", d.name, versions_one, run, d)


def versions_one(run: Run, d: Doc) -> None:
    r = run.report
    print(f"    rule: {RULE}")
    before = job_count(run, d.doc_id)
    status, dec = upload(run, d)
    r.check("9", f"{d.name}: re-upload -> 200 duplicate, job_id null",
            status == 200 and dec.get("duplicate") is True and dec.get("job_id") is None and not dec.get("queued")
            and dec.get("stale") is False, f"HTTP {status}, duplicate {dec.get('duplicate')}, job {dec.get('job_id')}")
    status, dec = upload(run, d, reprocess=True)
    r.check("9", f"{d.name}: upload reprocess=true on a current result -> no job",
            status == 200 and dec.get("job_id") is None and not dec.get("queued"),
            f"HTTP {status}, stale {dec.get('stale')}, job {dec.get('job_id')}")
    after = job_count(run, d.doc_id)
    r.check("9", f"{d.name}: no new job from either upload", after == before, f"jobs {before} -> {after}")
    if run.args.skip_reprocess:
        r.add("9", f"{d.name}: forced reprocess", SKIP, "--skip-reprocess")
        return
    _, old = run.rest.json("GET", f"/documents/{d.doc_id}")
    status, dec = force_reprocess(run, d)
    job_id = dec.get("job_id")
    if not r.check("9", f"{d.name}: reprocess?force=true -> 202 job", status == 202 and dec.get("queued") is True
                   and is_job_id(job_id), f"HTTP {status}, job {str(job_id)[:8]}"):
        return
    job = wait_job(run, job_id, f"{d.name} reprocess")
    d.jobs.append(("reprocess", job))
    print(f"    {d.name} reprocess stages: {stage_line(job)}")
    expected = "done_with_warnings" if run.offline else "done"
    r.check("9", f"{d.name}: reprocess job {expected}", job.get("state") == expected, f"{job.get('state')}")
    _, new = run.rest.json("GET", f"/documents/{d.doc_id}")
    r.check("9", f"{d.name}: new result published", new.get("result_job_id") == job_id
            and job_id != old.get("result_job_id") and str(new.get("published_at") or "") > str(
                old.get("published_at") or ""),
            f"result job {str(old.get('result_job_id'))[:8]} -> {str(new.get('result_job_id'))[:8]}, published_at "
            f"moved {str(new.get('published_at') or '') > str(old.get('published_at') or '')}")
    resp = run.rest.request("GET", f"/documents/{d.doc_id}/files/chunks.jsonl")
    same = resp.status_code == 200 and d.chunk_texts is not None and texts_hash(parse_jsonl(resp.content)) == d.chunk_texts
    r.check("9", f"{d.name}: chunk texts unchanged", same, f"HTTP {resp.status_code}")
    stages = stage_map(job)
    fig, emb = stages.get("figures") or {}, stages.get("embed") or {}
    fig_detail, emb_detail = fig.get("detail") or {}, emb.get("detail") or {}
    print(f"    {d.name} reprocess figures: {fig.get('status')} ({detail_counts(fig) or 'no counts'}); "
          f"embed: {emb.get('status')} ({detail_counts(emb) or 'no counts'})")
    if run.offline:
        r.add("9", f"{d.name}: reprocess sent nothing new to OpenRouter", SKIP,
              f"offline: figures {fig.get('status')}, embed {emb.get('status')}")
        return
    r.check("9", f"{d.name}: reprocess described no figure (all cached)",
            not fig_detail.get("described_now") and not fig_detail.get("failed"),
            f"described_now {fig_detail.get('described_now', 0)}, already_cached {fig_detail.get('already_cached', 0)}")
    r.check("9", f"{d.name}: reprocess embedded no chunk (all cached)",
            emb.get("status") == "done" and emb_detail.get("embedded") == 0,
            f"embedded {emb_detail.get('embedded')}, cached {emb_detail.get('cached')}")


# ============================================================================ step 10: summary


def summary(run: Run, started: float) -> None:
    r = run.report
    r.check("10", "no server path in any response body", not run.scanner.hits,
            f"{run.scanner.bodies} bodies scanned" + (f"; hits in {run.scanner.hits[:3]}" if run.scanner.hits else ""))
    print("\n" + "=" * 100)
    print(f"{'step':<5} {'result':<6} check")
    for step, name, status, detail in r.rows:
        print(f"{step:<5} {status:<6} {name}" + (f"  [{detail}]" if detail and status != PASS else ""))
    counts = Counter(row[2] for row in r.rows)
    print("-" * 100)
    print(f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    print("\nJob stage timings:")
    for d in run.docs:
        if d.job is not None and not any(label in ("process", "online reprocess") for label, _ in d.jobs):
            print(f"  {d.name} existing result: {stage_line(d.job)}")
        for label, job in d.jobs:
            print(f"  {d.name} {label} ({job.get('state')}): {stage_line(job)}")
    print("\nOpenRouter (counts from the jobs of this run):")
    if run.offline:
        print("  none: the service is offline")
    else:
        for d in run.docs:
            for label, job in d.jobs:
                stages = stage_map(job)
                print(f"  {d.name} {label}: figures [{detail_counts(stages.get('figures')) or '-'}], "
                      f"embed [{detail_counts(stages.get('embed')) or '-'}]")
        print(f"  search calls: {run.searches} (a query not yet cached costs 1 query embedding + 1 rerank)")
    print("\nStep timings: " + ", ".join(f"{s} {t:.0f}s" for s, t in run.timings))
    print(f"Total wall time: {time.monotonic() - started:.0f}s")


# ============================================================================ main


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="End-to-end smoke test of the protocol service (REST, MCP over "
                                                 "HTTP and stdio) with the public protocols.")
    parser.add_argument("--base-url", default=DEFAULT_BASE, help=f"the service (default {DEFAULT_BASE})")
    parser.add_argument("--stdio-cmd", default=DEFAULT_STDIO,
                        help=f"command of the MCP stdio server, split with shlex in POSIX mode (default "
                             f"{DEFAULT_STDIO!r}; '' skips step 7)")
    parser.add_argument("--protocols", nargs="+", default=list(DEFAULT_PROTOCOLS),
                        help="folder names under public_protocols/ (each with a protocol.pdf)")
    parser.add_argument("--timeout-min", type=float, default=120.0, help="limit per processing job (minutes)")
    parser.add_argument("--allow-openrouter", action="store_true",
                        help="accept that an online service sends the public protocols' figures, chunk texts and "
                             "the test query to OpenRouter (zero-data-retention routing)")
    parser.add_argument("--skip-reprocess", action="store_true", help="skip the forced reprocess of step 9")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    docs = []
    for name in dict.fromkeys(args.protocols):
        try:
            pdf = public_protocol(name)
        except NotPublic as exc:
            print(exc, file=sys.stderr)
            return 2
        data = pdf.read_bytes()
        docs.append(Doc(name=name, pdf=pdf, data=data, sha=hashlib.sha256(data).hexdigest()))
    run = Run(args, docs)
    started = time.monotonic()
    print(f"Smoke test of {run.base}: {', '.join(f'{d.name} ({len(d.data) // 1024} KiB, {d.sha[:12]})' for d in docs)}")
    steps: list[tuple[str, str, Callable[[Run], None]]] = [
        ("1", "service", step_service), ("2", "upload and processing", step_upload),
        ("3", "document", step_document), ("4", "files", step_files), ("5", "REST tools", step_rest_tools),
        ("6", "MCP over HTTP", step_mcp_http), ("7", "MCP over stdio", step_mcp_stdio),
        ("8", "known facts", step_facts), ("9", "duplicate and version rules", step_versions)]
    try:
        for number, title, fn in steps:
            print(f"\n[{number}] {title}", flush=True)
            t0 = time.monotonic()
            try:
                fn(run)
            except Exception as exc:  # noqa: BLE001
                run.report.error(number, title, exc)
            run.timings.append((number, time.monotonic() - t0))
            if number == "1":
                if run.offline is None:
                    print("\nThe service did not answer GET /version: stopping.")
                    summary(run, started)
                    return 1
                if run.offline:
                    print("    guard: the service is offline: nothing is sent to OpenRouter")
                elif args.allow_openrouter:
                    run.allowed = True
                    print("    guard: --allow-openrouter given\n" + openrouter_notice(run))
                else:
                    print("\nREFUSED (nothing was uploaded):\n" + openrouter_notice(run)
                          + "\nRe-run with --allow-openrouter to accept.")
                    return 2
    except KeyboardInterrupt:
        print("\ninterrupted")
        summary(run, started)
        return 130
    finally:
        run.rest.close()
    summary(run, started)
    return 1 if run.report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
