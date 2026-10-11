"""Run the pipeline on other sponsors' protocols and report only numbers.

The PDFs in test_protocols/ must stay private: this harness never prints file
names, titles or any document text. Protocols are reported as P1, P2, ... (the
mapping to files is written to output_tests/protocols.json for the user, not
printed). Each step's full log goes to a file in output_tests/Pn/. Errors are
reported as the exception type and the location in our code only, because
exception messages can quote document text.

  convert  convert every PDF (convert_protocol.py, no figure VLM; resumable)
  report   chunk each converted document and print aggregate metrics

Metrics: conversion status/time, element counts and post-processing counts,
heading structure (how many headings are numbered, level distribution), word
coverage against the PDF text layer (validate_output.py), glossary size,
whether protocol id / amendment / date were detected (booleans), chunk counts,
token statistics and chunk-check counts.
"""

import argparse
import json
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium

from project_paths import main_json, main_pdf

ROOT = Path(__file__).parent
SRC = ROOT / "test_protocols"
OUT = ROOT / "output_tests"


def protocols() -> list[tuple[str, Path]]:
    pdfs = sorted(SRC.glob("*.pdf"), key=lambda p: p.name.lower())
    mapping = {f"P{i}": str(p) for i, p in enumerate(pdfs, start=1)}
    OUT.mkdir(exist_ok=True)
    (OUT / "protocols.json").write_text(json.dumps(mapping, indent=1), encoding="utf-8")
    return [(pid, Path(path)) for pid, path in mapping.items()]


def safe_error(exc: BaseException) -> str:
    """Exception type and our own code locations only (no message text)."""
    frames = [f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)
              if Path(f.filename).parent == ROOT]
    return f"{type(exc).__name__} at {' > '.join(frames[-3:]) or 'library code'}"


def run_logged(cmd: list[str], log: Path) -> tuple[int, float]:
    start = time.time()
    with open(log, "w", encoding="utf-8") as fh:
        rc = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT,
                            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}).returncode
    return rc, time.time() - start


def last_code_location(log: Path) -> str:
    """For a failed subprocess: the last frame in our code from its traceback."""
    frames = re.findall(r'File "([^"]+)", line (\d+)', log.read_text(encoding="utf-8", errors="replace"))
    ours = [f"{Path(f).name}:{n}" for f, n in frames if Path(f).parent == ROOT]
    return ours[-1] if ours else "library code"


def cmd_convert(args) -> None:
    for pid, pdf in protocols():
        if args.only and pid not in args.only:
            continue
        out = OUT / pid
        out.mkdir(parents=True, exist_ok=True)
        pages = len(pdfium.PdfDocument(str(pdf)))
        print(f"{pid}: {pages} pages, converting ...", flush=True)
        rc, secs = run_logged([sys.executable, "convert_protocol.py", str(pdf), "--out", str(out),
                               "--figure-model", ""], out / "convert.log")
        status = "ok" if rc == 0 else f"FAILED (exit {rc}, at {last_code_location(out / 'convert.log')})"
        print(f"{pid}: {status} in {secs / 60:.1f} min", flush=True)


def doc_json(pid: str) -> Path | None:
    files = [p for p in (OUT / pid).glob("*.json")
             if p.name not in ("protocols.json", "document.json") and not p.name.endswith(".decisions.json")]
    return files[0] if files else None


PUBLIC = ROOT / "public_protocols"


def source_pdf(label: str) -> Path:
    """The PDF of MAIN, P1..Pn (never print the path) or a public protocol folder name."""
    if label == "MAIN":
        return main_pdf()
    if (PUBLIC / label / "protocol.pdf").exists():
        return PUBLIC / label / "protocol.pdf"
    return dict(protocols())[label]


def output_dir(label: str) -> Path:
    if label == "MAIN":
        return ROOT / "output"
    if (PUBLIC / label).is_dir():
        return ROOT / "output_public" / label
    return OUT / label


