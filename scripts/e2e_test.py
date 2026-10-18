"""Realistic end-to-end test of the protocol service running in Docker: uploads by every path, an extraction agent's
tool workflow over MCP HTTP and MCP stdio scored against the public USDM answer keys, several clients at once, a
restart in the middle of a conversion, the duplicate / version rules, failure cases, cache purge, a backup and
restore drill, and measurements (stage times, tool latency, memory, OpenRouter requests).

    .venv\\Scripts\\python.exe scripts\\e2e_test.py --allow-openrouter              # the compose service (port 8000)
    .venv\\Scripts\\python.exe scripts\\e2e_test.py --target e2e                    # a disposable offline container

Targets (--target): "compose" (default): http://127.0.0.1:8000, container protocol-service, restart with
`docker compose restart protocol-service` (run from the project root), stdio through
`docker exec -i protocol-service python -m service.mcp_stdio`, drill port 8001. "e2e": http://127.0.0.1:8002,
container protocol-e2e (`docker restart protocol-e2e`), drill port 8003. --base-url, --container, --restart-cmd,
--stdio-cmd and --drill-port override single values (commands are split with shlex, POSIX rules).

Protocols (--protocols, default Alexion_NCT04573309 Bayer_NCT04304508 PrECOG_NCT01797120 Sanofi_NCT03637764): folder
names directly under public_protocols/ only. Their order sets the roles (one worker: order matters): the 1st is
uploaded by REST and read over MCP HTTP, the 2nd by MCP ingest_document (data_base64) and read over MCP stdio, the
3rd through the inbox (docker cp + MCP ingest_document {"path"}) and read over stdio, the 4th by REST and read over
HTTP; the restart interrupts the 3rd (or, if it already finished, the 4th); the concurrency step runs while the
last converts. Copies of these protocols already in the service are deleted first (caches kept).

Steps (PASS / FAIL / SKIP table at the end; exit 1 on any FAIL, 2 when refused):
  1 service       /health, /version; a foreign Host -> 421 invalid_host, a foreign Origin -> 403 invalid_origin (REST
                  and /mcp); /tools.
  2 upload        the four upload paths, each a queued job (202 / queued: true); jobs polled in the background
                  (state / stage / progress changes printed); per-stage seconds.
  3 agent         per document as soon as it is done, over MCP HTTP or stdio: get_document polling, document_info,
                  outline (max_level 1 and 2), find_sections (inclusion, exclusion, objectives_endpoints), read_section
                  of the three roles following every cursor (parts of 2000 tokens), get_soa, get_table (rows) of every
                  SoA table, two search queries, quote_check of every value an agent would extract (each criterion
                  line, each objective / endpoint line or table cell, each SoA visit label, the NCT / sponsor id
                  values and their source lines). Scored against the public USDM keys (usdm_gold) where one exists:
                  identifiers equal, every key inclusion / exclusion criterion found (token window >= 0.8), SoA visits
                  and activities matched (soa.compare_with_key: usdm_gold's label / number alignment). FAIL: a wrong
                  identifier, a missing key criterion, a quote taken verbatim from a tool result not found by
                  quote_check (100% required), SoA below the floors (encounter recall < 0.8, activity recall < 0.2,
                  cell F1 < 0.7: far below the host baseline of the same pipeline, 1.0 / 0.28-1.0 / 0.81-0.89, so
                  they catch a missing or broken SoA, not the known extraction limits). SoA visit labels are composed
                  by soa.py (e.g. "Hour 0" from a "Hours" stub and a "0" header): their quote_check rate is reported
                  only; the header cells they were built from (get_soa header_cells, and the stub a unit came from)
                  are verbatim and count in the 100% check.
  4 concurrency   while the last protocol converts: agent 1 (1st document, MCP HTTP), agent 2 (2nd document, MCP
                  stdio) and a REST client repeating the same tool calls, plus /health polled every 0.5 s. MCP
                  structuredContent must equal the REST JSON for identical calls; no error. FAIL: /health max >= 2 s
                  (a SQLite read, the conversion runs in a child process; Docker's healthcheck allows 5 s), a local
                  tool call >= 60 s (the first call opens the document and, over stdio, waits for the service to
                  start in the new process: about 15 s per DEPLOY.md, x4 headroom under a running conversion), the
                  median of a tool called at least 4 times >= 3 s (the warm calls: quote_check, read_section, ...).
  5 restart       (--skip-restart skips) the restart command while the 3rd protocol converts; health awaited; the job
                  must be re-queued (attempts >= 2), finish done and be published. Never while an encrypted-PDF job
                  with a password is queued (passwords are memory-only): the encrypted upload comes after the restart.
  6 versions      re-upload of a done document -> 200 duplicate, job_id null; reprocess=true -> no job;
                  POST /documents/{id}/reprocess?force=true -> 202 and done; online it sends nothing new (figures
                  described_now 0, embed embedded 0).
  7 failures      a truncated public PDF -> 400 invalid_pdf; a text file -> 415 not_pdf; a made-up encrypted PDF
                  (e2e_lib, RC4 128-bit): no password -> 400 password_required, wrong -> 400 bad_password, right -> 202
                  and done; > MAX_UPLOAD_MB -> 413 too_large (Content-Length check, and a streamed body); a queued job
                  (queued behind a running one) cancelled; cancelling a running job -> 409 not_cancellable; deleting a
                  document whose job runs -> 409 document_busy; deleting a done document -> deleted, its files 404, its
                  conversion batches kept.
  8 operations    DELETE /documents/{id}?purge_caches=true on the encrypted made-up document (or, when it has no result,
                  the one-page made-up PDF uploaded once more): its batch cache and figure descriptions are gone
                  (counted with docker exec), every other document's cache is intact (offline, two placeholder figure
                  entries are seeded: the document's and another one; online, its chart's description). Backup /
                  restore drill (--skip-drill skips): read-only tar of the volume into a scratch folder, restore into a
                  new scratch volume, a throwaway container <container>-drill on 127.0.0.1:<drill port> (offline,
                  FIGURE_MODEL= so its fingerprint differs): registry integrity, document count, every result stale,
                  an existing PDF answers stale without a job, reprocess=true queues a job that finishes from caches;
                  then the container, the volume and the archive are removed.
  9 measurements  per-stage seconds per document; tool latency median / max per tool and transport; OpenRouter
                  requests per document DERIVED from job details (figures described_now + failed, embedding batches =
                  ceil(embedded / 32), search: 2 per query not issued before by this run; retries excluded); peak
                  container memory (docker stats every 5 s, and the cgroup's memory.peak, which a restart resets).
 10 summary       the table, scores, timings, counts, memory, wall time; --report <file.json> writes the same as JSON.

What is sent where. The public PDFs and two made-up PDFs go to the service only. A service that is ONLINE sends to
OpenRouter (zero-data-retention routing): figure images not in its cache, chunk texts not in its embedding cache, the
search queries with candidate chunks, and the made-up encrypted PDF's chart. Guard: when /version says offline=false
nothing is uploaded without --allow-openrouter (the script prints what would be sent and exits 2). The drill
container is always offline.

--numbers-only --pdf-label MAIN|P1|P2|P3: the same flow for ONE private PDF (resolved with test_protocols.source_pdf,
never printed) in a separate scratch container (--private-container, default protocol-service-private on
127.0.0.1:8004, its own volume; created and removed by the script; --allow-openrouter required). Only counts,
booleans and timings are printed: no identifiers, titles, quotes, file names or document ids; the score is structure
counts and the quote_check pass rate (no USDM key). --rehearsal <public name> runs that flow with a public PDF and an
offline scratch container, to check what it prints.

Output: ids, counts, timings, short labels of public documents; errors as HTTP status + error type, or exception type
+ our file:line; never a service error message.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for _p in (ROOT, ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import anyio  # noqa: E402
import httpx  # noqa: E402

import e2e_lib as lib  # noqa: E402
from smoke_test import (FINAL, RESULT_STATES, HttpError, NotPublic, Refused, detail_counts,  # noqa: E402
                        is_job_id, mcp_body, public_protocol, stage_line, stage_map)
from smoke_test import where as _where  # noqa: E402

PUBLIC = ROOT / "public_protocols"
OUTPUT_PUBLIC = ROOT / "output_public"
DEFAULT_PROTOCOLS = ("Alexion_NCT04573309", "Bayer_NCT04304508", "PrECOG_NCT01797120", "Sanofi_NCT03637764")
TARGETS = {
    "compose": {"base_url": "http://127.0.0.1:8000", "container": "protocol-service",
                "restart_cmd": "docker compose restart protocol-service",
                "stdio_cmd": "docker exec -i protocol-service python -m service.mcp_stdio", "drill_port": 8001},
    "e2e": {"base_url": "http://127.0.0.1:8002", "container": "protocol-e2e",
            "restart_cmd": "docker restart protocol-e2e",
            "stdio_cmd": "docker exec -i protocol-e2e python -m service.mcp_stdio", "drill_port": 8003},
}
PRIVATE_DEFAULT = {"container": "protocol-service-private", "port": 8004, "drill_port": 8005}
ROLES3 = ("inclusion", "exclusion", "objectives_endpoints")
SEARCH_QUERIES = ("What is the dose and route of the study drug?", "How long is the screening period?")
PART_TOKENS = 2000            # read_section part size: longer sections continue through 'cursor'
TOOL_TIMEOUT = 900.0
POLL_SECONDS = 2.0
PROGRESS_EVERY = 60.0
STARTUP_WAIT = 300.0          # a restarted container answers /health after about 15-30 s
HEALTH_MAX = 2.0              # seconds: /health max latency during the concurrency step
TOOL_MAX = 60.0               # seconds: any local tool call
TOOL_MEDIAN = 3.0             # seconds: median per tool ...
MEDIAN_MIN_CALLS = 4          # ... called at least this often (get_document / document_info / get_soa: once each)
ENC_RECALL_FLOOR, ACT_RECALL_FLOOR, CELL_F1_FLOOR = 0.8, 0.2, 0.7
LABEL = "e2e_test"            # docker label of every container / volume this script creates
# the options docker-compose.yml gives the service, for the containers this script starts
HARDENING = ("--memory", "8g", "--security-opt", "no-new-privileges:true", "--cap-drop", "ALL")
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
E_PASSWORD = "e2e-made-up-password-7Q"   # password of the made-up encrypted PDF (not a secret)
E_CODE, Q_CODE = "ENC-01", "QUEUE-01"


# ============================================================================ plumbing


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Report:
    """Named checks, printed as they happen, tabled at the end, exported as JSON."""

    def __init__(self, numbers_only: bool) -> None:
        self.rows: list[dict] = []
        self.numbers_only = numbers_only
        self.lock = threading.Lock()

    def add(self, step: str, name: str, status: str, detail: str = "") -> None:
        if self.numbers_only:
            name, detail = lib.scrub(name), lib.scrub(detail)
        with self.lock:
            self.rows.append({"step": step, "name": name, "status": status, "detail": detail})
            print(f"  {status:<4} {name}" + (f"  [{detail}]" if detail else ""), flush=True)

    def check(self, step: str, name: str, ok: bool, detail: str = "") -> bool:
        self.add(step, name, PASS if ok else FAIL, detail)
        return bool(ok)

    def error(self, step: str, name: str, exc: BaseException) -> None:
        self.add(step, name, FAIL, _where(exc))

    @property
    def failed(self) -> int:
        return sum(r["status"] == FAIL for r in self.rows)


@dataclass
class Target:
    base: str
    container: str
    restart_cmd: list[str]
    stdio_cmd: list[str]
    drill_port: int
    volume: str | None = None
    image: str | None = None


@dataclass
class Doc:
    name: str                      # public folder name, or "DOC" / "ENC" / "QUEUE"
    label: str                     # what the output shows
    data: bytes
    pdf: Path | None = None        # public PDF (never set for a private one)
    upload: str = "rest"           # rest | mcp_base64 | inbox
    transport: str = "http"        # agent transport of step 3: http | stdio
    password: str | None = None
    doc_id: str | None = None
    job_id: str | None = None
    final_job: dict | None = None
    jobs: list[tuple[str, dict]] = field(default_factory=list)
    workflows: list[dict] = field(default_factory=list)
    scores: dict = field(default_factory=dict)

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class Latency:
    """Seconds per (phase, transport, tool)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str, str], list[float]] = defaultdict(list)
        self.lock = threading.Lock()

    def add(self, phase: str, transport: str, tool: str, seconds: float) -> None:
        with self.lock:
            self.data[(phase, transport, tool)].append(seconds)

    def summary(self) -> dict:
        out: dict = {}
        with self.lock:
            for (phase, transport, tool), vals in sorted(self.data.items()):
                out.setdefault(phase, {}).setdefault(transport, {})[tool] = lib.summarize(vals)
        return out


