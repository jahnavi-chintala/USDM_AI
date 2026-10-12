"""Storage layer: uploads, job scratch space, published results, shared caches and the inbox.

`Storage` is the interface the registry, the worker and the APIs code against; `LocalStorage(config)`
implements it on a local (or mounted) filesystem under config.data_dir:

  documents/<doc_id>/        the published result of a document (doc_id = SHA-256 of the PDF bytes)
  work/<job_id>/             scratch space of one job: source.pdf, then every output in the final layout
  cache/vlm/, cache/batches/ caches shared across documents (owned by the pipeline)
  inbox/                     PDFs placed for ingestion by path

A cloud adapter (S3/GCS/Azure) implements the same methods: work/ stays a local scratch directory on the
worker (the pipeline needs real paths), publish() uploads the workdir under a fresh prefix and then flips
the document's pointer, and the read side serves files from the bucket (open_file) or a local mirror
(document_dir).

Atomic publish. A directory cannot be renamed onto an existing one on Windows, and on Linux rename() only
replaces an empty directory, so the swap takes three renames inside documents/:
  1. work/<job_id>          -> documents/<doc_id>.new   (the complete new result)
  2. documents/<doc_id>     -> documents/<doc_id>.old   (only when a result exists)
  3. documents/<doc_id>.new -> documents/<doc_id>
  4. delete documents/<doc_id>.old
Readers keep the old result until step 3; document_dir() bridges the instant between 2 and 3 by returning
.new. A failure in step 2 or 3 is rolled back (workdir restored, old result in place) and re-raised. Each
rename is atomic, so a .new is always a complete result: after a crash, recover() rolls it forward and
removes a leftover .old. Files a Windows reader keeps open can block a rename for a moment: renames are
retried briefly there; an .old that cannot be deleted is moved aside as <doc_id>.trash-<id> and purged by
recover().
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Iterator

from service.config import ServiceConfig

DOC_ID_RE = re.compile(r"[0-9a-f]{64}")   # SHA-256 hex of the PDF bytes
JOB_ID_RE = re.compile(r"[0-9a-f]{32}")   # uuid4 hex
SOURCE_NAME = "source.pdf"
PUBLISH_RECORD = "publish.json"           # {job_id, doc_id, published_at, result}: written before publishing
ERROR_LOG = ("logs", "error.log")

# Files the APIs may serve from a document folder, plus images: <images dir>/<name>.<IMAGE_SUFFIXES>, where
# the images dir is "images" or "<stem>_images" (Docling writes protocol_images/ next to protocol.json).
SAFE_FILES = frozenset({
    "protocol.md", "protocol.json", "protocol.html", "protocol.decisions.json", "quality.json",
    "chunks/chunks.jsonl", "chunks/parents.jsonl", "chunks/sections.json", "chunks/tables.jsonl",
    "chunks/glossary.json", "chunks/stats.json",
})
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_IMAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,250}")
CACHE_GLOB = "*cache*.json"               # chunks/ caches carried into a reprocess (embeddings, queries, rerank)

PDF_MAGIC = b"%PDF-"
HEADER_WINDOW = 1024                      # like Acrobat: the header may follow up to 1 KB of junk
READ_CHUNK = 1 << 20


class StorageError(Exception):
    pass


class UploadRejected(StorageError):
    """An upload that is not accepted. reason: "empty" | "not_pdf" | "too_large" (save_upload), or
    "invalid_pdf" | "password_required" | "bad_password" (the registry, after opening the PDF)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class UnsafePath(StorageError, ValueError):
    """A doc_id, job_id or file name that is malformed, not exposed or would leave its folder."""


@dataclass(frozen=True)
class SavedUpload:
    job_id: str
    path: Path      # work/<job_id>/source.pdf
    sha256: str     # = doc_id
    size: int


def new_job_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def check_doc_id(doc_id: str) -> str:
    if not isinstance(doc_id, str) or not DOC_ID_RE.fullmatch(doc_id):
        raise UnsafePath("invalid doc_id")
    return doc_id