def raw_doc(label: str):
    """The document as converted, BEFORE post-processing: the cached page batches, concatenated
    (as convert_protocol.py does). For MAIN, P1..Pn or a public protocol; the batch folder is the
    most recently written manifest under <out>/_chunks/. Never print paths for P protocols."""
    from docling_core.types.doc import DoclingDocument

    manifests = sorted((output_dir(label) / "_chunks").glob("*/*/manifest.json"),
                       key=lambda p: p.stat().st_mtime)
    if not manifests:
        raise FileNotFoundError("no batch cache")
    batch_dir = manifests[-1].parent
    files = sorted(f for f in batch_dir.glob("*.json") if f.name != "manifest.json")
    doc = DoclingDocument.concatenate([DoclingDocument.load_from_json(f) for f in files])
    doc.name = source_pdf(label).stem
    return doc


def final_doc_json(label: str) -> Path | None:
    """The post-processed Docling JSON of MAIN, P1..Pn or a public protocol."""
    if label.startswith("P") and label[1:].isdigit():
        return doc_json(label)
    out = output_dir(label)
    files = [p for p in out.glob("*.json") if p.name not in ("document.json",) and not p.name.endswith(".decisions.json")]
    return files[0] if files else None


def decision_counts(pid: str) -> dict:
    """Aggregate the post-processing decisions log (refs/pages/reasons only) into counts."""
    files = list((OUT / pid).glob("*.decisions.json"))
    if not files:
        return {}
    log = json.loads(files[0].read_text(encoding="utf-8"))
    out = {}
    for key, val in log.items():
        if isinstance(val, list):
            reasons = Counter(str(v.get("reason") or v.get("decision") or v.get("action") or "")
                              for v in val if isinstance(v, dict))
            out[key] = dict(reasons) if any(reasons) and len(reasons) > 1 else len(val)
        elif isinstance(val, dict):
            out[key] = {k: v for k, v in val.items() if isinstance(v, (int, float, bool))}
        elif isinstance(val, (int, float, bool)) or (isinstance(val, str) and len(val) <= 20 and val.isidentifier()):
            out[key] = val  # e.g. heading_source = "outline", caption_position = "above"
    return out


def heading_metrics(doc) -> dict:
    from docling_core.types.doc import SectionHeaderItem

    from protocol_postprocess import _SECTION_NUM

    heads = [h for h, _ in doc.iterate_items() if isinstance(h, SectionHeaderItem)]
    numbered = sum(bool(_SECTION_NUM.match(h.text.strip())) for h in heads)
    levels = Counter(h.level for h in heads)
    return {"headings": len(heads), "numbered_share": round(numbered / len(heads), 2) if heads else 0,
            "levels": dict(sorted(levels.items()))}


def postprocess_counts(log: Path) -> dict:
    m = re.search(r"Post-processing: (\{.*?\})", log.read_text(encoding="utf-8", errors="replace"))
    return json.loads(m.group(1).replace("'", '"')) if m else {}


