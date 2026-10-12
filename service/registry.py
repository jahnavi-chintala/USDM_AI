"""SQLite registry of documents and jobs: identity, the upload decision (duplicates, stale results,
reprocessing) and related documents.

Registry(config) opens config.registry_path in WAL mode. One connection per Registry, shared by the threads
of the process and guarded by an RLock; every multi-statement change runs in BEGIN IMMEDIATE, so another
process on the same file (`python -m service.import_existing`) serialises its writes with ours.

Tables
  documents         doc_id (SHA-256 of the PDF bytes), filenames (JSON list of every upload name), size, pages,
                    text_fingerprint, identifiers (JSON: nct, sponsor_protocol_id, version, date, ...), nct and
                    sponsor_protocol_id (normalised copies for lookups), pipeline_version + fingerprint of the
                    published result, status (latest job's state, "none" before any job), created_at,
                    updated_at, error (JSON {type, where, job_id} of the latest failure), quality (JSON summary),
                    current_job_id (latest job), result_job_id (job whose result is published), published_at
  jobs              seq (FIFO order), job_id, doc_id, state, kind ("process" | "reprocess" | "import"),
                    reprocess (0/1), filename, stage, progress_done/total, attempts, created/started/finished_at,
                    error_type + error_where (our file:line only, never an exception message), warnings (JSON),
                    stages (JSON StageResult list)
  related_documents doc_a < doc_b, reason ("same nct" | "same sponsor_protocol_id")

Upload decision (Registry.ingest, the one entry point for REST and MCP uploads):
  - the same bytes have a queued/running job                -> that job, duplicate=True
  - a published result made with the current fingerprint   -> that result, duplicate=True, job_id=None
  - a published result made with another version/fingerprint -> stale=True, no job; with reprocess=True a
    reprocess job is queued (caches seeded, the old result stays served until the new one is published)
  - no published result (new, failed or cancelled)          -> a new job is queued
  - always: another doc_id with the same text_fingerprint   -> warning "same text as <doc_id>" (never merged)
A PDF password is held in memory only (Registry.pdf_passwords), never in SQLite, files or logs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import subprocess
import sys
import threading
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from service.config import ROOT, ServiceConfig
from service.contracts import JOB_STATES, PipelineResult
from service.storage import (SavedUpload, Storage, UploadRejected, check_doc_id, check_job_id, new_job_id,
                             utcnow)

log = logging.getLogger("service.registry")

SCHEMA_VERSION = 1
MAX_ATTEMPTS = 3                       # a job interrupted this many times (crash loop) is failed at restart
RESULT_STATES = ("done", "done_with_warnings")
IMPORTED_VERSION = "imported"          # pipeline_version of an imported result: always stale
RELATION_KEYS = (("nct", "same nct"), ("sponsor_protocol_id", "same sponsor_protocol_id"))
MAX_FILENAMES = 100
INSPECT_TIMEOUT = 600.0
FPDF_ERR_PASSWORD = 4                  # pdfium's "incorrect password" load error

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    filenames TEXT NOT NULL DEFAULT '[]',
    size INTEGER,
    pages INTEGER,
    text_fingerprint TEXT,
    identifiers TEXT NOT NULL DEFAULT '{}',
    nct TEXT,
    sponsor_protocol_id TEXT,
    pipeline_version TEXT,
    fingerprint TEXT,
    status TEXT NOT NULL DEFAULT 'none',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error TEXT,
    quality TEXT NOT NULL DEFAULT '{}',
    current_job_id TEXT,
    result_job_id TEXT,
    published_at TEXT
);
CREATE INDEX IF NOT EXISTS documents_text ON documents(text_fingerprint);
CREATE INDEX IF NOT EXISTS documents_nct ON documents(nct);
CREATE INDEX IF NOT EXISTS documents_sponsor ON documents(sponsor_protocol_id);
CREATE TABLE IF NOT EXISTS jobs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL UNIQUE,
    doc_id TEXT NOT NULL,
    state TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'process',
    reprocess INTEGER NOT NULL DEFAULT 0,
    filename TEXT,
    stage TEXT,
    progress_done INTEGER,
    progress_total INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error_type TEXT,
    error_where TEXT,
    warnings TEXT NOT NULL DEFAULT '[]',
    stages TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, seq);
CREATE INDEX IF NOT EXISTS jobs_doc ON jobs(doc_id);
CREATE TABLE IF NOT EXISTS related_documents (
    doc_a TEXT NOT NULL,
    doc_b TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (doc_a, doc_b, reason),
    CHECK (doc_a < doc_b)
);
"""


