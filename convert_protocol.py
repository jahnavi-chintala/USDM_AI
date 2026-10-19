"""Convert a clinical trial protocol PDF to Markdown with Docling.

Pipeline choices (tuned for born-digital trial protocols):
  * Hybrid backend (hybrid_backend.py): text from docling-parse (Docling's
    default, cleanest extraction) with page images rendered by pdfium, since
    docling-parse draws some embedded images (RunLength-compressed) as blank
    boxes and the layout model would never see them.
  * Bitmap-only OCR (RapidOCR, torch backend): the text layer is used as-is,
    and OCR runs only on raster regions, which recovers the text inside figures.
  * TableFormer in ACCURATE mode + cell matching: Schedules of Activities and
    other dense, multi-row/multi-column tables.
  * Picture images + classification: figures are saved as PNGs, referenced
    from the Markdown and tagged with their type (flow chart, etc.).
  * Page headers/footers are layout-labelled by Docling and excluded from the
    Markdown body.
  * Post-processing (protocol_postprocess.py, settings in protocol_settings.py,
    optional --settings profile): page-split tables merged, heading levels from
    the PDF outline or section numbering, TOC rebuilt, footnotes attached, etc.
    Its decisions (refs, pages, reasons; never text) go to <stem>.decisions.json.
  * Outputs: Markdown (compact tables, referenced images, page-break markers),
    lossless Docling JSON, HTML, and every table as CSV.

Caching and output folders: pages are converted in batches, each in a fresh
process, and batches are cached under <out>/_chunks/<pdf sha>/<layout>-<config
hash>/ with a manifest, so a different PDF or different converter settings can
never reuse another run's batches. One output folder holds one document: a
folder already holding a different PDF is refused (use another --out). Batches
can run in parallel (--workers N at a time); the document does not depend on N.
"""

import argparse
import hashlib
import json
import logging
import os

# Cap native thread pools before numpy/torch load: OpenBLAS allocates a buffer
# per thread and fails outright when free RAM is tight.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "4")

import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from importlib.metadata import version
from pathlib import Path

import pypdfium2 as pdfium
from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.backend_options import ThreadedDoclingParseBackendOptions
from docling.datamodel.pipeline_options import (
    LayoutObjectDetectionOptions,
    PdfPipelineOptions,
    RapidOcrOptions,
    TableFormerMode,
)
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.standard_pdf_pipeline import StandardPdfPipeline
from docling_core.types.doc import DoclingDocument, DocumentOrigin, ImageRefMode, PictureItem, TableItem

from figure_vlm import attach_cached, label_mermaid_fences
from hybrid_backend import HybridPdfBackend
from project_paths import main_pdf
from protocol_postprocess import postprocess
from protocol_settings import ProtocolSettings

log = logging.getLogger("convert_protocol")


DEFAULT_LAYOUT = "layout_heron_101"
# Everything that changes what a batch contains. Part of the batch-cache key, so
# changing any of it (or upgrading docling) converts afresh instead of reusing.
CONVERTER_CONFIG = {
    "backend": "hybrid(docling-parse text, pdfium images, render_pages=False)",
    "ocr": "rapidocr-torch, bitmap regions only",
    "tables": "tableformer ACCURATE, cell matching",
    "images_scale": 2.0,
    "picture_classification": True,
}


def build_converter(layout: str = DEFAULT_LAYOUT, password: str | None = None) -> DocumentConverter:
    opts = PdfPipelineOptions()

    opts.layout_options = LayoutObjectDetectionOptions.from_preset(layout)

    opts.do_ocr = True
    opts.ocr_options = RapidOcrOptions(backend="torch", force_full_page_ocr=False)

    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE
    opts.table_structure_options.do_cell_matching = True

    opts.images_scale = 2.0
    opts.generate_picture_images = True
    opts.generate_page_images = False
    opts.do_picture_classification = True

    opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4)

    return DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=StandardPdfPipeline,
                pipeline_options=opts,
                backend=HybridPdfBackend,
                # Pages are rendered by pdfium (hybrid_backend.py), so docling-parse's
                # own render is unused. Turning it off also stops docling-parse
                # reporting vector shapes, which made OCR run on every ruled table
                # only for its output to be discarded; figures still get OCR'd.
                backend_options=ThreadedDoclingParseBackendOptions(render_pages=False, password=password),
            )
        }
    )


