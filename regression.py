"""Regression check for this protocol: does a code change alter the output?

  baseline        snapshot the current pipeline output (reports/regression/baseline.json)
  run NAME        re-run assembly + post-processing + figure attachment + chunking from
                  the cached conversion batches into reports/regression/NAME/ (never
                  touches output/), snapshot it, and print every difference from the
                  baseline

The snapshot holds post-processing counts, every heading (text, level), every
table (caption, shape, cell-text hash, footnotes), figures (page, caption,
child count), word coverage, and every chunk (id, content hash, section
number, pages). Runs are independent folders, so several can run at once.
Only this protocol is used (its content may be printed).
"""

import argparse
import hashlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pypdfium2 as pdfium

from project_paths import main_pdf

ROOT = Path(__file__).parent
PDF = main_pdf()
LAYOUT = "layout_heron_101"


def batch_dir() -> Path:
    """This protocol's cached conversion batches (manifest-keyed cache, or the older layout)."""
    for manifest in sorted((ROOT / "output" / "_chunks").glob(f"*/{LAYOUT}-*/manifest.json")):
        if json.loads(manifest.read_text(encoding="utf-8")).get("pdf_name") == PDF.name:
            return manifest.parent
    return ROOT / "output" / "_chunks" / LAYOUT
REG = ROOT / "reports" / "regression"
FIGURE_MODEL = "openrouter:anthropic/claude-opus-5.5"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def assemble(run_dir: Path) -> tuple[Path, dict]:
    """Concatenate cached batches, post-process, attach figure results, save JSON."""
    from docling_core.types.doc import DoclingDocument, ImageRefMode

    from figure_vlm import attach_cached
    from protocol_postprocess import postprocess

    doc = DoclingDocument.concatenate(
        [DoclingDocument.load_from_json(f) for f in sorted(batch_dir().glob("*.json"))
         if f.name != "manifest.json"])
    doc.name = PDF.stem
    counts = postprocess(doc, pdfium.PdfDocument(str(PDF)))
    figures = attach_cached(doc, FIGURE_MODEL, ROOT / "vlm_cache")
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{PDF.stem}.json"
    doc.save_as_json(path, image_mode=ImageRefMode.REFERENCED, artifacts_dir=Path("images"))
    return path, {"postprocess": counts, "figures": figures}


def snapshot(doc_path: Path, chunks_dir: Path, steps: dict) -> dict:
    from docling_core.types.doc import DoclingDocument, SectionHeaderItem

    from validate_output import page_coverage

    doc = DoclingDocument.load_from_json(doc_path)
    heads = [[h.text, h.level] for h, _ in doc.iterate_items() if isinstance(h, SectionHeaderItem)]
    tables = []
    for t in doc.tables:
        cells = "\x1f".join(f"{c.start_row_offset_idx},{c.start_col_offset_idx}:{c.text}"
                            for c in sorted(t.data.table_cells,
                                            key=lambda c: (c.start_row_offset_idx, c.start_col_offset_idx)))
        tables.append({"caption": t.caption_text(doc), "pages": [p.page_no for p in t.prov],
                       "shape": [t.data.num_rows, t.data.num_cols], "cells": _h(cells),
                       "footnotes": [f.resolve(doc).text for f in t.footnotes]})
    pictures = [{"page": p.prov[0].page_no if p.prov else None, "caption": p.caption_text(doc),
                 "children": len(p.children)} for p in doc.pictures]
    report = page_coverage(doc, pdfium.PdfDocument(str(PDF)))
    checked = [r for r in report if not r["expected"]]
    words = sum(r["pdf_words"] for r in checked)
    chunks = [json.loads(l) for l in (chunks_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()]
    stats = json.loads((chunks_dir / "stats.json").read_text(encoding="utf-8"))
    return {
        "steps": steps,
        "headings": heads,
        "tables": tables,
        "pictures": pictures,
        "coverage": round(sum(r["pdf_words"] * r["coverage"] for r in checked) / words, 4),
        "pages_below_97": [r["page"] for r in checked if r["coverage"] < 0.97],
        "chunks": [{"id": c["chunk_id"], "hash": c["content_hash"], "section": c.get("section_number"),
                    "pages": c["pages"], "type": c["chunk_type"]} for c in chunks],
        "chunk_checks": {k: len(v) for k, v in stats.get("checks", {}).items()},
        "document": {k: v for k, v in stats.get("document", {}).items() if k != "source_sha256"},
    }


def build(name: str) -> dict:
    run_dir = REG / name
    doc_path, steps = assemble(run_dir)
    chunks_dir = run_dir / "chunks"
    with open(run_dir / "chunk.log", "w", encoding="utf-8") as fh:
        rc = subprocess.run([sys.executable, "chunk_protocol.py", str(doc_path), "--out", str(chunks_dir)],
                            cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise SystemExit(f"chunking failed; see {run_dir / 'chunk.log'}")
    snap = snapshot(doc_path, chunks_dir, steps)
    (run_dir / "snapshot.json").write_text(json.dumps(snap, indent=1, ensure_ascii=False), encoding="utf-8")
    return snap


def diff_lists(name: str, old: list, new: list, limit: int = 15) -> list[str]:
    if old == new:
        return []
    out = [f"{name}: {len(old)} -> {len(new)}"]
    old_s = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in old}
    new_s = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in new}
    gone, added = [x for x in old_s if x not in new_s], [x for x in new_s if x not in old_s]
    out += [f"  - {x[:220]}" for x in sorted(gone)[:limit]] + [f"  + {x[:220]}" for x in sorted(added)[:limit]]
    if not gone and not added:
        out.append("  (same items, different order)")
    return out


def compare(base: dict, new: dict) -> list[str]:
    lines = []
    for key in ("steps", "coverage", "pages_below_97", "chunk_checks", "document"):
        if base.get(key) != new.get(key):
            lines.append(f"{key}: {json.dumps(base.get(key), ensure_ascii=False)}\n"
                         f"   -> {json.dumps(new.get(key), ensure_ascii=False)}")
    for key in ("headings", "tables", "pictures", "chunks"):
        lines += diff_lists(key, base.get(key, []), new.get(key, []))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("baseline")
    p = sub.add_parser("run")
    p.add_argument("name")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)

    if args.cmd == "baseline":
        snap = build("baseline")
        (REG / "baseline.json").write_text(json.dumps(snap, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"baseline: {len(snap['headings'])} headings, {len(snap['tables'])} tables, "
              f"{len(snap['chunks'])} chunks, coverage {snap['coverage']:.2%}")
        return
    base = json.loads((REG / "baseline.json").read_text(encoding="utf-8"))
    diffs = compare(base, build(args.name))
    print("\n".join(diffs) if diffs else "identical to baseline")


if __name__ == "__main__":
    main()
