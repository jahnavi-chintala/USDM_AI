"""Self-test for the service pipeline: `python tests/test_service_pipeline.py` (no pytest, offline).

Covers: ServiceConfig from the environment and its defaults agreeing with the modules; figure descriptions
(figure_vlm.describe_missing) with a fake client: described and cached, retried then failed, offline, unsupported
model; and an end-to-end process_document run on a public protocol (LZZT) from its cached conversion batches,
offline: every stage, the output layout, and chunks identical to output_public/. The end-to-end test is skipped
when output_public/LZZT_CDISC_Pilot has no batch cache.
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

LZZT = "LZZT_CDISC_Pilot"


class env:
    """Temporarily set (value) or remove (None) environment variables."""

    def __init__(self, **values):
        self.values, self.old = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_config_from_env_and_defaults() -> None:
    import embed_chunks
    import pipeline_version
    import retrieve
    from service.config import (DEFAULT_EMBED_MODEL, DEFAULT_FIGURE_MODEL, DEFAULT_RERANK_MODEL,
                                ServiceConfig)

    assert DEFAULT_FIGURE_MODEL == pipeline_version.DEFAULT_FIGURE_MODEL
    if not os.environ.get("EMBED_MODEL"):
        assert DEFAULT_EMBED_MODEL == embed_chunks.MODEL
    if not os.environ.get("RERANK_MODEL"):
        assert DEFAULT_RERANK_MODEL == retrieve.RERANK_MODEL
    with tempfile.TemporaryDirectory() as tmp:
        with env(DATA_DIR=tmp, OPENROUTER_OFFLINE="1", FIGURE_MODEL="", WORKER_CONCURRENCY="3",
                 MAX_UPLOAD_MB="7", LOG_LEVEL="debug", EMBED_MODEL=None, RERANK_MODEL=None):
            c = ServiceConfig.from_env()
        assert c.data_dir == Path(tmp).resolve() and c.offline and c.figure_model == ""
        assert (c.worker_concurrency, c.max_upload_mb, c.log_level) == (3, 7, "DEBUG")
        assert c.embed_model == DEFAULT_EMBED_MODEL and c.rerank_model == DEFAULT_RERANK_MODEL
        assert c.registry_path == c.data_dir / "registry.sqlite" and c.vlm_cache_dir == c.data_dir / "cache" / "vlm"
        assert "api_key_set" in c.public() and not any("KEY" in str(v) for v in c.public().values())
        with env(DATA_DIR=tmp, FIGURE_MODEL=None, OPENROUTER_OFFLINE=None):
            d = ServiceConfig.from_env()
        assert d.figure_model == DEFAULT_FIGURE_MODEL and not d.offline


# ------------------------------------------------------------------ figure descriptions

def _figure_doc():
    from docling_core.types.doc import BoundingBox, CoordOrigin, DoclingDocument, ImageRef, ProvenanceItem, Size

    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    img = Image.new("RGB", (120, 80), (255, 255, 255))
    for x in range(10, 110):
        img.putpixel((x, 40), (0, 0, 0))
    d.add_picture(image=ImageRef.from_pil(img, dpi=72),
                  prov=ProvenanceItem(page_no=1, bbox=BoundingBox(l=100, t=600, r=400, b=400,
                                                                  coord_origin=CoordOrigin.BOTTOMLEFT),
                                      charspan=(0, 0)))
    return d


class FakeRunner:
    def __init__(self, repo: str, fail: int = 0):
        self.repo, self.fail, self.calls = repo, fail, 0

    def ask(self, image, prompt):
        self.calls += 1
        if self.calls <= self.fail:
            raise ConnectionError("simulated outage")
        return "A line chart with one horizontal line."


def test_describe_missing_with_fake_client() -> None:
    import figure_vlm as fv

    model = "openrouter:test/model"
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        doc = _figure_doc()
        runner = FakeRunner(model, fail=1)  # first call fails, the retry succeeds
        s = fv.describe_missing(doc, model, cache, runner=runner, retries=2, backoff=0)
        assert s["to_describe"] == 1 and s["described_now"] == 1 and s["failed"] == 0, s
        assert len(list(cache.glob("*.json"))) == 1
        s2 = fv.describe_missing(_figure_doc(), model, cache, runner=FakeRunner(model), backoff=0)
        assert s2["already_cached"] == 1 and s2["to_describe"] == 0, s2
        attached = fv.attach_cached(doc, model, cache)
        assert attached["described"] == 1 and "horizontal line" in doc.pictures[0].meta.description.text
        # a figure that keeps failing is counted, not fatal, and nothing is cached
        empty = Path(tmp) / "empty"
        s3 = fv.describe_missing(_figure_doc(), model, empty, runner=FakeRunner(model, fail=99), retries=1,
                                 backoff=0)
        assert s3["failed"] == 1 and s3["described_now"] == 0 and not list(empty.glob("*.json")), s3


def test_describe_missing_offline_and_unsupported() -> None:
    import figure_vlm as fv

    with tempfile.TemporaryDirectory() as tmp:
        with env(OPENROUTER_OFFLINE="1"):
            s = fv.describe_missing(_figure_doc(), "openrouter:test/model", Path(tmp))
        assert s["offline"] == 1 and s["described_now"] == 0, s
        s = fv.describe_missing(_figure_doc(), "local/model", Path(tmp))
        assert s["unsupported_model"] == 1, s


# ------------------------------------------------------------------ end to end (public protocol, offline)

def test_process_document_end_to_end_offline() -> None:
    src_out = ROOT / "output_public" / LZZT
    batches = [b for b in (src_out / "_chunks").glob("*/*") if (b / "manifest.json").exists()]
    if not batches:
        print("   (skipped: no cached batches for", LZZT, ")")
        return
    from service.config import ServiceConfig
    from service.pipeline import process_document

    with tempfile.TemporaryDirectory() as tmp:
        config = ServiceConfig(data_dir=Path(tmp), offline=True)
        for b in batches:
            shutil.copytree(b, config.batch_cache_dir / b.parent.name / b.name)
        work = config.work_dir / "job1"
        (work / "chunks").mkdir(parents=True)
        shutil.copy2(ROOT / "public_protocols" / LZZT / "protocol.pdf", work / "source.pdf")
        for cache in (src_out / "chunks").glob("embedding_cache.*.json"):
            shutil.copy2(cache, work / "chunks" / cache.name)  # what the worker does on a reprocess
        events = []
        res = process_document(work / "source.pdf", work, config,
                               progress=lambda s, d=None, t=None: events.append(s))
        from service.contracts import STAGES

        assert [s.name for s in res.stages] == list(STAGES), [s.name for s in res.stages]
        assert list(dict.fromkeys(events)) == list(STAGES), events
        st = {s.name: s.status for s in res.stages}
        assert st["figures"] == "skipped_offline" and st["embed"] == "done" and st["convert"] == "done", st
        assert res.status == "done_with_warnings" and any("OPENROUTER_OFFLINE" in w for w in res.warnings)
        assert res.pages == 97 and len(res.sha256) == 64 and res.identifiers["sponsor_protocol_id"] == "H2Q-MC-LZZT"
        for name in ("protocol.json", "protocol.md", "protocol.html", "protocol.decisions.json", "quality.json",
                     "pipeline.json", "images", "tables", "logs/pipeline.log", "chunks/chunks.jsonl",
                     "chunks/embeddings.npy", "chunks/sections.json"):
            assert (work / name).exists(), name
        quality = json.loads((work / "quality.json").read_text(encoding="utf-8"))
        assert quality["coverage"]["coverage"] > 0.99 and quality["figures"]["pictures"] == 11
        texts = lambda p: [json.loads(x)["text"] for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]  # noqa: E731
        assert texts(work / "chunks" / "chunks.jsonl") == texts(src_out / "chunks" / "chunks.jsonl")
        assert (work / "chunks" / "sections.json").read_text(encoding="utf-8") == \
            (src_out / "chunks" / "sections.json").read_text(encoding="utf-8")
        # chunk ids derive from the source PDF (recorded in protocol.json as Docling's origin), not from the JSON
        saved = json.loads((work / "protocol.json").read_text(encoding="utf-8"))
        pdf_hash = f"{int(res.sha256, 16) & 0xFFFFFFFFFFFFFFFF:016x}"
        assert saved["origin"]["filename"] == "source.pdf" and f"{saved['origin']['binary_hash']:016x}" == pdf_hash
        stats = json.loads((work / "chunks" / "stats.json").read_text(encoding="utf-8"))["document"]
        assert stats["source_pdf_hash"] == pdf_hash and stats["doc_key"] == f"doc-{pdf_hash[:12]}"
        ids = [json.loads(x)["chunk_id"] for x in (work / "chunks" / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
        assert ids and all(i.startswith(f"doc-{pdf_hash[:12]}-") for i in ids), ids[:2]
        # the document folder is self-contained: the toolset opens it
        from protocol_api import ProtocolDocument

        assert ProtocolDocument.open(work).document_info()["identifiers"]["sponsor_protocol_id"]["value"] == "H2Q-MC-LZZT"


def test_platform_independent_outputs() -> None:
    """Saved JSON / Markdown / HTML are byte-identical on Windows and Linux: LF, and "/" in image URIs."""
    from convert_protocol import platform_independent

    with tempfile.TemporaryDirectory() as tmp:
        js, md = Path(tmp) / "protocol.json", Path(tmp) / "protocol.md"
        js.write_bytes(b'{\r\n "a": {"uri": "protocol_images\\\\image_000.png"},\r\n "b": {"uri": "data:image/png;'
                       b'base64,AA\\\\"},\r\n "c": {"uri": "https://x.org/a\\\\b"}, "d": "keep\\\\this"\r\n}\r\n')
        md.write_bytes(b"# Title\r\n\r\nText with a\\\\backslash\r\n")
        html = Path(tmp) / "protocol.html"
        html.write_bytes(b'<p>a%5Cb</p>\r\n<figure><img alt="x" src="images%5Cimage_000.png"></figure>\r\n'
                         b'<img src="data:image/png;base64,%5C"><img src="https://x.org/a%5Cb">\r\n')
        platform_independent(html)
        assert html.read_bytes() == (b'<p>a%5Cb</p>\n<figure><img alt="x" src="images/image_000.png"></figure>\n'
                                     b'<img src="data:image/png;base64,%5C"><img src="https://x.org/a%5Cb">\n')
        platform_independent(js)
        platform_independent(md)
        assert js.read_bytes() == (b'{\n "a": {"uri": "protocol_images/image_000.png"},\n "b": {"uri": "data:image/'
                                   b'png;base64,AA\\\\"},\n "c": {"uri": "https://x.org/a\\\\b"}, "d": "keep\\\\this"\n}\n')
        assert json.loads(js.read_text(encoding="utf-8"))["a"]["uri"] == "protocol_images/image_000.png"
        assert md.read_bytes() == b"# Title\n\nText with a\\\\backslash\n"  # Markdown: line ends only
        before = js.stat().st_mtime_ns
        platform_independent(js)  # already normalised: not rewritten
        assert js.stat().st_mtime_ns == before


def test_doc_key_from_source_pdf() -> None:
    """The document key (prefix of chunk ids without protocol id + amendment) is the source PDF's: the same
    whatever the JSON bytes; a JSON without an origin falls back to the JSON's hash."""
    import hashlib

    from chunk_protocol import doc_metadata
    from docling_core.types.doc import DoclingDocument, DocumentOrigin

    pdf_sha = hashlib.sha256(b"made-up pdf bytes").hexdigest()
    with tempfile.TemporaryDirectory() as tmp:
        keys = []
        for name in ("one", "two"):  # different JSON content, same PDF
            doc = DoclingDocument(name=name)
            doc.add_text(label="text", text=f"Made-up text {name}.")
            doc.origin = DocumentOrigin(mimetype="application/pdf", binary_hash=pdf_sha, filename="source.pdf")
            path = Path(tmp) / f"{name}.json"
            doc.save_as_json(path)
            meta = doc_metadata(DoclingDocument.load_from_json(path), path)
            keys.append(meta["doc_key"])
            assert meta["source_pdf_hash"] == pdf_sha[-16:] and meta["source_sha256"] != pdf_sha[:16]
        assert keys[0] == keys[1] == f"doc-{pdf_sha[-16:][:12]}"
        legacy = DoclingDocument(name="legacy")
        legacy.add_text(label="text", text="Made-up text.")
        path = Path(tmp) / "legacy.json"
        legacy.save_as_json(path)
        meta = doc_metadata(DoclingDocument.load_from_json(path), path)
        assert meta["source_pdf_hash"] is None and meta["doc_key"] == f"doc-{meta['source_sha256'][:12]}"