NOT_CONVERTED = 3  # worker exit code: Docling reported a non-SUCCESS status (not worth retrying)


def convert_chunk(src: Path, first: int, last: int, dest: Path, layout: str, password: str | None = None) -> None:
    """Convert one page range and save it as JSON with embedded images.

    A batch that Docling did not convert completely is never written, so it is
    never cached: an empty or partial batch would otherwise silently lose pages.
    """
    result = build_converter(layout, password).convert(src, page_range=(first, last), raises_on_error=False)
    if result.status != ConversionStatus.SUCCESS:
        log.error("Pages %d-%d: %s %s", first, last, result.status,
                  [e.error_message for e in result.errors])
        sys.exit(NOT_CONVERTED)
    tmp = dest.with_suffix(".tmp")
    result.document.save_as_json(tmp, image_mode=ImageRefMode.EMBEDDED)
    tmp.replace(dest)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def batch_cache(out: Path, src: Path, pdf_sha: str, layout: str, pages: int, chunk: int,
                root: Path | None = None) -> Path:
    """The batch folder for this PDF + converter configuration, checked by a manifest.

    The cache lives under `root` (default <out>/_chunks; the service shares one cache
    root across documents). Batches of an older cache layout (<out>/_chunks/<layout>/,
    keyed by layout only) are adopted once when they were produced from a PDF with the
    same name and the same batch ranges; nothing else is ever reused.
    """
    config = {**CONVERTER_CONFIG, "layout": layout, "docling": version("docling")}
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]
    cache = (root or out / "_chunks") / pdf_sha[:16] / f"{layout}-{config_hash}"
    manifest = {"pdf_name": src.name, "pdf_sha256": pdf_sha, "pages": pages, "batch_pages": chunk,
                "config": config}
    mfile = cache / "manifest.json"
    if mfile.exists():
        found = json.loads(mfile.read_text(encoding="utf-8"))
        stale = {k for k in ("pdf_sha256", "pages", "batch_pages", "config") if found.get(k) != manifest[k]}
        if stale:
            raise SystemExit(f"batch cache {cache} does not match this run ({', '.join(sorted(stale))}); "
                             "delete it or use another --out")
        return cache
    cache.mkdir(parents=True, exist_ok=True)
    legacy = out / "_chunks" / layout
    expected = [f"{a:04d}-{min(a + chunk - 1, pages):04d}.json" for a in range(1, pages + 1, chunk)]
    if legacy.is_dir() and not any(cache.glob("*.json")):
        names = sorted(p.name for p in legacy.glob("*.json"))
        same_doc = names and all(
            json.loads((legacy / n).read_text(encoding="utf-8"))["name"] == src.stem for n in names)
        if same_doc and set(names) <= set(expected):
            for n in names:
                (legacy / n).replace(cache / n)
            manifest["adopted_from"] = str(legacy)
            log.info("Adopted %d cached batches from %s", len(names), legacy)
    mfile.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return cache


def claim_output_folder(out: Path, src: Path, pdf_sha: str, force: bool) -> None:
    """One document per output folder: refuse a folder that already holds another PDF."""
    marker = out / "document.json"
    if marker.exists():
        held = json.loads(marker.read_text(encoding="utf-8"))
        if held.get("pdf_sha256") != pdf_sha and not force:
            raise SystemExit(f"{out} already holds another document ({held.get('pdf_name')}); "
                             "use a different --out (or --force to replace it)")
    out.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"pdf_name": src.name, "pdf_sha256": pdf_sha}, indent=1), encoding="utf-8")


