"""Self-test for the service core: storage, registry, worker and import (`python tests/test_service_core.py`).

Offline: a temporary DATA_DIR per test, synthetic PDFs written by hand (made-up text) and a fake pipeline
function in place of service.pipeline.process_document (which never runs here; the import reads its identifier
helper).
"""

import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service import import_existing as imp  # noqa: E402
from service import storage as st  # noqa: E402
from service.config import ROOT, ServiceConfig  # noqa: E402
from service.contracts import STAGES, PipelineResult, StageResult  # noqa: E402
from service.pipeline import IDENTIFIER_KEYS  # noqa: E402
from service.registry import (MAX_ATTEMPTS, DocumentBusy, Registry, inspect_pdf, inspect_pdf_in_process,  # noqa: E402
                              normalise_text, page_count, text_fingerprint)
from service.storage import LocalStorage, UnsafePath, UploadRejected  # noqa: E402
from service.worker import Worker  # noqa: E402

# ---------------------------------------------------------------- a minimal PDF writer (from test_text_reconcile)


def pdf_bytes(content: bytes, extra: bytes = b"") -> bytes:
    """One 612 x 792 page; F1 = Helvetica (WinAnsi). `extra` (a comment) changes the bytes, not the text."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n" + extra)
    offsets = []
    for n, obj in enumerate(objs, 1):
        offsets.append(buf.tell())
        buf.write(b"%d 0 obj\n" % n + obj + b"\nendobj\n")
    xref = buf.tell()
    buf.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        buf.write(b"%010d 00000 n \n" % off)
    buf.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return buf.getvalue()


def esc(s: str) -> bytes:
    b = s.encode("cp1252")
    return b"(" + b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def make_pdf(*lines: str, extra: bytes = b"") -> bytes:
    ops = [b"BT /F1 12 Tf 72 %d Td " % (720 - 16 * i) + esc(line) + b" Tj ET" for i, line in enumerate(lines)]
    return pdf_bytes(b"\n".join(ops), extra)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- fake pipelines

CURRENT = {"version": "1.0.0", "fingerprint": "fp-one"}
CALLS: list[dict] = []
CALLS_LOCK = threading.Lock()
SECRET = "Patient 0042 said something confidential"


def reset() -> None:
    CURRENT.update(version="1.0.0", fingerprint="fp-one")
    CALLS.clear()


def fake_pipeline(pdf_path, workdir, config, *, settings=None, progress=None, password=None):
    data = Path(pdf_path).read_bytes()
    seeded = (workdir / "chunks" / "embedding_cache.test.json").is_file()
    with CALLS_LOCK:
        CALLS.append({"sha": sha(data), "seeded": seeded, "password": password, "workdir": workdir.name})
    for i, stage in enumerate(STAGES, 1):
        if progress:
            progress(stage, i, len(STAGES))
    nct = re.search(rb"NCT\d{8}", data)
    sponsor = re.search(rb"Protocol No: ([A-Z0-9-]+)", data)
    run = f"{CURRENT['version']}/{CURRENT['fingerprint']}"
    (workdir / "chunks").mkdir(exist_ok=True)
    (workdir / "images").mkdir(exist_ok=True)
    (workdir / "protocol.md").write_text(f"# result {run}\n", encoding="utf-8")
    (workdir / "protocol.json").write_text(json.dumps({"run": run}), encoding="utf-8")
    (workdir / "chunks" / "chunks.jsonl").write_text('{"chunk_id": "c1"}\n', encoding="utf-8")
    (workdir / "chunks" / "embedding_cache.test.json").write_text(json.dumps({"run": run}), encoding="utf-8")
    (workdir / "images" / "image_000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return PipelineResult(
        status="done", pipeline_version=CURRENT["version"], fingerprint=CURRENT["fingerprint"], sha256=sha(data),
        pages=1, identifiers={"nct": nct.group(0).decode() if nct else None,
                              "sponsor_protocol_id": sponsor.group(1).decode() if sponsor else None,
                              "version": None, "date": None, "sources": {"nct": {"text": "quoted text"}}},
        quality={"score": 1.0}, warnings=[], stages=[StageResult(name=s, status="done") for s in STAGES])


def failing_pipeline(pdf_path, workdir, config, **kwargs):
    (workdir / "partial.txt").write_text("partial output", encoding="utf-8")
    raise ValueError(SECRET)


def library_failing_pipeline(pdf_path, workdir, config, **kwargs):
    return json.loads("{" + SECRET)  # fails inside the json package


def staged_failing_pipeline(pdf_path, workdir, config, *, progress=None, **kwargs):
    """Two stages complete, the third fails: the exception carries the stage list (service.contracts)."""
    for stage in ("hash", "convert", "postprocess"):
        progress(stage)
    exc = ValueError(SECRET)
    exc.pipeline_stages = [StageResult("hash", "done", 0.1), StageResult("convert", "done", 12.5, {"pages": 3}),
                           StageResult("postprocess", "failed", 0.4)]
    raise exc


def junk_stages_pipeline(pdf_path, workdir, config, **kwargs):
    exc = ValueError(SECRET)
    exc.pipeline_stages = "not a list of StageResult"
    raise exc


@contextmanager
def env(max_upload_mb: int = 100):
    tmp = Path(tempfile.mkdtemp(prefix="svc_core_"))
    config = ServiceConfig(data_dir=tmp / "data", max_upload_mb=max_upload_mb)
    storage = LocalStorage(config)
    registry = Registry(config, inspector=inspect_pdf_in_process)
    try:
        yield config, storage, registry
    finally:
        registry.close()
        shutil.rmtree(tmp, ignore_errors=True)


def worker(config, registry, storage, fn=fake_pipeline, **kwargs) -> Worker:
    return Worker(config, registry, storage, fn, poll_interval=0.05, **kwargs)


def ingest(registry, storage, data, name="upload.pdf", **kwargs) -> dict:
    return registry.ingest(storage, data, name, pipeline_version=CURRENT["version"],
                           fingerprint=CURRENT["fingerprint"], **kwargs)


def expect(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type as exc:
        return exc
    raise AssertionError(f"{exc_type.__name__} not raised by {fn.__name__}{args}")


def data_bytes(config) -> bytes:
    """Every byte stored under DATA_DIR except the work/ scratch (registry, WAL, documents, caches)."""
    return b"".join(p.read_bytes() for p in config.data_dir.rglob("*")
                    if p.is_file() and config.work_dir not in p.parents)


# ---------------------------------------------------------------- identity


def test_text_fingerprint() -> None:
    assert normalise_text("  \uff28\uff25\uff2c\uff2c\uff2f\u00a0World\n\t\ufb01ne ") == "hello world fine"
    with env() as (config, _, _):
        a, b, c, d, e = (config.data_dir / f"{n}.pdf" for n in "abcde")
        a.write_bytes(make_pdf("Same words here", "second line"))
        b.write_bytes(make_pdf("Same words here", "second line", extra=b"% other producer\n"))
        c.write_bytes(make_pdf("SAME words   here", "Second LINE"))
        d.write_bytes(make_pdf("Different words"))
        e.write_bytes(make_pdf())  # no text layer
        assert a.read_bytes() != b.read_bytes()
        fa = text_fingerprint(a)
        assert fa == text_fingerprint(b) == text_fingerprint(c) and len(fa) == 64
        assert text_fingerprint(d) != fa and text_fingerprint(e) is None
        assert page_count(a) == 1
        # the child-process inspection gives the same answer, and reports a broken PDF
        assert inspect_pdf(a) == {"pages": 1, "text_fingerprint": fa, "error": None}
        broken = config.data_dir / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4\nnot really a pdf\n")
        assert inspect_pdf(broken)["error"] == "invalid"


# ---------------------------------------------------------------- storage


def test_upload_checks() -> None:
    reset()
    with env(max_upload_mb=1) as (config, storage, registry):
        data = make_pdf("Upload check")
        saved = storage.save_upload("a" * 32, io.BytesIO(data))
        assert saved.sha256 == sha(data) and saved.size == len(data) and saved.path.read_bytes() == data
        assert saved.path == storage.workdir("a" * 32) / "source.pdf"
        assert storage.save_upload("b" * 32, [data[:10], data[10:]]).sha256 == saved.sha256  # chunk iterable
        storage.save_upload("c" * 32, b"\xef\xbb\xbf" + data)  # a few junk bytes before %PDF- are tolerated
        for job, source, reason in (("d" * 32, b"", "empty"), ("e" * 32, b"hello world", "not_pdf"),
                                    ("f" * 32, b"x" * 5000, "not_pdf"),
                                    ("0" * 32, b"%PDF-1.4\n" + b"0" * (1024 * 1024), "too_large")):
            assert expect(UploadRejected, storage.save_upload, job, source).reason == reason
            assert not storage.workdir(job).exists()
        assert storage.list_workdirs() == ["a" * 32, "b" * 32, "c" * 32]
        assert expect(UnsafePath, storage.save_upload, "../x", data)
        # a PDF header on bytes pdfium cannot open: rejected after the save, nothing registered or left behind
        assert expect(UploadRejected, ingest, registry, storage, b"%PDF-1.4\nnot a pdf\n").reason == "invalid_pdf"
        assert expect(UploadRejected, ingest, registry, storage, b"%PDF-1.4\n" + b"0" * (2 << 20)).reason == "too_large"
        assert registry.list_documents() == [] and len(storage.list_workdirs()) == 3


def test_safe_file_lookup() -> None:
    reset()
    with env() as (config, storage, registry):
        doc = ingest(registry, storage, make_pdf("Lookup"))["doc_id"]
        worker(config, registry, storage).run_once()
        assert storage.resolve_file(doc, "protocol.md").read_text(encoding="utf-8").startswith("# result")
        assert storage.resolve_file(doc, "images/image_000.png").is_file()
        with storage.open_file(doc, "chunks/chunks.jsonl") as fh:
            assert fh.read().startswith(b'{"chunk_id"')
        (storage.document_dir(doc) / "protocol_images").mkdir()
        (storage.document_dir(doc) / "protocol_images" / "image_000001_ab.png").write_bytes(b"png")
        assert storage.resolve_file(doc, "protocol_images/image_000001_ab.png").is_file()
        for name in ("../registry.sqlite", "..", ".", "", "chunks/../../registry.sqlite", "images/../protocol.md",
                     "./protocol.md", "/etc/passwd", "C:/Windows/win.ini", "C:\\Windows\\win.ini",
                     "chunks\\chunks.jsonl", "protocol.md\x00.png", "images/sub/a.png", "images/.hidden.png",
                     "images/a.svg", ".._images/a.png", "chunks/embeddings.npy", "publish.json", "source.pdf",
                     "logs/error.log", "PROTOCOL.MD", "//server/share/x.png"):
            expect(UnsafePath, storage.resolve_file, doc, name)
        for bad_id in ("../" + doc, doc.upper(), "x" * 64, doc[:-1]):
            expect(UnsafePath, storage.resolve_file, bad_id, "protocol.md")
        expect(FileNotFoundError, storage.resolve_file, doc, "quality.json")  # allowed but not written
        expect(FileNotFoundError, storage.resolve_file, "f" * 64, "protocol.md")  # unknown document
        try:  # a link inside the folder pointing outside it is refused too (needs symlink rights on Windows)
            os.symlink(config.registry_path, storage.document_dir(doc) / "images" / "link.png")
        except OSError:
            pass
        else:
            expect(UnsafePath, storage.resolve_file, doc, "images/link.png")
        # inbox
        (config.inbox_dir / "dropped.pdf").write_bytes(make_pdf("Inbox document"))
        (config.inbox_dir / "notes.txt").write_text("not a pdf")
        assert storage.list_inbox() == ["dropped.pdf"]
        for name in ("../registry.sqlite", "notes.txt", "sub/dropped.pdf", "..\\dropped.pdf", "C:dropped.pdf"):
            expect(UnsafePath, storage.inbox_file, name)
        expect(FileNotFoundError, storage.inbox_file, "missing.pdf")
        r = registry.ingest_file(storage, storage.inbox_file("dropped.pdf"), pipeline_version="1.0.0",
                                 fingerprint="fp-one")
        assert r["queued"] and registry.get_document(r["doc_id"])["filenames"] == ["dropped.pdf"]
        assert (config.inbox_dir / "dropped.pdf").is_file()


def _workdir_with(storage, text: str) -> str:
    job_id = st.new_job_id()
    (storage.create_workdir(job_id) / "protocol.md").write_text(text, encoding="utf-8")
    return job_id


def _served(storage, doc: str) -> str:
    return storage.resolve_file(doc, "protocol.md").read_text(encoding="utf-8")


def test_publish_keeps_old_result_until_swap() -> None:
    with env() as (config, storage, _):
        doc = "ab" * 32
        storage.publish(_workdir_with(storage, "v1"), doc)
        assert _served(storage, doc) == "v1"
        seen, original = [], storage._rename

        def spy(src, dst):
            before = _served(storage, doc)
            original(src, dst)
            seen.append((before, _served(storage, doc)))

        storage._rename = spy
        job = _workdir_with(storage, "v2")
        storage.publish(job, doc)
        # readers see v1 until the old folder is moved aside, then the complete new one: never nothing
        assert seen == [("v1", "v1"), ("v1", "v2"), ("v2", "v2")], seen
        assert _served(storage, doc) == "v2" and not storage.workdir(job).exists()
        assert sorted(os.listdir(config.documents_dir)) == [doc]


def test_publish_failure_rolls_back() -> None:
    with env() as (config, storage, _):
        doc = "cd" * 32
        storage.publish(_workdir_with(storage, "v1"), doc)
        original = storage._rename
        for failing_call in (2, 3):  # moving the old result aside / moving the new one in
            job = _workdir_with(storage, "v2")
            calls = {"n": 0}

            def flaky(src, dst):
                calls["n"] += 1
                if calls["n"] == failing_call:
                    raise PermissionError("simulated: a file is in use")
                original(src, dst)

            storage._rename = flaky
            expect(PermissionError, storage.publish, job, doc)
            storage._rename = original
            assert _served(storage, doc) == "v1"
            assert (storage.workdir(job) / "protocol.md").read_text(encoding="utf-8") == "v2"
            assert sorted(os.listdir(config.documents_dir)) == [doc]
            storage.remove_workdir(job)


class Crash(BaseException):
    """A process dying mid-publish: not an Exception, so no rollback code runs."""


def test_publish_crash_recovery() -> None:
    with env() as (config, storage, _):
        doc = "ef" * 32
        storage.publish(_workdir_with(storage, "v1"), doc)
        original, previous = storage._rename, "v1"
        for crash_at in (2, 3):  # after step 1 (.new + old folder) / after step 2 (.new + .old, no folder)
            job = _workdir_with(storage, f"new{crash_at}")
            calls = {"n": 0}

            def dying(src, dst):
                calls["n"] += 1
                if calls["n"] == crash_at:
                    raise Crash()
                original(src, dst)

            storage._rename = dying
            expect(Crash, storage.publish, job, doc)
            storage._rename = original
            assert f"{doc}.new" in os.listdir(config.documents_dir)
            assert _served(storage, doc) == (previous if crash_at == 2 else f"new{crash_at}")
            fresh = LocalStorage(config)
            assert fresh.recover() == [doc]
            assert _served(fresh, doc) == f"new{crash_at}" and sorted(os.listdir(config.documents_dir)) == [doc]
            previous = f"new{crash_at}"
        # a leftover backup next to a complete folder is simply removed
        (config.documents_dir / f"{doc}.old").mkdir()
        assert storage.recover() == [doc] and sorted(os.listdir(config.documents_dir)) == [doc]


# ---------------------------------------------------------------- upload decision


def test_duplicate_identical_and_in_progress() -> None:
    reset()
    with env() as (config, storage, registry):
        w = worker(config, registry, storage)
        data = make_pdf("Identical document", "NCT00000001")
        r1 = ingest(registry, storage, data, "first.pdf")
        assert r1["queued"] and not r1["duplicate"] and not r1["stale"] and r1["status"] == "queued"
        assert r1["doc_id"] == sha(data) and r1["warnings"] == []
        r2 = ingest(registry, storage, data, "C:\\Users\\someone\\second.pdf")  # queued: the same job
        assert r2["duplicate"] and r2["job_id"] == r1["job_id"] and not r2["queued"] and r2["status"] == "queued"
        job = registry.claim_next_job()
        r3 = ingest(registry, storage, data, "third.pdf")  # running: the same job
        assert r3["duplicate"] and r3["job_id"] == r1["job_id"] and r3["status"] == "running"
        assert w._run(job) == "done"
        r4 = ingest(registry, storage, data, "first.pdf")
        assert r4 == {"doc_id": r1["doc_id"], "job_id": None, "status": "done", "duplicate": True, "stale": False,
                      "queued": False, "warnings": []}, r4
        doc = registry.get_document(r1["doc_id"], pipeline_version="1.0.0", fingerprint="fp-one")
        assert doc["filenames"] == ["first.pdf", "second.pdf", "third.pdf"]
        assert doc["status"] == "done" and doc["has_result"] and doc["stale"] is False and doc["pages"] == 1
        assert doc["identifiers"]["nct"] == "NCT00000001" and "sources" not in doc["identifiers"]
        assert doc["quality"] == {"score": 1.0} and doc["current_job"]["state"] == "done"
        assert len(registry.list_jobs(doc_id=r1["doc_id"])) == 1 and len(CALLS) == 1
        assert storage.list_workdirs() == []  # duplicates leave no scratch behind
        j = registry.get_job(r1["job_id"])
        assert j["stage"] == "quality" and j["progress"] == {"done": 8, "total": 8} and j["attempts"] == 1
        assert [s["name"] for s in j["stages"]] == list(STAGES) and j["error"] is None


def test_stale_and_reprocess() -> None:
    reset()
    with env() as (config, storage, registry):
        w = worker(config, registry, storage)
        data = make_pdf("Stale document")
        doc_id = ingest(registry, storage, data)["doc_id"]
        w.run_until_idle()
        CURRENT.update(version="1.1.0", fingerprint="fp-two")
        r = ingest(registry, storage, data)
        assert r["stale"] and r["duplicate"] and r["job_id"] is None and r["status"] == "done"
        assert registry.get_document(doc_id, pipeline_version="1.1.0", fingerprint="fp-two")["stale"] is True
        r = ingest(registry, storage, data, reprocess=True)
        assert r["queued"] and r["stale"] and r["duplicate"] and r["status"] == "queued"
        job = registry.get_job(r["job_id"])
        assert job["reprocess"] and job["kind"] == "reprocess"
        assert (storage.workdir(r["job_id"]) / "chunks" / "embedding_cache.test.json").is_file()  # caches seeded
        assert "1.0.0/fp-one" in _served(storage, doc_id)  # the old result is still served
        again = ingest(registry, storage, data, reprocess=True)
        assert again["job_id"] == r["job_id"] and again["duplicate"] and not again["queued"]
        w.run_until_idle()
        assert CALLS[-1]["seeded"] is True and "1.1.0/fp-two" in _served(storage, doc_id)
        doc = registry.get_document(doc_id, pipeline_version="1.1.0", fingerprint="fp-two")
        assert doc["stale"] is False and doc["pipeline_version"] == "1.1.0" and doc["result_job_id"] == r["job_id"]
        # reprocess from the stored source (no upload): nothing to do when current, unless forced
        r5 = registry.request_reprocess(storage, doc_id, pipeline_version="1.1.0", fingerprint="fp-two")
        assert r5["job_id"] is None and r5["duplicate"] and not r5["queued"]
        r6 = registry.request_reprocess(storage, doc_id, pipeline_version="1.1.0", fingerprint="fp-two", force=True)
        assert r6["queued"] and storage.has_source(r6["job_id"])
        w.run_until_idle()
        assert registry.get_job(r6["job_id"])["state"] == "done" and storage.list_workdirs() == []
        assert len(registry.list_jobs(doc_id=doc_id)) == 3


def test_failed_job_and_retry() -> None:
    reset()
    with env() as (config, storage, registry):
        data = make_pdf("Failing document")
        r = ingest(registry, storage, data)
        assert worker(config, registry, storage, failing_pipeline).run_once() == r["job_id"]
        job = registry.get_job(r["job_id"])
        where = job["error"]["where"]
        assert job["state"] == "failed" and job["error"]["type"] == "ValueError", job
        assert where.startswith("tests/test_service_core.py:"), where
        line = Path(__file__).read_text(encoding="utf-8").splitlines()[int(where.rsplit(":", 1)[1]) - 1]
        assert "raise ValueError(SECRET)" in line, line
        doc = registry.get_document(r["doc_id"])
        assert doc["status"] == "failed" and not doc["has_result"] and storage.document_dir(r["doc_id"]) is None
        assert doc["error"] == {"type": "ValueError", "where": where, "job_id": r["job_id"]}
        assert job["stages"] == []  # failed before its first stage
        error_log = storage.workdir(r["job_id"]) / "logs" / "error.log"
        assert SECRET in error_log.read_text(encoding="utf-8")  # the full traceback stays in the workdir only
        # an exception raised inside a library is placed at our innermost frame
        r_lib = ingest(registry, storage, make_pdf("Library failure"))
        worker(config, registry, storage, library_failing_pipeline).run_once()
        lib = registry.get_job(r_lib["job_id"])["error"]
        assert lib["type"] == "JSONDecodeError" and lib["where"].startswith("tests/test_service_core.py:"), lib
        assert SECRET.encode() not in data_bytes(config)  # never in SQLite (nor its WAL)
        # failed: the next upload queues a new job
        r2 = ingest(registry, storage, data)
        assert r2["queued"] and not r2["duplicate"] and r2["job_id"] != r["job_id"]
        worker(config, registry, storage).run_once()
        doc = registry.get_document(r["doc_id"])
        assert doc["status"] == "done" and doc["error"] is None and doc["has_result"]
        # a failed reprocess keeps the published result
        CURRENT.update(version="2.0.0", fingerprint="fp-three")
        r3 = ingest(registry, storage, data, reprocess=True)
        worker(config, registry, storage, failing_pipeline).run_once()
        doc = registry.get_document(r["doc_id"], pipeline_version="2.0.0", fingerprint="fp-three")
        assert doc["status"] == "failed" and doc["has_result"] and doc["stale"] and doc["error"]["job_id"] == r3["job_id"]
        assert "1.0.0/fp-one" in _served(storage, r["doc_id"])
        r4 = ingest(registry, storage, data)  # stale result served, no job unless reprocess
        assert r4["job_id"] is None and r4["stale"]


def test_failed_job_keeps_completed_stages() -> None:
    reset()
    with env() as (config, storage, registry):
        r = ingest(registry, storage, make_pdf("Staged failure"))
        assert worker(config, registry, storage, staged_failing_pipeline).run_once() == r["job_id"]
        job = registry.get_job(r["job_id"])
        assert job["state"] == "failed" and job["error"]["type"] == "ValueError", job
        assert job["error"]["where"].startswith("tests/test_service_core.py:"), job["error"]
        # the completed stages as a finished job records them, then the failing one
        assert job["stages"] == [
            {"name": "hash", "status": "done", "seconds": 0.1, "detail": {}},
            {"name": "convert", "status": "done", "seconds": 12.5, "detail": {"pages": 3}},
            {"name": "postprocess", "status": "failed", "seconds": 0.4, "detail": {}}], job["stages"]
        assert registry.get_document(r["doc_id"])["current_job"]["stages"] == job["stages"]
        assert SECRET.encode() not in data_bytes(config)  # the message stays out of SQLite
        # something that is not a list of StageResult is ignored; the failure is still recorded
        r2 = ingest(registry, storage, make_pdf("Junk stages"))
        worker(config, registry, storage, junk_stages_pipeline).run_once()
        junk = registry.get_job(r2["job_id"])
        assert junk["state"] == "failed" and junk["stages"] == [] and junk["error"]["type"] == "ValueError", junk
        # a job that fails before the pipeline starts has no stages either
        r3 = ingest(registry, storage, make_pdf("No source"))
        (storage.workdir(r3["job_id"]) / "source.pdf").unlink()
        worker(config, registry, storage).run_once()
        missing = registry.get_job(r3["job_id"])
        assert missing["state"] == "failed" and missing["error"]["type"] == "SourceMissing" and missing["stages"] == []


def test_same_text_warning() -> None:
    reset()
    with env() as (config, storage, registry):
        a = make_pdf("Same words here", "NCT00000002")
        b = make_pdf("Same words here", "NCT00000002", extra=b"% another producer\n")
        c = make_pdf("SAME  words here", "nct00000002")
        ra = ingest(registry, storage, a)
        rb = ingest(registry, storage, b)
        assert rb["doc_id"] != ra["doc_id"] and rb["queued"] and not rb["duplicate"]
        assert rb["warnings"] == [f"same text as {ra['doc_id']}"]
        assert registry.get_job(rb["job_id"])["warnings"] == rb["warnings"]
        rc = ingest(registry, storage, c)
        assert rc["warnings"] == [f"same text as {ra['doc_id']}", f"same text as {rb['doc_id']}"]
        worker(config, registry, storage).run_until_idle()
        assert ingest(registry, storage, a)["warnings"] == [f"same text as {rb['doc_id']}",
                                                            f"same text as {rc['doc_id']}"]
        doc_b = registry.get_document(rb["doc_id"])
        assert doc_b["same_text_as"] == [ra["doc_id"], rc["doc_id"]] and doc_b["status"] == "done"
        assert doc_b["related"] == []  # the same NCT with the same text is not "another version"
        assert len(registry.list_documents()) == 3  # never merged


def test_related_documents() -> None:
    reset()
    with env() as (config, storage, registry):
        ids = {k: ingest(registry, storage, make_pdf(*lines))["doc_id"] for k, lines in {
            "v1": ("Study ABC version 1", "NCT01234567"),
            "v2": ("Study ABC version 2, amended", "NCT01234567"),
            "s1": ("Protocol No: XYZ-001", "first wording"),
            "s2": ("Protocol No: XYZ-001", "second wording", "NCT01234567"),
            "other": ("Unrelated study", "NCT07654321"),
        }.items()}
        worker(config, registry, storage).run_until_idle()
        rel = {k: {d["doc_id"]: d["reasons"] for d in registry.related_documents(v)} for k, v in ids.items()}
        assert rel["v1"] == {ids["v2"]: ["same nct"], ids["s2"]: ["same nct"]}, rel["v1"]
        assert rel["s1"] == {ids["s2"]: ["same sponsor_protocol_id"]}, rel["s1"]
        assert rel["s2"] == {ids["v1"]: ["same nct"], ids["v2"]: ["same nct"],
                             ids["s1"]: ["same sponsor_protocol_id"]}, rel["s2"]
        assert rel["other"] == {}
        assert registry.get_document(ids["v2"])["related"] == [
            {"doc_id": d, "reasons": ["same nct"]} for d in sorted((ids["v1"], ids["s2"]))]
        assert registry.delete_document(ids["v2"], storage)
        assert ids["v2"] not in {d["doc_id"] for d in registry.related_documents(ids["v1"])}


# ---------------------------------------------------------------- jobs and worker


def test_claim_fifo_atomic_and_cancel() -> None:
    reset()
    with env() as (config, storage, registry):
        first = [ingest(registry, storage, make_pdf(f"FIFO {i}"))["job_id"] for i in range(3)]
        claimed = [registry.claim_next_job() for _ in range(3)]
        assert [j["job_id"] for j in claimed] == first and registry.claim_next_job() is None
        assert all(j["state"] == "running" and j["attempts"] == 1 for j in claimed)
        assert registry.get_document(claimed[0]["doc_id"])["status"] == "running"
        registry.update_progress(first[0], "figures", 2, 5)
        assert registry.get_job(first[0])["progress"] == {"done": 2, "total": 5}
        assert registry.get_job(first[0])["stage"] == "figures"
        assert not registry.cancel_job(first[0])  # running jobs are not cancelled
        # many threads claiming: every job exactly once
        many = [ingest(registry, storage, make_pdf(f"Claim {i}"))["job_id"] for i in range(12)]
        got, lock = [], threading.Lock()

        def grab():
            while job := registry.claim_next_job():
                with lock:
                    got.append(job["job_id"])

        threads = [threading.Thread(target=grab) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(got) == sorted(many) and len(set(got)) == len(got)
        # cancel a queued job: it never starts, its workdir is gone
        w = worker(config, registry, storage)
        r = ingest(registry, storage, make_pdf("Cancelled document"))
        assert w.cancel(r["job_id"]) and not w.cancel(r["job_id"])
        assert registry.get_job(r["job_id"])["state"] == "cancelled" and not storage.workdir(r["job_id"]).exists()
        assert registry.get_document(r["doc_id"])["status"] == "cancelled"
        assert w.run_once() is None and registry.get_job(r["job_id"])["state"] == "cancelled"
        r2 = ingest(registry, storage, make_pdf("Cancelled document"))  # cancelled: queued again
        assert r2["queued"] and r2["job_id"] != r["job_id"] and w.run_once() == r2["job_id"]


def test_restart_recovery() -> None:
    reset()
    with env() as (config, storage, registry):
        r = ingest(registry, storage, make_pdf("Interrupted document"))
        job = registry.claim_next_job()  # the process dies while this job runs
        registry.update_progress(job["job_id"], "convert", 3, 10)
        registry.close()
        registry2 = Registry(config, inspector=inspect_pdf_in_process)
        try:
            w = worker(config, registry2, LocalStorage(config))
            report = w.recover()
            assert report["requeued"] == [r["job_id"]] and report["failed"] == [], report
            j = registry2.get_job(r["job_id"])
            assert j["state"] == "queued" and j["progress"] == {"done": None, "total": None}
            assert registry2.get_document(r["doc_id"])["status"] == "queued"
            assert w.run_once() == r["job_id"]
            j = registry2.get_job(r["job_id"])
            assert j["state"] == "done" and j["attempts"] == 2
            # a job that keeps killing the process fails after MAX_ATTEMPTS starts
            r2 = ingest(registry2, w.storage, make_pdf("Crash loop document"))
            for attempt in range(1, MAX_ATTEMPTS + 1):
                assert registry2.claim_next_job()["attempts"] == attempt
                report = w.recover()
            assert report["failed"] == [r2["job_id"]]
            j = registry2.get_job(r2["job_id"])
            assert j["state"] == "failed" and j["error"] == {"type": "Interrupted", "where": None}
            assert w.run_once() is None
        finally:
            registry2.close()


def test_recovery_after_publish() -> None:
    reset()
    with env() as (config, storage, registry):
        w = worker(config, registry, storage)
        # 1. crash after publishing, before recording the result
        r = ingest(registry, storage, make_pdf("Published but not recorded"))
        registry.record_success = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("simulated crash"))
        assert w._run(registry.claim_next_job()) == "running"
        del registry.record_success
        assert registry.get_job(r["job_id"])["state"] == "running" and storage.document_dir(r["doc_id"])
        report = worker(config, registry, LocalStorage(config)).recover()
        assert report["recorded"] == [r["job_id"]] and report["requeued"] == [], report
        assert registry.get_job(r["job_id"])["state"] == "done" and registry.get_document(r["doc_id"])["has_result"]
        # 2. crash between the renames of the publish
        r2 = ingest(registry, storage, make_pdf("Crash during publish"))
        original, calls = storage._rename, {"n": 0}

        def dying(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                raise Crash()
            original(src, dst)

        storage._rename = dying
        expect(Crash, w._run, registry.claim_next_job())
        storage._rename = original
        assert storage.workdir(r2["job_id"]).is_dir()  # crash before step 1: the workdir is intact
        w2 = worker(config, registry, LocalStorage(config))
        assert w2.recover()["requeued"] == [r2["job_id"]]
        assert w2.run_once() == r2["job_id"] and registry.get_job(r2["job_id"])["state"] == "done"
        assert len(CALLS) == 3  # one pipeline run per job, plus the re-queued one


def test_worker_threads() -> None:
    reset()
    with env() as (config, storage, registry):
        datas = [make_pdf(f"Threaded document {i}") for i in range(6)]
        jobs = [ingest(registry, storage, d)["job_id"] for d in datas]
        w = worker(config, registry, storage, concurrency=3)
        assert w.start() is not None and w.alive
        w.notify()
        deadline = time.time() + 30
        while time.time() < deadline and any(registry.get_job(j)["state"] != "done" for j in jobs):
            time.sleep(0.05)
        w.stop(timeout=10)
        assert not w.alive
        assert [registry.get_job(j)["state"] for j in jobs] == ["done"] * 6
        assert sorted(c["sha"] for c in CALLS) == sorted(sha(d) for d in datas)  # each exactly once
        late = ingest(registry, storage, make_pdf("After stop"))
        time.sleep(0.2)
        assert registry.get_job(late["job_id"])["state"] == "queued"  # stopped: nothing claimed


def test_password_memory_only() -> None:
    reset()
    password = "pw-Very-Secret-4711"
    with env() as (config, storage, registry):
        data = make_pdf("Password document")
        r = ingest(registry, storage, data, password=password)
        assert len(registry.pdf_passwords) == 1 and password not in repr(registry.pdf_passwords)
        worker(config, registry, storage).run_once()
        assert CALLS[-1]["password"] == password and len(registry.pdf_passwords) == 0
        assert registry.get_job(r["job_id"])["state"] == "done"
        assert ingest(registry, storage, data, password=password)["job_id"] is None  # duplicate: nothing held
        r2 = ingest(registry, storage, make_pdf("Password, cancelled"), password=password)
        assert len(registry.pdf_passwords) == 1 and registry.cancel_job(r2["job_id"], storage)
        assert len(registry.pdf_passwords) == 0
        everything = b"".join(p.read_bytes() for p in config.data_dir.rglob("*") if p.is_file())
        assert password.encode() not in everything


def test_delete_document() -> None:
    reset()
    with env() as (config, storage, registry):
        w = worker(config, registry, storage)
        done = ingest(registry, storage, make_pdf("Delete me", "NCT09999999"))
        w.run_once()
        queued = ingest(registry, storage, make_pdf("Delete me while queued"))
        busy = ingest(registry, storage, make_pdf("Delete me while running"))
        assert registry.delete_document(queued["doc_id"], storage)  # its queued job can never start
        assert registry.get_job(queued["job_id"]) is None and not storage.workdir(queued["job_id"]).exists()
        job = registry.claim_next_job()
        assert job["job_id"] == busy["job_id"]
        expect(DocumentBusy, registry.delete_document, busy["doc_id"], storage)
        w._run(job)
        assert registry.delete_document(busy["doc_id"], storage)
        assert registry.delete_document(done["doc_id"], storage)
        assert registry.get_document(done["doc_id"]) is None and storage.document_dir(done["doc_id"]) is None
        assert registry.list_documents() == [] and registry.list_jobs() == [] and storage.list_documents() == []
        assert not registry.delete_document(done["doc_id"], storage)
        assert w.run_once() is None


# ---------------------------------------------------------------- import of existing outputs


def test_import_existing() -> None:
    reset()
    logging.getLogger("service.pipeline").setLevel(logging.ERROR)  # "document_info unavailable" is expected here
    with env() as (config, storage, registry):
        src = Path(tempfile.mkdtemp(prefix="svc_import_"))
        try:
            folder, pdf = src / "Synthetic_Study", src / "synthetic.pdf"
            folder.mkdir()
            pdf.write_bytes(make_pdf("Imported study", "NCT05555555"))
            doc_id = sha(pdf.read_bytes())
            (folder / "document.json").write_text(json.dumps({"pdf_name": "synthetic.pdf", "pdf_sha256": doc_id}))
            (folder / "synthetic.json").write_text(json.dumps(
                {"pictures": [{"image": {"uri": "synthetic_images\\image_000000_ab.png"}}]}))
            for suffix in (".md", ".html", ".decisions.json"):
                (folder / f"synthetic{suffix}").write_text("converted", encoding="utf-8")
            for sub, name, text in (("synthetic_images", "image_000000_ab.png", "png"),
                                    ("unused_images", "x.png", "png"), ("tables", "table_001_p001.csv", "a,b"),
                                    ("chunks", "chunks.jsonl", "{}"),
                                    ("chunks", "embedding_cache.test.json", "{}"),
                                    ("chunks", "stats.json", json.dumps({"document": {
                                        "protocol_id": "SYN-1", "version_label": "Amendment 2", "amendment": "2",
                                        "acronym": "SYN", "version_date": "1 January 2024",
                                        "identifiers": {"nct": "NCT05555555", "sponsor_protocol_id": "SYN-1",
                                                        "sources": {"nct": {"text": "quoted"}}}}}))):
                (folder / sub).mkdir(exist_ok=True)
                (folder / sub / name).write_text(text, encoding="utf-8")
            (folder / "convert.log").write_text("log", encoding="utf-8")
            out = imp.import_folder(config, folder, pdf=pdf, registry=registry, storage=storage)
            assert out["doc_id"] == doc_id and out["status"] == "done" and out["pipeline_version"] == "imported"
            doc = registry.get_document(doc_id, pipeline_version="1.0.0", fingerprint="fp-one")
            assert doc["stale"] is True and doc["has_result"] and doc["pages"] == 1
            assert doc["identifiers"]["nct"] == "NCT05555555" and doc["identifiers"]["version"] == "Amendment 2"
            # the pipeline's key set (service.pipeline.document_identifiers; here from stats.json: the folder's
            # protocol.json is not a document protocol_api can open)
            assert doc["identifiers"] == {
                "nct": "NCT05555555", "sponsor_protocol_id": "SYN-1", "eudract": None, "eu_ct": None, "ind": None,
                "who_utn": None, "version": "Amendment 2", "amendment": "2", "date": "1 January 2024",
                "acronym": "SYN", "sponsor": None}, doc["identifiers"]
            assert set(doc["identifiers"]) == set(IDENTIFIER_KEYS)
            assert doc["filenames"] == ["synthetic.pdf"] and doc["current_job"]["kind"] == "import"
            base = storage.document_dir(doc_id)
            for name in ("source.pdf", "protocol.json", "protocol.md", "protocol.html", "protocol.decisions.json",
                         "synthetic_images/image_000000_ab.png", "tables/table_001_p001.csv", "chunks/chunks.jsonl",
                         "logs/convert.log", "publish.json"):
                assert (base / name).is_file(), name
            assert not (base / "unused_images").exists() and not (base / "document.json").exists()
            assert storage.resolve_file(doc_id, "synthetic_images/image_000000_ab.png").is_file()
            # stale like any imported result: an upload of the same PDF can reprocess it with caches
            r = ingest(registry, storage, pdf.read_bytes(), reprocess=True)
            assert r["stale"] and r["queued"]
            assert (storage.workdir(r["job_id"]) / "chunks" / "embedding_cache.test.json").is_file()
            registry.cancel_job(r["job_id"], storage)
            expect(imp.ImportRefused, imp.import_folder, config, folder, pdf=pdf, registry=registry, storage=storage)
            out = imp.import_folder(config, folder, pdf=pdf, registry=registry, storage=storage, replace=True,
                                    version=("1.0.0", "fp-one"))
            assert registry.get_document(doc_id, pipeline_version="1.0.0", fingerprint="fp-one")["stale"] is False
            # private folders and PDFs are refused on their path alone
            for path in (ROOT / "output", ROOT / "output" / "x", ROOT / "output_tests" / "x",
                         ROOT / "test_protocols" / "x"):
                assert imp.is_private(path)
                expect(imp.ImportRefused, imp.import_folder, config, path, registry=registry, storage=storage)
            expect(imp.ImportRefused, imp.import_folder, config, folder, pdf=ROOT / "test_protocols" / "x.pdf",
                   registry=registry, storage=storage)
            assert not imp.is_private(ROOT / "output_public" / "x") and not imp.is_private(folder)
            assert imp.main(["output"]) == 2 and imp.main([str(ROOT / "output_tests" / "x")]) == 2
        finally:
            shutil.rmtree(src, ignore_errors=True)


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        start = time.time()
        try:
            t()
            print(f"ok {t.__name__} ({time.time() - start:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed} tests passed" if not failed else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