def cmd_report(args) -> None:
    from docling_core.types.doc import DoclingDocument

    from validate_output import page_coverage

    for pid, pdf in protocols():
        if args.only and pid not in args.only:
            continue
        print(f"\n=== {pid}")
        path = doc_json(pid)
        if path is None:
            print("  not converted")
            continue
        try:
            doc = DoclingDocument.load_from_json(path)
            pages = len(pdfium.PdfDocument(str(pdf)))
            print(f"  pages {pages} | tables {len(doc.tables)} | pictures {len(doc.pictures)} | "
                  f"text items {len(doc.texts)}")
            print(f"  post-processing: {postprocess_counts(OUT / pid / 'convert.log')}")
            print(f"  decisions: {decision_counts(pid)}")
            print(f"  headings: {heading_metrics(doc)}")
            report = page_coverage(doc, pdfium.PdfDocument(str(pdf)))
            checked = [r for r in report if not r["expected"]]
            words = sum(r["pdf_words"] for r in checked)
            cov = sum(r["pdf_words"] * r["coverage"] for r in checked) / words if words else 0
            low = [r["coverage"] for r in checked if r["coverage"] < 0.97]
            print(f"  word coverage {cov:.2%} on {len(checked)} pages ({len(report) - len(checked)} TOC pages "
                  f"skipped); pages below 97%: {len(low)}"
                  + (f" (lowest {min(low):.0%})" if low else ""))
        except Exception as exc:  # noqa: BLE001 - report and continue with the next protocol
            print(f"  analysis failed: {safe_error(exc)}")
            continue

        chunks_dir = OUT / pid / "chunks"
        rc, secs = run_logged([sys.executable, "chunk_protocol.py", str(path), "--out", str(chunks_dir),
                               "--quiet-text"], OUT / pid / "chunk.log")
        if rc != 0:
            print(f"  chunking FAILED (exit {rc}, at {last_code_location(OUT / pid / 'chunk.log')})")
            continue
        stats = json.loads((chunks_dir / "stats.json").read_text(encoding="utf-8"))
        meta = stats.get("document", {})
        detected = {k: bool(meta.get(k)) for k in
                    ("protocol_id", "acronym", "title", "amendment", "version_label", "version_date")}
        glossary = chunks_dir / "abbreviations.json"
        n_gloss = len(json.loads(glossary.read_text(encoding="utf-8"))) if glossary.exists() else 0
        tokens = stats.get("tokens", {})
        print(f"  metadata detected: {detected}")
        print(f"  glossary entries: {n_gloss}")
        print(f"  chunks {stats.get('chunks')} | tokens " + ", ".join(
            f"{k} {v}" for k, v in tokens.items() if isinstance(v, (int, float))))
        print(f"  chunk types: {stats.get('chunk_types')}")
        print("  checks: " + ", ".join(f"{k} {len(v)}" for k, v in stats.get("checks", {}).items()))


def cmd_diagnose(args) -> None:
    """Where do the PDF words missing from the output go? (counts only)

    For every word in the PDF text layer that is missing from the converted
    page: is it in that page's furniture (header/footer items, i.e. a checker
    false alarm), elsewhere in the converted document (misplaced), or nowhere
    (lost)? Also: are low-coverage pages rotated, landscape or table pages, and
    do the missing words look like numbers?
    """
    from docling_core.types.doc import ContentLayer, DoclingDocument, TableItem, TextItem

    from validate_output import _words, page_coverage

    for pid, pdf_path in protocols():
        if args.only and pid not in args.only:
            continue
        path = doc_json(pid)
        if path is None:
            continue
        doc = DoclingDocument.load_from_json(path)
        pdf = pdfium.PdfDocument(str(pdf_path))
        report = page_coverage(doc, pdf)
        furniture, body_all = {}, Counter()
        table_pages = set()
        for item, _ in doc.iterate_items(traverse_pictures=True,
                                         included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE}):
            pages = {p.page_no for p in getattr(item, "prov", [])}
            if isinstance(item, TableItem):
                table_pages |= pages
                words = Counter()
                for c in item.data.table_cells:
                    words += _words(c.text)
            elif isinstance(item, TextItem):
                words = _words(f"{getattr(item, 'marker', '') or ''} {item.text}")
            else:
                continue
            if getattr(item, "content_layer", None) is not None and item.content_layer.value == "furniture":
                for p in pages:
                    furniture.setdefault(p, Counter()).update(words)
            else:
                body_all.update(words)
        cats = Counter()
        low = [r for r in report if not r["expected"] and r["coverage"] < 0.97]
        page_kind = Counter()
        for r in low:
            page = pdf[r["page"] - 1]
            w, h = page.get_size()
            page_kind["rotated" if page.get_rotation() else "unrotated"] += 1
            page_kind["landscape" if w > h else "portrait"] += 1
            page_kind["has_table" if r["page"] in table_pages else "no_table"] += 1
        for r in report:
            if r["expected"]:
                continue
            # page_coverage keeps only the top missing words; recompute the full multiset
            lines = [" ".join(l.split()) for l in pdf[r["page"] - 1].get_textpage().get_text_range().splitlines()]
            source = Counter()
            for l in lines:
                source += _words(l)
            covered = Counter()
            for item, _ in doc.iterate_items(traverse_pictures=True):
                if r["page"] in {p.page_no for p in getattr(item, "prov", [])}:
                    if isinstance(item, TableItem):
                        for c in item.data.table_cells:
                            covered += _words(c.text)
                    elif isinstance(item, TextItem):
                        covered += _words(f"{getattr(item, 'marker', '') or ''} {item.text}")
            missing = source - covered
            fur = furniture.get(r["page"], Counter())
            for word, n in missing.items():
                kind = ("furniture" if fur.get(word) else "elsewhere_in_doc" if body_all.get(word) else "lost")
                cats[kind] += n
                cats["numeric" if word.isdigit() else "alphabetic"] += n
        total = sum(cats[k] for k in ("furniture", "elsewhere_in_doc", "lost"))
        print(f"\n=== {pid}: {len(low)} low pages; missing word occurrences {total}")
        print("  missing words: " + ", ".join(
            f"{k} {cats[k]} ({cats[k] / total:.0%})" for k in ("furniture", "elsewhere_in_doc", "lost", "numeric")) if total else "")
        print(f"  low pages: {dict(page_kind)}; rotated pages in PDF: "
              f"{sum(1 for i in range(len(pdf)) if pdf[i].get_rotation())} of {len(pdf)}")