def _worker_command(src: Path, out: Path, layout: str, batches: Path, first: int, last: int,
                    password: str | None) -> tuple[list[str], dict[str, str]]:
    """The command and environment of the process converting pages first-last (one batch)."""
    cmd = [sys.executable, str(Path(__file__).resolve()), str(src), "--out", str(out), "--layout", layout,
           "--batch-dir", str(batches), "--range", str(first), str(last)]
    # The password reaches the worker through its environment, not its command line.
    env = {**os.environ, **({"PDF_PASSWORD": password} if password else {})}
    return cmd, env


def _not_converted(first: int, last: int) -> RuntimeError:
    return RuntimeError(f"Pages {first}-{last}: Docling did not convert them completely (see log)")


def _convert_alone(cmd: list[str], env: dict[str, str], first: int, last: int, subprocess_log: Path | None) -> None:
    """Run one batch's worker, up to 3 attempts, its output streamed to `subprocess_log` (appended) or stdout."""
    for attempt in range(1, 4):
        if subprocess_log is not None:
            with open(subprocess_log, "a", encoding="utf-8") as fh:
                rc = subprocess.run(cmd, env=env, stdout=fh, stderr=subprocess.STDOUT).returncode
        else:
            rc = subprocess.run(cmd, env=env).returncode
        if rc == 0:
            return
        if rc == NOT_CONVERTED:
            raise _not_converted(first, last)
        log.warning("Pages %d-%d: worker crashed (attempt %d), retrying", first, last, attempt)
    raise RuntimeError(f"Pages {first}-{last} failed after 3 attempts")


def _attempt_captured(first: int, last: int, cmd: list[str], env: dict[str, str]) -> tuple:
    """One attempt of one batch, its output captured: ((first, last), return code, output bytes).
    Runs in a pool thread, so it does no logging."""
    done = subprocess.run(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return (first, last), done.returncode, done.stdout


def _write_log(data: bytes, subprocess_log: Path | None) -> None:
    """Append `data` to `subprocess_log`, else write it to stdout (a text-only stream gets it decoded)."""
    if subprocess_log is not None:
        with open(subprocess_log, "ab") as fh:
            fh.write(data)
    elif getattr(sys.stdout, "buffer", None) is not None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    elif sys.stdout is not None:  # pythonw has no stdout at all
        sys.stdout.write(data.decode("utf-8", "replace"))
        sys.stdout.flush()


def _convert_parallel(jobs: list, workers: int, subprocess_log: Path | None, finished) -> None:
    """Convert the batches in `jobs` ((first, last, cmd, env) in page order), `workers` at a time.

    Pool threads only run one attempt and return its result; the output, the logging and `finished(first, last)`
    stay in this thread (the service's job log keeps only the calling thread's records). At most `workers`
    batches are in flight and the next one is submitted only after a result was handled, so nothing is queued
    behind a failure: a batch Docling did not convert, a crashed one, an exception of a pool task, or an exception
    in this thread that is not a console interrupt (progress, the log file) starts no further batch; the running
    ones finish (the pool waits for them, no process is killed) and stay cached. A Ctrl+C in a console reaches
    the workers too: they stop without writing their batch. A crashed batch (e.g. killed for memory) and the
    batches not yet started are then converted alone, in page order, once the pool has drained.
    """
    size = min(workers, len(jobs))
    log.info("Converting %d batches, %d at a time", len(jobs), size)
    commands = {(first, last): (cmd, env) for first, last, cmd, env in jobs}
    waiting, running = list(jobs), {}  # not yet submitted (page order); future -> (first, last)
    unconverted, crashed, error = [], [], None
    with ThreadPoolExecutor(max_workers=size) as pool:
        while True:
            while waiting and len(running) < size and error is None and not unconverted and not crashed:
                job = waiting.pop(0)
                running[pool.submit(_attempt_captured, *job)] = job[:2]
            if not running:
                break
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=running.get):
                del running[future]
                if future.exception() is not None:
                    error = error or future.exception()
                    continue
                (first, last), rc, output = future.result()
                if output and not output.endswith(b"\n"):  # a killed worker can stop mid-line
                    output += b"\n"
                _write_log(f"=== pages {first}-{last} ===\n".encode() + output, subprocess_log)
                if rc == 0:
                    finished(first, last)
                elif rc == NOT_CONVERTED:
                    unconverted.append((first, last))
                else:
                    log.warning("Pages %d-%d: worker crashed in parallel mode (exit code %d)", first, last, rc)
                    crashed.append((first, last))
    if error is not None:
        raise error
    if unconverted:
        raise _not_converted(*min(unconverted))
    for first, last in sorted(crashed + [job[:2] for job in waiting]):
        if (first, last) in crashed:
            log.info("Pages %d-%d: retrying alone after a crash in parallel mode", first, last)
            _write_log(f"=== pages {first}-{last} (retried alone) ===\n".encode(), subprocess_log)
        else:
            log.info("Pages %d-%d: converting alone after a crash in parallel mode", first, last)
        _convert_alone(*commands[(first, last)], first, last, subprocess_log)
        finished(first, last)


