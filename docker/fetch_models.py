"""Model files of the protocol service image: downloaded at build time, verified offline.

  python docker/fetch_models.py --download             # build stage, network on (HF_HOME=/opt/models)
  python docker/fetch_models.py --verify [--app /app]  # offline: every model loads, nothing is fetched

What the service loads and where it lives in the image (runtime: HF_HOME=/opt/models, HF_HUB_OFFLINE=1):
  * Docling layout model, preset layout_heron_101   docling-project/docling-layout-heron-101 @ main
  * TableFormer ACCURATE                            docling-project/docling-models @ v2.3.0
  * picture classifier                              docling-project/DocumentFigureClassifier-v2.5 @ main
      the three in /opt/models/hub (Hugging Face cache layout), fetched by Docling's own model code while the
      PDF pipeline of convert_protocol.build_converter is initialised (artifacts_path None, as in the service)
  * RapidOCR, torch backend (PP-OCRv6 det/rec small, PP-OCRv4 cls mobile, PP-OCRv6 keys): rapidocr's own model
      folder <site-packages>/rapidocr/models, where rapidocr resolves them when Docling's RapidOcrModel gets
      artifacts_path=None (figure_ocr.py and the pipeline's OCR stage); a present file whose SHA-256 matches is
      never fetched again
  * Qwen/Qwen3-Embedding-8B tokenizer files only (no weights), at the commit the host results were produced
      with; refs/main points at it, so AutoTokenizer.from_pretrained(name) resolves it offline

--download initialises a pipeline with the same options as convert_protocol.build_converter (kept in step by
hand; this stage has no application code, so code changes keep the model layer cached). --verify --app builds
the converter with the service's own code, so a model it needs that was not fetched fails the build. --verify
refuses every non-local connection (and the Dockerfile runs it with --network=none) and also checks that every
import of the runtime files resolves, OCR reads a synthetic image, the tokenizer counts a probe sentence as on
the host, and the Mermaid validator (Node.js) parses. A failed check names models, files or modules; any other
error only its exception type and file:line.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAYOUT_PRESET = "layout_heron_101"  # convert_protocol.DEFAULT_LAYOUT
TOKENIZER = "Qwen/Qwen3-Embedding-8B"  # protocol_api.TOKENIZER, chunk_protocol's default
TOKENIZER_REVISION = "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
TOKENIZER_FILES = ["config.json", "merges.txt", "tokenizer.json", "tokenizer_config.json", "vocab.json"]
PROBE_TEXT = "Schedule of Activities: Visit 2 (Day 1) - vital signs, ECG and HbA1c at screening."
PROBE_TOKENS = 27  # Qwen3 tokens of PROBE_TEXT (no special tokens), as on the host
OPTIONAL_IMPORTS = {"tiktoken"}  # chunk_protocol --tokenizer tiktoken:<enc>: a command-line option, not the service's
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


# ------------------------------------------------------------------------------------------------ helpers


class CheckFailed(Exception):
    """A failed check whose message names models, files, modules or counts only (printed as is)."""


def pipeline_options():
    """The model-relevant options of convert_protocol.build_converter (backend options do not load models)."""
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.pipeline_options import (LayoutObjectDetectionOptions, PdfPipelineOptions,
                                                    RapidOcrOptions, TableFormerMode)

    opts = PdfPipelineOptions()
    opts.layout_options = LayoutObjectDetectionOptions.from_preset(LAYOUT_PRESET)
    opts.do_ocr = True
    opts.ocr_options = RapidOcrOptions(backend="torch", force_full_page_ocr=False)
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.table_structure_options.do_cell_matching = True
    opts.generate_picture_images = True
    opts.do_picture_classification = True
    opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4)
    return opts


def converter(app: Path | None):
    if app is not None:
        from convert_protocol import build_converter

        return build_converter()
    from docling.datamodel.base_models import InputFormat
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline

    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(
        pipeline_cls=StandardPdfPipeline, pipeline_options=pipeline_options())})


def rapidocr_reader(app: Path | None):
    """figure_ocr's reader (RapidOcrModel with artifacts_path=None, torch backend)."""
    if app is not None:
        import figure_ocr

        return figure_ocr._rapidocr()
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.pipeline_options import RapidOcrOptions
    from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel

    return RapidOcrModel(enabled=True, artifacts_path=None, options=RapidOcrOptions(backend="torch"),
                         accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4)).reader