def cmd_metadata(args) -> None:
    """Why is version/amendment/acronym not detected? Pattern-hit counts per location only."""
    from docling_core.types.doc import ContentLayer, DocItemLabel, DoclingDocument, SectionHeaderItem

    from chunk_protocol import _VERSION, looks_like_acronym

    probes = {
        "kw_version": r"(?i)\bversion\b", "kw_amendment": r"(?i)\bamendment\b", "kw_edition": r"(?i)\bedition\b",
        "kw_revision": r"(?i)\brevision\b", "kw_original": r"(?i)\boriginal protocol\b",
        "v_number": r"(?i)\bv\.?\s?\d+(\.\d+)*\b", "kw_then_v_number": r"(?i)\b(version|amendment)\b\W{0,5}v\d",
        "kw_then_number": r"(?i)\b(version|amendment|edition|revision)\b\D{0,20}\d",
        "current_VERSION_regex": _VERSION.pattern,
    }
    for pid, _ in protocols():
        if args.only and pid not in args.only:
            continue
        path = doc_json(pid)
        if path is None:
            continue
        doc = DoclingDocument.load_from_json(path)
        places = {
            "header_footer": [t.text for t in doc.texts if t.label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER)],
            "page1_text": [t.text for t in doc.texts if t.prov and t.prov[0].page_no == 1
                           and t.content_layer == ContentLayer.BODY],
            "page1_table_cells": [c.text for tb in doc.tables if tb.prov and tb.prov[0].page_no == 1
                                  for c in tb.data.table_cells],
            "pages1to3_body": [t.text for t in doc.texts if t.prov and t.prov[0].page_no <= 3
                               and t.content_layer == ContentLayer.BODY]
                              + [c.text for tb in doc.tables if tb.prov and tb.prov[0].page_no <= 3
                                 for c in tb.data.table_cells],
        }
        print(f"\n=== {pid}")
        for place, texts in places.items():
            hits = {name: sum(bool(re.search(p, t, re.I if name == "current_VERSION_regex" else 0)) for t in texts)
                    for name, p in probes.items()}
            print(f"  {place:18s} items {len(texts):4d} | " + ", ".join(f"{k} {v}" for k, v in hits.items() if v))
        heads = [t for t in doc.texts if t.prov and t.prov[0].page_no == 1 and isinstance(t, SectionHeaderItem)]
        titles = [t for t in doc.texts if t.prov and t.prov[0].page_no == 1 and t.label == DocItemLabel.TITLE]
        print(f"  page-1 headings {len(heads)}, title items {len(titles)}, acronym-like headings "
              f"{sum(looks_like_acronym(t.text) for t in heads + titles)}, page-1 tables "
              f"{sum(1 for tb in doc.tables if tb.prov and tb.prov[0].page_no == 1)}")


