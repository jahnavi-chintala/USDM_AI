"""Check that no text from the PDF was lost during conversion.

For every page, the words in the PDF's own text layer are compared with the
words of the converted document's items on that page (text, list items,
headings, captions, footnotes, table cells; a table counts for every page it
spans). Running headers/footers are excluded on both sides: lines in the
top/bottom band of a page whose digit-normalised form ("Page #") is in that
band on >= 30 % of pages, and page-number lines ("12", "Page 12 of 140").
Pages whose coverage falls below the threshold are reported with their missing
words; --aggregate prints numbers only (for confidential documents).

Expected gaps: table-of-contents pages (rebuilt from the headings) and the
list of tables. Figure text is not in the PDF text layer (it is in images), so
it does not affect coverage.

Word coverage ignores one-character tokens, so a lost Schedule-of-Activities
mark ("X", "✓") would be invisible. Table QA therefore also counts, per table,
the one-character marks in its cells against the marks the text layer holds
inside the table's boxes (a mark may carry a footnote marker: "X^a", "Xb,c"),
and reports the ruling-line (lattice) check: the decisions log's verdicts on
the raw page fragments, and a fresh verification of the final single-page
tables (table_lattice.py). Numbers only in --aggregate mode.
"""

import argparse
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path

import pypdfium2 as pdfium
from docling_core.types.doc import ContentLayer, DoclingDocument, TableItem, TextItem

_WORD = re.compile(r"[^\W_]+")  # Unicode letters and digits, not just ASCII ("_" separates, as before)
# A one-character table mark, optionally followed by a footnote marker ("X", "X^a", "Xb,c", "✓*").
MARK = re.compile(r"^[XＸ✓✔√•●○◦■□▪☐☑☒✗✘](?:\^?[a-z0-9*†‡§¶,]{1,5})?$")
PAGE_NUMBER = re.compile(r"(?i)(page\s+)?\d+(\s+of\s+\d+)?")
FURNITURE_SHARE = 0.3  # share of pages a band line must repeat on (odd/even headers each reach ~50 %)
BAND = 0.12  # top and bottom share of the page height searched for running headers/footers


def _words(text: str) -> Counter:
    # "^" marks a superscript written by text_reconcile ("10^9/L", "ADA^a"); the PDF has no caret.
    return Counter(w for w in _WORD.findall(text.lower().replace("^", " ")) if len(w) > 1 or w.isdigit())


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", " ".join(line.split()))


def _band_lines(page: pdfium.PdfPage) -> set[str]:
    """Digit-normalised lines in the top and bottom bands of a page."""
    left, bottom, right, top = page.get_bbox()
    band = (top - bottom) * BAND
    tp = page.get_textpage()
    text = (tp.get_text_bounded(left=left, bottom=top - band, right=right, top=top) + "\n"
            + tp.get_text_bounded(left=left, bottom=bottom, right=right, top=bottom + band))
    return {_norm(l) for l in text.splitlines() if l.strip()}


def _furniture_lines(bands: list[set[str]]) -> set[str]:
    """Band lines repeated on many pages: running headers and footers."""
    counts = Counter(l for lines in bands for l in lines)
    return {l for l, n in counts.items() if n >= max(2, len(bands) * FURNITURE_SHARE)}


def page_coverage(doc: DoclingDocument, pdf: pdfium.PdfDocument) -> list[dict]:
    pdf_lines = [
        [" ".join(l.split()) for l in pdf[i].get_textpage().get_text_range().splitlines() if l.strip()]
        for i in range(len(pdf))
    ]
    bands = [_band_lines(pdf[i]) for i in range(len(pdf))]
    furniture = _furniture_lines(bands)

    pool: dict[int, Counter] = {}
    furniture_pool: dict[int, Counter] = {}  # words of items Docling labelled page header/footer
    for item, _ in doc.iterate_items(traverse_pictures=True,
                                     included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE}):
        is_furniture = getattr(item, "content_layer", None) == ContentLayer.FURNITURE
        target = furniture_pool if is_furniture else pool
        pages = {p.page_no for p in getattr(item, "prov", [])}
        if isinstance(item, TableItem):
            words = Counter()
            for c in item.data.table_cells:
                words += _words(c.text)
        elif isinstance(item, TextItem):
            # List numbers ("9.") are stored as the item's marker, not its text.
            words = _words(f"{getattr(item, 'marker', '') or ''} {item.text}")
        else:
            continue
        for p in pages:
            target.setdefault(p, Counter()).update(words)

    report = []
    for i, lines in enumerate(pdf_lines, start=1):
        # Page numbers and running header/footer lines (in this page's bands) are excluded.
        source = Counter()
        for l in lines:
            n = _norm(l)
            if not (n in furniture and n in bands[i - 1]) and not PAGE_NUMBER.fullmatch(l):
                source += _words(l)
        total = sum(source.values())
        if not total:
            continue
        not_in_body = source - pool.get(i, Counter())
        # Words in this page's header/footer items were recognised as furniture by
        # Docling (running headers vary by template, so line matching alone misses
        # many); they count as accounted for but are reported separately, so body
        # text mislabelled as furniture stays visible.
        missing = not_in_body - furniture_pool.get(i, Counter())
        report.append({
            "page": i,
            "coverage": round(1 - sum(missing.values()) / total, 4),
            "pdf_words": total,
            "furniture_words": sum(not_in_body.values()) - sum(missing.values()),
            "missing": [w for w, _ in missing.most_common(25)],
            # TOC pages are rebuilt from the headings, so their gap is intended.
            "expected": sum(bool(re.search(r"\.{5,}", l)) for l in lines) >= 5,
        })
    return report