def rapidocr_dir() -> Path:
    import rapidocr

    return Path(rapidocr.__file__).resolve().parent / "models"


def hub_cache() -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE)


def repo_dir(repo_id: str) -> Path:
    return hub_cache() / f"models--{repo_id.replace('/', '--')}"


def make_readable(*roots: Path) -> None:
    """a+r on files, a+rx on directories: the runtime user (not root) reads what root downloaded."""
    for root in roots:
        for path in [root, *root.rglob("*")]:
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            os.chmod(path, mode | (0o555 if path.is_dir() else 0o444))


def size_mb(root: Path) -> float:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()) / 2 ** 20


def where(exc: BaseException, roots: list[Path]) -> str:
    """file:line of the innermost frame in our own code (this script or the application), else the last one."""
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if any(Path(f.filename).resolve().is_relative_to(r) for r in roots)]
    frame = (ours or frames or [None])[-1]
    return f"{Path(frame.filename).name}:{frame.lineno}" if frame else "?"


class Recorder:
    """Which Hugging Face repos and RapidOCR files the code under test resolves (names, never contents)."""

    def __init__(self) -> None:
        self.repos: dict[str, str] = {}
        self.ocr_files: set[str] = set()

    def __enter__(self):
        import huggingface_hub
        from rapidocr.utils.download_file import DownloadFile

        self._hub, self._run = huggingface_hub.snapshot_download, DownloadFile.run.__func__
        recorder = self

        def snapshot_download(repo_id, *args, **kwargs):
            path = recorder._hub(repo_id, *args, **kwargs)
            recorder.repos[repo_id] = kwargs.get("revision") or "main"
            return path

        def run(cls, params):
            recorder.ocr_files.add(Path(params.save_path).name)
            return recorder._run(cls, params)

        huggingface_hub.snapshot_download = snapshot_download  # Docling imports it at call time
        DownloadFile.run = classmethod(run)
        return self

    def __exit__(self, *exc) -> None:
        import huggingface_hub
        from rapidocr.utils.download_file import DownloadFile

        huggingface_hub.snapshot_download = self._hub
        DownloadFile.run = classmethod(self._run)


def block_network() -> None:
    """Refuse every connection and name lookup that would leave the machine (loopback and Unix sockets pass)."""
    import socket

    real_connect, real_connect_ex, real_getaddrinfo = (socket.socket.connect, socket.socket.connect_ex,
                                                       socket.getaddrinfo)

    def allowed(sock, address) -> bool:
        return sock.family == getattr(socket, "AF_UNIX", object()) or (
            isinstance(address, tuple) and str(address[0]) in LOCAL_HOSTS)

    def connect(self, address):
        if not allowed(self, address):
            raise ConnectionRefusedError("network disabled by fetch_models.py --verify")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not allowed(self, address):
            raise ConnectionRefusedError("network disabled by fetch_models.py --verify")
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if host is not None and str(host) not in LOCAL_HOSTS:
            raise socket.gaierror("network disabled by fetch_models.py --verify")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, getaddrinfo


