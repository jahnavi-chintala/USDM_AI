"""Self-test for text_reconcile.py on synthetic PDFs (made-up text, written by hand).

Run: `python tests/test_text_reconcile.py`. Each case draws a line the way Word
does (superscripts as smaller raised runs, hyphens at line ends, words placed
with a gap but no space glyph), gives the converted item the text Docling
produces for it ("10 9 /L", "EGFRdirected", "WHOARE") and checks the edit, plus
negative cases that must stay untouched. Coordinates are PDF points, bottom-left
origin, on a 612 x 792 page.
"""

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pypdfium2 as pdfium
from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size,
                                    TableCell, TableData)

import text_reconcile as tr

# ---------------------------------------------------------------- a minimal PDF writer


def pdf_bytes(content: bytes) -> bytes:
    """One 612 x 792 page; F1 = Helvetica (WinAnsi), F3 = Symbol."""
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R /F3 5 0 R >> >> /Contents 6 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Symbol >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
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


def line(y: float, *runs, x: float = 72, size: float = 10) -> bytes:
    """One text line from runs: str (normal), ("sup"|"sub"|"small"|"raised", str), ("sym", hex code),
    ("gap", thousandths of an em with no glyph, as Word places some words)."""
    ops = [b"BT /F1 %g Tf %g %g Td" % (size, x, y)]
    for r in runs:
        if isinstance(r, str):
            ops.append(b"/F1 %g Tf 0 Ts " % size + esc(r) + b" Tj")
        elif r[0] == "sup":
            ops.append(b"/F1 %g Tf %g Ts " % (size * 0.65, size * 0.35) + esc(r[1]) + b" Tj")
        elif r[0] == "sub":
            ops.append(b"/F1 %g Tf %g Ts " % (size * 0.65, -size * 0.2) + esc(r[1]) + b" Tj")
        elif r[0] == "small":  # small caps: smaller, same baseline
            ops.append(b"/F1 %g Tf 0 Ts " % (size * 0.75) + esc(r[1]) + b" Tj")
        elif r[0] == "raised":  # full size, raised by 1/3 em (Word's "raised by 3 pt" position)
            ops.append(b"/F1 %g Tf %g Ts " % (size, size * 0.33) + esc(r[1]) + b" Tj")
        elif r[0] == "sym":
            ops.append(b"/F3 %g Tf 0 Ts <%02X> Tj" % (size, r[1]))
        elif r[0] == "gap":
            ops.append(b"[%d] TJ" % -r[1])
    ops.append(b"0 Ts ET")
    return b" ".join(ops)


def bl(t: float, b: float, l: float = 60, r: float = 560) -> BoundingBox:
    return BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.BOTTOMLEFT)


def new_doc(pages: int = 1) -> DoclingDocument:
    d = DoclingDocument(name="t")
    for p in range(1, pages + 1):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    return d


def item(d: DoclingDocument, text: str, top_line: float, lines: int = 1, label=DocItemLabel.TEXT, page: int = 1):
    """A text item whose box covers `lines` lines (12 pt apart) starting at baseline `top_line`."""
    box = bl(top_line + 10, top_line - 12 * (lines - 1) - 3)
    return d.add_text(label=label, text=text, prov=ProvenanceItem(page_no=page, bbox=box, charspan=(0, len(text))))


class Case:
    """A synthetic page plus a document whose items carry Docling-like text."""

    def __init__(self, content: list[bytes]):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "s.pdf"
        path.write_bytes(pdf_bytes(b"\n".join(content)))
        self.pdf = pdfium.PdfDocument(str(path))
        self.doc = new_doc()

    def run(self, log=None):
        return tr.reconcile_text(self.doc, self.pdf, log)

    def close(self):
        self.pdf.close()
        self.tmp.cleanup()


# ---------------------------------------------------------------- (a) superscripts and subscripts