def check_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise UnsafePath("invalid job_id")
    return job_id


def safe_relative_name(name: str) -> str:
    """`name` if it is an exposed file of a document folder (posix separators), else UnsafePath.

    Exposed: SAFE_FILES, and <images dir>/<file>.<image suffix> one level deep ("images" or "<stem>_images").
    Never "..", ".", empty parts, backslashes, drive letters, absolute paths, control characters or NUL.
    """
    if (not isinstance(name, str) or not name or len(name) > 300 or "\\" in name or ":" in name
            or name.startswith("/") or not name.isprintable()):
        raise UnsafePath("file name not allowed")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise UnsafePath("file name not allowed")
    if name in SAFE_FILES:
        return name
    if (len(parts) == 2 and is_image_dir(parts[0]) and _IMAGE_NAME.fullmatch(parts[1])
            and parts[1].lower().endswith(IMAGE_SUFFIXES)):
        return name
    raise UnsafePath("file not exposed")


def is_image_dir(name: str) -> bool:
    """"images" or "<stem>_images" (one path component, not hidden)."""
    return name == "images" or (name.endswith("_images") and not name.startswith(".") and len(name) <= 200)


def _iter_chunks(source) -> Iterator[bytes]:
    """bytes, a binary file-like object (.read) or an iterable of bytes chunks."""
    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        for i in range(0, len(data), READ_CHUNK):
            yield data[i:i + READ_CHUNK]
    elif hasattr(source, "read"):
        while True:
            chunk = source.read(READ_CHUNK)
            if not chunk:
                break
            yield chunk
    else:
        for chunk in source:
            if chunk:
                yield bytes(chunk)


class Storage(abc.ABC):
    """What the service needs from storage. All ids are validated (doc_id: 64 hex, job_id: 32 hex)."""

    # ---- jobs (scratch space; always local)
    @abc.abstractmethod
    def workdir(self, job_id: str) -> Path:
        """Local path of the job's scratch directory (it may not exist)."""

    @abc.abstractmethod
    def create_workdir(self, job_id: str) -> Path:
        """Create (if needed) and return the job's scratch directory."""

    @abc.abstractmethod
    def remove_workdir(self, job_id: str) -> None:
        """Delete the job's scratch directory (no error when missing)."""

    @abc.abstractmethod
    def list_workdirs(self) -> list[str]:
        """job_ids that have a scratch directory."""

    @abc.abstractmethod
    def remove_orphan_workdirs(self, known_job_ids: Iterable[str], min_age: float = 3600.0) -> int:
        """Delete scratch directories of unknown jobs older than min_age seconds; returns how many."""

    @abc.abstractmethod
    def has_source(self, job_id: str) -> bool:
        """True when work/<job_id>/source.pdf exists."""

    @abc.abstractmethod
    def save_upload(self, job_id: str, source, *, max_bytes: int | None = None) -> SavedUpload:
        """Stream bytes / a binary file object / an iterable of chunks to work/<job_id>/source.pdf, computing
        SHA-256 and size on the way; UploadRejected ("empty", "not_pdf", "too_large") leaves nothing behind."""

    @abc.abstractmethod
    def seed_reprocess(self, doc_id: str, job_id: str) -> bool:
        """Copy the published source.pdf and chunk caches into a new workdir; False without a source."""

    @abc.abstractmethod
    def seed_caches(self, doc_id: str, job_id: str) -> list[str]:
        """Copy documents/<doc_id>/chunks/*cache*.json into work/<job_id>/chunks/; returns the names."""

    @abc.abstractmethod
    def write_error_log(self, job_id: str, text: str) -> Path:
        """Append a failure's full traceback to work/<job_id>/logs/error.log (local only, never served)."""

    @abc.abstractmethod
    def write_publish_record(self, job_id: str, record: dict) -> None:
        """Write work/<job_id>/publish.json (job id + PipelineResult) before publish(), for crash recovery."""

    @abc.abstractmethod
    def publish(self, job_id: str, doc_id: str) -> None:
        """Move work/<job_id> into documents/<doc_id>, atomically replacing a previous result."""

    @abc.abstractmethod
    def recover(self) -> list[str]:
        """Finish or undo publishes interrupted by a crash; returns the doc_ids touched."""

    # ---- documents (read side)
    @abc.abstractmethod
    def document_dir(self, doc_id: str) -> Path | None:
        """Local folder of the published result, or None."""

    @abc.abstractmethod
    def resolve_file(self, doc_id: str, name: str) -> Path:
        """Local path of an exposed file (see safe_relative_name); UnsafePath or FileNotFoundError."""

    @abc.abstractmethod
    def read_publish_record(self, doc_id: str) -> dict | None:
        """documents/<doc_id>/publish.json, or None."""

    @abc.abstractmethod
    def delete_document(self, doc_id: str) -> bool:
        """Delete the published result (and leftovers of a swap); True when something was there."""

    @abc.abstractmethod
    def list_documents(self) -> list[str]:
        """doc_ids that have a published folder."""

    # ---- inbox
    @abc.abstractmethod
    def list_inbox(self) -> list[str]:
        """Names of the PDFs in inbox/ (top level only)."""

    @abc.abstractmethod
    def inbox_file(self, name: str) -> Path:
        """Local path of inbox/<name> (one path component, .pdf); UnsafePath or FileNotFoundError."""

    # ---- conveniences built on the above
    def has_document(self, doc_id: str) -> bool:
        return self.document_dir(doc_id) is not None

    def open_file(self, doc_id: str, name: str) -> BinaryIO:
        """An exposed file opened for binary reading (the caller closes it)."""
        return open(self.resolve_file(doc_id, name), "rb")


