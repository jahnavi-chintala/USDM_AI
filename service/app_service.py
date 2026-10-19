"""The service layer shared by the REST API (service.api) and the MCP server: both call these methods, so they
return the same results.

ProtocolService owns the storage, the registry and the background worker of one DATA_DIR:

    service = ProtocolService(ServiceConfig.from_env())
    service.start()                                   # recover interrupted work, start the worker
    service.ingest(fileobj, "protocol.pdf")           # {doc_id, job_id, status, duplicate, stale, queued, warnings}
    service.call_tool(doc_id, "read_section", {"section": "inclusion"})
    service.stop()                                    # stop the worker (a running job is re-queued at the next
                                                      # start), close the registry

Every method returns a JSON-ready dict and raises ServiceError on failure: `code` (the "type" of the JSON error),
`status` (the HTTP status the REST layer answers with) and a message. Messages name ids, reasons and the caller's
own arguments, never document text, with one exception: a protocol tool's lookup error carries its hints about
the same document (closest section titles or table captions), which the caller is reading anyway. Paths under
DATA_DIR are never shown. Unexpected exceptions are reported by type only; the log gets the type and our own
file:line.

Protocol tools run through protocol_api.ProtocolDocument.call_tool on the published result. Open documents are
kept in a small LRU cache, keyed by the published result (a reprocess that publishes a new result reopens it);
calls on one document are serialised (ProtocolDocument builds its indexes lazily and search writes its caches).
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import threading
from collections import OrderedDict
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from typing import Any, Callable

from service.config import ServiceConfig
from service.contracts import DOC_STATES, JOB_STATES, PipelineFn
from service.registry import DocumentBusy, Registry
from service.registry import NotFound as RegistryNotFound
from service.storage import (IMAGE_SUFFIXES, SAFE_FILES, LocalStorage, Storage, StorageError, UnsafePath,
                             UploadRejected, check_doc_id, check_job_id, is_image_dir)
from service.worker import Worker, describe_exception

log = logging.getLogger("service.app")

DOC_CACHE_SIZE = 4          # open ProtocolDocument instances kept (each holds a parsed Docling document)
MAX_LIST = 1000


# ============================================================================ errors

class ServiceError(Exception):
    """A request the service cannot fulfil. code: the JSON error "type"; status: the HTTP status."""

    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status:
            self.status = status
        self.details = details or {}

    def to_json(self) -> dict:
        out: dict[str, Any] = {"type": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class BadRequest(ServiceError):
    status, code = 400, "bad_request"


class NotFound(ServiceError):
    status, code = 404, "not_found"


class Conflict(ServiceError):
    status, code = 409, "conflict"


class PayloadTooLarge(ServiceError):
    status, code = 413, "too_large"


class UnsupportedMediaType(ServiceError):
    status, code = 415, "not_pdf"


class Unavailable(ServiceError):
    status, code = 503, "unavailable"


class UpstreamError(ServiceError):
    status, code = 502, "upstream_error"


# UploadRejected reasons (storage.save_upload, registry.ingest)
UPLOAD_ERRORS: dict[str, tuple[type[ServiceError], str]] = {
    "too_large": (PayloadTooLarge, "the PDF is larger than MAX_UPLOAD_MB"),
    "not_pdf": (UnsupportedMediaType, "the upload is not a PDF (no %PDF- header)"),
    "empty": (BadRequest, "the upload is empty"),
    "invalid_pdf": (BadRequest, "the PDF cannot be opened"),
    "password_required": (BadRequest, "the PDF is encrypted: pass its password"),
    "bad_password": (BadRequest, "the PDF password is wrong"),
}

# Errors a protocol tool returns ({"error", "error_type"} from call_tool) whose messages are written for the caller.
TOOL_ERRORS: dict[str, tuple[type[ServiceError], str]] = {
    "NotFoundError": (NotFound, "not_found"),
    "ProtocolAPIError": (BadRequest, "bad_request"),
    "SearchUnavailable": (Conflict, "search_unavailable"),
    "OfflineCacheMiss": (Unavailable, "offline"),
    "OfflineRefused": (Unavailable, "offline"),
    "UnknownTool": (NotFound, "unknown_tool"),
    "InvalidArguments": (BadRequest, "invalid_arguments"),
}
# The service's message for SearchUnavailable (protocol_api's names embed_chunks.py, a command line)
SEARCH_UNAVAILABLE = ("this document has no embeddings, so search is unavailable (it was processed offline): "
                      "reprocess it online with POST /documents/{doc_id}/reprocess?force=true")
# httpx transport errors of an OpenRouter call (search: query embedding, rerank)
NETWORK_ERRORS = {"TransportError", "NetworkError", "ConnectError", "ReadError", "WriteError", "CloseError",
                  "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
                  "ProtocolError", "RemoteProtocolError", "LocalProtocolError", "ProxyError", "UnsupportedProtocol"}

# Short names accepted for the files of a document (GET /documents/{doc_id}/files/{name})
FILE_ALIASES = {
    "md": "protocol.md", "markdown": "protocol.md", "json": "protocol.json", "html": "protocol.html",
    "decisions": "protocol.decisions.json", "decisions.json": "protocol.decisions.json", "quality": "quality.json",
    **{name.split("/", 1)[1]: name for name in SAFE_FILES if name.startswith("chunks/")},
}
MEDIA_TYPES = {
    ".md": "text/markdown; charset=utf-8", ".json": "application/json", ".jsonl": "application/x-ndjson",
    ".html": "text/html; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp",
}


def _package(name: str) -> str | None:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return None


class _OpenDocument:
    """One cache slot: the result it was opened from, the ProtocolDocument (opened on first use) and its lock."""

    __slots__ = ("key", "doc", "lock")

    def __init__(self, key: tuple) -> None:
        self.key = key
        self.doc = None
        self.lock = threading.Lock()


# ============================================================================ service

class ProtocolService:
    """Documents, jobs, files and protocol tools of one DATA_DIR (see the module docstring)."""

    def __init__(self, config: ServiceConfig | None = None, *, storage: Storage | None = None,
                 registry: Registry | None = None, pipeline_fn: PipelineFn | None = None, run_worker: bool = True,
                 settings=None, version: tuple[str, str] | None = None,
                 embed_fn: Callable[[str], Any] | None = None, rerank_fn: Callable | None = None,
                 doc_cache_size: int = DOC_CACHE_SIZE, worker_options: dict | None = None) -> None:
        """version=(pipeline_version, fingerprint) and pipeline_fn / embed_fn / rerank_fn replace the real ones
        in tests (embed_fn / rerank_fn: the query embedding and reranker of the search tool, see
        ProtocolDocument.open)."""
        self.config = config or ServiceConfig.from_env()
        if self.config.offline:  # the protocol tools and pipeline modules read the flag from the environment
            os.environ["OPENROUTER_OFFLINE"] = "1"
        self.settings = settings
        if version is None:
            from pipeline_version import PIPELINE_VERSION, pipeline_fingerprint

            version = (PIPELINE_VERSION, pipeline_fingerprint(settings, self.config.models()))
        self.pipeline_version, self.fingerprint = version
        self.storage = storage or LocalStorage(self.config)
        self.registry = registry or Registry(self.config)
        self.worker: Worker | None = None
        if run_worker:
            if pipeline_fn is None:
                from service.pipeline import process_document_isolated as pipeline_fn
            self.worker = Worker(self.config, self.registry, self.storage, pipeline_fn, settings=settings,
                                 **(worker_options or {}))
        self.embed_fn, self.rerank_fn = embed_fn, rerank_fn
        self.doc_cache_size = max(1, doc_cache_size)
        self._docs: OrderedDict[str, _OpenDocument] = OrderedDict()
        self._docs_guard = threading.Lock()
        self._started = False
        self._closed = False
        self._version_info: dict | None = None

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> dict:
        """Recover interrupted work and start the worker; returns the worker's recovery report."""
        report = self.worker.start() if self.worker else {}
        self._started = True
        return report

    def stop(self, timeout: float | None = 30.0) -> None:
        """Stop the worker (waits up to timeout for a running job; one still running is re-queued at the next
        start), drop the open documents and close the registry."""
        if self._closed:
            return
        if self.worker:
            self.worker.stop(timeout)
        with self._docs_guard:
            self._docs.clear()
        self.registry.close()
        self._closed = True

    def __enter__(self) -> "ProtocolService":
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def _notify(self) -> None:
        if self.worker:
            self.worker.notify()

    # ---------------------------------------------------------------- service information
    def health(self) -> dict:
        """{"status": "ok" | "unavailable", "registry", "worker", "jobs"}: unavailable when the registry cannot be
        read or the started worker has stopped."""
        try:
            jobs, registry_ok = self.registry.job_counts(), True
        except Exception as exc:  # noqa: BLE001 - health must answer, whatever the failure
            log.error("health: registry check failed: %s at %s", *describe_exception(exc))
            jobs, registry_ok = None, False
        if self.worker is None:
            worker = "disabled"
        elif not self._started:
            worker = "not_started"
        else:
            worker = "running" if self.worker.alive else "stopped"
        ok = registry_ok and worker in ("running", "disabled")
        return {"status": "ok" if ok else "unavailable", "registry": "ok" if registry_ok else "error",
                "worker": worker, "jobs": jobs, "offline": self.config.offline}

    def version(self) -> dict:
        """Pipeline version and fingerprint, what the fingerprint covers, package versions, models, offline flag."""
        if self._version_info is None:
            try:
                from pipeline_version import fingerprint_inputs

                inputs = fingerprint_inputs(self.settings, self.config.models())
            except Exception as exc:  # noqa: BLE001 - /version still answers without the details
                log.error("version: fingerprint inputs failed: %s at %s", *describe_exception(exc))
                inputs = None
            self._version_info = {
                "pipeline_version": self.pipeline_version, "fingerprint": self.fingerprint,
                "fingerprint_inputs": inputs,
                "packages": {p: _package(p) for p in ("docling", "docling-core", "docling-parse", "fastapi",
                                                      "mcp")},
                "models": {**self.config.models(), **({"tokenizer": inputs["models"].get("tokenizer")}
                                                      if inputs else {})},
            }
        return {**self._version_info, "offline": self.config.offline, "config": self.config.public()}

    # ---------------------------------------------------------------- uploads
    def ingest(self, source, filename: str | None = None, *, reprocess: bool = False,
               password: str | None = None) -> dict:
        """Save an upload (bytes, a binary file object or an iterable of chunks) and apply the duplicate/version
        rules (registry.Registry.ingest). Returns {doc_id, job_id, status, duplicate, stale, queued, warnings}.
        A password needs the worker of this process: it is held in this process's memory until the job starts."""
        self._check_password_here(password)
        try:
            decision = self.registry.ingest(self.storage, source, filename, pipeline_version=self.pipeline_version,
                                            fingerprint=self.fingerprint, reprocess=bool(reprocess),
                                            password=password or None)
        except UploadRejected as exc:
            raise _upload_error(exc.reason) from None
        if decision.get("queued"):
            self._notify()
        return decision

    def ingest_base64(self, data: str, filename: str | None = None, **kwargs) -> dict:
        """ingest() of a base64 PDF (a "data:application/pdf;base64," prefix is accepted); the size limit is
        checked before decoding."""
        if not isinstance(data, str) or not data.strip():
            raise BadRequest("no base64 data", code="empty")
        if data.lstrip().startswith("data:"):
            data = data.split(",", 1)[1] if "," in data else ""
        limit = self.config.max_upload_mb * 1024 * 1024
        compact = "".join(data.split())
        if len(compact) > (limit // 3 + 1) * 4:
            raise PayloadTooLarge(UPLOAD_ERRORS["too_large"][1])
        try:
            raw = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError):
            raise BadRequest("the data is not valid base64", code="invalid_base64") from None
        return self.ingest(raw, filename, **kwargs)

    def list_inbox(self) -> dict:
        return {"files": self.storage.list_inbox()}

    def ingest_inbox(self, name: str, **kwargs) -> dict:
        """ingest() of a PDF placed in DATA_DIR/inbox (name: a file name in that folder, no path)."""
        try:
            path = self.storage.inbox_file(name)
        except UnsafePath:
            raise BadRequest("not a PDF file name in the inbox", code="invalid_name") from None
        except FileNotFoundError:
            raise NotFound("no such file in the inbox", code="file_not_found") from None
        try:
            with open(path, "rb") as fh:
                return self.ingest(fh, name, **kwargs)
        except OSError as exc:
            raise ServiceError(f"the inbox file cannot be read ({type(exc).__name__})", code="read_failed") from None

    def _check_password_here(self, password: str | None) -> None:
        if password and self.worker is None:
            raise BadRequest("this process runs no worker, so a PDF password would never reach the job: encrypted "
                             "PDFs must be uploaded through the REST API or the HTTP MCP endpoint, whose process "
                             "runs the worker that holds the password in memory", code="password_unsupported_here")

    def reprocess(self, doc_id: str, *, force: bool = False, password: str | None = None) -> dict:
        """Queue a reprocess of a stored document from its source.pdf when its result is stale (or force=True);
        the same dict as ingest(). password: of an encrypted source PDF, held like an upload's (needs the worker
        of this process; dropped when no job is queued)."""
        self._check_doc_id(doc_id)
        self._check_password_here(password)
        try:
            decision = self.registry.request_reprocess(self.storage, doc_id, pipeline_version=self.pipeline_version,
                                                       fingerprint=self.fingerprint, force=bool(force),
                                                       password=password or None)
        except RegistryNotFound:
            raise NotFound("no such document, or no stored source PDF", code="document_not_found") from None
        if decision.get("queued"):
            self._notify()
        return decision

    # ---------------------------------------------------------------- documents
    @staticmethod
    def _check_doc_id(doc_id: str) -> str:
        try:
            return check_doc_id(doc_id)
        except UnsafePath:
            raise BadRequest("doc_id must be the 64 hex digits of the PDF's SHA-256", code="invalid_id") from None

    def _record(self, doc_id: str) -> dict:
        self._check_doc_id(doc_id)
        doc = self.registry.get_document(doc_id, pipeline_version=self.pipeline_version, fingerprint=self.fingerprint)
        if doc is None:
            raise NotFound("no such document", code="document_not_found")
        return doc

    def list_documents(self, *, status: str | None = None, limit: int = 100, offset: int = 0) -> dict:
        if status is not None and status not in DOC_STATES:
            raise BadRequest(f"status must be one of {', '.join(DOC_STATES)}", code="invalid_status")
        limit, offset = max(0, min(int(limit), MAX_LIST)), max(0, int(offset))
        docs = self.registry.list_documents(status=status, limit=limit, offset=offset,
                                            pipeline_version=self.pipeline_version, fingerprint=self.fingerprint)
        return {"documents": docs, "limit": limit, "offset": offset}

    def get_document(self, doc_id: str) -> dict:
        """The registry record (status, pipeline version, stale, identifiers, quality summary, related and
        same-text documents, current job) plus the warnings of the served result and its files."""
        doc = self._record(doc_id)
        result_job = self.registry.get_job(doc["result_job_id"]) if doc["result_job_id"] else None
        doc["warnings"] = (result_job or {}).get("warnings", [])
        doc["files"] = self._files(doc_id)["files"] if doc["has_result"] else []
        return doc

    def delete_document(self, doc_id: str, *, purge_caches: bool = False) -> dict:
        """Delete the record, the jobs and the stored files. Conflict while a job of the document is running.
        purge_caches: also remove what the shared caches hold for this PDF (its conversion batches, its figure
        descriptions and its identity-model answers), so no trace of it is left under DATA_DIR; a later upload
        converts, describes and identifies afresh. Returns "purged": {"batches", "figures", "identity"} (counts)."""
        self._check_doc_id(doc_id)
        self._evict(doc_id)
        figure_key = self._figure_cache_key(doc_id) if purge_caches else None
        try:
            deleted = self.registry.delete_document(doc_id, self.storage)
        except DocumentBusy:
            raise Conflict("the document has a running job; delete it when the job has finished",
                           code="document_busy") from None
        except StorageError:
            raise ServiceError("the document files could not be deleted (in use); try again",
                               code="delete_failed") from None
        if not deleted:
            raise NotFound("no such document", code="document_not_found")
        self._evict(doc_id)
        out: dict[str, Any] = {"doc_id": doc_id, "deleted": True}
        if purge_caches:
            out["purged"] = self.storage.purge_caches(doc_id, figure_key)
        return out

    def _figure_cache_key(self, doc_id: str) -> str | None:
        """The figure cache's key for the document (figure_vlm.document_key of its stored Docling JSON; the identity
        cache's entries carry the same key), or None without a readable stored result (then only its conversion
        batches are purged)."""
        folder = self.storage.document_dir(doc_id)
        path = folder / "protocol.json" if folder else None
        if path is None or not path.is_file():
            return None
        try:
            from docling_core.types.doc import DoclingDocument

            from figure_vlm import document_key

            return document_key(DoclingDocument.load_from_json(path))
        except Exception as exc:  # noqa: BLE001 - the purge of batches still runs
            log.warning("figure cache key of %s not computed: %s at %s", doc_id[:12], *describe_exception(exc))
            return None

    # ---------------------------------------------------------------- jobs
    def get_job(self, job_id: str) -> dict:
        try:
            check_job_id(job_id)
        except UnsafePath:
            raise BadRequest("job_id must be 32 hex digits", code="invalid_id") from None
        job = self.registry.get_job(job_id)
        if job is None:
            raise NotFound("no such job", code="job_not_found")
        return job

    def list_jobs(self, *, doc_id: str | None = None, state: str | None = None, limit: int = 100) -> dict:
        """Jobs, newest first; of one document with doc_id (NotFound for a well-formed id without a document, as
        get_document), and/or in one state."""
        if doc_id is not None:
            self._record(doc_id)
        if state is not None and state not in JOB_STATES:
            raise BadRequest(f"state must be one of {', '.join(JOB_STATES)}", code="invalid_state")
        limit = max(0, min(int(limit), MAX_LIST))
        return {"jobs": self.registry.list_jobs(doc_id=doc_id, state=state, limit=limit), "limit": limit}

    def cancel_job(self, job_id: str) -> dict:
        """Cancel a queued job (a running job cannot be interrupted: Conflict). Returns the job."""
        job = self.get_job(job_id)
        cancelled = self.worker.cancel(job_id) if self.worker else self.registry.cancel_job(job_id, self.storage)
        if not cancelled:
            raise Conflict(f"the job is {job['state']}: only a queued job can be cancelled", code="not_cancellable")
        return self.get_job(job_id)

    # ---------------------------------------------------------------- files
    def _files(self, doc_id: str) -> dict:
        base = self.storage.document_dir(doc_id)
        if base is None:
            return {"files": [], "images": []}
        files = sorted(n for n in SAFE_FILES if base.joinpath(*n.split("/")).is_file())
        images = sorted(f"{d.name}/{f.name}" for d in base.iterdir() if d.is_dir() and is_image_dir(d.name)
                        for f in d.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES)
        return {"files": files, "images": images}

    def list_files(self, doc_id: str) -> dict:
        """{"doc_id", "files", "images", "aliases"}: what file() serves for this document (published result only).
        aliases: the short names of the listed files; file() accepts every alias, a missing file is
        file_not_found."""
        doc = self._record(doc_id)
        if not doc["has_result"]:
            raise Conflict(f"the document has no result yet (status {doc['status']})", code="not_ready")
        files = self._files(doc_id)
        aliases = {alias: name for alias, name in sorted(FILE_ALIASES.items()) if name in files["files"]}
        return {"doc_id": doc_id, **files, "aliases": aliases}

    def file(self, doc_id: str, name: str) -> tuple[Path, str, str]:
        """(path, media type, canonical name) of an exposed file of the published result: protocol.md / .json /
        .html, protocol.decisions.json, quality.json, chunks/*.jsonl|json (short names like "chunks.jsonl" or
        "decisions" are accepted, see FILE_ALIASES) and images/<file>."""
        doc = self._record(doc_id)
        if not doc["has_result"]:
            raise Conflict(f"the document has no result yet (status {doc['status']})", code="not_ready")
        canonical = FILE_ALIASES.get(name, name)
        try:
            path = self.storage.resolve_file(doc_id, canonical)
        except UnsafePath:
            raise NotFound("this file is not available: see list_files for the names served",
                           code="file_not_found") from None
        except FileNotFoundError:
            raise NotFound("the document has no such file", code="file_not_found") from None
        media = MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return path, media, canonical

    # ---------------------------------------------------------------- protocol tools
    @staticmethod
    def tools(style: str = "anthropic") -> dict:
        """The protocol tool definitions (protocol_api.tool_schemas): style "anthropic" or "openai"."""
        from protocol_api import tool_schemas

        if style not in ("anthropic", "openai"):
            raise BadRequest("style must be 'anthropic' or 'openai'", code="invalid_style")
        return {"tools": tool_schemas(style)}

    @staticmethod
    def tool_names() -> list[str]:
        from protocol_api import TOOLS

        return [t["name"] for t in TOOLS]

    def _evict(self, doc_id: str) -> None:
        with self._docs_guard:
            self._docs.pop(doc_id, None)

    def _slot(self, doc_id: str) -> _OpenDocument:
        doc = self._record(doc_id)
        if not doc["has_result"]:
            raise Conflict(f"the document has no result yet (status {doc['status']})", code="not_ready")
        key = (doc["result_job_id"], doc["published_at"])
        with self._docs_guard:
            slot = self._docs.get(doc_id)
            if slot is None or slot.key != key:
                slot = self._docs[doc_id] = _OpenDocument(key)
            self._docs.move_to_end(doc_id)
            while len(self._docs) > self.doc_cache_size:
                self._docs.popitem(last=False)
        return slot

    def call_tool(self, doc_id: str, name: str, args: dict | None = None) -> dict:
        """Run protocol tool `name` on the published result of doc_id (ProtocolDocument.call_tool). A tool error
        is raised as ServiceError: unknown tool / invalid arguments 400-404, no such section or table 404, search
        without embeddings 409, offline cache miss 503, OpenRouter failure 502."""
        names = self.tool_names()
        if name not in names:
            raise NotFound("unknown tool", code="unknown_tool", details={"tools": names})
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise BadRequest("tool arguments must be a JSON object", code="invalid_arguments")
        slot = self._slot(doc_id)
        with slot.lock:
            if slot.doc is None:
                slot.doc = self._open(doc_id)
            result = slot.doc.call_tool(name, args)
        if isinstance(result, dict) and "error" in result:
            raise self._tool_error(doc_id, name, result)
        return _without_paths(result, self._path_forms(doc_id))  # e.g. document_info's out_dir

    def _open(self, doc_id: str):
        from protocol_api import ProtocolDocument

        folder = self.storage.document_dir(doc_id)
        if folder is None:
            raise NotFound("the document has no stored result", code="document_not_found")
        try:
            return ProtocolDocument.open(folder, embed_fn=self.embed_fn, rerank_fn=self.rerank_fn)
        except Exception as exc:  # noqa: BLE001 - the message can quote paths or document text
            error_type, where = describe_exception(exc)
            log.error("opening document %s failed: %s at %s", doc_id, error_type, where)
            raise ServiceError(f"the stored result cannot be opened ({error_type})",
                               code="document_unreadable") from None

    def _path_forms(self, doc_id: str) -> list[tuple[str, str]]:
        """(path text, label) of the document folder and DATA_DIR in every spelling, longest first."""
        forms: dict[str, str] = {}
        for path, label in ((self.config.data_dir, "<data>"), (self.storage.document_dir(doc_id), "<document>")):
            if path is not None:
                for p in (Path(path), Path(path).resolve()):
                    forms[str(p)] = forms[p.as_posix()] = label
        return sorted(forms.items(), key=lambda f: -len(f[0]))

    def _clean(self, doc_id: str, message: str) -> str:
        """The message without the paths of the document folder or DATA_DIR."""
        return _without_paths(message, self._path_forms(doc_id))

    def _tool_error(self, doc_id: str, name: str, result: dict) -> ServiceError:
        error_type, message = result.get("error_type"), str(result.get("error") or "")
        details = {"tool": name}
        if error_type is None:  # an untyped error (call_tool's own checks are typed: UnknownTool / InvalidArguments)
            return BadRequest(self._clean(doc_id, message), code="invalid_arguments", details=details)
        if error_type in TOOL_ERRORS:
            cls, code = TOOL_ERRORS[error_type]
            if error_type == "SearchUnavailable":
                message = SEARCH_UNAVAILABLE.format(doc_id=doc_id)
            return cls(self._clean(doc_id, message), code=code, details=details)
        if error_type in NETWORK_ERRORS:
            return UpstreamError(f"{name}: the OpenRouter request failed ({error_type})", details=details)
        if error_type == "RuntimeError" and message.startswith("OpenRouter "):
            status = message.split(":", 1)[0]  # "OpenRouter 429"; the response body is not passed on
            return UpstreamError(f"{name}: {status}", details=details)
        if error_type == "RuntimeError" and message.startswith("OPENROUTER_API_KEY not set"):
            return Unavailable(f"{name} needs OPENROUTER_API_KEY, which is not set", code="no_api_key",
                               details=details)
        log.error("tool %s on %s failed: %s", name, doc_id, error_type)
        return ServiceError(f"{name} failed ({error_type})", code="tool_failed", details=details)


def _without_paths(value: Any, forms: list[tuple[str, str]]) -> Any:
    """`value` (a tool result or message) with every path text of `forms` replaced by its label."""
    if isinstance(value, str):
        for form, label in forms:
            if form in value:
                value = value.replace(form, label)
        return value
    if isinstance(value, dict):
        return {k: _without_paths(v, forms) for k, v in value.items()}
    if isinstance(value, list):
        return [_without_paths(v, forms) for v in value]
    return value


def _upload_error(reason: str) -> ServiceError:
    cls, message = UPLOAD_ERRORS.get(reason, (BadRequest, "the upload was rejected"))
    return cls(message, code=reason)
