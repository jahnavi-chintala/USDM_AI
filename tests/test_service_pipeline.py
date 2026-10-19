"""Self-test for the service pipeline: `python tests/test_service_pipeline.py` (no pytest, offline).

Covers: ServiceConfig from the environment and its defaults agreeing with the modules; figure descriptions
(figure_vlm.describe_missing) with a fake client: described and cached, retried then failed, offline, unsupported
model; the identifier set of a document folder (document_identifiers, also what the import records); the stages
a failed run keeps (in process and in the child process); the identity stage on a small made-up document (between
chunk and embed, its result keys, the skipped_offline / partial warnings, a failing stage function that only
warns); and an end-to-end process_document run on a public protocol (LZZT) from its cached conversion batches,
offline: every stage, the output layout, and chunks identical to output_public/. The end-to-end test is skipped
when output_public/LZZT_CDISC_Pilot has no batch cache; the one identity-stage test that chunks for real (the
others replace the chunk step) when the chunk tokenizer is not in the local Hugging Face cache.
"""

import json
import logging
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

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
    from service.config import (DEFAULT_EMBED_MODEL, DEFAULT_FIGURE_MODEL, DEFAULT_IDENTITY_MODEL,
                                DEFAULT_RERANK_MODEL, ServiceConfig)

    assert DEFAULT_FIGURE_MODEL == pipeline_version.DEFAULT_FIGURE_MODEL
    assert DEFAULT_IDENTITY_MODEL == pipeline_version.DEFAULT_IDENTITY_MODEL
    if not os.environ.get("EMBED_MODEL"):
        assert DEFAULT_EMBED_MODEL == embed_chunks.MODEL
    if not os.environ.get("RERANK_MODEL"):
        assert DEFAULT_RERANK_MODEL == retrieve.RERANK_MODEL
    with tempfile.TemporaryDirectory() as tmp:
        with env(DATA_DIR=tmp, OPENROUTER_OFFLINE="1", FIGURE_MODEL="", WORKER_CONCURRENCY="3",
                 MAX_UPLOAD_MB="7", LOG_LEVEL="debug", EMBED_MODEL=None, RERANK_MODEL=None, IDENTITY_MODEL=None):
            c = ServiceConfig.from_env()
        assert c.data_dir == Path(tmp).resolve() and c.offline and c.figure_model == ""
        assert (c.worker_concurrency, c.max_upload_mb, c.log_level) == (3, 7, "DEBUG")
        assert c.embed_model == DEFAULT_EMBED_MODEL and c.rerank_model == DEFAULT_RERANK_MODEL
        assert c.registry_path == c.data_dir / "registry.sqlite" and c.vlm_cache_dir == c.data_dir / "cache" / "vlm"
        assert "api_key_set" in c.public() and not any("KEY" in str(v) for v in c.public().values())
        with env(DATA_DIR=tmp, FIGURE_MODEL=None, OPENROUTER_OFFLINE=None, IDENTITY_MODEL=None):
            d = ServiceConfig.from_env()
        assert d.figure_model == DEFAULT_FIGURE_MODEL and not d.offline
        # the identity model: unset -> the default (on), set -> stripped, "" -> off; shared cache folder; in the
        # models the fingerprint covers and in what /version shows
        default = "openrouter:anthropic/claude-opus-5.5"
        assert c.identity_model == DEFAULT_IDENTITY_MODEL == default and d.identity_model == DEFAULT_IDENTITY_MODEL
        with env(DATA_DIR=tmp, IDENTITY_MODEL="  openrouter:test/identity  "):
            e = ServiceConfig.from_env()
        assert e.identity_model == "openrouter:test/identity"
        with env(DATA_DIR=tmp, IDENTITY_MODEL="  "):
            assert ServiceConfig.from_env().identity_model == ""
        assert e.identity_cache_dir == e.data_dir / "cache" / "identity"
        assert e.models()["identity"] == "openrouter:test/identity" and c.models()["identity"] == default
        assert e.public()["identity_model"] == "openrouter:test/identity" and c.public()["identity_model"] == default
        assert set(e.models()) == {"figure", "identity", "embedding", "reranker"}
        assert pipeline_version.pipeline_fingerprint(None, e.models()) != pipeline_version.pipeline_fingerprint(
            None, c.models())
        assert pipeline_version.fingerprint_inputs(None, e.models())["models"]["identity"] == e.identity_model


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
        # the identity model's cached answers (identity_llm.py's default cache of the public output), when there are
        # any: offline the stage then uses them (done); without, it keeps the rules (skipped_offline)
        cached_identity = sorted((src_out / "identity_cache").glob("*.json"))
        if cached_identity:
            config.identity_cache_dir.mkdir(parents=True, exist_ok=True)
            for entry in cached_identity:
                shutil.copy2(entry, config.identity_cache_dir / entry.name)
        events = []
        res = process_document(work / "source.pdf", work, config,
                               progress=lambda s, d=None, t=None: events.append(s))
        from service.contracts import STAGES

        assert [s.name for s in res.stages] == list(STAGES), [s.name for s in res.stages]
        assert list(dict.fromkeys(events)) == list(STAGES), events
        st = {s.name: s.status for s in res.stages}
        assert st["figures"] == "skipped_offline" and st["embed"] == "done" and st["convert"] == "done", st
        # the default identity model, offline: a cached answer is used, else the rules are kept with a warning
        assert st["identity"] == ("done" if cached_identity else "skipped_offline"), st
        assert any(w.startswith("identity:") for w in res.warnings) != bool(cached_identity), res.warnings
        assert res.status == "done_with_warnings" and any("OPENROUTER_OFFLINE" in w for w in res.warnings)
        assert res.pages == 97 and len(res.sha256) == 64 and res.identifiers["sponsor_protocol_id"] == "H2Q-MC-LZZT"
        from service.pipeline import IDENTIFIER_KEYS

        assert set(res.identifiers) == set(IDENTIFIER_KEYS), list(res.identifiers)
        assert res.identifiers["sponsor"] == "Eli Lilly and Company", res.identifiers["sponsor"]
        for name in ("protocol.json", "protocol.md", "protocol.html", "protocol.decisions.json", "quality.json",
                     "pipeline.json", "images", "tables", "logs/pipeline.log", "chunks/chunks.jsonl",
                     "chunks/embeddings.npy", "chunks/sections.json", "chunks/identity.json"):
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