class RegistryError(Exception):
    pass


class NotFound(RegistryError):
    pass


class DocumentBusy(RegistryError):
    """The document has a running job (delete / import refused until it finishes)."""


# ---------------------------------------------------------------- PDF identity


def normalise_text(text: str) -> str:
    """NFKC, casefold, whitespace collapsed to single spaces."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


_PDFIUM_LOCK = threading.Lock()  # pdfium is not thread-safe: serialise this module's in-process calls


def _pdf_text_and_pages(pdf_path: Path, password: str | None = None) -> tuple[str, int]:
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(pdf_path), password=password or None)
        try:
            parts = []
            for i in range(len(pdf)):
                page = pdf[i]
                try:
                    textpage = page.get_textpage()
                    try:
                        parts.append(textpage.get_text_range())
                    finally:
                        textpage.close()
                finally:
                    page.close()
            return "\n".join(parts), len(pdf)
        finally:
            pdf.close()


def _fingerprint_of(text: str) -> str | None:
    norm = normalise_text(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest() if norm else None


def text_fingerprint(pdf_path: Path, password: str | None = None) -> str | None:
    """SHA-256 of the pdfium text layer, normalised (NFKC, casefold, whitespace collapsed); None when the PDF
    has no text layer (a scan: an empty text would make every scan "the same text")."""
    return _fingerprint_of(_pdf_text_and_pages(pdf_path, password)[0])


def page_count(pdf_path: Path, password: str | None = None) -> int:
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(pdf_path), password=password or None)
        try:
            return len(pdf)
        finally:
            pdf.close()


def inspect_pdf_in_process(pdf_path: Path, password: str | None = None) -> dict:
    """{"pages", "text_fingerprint", "error"}: error None | "password" (missing or wrong) | "invalid"."""
    try:
        text, pages = _pdf_text_and_pages(Path(pdf_path), password)
    except Exception as exc:  # noqa: BLE001 - pdfium raises PdfiumError; anything else is a broken file too
        kind = "password" if getattr(exc, "err_code", None) == FPDF_ERR_PASSWORD else "invalid"
        return {"pages": None, "text_fingerprint": None, "error": kind}
    return {"pages": pages, "text_fingerprint": _fingerprint_of(text), "error": None}


def inspect_pdf(pdf_path: Path, password: str | None = None, *, timeout: float = INSPECT_TIMEOUT) -> dict:
    """inspect_pdf_in_process() in a child process: pdfium is not thread-safe and native code parsing an
    untrusted upload must not be able to take the service down. The password goes through stdin.
    Extra errors: "timeout", "inspect_failed" (the child died): the upload then proceeds uninspected."""
    cmd = [sys.executable, "-m", "service.registry", "--inspect", str(Path(pdf_path).resolve())]
    empty = {"pages": None, "text_fingerprint": None}
    try:
        proc = subprocess.run(cmd, input=json.dumps({"password": password}), capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return {**empty, "error": "timeout"}
    lines = proc.stdout.strip().splitlines()
    try:
        out = json.loads(lines[-1]) if proc.returncode == 0 and lines else None
    except ValueError:
        out = None
    if not isinstance(out, dict):
        log.warning("PDF inspection failed (exit code %s)", proc.returncode)
        return {**empty, "error": "inspect_failed"}
    return out


# ---------------------------------------------------------------- small helpers


class PasswordVault:
    """PDF passwords of queued jobs, in this process's memory only (never SQLite, files or logs). A restart
    forgets them: such a job then fails on its encrypted PDF and is submitted again with the password."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}
        self._lock = threading.Lock()

    def put(self, job_id: str, password: str | None) -> None:
        if password:
            with self._lock:
                self._items[job_id] = password

    def pop(self, job_id: str) -> str | None:
        with self._lock:
            return self._items.pop(job_id, None)

    def discard(self, job_id: str) -> None:
        self.pop(job_id)

    def __len__(self) -> int:
        return len(self._items)

    def __repr__(self) -> str:
        return f"PasswordVault({len(self)} held)"