def probe_image():
    """A synthetic white image with two printed words (made-up text, nothing from a document)."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (720, 160), "white")
    ImageDraw.Draw(img).text((24, 40), "VISIT SCHEDULE", fill="black", font=ImageFont.load_default(size=56))
    return img


# ------------------------------------------------------------------------------------------------ download


def download() -> int:
    if os.environ.get("HF_HUB_OFFLINE", "").strip() not in ("", "0"):
        print("--download needs the network: HF_HUB_OFFLINE is set", file=sys.stderr)
        return 2
    xet = Path(tempfile.mkdtemp(prefix="hf_xet_"))
    os.environ["HF_XET_CACHE"] = str(xet)  # the Xet chunk cache would double the model layer
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    from docling.datamodel.base_models import InputFormat
    from huggingface_hub import snapshot_download

    start = time.time()
    try:
        with Recorder() as rec:
            converter(None).initialize_pipeline(InputFormat.PDF)  # Docling fetches what this pipeline loads
            rapidocr_reader(None)  # figure_ocr's reader: the same RapidOCR files (already present)
        snapshot_download(TOKENIZER, revision=TOKENIZER_REVISION, allow_patterns=TOKENIZER_FILES)
        refs = repo_dir(TOKENIZER) / "refs"
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "main").write_text(TOKENIZER_REVISION, encoding="utf-8")  # offline: name -> this snapshot
    finally:
        shutil.rmtree(xet, ignore_errors=True)
    shutil.rmtree(hub_cache().parent / "xet", ignore_errors=True)
    make_readable(hub_cache(), rapidocr_dir())
    for repo, rev in sorted({**rec.repos, TOKENIZER: TOKENIZER_REVISION}.items()):
        print(f"fetched {repo} @ {rev}: {size_mb(repo_dir(repo)):.0f} MB")
    print(f"rapidocr files: {sorted(rec.ocr_files)} in {rapidocr_dir()}")
    print(f"download done in {time.time() - start:.0f}s; {hub_cache()}: {size_mb(hub_cache()):.0f} MB, "
          f"rapidocr: {size_mb(rapidocr_dir()):.0f} MB")
    return 0


# ------------------------------------------------------------------------------------------------ verify


def check_imports(app: Path) -> str:
    """Every import of the runtime files resolves (a runtime file or package missing from the image fails here,
    even for imports inside functions; the command-line-only research imports service.runtime lists as
    LAZY_RESEARCH_EDGES are not in the image by design), and every runtime module imports."""
    import importlib
    import importlib.util

    from service.runtime import LAZY_RESEARCH_EDGES, _imports, runtime_files

    files = [f for f in runtime_files() if f.endswith(".py")]
    modules = [f[:-3].replace("/", ".").removesuffix(".__init__") for f in files]
    missing: set[str] = set()
    for f, module in zip(files, modules):
        for name in _imports(app / f):
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top in OPTIONAL_IMPORTS or top == "__future__" \
                    or (module, top) in LAZY_RESEARCH_EDGES:
                continue
            if importlib.util.find_spec(top) is None:
                missing.add(top)
    if missing:
        raise CheckFailed(f"unresolved imports in the runtime files: {sorted(missing)}")
    for m in modules:
        importlib.import_module(m)
    return f"{len(modules)} runtime modules import, every import resolves"


def check_pipeline(app: Path | None, rec: Recorder) -> str:
    from docling.datamodel.base_models import InputFormat

    converter(app).initialize_pipeline(InputFormat.PDF)
    for repo, rev in rec.repos.items():
        snap = repo_dir(repo) / "refs" / rev
        if not snap.is_file():
            raise CheckFailed(f"{repo}: no cached revision {rev}")
    return "PDF pipeline initialised; models: " + ", ".join(f"{r}@{v}" for r, v in sorted(rec.repos.items()))


def check_ocr(app: Path | None, rec: Recorder) -> str:
    import numpy as np

    reader = rapidocr_reader(app)
    result = reader(np.asarray(probe_image()))
    texts = [" ".join(str(t).split()) for t in (result.txts or ())] if result is not None else []
    if not texts or "SCHEDULE" not in " ".join(texts).upper():
        raise CheckFailed(f"OCR did not read the probe image ({len(texts)} boxes)")
    files = sorted(rec.ocr_files)
    for name in files:
        if not (rapidocr_dir() / name).is_file():
            raise CheckFailed(f"rapidocr model {name} missing")
    return f"RapidOCR torch read the probe image ({len(texts)} boxes); files: {files} in {rapidocr_dir()}"


def check_tokenizer(app: Path | None) -> str:
    from transformers import AutoTokenizer
    from transformers.utils import logging as hf_logging

    hf_logging.set_verbosity_error()
    tok = AutoTokenizer.from_pretrained(TOKENIZER, local_files_only=True)
    n = len(tok(PROBE_TEXT, add_special_tokens=False)["input_ids"])
    if n != PROBE_TOKENS:
        raise CheckFailed(f"tokenizer counts {n} tokens for the probe, expected {PROBE_TOKENS}")
    extra = ""
    if app is not None:
        import protocol_api
        from chunk_protocol import build_tokenizer

        if protocol_api.TOKENIZER != TOKENIZER:
            raise CheckFailed(f"protocol_api.TOKENIZER is {protocol_api.TOKENIZER}, fetch_models.py has {TOKENIZER}")
        chunked = build_tokenizer(TOKENIZER, 512).count_tokens(PROBE_TEXT)  # chunk_protocol's loader, offline
        if chunked != PROBE_TOKENS:
            raise CheckFailed(f"chunk_protocol counts {chunked} tokens for the probe, expected {PROBE_TOKENS}")
        extra = "; chunk_protocol counts the same"
    snapshot = repo_dir(TOKENIZER) / "snapshots" / (repo_dir(TOKENIZER) / "refs" / "main").read_text().strip()
    weights = [p.name for p in snapshot.iterdir() if p.suffix in (".safetensors", ".bin", ".gguf")]
    if weights:
        raise CheckFailed(f"tokenizer snapshot holds weights: {weights}")
    return f"{TOKENIZER} tokenizer: {n} probe tokens, {len(list(snapshot.iterdir()))} files, no weights{extra}"


def check_mermaid(app: Path) -> str:
    import figure_vlm

    ok, _ = figure_vlm.validate_mermaid("flowchart TD\n  A[Screening] --> B[Randomisation]")
    bad, _ = figure_vlm.validate_mermaid("flowchart TD\n  A[Screening --> ")
    if not ok or bad:
        raise CheckFailed(f"Mermaid validator: valid diagram ok={ok}, broken diagram ok={bad}")
    return "Mermaid validator (node tools/mermaid_validate/validate.mjs) parses"


def verify(app: Path | None) -> int:
    os.environ["HF_HUB_OFFLINE"] = "1"  # before huggingface_hub / transformers are imported
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    block_network()
    roots = [HERE]
    if app is not None:
        app = app.resolve()
        sys.path.insert(0, str(app))
        roots.append(app)
    rec = Recorder()
    checks = [("pipeline", lambda: check_pipeline(app, rec)), ("ocr", lambda: check_ocr(app, rec)),
              ("tokenizer", lambda: check_tokenizer(app))]
    if app is not None:
        checks = [("imports", lambda: check_imports(app))] + checks + [("mermaid", lambda: check_mermaid(app))]
    failed = 0
    with rec:
        for name, check in checks:
            start = time.time()
            try:
                detail = check()
                print(f"ok {name} ({time.time() - start:.0f}s): {detail}", flush=True)
            except CheckFailed as exc:
                failed += 1
                print(f"FAIL {name}: {exc}", flush=True)
            except Exception as exc:  # noqa: BLE001 - reported by type and location only
                failed += 1
                print(f"FAIL {name}: {type(exc).__name__} at {where(exc, roots)}", flush=True)
    print("verify: all models load offline" if not failed else f"verify: {failed} of {len(checks)} checks failed")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--download", action="store_true", help="fetch every model (network on)")
    mode.add_argument("--verify", action="store_true", help="load every model offline")
    parser.add_argument("--app", type=Path, help="application folder: verify through the service's own code")
    args = parser.parse_args(argv)
    if args.download:
        return download()
    return verify(args.app)


if __name__ == "__main__":
    sys.exit(main())