def test_encrypted_pdf_converts_with_its_password() -> None:
    """An encrypted PDF converts with the right password (the batch subprocess gets it from Docling's backend
    options as a SecretStr: hybrid_backend must unwrap it for pdfium). Made-up content from scripts/e2e_lib.py;
    skipped where that is not present (the Docker image's test target)."""
    import importlib

    sys.path.insert(0, str(ROOT / "scripts"))
    try:  # a host-side helper, not a dependency of the image (hence no import statement)
        e2e_lib = importlib.import_module("e2e_lib")
    except ImportError:
        print("   (skipped: scripts/e2e_lib.py not present)")
        return
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    from convert_protocol import convert_batches

    with tempfile.TemporaryDirectory() as tmp:
        pdf = Path(tmp) / "locked.pdf"
        pdf.write_bytes(e2e_lib.make_pdf(e2e_lib.synthetic_protocol("ZX-4411", with_figure=False),
                                         user_password="right-pw"))
        log = Path(tmp) / "convert.log"
        doc, pages = convert_batches(pdf, Path(tmp) / "out", password="right-pw", subprocess_log=log)
        assert pages >= 1 and any("ZX-4411" in t.text for t in doc.texts), "converted text"
        try:
            convert_batches(pdf, Path(tmp) / "out2", password="wrong-pw", subprocess_log=log)
        except Exception as exc:  # noqa: BLE001 - pdfium refuses the wrong password
            assert "right-pw" not in str(exc)
        else:
            raise AssertionError("a wrong password must not convert")
        assert "right-pw" not in log.read_text(encoding="utf-8", errors="replace")


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
            assert [(s.name, s.status) for s in exc.pipeline_stages] == [("hash", "failed")]  # the child reports them
        else:
            raise AssertionError("expected PipelineFailed")
        assert (work / "logs" / "error.log").exists()
        assert "secret-pw" not in "".join(p.read_text(encoding="utf-8", errors="replace")
                                          for p in (work / "logs").glob("*"))