def clean_filename(name: str | None) -> str:
    """The base name of a client file name: no directories, no control characters, at most 255 characters."""
    base = PurePosixPath(str(name or "").replace("\\", "/")).name
    base = "".join(ch for ch in base if ch.isprintable()).strip()
    return base[:255] or "upload.pdf"


def clean_identifiers(identifiers: dict | None) -> dict:
    """Scalar values only (drops provenance like "sources" / "others"); strings capped at 500 characters."""
    out = {}
    for key, value in (identifiers or {}).items():
        if isinstance(value, str):
            out[str(key)] = value[:500]
        elif value is None or isinstance(value, (int, float, bool)):
            out[str(key)] = value
    return out


def _norm_id(value: Any) -> str | None:
    text = " ".join(str(value).split()).upper() if value not in (None, "") else ""
    return text or None


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _stages_json(stages: list) -> str:
    return json.dumps([asdict(s) if is_dataclass(s) else s for s in stages or []], default=str)


def _short(text: str | None, limit: int) -> str | None:
    return None if text is None else str(text)[:limit]


# ---------------------------------------------------------------- the registry


class Registry:
    """Documents, jobs and related documents in SQLite. Thread-safe; see the module docstring."""

    def __init__(self, config: ServiceConfig | str | Path, *,
                 inspector: Callable[[Path, str | None], dict] | None = None) -> None:
        self.path = config.registry_path if isinstance(config, ServiceConfig) else Path(config)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.inspector = inspector or inspect_pdf
        self.pdf_passwords = PasswordVault()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=30000")
            with self._tx() as c:  # statement by statement: executescript() would commit on its own
                for statement in SCHEMA.split(";"):
                    if statement.strip():
                        c.execute(statement)
                c.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Registry":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    # ---- upload decision
    def ingest(self, storage: Storage, source, filename: str | None, *, pipeline_version: str, fingerprint: str,
               reprocess: bool = False, password: str | None = None) -> dict:
        """Save an upload (bytes, binary file object or iterable of chunks) and decide what to do with it.

        Returns {doc_id, job_id (None: nothing queued), status, duplicate, stale, queued, warnings}. Raises
        UploadRejected ("empty", "not_pdf", "too_large", "invalid_pdf", "password_required", "bad_password").
        The workdir is kept only when this call queued a job; call worker.notify() when queued is True.
        """
        job_id = new_job_id()
        saved = storage.save_upload(job_id, source)
        try:
            decision = self.submit_upload(storage, saved, filename, pipeline_version=pipeline_version,
                                          fingerprint=fingerprint, reprocess=reprocess, password=password)
        except BaseException:
            self.pdf_passwords.discard(job_id)
            storage.remove_workdir(job_id)
            raise
        if decision["job_id"] != job_id:
            storage.remove_workdir(job_id)
        return decision

    def ingest_file(self, storage: Storage, path: Path, filename: str | None = None, **kwargs) -> dict:
        """ingest() of a local file (e.g. storage.inbox_file(name)); the file itself is left in place."""
        with open(path, "rb") as fh:
            return self.ingest(storage, fh, filename or Path(path).name, **kwargs)

    def submit_upload(self, storage: Storage, saved: SavedUpload, filename: str | None, *, pipeline_version: str,
                      fingerprint: str, reprocess: bool = False, password: str | None = None) -> dict:
        """The decision for an upload already saved in work/<saved.job_id>/ (see ingest(), which also removes
        the workdir when it is not used)."""
        doc_id, job_id = check_doc_id(saved.sha256), check_job_id(saved.job_id)
        known = self._one("SELECT published_at FROM documents WHERE doc_id=?", (doc_id,))
        pages = text_fp = None
        if known is None:  # a new document: open it once (rejects broken / locked PDFs early)
            info = self.inspector(saved.path, password)
            if info.get("error") == "password":
                raise UploadRejected("bad_password" if password else "password_required")
            if info.get("error") == "invalid":
                raise UploadRejected("invalid_pdf", "the PDF cannot be opened")
            pages, text_fp = info.get("pages"), info.get("text_fingerprint")
        elif reprocess and known["published_at"]:
            storage.seed_caches(doc_id, job_id)  # before the job becomes visible to the worker
        self.pdf_passwords.put(job_id, password)
        with self._tx() as c:
            self._upsert_document(c, doc_id, filename=clean_filename(filename), size=saved.size, pages=pages,
                                  text_fp=text_fp)
            decision = self._decide(c, doc_id, job_id, pipeline_version=pipeline_version, fingerprint=fingerprint,
                                    reprocess=reprocess, filename=clean_filename(filename))
        if decision["job_id"] != job_id:
            self.pdf_passwords.discard(job_id)
        return decision

    def request_reprocess(self, storage: Storage, doc_id: str, *, pipeline_version: str, fingerprint: str,
                          force: bool = False) -> dict:
        """Reprocess a published document from its stored source.pdf (no new upload). Queues a job when the
        result is stale (or force=True); returns the same dict as ingest(). NotFound without a stored source."""
        check_doc_id(doc_id)
        doc = self._one("SELECT filenames FROM documents WHERE doc_id=?", (doc_id,))
        if doc is None:
            raise NotFound(doc_id)
        job_id = new_job_id()
        if not storage.seed_reprocess(doc_id, job_id):
            storage.remove_workdir(job_id)
            raise NotFound(f"{doc_id}: no stored source.pdf")
        try:
            with self._tx() as c:
                names = json.loads(doc["filenames"] or "[]")
                decision = self._decide(c, doc_id, job_id, pipeline_version=pipeline_version, fingerprint=fingerprint,
                                        reprocess=True, filename=names[0] if names else None, force=force)
        except BaseException:
            storage.remove_workdir(job_id)
            raise
        if decision["job_id"] != job_id:
            storage.remove_workdir(job_id)
        return decision

    def _upsert_document(self, c: sqlite3.Connection, doc_id: str, *, filename: str | None, size: int | None,
                         pages: int | None, text_fp: str | None) -> None:
        now = utcnow()
        row = c.execute("SELECT filenames FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if row is None:
            c.execute("INSERT INTO documents (doc_id, filenames, size, pages, text_fingerprint, status, created_at, "
                      "updated_at) VALUES (?,?,?,?,?,'none',?,?)",
                      (doc_id, json.dumps([filename] if filename else []), size, pages, text_fp, now, now))
            return
        names = json.loads(row["filenames"] or "[]")
        if filename and filename not in names and len(names) < MAX_FILENAMES:
            names.append(filename)
        c.execute("UPDATE documents SET filenames=?, size=COALESCE(size, ?), pages=COALESCE(pages, ?), "
                  "text_fingerprint=COALESCE(text_fingerprint, ?), updated_at=? WHERE doc_id=?",
                  (json.dumps(names), size, pages, text_fp, now, doc_id))

    def _decide(self, c: sqlite3.Connection, doc_id: str, job_id: str, *, pipeline_version: str, fingerprint: str,
                reprocess: bool, filename: str | None, force: bool = False) -> dict:
        doc = c.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        warnings = [f"same text as {d}" for d in self._same_text(c, doc_id, doc["text_fingerprint"])]
        has_result = doc["published_at"] is not None
        stale = bool(has_result and (doc["pipeline_version"], doc["fingerprint"]) != (pipeline_version, fingerprint))
        active = c.execute("SELECT job_id FROM jobs WHERE doc_id=? AND state IN ('queued','running') "
                           "ORDER BY seq LIMIT 1", (doc_id,)).fetchone()

        def answer(jid: str | None, duplicate: bool, status: str, queued: bool = False) -> dict:
            return {"doc_id": doc_id, "job_id": jid, "status": status, "duplicate": duplicate, "stale": stale,
                    "queued": queued, "warnings": warnings}

        if active:
            return answer(active["job_id"], True, doc["status"])
        if has_result and not force and (not stale or not reprocess):
            return answer(None, True, doc["status"])
        kind = "reprocess" if has_result else "process"
        now = utcnow()
        c.execute("INSERT INTO jobs (job_id, doc_id, state, kind, reprocess, filename, created_at, warnings) "
                  "VALUES (?,?,'queued',?,?,?,?,?)",
                  (job_id, doc_id, kind, int(kind == "reprocess"), filename, now, json.dumps(warnings)))
        c.execute("UPDATE documents SET status='queued', current_job_id=?, updated_at=? WHERE doc_id=?",
                  (job_id, now, doc_id))
        return answer(job_id, has_result, "queued", queued=True)

    @staticmethod
    def _same_text(c: sqlite3.Connection, doc_id: str, text_fp: str | None) -> list[str]:
        if not text_fp:
            return []
        rows = c.execute("SELECT doc_id FROM documents WHERE text_fingerprint=? AND doc_id<>? ORDER BY rowid",
                         (text_fp, doc_id)).fetchall()
        return [r["doc_id"] for r in rows]

    # ---- worker side
    def claim_next_job(self) -> dict | None:
        """Atomically move the oldest queued job to running (attempts + 1) and return it, or None."""
        with self._tx() as c:
            row = c.execute("SELECT seq, job_id, doc_id FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if row is None:
                return None
            now = utcnow()
            c.execute("UPDATE jobs SET state='running', started_at=?, attempts=attempts+1, stage=NULL, "
                      "progress_done=NULL, progress_total=NULL WHERE seq=?", (now, row["seq"]))
            c.execute("UPDATE documents SET status='running', current_job_id=?, updated_at=? WHERE doc_id=?",
                      (row["job_id"], now, row["doc_id"]))
            return self._job_dict(c.execute("SELECT * FROM jobs WHERE seq=?", (row["seq"],)).fetchone())

    def update_progress(self, job_id: str, stage: str, done: int | None = None, total: int | None = None) -> None:
        """Current stage (one of STAGES) and its progress of a running job."""
        with self._lock:
            self._conn.execute("UPDATE jobs SET stage=?, progress_done=?, progress_total=? "
                               "WHERE job_id=? AND state='running'",
                               (_short(stage, 40), None if done is None else int(done),
                                None if total is None else int(total), job_id))

    def record_success(self, job_id: str, result: PipelineResult) -> dict:
        """After publish: the job is done / done_with_warnings and the document describes the new result
        (version, fingerprint, pages, identifiers, quality); related documents are linked again."""
        with self._tx() as c:
            job = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise NotFound(job_id)
            doc_id = job["doc_id"]
            warnings = list(dict.fromkeys(json.loads(job["warnings"] or "[]") + [str(w) for w in result.warnings]))
            state = (result.status if result.status in RESULT_STATES
                     else "done_with_warnings" if result.warnings else "done")
            now = utcnow()
            last_stage = result.stages[-1].name if result.stages else job["stage"]
            c.execute("UPDATE jobs SET state=?, stage=?, finished_at=?, warnings=?, stages=?, error_type=NULL, "
                      "error_where=NULL WHERE job_id=?",
                      (state, last_stage, now, json.dumps(warnings), _stages_json(result.stages), job_id))
            ids = clean_identifiers(result.identifiers)
            c.execute("UPDATE documents SET status=?, pipeline_version=?, fingerprint=?, pages=COALESCE(?, pages), "
                      "identifiers=?, nct=?, sponsor_protocol_id=?, quality=?, error=NULL, current_job_id=?, "
                      "result_job_id=?, published_at=?, updated_at=? WHERE doc_id=?",
                      (state, result.pipeline_version, result.fingerprint, result.pages or None, json.dumps(ids),
                       _norm_id(ids.get("nct")), _norm_id(ids.get("sponsor_protocol_id")),
                       json.dumps(_jsonable(result.quality or {})), job_id, job_id, now, now, doc_id))
            self._link_related(c, doc_id)
            return self._job_dict(c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def record_failure(self, job_id: str, error_type: str, error_where: str | None) -> None:
        """The job failed: exception type and our own file:line only. A published result stays as it is."""
        error_type, error_where = _short(error_type, 120) or "Error", _short(error_where, 300)
        with self._tx() as c:
            job = c.execute("SELECT doc_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise NotFound(job_id)
            now = utcnow()
            c.execute("UPDATE jobs SET state='failed', finished_at=?, error_type=?, error_where=? "
                      "WHERE job_id=? AND state IN ('queued','running')", (now, error_type, error_where, job_id))
            c.execute("UPDATE documents SET status='failed', error=?, updated_at=? WHERE doc_id=? AND current_job_id=?",
                      (json.dumps({"type": error_type, "where": error_where, "job_id": job_id}), now,
                       job["doc_id"], job_id))

    def running_jobs(self) -> list[dict]:
        return [self._job_dict(r) for r in self._all("SELECT * FROM jobs WHERE state='running' ORDER BY seq")]

    def job_counts(self) -> dict[str, int]:
        """Number of jobs per state (every state of JOB_STATES, 0 when none); also a cheap liveness probe."""
        counts = {s: 0 for s in JOB_STATES}
        counts.update({r["state"]: r["n"] for r in self._all("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")})
        return counts

    def requeue_job(self, job_id: str) -> str:
        """A job left running by a crash goes back to the queue (keeping its FIFO position), or fails after
        MAX_ATTEMPTS starts. Returns the job's new state."""
        with self._tx() as c:
            job = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise NotFound(job_id)
            if job["state"] != "running":
                return job["state"]
            now = utcnow()
            if job["attempts"] >= MAX_ATTEMPTS:
                c.execute("UPDATE jobs SET state='failed', finished_at=?, error_type='Interrupted', error_where=NULL "
                          "WHERE job_id=?", (now, job_id))
                c.execute("UPDATE documents SET status='failed', error=?, updated_at=? "
                          "WHERE doc_id=? AND current_job_id=?",
                          (json.dumps({"type": "Interrupted", "where": None, "job_id": job_id}), now,
                           job["doc_id"], job_id))
                return "failed"
            c.execute("UPDATE jobs SET state='queued', started_at=NULL, stage=NULL, progress_done=NULL, "
                      "progress_total=NULL WHERE job_id=?", (job_id,))
            c.execute("UPDATE documents SET status='queued', updated_at=? WHERE doc_id=? AND current_job_id=?",
                      (now, job["doc_id"], job_id))
            return "queued"

    def cancel_job(self, job_id: str, storage: Storage | None = None) -> bool:
        """Cancel a queued job (it can never start afterwards: claiming requires state queued). Running jobs
        are not interrupted. Removes the job's workdir when storage is given. True when cancelled."""
        with self._tx() as c:
            job = c.execute("SELECT doc_id FROM jobs WHERE job_id=? AND state='queued'", (job_id,)).fetchone()
            if job is None:
                return False
            now = utcnow()
            c.execute("UPDATE jobs SET state='cancelled', finished_at=? WHERE job_id=?", (now, job_id))
            c.execute("UPDATE documents SET status='cancelled', updated_at=? WHERE doc_id=? AND current_job_id=?",
                      (now, job["doc_id"], job_id))
        self.pdf_passwords.discard(job_id)
        if storage is not None:
            storage.remove_workdir(job_id)
        return True

    def known_job_ids(self) -> set[str]:
        return {r["job_id"] for r in self._all("SELECT job_id FROM jobs")}

    # ---- import of an existing result (service.import_existing)
    def register_import(self, doc_id: str, job_id: str, *, filename: str | None, size: int | None,
                        pages: int | None, text_fp: str | None) -> list[str]:
        """Register the document and a running "import" job (publish, then record_success). DocumentBusy when
        the document has an active job. Returns the "same text as" warnings."""
        check_doc_id(doc_id)
        check_job_id(job_id)
        with self._tx() as c:
            if c.execute("SELECT 1 FROM jobs WHERE doc_id=? AND state IN ('queued','running')", (doc_id,)).fetchone():
                raise DocumentBusy(doc_id)
            self._upsert_document(c, doc_id, filename=clean_filename(filename), size=size, pages=pages,
                                  text_fp=text_fp)
            warnings = [f"same text as {d}" for d in self._same_text(c, doc_id, text_fp)]
            now = utcnow()
            c.execute("INSERT INTO jobs (job_id, doc_id, state, kind, filename, attempts, created_at, started_at, "
                      "warnings) VALUES (?,?,'running','import',?,1,?,?,?)",
                      (job_id, doc_id, clean_filename(filename), now, now, json.dumps(warnings)))
            c.execute("UPDATE documents SET status='running', current_job_id=?, updated_at=? WHERE doc_id=?",
                      (job_id, now, doc_id))
        return warnings

    # ---- related documents
    def _link_related(self, c: sqlite3.Connection, doc_id: str) -> None:
        """Same NCT or sponsor protocol id but a different text: likely another version or amendment."""
        c.execute("DELETE FROM related_documents WHERE doc_a=? OR doc_b=?", (doc_id, doc_id))
        doc = c.execute("SELECT nct, sponsor_protocol_id, text_fingerprint FROM documents WHERE doc_id=?",
                        (doc_id,)).fetchone()
        now = utcnow()
        for column, reason in RELATION_KEYS:
            if not doc[column]:
                continue
            for other in c.execute(f"SELECT doc_id, text_fingerprint FROM documents WHERE {column}=? AND doc_id<>?",
                                   (doc[column], doc_id)).fetchall():
                if doc["text_fingerprint"] and doc["text_fingerprint"] == other["text_fingerprint"]:
                    continue  # the same text: reported as "same text as", not as a version
                a, b = sorted((doc_id, other["doc_id"]))
                c.execute("INSERT OR IGNORE INTO related_documents VALUES (?,?,?,?)", (a, b, reason, now))

    def related_documents(self, doc_id: str) -> list[dict]:
        """[{doc_id, reasons}] of the documents related to doc_id."""
        rows = self._all("SELECT doc_b AS other, reason FROM related_documents WHERE doc_a=? "
                         "UNION ALL SELECT doc_a AS other, reason FROM related_documents WHERE doc_b=?",
                         (doc_id, doc_id))
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["other"], []).append(r["reason"])
        return [{"doc_id": d, "reasons": sorted(rs)} for d, rs in sorted(out.items())]

    # ---- reads
    @staticmethod
    def _job_dict(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        return {
            "job_id": row["job_id"], "doc_id": row["doc_id"], "state": row["state"], "kind": row["kind"],
            "reprocess": bool(row["reprocess"]), "filename": row["filename"], "stage": row["stage"],
            "progress": {"done": row["progress_done"], "total": row["progress_total"]},
            "attempts": row["attempts"], "created_at": row["created_at"], "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "error": {"type": row["error_type"], "where": row["error_where"]} if row["error_type"] else None,
            "warnings": json.loads(row["warnings"] or "[]"), "stages": json.loads(row["stages"] or "[]"),
        }

    @staticmethod
    def _doc_dict(row: sqlite3.Row, pipeline_version: str | None, fingerprint: str | None) -> dict:
        has_result = row["published_at"] is not None
        stale = None
        if pipeline_version is not None and fingerprint is not None:
            stale = bool(has_result and (row["pipeline_version"], row["fingerprint"]) != (pipeline_version, fingerprint))
        return {
            "doc_id": row["doc_id"], "filenames": json.loads(row["filenames"] or "[]"), "size": row["size"],
            "pages": row["pages"], "text_fingerprint": row["text_fingerprint"],
            "identifiers": json.loads(row["identifiers"] or "{}"), "status": row["status"],
            "pipeline_version": row["pipeline_version"], "fingerprint": row["fingerprint"],
            "has_result": has_result, "published_at": row["published_at"], "stale": stale,
            "quality": json.loads(row["quality"] or "{}"),
            "error": json.loads(row["error"]) if row["error"] else None,
            "current_job_id": row["current_job_id"], "result_job_id": row["result_job_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def get_document(self, doc_id: str, *, pipeline_version: str | None = None,
                     fingerprint: str | None = None) -> dict | None:
        """The document with related documents, same-text documents and its current job; stale is computed
        when the running pipeline_version and fingerprint are given (else None)."""
        row = self._one("SELECT * FROM documents WHERE doc_id=?", (doc_id,))
        if row is None:
            return None
        doc = self._doc_dict(row, pipeline_version, fingerprint)
        doc["related"] = self.related_documents(doc_id)
        with self._lock:
            doc["same_text_as"] = self._same_text(self._conn, doc_id, row["text_fingerprint"])
        doc["current_job"] = self.get_job(row["current_job_id"]) if row["current_job_id"] else None
        return doc

    def list_documents(self, *, status: str | None = None, limit: int = 100, offset: int = 0,
                       pipeline_version: str | None = None, fingerprint: str | None = None) -> list[dict]:
        """Documents, most recently updated first (no related / job details: see get_document)."""
        sql, params = "SELECT * FROM documents", []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, doc_id LIMIT ? OFFSET ?"
        params += [max(0, int(limit)), max(0, int(offset))]
        return [self._doc_dict(r, pipeline_version, fingerprint) for r in self._all(sql, tuple(params))]

    def get_job(self, job_id: str) -> dict | None:
        return self._job_dict(self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,)))

    def list_jobs(self, *, doc_id: str | None = None, state: str | None = None, limit: int = 100) -> list[dict]:
        """Jobs, newest first, optionally of one document and/or in one state."""
        if state is not None and state not in JOB_STATES:
            raise ValueError(f"unknown job state {state!r}")
        clauses, params = [], []
        if doc_id:
            clauses.append("doc_id=?")
            params.append(doc_id)
        if state:
            clauses.append("state=?")
            params.append(state)
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY seq DESC LIMIT ?"
        return [self._job_dict(r) for r in self._all(sql, (*params, max(0, int(limit))))]

    def delete_document(self, doc_id: str, storage: Storage | None = None) -> bool:
        """Delete the document's rows (documents, jobs, related_documents): its queued jobs can never start.
        DocumentBusy while a job of it is running. With storage, its result and workdirs are deleted too."""
        check_doc_id(doc_id)
        with self._tx() as c:
            if c.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone() is None:
                return False
            if c.execute("SELECT 1 FROM jobs WHERE doc_id=? AND state='running'", (doc_id,)).fetchone():
                raise DocumentBusy(doc_id)
            job_ids = [r["job_id"] for r in c.execute("SELECT job_id FROM jobs WHERE doc_id=?", (doc_id,))]
            c.execute("DELETE FROM jobs WHERE doc_id=?", (doc_id,))
            c.execute("DELETE FROM related_documents WHERE doc_a=? OR doc_b=?", (doc_id, doc_id))
            c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        for job_id in job_ids:
            self.pdf_passwords.discard(job_id)
            if storage is not None:
                storage.remove_workdir(job_id)
        if storage is not None:
            storage.delete_document(doc_id)
        return True


def _main(argv: list[str] | None = None) -> int:
    """`python -m service.registry --inspect <pdf>` (password as JSON on stdin): used by inspect_pdf()."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inspect", required=True, help="PDF to open: prints {pages, text_fingerprint, error}")
    args = parser.parse_args(argv)
    try:
        password = (json.loads(sys.stdin.read() or "{}") or {}).get("password")
    except ValueError:
        password = None
    print(json.dumps(inspect_pdf_in_process(Path(args.inspect), password)))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