def convert_batches(src: Path, out: Path, *, chunk: int = 15, layout: str = DEFAULT_LAYOUT,
                    password: str | None = None, batch_root: Path | None = None,
                    progress=None, subprocess_log: Path | None = None,
                    workers: int = 1) -> tuple[DoclingDocument, int]:
    """Convert the PDF in page batches (cached) and concatenate them: (raw document, page count).

    Each batch runs in a fresh process: memory grows across conversions in one process and
    eventually crashes natively on a long protocol. Finished batches are cached, so a rerun
    resumes where it left off. progress("convert", i, n) after each batch (cached ones first);
    the workers' output goes to `subprocess_log` when given (appended), else to this process's
    stdout. Up to `workers` batches run at the same time. The document does not depend on
    `workers` (the batches are concatenated in page order).
    """
    if workers < 1:
        raise ValueError(f"workers must be at least 1 (got {workers})")
    pdf_sha = file_sha256(src)
    probe = pdfium.PdfDocument(str(src), password=password)
    n_pages = len(probe)
    probe.close()  # an open handle would keep the file locked on Windows
    batches = batch_cache(out, src, pdf_sha, layout, n_pages, chunk, root=batch_root)
    start = time.time()
    ranges = [(first, min(first + chunk - 1, n_pages)) for first in range(1, n_pages + 1, chunk)]
    batch_files = [batches / f"{first:04d}-{last:04d}.json" for first, last in ranges]
    reported = 0

    def report() -> None:
        nonlocal reported
        reported += 1
        if progress is not None:
            progress("convert", reported, len(ranges))

    def finished(first: int, last: int) -> None:
        log.info("Pages %d-%d done (%.0fs elapsed)", first, last, time.time() - start)
        report()

    jobs = []
    for (first, last), dest in zip(ranges, batch_files):
        if dest.exists():
            log.info("Pages %d-%d cached", first, last)
            report()
        else:
            jobs.append((first, last, *_worker_command(src, out, layout, batches, first, last, password)))
    if min(workers, len(jobs)) <= 1:  # nothing can run in parallel: today's path
        for first, last, cmd, env in jobs:
            _convert_alone(cmd, env, first, last, subprocess_log)
            finished(first, last)
    else:
        _convert_parallel(jobs, workers, subprocess_log, finished)
    doc = DoclingDocument.concatenate([DoclingDocument.load_from_json(f) for f in batch_files])
    # Record the source PDF (concatenation keeps no origin), as Docling does: binary_hash from the SHA-256 of the
    # bytes. Chunk ids derive from it (chunk_protocol.doc_metadata), so they depend on the PDF alone, not on the
    # JSON, whose model floats differ slightly between platforms.
    doc.origin = DocumentOrigin(mimetype="application/pdf", binary_hash=pdf_sha, filename=src.name)
    log.info("Converted %s in %.1fs", src.name, time.time() - start)
    return doc, n_pages


_JSON_URI = re.compile(rb'("uri":\s*")([^"]*)(")')
_HTML_SRC = re.compile(rb'(<img\b[^>]*?\bsrc=")([^"]*)(")', re.I)