def test_failed_run_keeps_completed_stages() -> None:
    """process_document raises with the stages so far on the exception (the worker records them): the completed
    ones, then the failing one with status "failed". The conversion is replaced by a function that reports
    progress and fails; a stage reported earlier and never finished ran through."""
    import convert_protocol
    from service.config import ServiceConfig
    from service.pipeline import process_document

    def failing_after(*reported):
        def convert(src, out, *, progress=None, **kwargs):
            for stage in reported:
                progress(stage)
            raise ValueError("made-up message")
        return convert

    with tempfile.TemporaryDirectory() as tmp:
        config = ServiceConfig(data_dir=Path(tmp), offline=True, figure_model="")
        work = config.work_dir / "job1"
        work.mkdir(parents=True)
        (work / "source.pdf").write_bytes(b"%PDF-1.4\nmade-up bytes\n")
        real = convert_protocol.convert_document
        try:
            for reported, expected in (
                    (("convert",), [("hash", "done"), ("convert", "failed")]),
                    (("convert", "postprocess"), [("hash", "done"), ("convert", "done"), ("postprocess", "failed")]),
                    (("convert", "postprocess", "figures"), [("hash", "done"), ("convert", "done"),
                                                              ("postprocess", "done"), ("figures", "failed")])):
                convert_protocol.convert_document = failing_after(*reported)
                try:
                    process_document(work / "source.pdf", work, config)
                except ValueError as exc:
                    stages = exc.pipeline_stages
                else:
                    raise AssertionError("expected ValueError")
                assert [(s.name, s.status) for s in stages] == expected, stages
                assert all(s.seconds >= 0 for s in stages) and stages[0].detail == {}, stages
        finally:
            convert_protocol.convert_document = real
        # a failure in the first stage: the hash itself
        try:
            process_document(work / "missing.pdf", work, config)
        except FileNotFoundError as exc:
            assert [(s.name, s.status) for s in exc.pipeline_stages] == [("hash", "failed")]
        else:
            raise AssertionError("expected FileNotFoundError")


# ------------------------------------------------------------------ the identity stage (small made-up document)