def _empty_cols(table) -> int:
    """Columns with no text in any body row."""
    from table_merge import _header_rows

    h = _header_rows(table)
    filled = {c for cell in table.data.table_cells if cell.start_row_offset_idx >= h and cell.text.strip()
              for c in range(cell.start_col_offset_idx, cell.end_col_offset_idx)}
    return table.data.num_cols - len(filled)


def _col_headers(table) -> list[str]:
    from table_merge import _header_rows

    h = max(_header_rows(table), 1)
    out = []
    for col in range(table.data.num_cols):
        cells = sorted({(c.start_row_offset_idx, c.start_col_offset_idx, c.text) for c in table.data.table_cells
                        if c.start_row_offset_idx < h and c.start_col_offset_idx <= col < c.end_col_offset_idx})
        out.append("|".join("".join(t.split()).lower() for _, _, t in cells))
    return out


def _col_stats(table, col: int) -> tuple[float, float]:
    """(share of body rows with text in this column, mean chars when filled)."""
    from table_merge import _header_rows

    h = _header_rows(table)
    body = max(table.data.num_rows - h, 1)
    texts = [c.text.strip() for c in table.data.table_cells
             if c.start_row_offset_idx >= h and c.start_col_offset_idx == col and c.text.strip()]
    return len(texts) / body, (sum(map(len, texts)) / len(texts) if texts else 0)


def _complementary(table, c1: int, c2: int) -> float:
    """Share of body rows with text in c1 or c2 that have text in exactly one of them."""
    from table_merge import _header_rows

    h = _header_rows(table)
    filled = {c: {cell.start_row_offset_idx for cell in table.data.table_cells
                  if cell.start_row_offset_idx >= h and cell.start_col_offset_idx == c and cell.text.strip()}
              for c in (c1, c2)}
    union = filled[c1] | filled[c2]
    return len(filled[c1] ^ filled[c2]) / len(union) if union else 1.0