class LocalStorage(Storage):
    """Storage on the local filesystem under config.data_dir (work/ and documents/ on the same volume)."""

    def __init__(self, config: ServiceConfig, *, rename_attempts: int | None = None) -> None:
        self.config = config
        self.documents = config.documents_dir
        self.work = config.work_dir
        self.inbox = config.inbox_dir
        for d in (self.documents, self.work, config.cache_dir, config.vlm_cache_dir, config.batch_cache_dir,
                  self.inbox):
            d.mkdir(parents=True, exist_ok=True)
        # Windows: antivirus / indexer / a reader holding a file open make directory renames fail for a moment.
        self._rename_attempts = rename_attempts or (6 if os.name == "nt" else 1)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- helpers
    def _doc_lock(self, doc_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(doc_id, threading.Lock())

    def _paths(self, doc_id: str) -> tuple[Path, Path, Path]:
        base = self.documents / check_doc_id(doc_id)
        return base, self.documents / f"{doc_id}.new", self.documents / f"{doc_id}.old"

    def _rename(self, src: Path, dst: Path) -> None:
        """Rename src to a name that must not exist yet (Linux would silently replace an empty directory)."""
        if dst.exists():
            raise FileExistsError(f"{dst.name} already exists")
        for attempt in range(self._rename_attempts):
            try:
                os.rename(src, dst)
                return
            except PermissionError:
                if attempt + 1 >= self._rename_attempts:
                    raise
                time.sleep(0.05 * 2 ** attempt)

    def _discard(self, path: Path) -> bool:
        """Delete a tree; if open files keep part of it, move the rest aside as <doc_id>.trash-<id>."""
        if not path.exists():
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        trash = path.with_name(f"{path.name.split('.')[0]}.trash-{uuid.uuid4().hex[:8]}")
        try:
            os.rename(path, trash)
        except OSError:
            return False
        shutil.rmtree(trash, ignore_errors=True)
        return True

    def _recover_doc(self, doc_id: str) -> str | None:
        """Bring documents/<doc_id> back to a single folder after an interrupted publish (lock held)."""
        dst, new, old = self._paths(doc_id)
        action = None
        if new.is_dir():  # a complete new result: roll forward
            if dst.is_dir():
                if old.exists() and not self._discard(old):
                    raise StorageError("a previous backup of the document could not be removed")
                self._rename(dst, old)
            self._rename(new, dst)
            action = "rolled_forward"
        elif old.is_dir() and not dst.is_dir():  # only the backup is left: put it back
            self._rename(old, dst)
            action = "restored"
        if old.exists():
            self._discard(old)
            action = action or "cleaned"
        for trash in self.documents.glob(f"{doc_id}.trash-*"):
            shutil.rmtree(trash, ignore_errors=True)
        return action

    # ---- jobs
    def workdir(self, job_id: str) -> Path:
        return self.work / check_job_id(job_id)

    def create_workdir(self, job_id: str) -> Path:
        path = self.workdir(job_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def remove_workdir(self, job_id: str) -> None:
        shutil.rmtree(self.workdir(job_id), ignore_errors=True)

    def list_workdirs(self) -> list[str]:
        return sorted(p.name for p in self.work.iterdir() if p.is_dir() and JOB_ID_RE.fullmatch(p.name))

    def remove_orphan_workdirs(self, known_job_ids: Iterable[str], min_age: float = 3600.0) -> int:
        known, now, removed = set(known_job_ids), time.time(), 0
        for job_id in self.list_workdirs():
            path = self.workdir(job_id)
            try:
                old_enough = now - path.stat().st_mtime >= min_age
            except OSError:
                continue
            if job_id not in known and old_enough:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        return removed

    def has_source(self, job_id: str) -> bool:
        return (self.workdir(job_id) / SOURCE_NAME).is_file()

    def save_upload(self, job_id: str, source, *, max_bytes: int | None = None) -> SavedUpload:
        limit = max_bytes if max_bytes is not None else self.config.max_upload_mb * 1024 * 1024
        work = self.create_workdir(job_id)
        part, final = work / (SOURCE_NAME + ".part"), work / SOURCE_NAME
        digest, size, head, checked = hashlib.sha256(), 0, b"", False
        try:
            with open(part, "wb") as out:
                for chunk in _iter_chunks(source):
                    size += len(chunk)
                    if size > limit:
                        raise UploadRejected("too_large", f"larger than {limit} bytes")
                    if not checked:
                        head += chunk[:HEADER_WINDOW - len(head)]
                        if len(head) >= HEADER_WINDOW:
                            if PDF_MAGIC not in head:
                                raise UploadRejected("not_pdf", "no %PDF- header")
                            checked = True
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise UploadRejected("empty")
            if not checked and PDF_MAGIC not in head:
                raise UploadRejected("not_pdf", "no %PDF- header")
            os.replace(part, final)
        except BaseException:
            self.remove_workdir(job_id)
            raise
        return SavedUpload(job_id=job_id, path=final, sha256=digest.hexdigest(), size=size)

    def seed_reprocess(self, doc_id: str, job_id: str) -> bool:
        with self._doc_lock(doc_id):  # never while a publish of the same document swaps folders
            base = self.document_dir(doc_id)
            src = base / SOURCE_NAME if base else None
            if src is None or not src.is_file():
                return False
            work = self.create_workdir(job_id)
            part = work / (SOURCE_NAME + ".part")
            shutil.copyfile(src, part)
            os.replace(part, work / SOURCE_NAME)
            self._copy_caches(base, work)
        return True

    def seed_caches(self, doc_id: str, job_id: str) -> list[str]:
        with self._doc_lock(doc_id):
            base = self.document_dir(doc_id)
            return self._copy_caches(base, self.create_workdir(job_id)) if base else []

    @staticmethod
    def _copy_caches(base: Path, work: Path) -> list[str]:
        src = base / "chunks"
        if not src.is_dir():
            return []
        dst = work / "chunks"
        dst.mkdir(exist_ok=True)
        names = []
        for f in sorted(src.glob(CACHE_GLOB)):
            if f.is_file() and not f.is_symlink():
                try:
                    shutil.copy2(f, dst / f.name)
                except FileNotFoundError:  # a cache is an optimisation: a missing one is recomputed
                    continue
                names.append(f.name)
        return names

    def write_error_log(self, job_id: str, text: str) -> Path:
        path = self.workdir(job_id).joinpath(*ERROR_LOG)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"==== {utcnow()}\n{text}\n")
        return path

    def write_publish_record(self, job_id: str, record: dict) -> None:
        path = self.create_workdir(job_id) / PUBLISH_RECORD
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str), encoding="utf-8")

    def publish(self, job_id: str, doc_id: str) -> None:
        work = self.workdir(job_id)
        if not work.is_dir():
            raise FileNotFoundError(f"workdir of job {job_id} is missing")
        dst, new, old = self._paths(doc_id)
        with self._doc_lock(doc_id):
            self._recover_doc(doc_id)
            self._rename(work, new)                                   # 1
            had_old = dst.is_dir()
            if had_old:
                try:
                    self._rename(dst, old)                            # 2
                except Exception:
                    self._rename(new, work)
                    raise
            try:
                self._rename(new, dst)                                # 3
            except Exception:
                if had_old:
                    self._rename(old, dst)
                self._rename(new, work)
                raise
            if had_old:
                self._discard(old)                                    # 4

    def recover(self) -> list[str]:
        pattern = re.compile(r"([0-9a-f]{64})\.(new|old|trash-[0-9a-f]+)")
        doc_ids = sorted({m.group(1) for p in self.documents.iterdir() if (m := pattern.fullmatch(p.name))})
        touched = []
        for doc_id in doc_ids:
            with self._doc_lock(doc_id):
                if self._recover_doc(doc_id):
                    touched.append(doc_id)
        return touched

    # ---- documents
    def document_dir(self, doc_id: str) -> Path | None:
        dst, new, old = self._paths(doc_id)
        for path in (dst, new, old):  # new/old only exist during (or after a crash in) a publish
            if path.is_dir():
                return path
        return None

    def resolve_file(self, doc_id: str, name: str) -> Path:
        rel = safe_relative_name(name)
        base = self.document_dir(doc_id)
        if base is None:
            raise FileNotFoundError("document not found")
        real_base = base.resolve()
        real = base.joinpath(*PurePosixPath(rel).parts).resolve()
        if not real.is_relative_to(real_base):
            raise UnsafePath("file outside the document folder")
        if not real.is_file():
            raise FileNotFoundError(rel)
        return real

    def read_publish_record(self, doc_id: str) -> dict | None:
        base = self.document_dir(doc_id)
        try:
            return json.loads((base / PUBLISH_RECORD).read_text(encoding="utf-8")) if base else None
        except (OSError, ValueError):
            return None

    def delete_document(self, doc_id: str) -> bool:
        dst, new, old = self._paths(doc_id)
        with self._doc_lock(doc_id):
            existed = any(p.exists() for p in (dst, new, old))
            for path in (new, dst, old):
                if not self._discard(path):
                    raise StorageError("document folder could not be deleted (files in use)")
            for trash in self.documents.glob(f"{doc_id}.trash-*"):
                shutil.rmtree(trash, ignore_errors=True)
        return existed

    def list_documents(self) -> list[str]:
        return sorted(p.name for p in self.documents.iterdir() if p.is_dir() and DOC_ID_RE.fullmatch(p.name))

    # ---- inbox
    def list_inbox(self) -> list[str]:
        return sorted(p.name for p in self.inbox.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")

    def inbox_file(self, name: str) -> Path:
        if (not isinstance(name, str) or not name or "\x00" in name or "\\" in name or ":" in name
                or PurePosixPath(name).name != name or name in (".", "..") or not name.lower().endswith(".pdf")):
            raise UnsafePath("inbox file name not allowed")
        real_inbox = self.inbox.resolve()
        real = (self.inbox / name).resolve()
        if not real.is_relative_to(real_inbox):
            raise UnsafePath("file outside the inbox")
        if not real.is_file():
            raise FileNotFoundError(name)
        return real