class patched:
    """Temporarily replace module attributes: patched(module, name=value, ...)."""

    def __init__(self, module, **attrs):
        self.module, self.attrs, self.old = module, attrs, {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.old[k] = getattr(self.module, k)
            setattr(self.module, k, v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            setattr(self.module, k, v)


def _stub_chunk(json_path, chunks_dir, **kwargs):
    """A chunk_protocol.chunk_document that only makes the folder and returns the counts process_document reads:
    for the tests of the stage bookkeeping, which need no chunked document (and no tokenizer)."""
    Path(chunks_dir).mkdir(parents=True, exist_ok=True)
    return {"chunks": 1, "parents": {"parents": 0}}


def _identity_run(tmp: Path, name: str, *, identity=None, model: str = "", quality=None, chunk=None):
    """process_document on a made-up document, offline. The conversion is replaced by a function that saves the
    synthetic Docling document of tests/test_service_api.py, embedding by one that records whether
    chunks/identity.json exists by then, and the quality report by a fixed one (the made-up PDF has one page, the
    document six); the stage bookkeeping runs for real. `identity` replaces identity_llm.run_identity (None: the
    real one, which needs the real chunker), `quality` / `chunk` the quality report / chunking (None: the real
    chunker, which needs the chunk tokenizer; _stub_chunk, or a function that fails, for instance).
    Returns a namespace: result (PipelineResult) or error (the exception), events (progress stages), work,
    config, seen (identity: the (out_dir, kwargs) of each run_identity call, identity_json_at_embed)."""
    import types

    import chunk_protocol
    import convert_protocol
    import embed_chunks
    import identity_llm
    import validate_output
    from service.config import ServiceConfig
    from service.pipeline import process_document
    from test_service_api import synthetic_protocol
    from test_service_core import make_pdf

    config = ServiceConfig(data_dir=tmp / name, offline=True, figure_model="", identity_model=model)
    work = config.work_dir / "job1"
    work.mkdir(parents=True)
    (work / "source.pdf").write_bytes(make_pdf("Quillmore study protocol", "Made-up text for an identity test"))
    seen = {"identity": [], "identity_json_at_embed": None}
    run = types.SimpleNamespace(result=None, error=None, events=[], work=work, config=config, seen=seen)

    def convert(src, out, *, progress=None, **kwargs):
        for stage in ("convert", "postprocess", "figures", "save"):
            progress(stage)
        synthetic_protocol().save_as_json(out / "protocol.json")
        return {"stem": "protocol", "pages": 6, "tables": 1, "pictures": 0, "postprocess": {}, "figures": {}}

    real_identity = identity_llm.run_identity

    def run_identity(out_dir, **kwargs):
        seen["identity"].append((Path(out_dir), kwargs))
        return (identity or real_identity)(out_dir, **kwargs)

    def embed(chunks_path, model, progress=None):
        seen["identity_json_at_embed"] = (Path(chunks_path).parent / "identity.json").is_file()
        return {"embedded": 1}

    def quality_report(doc, pdf, decisions):
        return {"coverage": {"coverage": 1.0, "pages_below_threshold": 0}, "tables": {"tables": 1, "marks_missing": 0},
                "figures": {"pictures": 0}}

    with patched(convert_protocol, convert_document=convert), patched(embed_chunks, embed_document=embed), \
            patched(identity_llm, run_identity=run_identity), \
            patched(validate_output, quality_report=quality or quality_report), \
            patched(chunk_protocol, chunk_document=chunk or chunk_protocol.chunk_document):
        try:
            run.result = process_document(work / "source.pdf", work, config,
                                          progress=lambda s, d=None, t=None: run.events.append(s))
        except Exception as exc:  # noqa: BLE001 - the tests read the stages the exception carries
            run.error = exc
    return run


def test_identity_stage_between_chunk_and_embed() -> None:
    """The one run with the real chunker and the real run_identity. Rules only (IDENTITY_MODEL off, the default): the
    stage runs after chunk and before embed, with the configured model and the shared cache folder, and writes
    chunks/identity.json. Then a model set without network: the contract of the stage function - a status the
    pipeline knows, the rules record still written, a warning - without more (the fake-result tests below cover the
    texts per status)."""
    from service.contracts import STAGES
    from test_service_api import TOKENIZER, tokenizer_cached

    i = STAGES.index("chunk")
    assert STAGES[i:i + 3] == ("chunk", "identity", "embed")
    if not tokenizer_cached():
        print(f"   (skipped the run: tokenizer {TOKENIZER} not in the local Hugging Face cache)")
        return
    offline = "identity: the model step was skipped (OPENROUTER_OFFLINE is set); the rule values are kept"
    with tempfile.TemporaryDirectory() as tmp:
        run = _identity_run(Path(tmp), "rules")
        assert run.error is None, type(run.error)
        res = run.result
        assert [s.name for s in res.stages] == list(STAGES), [s.name for s in res.stages]
        assert list(dict.fromkeys(run.events)) == list(STAGES), run.events
        stage = next(s for s in res.stages if s.name == "identity")
        assert stage.status == "done" and stage.seconds >= 0, stage
        assert res.status == "done" and res.warnings == [], (res.status, res.warnings)
        assert (run.work / "chunks" / "identity.json").is_file() and run.seen["identity_json_at_embed"] is True
        assert run.seen["identity"] == [(run.work, {"model": "", "cache_dir": run.config.identity_cache_dir})]
        assert json.loads((run.work / "pipeline.json").read_text(encoding="utf-8"))["stages"][6]["name"] == "identity"
        # a model set, no network: offline without a cached answer is "skipped_offline"; a model id the stage cannot
        # use is some other non-"done" status. Either way the job finishes with one warning and a record.
        for name, model, statuses in (("offline", "openrouter:test/identity", ("skipped_offline",)),
                                      ("unusable", "test/identity", ("skipped_offline", "partial"))):
            run = _identity_run(Path(tmp), name, model=model)
            assert run.error is None, (name, type(run.error))
            res = run.result
            stage = next(s for s in res.stages if s.name == "identity")
            assert stage.status in statuses, (name, stage)
            assert set(stage.detail) <= {"model", "accepted", "confirmed", "kept_rules", "cache", "error", "reason"}
            assert len(res.warnings) == 1 and res.status == "done_with_warnings", (name, res.warnings)
            if stage.status == "skipped_offline":
                assert res.warnings == [offline], (name, res.warnings)
            else:
                assert res.warnings[0].startswith("identity: the model step failed (") and \
                    res.warnings[0].endswith("); the rule values are kept"), (name, res.warnings)
            assert (run.work / "chunks" / "identity.json").is_file() and run.seen["identity_json_at_embed"] is True


def test_identity_stage_results_and_warnings() -> None:
    """The result's other keys that are not None become the stage's detail; skipped_offline and partial are
    warnings (done_with_warnings) naming the fixed reason, else the error type, never more."""
    model = "openrouter:test/identity"
    counts = {"model": model, "accepted": 3, "confirmed": 2, "kept_rules": 4}
    cases = [
        ("hit", {"status": "done", **counts, "cache": "hit"}, "done", {**counts, "cache": "hit"}, None),
        ("offline", {"status": "skipped_offline", **counts, "cache": None}, "skipped_offline", counts,
         "identity: the model step was skipped (OPENROUTER_OFFLINE is set); the rule values are kept"),
        ("error", {"status": "partial", **counts, "cache": "miss", "error": "ConnectionError"}, "partial",
         {**counts, "cache": "miss", "error": "ConnectionError"},
         "identity: the model step failed (ConnectionError); the rule values are kept"),
        ("reason", {"status": "partial", **counts, "cache": "miss", "reason": "the answer was not valid"}, "partial",
         {**counts, "cache": "miss", "reason": "the answer was not valid"},
         "identity: the model step failed (the answer was not valid); the rule values are kept"),
        ("both", {"status": "partial", **counts, "cache": "miss", "error": "IdentityModelError",
                  "reason": "unsupported model"}, "partial",
         {**counts, "cache": "miss", "error": "IdentityModelError", "reason": "unsupported model"},  # the label wins
         "identity: the model step failed (unsupported model); the rule values are kept"),
        ("neither", {"status": "partial", **counts, "cache": None}, "partial", counts,
         "identity: the model step failed (unknown); the rule values are kept")]
    with tempfile.TemporaryDirectory() as tmp:
        for name, answer, status, detail, warning in cases:
            run = _identity_run(Path(tmp), name, identity=lambda out_dir, answer=answer, **kw: dict(answer),
                                model=model, chunk=_stub_chunk)
            assert run.error is None, (name, type(run.error))
            res = run.result
            stage = next(s for s in res.stages if s.name == "identity")
            assert (stage.status, stage.detail) == (status, detail), (name, stage)
            assert res.warnings == ([warning] if warning else []), (name, res.warnings)
            assert res.status == ("done_with_warnings" if warning else "done"), (name, res.status)
            assert run.seen["identity"] == [(run.work, {"model": model, "cache_dir": run.config.identity_cache_dir})]
            assert [s.name for s in res.stages].index("embed") == [s.name for s in res.stages].index("identity") + 1


def test_identity_stage_failure_is_a_warning() -> None:
    """run_identity raising does not fail the job: the stage is "failed" with the exception type only, a warning
    says so, the later stages run and the message (which can quote the document) is nowhere in the outputs."""
    secret = "Patient 0042 said something confidential"

    def failing(out_dir, **kwargs):
        raise ValueError(secret)

    with tempfile.TemporaryDirectory() as tmp:
        run = _identity_run(Path(tmp), "failing", identity=failing, model="openrouter:test/identity",
                            chunk=_stub_chunk)
        assert run.error is None, type(run.error)
        res = run.result
        st = {s.name: s for s in res.stages}
        assert (st["identity"].status, st["identity"].detail) == ("failed", {"error": "ValueError"}), st["identity"]
        assert st["embed"].status == "done" and st["quality"].status == "done", st
        assert res.status == "done_with_warnings"
        assert res.warnings == ["identity record not written (ValueError): document_info uses the rules"], res.warnings
        assert not (run.work / "chunks" / "identity.json").exists()
        everything = "".join(p.read_text(encoding="utf-8", errors="replace") for p in run.work.rglob("*")
                             if p.is_file() and p.suffix in (".json", ".log", ".md", ".jsonl"))
        assert secret not in everything and "ValueError" in everything
        log = (run.work / "logs" / "pipeline.log").read_text(encoding="utf-8")
        assert "identity stage failed: ValueError at tests/test_service_pipeline.py:" in log, log


def test_failures_around_identity_keep_the_stages() -> None:
    """A fatal error after the identity stage lists it as completed; one in chunking (before it) does not list it. A
    quality report that fails leaves no PDF handle open (Windows would keep source.pdf locked)."""
    def failing(*args, **kwargs):
        raise ValueError("made-up message")

    with tempfile.TemporaryDirectory() as tmp:
        run = _identity_run(Path(tmp), "early", chunk=failing)
        assert isinstance(run.error, ValueError) and run.result is None
        assert [(s.name, s.status) for s in run.error.pipeline_stages] == [
            ("hash", "done"), ("convert", "done"), ("postprocess", "done"), ("figures", "skipped"), ("save", "done"),
            ("chunk", "failed")], run.error.pipeline_stages
        assert run.seen["identity"] == []
        run = _identity_run(Path(tmp), "late", quality=failing, chunk=_stub_chunk,
                            identity=lambda out_dir, **kw: {"status": "done", "model": None, "accepted": 0,
                                                            "confirmed": 0, "kept_rules": 0, "cache": None})
        assert isinstance(run.error, ValueError) and run.result is None
        assert [(s.name, s.status) for s in run.error.pipeline_stages] == [
            ("hash", "done"), ("convert", "done"), ("postprocess", "done"), ("figures", "skipped"), ("save", "done"),
            ("chunk", "done"), ("identity", "done"), ("embed", "done"), ("quality", "failed")], \
            run.error.pipeline_stages
        (run.work / "source.pdf").unlink()  # PermissionError on Windows while the quality stage's handle is open


def test_isolated_failure_keeps_completed_stages() -> None:
    """Through the worker with the real child process: a file that is not a PDF hashes, then fails to convert; the
    job keeps the completed stage and the failing one, and only the exception type and our file:line as error."""
    from service.config import ServiceConfig
    from service.pipeline import process_document_isolated
    from service.registry import Registry
    from service.storage import LocalStorage
    from service.worker import Worker

    with tempfile.TemporaryDirectory() as tmp:
        config = ServiceConfig(data_dir=Path(tmp), offline=True, figure_model="")
        storage = LocalStorage(config)
        registry = Registry(config, inspector=lambda path, password: {"pages": 1, "text_fingerprint": None,
                                                                      "error": None})
        try:
            made_up = b"%PDF-1.4\nthis is not a pdf, only the header is\n"
            d = registry.ingest(storage, made_up, "broken.pdf", pipeline_version="1.0.0", fingerprint="fp")
            assert Worker(config, registry, storage, process_document_isolated).run_once() == d["job_id"]
            job = registry.get_job(d["job_id"])
            assert job["state"] == "failed" and job["error"]["where"], job
            assert [(s["name"], s["status"]) for s in job["stages"]] == [("hash", "done"), ("convert", "failed")], job
            assert all(isinstance(s["seconds"], float) for s in job["stages"]), job["stages"]
            assert "this is not a pdf" not in json.dumps(job)
        finally:
            registry.close()


def test_document_identifiers() -> None:
    """One key set for the pipeline and the import: from document_info, else from chunks/stats.json, else all None;
    the import of a folder records what document_identifiers returns."""
    from service.pipeline import IDENTIFIER_KEYS, document_identifiers

    keys = {"nct", "sponsor_protocol_id", "eudract", "eu_ct", "ind", "who_utn", "version", "amendment", "date",
            "acronym", "sponsor"}
    assert set(IDENTIFIER_KEYS) == keys and len(IDENTIFIER_KEYS) == len(keys)
    logged: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            logged.append(record.getMessage())

    handler, pipeline_log = Capture(), logging.getLogger("service.pipeline")
    pipeline_log.addHandler(handler)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            # fallback: stats.json only (there is no document to open); a field may be {"value": ...}
            stats = tmp / "stats_only"
            (stats / "chunks").mkdir(parents=True)
            (stats / "chunks" / "stats.json").write_text(json.dumps({"document": {
                "protocol_id": "SYN-1", "acronym": "SYN", "amendment": "2", "version_label": "Amendment 2",
                "version_date": "1 January 2024",
                "identifiers": {"nct": "NCT05555555", "eudract": {"value": "2020-000123-45"},
                                "sources": {"nct": {"text": "quoted"}}}}}), encoding="utf-8")
            got = document_identifiers(stats)
            assert got == {"nct": "NCT05555555", "sponsor_protocol_id": "SYN-1", "eudract": "2020-000123-45",
                           "eu_ct": None, "ind": None, "who_utn": None, "version": "Amendment 2", "amendment": "2",
                           "date": "1 January 2024", "acronym": "SYN", "sponsor": None}, got
            assert logged and "FileNotFoundError" in logged[0] and str(tmp) not in logged[0], logged  # type only
            # nothing readable: every key, all None
            (tmp / "empty").mkdir()
            (tmp / "broken" / "chunks").mkdir(parents=True)
            (tmp / "broken" / "chunks" / "stats.json").write_text("{not json", encoding="utf-8")
            for folder in ("empty", "broken", "missing"):
                assert document_identifiers(tmp / folder) == dict.fromkeys(IDENTIFIER_KEYS), folder
            # a folder protocol_api opens: the values of document_info (the synthetic protocol of test_service_api)
            from test_service_api import build_converted_folder, make_service, tokenizer_cached
            from test_service_core import make_pdf, sha

            if not tokenizer_cached():
                print("   (skipped the document_info part: the chunk tokenizer is not in the local cache)")
                return
            folder, pdf = tmp / "Quillmore", tmp / "quillmore.pdf"
            build_converted_folder(folder)
            pdf.write_bytes(make_pdf("Quillmore study protocol", "Made-up text for an identifier test"))
            expected = {"nct": None, "sponsor_protocol_id": "QM-2024-07", "eudract": None, "eu_ct": None, "ind": None,
                        "who_utn": None, "version": "Version 2.0", "amendment": None, "date": "3 March 2024",
                        "acronym": None, "sponsor": None}
            assert document_identifiers(folder) == expected
            from service.import_existing import import_folder

            service = make_service(tmp / "data", run_worker=False)
            try:
                out = import_folder(service.config, folder, pdf=pdf, version=("1.0.0", "fp"),
                                    registry=service.registry, storage=service.storage)
                assert out["doc_id"] == sha(pdf.read_bytes())
                assert service.get_document(out["doc_id"])["identifiers"] == expected
            finally:
                service.stop()
    finally:
        pipeline_log.removeHandler(handler)


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
            assert doc["identifiers"]["sponsor"] == "Eli Lilly and Company", doc["identifiers"]
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