def test_isolated_failure_reports_type_and_location_only() -> None:
    from service.config import ServiceConfig
    from service.pipeline import PipelineFailed, process_document_isolated
    from service.worker import describe_exception

    with tempfile.TemporaryDirectory() as tmp:
        config = ServiceConfig(data_dir=Path(tmp), offline=True, figure_model="")
        work = Path(tmp) / "work" / "j"
        work.mkdir(parents=True)
        try:
            process_document_isolated(work / "source.pdf", work, config, password="secret-pw")  # no such file
        except PipelineFailed as exc:
            etype, where = describe_exception(exc)
            assert etype == "FileNotFoundError" and where and where.split(":")[0].endswith(".py"), (etype, where)
            assert "source.pdf" not in str(exc)  # the message names type and location only
        else:
            raise AssertionError("expected PipelineFailed")
        assert (work / "logs" / "error.log").exists()
        assert "secret-pw" not in "".join(p.read_text(encoding="utf-8", errors="replace")
                                          for p in (work / "logs").glob("*"))


def test_service_end_to_end_isolated() -> None:
    """Upload -> registry decision -> worker -> isolated pipeline -> atomic publish -> registry; then the
    duplicate and reprocess rules on the real result. Public protocol (LZZT), offline, cached batches."""
    src_out = ROOT / "output_public" / LZZT
    batches = [b for b in (src_out / "_chunks").glob("*/*") if (b / "manifest.json").exists()]
    if not batches:
        print("   (skipped: no cached batches for", LZZT, ")")
        return
    from pipeline_version import PIPELINE_VERSION, pipeline_fingerprint
    from service.config import ServiceConfig
    from service.pipeline import process_document_isolated
    from service.registry import Registry
    from service.storage import LocalStorage
    from service.worker import Worker

    with tempfile.TemporaryDirectory() as tmp:
        config = ServiceConfig(data_dir=Path(tmp), offline=True)
        for b in batches:
            shutil.copytree(b, config.batch_cache_dir / b.parent.name / b.name)
        storage, registry = LocalStorage(config), Registry(config)
        version, fp = PIPELINE_VERSION, pipeline_fingerprint(None, config.models())
        pdf = ROOT / "public_protocols" / LZZT / "protocol.pdf"
        try:
            with open(pdf, "rb") as fh:
                d = registry.ingest(storage, fh, "lzzt.pdf", pipeline_version=version, fingerprint=fp)
            assert d["queued"] and d["job_id"] and not d["duplicate"], d
            worker = Worker(config, registry, storage, process_document_isolated)
            assert worker.run_until_idle() == [d["job_id"]]
            job = registry.get_job(d["job_id"])
            assert job["state"] == "done_with_warnings" and job["stage"] == "quality", job
            doc = registry.get_document(d["doc_id"], pipeline_version=version, fingerprint=fp)
            assert doc["status"] == "done_with_warnings" and doc["has_result"] and not doc["stale"], doc
            assert doc["pages"] == 97 and doc["identifiers"]["sponsor_protocol_id"] == "H2Q-MC-LZZT", doc
            folder = storage.document_dir(d["doc_id"])
            for name in ("source.pdf", "protocol.json", "quality.json", "chunks/chunks.jsonl"):
                assert (folder / name).exists(), name
            assert storage.resolve_file(d["doc_id"], "chunks/sections.json").exists()
            # the files the APIs serve never name a path on the server (DATA_DIR, the job's workdir)
            from service.storage import SAFE_FILES

            forms = {str(Path(tmp)), Path(tmp).as_posix(), str(Path(tmp).resolve()), Path(tmp).resolve().as_posix()}
            forms |= {json.dumps(f)[1:-1] for f in forms}
            for name in SAFE_FILES:
                if (folder / name).is_file():
                    text = (folder / name).read_text(encoding="utf-8", errors="replace")
                    assert not any(f in text for f in forms), f"{name} names a server path"
            figures = [f for line in (folder / "chunks" / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
                       if line.strip() for f in json.loads(line).get("figures", [])]
            assert figures and all((folder / f["image_path"]).is_file() for f in figures)  # relative to the folder
            # the same bytes again: the existing result, no job
            with open(pdf, "rb") as fh:
                again = registry.ingest(storage, fh, "copy.pdf", pipeline_version=version, fingerprint=fp)
            assert again["duplicate"] and again["job_id"] is None and not again["stale"], again
            # a newer pipeline: stale without reprocess, a job with it
            with open(pdf, "rb") as fh:
                stale = registry.ingest(storage, fh, "lzzt.pdf", pipeline_version="9.9.9", fingerprint="f" * 16)
            assert stale["stale"] and stale["job_id"] is None, stale
            with open(pdf, "rb") as fh:
                re = registry.ingest(storage, fh, "lzzt.pdf", pipeline_version="9.9.9", fingerprint="f" * 16,
                                     reprocess=True)
            assert re["queued"] and re["job_id"], re
        finally:
            registry.close()


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed} tests passed" if not failed else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