def _mark_map() -> dict:
    try:  # Symbol/Wingdings private-use glyphs as the pipeline maps them (U+F0FC -> "✓", ...)
        from protocol_postprocess import _SYMBOL_FONT
        return str.maketrans(_SYMBOL_FONT)
    except Exception:  # noqa: BLE001
        return str.maketrans({})


def _marks(tokens, trans) -> int:
    return sum(1 for w in tokens if MARK.match(unicodedata.normalize("NFKC", w).translate(trans)))


def table_checks(doc: DoclingDocument, pdf: pdfium.PdfDocument, decisions: dict | None = None,
                 lattice: bool = True) -> dict:
    """Table QA (numbers only).

    marks: per table, one-character marks in the cells vs in the text layer
    inside the table's boxes (all pages of a merged table); a table "misses"
    marks when the text layer holds more. lattice: verdict counts from the
    decisions log ("table_lattice", raw fragments) and a verification of the
    final single-page tables against the ruling lines.
    """
    from table_lattice import PageFrame, check_table, PageCache, page_words

    from table_merge import leading_header_rows

    trans = _mark_map()
    words: dict[int, list] = {}
    per_table = []
    for t in doc.tables:
        if t.label.value != "table" or not t.prov:
            continue
        in_doc = _marks((w for c in t.data.table_cells for w in c.text.split()), trans)
        # A merged table keeps its header once; the PDF repeats it on every page. Its height
        # (from the first page's header cells) is left out on the continuation pages.
        band = 0.0
        n_head = leading_header_rows(t)
        if len(t.prov) > 1 and n_head:
            h0 = doc.pages[t.prov[0].page_no].size.height
            top0 = t.prov[0].bbox.to_top_left_origin(h0).t
            bottoms = [c.bbox.to_top_left_origin(h0).b for c in t.data.table_cells
                       if c.start_row_offset_idx < n_head and c.bbox is not None]
            band = max(bottoms) - top0 if bottoms else 0.0
        in_pdf = 0
        for k, p in enumerate(t.prov):
            if p.page_no not in words:
                page = pdf[p.page_no - 1]
                words[p.page_no] = page_words(page, PageFrame(page))
            l, b, r, top = p.bbox.to_bottom_left_origin(doc.pages[p.page_no].size.height).as_tuple()
            limit = top - band if k else top  # below the repeated header on continuation pages
            in_pdf += _marks((w for w, (wl, wb, wr, wt) in words[p.page_no]
                              if l - 1 <= (wl + wr) / 2 <= r + 1 and b - 1 <= (wb + wt) / 2 <= limit + 1), trans)
        per_table.append({"table": t.self_ref, "pages": [p.page_no for p in t.prov], "marks_doc": in_doc,
                          "marks_pdf": in_pdf})
    out = {
        "tables": len(per_table),
        "tables_with_marks": sum(1 for x in per_table if x["marks_pdf"] or x["marks_doc"]),
        "marks_pdf": sum(x["marks_pdf"] for x in per_table),
        "marks_doc": sum(x["marks_doc"] for x in per_table),
        "marks_missing": sum(max(0, x["marks_pdf"] - x["marks_doc"]) for x in per_table),
        "tables_missing_marks": sum(1 for x in per_table if x["marks_pdf"] > x["marks_doc"]),
        "tables_extra_marks": sum(1 for x in per_table if x["marks_doc"] > x["marks_pdf"]),
    }
    if decisions and decisions.get("table_lattice"):
        out["lattice_raw"] = dict(Counter(e.get("action") for e in decisions["table_lattice"]))
    if lattice:
        cache, verdicts = PageCache(pdf), Counter()
        for t in doc.tables:
            if t.label.value != "table" or not t.prov:
                continue
            if len(t.prov) > 1:
                verdicts["multi_page_not_checked"] += 1
                continue
            entry = check_table(t, doc, cache, mode="verify")
            verdicts[entry["action"] + (f":{entry['reason']}" if entry.get("reason") else "")] += 1
        out["lattice_final"] = dict(verdicts)
    out["per_table"] = per_table
    return out