def test_scripts() -> None:
    c = Case([
        line(700, "ANC ", ("sym", 0xB3), "1.0 x 10", ("sup", "9"), "/L and 1.73 m", ("sup", "2"), " BSA"),
        line(670, "Response", ("sup", "a"), " and X", ("sup", "b,c"), " were met."),
        line(640, "every 3", ("sup", "rd"), " cycle; PAXLOVID", ("sup", "TM"), " tablets"),
        line(610, "HbA", ("sub", "1c"), " below 8% (AUC", ("sub", "0-24"), ")"),
        line(580, ("sup", "a"), " Within 42 days of dosing"),
        line(550, "studies of ", ("sup", "11"), "C-labeled tracers"),
    ])
    d = c.doc
    exp = item(d, "ANC ≥1.0 x 10 9 /L and 1.73 m 2 BSA", 700)
    mark = item(d, "Response a and X b,c were met.", 670)
    word = item(d, "every 3 rd cycle; PAXLOVID TM tablets", 640)
    sub = item(d, "HbA 1c below 8% (AUC 0-24 )", 610)
    note = item(d, "a Within 42 days of dosing", 580, label=DocItemLabel.FOOTNOTE)
    iso = item(d, "studies of 11 C-labeled tracers", 550)
    log = {}
    counts = c.run(log)
    assert exp.text == "ANC ≥1.0 x 10^9/L and 1.73 m^2 BSA", exp.text
    assert mark.text == "Response^a and X^b,c were met.", mark.text
    assert word.text == "every 3rd cycle; PAXLOVID™ tablets", word.text
    assert sub.text == "HbA1c below 8% (AUC0-24)", sub.text
    assert note.text == "^a Within 42 days of dosing", note.text
    assert iso.text == "studies of ^11C-labeled tracers", iso.text
    assert counts["superscript"] == 6 and counts["subscript"] == 2, counts
    assert counts["ordinal"] == 1 and counts["trademark"] == 1, counts
    # the log has refs/pages/edit types, never text
    entries = log["text_reconcile"]
    assert all(set(e) <= {"item", "page", "edits", "cell"} for e in entries), entries
    assert all(isinstance(v, int) for e in entries for v in e["edits"].values())
    # idempotent: a second pass changes nothing
    again = c.run()
    assert not any(k in again for k in ("superscript", "subscript", "ordinal", "trademark")), again
    assert exp.text == "ANC ≥1.0 x 10^9/L and 1.73 m^2 BSA"
    c.close()


def test_glued_superscript_in_cell() -> None:
    # A table cell whose text came out glued ("109/L"), and one with a marker after a mark.
    c = Case([
        line(700, "<25 x 10", ("sup", "9"), "/L", x=80),
        line(680, "Fever", ("sup", "a"), x=80),
        line(660, "X", ("sup", "b"), x=80),
    ])
    cells = []
    for row, (text, y) in enumerate([("<25 x 109/L", 700), ("Fever a", 680), ("X b", 660)]):
        cells.append(TableCell(text=text, start_row_offset_idx=row, end_row_offset_idx=row + 1,
                               start_col_offset_idx=0, end_col_offset_idx=1,
                               bbox=BoundingBox(l=75, t=792 - y - 10, r=300, b=792 - y + 3,
                                                coord_origin=CoordOrigin.TOPLEFT)))
    table = c.doc.add_table(data=TableData(num_rows=3, num_cols=1, table_cells=cells),
                            prov=ProvenanceItem(page_no=1, bbox=bl(712, 650, 70, 310), charspan=(0, 0)))
    counts = c.run()
    assert [x.text for x in table.data.table_cells] == ["<25 x 10^9/L", "Fever^a", "X^b"], \
        [x.text for x in table.data.table_cells]
    assert counts["cells_checked"] == 3, counts
    c.close()


def test_full_size_raised_markers() -> None:
    # Some documents raise a footnote marker without shrinking it. A marker-shaped run right after its
    # word is a marker; a raised word, a capital, a run opening a line or one far from its word is not.
    c = Case([
        line(700, "Treatment Phase", ("raised", "a"), " starts", x=80),
        line(670, "Pregnancy test (WOCBP only) ", ("raised", "b"), x=80),
        line(640, "Sample P00", ("raised", "b,g"), " drawn", x=80),
        line(610, "Cohort ", ("raised", "Arm B"), " text", x=80),
        line(580, ("raised", "a"), " Note opening a line", x=80),
        line(550, "Wide gap", ("gap", 900), ("raised", "c"), x=80),
    ])
    d = c.doc
    phase = item(d, "Treatment Phasea starts", 700)
    preg = item(d, "Pregnancy test (WOCBP only) b", 670)
    pk = item(d, "Sample P00 b,g drawn", 640)
    word = item(d, "Cohort Arm B text", 610)
    note = item(d, "a Note opening a line", 580)
    far = item(d, "Wide gap c", 550)
    counts = c.run()
    assert phase.text == "Treatment Phase^a starts", phase.text
    assert preg.text == "Pregnancy test (WOCBP only)^b", preg.text
    assert pk.text == "Sample P00^b,g drawn", pk.text
    assert word.text == "Cohort Arm B text", word.text
    assert note.text == "a Note opening a line", note.text
    assert far.text == "Wide gap c", far.text
    assert counts["raised_marker"] == 3 and not counts.get("superscript"), counts
    assert not {k for k in c.run() if k in ("raised_marker", "superscript")}  # idempotent
    c.close()


