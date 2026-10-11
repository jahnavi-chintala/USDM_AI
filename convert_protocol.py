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
folder already holding a different PDF is refused (use another --out).
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

import shutil
import subprocess
import sys
import time
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
from docling_core.types.doc import DoclingDocument, ImageRefMode, PictureItem, TableItem

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


def batch_cache(out: Path, src: Path, pdf_sha: str, layout: str, pages: int, chunk: int) -> Path:
    """The batch folder for this PDF + converter configuration, checked by a manifest.

    Batches of an older cache layout (<out>/_chunks/<layout>/, keyed by layout
    only) are adopted once when they were produced from a PDF with the same
    name and the same batch ranges; nothing else is ever reused.
    """
    config = {**CONVERTER_CONFIG, "layout": layout, "docling": version("docling")}
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:8]
    cache = out / "_chunks" / pdf_sha[:16] / f"{layout}-{config_hash}"
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
    parser.add_argument("--batch-dir", help=argparse.SUPPRESS)  # internal: worker mode
    parser.add_argument("--range", type=int, nargs=2, metavar=("FIRST", "LAST"),
                        help=argparse.SUPPRESS)  # internal: worker mode
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    src = Path(args.pdf)
    out = Path(args.out)
    stem = src.stem

    if args.range:  # worker: convert one batch into the folder the orchestrator chose
        first, last = args.range
        convert_chunk(src, first, last, Path(args.batch_dir) / f"{first:04d}-{last:04d}.json", args.layout,
                      args.password)
        return

    pdf_sha = file_sha256(src)
    claim_output_folder(out, src, pdf_sha, args.force)
    n_pages = len(pdfium.PdfDocument(str(src), password=args.password))
    batches = batch_cache(out, src, pdf_sha, args.layout, n_pages, args.chunk)

    # Convert in page batches, each in a fresh process: memory grows across
    # conversions in one process and eventually crashes natively on a long
    # protocol. Finished batches are cached, so a rerun resumes where it left off.
    start = time.time()
    batch_files = []
    for first in range(1, n_pages + 1, args.chunk):
        last = min(first + args.chunk - 1, n_pages)
        dest = batches / f"{first:04d}-{last:04d}.json"
        batch_files.append(dest)
        if dest.exists():
            log.info("Pages %d-%d cached", first, last)
            continue
        cmd = [sys.executable, __file__, str(src), "--out", str(out), "--layout", args.layout,
               "--batch-dir", str(batches), "--range", str(first), str(last)]
        # The password reaches the worker through its environment, not its command line.
        env = {**os.environ, **({"PDF_PASSWORD": args.password} if args.password else {})}
        for attempt in range(1, 4):
            rc = subprocess.run(cmd, env=env).returncode
            if rc == 0:
                break
            if rc == NOT_CONVERTED:
                raise RuntimeError(f"Pages {first}-{last}: Docling did not convert them completely (see log)")
            log.warning("Pages %d-%d: worker crashed (attempt %d), retrying", first, last, attempt)
        else:
            raise RuntimeError(f"Pages {first}-{last} failed after 3 attempts")
        log.info("Pages %d-%d done (%.0fs elapsed)", first, last, time.time() - start)

    doc = DoclingDocument.concatenate([DoclingDocument.load_from_json(f) for f in batch_files])
    doc.name = stem
    log.info("Converted %s in %.1fs", src.name, time.time() - start)

    pdf = pdfium.PdfDocument(str(src), password=args.password)
    log.info("Post-processing: %s", postprocess(doc, pdf, ProtocolSettings.load(args.settings)))
    (out / f"{stem}.decisions.json").write_text(
        json.dumps(getattr(postprocess, "last_decisions", {}), indent=1, default=str), encoding="utf-8")
    if args.figure_model:
        log.info("Figure descriptions (%s): %s", args.figure_model,
                 attach_cached(doc, args.figure_model, Path(args.vlm_cache)))

    images = Path(f"{stem}_images")
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

    log.info("Pages: %d | tables: %d | pictures: %d", len(doc.pages), n_tables, n_pictures)
    log.info("Outputs written to %s", out.resolve())


if __name__ == "__main__":
    main()