def aggregate(report: list[dict], threshold: float = 0.97) -> dict:
    """Coverage summary with numbers only (no words), for confidential documents."""
    checked = [r for r in report if not r["expected"]]
    words = sum(r["pdf_words"] for r in checked)
    low = [r for r in checked if r["coverage"] < threshold]
    return {
        "coverage": round(sum(r["pdf_words"] * r["coverage"] for r in checked) / words, 4) if words else None,
        "pdf_words": words,
        "furniture_share": round(sum(r.get("furniture_words", 0) for r in checked) / words, 4) if words else None,
        "pages_checked": len(checked),
        "toc_pages_skipped": len(report) - len(checked),
        "pages_below_threshold": len(low),
        "lowest_page_coverage": min((r["coverage"] for r in checked), default=None),
        "pages_below": [r["page"] for r in low],
    }


def quality_report(doc: DoclingDocument, pdf: pdfium.PdfDocument, decisions: dict | None = None,
                   threshold: float = 0.97) -> dict:
    """Numbers-only quality report of a converted document: word coverage against the PDF text layer
    (per page and overall), table QA (marks, ruling-line verdicts) and figure counts. No document text."""
    from figure_vlm import skip_figure

    report = page_coverage(doc, pdf)
    tables = table_checks(doc, pdf, decisions)
    pictures = list(doc.pictures)
    described = sum(1 for p in pictures if p.meta is not None and getattr(p.meta, "description", None))
    figures = {"pictures": len(pictures), "skipped_logo_like": sum(1 for p in pictures if skip_figure(p, doc)),
               "described": described}
    return {
        "coverage": aggregate(report, threshold),
        "pages": [{k: v for k, v in r.items() if k != "missing"} for r in report],
        "tables": {k: v for k, v in tables.items() if k != "per_table"},
        "tables_per_table": [{k: v for k, v in t.items()} for t in tables.get("per_table", [])],
        "figures": figures,
    }


def main() -> None:
    from project_paths import main_json, main_pdf

    parser = argparse.ArgumentParser()
    parser.add_argument("json", nargs="?", default=str(main_json()))
    parser.add_argument("--pdf", default=str(main_pdf()))
    parser.add_argument("--threshold", type=float, default=0.97)
    parser.add_argument("--report", help="write the full per-page report as JSON")
    parser.add_argument("--aggregate", action="store_true",
                        help="print numbers only, never document words (confidential documents)")
    parser.add_argument("--no-tables", action="store_true", help="skip the table QA lines")
    args = parser.parse_args()

    doc = DoclingDocument.load_from_json(Path(args.json))
    pdf = pdfium.PdfDocument(args.pdf)
    report = page_coverage(doc, pdf)
    tables = None
    if not args.no_tables:
        dec_path = Path(args.json).with_name(Path(args.json).stem + ".decisions.json")
        decisions = json.loads(dec_path.read_text(encoding="utf-8")) if dec_path.exists() else None
        tables = table_checks(doc, pdf, decisions)
    if args.aggregate:
        print(json.dumps(aggregate(report, args.threshold)))
        if tables is not None:  # table lines: numbers only
            print(json.dumps({"tables": {k: v for k, v in tables.items() if k != "per_table"}}))
        if args.report:  # numbers only here too
            Path(args.report).write_text(json.dumps(
                [{k: v for k, v in r.items() if k != "missing"} for r in report], indent=2), encoding="utf-8")
        return
    checked = [r for r in report if not r["expected"]]
    words = sum(r["pdf_words"] for r in checked)
    covered = sum(r["pdf_words"] * r["coverage"] for r in checked)
    low = [r for r in checked if r["coverage"] < args.threshold]
    furniture = sum(r["furniture_words"] for r in checked)
    print(f"Word coverage: {covered / words:.2%} of {words} PDF words on {len(checked)} pages "
          f"({len(report) - len(checked)} TOC pages skipped); {len(low)} pages below {args.threshold:.0%}; "
          f"{furniture} words ({furniture / words:.1%}) accounted for by header/footer items")
    for r in low:
        print(f"  p{r['page']:<4} {r['coverage']:.1%}  missing: {' '.join(r['missing'][:15])}")
    if tables is not None:
        print(f"Table marks: {tables['marks_doc']} in tables / {tables['marks_pdf']} in the text layer "
              f"({tables['tables_with_marks']} of {tables['tables']} tables have marks); "
              f"{tables['marks_missing']} missing in {tables['tables_missing_marks']} tables, "
              f"{tables['tables_extra_marks']} tables with extra marks")
        for x in tables["per_table"]:
            if x["marks_pdf"] != x["marks_doc"]:
                print(f"  {x['table']} p{x['pages'][0]}-{x['pages'][-1]}: {x['marks_doc']} marks in cells, "
                      f"{x['marks_pdf']} in the text layer")
        if "lattice_raw" in tables:
            print(f"Ruling-line check, raw page fragments (decisions log): {tables['lattice_raw']}")
        print(f"Ruling-line check, final single-page tables: {tables.get('lattice_final')}")
    if args.report:
        Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