def column_alignment(a, b) -> None:
    """How the column headers of a and b line up (numbers only)."""
    from difflib import SequenceMatcher

    ha, hb = _col_headers(a), _col_headers(b)
    sm = SequenceMatcher(None, ha, hb, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            print(f"      equal cols a{i1}-{i2 - 1} = b{j1}-{j2 - 1}")
            continue
        print(f"      {tag} a{i1}-{i2 - 1} / b{j1}-{j2 - 1}: header chars a {[len(x) for x in ha[i1:i2]]} "
              f"b {[len(x) for x in hb[j1:j2]]} | joined-equal "
              f"{''.join(ha[i1:i2]).replace('|', '') == ''.join(hb[j1:j2]).replace('|', '')}")
        for t, cols in (("a", range(i1, i2)), ("b", range(j1, j2))):
            tab = a if t == "a" else b
            for c in cols:
                fill, chars = _col_stats(tab, c)
                nb = [f"{_complementary(tab, c, d):.0%}" for d in (c - 1, c + 1) if 0 <= d < tab.data.num_cols]
                print(f"        {t}{c}: body fill {fill:.0%}, mean chars {chars:.0f}, "
                      f"exclusive-with-neighbours {nb}")


def table_diagnostics(doc, json_path: Path, chunks_dir: Path) -> None:
    """Numbers only: rejected cols_differ pairs, and tables_split_mid_row chunk lines."""
    from docling_core.types.doc import ContentLayer, TableItem

    from table_merge import PageBoxes, _continuation_reason, _header_rows, _is_continuation

    boxes = PageBoxes(doc)
    pairs = []
    for parent in [doc.body, *doc.groups]:
        prev = None
        for ref in parent.children:
            item = ref.resolve(doc)
            if item.content_layer == ContentLayer.FURNITURE:
                continue
            if isinstance(item, TableItem) and prev is not None:
                pairs.append((prev, item))
            prev = item if isinstance(item, TableItem) else None
    reasons = Counter(_is_continuation(a, b, boxes)[1] for a, b in pairs)
    print(f"  adjacent table pairs {len(pairs)}: {dict(reasons)}")
    for a, b in pairs:
        ok, reason = _is_continuation(a, b, boxes)
        if reason != "cols_differ":
            continue
        print(f"    cols_differ: pages {a.prov[-1].page_no}->{b.prov[0].page_no} | cols {a.data.num_cols}->"
              f"{b.data.num_cols} | rows {a.data.num_rows}/{b.data.num_rows} | header rows {_header_rows(a)}/"
              f"{_header_rows(b)} | empty body cols {_empty_cols(a)}/{_empty_cols(b)} | header match "
              f"{_continuation_reason(a, b)} | next has caption {bool(b.captions)} | prev has caption "
              f"{bool(a.captions)} | ends/starts page {boxes.ends_page(a.prov[-1])}/{boxes.starts_page(b.prov[0])}")
        if _continuation_reason(a, b) in ("header_text", "header_words"):
            column_alignment(a, b)

    stats = json.loads((chunks_dir / "stats.json").read_text(encoding="utf-8"))
    flagged = stats.get("checks", {}).get("tables_split_mid_row", [])
    if not flagged:
        return
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-Embedding-8B")
    n = lambda s: len(tok.encode(s, add_special_tokens=False))  # noqa: E731
    records = {json.loads(l)["chunk_id"]: json.loads(l)
               for l in (chunks_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    tables = {t.self_ref: t for t in doc.tables}
    for entry in flagged:
        cid, ref = entry.split(" (")[0], entry.split(" (")[1].rstrip(")")
        rec, table = records[cid], tables[ref]
        rows = set(rows_of(doc, table))
        print(f"    mid-row chunk: chunk tokens {rec['n_tokens']} | table rows {table.data.num_rows} cols "
              f"{table.data.num_cols} | table parts {sum(ref in r['doc_item_refs'] for r in records.values())}")
        for line in rec["text"].splitlines():
            if len(line) <= 15 or line in rows:
                continue
            hosts = [r for r in rows if line in r]
            if not hosts:
                continue
            host = hosts[0]
            label, sep, _ = host.partition(": ")
            print(f"      fragment tokens {n(line)} of row tokens {n(host)} | row has label sep {bool(sep)} | "
                  f"label chars {len(label) if sep else 0} | fragment at row start {host.startswith(line)} | "
                  f"fragment at row end {host.rstrip().endswith(line.rstrip())} | host rows {len(hosts)}")


def rows_of(doc, table) -> list[str]:
    from chunk_protocol import ProtocolSerializerProvider

    ser = ProtocolSerializerProvider().get_serializer(doc)
    _, rows = ser.table_serializer.split_prefix(ser.serialize(item=table).text, table, doc, ser)
    return [r.rstrip("\n") for r in rows]


def cmd_tables(args) -> None:
    from docling_core.types.doc import DoclingDocument

    targets = [(pid, doc_json(pid), OUT / pid / "chunks") for pid, _ in protocols()
               if not args.only or pid in args.only]
    if args.main:
        targets.insert(0, ("MAIN", main_json(), ROOT / "output" / "chunks"))
    for pid, path, chunks_dir in targets:
        print(f"\n=== {pid}")
        if path is None:
            print("  not converted")
            continue
        try:
            table_diagnostics(DoclingDocument.load_from_json(path), path, chunks_dir)
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {safe_error(exc)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("tables")
    p.add_argument("--only", nargs="*")
    p.add_argument("--main", action="store_true", help="also the main protocol (output/)")
    p.set_defaults(func=cmd_tables)
    for name, func in (("convert", cmd_convert), ("report", cmd_report), ("diagnose", cmd_diagnose),
                       ("metadata", cmd_metadata)):
        p = sub.add_parser(name)
        p.add_argument("--only", nargs="*", help="protocol ids, e.g. P1 P3")
        p.set_defaults(func=func)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