def platform_independent(path: Path) -> None:
    """Make a saved output byte-identical on every platform: Docling writes in text mode (CRLF on Windows) and a
    referenced image's path with the OS separator (JSON "protocol_images\\\\image_000.png", HTML
    src="images%5Cimage_000.png", a link no browser resolves). LF and "/" everywhere keep the JSON's hash, and
    the chunk ids and source_sha256 derived from it, the same on a Windows host and in the Linux container."""
    data = path.read_bytes()
    fixed = data.replace(b"\r\n", b"\n")

    def local(value: bytes) -> bool:
        return not value.startswith(b"data:") and b"://" not in value

    if path.suffix == ".json":
        fixed = _JSON_URI.sub(lambda m: m.group(1) + m.group(2).replace(b"\\\\", b"/") + m.group(3)
                              if local(m.group(2)) else m.group(0), fixed)
    elif path.suffix == ".html":
        fixed = _HTML_SRC.sub(lambda m: m.group(1) + re.sub(rb"%5[cC]", b"/", m.group(2)) + m.group(3)
                              if local(m.group(2)) else m.group(0), fixed)
    if fixed != data:
        path.write_bytes(fixed)


def save_outputs(doc: DoclingDocument, out: Path, stem: str, images_dir: str) -> dict[str, int]:
    """Markdown, lossless JSON and HTML (images referenced from `images_dir`, relative to `out`),
    and every table as CSV under out/tables/. Returns counts."""
    images = Path(images_dir)
    shutil.rmtree(out / images, ignore_errors=True)
    (out / f"{stem}.raw.json").unlink(missing_ok=True)

    md_path = out / f"{stem}.md"
    doc.save_as_markdown(md_path, image_mode=ImageRefMode.REFERENCED,
                         artifacts_dir=images, compact_tables=True, traverse_pictures=True,
                         include_picture_classification=True,
                         page_break_placeholder="<!-- page break -->")
    md_path.write_text(label_mermaid_fences(md_path.read_text(encoding="utf-8")), encoding="utf-8")
    doc.save_as_json(out / f"{stem}.json", image_mode=ImageRefMode.REFERENCED,
                     artifacts_dir=images)
    doc.save_as_html(out / f"{stem}.html", image_mode=ImageRefMode.REFERENCED,
                     artifacts_dir=images)
    for suffix in (".md", ".json", ".html"):
        platform_independent(out / f"{stem}{suffix}")

    tables_dir = out / "tables"
    tables_dir.mkdir(exist_ok=True)
    for old in tables_dir.glob("*.csv"):
        old.unlink()
    n_tables = n_pictures = 0
    for item, _ in doc.iterate_items():
        if isinstance(item, TableItem):
            n_tables += 1
            page = item.prov[0].page_no if item.prov else 0
            item.export_to_dataframe(doc=doc).to_csv(
                tables_dir / f"table_{n_tables:03d}_p{page:03d}.csv", index=False
            )
        elif isinstance(item, PictureItem):
            n_pictures += 1
    return {"pages": len(doc.pages), "tables": n_tables, "pictures": n_pictures}