def test_scripts_negative() -> None:
    # Small caps (smaller, same baseline) and a raised bullet opening a line are not scripts.
    c = Case([
        line(700, "Normal text with ", ("small", "SMALL CAPS"), " inside"),
        line(670, ("sup", "•"), " Serum creatinine"),
    ])
    caps = item(c.doc, "Normal text with SMALL CAPS inside", 700)
    bullet = item(c.doc, "• Serum creatinine", 670)
    c.run()
    assert caps.text == "Normal text with SMALL CAPS inside", caps.text
    assert bullet.text == "• Serum creatinine", bullet.text
    c.close()


# ---------------------------------------------------------------- (b) line-end hyphens


def test_hyphens_restored() -> None:
    c = Case([
        line(700, "prior EGFR-"), line(688, "directed therapy"),
        line(660, "J Clin Res. 2015;12(3):727-"), line(648, "38."),
        # suspended hyphens: the space glyph after the hyphen is kept ("Days 1- and 8") ...
        line(620, "on Days 1- "), line(608, "and 8 only"),
        # ... or, where pdfium drops it with a hyphen it takes for hyphenation, the document's
        # own usage elsewhere decides ("pre- and post-infusion" within a line)
        line(580, "pre- "), line(568, "and post-dose samples"),
        line(540, "Collect pre- and post-infusion."),
    ])
    compound = item(c.doc, "prior EGFRdirected therapy", 700, lines=2)
    numeric = item(c.doc, "J Clin Res. 2015;12(3):72738.", 660, lines=2)
    days = item(c.doc, "on Days 1and 8 only", 620, lines=2)
    suspended = item(c.doc, "preand post-dose samples", 580, lines=2)
    item(c.doc, "Collect pre- and post-infusion.", 540)
    counts = c.run()
    assert compound.text == "prior EGFR-directed therapy", compound.text
    assert numeric.text == "J Clin Res. 2015;12(3):727-38.", numeric.text
    assert days.text == "on Days 1- and 8 only", days.text
    assert suspended.text == "pre- and post-dose samples", suspended.text
    assert counts["hyphen_restored"] == 4, counts
    c.close()


def test_spaced_hyphens_in_cells() -> None:
    c = Case([
        line(700, "28-", x=80), line(688, "day cycle", x=80),
        line(660, "Cycles 1-", x=80), line(648, "3", x=80),
        line(620, "----------", x=80), line(608, "Consider", x=80),
        line(580, "every evero-", x=80), line(568, "limus dose", x=80),
        line(540, "Give everolimus daily.", x=80),
    ])
    rows = [("28- day cycle", 700), ("Cycles 1- 3", 660), ("---------- Consider", 620), ("every evero- limus dose", 580)]
    cells = [TableCell(text=t, start_row_offset_idx=i, end_row_offset_idx=i + 1, start_col_offset_idx=0,
                       end_col_offset_idx=1, bbox=BoundingBox(l=75, t=792 - y - 10, r=200, b=792 - y + 15,
                                                              coord_origin=CoordOrigin.TOPLEFT))
             for i, (t, y) in enumerate(rows)]
    table = c.doc.add_table(data=TableData(num_rows=4, num_cols=1, table_cells=cells),
                            prov=ProvenanceItem(page_no=1, bbox=bl(712, 560, 70, 210), charspan=(0, 0)))
    item(c.doc, "Give everolimus daily.", 540)
    counts = c.run()
    got = [x.text for x in table.data.table_cells]
    # a rule of dashes stays; a word hyphenated in a narrow cell is joined (the document
    # writes "everolimus" elsewhere and never "evero-limus")
    assert got == ["28-day cycle", "Cycles 1-3", "---------- Consider", "every everolimus dose"], got
    assert counts["hyphen_unspaced"] == 2 and counts["hyphen_joined"] == 1, counts
    c.close()