def docker(*args: str, input_bytes: bytes | None = None, timeout: float = 180) -> subprocess.CompletedProcess:
    """`docker <args>` (bytes in, bytes out); never raises on a non-zero exit."""
    try:
        return subprocess.run(["docker", *args], input=input_bytes, capture_output=True, timeout=timeout,
                              cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(["docker", *args], 124, b"", b"timeout")


def out_text(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or b"").decode("utf-8", "replace")


class Ctx:
    """Everything one run shares."""

    def __init__(self, args: argparse.Namespace, target: Target, numbers_only: bool) -> None:
        self.args = args
        self.target = target
        self.numbers_only = numbers_only
        self.report = Report(numbers_only)
        self.version: dict = {}
        self.offline: bool | None = None
        self.allowed = False
        self.lat = Latency()
        self.health_lat: list[float] = []
        self.searched: set[tuple[str, str]] = set()
        self.search_requests = 0
        self.search_calls = 0
        self.timings: list[tuple[str, float]] = []
        self.t0 = time.monotonic()
        self.started = now_iso()
        self.cleanups: list[tuple[str, Callable[[], None]]] = []
        self.watcher: JobWatcher | None = None
        self.mem: MemSampler | None = None
        self.memory: dict = {}
        self.docs: list[Doc] = []
        self.extra: dict[str, Doc] = {}
        self.json: dict = {"docs": {}, "steps": {}}
        self.lock = threading.Lock()
        self._clients: list[httpx.Client] = []
        self._local = threading.local()
        self.restart_info: dict = {}

    # ---- output
    def pub(self, text: str) -> None:
        """Print a line that may hold public document content (never in numbers-only mode)."""
        if not self.numbers_only:
            print(text, flush=True)

    def say(self, text: str) -> None:
        """Print a line built from fixed text, counts and timings (scrubbed of ids in numbers-only mode)."""
        print(lib.scrub(text) if self.numbers_only else text, flush=True)

    def ident(self, value: str | None, n: int = 12) -> str:
        return "<id>" if self.numbers_only else str(value or "")[:n]

    def elapsed(self) -> float:
        return time.monotonic() - self.t0

    # ---- guard
    def require(self, action: str) -> None:
        if not (self.offline is True or self.allowed):
            raise Refused(action)

    # ---- HTTP
    @property
    def http(self) -> httpx.Client:
        """One httpx client per thread."""
        client = getattr(self._local, "client", None)
        if client is None:
            client = httpx.Client(base_url=self.target.base, timeout=httpx.Timeout(TOOL_TIMEOUT, connect=15.0),
                                  trust_env=False)
            self._local.client = client
            with self.lock:
                self._clients.append(client)
        return client

    def reset_http(self) -> None:
        """Drop this thread's client (after a restart its pooled connections point at the old process)."""
        client = getattr(self._local, "client", None)
        if client is not None:
            self._local.client = None
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def close_clients(self) -> None:
        with self.lock:
            for c in self._clients:
                try:
                    c.close()
                except Exception:  # noqa: BLE001
                    pass
            self._clients.clear()

    def get_json(self, method: str, path: str, *, ok: tuple[int, ...] = (200,), **kw) -> tuple[int, Any]:
        r = self.http.request(method, path, **kw)
        try:
            body = r.json()
        except ValueError:
            body = None
        if r.status_code not in ok:
            raise HttpError(method, path[:80], r.status_code, body.get("type") if isinstance(body, dict) else None)
        return r.status_code, body

    def upload(self, d: Doc, data: bytes | None = None, *, reprocess: bool = False,
               password: str | None = None, filename: str | None = None) -> tuple[int, Any]:
        """POST /documents (multipart); returns (status, body) without raising on an HTTP error status."""
        self.require(f"upload {d.label}")
        form = {"reprocess": "true" if reprocess else "false"}
        if password is not None:
            form["password"] = password
        name = filename or ("document.pdf" if self.numbers_only else f"{d.name}.pdf")
        r = self.http.post("/documents", files={"file": (name, d.data if data is None else data,
                                                         "application/pdf")}, data=form)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None


def err_type(body: Any) -> str | None:
    return body.get("type") if isinstance(body, dict) else None


def wait_health(base: str, timeout: float = STARTUP_WAIT) -> float | None:
    """Seconds until GET /health answers 200, or None at the timeout."""
    t0 = time.monotonic()
    with httpx.Client(base_url=base, timeout=5.0, trust_env=False) as client:
        while time.monotonic() - t0 < timeout:
            try:
                if client.get("/health").status_code == 200:
                    return time.monotonic() - t0
            except httpx.HTTPError:
                pass
            time.sleep(1.0)
    return None


# ============================================================================ background: jobs and memory


class JobWatcher(threading.Thread):
    """Polls every tracked job until it is final; prints state / stage / progress changes (and every minute);
    keeps the transitions (elapsed seconds, state, stage, attempts)."""

    def __init__(self, ctx: Ctx) -> None:
        super().__init__(name="job-watcher", daemon=True)
        self.ctx = ctx
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.client = httpx.Client(base_url=ctx.target.base, timeout=15.0, trust_env=False)

    def track(self, job_id: str, label: str) -> None:
        with self.lock:
            self.jobs.setdefault(job_id, {"label": label, "job": None, "transitions": [], "key": None,
                                          "printed": 0.0, "final": False, "polled": 0.0})

    def run(self) -> None:
        while not self.stop_event.is_set():
            with self.lock:
                ids = [j for j, e in self.jobs.items() if not e["final"]]
            for job_id in ids:
                self.poll(job_id)
            self.stop_event.wait(POLL_SECONDS)
        self.client.close()

    def poll(self, job_id: str) -> dict | None:
        try:
            r = self.client.get(f"/jobs/{job_id}")
        except httpx.HTTPError:
            return None
        if r.status_code != 200:
            if r.status_code == 404:  # deleted with its document
                with self.lock:
                    e = self.jobs.get(job_id)
                    if e is not None:
                        e["final"] = True
                        e["deleted"] = True
            return None
        job = r.json()
        self._update(job_id, job)
        return job

    def _update(self, job_id: str, job: dict) -> None:
        t = time.monotonic()
        with self.lock:
            e = self.jobs[job_id]
            e["job"], e["polled"] = job, t
            prog = job.get("progress") or {}
            key = (job.get("state"), job.get("stage"), job.get("attempts"))
            if key != e["key"]:
                e["transitions"].append({"t": round(self.ctx.elapsed(), 1), "state": key[0], "stage": key[1],
                                         "attempts": key[2]})
            if key != e["key"] or (key[0] == "running" and t - e["printed"] >= PROGRESS_EVERY):
                p = f" {prog.get('done')}/{prog.get('total')}" if prog.get("total") else ""
                ident = "" if self.ctx.numbers_only else f" {job_id[:8]}"
                print(f"    [{self.ctx.elapsed():6.0f}s] {e['label']} job{ident}: {key[0]} {key[1] or '-'}{p} "
                      f"(attempt {key[2]})", flush=True)
                e["printed"] = t
            e["key"] = key
            if job.get("state") in FINAL:
                e["final"] = True

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            e = self.jobs.get(job_id)
            return dict(e["job"]) if e and e["job"] else None

    def transitions(self, job_id: str) -> list[dict]:
        with self.lock:
            return list((self.jobs.get(job_id) or {}).get("transitions") or [])

    def wait(self, job_id: str, timeout: float, pred: Callable[[dict], bool] | None = None) -> dict | None:
        """The job once pred(job) holds (default: final), polling directly when the cached state is old; None at
        the timeout or when the job was deleted."""
        pred = pred or (lambda j: j.get("state") in FINAL)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                e = self.jobs.get(job_id)
                job, polled, deleted = (e["job"], e["polled"], e.get("deleted")) if e else (None, 0.0, False)
            if deleted:
                return None
            if job is None or time.monotonic() - polled > 2 * POLL_SECONDS + 1:
                job = self.poll(job_id) or job
            if job is not None and pred(job):
                return job
            time.sleep(0.5)
        return None


class MemSampler(threading.Thread):
    """`docker stats --no-stream` every 5 s: the container's peak memory over the run (a restart does not reset it,
    unlike the cgroup's memory.peak)."""

    def __init__(self, container: str) -> None:
        super().__init__(name="mem-sampler", daemon=True)
        self.container = container
        self.peak: int | None = None
        self.samples = 0
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.is_set():
            proc = docker("stats", "--no-stream", "--format", "{{.MemUsage}}", self.container, timeout=30)
            used = lib.parse_mem_usage(out_text(proc).strip()) if proc.returncode == 0 else None
            if used is not None:
                self.samples += 1
                self.peak = max(self.peak or 0, used)
            self.stop_event.wait(5.0)


def cgroup_peak(container: str) -> int | None:
    proc = docker("exec", container, "cat", "/sys/fs/cgroup/memory.peak", timeout=30)
    text = out_text(proc).strip()
    return int(text) if proc.returncode == 0 and text.isdigit() else None


# ============================================================================ MCP and REST callers


@dataclass
class Call:
    tool: str
    args: dict
    ok: bool
    body: Any
    etype: str | None
    seconds: float
    text_ok: bool = True


class McpCaller:
    def __init__(self, ctx: Ctx, client, transport: str, phase: str) -> None:
        self.ctx, self.client, self.transport, self.phase = ctx, client, transport, phase

    async def __call__(self, doc_id: str, tool: str, args: dict) -> Call:
        t = time.monotonic()
        result = await self.client.call_tool(tool, {"doc_id": doc_id, **args})
        dt = time.monotonic() - t
        try:
            sc, text_ok = mcp_body(result)
        except (ValueError, TypeError, AttributeError, IndexError):
            sc, text_ok = result.structured_content, False
        ok = not result.is_error
        self.ctx.lat.add(self.phase, self.transport, tool, dt)
        return Call(tool, args, ok, sc, None if ok else err_type(sc), dt, text_ok)


class RestCaller:
    """The REST equivalent of the MCP tools (typed tool routes, GET /documents/{id}); one per thread."""

    transport = "rest"

    def __init__(self, ctx: Ctx, phase: str) -> None:
        self.ctx, self.phase = ctx, phase

    async def __call__(self, doc_id: str, tool: str, args: dict) -> Call:
        t = time.monotonic()
        if tool == "get_document":
            r = self.ctx.http.get(f"/documents/{doc_id}")
        else:
            r = self.ctx.http.post(f"/documents/{doc_id}/tools/{tool}", json=args)
        dt = time.monotonic() - t
        try:
            body = r.json()
        except ValueError:
            body = None
        self.ctx.lat.add(self.phase, "rest", tool, dt)
        ok = r.status_code == 200
        return Call(tool, args, ok, body, None if ok else (err_type(body) or f"HTTP {r.status_code}"), dt)


@asynccontextmanager
async def mcp_session(ctx: Ctx, transport: str):
    """An MCP client session over Streamable HTTP (<base>/mcp) or stdio (--stdio-cmd)."""
    from mcp import Client, StdioServerParameters, stdio_client

    if transport == "http":
        async with Client(f"{ctx.target.base}/mcp", mode="legacy", read_timeout_seconds=TOOL_TIMEOUT) as client:
            yield client
        return
    argv = ctx.target.stdio_cmd
    fd, log_path = tempfile.mkstemp(prefix="e2e_stdio_", suffix=".log")
    os.close(fd)
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as errlog:
            params = StdioServerParameters(command=argv[0], args=argv[1:], env=dict(os.environ), cwd=str(ROOT))
            async with Client(stdio_client(params, errlog=errlog), mode="legacy",
                              read_timeout_seconds=TOOL_TIMEOUT) as client:
                yield client
        errors = [line for line in Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()
                  if re.search(r"\b(ERROR|CRITICAL)\b", line)]
        if errors:
            ctx.say(f"    stdio server logged {len(errors)} error line(s)")
            for line in errors[-3:]:
                ctx.pub(f"      stderr: {line[:160]}")  # the server logs exception types and file:line only
    finally:
        try:
            os.unlink(log_path)
        except OSError:
            pass


# ============================================================================ the agent workflow


def tool_error_expected(ctx: Ctx, call: Call) -> bool:
    """search on an offline service answers 409 search_unavailable / 503 offline: expected."""
    return call.tool == "search" and ctx.offline and call.etype in ("search_unavailable", "offline")


async def agent_workflow(ctx: Ctx, caller, d: Doc) -> dict:
    """What an extraction agent does with one document (see the module docstring, step 3). Returns the calls and
    what the scoring needs; never raises on a tool error (it is recorded)."""
    calls: list[Call] = []
    doc_id = d.doc_id

    async def c(tool: str, args: dict) -> Call:
        r = await caller(doc_id, tool, args)
        calls.append(r)
        return r

    wf: dict[str, Any] = {"transport": caller.transport, "polls": 0, "reads": {}, "quotes": [], "search": []}
    t0 = time.monotonic()
    deadline = t0 + ctx.args.timeout_min * 60
    while True:  # 1. get_document until the result is published
        r = await c("get_document", {})
        wf["polls"] += 1
        status = (r.body or {}).get("status") if r.ok else None
        if r.ok and status in RESULT_STATES and r.body.get("has_result"):
            break
        if (r.ok and status in ("failed", "cancelled")) or time.monotonic() > deadline:
            wf["aborted"] = f"document {status}"
            wf["calls"] = calls
            return wf
        await anyio.sleep(POLL_SECONDS)
    info = await c("document_info", {})  # 2.
    wf["info"] = info.body if info.ok else {}
    for level in (1, 2):  # 3.
        o = await c("outline", {"max_level": level})
        wf[f"outline_{level}"] = (o.body or {}).get("count") if o.ok else None
    wf["find"] = {}
    for role in ROLES3:  # 4.
        f = await c("find_sections", {"role": role})
        wf["find"][role] = len((f.body or {}).get("sections") or []) if f.ok else None
    for role in ROLES3:  # 5. every part, following 'cursor'
        parts: list[dict] = []
        r = await c("read_section", {"section": role, "max_tokens": PART_TOKENS})
        while r.ok and len(parts) < 200:
            parts.append(r.body)
            cursor = r.body.get("cursor")
            if not cursor:
                break
            r = await c("read_section", {"cursor": cursor, "max_tokens": PART_TOKENS})
        spans = [(p.get("part") or {}) for p in parts]
        complete = r.ok and bool(parts) and not parts[-1].get("cursor") and spans[0].get("first_block") == 0 and all(
            a.get("end_block") == b.get("first_block") for a, b in zip(spans, spans[1:])) and \
            spans[-1].get("end_block") == spans[-1].get("total_blocks")
        wf["reads"][role] = {"parts": len(parts), "complete": complete, "error": None if r.ok else r.etype,
                             "tokens": sum(p.get("tokens") or 0 for p in parts),
                             "markdown": "\n\n".join(p.get("markdown") or "" for p in parts), "results": parts}
    soa = await c("get_soa", {})  # 6.
    wf["soa"] = soa.body if soa.ok else {}
    tids = list(dict.fromkeys(t for s in (wf["soa"].get("soas") or []) for t in (s.get("table_ids") or [])
                              if t))
    wf["tables"] = []
    for tid in tids:
        t = await c("get_table", {"table": tid, "format": "rows"})
        rows = sum(len(f.get("row_lines") or []) for f in (t.body or {}).get("fragments") or []) if t.ok else None
        wf["tables"].append({"table": tid, "ok": t.ok, "rows": rows})
    for q in SEARCH_QUERIES:  # 7.
        ctx.require(f"search on {d.label}")
        with ctx.lock:
            first = (doc_id, q) not in ctx.searched
            ctx.searched.add((doc_id, q))
            ctx.search_calls += 1
        s = await c("search", {"query": q})
        if s.ok and first:
            with ctx.lock:
                ctx.search_requests += 2  # query embedding + rerank (both cached afterwards)
        wf["search"].append({"ok": s.ok, "etype": s.etype, "blocks": len((s.body or {}).get("blocks") or [])})
    quotes: list[lib.Quote] = []  # 8. every value an agent would extract
    for role in ROLES3:
        for part in wf["reads"][role]["results"]:
            quotes += lib.section_quotes(part, role, tables=(role == "objectives_endpoints"))
    quotes += lib.visit_quotes(wf["soa"]) + lib.identifier_quotes(wf["info"])
    for q in quotes:
        args = {"quote": q.text, **({"section_id": q.section_id} if q.section_id else {})}
        r = await c("quote_check", args)
        wf["quotes"].append({"category": q.category, "verbatim": q.verbatim, "ok": r.ok,
                             "found": bool(r.ok and (r.body or {}).get("found")),
                             "match": (r.body or {}).get("match") if r.ok else r.etype,
                             "elsewhere": bool(r.ok and (r.body or {}).get("found_elsewhere")),
                             "text": q.text})
    wf["calls"] = calls
    wf["seconds"] = round(time.monotonic() - t0, 1)
    return wf


def run_agent(ctx: Ctx, d: Doc, transport: str, phase: str) -> dict:
    """agent_workflow over an MCP transport (http / stdio) or REST, in this thread's own event loop."""
    async def main() -> dict:
        if transport == "rest":
            return await agent_workflow(ctx, RestCaller(ctx, phase), d)
        async with mcp_session(ctx, transport) as client:
            return await agent_workflow(ctx, McpCaller(ctx, client, transport, phase), d)

    return anyio.run(main)


# ============================================================================ scoring


def folder_nct(name: str) -> str | None:
    m = re.search(r"NCT\d{8}", name)
    return m.group(0) if m else None


def pdf_text_has(pdf: Path, needle: str) -> bool:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    try:
        for i in range(len(doc)):
            page = doc[i]
            tp = page.get_textpage()
            text = tp.get_text_range()
            tp.close()
            page.close()
            if needle in text:
                return True
        return False
    finally:
        doc.close()


_SCORE_LOCK = threading.Lock()   # soa.compare_with_key swaps a usdm_gold function while it runs


def score_workflow(ctx: Ctx, d: Doc, wf: dict, step: str = "3", keyed: bool = True) -> dict:
    """Checks of one agent workflow (rows of `step`); returns the scores for the summary."""
    r = ctx.report
    n = f"{d.label} [{wf['transport']}]"
    calls: list[Call] = wf.get("calls") or []
    if wf.get("aborted"):
        r.add(step, f"{n}: agent workflow", FAIL, str(wf["aborted"]))
        return {}
    bad = [f"{c.tool}:{c.etype}" for c in calls if not c.ok and not tool_error_expected(ctx, c)]
    r.check(step, f"{n}: agent workflow without unexpected tool errors", not bad,
            f"{len(calls)} calls in {wf.get('seconds')}s, get_document polls {wf['polls']}"
            + (f"; errors {Counter(bad).most_common(5)}" if bad else ""))
    if wf["transport"] in ("http", "stdio"):
        mism = sum(not c.text_ok for c in calls)
        r.check(step, f"{n}: MCP text block = structuredContent", mism == 0, f"{len(calls) - mism}/{len(calls)}")
    info = wf.get("info") or {}
    scores: dict[str, Any] = {"transport": wf["transport"], "calls": len(calls), "seconds": wf.get("seconds"),
                              "sections": info.get("section_count"), "outline_1": wf.get("outline_1"),
                              "outline_2": wf.get("outline_2"), "find_sections": wf.get("find")}
    reads = wf["reads"]
    for role in ROLES3:
        rd = reads[role]
        r.check(step, f"{n}: read_section {role} complete through every cursor", rd["complete"],
                f"{rd['parts']} part(s), {rd['tokens']} tokens" + (f", error {rd['error']}" if rd["error"] else ""))
    scores["read_parts"] = {k: v["parts"] for k, v in reads.items()}
    soas = (wf.get("soa") or {}).get("soas") or []
    scores["soa"] = {"tables": len(soas), "visits": sum(len(s.get("visits") or []) for s in soas),
                     "activities": sum(len(s.get("activities") or []) for s in soas),
                     "get_table_ok": sum(t["ok"] for t in wf["tables"]), "get_table": len(wf["tables"]),
                     "table_rows": sum(t["rows"] or 0 for t in wf["tables"])}
    r.check(step, f"{n}: get_soa available, get_table (rows) of every SoA table",
            (wf.get("soa") or {}).get("available") is True and scores["soa"]["visits"] > 0
            and scores["soa"]["get_table_ok"] == len(wf["tables"]) and len(wf["tables"]) > 0,
            f"{len(soas)} SoA(s), {scores['soa']['visits']} visits, {scores['soa']['activities']} activities, "
            f"{scores['soa']['get_table_ok']}/{len(wf['tables'])} tables, {scores['soa']['table_rows']} rows")
    srch = wf["search"]
    if ctx.offline:
        ok = all(s["etype"] in ("search_unavailable", "offline") for s in srch)
        r.check(step, f"{n}: search offline -> 409 search_unavailable", ok,
                f"{[s['etype'] for s in srch]}")
    else:
        r.check(step, f"{n}: search answers with blocks", all(s["ok"] and s["blocks"] > 0 for s in srch),
                f"{[(s['etype'] or 'ok', s['blocks']) for s in srch]}")
    # quote_check
    qs = wf["quotes"]
    verb = [q for q in qs if q["verbatim"]]
    derived = [q for q in qs if not q["verbatim"]]
    found_v = sum(q["found"] for q in verb)
    per_cat = {}
    for q in verb:
        a = per_cat.setdefault(q["category"], [0, 0])
        a[0] += q["found"]
        a[1] += 1
    scores["quote_check"] = {"verbatim_found": found_v, "verbatim": len(verb),
                             "visit_labels_found": sum(q["found"] for q in derived), "visit_labels": len(derived),
                             "per_category": {k: f"{a}/{b}" for k, (a, b) in per_cat.items()}}
    missing = [q for q in verb if not q["found"]]
    r.check(step, f"{n}: quote_check of every value taken verbatim from a tool result -> found (100%)",
            bool(verb) and not missing,
            f"{found_v}/{len(verb)} ({', '.join(f'{k} {v}' for k, v in scores['quote_check']['per_category'].items())})")
    for q in missing[:5]:
        ctx.pub(f"      not found ({q['category']}, elsewhere {q['elsewhere']}): {q['text'][:100]!r}")
    scores["quote_check"]["header_cells_too_short"] = lib.short_header_cells(wf.get("soa") or {})
    ctx.say(f"    {d.label}: quote_check of SoA visit labels (composed by soa.py, reported only): "
            f"{scores['quote_check']['visit_labels_found']}/{len(derived)}; header cells too short to quote "
            f"(located by table, row and column): {scores['quote_check']['header_cells_too_short']}")
    if ctx.numbers_only or not keyed:
        return scores
    # identity
    ids = info.get("identifiers") or {}
    nct = folder_nct(d.name)
    try:
        import usdm_gold

        study = usdm_gold.load_study(d.name) if d.name in usdm_gold.key_paths() else None
    except Exception as exc:  # noqa: BLE001
        r.add(step, f"{n}: USDM key", SKIP, f"usdm_gold unavailable ({_where(exc)})")
        study = None
    got_nct = (ids.get("nct") or {}).get("value")
    if nct and d.pdf is not None:
        in_pdf = pdf_text_has(d.pdf, nct)
        if in_pdf:
            r.check(step, f"{n}: document_info nct = {nct}", got_nct == nct, f"got {got_nct}")
        else:
            r.check(step, f"{n}: document_info nct absent or {nct} (the PDF's text does not print it)",
                    got_nct in (None, nct), f"got {got_nct}")
    if study is not None:
        expected = lib.key_identifiers(study)
        cmp = lib.compare_identifiers(expected, info)
        scores["identifiers"] = {k: v[2] for k, v in cmp.items()}
        for key, (exp, got, equal) in cmp.items():
            r.check(step, f"{n}: identifier {key} = key {exp}", equal, f"got {got}")
        for cat in ("inclusion", "exclusion"):
            found = lib.criteria_found(study, cat, reads[cat]["markdown"])
            miss = [i for i, s in found if s < lib.CRITERION_MATCH]
            scores[f"{cat}_criteria"] = f"{len(found) - len(miss)}/{len(found)}"
            if found:
                r.check(step, f"{n}: every {cat} criterion of the USDM key found", not miss,
                        f"{len(found) - len(miss)}/{len(found)} (window >= {lib.CRITERION_MATCH}, lowest "
                        f"{min(s for _, s in found):.2f})" + (f"; missing ids {miss}" if miss else ""))
        md = reads["objectives_endpoints"]["markdown"]
        scores["objectives_found"] = "%d/%d" % lib.texts_found([o.text for o in study.of("Objective")], md)
        scores["endpoints_found"] = "%d/%d" % lib.texts_found([o.text for o in study.of("Endpoint")], md)
        try:
            import soa as soa_mod

            with _SCORE_LOCK:
                rep = soa_mod.compare_with_key(d.name, soas)
            key, cells = rep["key"], rep["cells"]
            n_enc = round(rep["encounter_recall"] * key["visits"])
            n_act = round(rep["activity_recall"] * key["activities"])
            scores["soa_vs_key"] = {"visits": f"{n_enc}/{key['visits']}", "activities": f"{n_act}/{key['activities']}",
                                    "encounter_recall": rep["encounter_recall"],
                                    "activity_recall": rep["activity_recall"], "cell_f1": cells["f1"],
                                    "cell_precision": cells["precision"], "cell_recall": cells["recall"]}
            r.check(step, f"{n}: SoA vs USDM key above the floors",
                    rep["encounter_recall"] >= ENC_RECALL_FLOOR and rep["activity_recall"] >= ACT_RECALL_FLOOR
                    and cells["f1"] >= CELL_F1_FLOOR,
                    f"visits {n_enc}/{key['visits']} ({rep['encounter_recall']}), activities {n_act}/"
                    f"{key['activities']} ({rep['activity_recall']}), cell F1 {cells['f1']}; floors "
                    f"{ENC_RECALL_FLOOR}/{ACT_RECALL_FLOOR}/{CELL_F1_FLOOR}")
        except Exception as exc:  # noqa: BLE001
            r.error(step, f"{n}: SoA vs USDM key", exc)
        ctx.say(f"    {d.label}: objectives {scores['objectives_found']}, endpoints {scores['endpoints_found']} "
                "of the key found in read_section('objectives_endpoints') (reported only)")
    elif nct:
        r.add(step, f"{n}: USDM key", SKIP, "no answer key: identity and self-consistency only")
    return scores


# ============================================================================ step 1: service


def step_service(ctx: Ctx) -> bool:
    r = ctx.report
    secs = wait_health(ctx.target.base, 180)
    if secs is None:
        r.add("1", "GET /health answers", FAIL, "no answer within 180 s")
        return False
    status, health = ctx.get_json("GET", "/health", ok=(200, 503))
    r.check("1", "GET /health ok", status == 200 and (health or {}).get("status") == "ok",
            f"HTTP {status}, worker {(health or {}).get('worker')}, jobs {(health or {}).get('jobs')}")
    _, version = ctx.get_json("GET", "/version")
    ctx.version = version or {}
    config = ctx.version.get("config") or {}
    ctx.offline = bool(ctx.version.get("offline"))
    r.check("1", "GET /version", bool(ctx.version.get("pipeline_version")) and bool(ctx.version.get("fingerprint")),
            f"pipeline {ctx.version.get('pipeline_version')}, fingerprint {ctx.version.get('fingerprint')}, offline "
            f"{ctx.offline}, max_upload_mb {config.get('max_upload_mb')}, figure model set "
            f"{bool(config.get('figure_model'))}")
    if not ctx.offline:
        r.check("1", "online service has an OpenRouter API key", bool(config.get("api_key_set")),
                f"api_key_set {config.get('api_key_set')}")
    return True


def step_host_origin(ctx: Ctx) -> None:
    r = ctx.report
    foreign = "e2e-foreign.invalid"
    port = urlsplit(ctx.target.base).port or 80
    for path, method in (("/health", "GET"), ("/mcp", "POST")):
        kw = {"json": {"jsonrpc": "2.0", "id": 1, "method": "ping"}} if method == "POST" else {}
        resp = ctx.http.request(method, path, headers={"Host": f"{foreign}:{port}"}, **kw)
        r.check("1", f"{method} {path} with a foreign Host -> 421 invalid_host",
                resp.status_code == 421 and err_type(_json(resp)) == "invalid_host",
                f"HTTP {resp.status_code} {err_type(_json(resp))}")
        resp = ctx.http.request(method, path, headers={"Origin": f"http://{foreign}"}, **kw)
        r.check("1", f"{method} {path} with a foreign Origin -> 403 invalid_origin",
                resp.status_code == 403 and err_type(_json(resp)) == "invalid_origin",
                f"HTTP {resp.status_code} {err_type(_json(resp))}")
    resp = ctx.http.get("/health", headers={"Origin": f"http://127.0.0.1:{port}"})
    r.check("1", "GET /health from an allowed Origin -> 200", resp.status_code == 200, f"HTTP {resp.status_code}")
    _, tools = ctx.get_json("GET", "/tools")
    names = sorted(t.get("name") for t in (tools or {}).get("tools") or [])
    r.check("1", "GET /tools lists the 9 protocol tools", len(names) == 9, f"{len(names)}")


def _json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def notice(ctx: Ctx, private: bool = False) -> str:
    config = ctx.version.get("config") or {}
    lines = [f"The service at {ctx.target.base} reports offline=false. This run makes it send to OpenRouter "
             "(zero-data-retention routing, no fallbacks):",
             f"  - every figure image not yet in its figure cache -> {config.get('figure_model') or '(figures off)'}",
             f"  - the text of every chunk not yet in its embedding cache, in batches of 32 -> "
             f"{config.get('embed_model')}",
             f"  - the search queries {list(SEARCH_QUERIES)} per document -> {config.get('embed_model')} (query) and "
             f"{config.get('rerank_model')} (with the candidate chunk texts)",
             "  - a made-up encrypted PDF: its invented bar chart (1 figure) and its 1-3 chunks; a made-up one-page "
             "PDF: its chunks",
             "  - the forced reprocess finds everything cached and sends nothing new; the drill container is offline"]
    if private:
        lines.insert(1, "  (the private PDF's figures and chunk texts, counts unknown here)")
        return "\n".join(lines)
    est, total = [], 0
    for d in ctx.docs:
        chunks_file = OUTPUT_PUBLIC / d.name / "chunks" / "chunks.jsonl"
        if chunks_file.is_file():
            chunks = [json.loads(x) for x in chunks_file.read_text(encoding="utf-8").splitlines() if x.strip()]
            figs = len({f.get("image_path") for c in chunks for f in c.get("figures") or []})
            emb = -(-len(chunks) // lib.EMBED_BATCH)
            total += figs + emb + 2 * len(SEARCH_QUERIES)
            est.append(f"{d.name}: <= {figs} figure, ~{emb} embedding, {2 * len(SEARCH_QUERIES)} search requests")
    lines.append("  roughly, when nothing is cached yet (from output_public): " + "; ".join(est)
                 + f"; about {total + 4} requests in all (a diagram figure adds a Mermaid request)")
    lines.append("  copies of these protocols already in the service are deleted first (their caches stay: figure "
                 "descriptions are reused, chunk embeddings are sent again)")
    return "\n".join(lines)


# ============================================================================ step 2: uploads


async def mcp_ingest(ctx: Ctx, transport: str, args: dict) -> tuple[bool, Any]:
    async with mcp_session(ctx, transport) as client:
        result = await client.call_tool("ingest_document", args)
        sc, _ = mcp_body(result)
        return not result.is_error, sc


def delete_existing(ctx: Ctx, d: Doc) -> None:
    """Delete a copy of this PDF already in the service (waiting for a running job of it first)."""
    status, _ = ctx.get_json("GET", f"/documents/{d.sha}", ok=(200, 404))
    if status == 404:
        return
    for _ in range(int(ctx.args.timeout_min * 6) + 1):
        resp = ctx.http.delete(f"/documents/{d.sha}")
        if resp.status_code == 200:
            ctx.say(f"    {d.label}: an existing copy was deleted first (caches kept)")
            return
        if resp.status_code == 409:
            time.sleep(10)
            continue
        raise HttpError("DELETE", f"/documents/{d.sha[:12]}", resp.status_code, err_type(_json(resp)))
    raise TimeoutError("an existing copy stayed busy")


def upload_one(ctx: Ctx, d: Doc) -> None:
    r = ctx.report
    t0 = time.monotonic()
    if d.upload == "rest":
        status, dec = ctx.upload(d)
        path = "REST multipart"
    elif d.upload == "mcp_base64":
        ctx.require(f"upload {d.label}")
        ok, dec = anyio.run(mcp_ingest, ctx, "http", {"data_base64": base64.b64encode(d.data).decode(),
                                                      "filename": f"{d.name}.pdf"})
        status = 202 if ok and (dec or {}).get("queued") else (200 if ok else 400)
        path = "MCP ingest_document data_base64 (HTTP)"
    else:
        ctx.require(f"upload {d.label}")
        proc = docker("cp", str(d.pdf), f"{ctx.target.container}:/data/inbox/{d.name}.pdf", timeout=300)
        if proc.returncode != 0:
            r.add("2", f"{d.label}: docker cp into the inbox", FAIL, f"exit {proc.returncode}")
            return
        ctx.cleanups.append((f"inbox file of {d.label}", partial(
            docker, "exec", ctx.target.container, "rm", "-f", f"/data/inbox/{d.name}.pdf")))
        ok, dec = anyio.run(mcp_ingest, ctx, "stdio", {"path": f"{d.name}.pdf"})
        status = 202 if ok and (dec or {}).get("queued") else (200 if ok else 400)
        path = "inbox (docker cp) + MCP ingest_document path (stdio)"
    dec = dec or {}
    d.doc_id = dec.get("doc_id")
    d.job_id = dec.get("job_id")
    r.check("2", f"{d.label}: upload by {path} -> 202 queued job", status == 202 and dec.get("queued") is True
            and is_job_id(d.job_id) and d.doc_id == d.sha and not dec.get("duplicate"),
            f"HTTP {status}, doc {ctx.ident(d.doc_id)}, job {ctx.ident(d.job_id, 8)}, "
            f"{time.monotonic() - t0:.1f}s" + (f", {err_type(dec)}" if err_type(dec) else ""))
    if is_job_id(d.job_id):
        ctx.watcher.track(d.job_id, d.label)


def finish_job(ctx: Ctx, d: Doc, job_id: str, label: str, step: str) -> dict | None:
    """Wait for a job of d; one row; returns the final job."""
    job = ctx.watcher.wait(job_id, ctx.args.timeout_min * 60)
    if job is None:
        ctx.report.add(step, f"{d.label}: {label} job finished", FAIL, "not final before --timeout-min (or deleted)")
        return None
    error = job.get("error") or {}
    ctx.report.check(step, f"{d.label}: {label} job finished", job.get("state") in RESULT_STATES,
                     f"{job.get('state')}, attempts {job.get('attempts')}"
                     + (f", error {error.get('type')} at {error.get('where')}" if error else ""))
    d.jobs.append((label, job))
    ctx.say(f"    {d.label} {label} stages: {stage_line(job)}")
    return job


def check_stages(ctx: Ctx, d: Doc, job: dict, step: str) -> None:
    stages = stage_map(job)
    fig, emb = stages.get("figures") or {}, stages.get("embed") or {}
    fd, ed = fig.get("detail") or {}, emb.get("detail") or {}
    ctx.say(f"    {d.label} figures: {fig.get('status')} ({detail_counts(fig) or 'no counts'}); embed: "
            f"{emb.get('status')} ({detail_counts(emb) or 'no counts'})")
    if ctx.offline:
        ctx.report.check(step, f"{d.label}: offline: figures not described, embed skipped_offline, status "
                               "done_with_warnings",
                         fig.get("status") in ("skipped_offline", "done", "skipped") and not fd.get("described_now")
                         and emb.get("status") == "skipped_offline" and job.get("state") == "done_with_warnings",
                         f"figures {fig.get('status')}, embed {emb.get('status')}, {job.get('state')}")
    else:
        figure_model = bool((ctx.version.get("config") or {}).get("figure_model"))
        ok_fig = (fig.get("status") == "done" and not fd.get("failed")) if figure_model else \
            fig.get("status") == "skipped"
        ctx.report.check(step, f"{d.label}: online: figures and embed stages done", ok_fig and emb.get("status") ==
                         "done", f"figures {fig.get('status')}, embed {emb.get('status')}, {job.get('state')}")


# ============================================================================ step 3: agent workflow per document


def workflow_step(ctx: Ctx, d: Doc, transport: str, *, step: str = "3", wait: bool = True) -> None:
    if wait and d.final_job is None:
        if not d.job_id:
            ctx.report.add(step, f"{d.label}: agent workflow", SKIP, "no job")
            return
        job = finish_job(ctx, d, d.job_id, "process", step="2")
        if job is None or job.get("state") not in RESULT_STATES:
            ctx.report.add(step, f"{d.label}: agent workflow", SKIP, "no result")
            return
        d.final_job = job
        check_stages(ctx, d, job, "2")
    print(f"\n[{step}] agent workflow on {d.label} over MCP {transport}  ({ctx.elapsed():.0f}s)", flush=True)
    t0 = time.monotonic()
    try:
        wf = run_agent(ctx, d, transport, "sequential")
    except Exception as exc:  # noqa: BLE001
        ctx.report.error(step, f"{d.label} [{transport}]: agent workflow", exc)
        return
    d.workflows.append(wf)
    try:
        d.scores[transport] = score_workflow(ctx, d, wf, step)
    except Exception as exc:  # noqa: BLE001
        ctx.report.error(step, f"{d.label} [{transport}]: scoring", exc)
    ctx.timings.append((f"3 {d.label} {transport}", time.monotonic() - t0))


# ============================================================================ step 4: several clients at once


def comparable(c: Call) -> bool:
    return c.tool != "get_document"


def call_key(doc_id: str, c: Call) -> str:
    return json.dumps([doc_id, c.tool, c.args], sort_keys=True, ensure_ascii=False)


def step_concurrency(ctx: Ctx, agents: list[tuple[Doc, str]], converting: Doc | None) -> None:
    r = ctx.report
    running_at_start = None
    if converting is not None and converting.job_id:
        job = ctx.watcher.wait(converting.job_id, ctx.args.timeout_min * 60,
                               lambda j: j.get("state") == "running" or j.get("state") in FINAL)
        running_at_start = bool(job and job.get("state") == "running")
    ctx.say(f"    while {converting.label if converting else '-'} converts (running at start {running_at_start}): "
            + ", ".join(f"agent {i + 1} {d.label} over MCP {t}" for i, (d, t) in enumerate(agents))
            + ", a REST client repeating their calls, /health every 0.5 s")
    results: dict[str, Any] = {}
    errors: dict[str, BaseException] = {}
    done = threading.Event()

    def agent(key: str, d: Doc, transport: str) -> None:
        try:
            results[key] = run_agent(ctx, d, transport, "concurrent")
        except BaseException as exc:  # noqa: BLE001
            errors[key] = exc

    def rest_client() -> None:
        try:
            for i, (d, _) in enumerate(agents):
                results[f"rest{i}"] = run_agent(ctx, d, "rest", "concurrent")
        except BaseException as exc:  # noqa: BLE001
            errors["rest"] = exc

    def health() -> None:
        with httpx.Client(base_url=ctx.target.base, timeout=30.0, trust_env=False) as client:
            while not done.is_set():
                t = time.monotonic()
                try:
                    ok = client.get("/health").status_code == 200
                except httpx.HTTPError:
                    ok = False
                dt = time.monotonic() - t
                ctx.health_lat.append(dt)
                if not ok:
                    errors.setdefault("health", RuntimeError("/health not 200"))
                done.wait(max(0.0, 0.5 - dt))

    threads = [threading.Thread(target=agent, args=(f"agent{i}", d, t), name=f"agent{i}")
               for i, (d, t) in enumerate(agents)]
    threads.append(threading.Thread(target=rest_client, name="rest-client"))
    hthread = threading.Thread(target=health, name="health")
    t0 = time.monotonic()
    hthread.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    done.set()
    hthread.join()
    wall = time.monotonic() - t0
    running_at_end = None
    if converting is not None and converting.job_id:
        job = ctx.watcher.get(converting.job_id)
        running_at_end = bool(job and job.get("state") == "running")
    ctx.say(f"    concurrency: {wall:.0f}s; {converting.label if converting else '-'} running at start "
            f"{running_at_start}, at end {running_at_end}")
    for key, exc in errors.items():
        r.error("4", f"concurrency: {key}", exc)
    total = equal = 0
    differ: list[str] = []
    for i, (d, transport) in enumerate(agents):
        wf, rest = results.get(f"agent{i}"), results.get(f"rest{i}")
        if not wf or not rest:
            r.add("4", f"{d.label}: MCP {transport} = REST for identical calls", FAIL, "a client did not finish")
            continue
        rest_by = {}
        for c in rest["calls"]:
            if comparable(c):
                rest_by.setdefault(call_key(d.doc_id, c), c)
        n_tot = n_eq = 0
        for c in wf["calls"]:
            if not comparable(c):
                continue
            other = rest_by.get(call_key(d.doc_id, c))
            if other is None:
                continue
            n_tot += 1
            same = (c.ok and other.ok and c.body == other.body) or (not c.ok and not other.ok and c.etype == other.etype)
            n_eq += same
            if not same:
                differ.append(c.tool)
        total += n_tot
        equal += n_eq
        r.check("4", f"{d.label}: MCP {transport} structuredContent = REST JSON for identical calls",
                n_tot > 0 and n_eq == n_tot, f"{n_eq}/{n_tot} equal" + (f"; differ {Counter(differ).most_common(4)}"
                                                                         if differ else ""))
        bad = [f"{c.tool}:{c.etype}" for w in (wf, rest) for c in w["calls"] if not c.ok
               and not tool_error_expected(ctx, c)]
        r.check("4", f"{d.label}: no tool error over MCP {transport} or REST", not bad,
                f"{len(wf['calls'])} + {len(rest['calls'])} calls" + (f"; {Counter(bad).most_common(4)}" if bad else ""))
        d.scores[f"concurrent_{transport}"] = {"calls": len(wf["calls"]), "seconds": wf.get("seconds"),
                                              "rest_calls": len(rest["calls"]), "rest_seconds": rest.get("seconds")}
    hs = lib.summarize(ctx.health_lat)
    r.check("4", f"/health during the concurrent load: max < {HEALTH_MAX} s", hs["n"] > 0 and hs["max"] < HEALTH_MAX
            and "health" not in errors, f"{hs['n']} polls, median {hs['median']}s, max {hs['max']}s")
    lat = ctx.lat.summary().get("concurrent", {})
    worst = []
    for transport, tools in lat.items():
        for tool, s in tools.items():
            if tool == "search":
                continue
            # a tool called once or twice per session is dominated by its first call (stdio: the service starting
            # in the new process; any transport: opening the document): only the max applies to it
            if s["max"] >= TOOL_MAX or (s["n"] >= MEDIAN_MIN_CALLS and s["median"] >= TOOL_MEDIAN):
                worst.append(f"{transport}/{tool} median {s['median']} max {s['max']}")
    allmax = max((s["max"] for t in lat.values() for k, s in t.items() if k != "search"), default=0.0)
    r.check("4", f"local tools under load: every call < {TOOL_MAX} s, median < {TOOL_MEDIAN} s per tool called "
                 f">= {MEDIAN_MIN_CALLS} times", not worst and bool(lat),
            f"slowest call {allmax:.2f}s" + (f"; over: {worst[:4]}" if worst else ""))
    ctx.json["steps"]["concurrency"] = {"wall_seconds": round(wall, 1), "running_at_start": running_at_start,
                                        "running_at_end": running_at_end, "compared_calls": total,
                                        "equal_calls": equal, "health": hs}


# ============================================================================ step 5: restart


class Restarter(threading.Thread):
    """Waits for the window (set by the main thread when no client session is open) and for a candidate job to be
    running (convert stage past its first batch when possible), then runs the restart command and waits for
    /health."""

    def __init__(self, ctx: Ctx, candidates: list[Doc]) -> None:
        super().__init__(name="restarter", daemon=True)
        self.ctx, self.candidates = ctx, candidates
        self.window = threading.Event()
        self.finished = threading.Event()
        self.result: dict = {}

    def run(self) -> None:
        try:
            self._run()
        except BaseException as exc:  # noqa: BLE001
            self.result["error"] = exc
        finally:
            self.finished.set()

    def _pick(self) -> tuple[Doc | None, dict | None]:
        deadline = time.monotonic() + self.ctx.args.timeout_min * 60
        while time.monotonic() < deadline:
            for d in self.candidates:
                if not d.job_id:
                    continue
                job = self.ctx.watcher.get(d.job_id)
                if job is None:
                    break  # not polled yet: wait for it
                if job.get("state") in FINAL:
                    continue
                if job.get("state") == "running":
                    prog = job.get("progress") or {}
                    started = (self.ctx.watcher.transitions(d.job_id) or [{}])
                    run_t = next((t["t"] for t in started if t["state"] == "running"), self.ctx.elapsed())
                    mid = job.get("stage") not in (None, "hash", "convert") or (prog.get("done") or 0) >= 1 \
                        or self.ctx.elapsed() - run_t > 120
                    if mid:
                        return d, job
                break  # the first unfinished candidate is the one to interrupt
            else:
                return None, None
            time.sleep(1.0)
        return None, None

    def _run(self) -> None:
        ctx = self.ctx
        self.window.wait()
        d, job = self._pick()
        if d is None:
            self.result["skip"] = "no candidate job was running"
            return
        enc = ctx.extra.get("enc")
        if enc is not None and enc.job_id and (ctx.watcher.get(enc.job_id) or {}).get("state") in ("queued",
                                                                                                    "running"):
            self.result["skip"] = "an encrypted-PDF job with a password is queued"
            return
        self.result.update(doc=d, job_before=job, peak_before=cgroup_peak(ctx.target.container))
        ctx.say(f"\n[5] restart while {d.label} is {job.get('state')} in stage {job.get('stage')} "
                f"({(job.get('progress') or {}).get('done')}/{(job.get('progress') or {}).get('total')}): "
                f"{' '.join(ctx.target.restart_cmd)}")
        t0 = time.monotonic()
        try:
            proc = subprocess.run(ctx.target.restart_cmd, capture_output=True, timeout=600, cwd=str(ROOT))
            self.result["exit"] = proc.returncode
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.result["exit"] = type(exc).__name__
        self.result["restart_seconds"] = time.monotonic() - t0
        self.result["health_seconds"] = wait_health(ctx.target.base)
        ctx.say(f"    restart command {self.result['restart_seconds']:.0f}s (exit {self.result['exit']}), healthy "
                f"after {self.result['health_seconds'] if self.result['health_seconds'] is None else round(self.result['health_seconds'])}s")


def check_restart(ctx: Ctx, rs: Restarter) -> Doc | None:
    """Right after the restart: the command, /health, and the interrupted job claimed again (attempts + 1).
    Returns the document whose final state check_restart_final checks later."""
    r = ctx.report
    ctx.reset_http()  # pooled connections of the old server process
    res = rs.result
    if "error" in res:
        r.error("5", "restart", res["error"])
        return None
    if "skip" in res:
        r.add("5", "restart in the middle of a conversion", SKIP, res["skip"])
        return None
    d: Doc = res["doc"]
    before = res["job_before"].get("attempts") or 1
    ctx.restart_info = {"doc": d.label, "stage": res["job_before"].get("stage"), "attempts_before": before,
                        "restart_seconds": round(res["restart_seconds"], 1),
                        "health_seconds": None if res["health_seconds"] is None else round(res["health_seconds"], 1),
                        "cgroup_peak_before": res.get("peak_before")}
    r.check("5", "restart command succeeded and /health answers again",
            res["exit"] == 0 and res["health_seconds"] is not None,
            f"exit {res['exit']}, restart {res['restart_seconds']:.0f}s, healthy after "
            f"{res['health_seconds'] if res['health_seconds'] is None else round(res['health_seconds'])}s")
    job = ctx.watcher.wait(d.job_id, 600, lambda j: (j.get("attempts") or 0) >= before + 1
                           or j.get("state") in FINAL)
    trans = [t["state"] for t in ctx.watcher.transitions(d.job_id)]
    r.check("5", f"{d.label}: the interrupted job was re-queued and started again (attempts >= 2)",
            job is not None and (job.get("attempts") or 0) >= before + 1,
            f"attempts {before} -> {(job or {}).get('attempts')}, now {(job or {}).get('state')}, states seen "
            f"{list(dict.fromkeys(trans))}")
    return d


def check_restart_final(ctx: Ctx, d: Doc | None) -> None:
    if d is None:
        return
    r = ctx.report
    job = ctx.watcher.wait(d.job_id, ctx.args.timeout_min * 60)
    if job is None:
        r.add("5", f"{d.label}: the interrupted job finished", FAIL, "not final")
        return
    r.check("5", f"{d.label}: the interrupted job finished done", job.get("state") in RESULT_STATES,
            f"{job.get('state')}, attempts {job.get('attempts')}")
    _, rec = ctx.get_json("GET", f"/documents/{d.doc_id}")
    r.check("5", f"{d.label}: its result is published", rec.get("has_result") is True
            and rec.get("result_job_id") == d.job_id,
            f"has_result {rec.get('has_result')}, result job is this job {rec.get('result_job_id') == d.job_id}, "
            f"status {rec.get('status')}")
    ctx.restart_info.update(attempts=job.get("attempts"), state=job.get("state"))


# ============================================================================ step 6: duplicate / version rules


def job_count(ctx: Ctx, doc_id: str) -> int:
    _, body = ctx.get_json("GET", f"/documents/{doc_id}/jobs")
    return len(body.get("jobs") or [])


def step_versions(ctx: Ctx, d: Doc) -> str | None:
    """Duplicate and version rules on a done document; returns the forced reprocess job id (queued)."""
    r = ctx.report
    before = job_count(ctx, d.doc_id)
    status, dec = ctx.upload(d)
    r.check("6", f"{d.label}: re-upload of a done document -> 200 duplicate, job_id null",
            status == 200 and (dec or {}).get("duplicate") is True and (dec or {}).get("job_id") is None
            and not (dec or {}).get("queued") and (dec or {}).get("stale") is False,
            f"HTTP {status}, duplicate {(dec or {}).get('duplicate')}, job {(dec or {}).get('job_id')}")
    status, dec = ctx.upload(d, reprocess=True)
    r.check("6", f"{d.label}: reprocess=true on a current result -> no job",
            status == 200 and (dec or {}).get("job_id") is None and not (dec or {}).get("queued"),
            f"HTTP {status}, stale {(dec or {}).get('stale')}, job {(dec or {}).get('job_id')}")
    after = job_count(ctx, d.doc_id)
    r.check("6", f"{d.label}: neither upload made a job", after == before, f"jobs {before} -> {after}")
    ctx.require(f"reprocess {d.label}")
    status, dec = ctx.get_json("POST", f"/documents/{d.doc_id}/reprocess", ok=(200, 202), params={"force": "true"})
    job_id = (dec or {}).get("job_id")
    if r.check("6", f"{d.label}: POST /documents/{{id}}/reprocess?force=true -> 202 job",
               status == 202 and (dec or {}).get("queued") is True and is_job_id(job_id),
               f"HTTP {status}, job {ctx.ident(job_id, 8)}"):
        ctx.watcher.track(job_id, f"{d.label} reprocess")
        return job_id
    return None


def check_reprocess(ctx: Ctx, d: Doc, job_id: str, old_result: str | None) -> None:
    r = ctx.report
    job = finish_job(ctx, d, job_id, "forced reprocess", step="6")
    if job is None:
        return
    _, rec = ctx.get_json("GET", f"/documents/{d.doc_id}")
    r.check("6", f"{d.label}: the reprocess published a new result", rec.get("result_job_id") == job_id
            and job_id != old_result, f"result job changed {rec.get('result_job_id') != old_result}")
    stages = stage_map(job)
    fd = (stages.get("figures") or {}).get("detail") or {}
    ed = (stages.get("embed") or {}).get("detail") or {}
    if ctx.offline:
        r.add("6", f"{d.label}: the reprocess sent nothing new to OpenRouter", SKIP,
              f"offline: figures {(stages.get('figures') or {}).get('status')}, embed "
              f"{(stages.get('embed') or {}).get('status')}")
        return
    r.check("6", f"{d.label}: the reprocess sent nothing new (figures described_now 0, embed embedded 0)",
            not fd.get("described_now") and not fd.get("failed") and (stages.get("embed") or {}).get("status") ==
            "done" and ed.get("embedded") == 0,
            f"described_now {fd.get('described_now', 0)}, already_cached {fd.get('already_cached', 0)}, embedded "
            f"{ed.get('embedded')}, cached {ed.get('cached')}")


# ============================================================================ step 7: failure cases


def raw_post_content_length(base: str, length: int) -> tuple[int | None, str | None]:
    """POST /documents announcing a body of `length` bytes and sending none: the upload route answers from the
    headers alone. (status, error type)."""
    u = urlsplit(base)
    with socket.create_connection((u.hostname, u.port or 80), timeout=30) as s:
        s.sendall((f"POST /documents HTTP/1.1\r\nHost: {u.hostname}:{u.port or 80}\r\n"
                   "Content-Type: multipart/form-data; boundary=e2eboundary\r\n"
                   f"Content-Length: {length}\r\nConnection: close\r\n\r\n").encode())
        data = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        except OSError:
            pass
    head, _, body = data.partition(b"\r\n\r\n")
    m = re.match(rb"HTTP/1\.[01] (\d{3})", head)
    try:
        etype = json.loads(body.decode("utf-8", "replace")).get("type")
    except (ValueError, AttributeError):
        etype = None
        m2 = re.search(rb'"type"\s*:\s*"([a-z_]+)"', body)
        etype = m2.group(1).decode() if m2 else None
    return (int(m.group(1)) if m else None), etype


def streamed_oversize(ctx: Ctx, limit: int) -> tuple[int | None, str | None, str | None]:
    """POST /documents with a chunked body (no Content-Length) 1 MiB beyond the limit: (status, type, exception)."""
    boundary = "e2eoversize"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.pdf\"\r\n"
            "Content-Type: application/pdf\r\n\r\n%PDF-1.4\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    total = limit + (1 << 20)
    block = b"0" * (1 << 20)

    def body():
        yield head
        sent = len(head)
        while sent < total:
            n = min(len(block), total - sent)
            yield block[:n]
            sent += n
        yield tail

    try:
        with httpx.Client(base_url=ctx.target.base, timeout=120.0, trust_env=False) as client:
            resp = client.post("/documents", content=body(),
                               headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
            return resp.status_code, err_type(_json(resp)), None
    except httpx.HTTPError as exc:
        return None, None, type(exc).__name__


def step_failures_upload(ctx: Ctx, enc: Doc) -> None:
    """Rejected uploads (no job) and the encrypted PDF with its password (a queued job)."""
    r = ctx.report
    src = ctx.docs[0].data if ctx.docs and ctx.docs[0].pdf is not None else lib.make_pdf(
        lib.synthetic_protocol("TRUNC-01"))
    corrupt = Doc("corrupt", "corrupt PDF", lib.truncated(src, 0.4))
    status, body = ctx.upload(corrupt, filename="corrupt.pdf")
    r.check("7", "truncated PDF -> 400 invalid_pdf", status == 400 and err_type(body) == "invalid_pdf",
            f"HTTP {status} {err_type(body)}, {len(corrupt.data) // 1024} KiB")
    text = Doc("text", "text file", lib.NOT_A_PDF)
    status, body = ctx.upload(text, filename="notes.pdf")
    r.check("7", "a text file -> 415 not_pdf", status == 415 and err_type(body) == "not_pdf",
            f"HTTP {status} {err_type(body)}")
    status, body = ctx.upload(enc, filename="encrypted.pdf")
    r.check("7", "encrypted PDF without a password -> 400 password_required",
            status == 400 and err_type(body) == "password_required", f"HTTP {status} {err_type(body)}")
    status, body = ctx.upload(enc, password="not-the-password", filename="encrypted.pdf")
    r.check("7", "encrypted PDF with a wrong password -> 400 bad_password",
            status == 400 and err_type(body) == "bad_password", f"HTTP {status} {err_type(body)}")
    max_mb = int((ctx.version.get("config") or {}).get("max_upload_mb") or 100)
    limit = max_mb * 1024 * 1024 + 64 * 1024  # service.api: MAX_UPLOAD_MB plus the multipart allowance
    status, etype = raw_post_content_length(ctx.target.base, limit + 1)
    r.check("7", f"upload announcing > MAX_UPLOAD_MB ({max_mb} MiB) -> 413 too_large",
            status == 413 and etype == "too_large", f"HTTP {status} {etype}")
    t0 = time.monotonic()
    status, etype, exc = streamed_oversize(ctx, limit)
    r.check("7", "streamed upload beyond MAX_UPLOAD_MB (chunked, no Content-Length) -> 413 too_large",
            status == 413 and etype == "too_large", f"HTTP {status} {etype}"
            + (f", {exc}" if exc else "") + f", {time.monotonic() - t0:.1f}s")
    status, body = ctx.upload(enc, password=E_PASSWORD, filename="encrypted.pdf")
    enc.doc_id, enc.job_id = (body or {}).get("doc_id"), (body or {}).get("job_id")
    if r.check("7", "encrypted PDF with the right password -> 202 queued", status == 202 and is_job_id(enc.job_id)
               and enc.doc_id == enc.sha, f"HTTP {status} {err_type(body) or ''}, job {ctx.ident(enc.job_id, 8)}"):
        ctx.watcher.track(enc.job_id, enc.label)


def running_job(ctx: Ctx, docs: list[Doc]) -> Doc | None:
    for d in docs:
        if d.job_id and (ctx.watcher.get(d.job_id) or {}).get("state") == "running":
            return d
    return None


def step_failures_jobs(ctx: Ctx, q: Doc, candidates: list[Doc]) -> None:
    """With a job running: delete its document -> 409, cancel it -> 409; a job queued behind it cancelled."""
    r = ctx.report
    d = running_job(ctx, candidates)
    if d is None:
        r.add("7", "delete / cancel while a job runs", SKIP, "no job of this run was running")
    else:
        resp = ctx.http.delete(f"/documents/{d.doc_id}")
        r.check("7", f"DELETE {d.label} while its job runs -> 409 document_busy",
                resp.status_code == 409 and err_type(_json(resp)) == "document_busy",
                f"HTTP {resp.status_code} {err_type(_json(resp))}")
        resp = ctx.http.post(f"/jobs/{d.job_id}/cancel")
        r.check("7", f"cancel the running job of {d.label} -> 409 not_cancellable",
                resp.status_code == 409 and err_type(_json(resp)) == "not_cancellable",
                f"HTTP {resp.status_code} {err_type(_json(resp))}")
    status, body = ctx.upload(q, filename="queued.pdf")
    q.doc_id, job_id = (body or {}).get("doc_id"), (body or {}).get("job_id")
    if not (status == 202 and is_job_id(job_id)):
        r.add("7", "a second made-up PDF queued", FAIL, f"HTTP {status} {err_type(body)}")
        return
    resp = ctx.http.post(f"/jobs/{job_id}/cancel")
    state = (_json(resp) or {}).get("state")
    r.check("7", "cancel a queued job -> cancelled", resp.status_code == 200 and state == "cancelled",
            f"HTTP {resp.status_code}, state {state}, queued behind a running job {d is not None}")
    _, rec = ctx.get_json("GET", f"/documents/{q.doc_id}")
    r.check("7", "the cancelled document has no result and status cancelled",
            rec.get("status") == "cancelled" and rec.get("has_result") is False,
            f"status {rec.get('status')}, has_result {rec.get('has_result')}")


def batch_listing(ctx: Ctx) -> dict[str, int] | None:
    """{pdf hash prefix: batch files} of /data/cache/batches, and "VLM": figure-description files."""
    script = ('cd /data/cache/batches 2>/dev/null && for d in */; do [ -d "$d" ] && echo "B ${d%/} '
              '$(find "$d" -name "*.json" ! -name manifest.json | wc -l)"; done; '
              'echo "V $(ls /data/cache/vlm 2>/dev/null | grep -c "\\.json$")"')
    proc = docker("exec", ctx.target.container, "sh", "-c", script, timeout=60)
    if proc.returncode != 0:
        return None
    out: dict[str, int] = {}
    for line in out_text(proc).splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0] == "B" and parts[2].isdigit():
            out[parts[1]] = int(parts[2])
        elif len(parts) == 2 and parts[0] == "V" and parts[1].isdigit():
            out["VLM"] = int(parts[1])
    return out


def step_delete_done(ctx: Ctx, q: Doc) -> None:
    """Re-upload the cancelled made-up PDF, wait for it, delete it: deleted, 404s, conversion batches kept."""
    r = ctx.report
    status, body = ctx.upload(q, filename="queued.pdf")
    q.job_id = (body or {}).get("job_id")
    if not r.check("7", "the cancelled document uploaded again -> 202 new job", status == 202 and
                   is_job_id(q.job_id), f"HTTP {status} {err_type(body) or ''}"):
        return
    ctx.watcher.track(q.job_id, q.label)
    job = finish_job(ctx, q, q.job_id, "process", step="7")
    if job is None or job.get("state") not in RESULT_STATES:
        return
    resp = ctx.http.delete(f"/documents/{q.doc_id}")
    body = _json(resp) or {}
    r.check("7", "DELETE a done document -> deleted (no 'purged' without the flag)",
            resp.status_code == 200 and body.get("deleted") is True and "purged" not in body,
            f"HTTP {resp.status_code}, deleted {body.get('deleted')}")
    codes = [ctx.http.get(p).status_code for p in (f"/documents/{q.doc_id}", f"/documents/{q.doc_id}/files",
                                                   f"/documents/{q.doc_id}/files/chunks.jsonl",
                                                   f"/jobs/{q.job_id}", f"/documents/{q.doc_id}/jobs")]
    r.check("7", "the deleted document, its files, its job and its job list -> 404", codes == [404] * 5, f"{codes}")
    listing = batch_listing(ctx)
    r.check("7", "a plain delete keeps the conversion batches (shared cache)",
            listing is not None and listing.get(q.doc_id[:16], 0) > 0,
            f"{(listing or {}).get(q.doc_id[:16], 0)} batch file(s) of it")


# ============================================================================ step 8: purge and drill


def seed_fake_figures(ctx: Ctx, enc: Doc) -> tuple[int, str | None]:
    """Offline only: two placeholder figure-cache entries, one with the encrypted document's figure key (as the
    service computes it from its stored protocol.json) and one with another key. (seeded count, other file)."""
    code = ("import json,sys;from docling_core.types.doc import DoclingDocument;from figure_vlm import "
            "document_key;print(document_key(DoclingDocument.load_from_json(sys.argv[1])))")
    proc = docker("exec", ctx.target.container, "python", "-c", code,
                  f"/data/documents/{enc.doc_id}/protocol.json", timeout=120)
    key = out_text(proc).strip().splitlines()[-1] if proc.returncode == 0 and out_text(proc).strip() else None
    if not key:
        return 0, None
    seeded, other = 0, f"e2e_placeholder_other_{enc.doc_id[:8]}.json"
    for fname, doc_key in ((f"e2e_placeholder_{enc.doc_id[:8]}.json", key), (other, "e2e-not-this-document")):
        entry = {"model": "e2e-placeholder", "prompt_version": "e2e", "doc_key": doc_key, "page": 2,
                 "bbox": [0, 0, 1, 1], "description": "placeholder written by scripts/e2e_test.py"}
        proc = docker("exec", "-i", ctx.target.container, "sh", "-c", f"cat > /data/cache/vlm/{fname}",
                      input_bytes=json.dumps(entry).encode(), timeout=60)
        seeded += proc.returncode == 0
    ctx.cleanups.append(("placeholder figure entry", partial(docker, "exec", ctx.target.container, "rm", "-f",
                                                             f"/data/cache/vlm/{other}")))
    return seeded, other


def purge_target(ctx: Ctx, enc: Doc, q: Doc) -> Doc | None:
    """The made-up document of the purge check: the encrypted one (it has a figure), or, when it has no result, the
    one-page made-up PDF uploaded once more (its batches are cached: a short job)."""
    if enc.doc_id and enc.final_job and enc.final_job.get("state") in RESULT_STATES:
        return enc
    ctx.say("    the encrypted document has no result: the purge is checked on the one-page made-up PDF instead")
    status, body = ctx.upload(q, filename="queued.pdf")
    q.job_id = (body or {}).get("job_id")
    if status != 202 or not is_job_id(q.job_id):
        ctx.report.add("8", "made-up PDF for the purge check uploaded", FAIL, f"HTTP {status} {err_type(body)}")
        return None
    ctx.watcher.track(q.job_id, q.label)
    q.final_job = finish_job(ctx, q, q.job_id, "process (for purge)", step="8")
    return q if q.final_job and q.final_job.get("state") in RESULT_STATES else None


def step_purge(ctx: Ctx, enc: Doc | None) -> None:
    r = ctx.report
    if enc is None:
        r.add("8", "delete with purge_caches", SKIP, "no made-up document with a result")
        return
    expected_figures = None
    if ctx.offline:
        seeded, _ = seed_fake_figures(ctx, enc)
        expected_figures = 1 if seeded == 2 else None
        ctx.say(f"    offline: {seeded} placeholder figure entries seeded (this document's key + another)")
    else:
        fd = (stage_map(enc.final_job).get("figures") or {}).get("detail") or {}
        expected_figures = int(fd.get("described_now") or 0) + int(fd.get("already_cached") or 0)
    before = batch_listing(ctx)
    sha16 = enc.doc_id[:16]
    resp = ctx.http.delete(f"/documents/{enc.doc_id}", params={"purge_caches": "true"})
    body = _json(resp) or {}
    purged = body.get("purged")
    if not isinstance(purged, dict):
        r.add("8", "DELETE ?purge_caches=true -> deleted with purged counts", FAIL,
              f"HTTP {resp.status_code}, deleted {body.get('deleted')}: service lacks purge_caches")
        return
    r.check("8", "DELETE ?purge_caches=true -> deleted with purged counts",
            resp.status_code == 200 and body.get("deleted") is True,
            f"purged batches {purged.get('batches')}, figures {purged.get('figures')}")
    after = batch_listing(ctx)
    if before is None or after is None:
        r.add("8", "cache listing (docker exec)", FAIL, "docker exec failed")
        return
    r.check("8", "the document's conversion batches are gone",
            before.get(sha16, 0) > 0 and sha16 not in after and purged.get("batches") == before.get(sha16, -1),
            f"before {before.get(sha16, 0)} file(s), purged {purged.get('batches')}, after {after.get(sha16, 0)}")
    others_b = {k: v for k, v in before.items() if k not in (sha16, "VLM")}
    others_a = {k: v for k, v in after.items() if k not in (sha16, "VLM")}
    r.check("8", "every other document's batch cache is intact", others_b == others_a,
            f"{len(others_a)} other document(s), {sum(others_a.values())} batch files")
    r.check("8", "the document's figure descriptions are gone, the others stay",
            after.get("VLM") == before.get("VLM", 0) - purged.get("figures", -1)
            and (expected_figures is None or purged.get("figures") == expected_figures),
            f"figure entries {before.get('VLM')} -> {after.get('VLM')}, purged {purged.get('figures')}, expected "
            f"{expected_figures}")
    codes = [ctx.http.get(p).status_code for p in (f"/documents/{enc.doc_id}", f"/documents/{enc.doc_id}/files")]
    r.check("8", "the purged document -> 404", codes == [404, 404], f"{codes}")
    ctx.json["steps"]["purge"] = {"purged": purged, "expected_figures": expected_figures}


def labelled(kind: str, name: str) -> bool | None:
    """True: exists with our label; False: exists without it; None: does not exist."""
    fmt = "{{index .Config.Labels \"%s\"}}" % LABEL if kind == "container" else "{{index .Labels \"%s\"}}" % LABEL
    proc = docker(kind, "inspect", "-f", fmt, name, timeout=30)
    if proc.returncode != 0:
        return None
    return out_text(proc).strip() not in ("", "<no value>")


def remove_ours(kind: str, name: str) -> bool:
    """Remove a leftover container / volume of an earlier run (our label only). False: a foreign one exists."""
    state = labelled(kind, name)
    if state is None:
        return True
    if state is False:
        return False
    docker(*(["rm", "-f", name] if kind == "container" else ["volume", "rm", "-f", name]), timeout=120)
    return True


def step_drill(ctx: Ctx, drill_doc: Doc) -> None:
    r = ctx.report
    t = ctx.target
    name, vol = f"{t.container}-drill", f"{t.container}-drill-data"
    if not t.volume or not t.image:
        r.add("8", "backup / restore drill", SKIP, "volume or image of the target container unknown")
        return
    if not remove_ours("container", name) or not remove_ours("volume", vol):
        r.add("8", "backup / restore drill", SKIP, f"a container or volume named {name} exists (not ours)")
        return
    # quiet service: no job queued or running (a running job would be re-queued in the throwaway container)
    deadline = time.monotonic() + ctx.args.timeout_min * 60
    while time.monotonic() < deadline:
        _, health = ctx.get_json("GET", "/health", ok=(200, 503))
        jobs = (health or {}).get("jobs") or {}
        if not jobs.get("queued") and not jobs.get("running"):
            break
        time.sleep(5)
    else:
        r.add("8", "backup / restore drill", SKIP, "the service never became idle")
        return
    _, listing = ctx.get_json("GET", "/documents", params={"limit": 1000})
    source_docs = (listing or {}).get("documents") or []
    archive_dir = Path(tempfile.mkdtemp(prefix="e2e_drill_"))
    ctx.cleanups.append(("drill archive", partial(shutil.rmtree, archive_dir, True)))
    ctx.cleanups.append(("drill volume", partial(docker, "volume", "rm", "-f", vol)))
    ctx.cleanups.append(("drill container", partial(docker, "rm", "-f", name)))
    t0 = time.monotonic()
    bind = f"type=bind,source={archive_dir},target=/backup"
    proc = docker("run", "--rm", "--user", "0:0", "--entrypoint", "tar", "-v", f"{t.volume}:/data:ro", "--mount",
                  bind, t.image, "-czf", "/backup/drill.tgz", "-C", "/data", ".", timeout=1800)
    archive = archive_dir / "drill.tgz"
    size = archive.stat().st_size if archive.is_file() else 0
    if not r.check("8", "read-only tar of the volume into a scratch folder", proc.returncode == 0 and size > 0,
                   f"exit {proc.returncode}, {size / 1024 ** 2:.0f} MiB, {time.monotonic() - t0:.0f}s"):
        return
    proc = docker("volume", "create", "--label", f"{LABEL}=drill", vol, timeout=60)
    proc2 = docker("run", "--rm", "--user", "0:0", "--entrypoint", "tar", "-v", f"{vol}:/data", "--mount",
                   bind + ",readonly", t.image, "-xzf", "/backup/drill.tgz", "-C", "/data", timeout=1800)
    if not r.check("8", "restore into a new scratch volume", proc.returncode == 0 and proc2.returncode == 0,
                   f"exit {proc.returncode}/{proc2.returncode}"):
        return
    extra = ["-v", f"{ROOT / 'service'}:/app/service:ro"] if ctx.args.mount_service_code else []
    proc = docker("run", "-d", "--name", name, "--label", f"{LABEL}=drill", "-p",
                  f"127.0.0.1:{t.drill_port}:8000", *HARDENING, "-e", "OPENROUTER_OFFLINE=1", "-e", "FIGURE_MODEL=",
                  "-v", f"{vol}:/data", *extra, t.image, timeout=120)
    if not r.check("8", f"throwaway container {name} started (offline, FIGURE_MODEL empty)", proc.returncode == 0,
                   f"exit {proc.returncode}, port {t.drill_port}"):
        return
    base = f"http://127.0.0.1:{t.drill_port}"
    secs = wait_health(base)
    if not r.check("8", "the drill container answers /health", secs is not None, f"after {secs and round(secs)}s"):
        return
    code = "import sqlite3;print(sqlite3.connect('file:/data/registry.sqlite?mode=ro',uri=True).execute(" \
           "'pragma integrity_check').fetchone()[0])"
    proc = docker("exec", name, "python", "-c", code, timeout=120)
    integrity = out_text(proc).strip()
    r.check("8", "restored registry: integrity_check ok", proc.returncode == 0 and integrity == "ok",
            f"exit {proc.returncode}, {integrity[:20]}")
    proc = docker("exec", name, "stat", "-c", "%u", "/data/documents", "/data/registry.sqlite", timeout=60)
    owners = out_text(proc).split()
    r.check("8", "restored files owned by uid 10001", proc.returncode == 0 and set(owners) == {"10001"},
            f"{owners}")
    with httpx.Client(base_url=base, timeout=httpx.Timeout(TOOL_TIMEOUT, connect=15), trust_env=False) as client:
        docs = client.get("/documents", params={"limit": 1000}).json().get("documents") or []
        r.check("8", "restored document count = source", len(docs) == len(source_docs),
                f"{len(docs)} vs {len(source_docs)}")
        with_result = [x for x in docs if x.get("has_result")]
        stale = sum(x.get("stale") is True for x in with_result)
        r.check("8", "every restored result is stale (another fingerprint)", stale == len(with_result),
                f"{stale}/{len(with_result)}")
        ver = client.get("/version").json()
        r.check("8", "the drill container is offline", ver.get("offline") is True, f"offline {ver.get('offline')}")
        name_pdf = "document.pdf" if ctx.numbers_only else f"{drill_doc.name}.pdf"
        resp = client.post("/documents", files={"file": (name_pdf, drill_doc.data, "application/pdf")},
                           data={"reprocess": "false"})
        dec = _json(resp) or {}
        r.check("8", f"upload of an existing PDF ({drill_doc.label}) -> stale, no job",
                resp.status_code == 200 and dec.get("stale") is True and dec.get("job_id") is None,
                f"HTTP {resp.status_code}, stale {dec.get('stale')}, job {dec.get('job_id')}")
        resp = client.post("/documents", files={"file": (name_pdf, drill_doc.data, "application/pdf")},
                           data={"reprocess": "true"})
        dec = _json(resp) or {}
        job_id = dec.get("job_id")
        if not r.check("8", "reprocess=true -> 202 queued", resp.status_code == 202 and is_job_id(job_id),
                       f"HTTP {resp.status_code}"):
            return
        t1 = time.monotonic()
        job = None
        while time.monotonic() - t1 < ctx.args.timeout_min * 60:
            job = client.get(f"/jobs/{job_id}").json()
            if job.get("state") in FINAL:
                break
            time.sleep(POLL_SECONDS)
        stages = stage_map(job)
        conv = (stages.get("convert") or {}).get("seconds")
        r.check("8", "the reprocess finished offline from the restored caches",
                (job or {}).get("state") == "done_with_warnings"
                and (stages.get("figures") or {}).get("status") == "skipped"
                and (stages.get("embed") or {}).get("status") == "skipped_offline",
                f"{(job or {}).get('state')} in {time.monotonic() - t1:.0f}s, convert {conv}s, figures "
                f"{(stages.get('figures') or {}).get('status')}, embed {(stages.get('embed') or {}).get('status')}")
        rec = client.get(f"/documents/{drill_doc.sha}").json()
        r.check("8", "the reprocessed result is current (stale false)", rec.get("stale") is False
                and rec.get("result_job_id") == job_id, f"stale {rec.get('stale')}")
    ctx.json["steps"]["drill"] = {"archive_mib": round(size / 1024 ** 2, 1), "documents": len(source_docs),
                                  "reprocess_stages": lib.stage_seconds(job)}


# ============================================================================ step 9/10: summary


def summary(ctx: Ctx) -> None:
    r = ctx.report
    print("\n" + "=" * 110)
    print(f"{'step':<5} {'result':<6} check")
    for row in r.rows:
        detail = f"  [{row['detail']}]" if row["detail"] and row["status"] != PASS else ""
        print(f"{row['step']:<5} {row['status']:<6} {row['name']}{detail}")
    counts = Counter(row["status"] for row in r.rows)
    print("-" * 110)
    print(f"{counts[PASS]} passed, {counts[FAIL]} failed, {counts[SKIP]} skipped")
    print("\nPer-stage seconds (jobs of this run):")
    docs = ctx.docs + list(ctx.extra.values())
    for d in docs:
        for label, job in d.jobs:
            secs = lib.stage_seconds(job)
            print(f"  {d.label:<22} {label:<19} {str(job.get('state')):<19} attempts {job.get('attempts')}  "
                  + " ".join(f"{k} {v:.0f}" for k, v in secs.items()) + f"  (total {sum(secs.values()):.0f}s)")
    print("\nScores:")
    for d in ctx.docs:
        for transport, s in d.scores.items():
            if transport.startswith("concurrent"):
                continue
            q = s.get("quote_check") or {}
            parts = [f"quote_check verbatim {q.get('verbatim_found')}/{q.get('verbatim')}",
                     f"visit labels {q.get('visit_labels_found')}/{q.get('visit_labels')}"]
            for k in ("inclusion_criteria", "exclusion_criteria", "objectives_found", "endpoints_found"):
                if k in s:
                    parts.append(f"{k.replace('_found', '').replace('_', ' ')} {s[k]}")
            if "soa_vs_key" in s:
                sv = s["soa_vs_key"]
                parts.append(f"SoA visits {sv['visits']}, activities {sv['activities']}, cell F1 {sv['cell_f1']}")
            if "identifiers" in s:
                parts.append(f"identifiers equal {sum(s['identifiers'].values())}/{len(s['identifiers'])}")
            soa = s.get("soa") or {}
            parts.append(f"SoA {soa.get('tables')} table(s), {soa.get('visits')} visits, {soa.get('activities')} "
                         f"activities; {s.get('calls')} calls in {s.get('seconds')}s")
            print(f"  {d.label} [{transport}]: " + "; ".join(parts))
    lat = ctx.lat.summary()
    print("\nTool latency (seconds, median / max, n):")
    for phase, transports in lat.items():
        for transport, tools in transports.items():
            print(f"  {phase:<10} {transport:<6} " + ", ".join(f"{k} {v['median']}/{v['max']} ({v['n']})"
                                                         for k, v in tools.items()))
    hs = lib.summarize(ctx.health_lat)
    print(f"  /health under load: median {hs['median']}s, max {hs['max']}s ({hs['n']} polls)")
    print("\nOpenRouter requests (DERIVED from job details and this run's search calls; retries excluded):")
    orq: dict[str, Any] = {}
    if ctx.offline:
        print("  none: the service is offline")
    else:
        total = 0
        for d in docs:
            for label, job in d.jobs:
                req = lib.derived_requests(job)
                total += req["figures"] + req["embed_batches"]
                orq[f"{d.label} {label}"] = req
                print(f"  {d.label} {label}: {req['figures']} figure description(s) (+1 Mermaid per diagram), "
                      f"{req['embed_batches']} embedding request(s) ({req['embedded_chunks']} chunks)")
        print(f"  search: {ctx.search_calls} calls, {ctx.search_requests} requests (query embedding + rerank of each "
              "query not issued before by this run; a query cached by an earlier run costs nothing)")
        total += ctx.search_requests
        print(f"  total about {total} requests")
        orq["search_requests"], orq["total"] = ctx.search_requests, total
    mem = ctx.memory
    print(f"\nPeak container memory: docker stats {lib.gib(mem.get('sampled_peak'))} ({mem.get('samples')} samples);"
          f" cgroup memory.peak {lib.gib(mem.get('cgroup_peak_end'))} since the last start"
          + (f", {lib.gib(mem.get('cgroup_peak_before_restart'))} before the restart"
             if mem.get("cgroup_peak_before_restart") else ""))
    ri = ctx.restart_info
    if ri:
        print(f"Restart: {ri.get('doc')} interrupted in stage {ri.get('stage')}; command {ri.get('restart_seconds')}s, "
              f"healthy after {ri.get('health_seconds')}s; attempts {ri.get('attempts_before')} -> "
              f"{ri.get('attempts')}, final {ri.get('state')}")
    print("\nStep timings: " + ", ".join(f"{s} {t:.0f}s" for s, t in ctx.timings))
    wall = ctx.elapsed()
    print(f"Total wall time: {wall:.0f}s ({wall / 60:.1f} min)")
    ctx.json.update(started=ctx.started, rows=r.rows, passed=counts[PASS], failed=counts[FAIL], skipped=counts[SKIP],
                    latency=lat, health=hs, openrouter=orq, memory=mem, restart=ctx.restart_info,
                    wall_seconds=round(wall, 1), offline=ctx.offline,
                    target={"base": ctx.target.base, "container": ctx.target.container},
                    timings={s: round(t, 1) for s, t in ctx.timings})
    for d in docs:
        ctx.json["docs"][d.label] = {"doc_id": None if ctx.numbers_only else d.doc_id, "scores": d.scores,
                                     "jobs": [{"label": label, "state": job.get("state"),
                                               "attempts": job.get("attempts"),
                                               "stage_seconds": lib.stage_seconds(job)} for label, job in d.jobs]}
    if ctx.args.report:
        Path(ctx.args.report).write_text(json.dumps(ctx.json, indent=1, ensure_ascii=False, default=str),
                                         encoding="utf-8")
        print(f"Report written to {ctx.args.report}")


# ============================================================================ the flow


def run_flow(ctx: Ctx, plan: dict) -> None:
    """The steps in the order one worker needs (see the module docstring)."""
    args = ctx.args
    docs: list[Doc] = plan["docs"]
    enc, q = ctx.extra["enc"], ctx.extra["queue"]
    ctx.watcher = JobWatcher(ctx)
    ctx.watcher.start()
    ctx.mem = MemSampler(ctx.target.container)
    ctx.mem.start()

    print("\n[2] uploads", flush=True)
    t0 = time.monotonic()
    for d in docs + [enc, q]:
        try:
            delete_existing(ctx, d)
        except Exception as exc:  # noqa: BLE001
            ctx.report.error("2", f"{d.label}: delete an existing copy", exc)
    for d in docs:
        try:
            upload_one(ctx, d)
        except Exception as exc:  # noqa: BLE001
            ctx.report.error("2", f"{d.label}: upload", exc)
    ctx.timings.append(("2 uploads", time.monotonic() - t0))

    restarter = None
    if not args.skip_restart:
        restarter = Restarter(ctx, plan["restart_candidates"])
        restarter.start()
    for d, transport in plan["before_restart"]:
        workflow_step(ctx, d, transport)
    restarted = None
    if restarter is not None:
        restarter.window.set()  # no client session is open from here until the restart is over
        restarter.finished.wait()
        t0 = time.monotonic()
        restarted = check_restart(ctx, restarter)
        ctx.timings.append(("5 restart", (restarter.result.get("restart_seconds") or 0)
                            + (restarter.result.get("health_seconds") or 0) + time.monotonic() - t0))
    else:
        ctx.report.add("5", "restart in the middle of a conversion", SKIP, "--skip-restart")
    for d, transport in plan["after_restart"]:
        workflow_step(ctx, d, transport)

    print(f"\n[7] failure uploads  ({ctx.elapsed():.0f}s)", flush=True)
    t0 = time.monotonic()
    try:
        step_failures_upload(ctx, enc)
    except Exception as exc:  # noqa: BLE001
        ctx.report.error("7", "failure uploads", exc)
    converting = plan["converting"]() if callable(plan["converting"]) else plan["converting"]
    print(f"\n[4] several clients at once  ({ctx.elapsed():.0f}s)", flush=True)
    try:
        if converting is not None and converting.job_id:
            ctx.watcher.wait(converting.job_id, args.timeout_min * 60,
                             lambda j: j.get("state") == "running" or j.get("state") in FINAL)
        step_failures_jobs(ctx, q, [converting] + docs + [enc] if converting else docs + [enc])
        t1 = time.monotonic()
        step_concurrency(ctx, plan["agents"], converting)
        ctx.timings.append(("4 concurrency", time.monotonic() - t1))
    except Exception as exc:  # noqa: BLE001
        ctx.report.error("4", "several clients at once", exc)
    ctx.timings.append(("7 failure uploads + 4", time.monotonic() - t0))

    dup: Doc = plan["dup"]
    if restarted is not None and restarted is dup:  # before its forced reprocess publishes another result
        try:
            check_restart_final(ctx, restarted)
        except Exception as exc:  # noqa: BLE001
            ctx.report.error("5", "restart: final state", exc)
        restarted = None
    print(f"\n[6] duplicate and version rules  ({ctx.elapsed():.0f}s)", flush=True)
    reprocess_job = old_result = None
    try:
        _, rec = ctx.get_json("GET", f"/documents/{dup.doc_id}")
        old_result = rec.get("result_job_id")
        reprocess_job = step_versions(ctx, dup)
    except Exception as exc:  # noqa: BLE001
        ctx.report.error("6", "duplicate and version rules", exc)

    for d, transport in plan["late"]:
        workflow_step(ctx, d, transport)
    try:
        check_restart_final(ctx, restarted)
    except Exception as exc:  # noqa: BLE001
        ctx.report.error("5", "restart: final state", exc)

    print(f"\n[7] encrypted PDF, forced reprocess, delete after done  ({ctx.elapsed():.0f}s)", flush=True)
    if enc.job_id:
        job = finish_job(ctx, enc, enc.job_id, "process (encrypted)", step="7")
        enc.final_job = job
        if job is not None:
            _, rec = ctx.get_json("GET", f"/documents/{enc.doc_id}")
            ctx.report.check("7", "the encrypted PDF's result is published", rec.get("has_result") is True,
                             f"status {rec.get('status')}, pages {rec.get('pages')}")
    if reprocess_job:
        try:
            check_reprocess(ctx, dup, reprocess_job, old_result)
        except Exception as exc:  # noqa: BLE001
            ctx.report.error("6", "forced reprocess", exc)
    try:
        step_delete_done(ctx, q)
    except Exception as exc:  # noqa: BLE001
        ctx.report.error("7", "delete after done", exc)

    print(f"\n[8] operations  ({ctx.elapsed():.0f}s)", flush=True)
    t0 = time.monotonic()
    try:
        step_purge(ctx, purge_target(ctx, enc, q))
    except Exception as exc:  # noqa: BLE001
        ctx.report.error("8", "purge", exc)
    for d in (enc, q):  # leftovers of a failed path (a failed encrypted job, an unpurged copy): removed quietly
        if d.doc_id and ctx.http.get(f"/documents/{d.doc_id}").status_code == 200:
            ctx.http.delete(f"/documents/{d.doc_id}", params={"purge_caches": "true"})
    if args.skip_drill:
        ctx.report.add("8", "backup / restore drill", SKIP, "--skip-drill")
    else:
        try:
            step_drill(ctx, plan["drill"])
        except Exception as exc:  # noqa: BLE001
            ctx.report.error("8", "backup / restore drill", exc)
    ctx.timings.append(("8 operations", time.monotonic() - t0))


def resolve_target(args: argparse.Namespace) -> Target:
    preset = TARGETS[args.target]
    restart = args.restart_cmd or preset["restart_cmd"]
    stdio = args.stdio_cmd or preset["stdio_cmd"]
    return Target(base=(args.base_url or preset["base_url"]).rstrip("/"),
                  container=args.container or preset["container"],
                  restart_cmd=shlex.split(restart, posix=True), stdio_cmd=shlex.split(stdio, posix=True),
                  drill_port=args.drill_port or preset["drill_port"])


def inspect_target(t: Target) -> None:
    proc = docker("inspect", "-f", "{{json .Mounts}}|{{.Config.Image}}", t.container, timeout=30)
    if proc.returncode != 0:
        return
    mounts, _, image = out_text(proc).strip().partition("|")
    try:
        for m in json.loads(mounts):
            if m.get("Destination") == "/data" and m.get("Type") == "volume":
                t.volume = m.get("Name")
    except ValueError:
        pass
    t.image = image or None


def made_up_docs(numbers_only: bool) -> tuple[Doc, Doc]:
    enc = Doc("ENC", "encrypted made-up PDF", lib.make_pdf(lib.synthetic_protocol(E_CODE), user_password=E_PASSWORD,
                                                           revision=3), password=E_PASSWORD)
    q = Doc("QUEUE", "queued made-up PDF", lib.make_pdf(lib.synthetic_protocol(Q_CODE, with_figure=False)))
    return enc, q


def run_public(args: argparse.Namespace) -> int:
    target = resolve_target(args)
    inspect_target(target)
    docs = []
    for name in dict.fromkeys(args.protocols):
        try:
            pdf = public_protocol(name)
        except NotPublic as exc:
            print(exc, file=sys.stderr)
            return 2
        docs.append(Doc(name, name, pdf.read_bytes(), pdf=pdf))
    if len(docs) < 2:
        print("--protocols needs at least two public protocols", file=sys.stderr)
        return 2
    paths = ["rest", "mcp_base64", "inbox", "rest"]
    transports = ["http", "stdio", "stdio", "http"]
    for i, d in enumerate(docs):
        d.upload, d.transport = paths[i % 4], transports[i % 4]
    ctx = Ctx(args, target, numbers_only=False)
    ctx.docs = docs
    enc, q = made_up_docs(False)
    ctx.extra = {"enc": enc, "queue": q}
    print(f"End-to-end test of {target.base} (container {target.container}, volume {target.volume}, image "
          f"{target.image}): " + ", ".join(f"{d.name} ({len(d.data) // 1024} KiB, {d.sha[:12]})" for d in docs))
    restart_c = docs[2:] if len(docs) >= 3 else docs[-1:]
    plan = {"docs": docs, "restart_candidates": restart_c,
            "before_restart": [(d, d.transport) for d in docs[:2] if d not in restart_c],
            "after_restart": [(d, d.transport) for d in docs[2:-1]],
            "converting": docs[-1], "agents": [(docs[0], "http"), (docs[1], "stdio")],
            "dup": docs[0], "late": [(docs[-1], docs[-1].transport)], "drill": docs[0]}
    return execute(ctx, plan)


def run_private(args: argparse.Namespace) -> int:
    """--numbers-only (or --rehearsal): one PDF in a scratch container created and removed here."""
    rehearsal = args.rehearsal is not None
    if rehearsal:
        try:
            pdf = public_protocol(args.rehearsal)
        except NotPublic as exc:
            print(exc, file=sys.stderr)
            return 2
    else:
        if not args.allow_openrouter:
            print("--numbers-only runs an online scratch container: --allow-openrouter is required (nothing was "
                  "started)", file=sys.stderr)
            return 2
        try:
            import test_protocols

            pdf = Path(test_protocols.source_pdf(args.pdf_label))
        except Exception as exc:  # noqa: BLE001 - never print the path or the message
            print(f"the private PDF could not be resolved ({type(exc).__name__})", file=sys.stderr)
            return 2
        if not pdf.is_file():
            print("the private PDF could not be resolved (no such file)", file=sys.stderr)
            return 2
    name = args.private_container
    vol = f"{name}-data"
    port = args.private_port
    image_src = Target("", args.container or TARGETS[args.target]["container"], [], [], 0)
    inspect_target(image_src)
    image = args.image or image_src.image or "protocol-service:latest"
    if not remove_ours("container", name) or not remove_ours("volume", vol):
        print(f"a container or volume named {name} exists that this script did not create: refusing",
              file=sys.stderr)
        return 2
    target = Target(f"http://127.0.0.1:{port}", name, ["docker", "restart", name],
                    ["docker", "exec", "-i", name, "python", "-m", "service.mcp_stdio"],
                    args.drill_port or PRIVATE_DEFAULT["drill_port"], volume=vol, image=image)
    data = pdf.read_bytes()
    d = Doc("DOC", "DOC", data, pdf=None, upload="rest", transport="http")
    ctx = Ctx(args, target, numbers_only=True)
    ctx.docs = [d]
    enc, q = made_up_docs(True)
    ctx.extra = {"enc": enc, "queue": q}
    print(f"Numbers-only end-to-end test{' (rehearsal with a public PDF)' if rehearsal else ''}: one PDF of "
          f"{len(data) // 1024} KiB in a scratch container on 127.0.0.1:{port}")
    env = ["-e", "OPENROUTER_OFFLINE=1"] if rehearsal else ["--env-file", str(ROOT / ".env")]
    extra = ["-v", f"{ROOT / 'service'}:/app/service:ro"] if args.mount_service_code else []
    docker("volume", "create", "--label", f"{LABEL}=private", vol, timeout=60)
    proc = docker("run", "-d", "--name", name, "--label", f"{LABEL}=private", "-p", f"127.0.0.1:{port}:8000",
                  *HARDENING, *env, "-v", f"{vol}:/data", *extra, image, timeout=120)
    ctx.cleanups.append(("private volume", partial(docker, "volume", "rm", "-f", vol)))
    ctx.cleanups.append(("private container", partial(docker, "rm", "-f", name)))
    if proc.returncode != 0:
        print(f"the scratch container did not start (exit {proc.returncode})", file=sys.stderr)
        run_cleanups(ctx)
        return 1
    plan = {"docs": [d], "restart_candidates": [d], "before_restart": [],
            "after_restart": [(d, "http"), (d, "stdio")],
            "converting": enc, "agents": [(d, "http"), (d, "stdio")],
            "dup": d, "late": [], "drill": d}
    return execute(ctx, plan, private=True)


def run_cleanups(ctx: Ctx) -> None:
    for label, fn in reversed(ctx.cleanups):
        try:
            fn()
        except Exception:  # noqa: BLE001
            print(f"  cleanup of {label} failed", flush=True)
    ctx.cleanups.clear()


def execute(ctx: Ctx, plan: dict, private: bool = False) -> int:
    args = ctx.args
    code = 0
    try:
        print("\n[1] service", flush=True)
        t0 = time.monotonic()
        try:
            ok = step_service(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.report.error("1", "service", exc)
            ok = False
        if not ok or ctx.offline is None:
            print("\nThe service did not answer: stopping.")
            summary(ctx)
            return 1
        if ctx.offline:
            print("    guard: the service is offline: nothing is sent to OpenRouter")
        elif args.allow_openrouter:
            ctx.allowed = True
            print("    guard: --allow-openrouter given\n" + notice(ctx, private))
        else:
            print("\nREFUSED (nothing was uploaded):\n" + notice(ctx, private)
                  + "\nRe-run with --allow-openrouter to accept.")
            return 2
        try:
            step_host_origin(ctx)
        except Exception as exc:  # noqa: BLE001
            ctx.report.error("1", "Host / Origin checks", exc)
        ctx.timings.append(("1 service", time.monotonic() - t0))
        run_flow(ctx, plan)
    except KeyboardInterrupt:
        print("\ninterrupted")
        code = 130
    except Exception as exc:  # noqa: BLE001 - never a raw traceback (its message could quote a document)
        ctx.report.error("-", "unexpected failure of the test script", exc)
    finally:
        if ctx.watcher is not None:
            ctx.watcher.stop_event.set()
        if ctx.mem is not None:
            ctx.mem.stop_event.set()
            ctx.memory = {"sampled_peak": ctx.mem.peak, "samples": ctx.mem.samples,
                          "cgroup_peak_end": cgroup_peak(ctx.target.container),
                          "cgroup_peak_before_restart": ctx.restart_info.get("cgroup_peak_before")}
        run_cleanups(ctx)
        ctx.close_clients()
    if ctx.mem is not None or ctx.report.rows:
        summary(ctx)
    if code:
        return code
    return 1 if ctx.report.failed else 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="End-to-end test of the protocol service in Docker (see the module "
                                            "docstring).")
    p.add_argument("--target", choices=sorted(TARGETS), default="compose",
                   help="preset of base URL, container, restart and stdio commands, drill port (default compose)")
    p.add_argument("--base-url")
    p.add_argument("--container")
    p.add_argument("--restart-cmd", help="command that restarts the service (split with shlex, POSIX rules)")
    p.add_argument("--stdio-cmd", help="command of the MCP stdio server (split with shlex, POSIX rules)")
    p.add_argument("--drill-port", type=int, help="host port of the drill container (default 8001 / 8003 / 8005)")
    p.add_argument("--protocols", nargs="+", default=list(DEFAULT_PROTOCOLS),
                   help="folder names directly under public_protocols/ (order sets the roles)")
    p.add_argument("--allow-openrouter", action="store_true",
                   help="accept that an online service sends figures, chunk texts and the test queries to "
                        "OpenRouter (zero-data-retention routing)")
    p.add_argument("--skip-restart", action="store_true")
    p.add_argument("--skip-drill", action="store_true")
    p.add_argument("--timeout-min", type=float, default=120.0, help="limit per wait (minutes)")
    p.add_argument("--report", help="write the results as JSON to this file")
    p.add_argument("--mount-service-code", action="store_true",
                   help="mount the repository's service/ read-only into the drill / scratch containers")
    p.add_argument("--numbers-only", action="store_true",
                   help="one private PDF (--pdf-label) in a scratch container; prints numbers only")
    p.add_argument("--pdf-label", choices=("MAIN", "P1", "P2", "P3"))
    p.add_argument("--private-container", default=PRIVATE_DEFAULT["container"])
    p.add_argument("--private-port", type=int, default=PRIVATE_DEFAULT["port"])
    p.add_argument("--image", help="image of the scratch container (default: the target container's image)")
    p.add_argument("--rehearsal", metavar="PUBLIC_NAME",
                   help="the numbers-only flow with a public PDF and an offline scratch container")
    args = p.parse_args(argv)
    if args.numbers_only and not args.pdf_label and not args.rehearsal:
        p.error("--numbers-only needs --pdf-label")
    return args


def quiet_excepthooks() -> None:
    """Uncaught exceptions (any thread) print the exception type and our file:line only, never a message."""
    def thread_hook(hook_args) -> None:
        print(f"thread {hook_args.thread.name if hook_args.thread else '?'} failed: "
              f"{lib.scrub(_where(hook_args.exc_value))}", file=sys.stderr, flush=True)

    def main_hook(exc_type, exc, tb) -> None:
        print(f"failed: {lib.scrub(_where(exc))}", file=sys.stderr, flush=True)

    threading.excepthook = thread_hook
    sys.excepthook = main_hook
    import logging

    logging.disable(logging.CRITICAL)  # library log records (MCP SDK, httpx) can quote what they received


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.numbers_only or args.rehearsal:
        quiet_excepthooks()
        return run_private(args)
    return run_public(args)


if __name__ == "__main__":
    sys.exit(main())