def convert_document(src: Path, out: Path, *, chunk: int = 15, layout: str = DEFAULT_LAYOUT,
                     settings: ProtocolSettings | None = None, figure_model: str = "",
                     vlm_cache: Path = Path("vlm_cache"), describe_figures: bool = False,
                     figure_concurrency: int = 4, force: bool = False, password: str | None = None,
                     stem: str | None = None, images_dir: str | None = None, batch_root: Path | None = None,
                     progress=None, subprocess_log: Path | None = None, convert_workers: int = 1) -> dict:
    """The whole conversion: batches -> post-processing -> figure descriptions -> saved outputs.

    Outputs go to `out` as <stem>.json/.md/.html, <stem>.decisions.json, <images_dir>/ and
    tables/ (defaults: the PDF's stem and "<stem>_images", as the command line writes them).
    Figure descriptions: with describe_figures, figure_vlm.describe_missing first runs
    `figure_model` (OpenRouter, zero-data-retention) for every figure not skipped and not
    cached in `vlm_cache`; then the cached results are attached. Without it, only cached
    results are attached (the command line's behaviour). progress(stage, done, total) with
    stages "convert", "postprocess", "figures", "save". convert_workers batches are converted at the same time
    (the document does not depend on it). Returns counts and the figure stats.
    """
    stem = stem or src.stem
    images_dir = images_dir or f"{stem}_images"
    claim_output_folder(out, src, file_sha256(src), force)
    doc, n_pages = convert_batches(src, out, chunk=chunk, layout=layout, password=password,
                                   batch_root=batch_root, progress=progress, subprocess_log=subprocess_log,
                                   workers=convert_workers)
    doc.name = stem

    if progress is not None:
        progress("postprocess")
    pdf = pdfium.PdfDocument(str(src), password=password)
    try:
        counts = postprocess(doc, pdf, settings or ProtocolSettings())
    finally:
        pdf.close()
    log.info("Post-processing: %s", counts)
    (out / f"{stem}.decisions.json").write_text(
        json.dumps(getattr(postprocess, "last_decisions", {}), indent=1, default=str), encoding="utf-8")

    figures: dict = {}
    if figure_model:
        if progress is not None:
            progress("figures")
        if describe_figures:
            from figure_vlm import describe_missing

            figures = describe_missing(doc, figure_model, vlm_cache, concurrency=figure_concurrency,
                                       progress=progress)
        figures.update(attach_cached(doc, figure_model, vlm_cache))
        log.info("Figure descriptions (%s): %s", figure_model, figures)

    if progress is not None:
        progress("save")
    saved = save_outputs(doc, out, stem, images_dir)
    log.info("Pages: %d | tables: %d | pictures: %d", saved["pages"], saved["tables"], saved["pictures"])
    log.info("Outputs written to %s", out.resolve())
    return {"stem": stem, "json": out / f"{stem}.json", **saved, "postprocess": counts, "figures": figures}


def at_least_one(text: str) -> int:
    """argparse type: an integer >= 1."""
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", nargs="?", default=str(main_pdf()), help="default: the main protocol (project_paths)")
    parser.add_argument("--out", default="output", help="output folder (one document per folder)")
    parser.add_argument("--chunk", type=int, default=15, help="pages per conversion batch")
    parser.add_argument("--layout", default=DEFAULT_LAYOUT,
                        choices=LayoutObjectDetectionOptions.list_preset_ids())
    parser.add_argument("--settings", help="post-processing profile (JSON/YAML, protocol_settings.py)")
    parser.add_argument("--figure-model", default="openrouter:anthropic/claude-opus-5.5",
                        help="attach this model's cached figure_vlm.py results ('' to skip); "
                             "the pipeline itself never calls a VLM")
    parser.add_argument("--vlm-cache", default="vlm_cache")
    parser.add_argument("--force", action="store_true", help="replace another document in --out")
    parser.add_argument("--password", default=os.environ.get("PDF_PASSWORD"),
                        help="PDF password (or set PDF_PASSWORD); passed to both parsers")
    parser.add_argument("--workers", type=at_least_one, default=1,
                        help="batches converted at the same time, each in its own process")
    parser.add_argument("--batch-dir", help=argparse.SUPPRESS)  # internal: worker mode
    parser.add_argument("--range", type=int, nargs=2, metavar=("FIRST", "LAST"),
                        help=argparse.SUPPRESS)  # internal: worker mode
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    src = Path(args.pdf)
    out = Path(args.out)

    if args.range:  # worker: convert one batch into the folder the orchestrator chose
        first, last = args.range
        convert_chunk(src, first, last, Path(args.batch_dir) / f"{first:04d}-{last:04d}.json", args.layout,
                      args.password)
        return

    # The command line attaches cached figure descriptions only (figure_vlm.py is the explicit step
    # that calls a model); the service passes describe_figures=True.
    convert_document(src, out, chunk=args.chunk, layout=args.layout, settings=ProtocolSettings.load(args.settings),
                     figure_model=args.figure_model, vlm_cache=Path(args.vlm_cache), force=args.force,
                     password=args.password, convert_workers=args.workers)


if __name__ == "__main__":
    main()