def test_hyphens_negative() -> None:
    c = Case([
        line(700, "Follow-up visits on Days 1-3 at 10:00 (see Section 5.2)."),
        line(670, "The treatment period is short."),
        line(640, "the treat-"), line(628, "ment arm"),
        line(600, "the follow\xad"), line(588, "ing week"),
    ])
    normal = item(c.doc, "Follow-up visits on Days 1-3 at 10:00 (see Section 5.2).", 700)
    item(c.doc, "The treatment period is short.", 670)
    typo = item(c.doc, "the treatment arm", 640, lines=2)
    # a soft hyphen at the end of a layout box: Docling's _merge_elements joins across boxes
    soft = c.doc.add_text(label=DocItemLabel.TEXT, text="the following week",
                          prov=ProvenanceItem(page_no=1, bbox=bl(610, 597), charspan=(0, 10)))
    soft.prov.append(ProvenanceItem(page_no=1, bbox=bl(598, 585), charspan=(10, 18)))
    counts = c.run()
    assert normal.text == "Follow-up visits on Days 1-3 at 10:00 (see Section 5.2).", normal.text
    assert typo.text == "the treatment arm", typo.text  # typographic hyphenation stays joined
    assert soft.text == "the following week", soft.text
    assert counts.get("hyphen_restored", 0) == 0, counts
    assert counts["hyphen_kept_joined"] == 1 and counts["hyphen_skipped_across_boxes"] == 1, counts
    c.close()


def test_auto_hyphenated_document() -> None:
    # A document that hyphenates words at line ends (most line-end splits are words it
    # writes joined elsewhere): a split without such evidence stays joined too.
    c = Case([
        line(700, "the treat-"), line(688, "ment arm"),
        line(660, "each pati-"), line(648, "ent group"),
        line(620, "the dos-"), line(608, "ing plan"),
        line(580, "signed con-"), line(568, "sent form"),
        line(540, "treatment patient dosing"),
    ])
    items = [item(c.doc, t, y, lines=2) for t, y in
             [("the treatment arm", 700), ("each patient group", 660), ("the dosing plan", 620),
              ("signed consent form", 580)]]
    item(c.doc, "treatment patient dosing", 540)
    counts = c.run()
    assert [i.text for i in items] == ["the treatment arm", "each patient group", "the dosing plan",
                                       "signed consent form"], [i.text for i in items]
    assert counts.get("hyphen_restored", 0) == 0 and counts["hyphen_kept_joined"] == 4, counts
    c.close()


def test_hyphen_across_boxes_with_evidence() -> None:
    # Across two boxes a hyphen is restored only when the document writes the compound hyphenated.
    c = Case([
        line(700, "naive to EGFR-directed agents"),
        line(670, "prior EGFR-"), line(658, "directed therapy"),
    ])
    item(c.doc, "naive to EGFR-directed agents", 700)
    it = c.doc.add_text(label=DocItemLabel.TEXT, text="prior EGFRdirected therapy",
                        prov=ProvenanceItem(page_no=1, bbox=bl(680, 667), charspan=(0, 10)))
    it.prov.append(ProvenanceItem(page_no=1, bbox=bl(668, 655), charspan=(10, 26)))
    c.run()
    assert it.text == "prior EGFR-directed therapy", it.text
    c.close()


# ---------------------------------------------------------------- (c) merged words


def test_merged_words() -> None:
    c = Case([
        line(700, "ADVANCED SOLID TUMORS WHO", ("gap", 300), "ARE REFRACTORY"),
        line(670, "Cohort A", ("gap", 300), "will enroll"),
        line(640, "Tight", ("gap", 40), "kerning"),  # 0.04 em: no word gap, nothing inserted
    ])
    title = item(c.doc, "ADVANCED SOLID TUMORS WHOARE REFRACTORY", 700)
    cohort = item(c.doc, "Cohort Awill enroll", 670)
    tight = item(c.doc, "Tightkerning", 640)
    counts = c.run()
    assert title.text == "ADVANCED SOLID TUMORS WHO ARE REFRACTORY", title.text
    assert cohort.text == "Cohort A will enroll", cohort.text
    assert tight.text == "Tightkerning", tight.text
    assert counts["space_restored"] == 2, counts
    c.close()


# ---------------------------------------------------------------- alignment


def test_alignment_robust_and_skips() -> None:
    # Docling's normalised quotes, mapped Symbol glyphs and dropped list markers still align;
    # text that is not in the PDF (OCR, inserted labels) is skipped and logged.
    c = Case([
        line(700, "• ", "“Quoted” dose ", ("sym", 0xB3), "10 mg x 10", ("sup", "3"), "/", ("sym", 0x6D), "L"),
        line(670, "Printed words on the page"),
    ])
    li = c.doc.add_list_item(text='"Quoted" dose ≥10 mg x 10 3 /μL',
                             prov=ProvenanceItem(page_no=1, bbox=bl(710, 697), charspan=(0, 10)))
    other = item(c.doc, "completely different words from an image", 670)
    log = {}
    counts = c.run(log)
    assert li.text == '"Quoted" dose ≥10 mg x 10^3/μL', li.text
    assert other.text == "completely different words from an image"
    assert counts["items_skipped_low_alignment"] == 1, counts
    skipped = [e for e in log["text_reconcile"] if e.get("reason") == "low_alignment"]
    assert len(skipped) == 1 and skipped[0]["item"] is other and skipped[0]["page"] == 1
    c.close()


# ---------------------------------------------------------------- cover-page label/value pairs


def tl_item(d, text: str, l: float, top: float, r: float, height: float = 10, page: int = 1,
            label=DocItemLabel.TEXT):
    return d.add_text(label=label, text=text,
                      prov=ProvenanceItem(page_no=page, bbox=bl(792 - top, 792 - top - height, l, r),
                                          charspan=(0, len(text))))


def test_pair_label_values() -> None:
    d = new_doc(7)
    tl_item(d, "SYNTHETIC PROTOCOL TITLE", 100, 100, 400)
    # Docling's reading order: a block of labels, then a block of values
    labels = [("Study Intervention Number:", "QZ-0071234"), ("US IND Number:", "123456"),
              ("Protocol Number:", "ZX-9001-04"), ("ClinicalTrials.gov NCT:", "NCT09876543")]
    for k, (lab, _) in enumerate(labels):
        tl_item(d, lab, 100, 200 + 20 * k, 250, label=DocItemLabel.SECTION_HEADER if k == 2 else DocItemLabel.TEXT)
    for k, (_, val) in enumerate(labels):
        tl_item(d, val, 316, 200 + 20 * k + 0.5, 390)
    tl_item(d, "Approval Date:", 100, 300, 170)
    tl_item(d, "01 Jan 2031", 200, 300, 260)  # its own value column
    tl_item(d, "Total pages: 9", 450, 300, 520)  # a separate note further along the line
    tl_item(d, "Sponsor Address:", 100, 340, 190)
    tl_item(d, "1 Example Road", 100, 355, 190)  # value below the label: already in order
    # page 2: a single "Note:" with text beside it is not a label column
    tl_item(d, "Note:", 100, 200, 130, page=2)
    tl_item(d, "this is body text", 140, 200, 400, page=2)
    # page 7: untouched with max_page=5, paired on the default (all pages) pass below
    for k in range(3):
        tl_item(d, f"Key {k}:", 100, 200 + 20 * k, 150, page=7)
        tl_item(d, f"value {k}", 300, 200 + 20 * k, 350, page=7)
    log = {}
    n = tr.pair_label_values(d, log, max_page=5)
    texts = [t.text for t, _ in d.iterate_items()]  # reading order
    assert n == 5, (n, texts)
    assert texts[:7] == ["SYNTHETIC PROTOCOL TITLE", "Study Intervention Number: QZ-0071234",
                         "US IND Number: 123456", "Protocol Number: ZX-9001-04",
                         "ClinicalTrials.gov NCT: NCT09876543", "Approval Date: 01 Jan 2031", "Total pages: 9"], texts
    assert "Sponsor Address:" in texts and "1 Example Road" in texts
    assert "Note:" in texts and "this is body text" in texts
    assert sum(t.startswith("Key ") for t in texts) == 3 and "value 0" in texts
    merged = next(t for t in d.texts if t.text.startswith("Study Intervention Number:"))
    b = merged.prov[0].bbox
    assert b.l == 100 and b.r == 390, b  # union of label and value boxes
    # a label the layout model called a heading becomes plain text (a field opens no section)
    pn = next(t for t in d.texts if t.text.startswith("Protocol Number:"))
    assert pn.label == DocItemLabel.TEXT and not any(t.label == DocItemLabel.SECTION_HEADER for t in d.texts)
    assert all(set(e) == {"item", "page", "values"} for e in log["label_value_pairs"])
    # default: every page; the lone "Note:" still stays, a second pass finds nothing new on page 1
    assert tr.pair_label_values(d) == 3
    texts = [t.text for t, _ in d.iterate_items()]
    assert "Key 0: value 0" in texts and "Note:" in texts and "this is body text" in texts, texts


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
