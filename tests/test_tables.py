"""Self-test for the table pipeline: ruling-line lattice (table_lattice.py), row-structure fixes
(table_merge.py), the chunk row serializer (chunk_protocol.ProtocolTableSerializer) and table QA
(validate_output.table_checks).

Run: `python tests/test_tables.py` (no pytest). Synthetic PDFs are drawn with pypdfium2: ruled
tables with Word-style rules (thin filled rectangles) or stroked lines, text in Helvetica. The
TableFormer side is a hand-built DoclingDocument that reproduces each failure mode: fused visit
columns, text moved across a row rule, a wrong header span, an unruled (booktabs) table that must
stay untouched, a rotated page; tables ruled with horizontal lines only (fused and split rows
corrected with columns from TableFormer, spans kept, booktabs / row-group rules, unseparable
columns, header levels without a rule and rows above the top rule left alone); booktabs tables
(rules at the top, under the header and at the bottom only) where a wrapped row label's second
line is joined to its row, while group labels (indented or empty-first-column rows below, bold),
numbered lines, header lines and lines at row distance stay rows.
"""

import ctypes
import sys
import tempfile
import traceback
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pypdfium2 as pdfium
import pypdfium2.raw as pr
from docling_core.types.doc import BoundingBox, CoordOrigin, DoclingDocument, ProvenanceItem, Size, TableCell, TableData

import table_lattice as tl
import table_merge as tm
from protocol_settings import ProtocolSettings

TMP = Path(tempfile.mkdtemp(prefix="test_tables_"))


# ------------------------------------------------------------------ synthetic PDFs

class Page:
    """Draws on one pdfium page, in the displayed page's coordinates (bottom-left origin).

    rot90: the page is portrait (w x h) in user space with /Rotate 90 and displays as
    landscape (h x w); content is drawn rotated so that it reads upright on screen, as a
    landscape table on a rotated page does.
    """

    def __init__(self, pdf: pdfium.PdfDocument, w: float, h: float, rot90: bool = False):
        self.pdf, self.page, self.w, self.rot90 = pdf, pdf.new_page(w, h), w, rot90

    def _pt(self, x: float, y: float) -> tuple[float, float]:
        return (self.w - y, x) if self.rot90 else (x, y)  # displayed -> user space

    def text(self, s: str, x: float, y: float, size: float = 9, font: bytes = b"Helvetica") -> None:
        obj = pr.FPDFPageObj_NewTextObj(self.pdf.raw, font, ctypes.c_float(size))
        buf = (s + "\x00").encode("utf-16-le")
        pr.FPDFText_SetText(obj, ctypes.cast(ctypes.c_char_p(buf), ctypes.POINTER(pr.FPDF_WCHAR)))
        ux, uy = self._pt(x, y)
        pr.FPDFPageObj_Transform(obj, *((0, 1, -1, 0) if self.rot90 else (1, 0, 0, 1)), ux, uy)
        pr.FPDFPage_InsertObject(self.page.raw, obj)

    def rule(self, x0: float, y0: float, x1: float, y1: float, width: float = 0.5) -> None:
        """A Word-style rule: a thin filled rectangle."""
        if abs(y1 - y0) < 1e-6:
            l, b, r, t = min(x0, x1), y0 - width / 2, max(x0, x1), y0 + width / 2
        else:
            l, b, r, t = x0 - width / 2, min(y0, y1), x0 + width / 2, max(y0, y1)
        (ax, ay), (bx, by) = self._pt(l, b), self._pt(r, t)
        obj = pr.FPDFPageObj_CreateNewRect(min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay))
        pr.FPDFPageObj_SetFillColor(obj, 0, 0, 0, 255)
        pr.FPDFPath_SetDrawMode(obj, pr.FPDF_FILLMODE_ALTERNATE, False)
        pr.FPDFPage_InsertObject(self.page.raw, obj)

    def line(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """A stroked line."""
        (x0, y0), (x1, y1) = self._pt(x0, y0), self._pt(x1, y1)
        obj = pr.FPDFPageObj_CreateNewPath(x0, y0)
        pr.FPDFPath_LineTo(obj, x1, y1)
        pr.FPDFPageObj_SetStrokeColor(obj, 0, 0, 0, 255)
        pr.FPDFPageObj_SetStrokeWidth(obj, ctypes.c_float(0.75))
        pr.FPDFPath_SetDrawMode(obj, 0, True)
        pr.FPDFPage_InsertObject(self.page.raw, obj)

    def grid(self, xs: list[float], ys: list[float], stroked: bool = False) -> None:
        draw = self.line if stroked else self.rule
        for y in ys:
            draw(xs[0], y, xs[-1], y)
        for x in xs:
            draw(x, ys[-1], x, ys[0])


def save(pdf: pdfium.PdfDocument, pages: list[Page], name: str, rotation: int = 0) -> pdfium.PdfDocument:
    for p in pages:
        pr.FPDFPage_GenerateContent(p.page.raw)
        if rotation:
            p.page.set_rotation(rotation)
    path = TMP / f"{name}.pdf"
    pdf.save(str(path))
    return pdfium.PdfDocument(str(path))


# ------------------------------------------------------------------ synthetic TableFormer output

def cell(text: str, r: int, c: int, box, rs: int = 1, cs: int = 1, header: bool = False,
         page_h: float = 792) -> TableCell:
    """A TableFormer cell; box (l, b, r, t) in displayed-page points, stored TOPLEFT like TableFormer's."""
    l, b, rr, t = box
    return TableCell(text=text, start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                     end_col_offset_idx=c + cs, row_span=rs, col_span=cs, column_header=header,
                     bbox=BoundingBox(l=l, t=page_h - t, r=rr, b=page_h - b, coord_origin=CoordOrigin.TOPLEFT))


def doc_with_table(cells: list[TableCell], n_rows: int, n_cols: int, box, size=(612, 792),
                   pages: int = 1) -> DoclingDocument:
    d = DoclingDocument(name="t")
    for p in range(1, pages + 1):
        d.add_page(page_no=p, size=Size(width=size[0], height=size[1]))
    l, b, r, t = box
    d.add_table(data=TableData(num_rows=n_rows, num_cols=n_cols, table_cells=cells),
                prov=ProvenanceItem(page_no=1, bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.BOTTOMLEFT),
                                    charspan=(0, 0)))
    return d


def grid_of(table) -> dict[tuple[int, int], str]:
    return {(c.start_row_offset_idx, c.start_col_offset_idx): c.text for c in table.data.table_cells}


def words(table) -> Counter:
    return Counter(w for c in table.data.table_cells for w in c.text.split())


# A 4-column SoA-like table: Activity | V1 | V2 | V3 (visit columns 50 pt wide).
XS = [72, 172, 222, 272, 322]
YS = [700, 685, 670, 655]


def draw_soa(page: Page, ruled: str = "full", dy: float = 0) -> None:
    ys = [y - dy for y in YS]
    if ruled == "full":
        page.grid(XS, ys)
    elif ruled == "booktabs":  # top, header and bottom rules only
        for y in (ys[0], ys[1], ys[-1]):
            page.rule(XS[0], y, XS[-1], y)
    for s, x in (("Activity", 75), ("V1", 190), ("V2", 240), ("V3", 290)):
        page.text(s, x, 690 - dy)
    for s, x in (("Exam", 75), ("X", 192), ("X", 292)):
        page.text(s, x, 675 - dy)
    for s, x in (("Labs", 75), ("X", 242), ("X", 292)):
        page.text(s, x, 660 - dy)


def fused_soa_cells(dy: float = 0, page_h: float = 792) -> list[TableCell]:
    """TableFormer's output with visit columns V2 and V3 fused into one."""
    k = {"page_h": page_h}
    return [cell("Activity", 0, 0, (72, 685 - dy, 172, 700 - dy), header=True, **k),
            cell("V1", 0, 1, (172, 685 - dy, 222, 700 - dy), header=True, **k),
            cell("V2 V3", 0, 2, (222, 685 - dy, 322, 700 - dy), header=True, **k),
            cell("Exam", 1, 0, (72, 670 - dy, 172, 685 - dy), **k), cell("X", 1, 1, (172, 670 - dy, 222, 685 - dy), **k),
            cell("X", 1, 2, (222, 670 - dy, 322, 685 - dy), **k),
            cell("Labs", 2, 0, (72, 655 - dy, 172, 670 - dy), **k), cell("X X", 2, 2, (222, 655 - dy, 322, 670 - dy), **k)]


# ------------------------------------------------------------------ lattice tests

def test_fused_columns_corrected() -> None:
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_soa(page)
    pdf = save(pdf, [page], "fused")
    d = doc_with_table(fused_soa_cells(), 3, 3, (70, 653, 324, 702))
    before = words(d.tables[0])
    log = {}
    counts = tl.correct_tables_with_lattice(d, pdf, log)
    t = d.tables[0]
    assert counts["corrected"] == 1, counts
    assert t.data.num_cols == 4 and t.data.num_rows == 3
    g = grid_of(t)
    assert (g[(0, 2)], g[(0, 3)]) == ("V2", "V3"), g
    assert g[(1, 1)] == "X" and g[(1, 3)] == "X" and (1, 2) not in g
    assert g[(2, 2)] == "X" and g[(2, 3)] == "X" and (2, 1) not in g
    assert words(t) == before  # never drops text
    assert all(c.column_header == (c.start_row_offset_idx == 0) for c in t.data.table_cells)
    entry = log["table_lattice"][0]
    assert entry["action"] == "corrected" and entry["cols_before"] == 3 and entry["cols_after"] == 4
    assert entry["rule_coverage"] >= 0.9
    # the log holds numbers and labels only, never cell text
    assert not any(isinstance(v, str) and ("Exam" in v or "Labs" in v) for e in log["table_lattice"] for v in e.values())


def test_verify_mode_changes_nothing() -> None:
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_soa(page)
    pdf = save(pdf, [page], "verify")
    d = doc_with_table(fused_soa_cells(), 3, 3, (70, 653, 324, 702))
    snapshot = d.tables[0].data.model_dump()
    counts = tl.correct_tables_with_lattice(d, pdf, {}, mode="verify")
    assert counts.get("disagree") == 1, counts
    assert d.tables[0].data.model_dump() == snapshot


def test_stroked_rules_and_lattice_agreement() -> None:
    """Stroked lines work like filled rules; a correct TableFormer grid is verified, not rebuilt."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    page.grid(XS, YS, stroked=True)
    for s, x in (("Activity", 75), ("V1", 190), ("V2", 240), ("V3", 290)):
        page.text(s, x, 690)
    for s, x in (("Exam", 75), ("X", 192), ("X", 292)):
        page.text(s, x, 675)
    pdf = save(pdf, [page], "stroked")
    cells = [cell("Activity", 0, 0, (72, 685, 172, 700), header=True), cell("V1", 0, 1, (172, 685, 222, 700), header=True),
             cell("V2", 0, 2, (222, 685, 272, 700), header=True), cell("V3", 0, 3, (272, 685, 322, 700), header=True),
             cell("Exam", 1, 0, (72, 670, 172, 685)), cell("X", 1, 1, (172, 670, 222, 685)),
             cell("X", 1, 3, (272, 670, 322, 685))]
    d = doc_with_table(cells, 3, 4, (70, 653, 324, 702))
    snapshot = d.tables[0].data.model_dump()
    counts = tl.correct_tables_with_lattice(d, pdf, {})
    assert counts.get("verified_ok") == 1, counts
    assert d.tables[0].data.model_dump() == snapshot


def test_row_boundary_text_moved_back() -> None:
    """The last line of a cell sits near the row rule; TableFormer put it into the next row."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    xs, ys = [72, 172, 400], [700, 685, 655, 625]
    page.grid(xs, ys)
    page.text("Item", 75, 690)
    page.text("Description", 175, 690)
    page.text("Item A", 75, 673)
    page.text("first line of one", 175, 673)
    page.text("second line", 175, 658)
    page.text("Item B", 75, 643)
    page.text("other cell text", 175, 643)
    pdf = save(pdf, [page], "rowmove")
    cells = [cell("Item", 0, 0, (72, 685, 172, 700), header=True),
             cell("Description", 0, 1, (172, 685, 400, 700), header=True),
             cell("Item A", 1, 0, (72, 665, 172, 685)), cell("first line of one", 1, 1, (172, 665, 400, 684)),
             cell("Item B", 2, 0, (72, 625, 172, 650)),
             cell("second line other cell text", 2, 1, (172, 627, 400, 666))]
    d = doc_with_table(cells, 3, 2, (70, 623, 402, 702))
    counts = tl.correct_tables_with_lattice(d, pdf, {})
    assert counts["corrected"] == 1, counts
    g = grid_of(d.tables[0])
    assert g[(1, 1)] == "first line of one second line", g
    assert g[(2, 1)] == "other cell text", g


def test_unruled_table_not_corrected() -> None:
    """A booktabs table (no vertical rules) proves nothing: TableFormer's grid stays, flagged."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_soa(page, ruled="booktabs")
    pdf = save(pdf, [page], "booktabs")
    d = doc_with_table(fused_soa_cells(), 3, 3, (70, 653, 324, 702))
    snapshot = d.tables[0].data.model_dump()
    log = {}
    tl.correct_tables_with_lattice(d, pdf, log)
    assert log["table_lattice"][0]["action"] in ("disagree_not_ruled", "skipped"), log["table_lattice"][0]
    assert d.tables[0].data.model_dump() == snapshot


def test_no_rules_at_all_skipped() -> None:
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_soa(page, ruled="none")
    pdf = save(pdf, [page], "norules")
    d = doc_with_table(fused_soa_cells(), 3, 3, (70, 653, 324, 702))
    log = {}
    counts = tl.correct_tables_with_lattice(d, pdf, log)
    assert counts.get("skipped:no_rules") == 1 and d.tables[0].data.num_cols == 3, counts


def test_spanning_header_from_rules() -> None:
    """Header 'Period' spans both visit columns and 'Activity' both header rows; TableFormer had
    them one column / one row wide."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    xs, ys = [72, 172, 232, 292], [700, 685, 670, 655]
    page.rule(72, 700, 292, 700)
    page.rule(172, 685, 292, 685)  # no rule under "Activity": it spans the two header rows
    page.rule(72, 670, 292, 670)
    page.rule(72, 655, 292, 655)
    for x in (72, 172, 292):
        page.rule(x, 655, x, 700)
    page.rule(232, 655, 232, 685)  # no rule inside "Period": it spans both visit columns
    page.text("Activity", 75, 682)
    page.text("Period", 210, 690)
    page.text("Day 1", 180, 675)
    page.text("Day 8", 240, 675)
    for s, x in (("Exam", 75), ("X", 195), ("X", 255)):
        page.text(s, x, 660)
    pdf = save(pdf, [page], "span")
    cells = [cell("Activity", 0, 0, (72, 670, 172, 700), header=True),
             cell("Period", 0, 1, (172, 685, 232, 700), header=True),  # wrong: one column wide
             cell("Day 1", 1, 1, (172, 670, 232, 685), header=True), cell("Day 8", 1, 2, (232, 670, 292, 685), header=True),
             cell("Exam", 2, 0, (72, 655, 172, 670)), cell("X", 2, 1, (172, 655, 232, 670)),
             cell("X", 2, 2, (232, 655, 292, 670))]
    cells[0].end_row_offset_idx, cells[0].row_span = 1, 1  # wrong: one row high
    d = doc_with_table(cells, 3, 3, (70, 653, 294, 702))
    counts = tl.correct_tables_with_lattice(d, pdf, {})
    assert counts["corrected"] == 1, counts
    by_text = {c.text: c for c in d.tables[0].data.table_cells}
    period, activity = by_text["Period"], by_text["Activity"]
    assert (period.start_col_offset_idx, period.col_span, period.column_header) == (1, 2, True)
    assert (activity.row_span, activity.column_header) == (2, True)
    assert not by_text["Exam"].column_header


def test_rotated_page() -> None:
    """A landscape table on a portrait page with /Rotate 90 (content drawn rotated, displayed
    upright): rules and characters are mapped into Docling's displayed page frame."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792, rot90=True)
    draw_soa(page, dy=300)
    pdf = save(pdf, [page], "rotated", rotation=90)
    assert tl.PageFrame(pdf[0]).rot == 90 and pdf[0].get_size() == (792, 612)
    d = doc_with_table(fused_soa_cells(dy=300, page_h=612), 3, 3, (70, 353, 324, 402), size=(792, 612))
    counts = tl.correct_tables_with_lattice(d, pdf, {})
    assert counts["corrected"] == 1, counts
    g = grid_of(d.tables[0])
    assert d.tables[0].data.num_cols == 4 and (g[(0, 2)], g[(0, 3)]) == ("V2", "V3"), g
    assert g[(2, 2)] == "X" and g[(2, 3)] == "X"


def test_text_layer_mismatch_skipped() -> None:
    """Cell text that is not in the text layer (OCR) cannot be placed: no correction."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    page.grid(XS, YS)
    pdf = save(pdf, [page], "notext")
    d = doc_with_table(fused_soa_cells(), 3, 3, (70, 653, 324, 702))
    counts = tl.correct_tables_with_lattice(d, pdf, {})
    assert d.tables[0].data.num_cols == 3 and not counts.get("corrected"), counts


def test_superscript_marker_convention() -> None:
    """Cell text written with the "^" superscript convention still matches the text layer."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_soa(page)
    page.text("a", 199, 678, size=5)  # a raised footnote marker after the first X
    pdf = save(pdf, [page], "caret")
    cells = fused_soa_cells()
    cells[4].text = "X^a"
    d = doc_with_table(cells, 3, 3, (70, 653, 324, 702))
    counts = tl.correct_tables_with_lattice(d, pdf, {})
    assert counts["corrected"] == 1, counts
    assert grid_of(d.tables[0])[(1, 1)] == "X^a"


def test_header_note_split() -> None:
    """A header cell spanning two header rows holds a name and, below the other columns' row
    rule, a note: the note is kept apart (newline) so it does not join the column name."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    xs, ys = [72, 172, 232, 292, 400], [700, 685, 670, 655]
    for y in (700, 670, 655):
        page.rule(72, y, 400, y)
    page.rule(72, 685, 292, 685)  # the notes column spans both header rows
    for x in xs:
        page.rule(x, 655, x, 700)
    page.text("Activity", 75, 682)
    page.text("Treatment", 190, 690)
    page.text("Treatment", 240, 690)
    page.text("Day 1", 180, 675)
    page.text("Day 8", 240, 675)
    page.text("Notes", 300, 691)
    page.text("Day 8 is Cycle 1 only", 300, 673)
    for s, x in (("Exam", 75), ("X", 195), ("See 8.2", 300)):
        page.text(s, x, 660)
    pdf = save(pdf, [page], "note")
    cells = [cell("Activity", 0, 0, (72, 670, 172, 700), rs=2, header=True),
             cell("Treatment", 0, 1, (172, 685, 232, 700), header=True),
             cell("Treatment", 0, 2, (232, 685, 292, 700), header=True),
             cell("Notes Day 8 is Cycle 1 only", 0, 3, (292, 670, 400, 700), rs=2, header=True),
             cell("Day 1", 1, 1, (172, 670, 232, 685), header=True), cell("Day 8", 1, 2, (232, 670, 292, 685), header=True),
             cell("Exam", 2, 0, (72, 655, 172, 670)), cell("X", 2, 1, (172, 655, 232, 670)),
             cell("See 8.2", 2, 3, (292, 655, 400, 670))]
    d = doc_with_table(cells, 3, 4, (70, 653, 402, 702))
    counts = tl.correct_tables_with_lattice(d, pdf, {})
    assert counts.get("header_notes_split") == 1, counts
    assert grid_of(d.tables[0])[(0, 3)] == "Notes\nDay 8 is Cycle 1 only"
    cols, rows = serialize(d)
    assert "Notes (header note: Day 8 is Cycle 1 only)" in " | ".join(cols), cols
    assert rows == ["Exam: Treatment [Day 1 = X]; Notes = See 8.2"], rows


# ------------------------------------------------------------------ row-ruled tables (horizontal rules only)

# An abbreviation table: 2 columns split at x=150, a rule under every row, no vertical rules.
ABBR = [("Abbreviation", "Definition"), ("AE", "adverse event"), ("BMI", "body mass index"),
        ("CRF", "case report form"), ("ECG", "electrocardiogram"), ("HR", "heart rate"), ("IV", "intravenous"),
        ("MCV", "mean corpuscular volume")]
AYS = [700, 685, 670, 655, 640, 625, 610, 595, 580]


def draw_abbrev(page: Page, rules: list[float] | None = None, cell_by_cell: bool = True) -> None:
    """Rules at `rules` (default: every row boundary). cell_by_cell: each rule drawn as two
    pieces meeting at the column boundary, as Word draws cell borders."""
    for y in AYS if rules is None else rules:
        if cell_by_cell:
            page.rule(72, y, 150, y)
            page.rule(150.5, y, 400, y)
        else:
            page.rule(72, y, 400, y)
    for (a, b), y in zip(ABBR, AYS):
        page.text(a, 75, y - 10)
        page.text(b, 155, y - 10)


def abbrev_tf_cells() -> list[TableCell]:
    """TableFormer's output: AE and BMI fused into one row; MCV's definition split over two rows
    (one line of text: "mean ... volume" in one row, "corpuscular" alone in the next)."""
    cells = [cell("Abbreviation", 0, 0, (72, 685, 150, 700), header=True),
             cell("Definition", 0, 1, (150, 685, 400, 700), header=True),
             cell("AE BMI", 1, 0, (72, 655, 150, 685)), cell("adverse event body mass index", 1, 1, (150, 655, 400, 685))]
    for r, ((a, b), (top, bot)) in enumerate(zip(ABBR[3:7], zip(AYS[3:7], AYS[4:8])), start=2):
        cells += [cell(a, r, 0, (72, bot, 150, top)), cell(b, r, 1, (150, bot, 400, top))]
    return cells + [cell("MCV", 6, 0, (72, 587, 150, 595)), cell("mean volume", 6, 1, (150, 587, 400, 595)),
                    cell("corpuscular", 7, 1, (150, 580, 400, 587))]


def test_row_ruled_fused_and_split_rows_corrected() -> None:
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_abbrev(page)
    pdf = save(pdf, [page], "rowruled")
    # baseline (row-ruled mode off): not fully ruled, stays on TableFormer
    d = doc_with_table(abbrev_tf_cells(), 8, 2, (70, 578, 402, 702))
    snapshot = d.tables[0].data.model_dump()
    log = {}
    counts = tl.correct_tables_with_lattice(d, pdf, log, row_ruled=False)
    assert counts.get("disagree_not_ruled") == 1 and d.tables[0].data.model_dump() == snapshot, counts
    d = doc_with_table(abbrev_tf_cells(), 8, 2, (70, 578, 402, 702))
    before = words(d.tables[0])
    log = {}
    counts = tl.correct_tables_with_lattice(d, pdf, log)
    assert counts["corrected"] == 1 and counts["corrected_rows"] == 1, counts
    t = d.tables[0]
    assert (t.data.num_rows, t.data.num_cols) == (8, 2)
    g = grid_of(t)
    assert g == {(r, c): ABBR[r][c] for r in range(8) for c in range(2)}, g
    assert words(t) == before
    assert all(c.column_header == (c.start_row_offset_idx == 0) for c in t.data.table_cells)
    entry = log["table_lattice"][0]
    assert entry["lattice_mode"] == "rows" and entry["row_rule_coverage"] >= 0.9, entry
    assert (entry["rows_before"], entry["rows_after"], entry["virtual_boundaries"]) == (8, 8, 1), entry
    # the column boundary sits where the Word borders meet (x=150.25), not mid-gap
    assert entry["boundaries_at_rule_breaks"] == 1
    assert all(abs(c.bbox.r - 150.25) < 0.01 for c in t.data.table_cells if c.start_col_offset_idx == 0)
    assert not any(isinstance(v, str) and ("AE" in v or "MCV" in v) for v in entry.values())


def test_row_ruled_booktabs_and_row_groups_not_corrected() -> None:
    """Rules only at top / under the header / bottom (booktabs), or only between row groups:
    the rules say nothing about the rows in between, so TableFormer's grid stays."""
    for name, rules in (("booktabs_rows", [700, 685, 580]), ("groups_rows", [700, 685, 640, 580])):
        pdf = pdfium.PdfDocument.new()
        page = Page(pdf, 612, 792)
        draw_abbrev(page, rules=rules)
        pdf = save(pdf, [page], name)
        d = doc_with_table(abbrev_tf_cells(), 8, 2, (70, 578, 402, 702))
        snapshot = d.tables[0].data.model_dump()
        log = {}
        counts = tl.correct_tables_with_lattice(d, pdf, log)
        entry = log["table_lattice"][0]
        assert entry["action"] == "disagree_not_ruled" and entry["row_rule_coverage"] < 0.5, (name, entry)
        assert not counts.get("corrected") and d.tables[0].data.model_dump() == snapshot, name


def test_row_ruled_spanning_header_kept() -> None:
    """'Treatment' spans both day columns (a short rule under it only); 'Parameter' spans both
    header rows. TableFormer fused two body rows; the rebuild splits them and keeps the spans."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    for y in (700, 670, 655, 640):
        page.rule(72, y, 400, y)
    page.rule(150, 685, 400, 685)
    for s, x, y in (("Parameter", 75, 681), ("Treatment", 240, 690), ("Day 1", 160, 675), ("Day 8", 280, 675),
                    ("Weight", 75, 660), ("70", 160, 660), ("72", 280, 660),
                    ("Height", 75, 645), ("170", 160, 645), ("171", 280, 645)):
        page.text(s, x, y)
    pdf = save(pdf, [page], "rowspan")
    cells = [cell("Parameter", 0, 0, (72, 670, 150, 700), rs=2, header=True),
             cell("Treatment", 0, 1, (150, 685, 400, 700), cs=2, header=True),
             cell("Day 1", 1, 1, (150, 670, 270, 685), header=True), cell("Day 8", 1, 2, (270, 670, 400, 685), header=True),
             cell("Weight Height", 2, 0, (72, 640, 150, 670)), cell("70 170", 2, 1, (150, 640, 270, 670)),
             cell("72 171", 2, 2, (270, 640, 400, 670))]
    d = doc_with_table(cells, 3, 3, (70, 638, 402, 702))
    log = {}
    counts = tl.correct_tables_with_lattice(d, pdf, log)
    assert counts["corrected_rows"] == 1, (counts, log["table_lattice"][0])
    t = d.tables[0]
    by_text = {c.text: c for c in t.data.table_cells}
    assert (t.data.num_rows, t.data.num_cols) == (4, 3)
    tr, par = by_text["Treatment"], by_text["Parameter"]
    assert (tr.start_row_offset_idx, tr.start_col_offset_idx, tr.col_span, tr.column_header) == (0, 1, 2, True)
    assert (par.row_span, par.column_header) == (2, True)
    assert grid_of(t)[(2, 0)] == "Weight" and grid_of(t)[(3, 0)] == "Height"
    assert grid_of(t)[(2, 2)] == "72" and grid_of(t)[(3, 1)] == "170"
    assert not by_text["Weight"].column_header and by_text["Day 8"].column_header
    assert log["table_lattice"][0]["span_bands_open"] == 1


def test_row_ruled_header_levels_without_rule_skipped() -> None:
    """Two header levels with no rule between them ('Treatment' over 'Day 1 | Day 8'): one row
    band holds the spanning cell and the cells under it; merging them would destroy the header,
    so the table is left alone (in a long table these few unruled pairs pass the threshold)."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    for y in (700, 670, 655, 640):
        page.rule(72, y, 400, y)
    for s, x, y in (("Parameter", 75, 681), ("Treatment", 240, 690), ("Day 1", 160, 675), ("Day 8", 280, 675),
                    ("Weight", 75, 660), ("70", 160, 660), ("72", 280, 660),
                    ("Height", 75, 645), ("170", 160, 645), ("171", 280, 645)):
        page.text(s, x, y)
    pdf = save(pdf, [page], "levels")
    cells = [cell("Parameter", 0, 0, (72, 670, 150, 700), rs=2, header=True),
             cell("Treatment", 0, 1, (150, 685, 400, 700), cs=2, header=True),
             cell("Day 1", 1, 1, (150, 670, 270, 685), header=True), cell("Day 8", 1, 2, (270, 670, 400, 685), header=True),
             cell("Weight Height", 2, 0, (72, 640, 150, 670)), cell("70 170", 2, 1, (150, 640, 270, 670)),
             cell("72 171", 2, 2, (270, 640, 400, 670))]
    d = doc_with_table(cells, 3, 3, (70, 638, 402, 702))
    snapshot = d.tables[0].data.model_dump()
    log = {}
    tl.correct_tables_with_lattice(d, pdf, log, min_row_rule_coverage=0.5)
    entry = log["table_lattice"][0]
    assert (entry["action"], entry.get("reason")) == ("skipped", "span_conflict"), entry
    assert d.tables[0].data.model_dump() == snapshot


def test_row_ruled_columns_not_separable_skipped() -> None:
    """TableFormer's column boundary is not a whitespace gap (a column-0 cell's words run past
    where column 1's words start in other rows): no virtual columns, no correction."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    for y in (700, 685, 670, 655):
        page.rule(72, y, 400, y)
    for s, x, y in (("Item", 75, 690), ("Value", 155, 690), ("Alpha", 75, 675), ("first", 155, 675),
                    ("a long label running right", 75, 660), ("second", 300, 660)):
        page.text(s, x, y)
    pdf = save(pdf, [page], "notsep")
    cells = [cell("Item", 0, 0, (72, 685, 150, 700), header=True), cell("Value", 0, 1, (150, 685, 400, 700), header=True),
             cell("Alpha", 1, 0, (72, 670, 150, 685)), cell("first", 1, 1, (150, 670, 400, 685)),
             cell("a long label running right", 2, 0, (72, 655, 290, 670)), cell("second", 2, 1, (290, 655, 400, 670))]
    d = doc_with_table(cells, 3, 2, (70, 653, 402, 702))
    snapshot = d.tables[0].data.model_dump()
    log = {}
    counts = tl.correct_tables_with_lattice(d, pdf, log)
    entry = log["table_lattice"][0]
    assert (entry["action"], entry["reason"], entry["lattice_mode"]) == ("skipped", "columns_not_separable", "rows"), entry
    assert counts.get("skipped:columns_not_separable") == 1 and d.tables[0].data.model_dump() == snapshot


def test_row_ruled_rows_above_top_rule_not_merged() -> None:
    """No rule above the header and TableFormer saw two header rows there: the rules cannot
    tell whether they are one row, so the table is left alone."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_abbrev(page, rules=AYS[1:])
    page.text("(acronym)", 75, 702)
    pdf = save(pdf, [page], "edge")
    cells = [cell("(acronym)", 0, 0, (72, 700, 150, 712), header=True)] + [
        c.model_copy(update={"start_row_offset_idx": c.start_row_offset_idx + 1,
                             "end_row_offset_idx": c.end_row_offset_idx + 1}) for c in abbrev_tf_cells()]
    d = doc_with_table(cells, 9, 2, (70, 578, 402, 714))
    snapshot = d.tables[0].data.model_dump()
    log = {}
    tl.correct_tables_with_lattice(d, pdf, log, min_row_rule_coverage=0.8)
    entry = log["table_lattice"][0]
    assert (entry["action"], entry.get("reason")) == ("skipped", "rows_outside_rules"), entry
    assert d.tables[0].data.model_dump() == snapshot


# ------------------------------------------------------------------ booktabs tables: rows from the text layer

# Label | H0 | H1 | N; Helvetica 9, rows 15 pt apart, a wrapped cell's lines 11 pt apart.
BT_X = [72, 172, 232, 292, 352]
BT_HEAD = [(0, [("Indication", 75, 688)], True), (1, [("H0", 180, 688)], True), (2, [("H1", 240, 688)], True),
           (3, [("N", 300, 688)], True)]


def bt_row(label: str | None, y: float, values=("15%", "30%", "26"), x: float = 75) -> list:
    """A TableFormer row: the label (column 0, at x) and values on one line at baseline y."""
    row = [(0, [(label, x, y)])] if label else []
    return row + [(c, [(v, BT_X[c] + 8, y)]) for c, v in enumerate(values, start=1)]


def booktabs_doc(name: str, rows: list, bold: tuple = (), rules: tuple = (700, 682), bottom: bool = True):
    """Draws the rows' text (bold: texts set in Helvetica-Bold) with rules at `rules` and (bottom)
    under the last line, and builds TableFormer's cells (one per (column, lines[, header]) entry
    of a row; the cell box spans the column and its lines)."""
    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    cells = []
    for r, row in enumerate(rows):
        for col, parts, *head in row:
            for s, x, y in parts:
                page.text(s, x, y, font=b"Helvetica-Bold" if s in bold else b"Helvetica")
            ys = [y for _, _, y in parts]
            cells.append(cell(" ".join(s for s, _, _ in parts), r, col, (BT_X[col], min(ys) - 3, BT_X[col + 1], max(ys) + 8),
                              header=bool(head and head[0])))
    low = min(y for row in rows for _, parts, *_ in row for _, _, y in parts)
    for y in (*rules, low - 6) if bottom else rules:
        page.rule(BT_X[0], y, BT_X[-1], y)
    return save(pdf, [page], name), doc_with_table(cells, len(rows), 4, (70, low - 8, 354, 702))


def run_text_rows(pdf, d, **kw) -> tuple[dict, dict]:
    log = {}
    counts = tl.correct_tables_with_lattice(d, pdf, log, **kw)  # text_rows is on by default
    entry = log["table_lattice"][0]
    # the log holds numbers and identifiers only, never cell text
    for k, v in entry.items():
        if isinstance(v, str):
            assert v.replace("_", "").replace(":", "").isalpha() and v.islower(), (k, v)
        elif isinstance(v, dict):
            assert all(isinstance(n, int) and kk.replace("_", "").isalpha() for kk, n in v.items()), (k, v)
    return counts, entry


def test_text_rows_wrapped_label_joined() -> None:
    """'Liver and' / 'biliary': one row label on two lines (11 pt apart, rows are 15 pt apart);
    TableFormer made the second line a row of its own. Joined only with text_rows on."""
    rows = [BT_HEAD, bt_row("Liver and", 668), [(0, [("biliary", 75, 657)])], bt_row("Ovarian", 642),
            bt_row("Gastric", 627, ("20%", "40%", "17"))]
    pdf, d = booktabs_doc("tr_wrapped", rows)
    snapshot = d.tables[0].data.model_dump()
    counts = tl.correct_tables_with_lattice(d, pdf, {}, text_rows=False)
    assert not counts.get("text_rows_joined") and d.tables[0].data.model_dump() == snapshot, counts
    counts = tl.correct_tables_with_lattice(d, pdf, {}, mode="verify")  # verify mode changes nothing
    assert not counts.get("text_rows_joined") and d.tables[0].data.model_dump() == snapshot, counts
    pdf, d = booktabs_doc("tr_wrapped", rows)
    before = words(d.tables[0])
    counts, entry = run_text_rows(pdf, d)
    t = d.tables[0]
    assert (counts["text_rows_tables"], counts["text_rows_joined"]) == (1, 1), (counts, entry)
    assert entry["action"] == "disagree_not_ruled" and entry["rows_after"] == 4, entry
    assert (t.data.num_rows, t.data.num_cols) == (4, 4)
    g = grid_of(t)
    assert g[(1, 0)] == "Liver and biliary" and g[(1, 3)] == "26", g
    assert g[(2, 0)] == "Ovarian" and g[(3, 0)] == "Gastric" and g[(3, 2)] == "40%", g
    assert words(t) == before
    joined = next(c for c in t.data.table_cells if c.text == "Liver and biliary")
    assert joined.row_span == 1 and joined.bbox.to_bottom_left_origin(792).b < 657 - 2
    # the same table without any rules: joined only with text_rows_unruled
    pdf, d = booktabs_doc("tr_unruled", rows, rules=(), bottom=False)
    snapshot = d.tables[0].data.model_dump()
    counts, entry = run_text_rows(pdf, d)
    assert entry["reason"] == "no_rules" and d.tables[0].data.model_dump() == snapshot, entry
    counts, entry = run_text_rows(pdf, d, text_rows_unruled=True)
    assert counts["text_rows_joined"] == 1 and grid_of(d.tables[0])[(1, 0)] == "Liver and biliary", entry


def test_text_rows_group_label_kept() -> None:
    """A label-only line 11 pt under a row with values labels a group: the rows under it are
    indented, or have no text in the first column. It stays a row."""
    for name, rows in (
            ("tr_indented", [BT_HEAD, bt_row("Overall", 668), [(0, [("Hepatic", 75, 657)])], bt_row("ALT", 642, x=85),
                             bt_row("AST", 627, x=85)]),
            ("tr_empty_first", [BT_HEAD, bt_row("Overall", 668), [(0, [("Hepatic", 75, 657)])], bt_row(None, 642),
                                bt_row("Renal", 627)])):
        pdf, d = booktabs_doc(name, rows)
        snapshot = d.tables[0].data.model_dump()
        counts, entry = run_text_rows(pdf, d)
        assert not counts.get("text_rows_joined") and d.tables[0].data.model_dump() == snapshot, (name, entry)
        assert entry["text_rows_reasons"] == {"next_row": 1}, (name, entry)


def test_text_rows_bold_group_label_kept() -> None:
    rows = [BT_HEAD, bt_row("Overall", 668), [(0, [("Hepatic", 75, 657)])], bt_row("ALT", 642), bt_row("AST", 627)]
    pdf, d = booktabs_doc("tr_bold", rows, bold=("Hepatic",))
    snapshot = d.tables[0].data.model_dump()
    counts, entry = run_text_rows(pdf, d)
    assert d.tables[0].data.model_dump() == snapshot and entry["text_rows_reasons"] == {"style": 1}, entry


def test_text_rows_numbered_line_kept() -> None:
    rows = [BT_HEAD, bt_row("1. Hepatic", 668), [(0, [("2. Renal", 75, 657)])], bt_row("Ovarian", 642),
            bt_row("Gastric", 627)]
    pdf, d = booktabs_doc("tr_numbered", rows)
    snapshot = d.tables[0].data.model_dump()
    counts, entry = run_text_rows(pdf, d)
    assert d.tables[0].data.model_dump() == snapshot and entry["text_rows_reasons"] == {"numbered": 1}, entry


def test_text_rows_header_band_untouched() -> None:
    """'Parameter' / '(units)': a header cell on two lines that TableFormer split into two
    header rows, above the header rule. Untouched whether TableFormer flagged the rows as
    header or not (then the rule under the header marks the band)."""
    head = [(0, [("Parameter", 75, 688)]), (1, [("Day 1", 180, 688)]), (2, [("Day 8", 240, 688)]),
            (3, [("Day 15", 300, 688)])]
    for flagged in (True, False):
        rows = [[(*c, flagged) for c in head], [(0, [("(units)", 75, 677)], flagged)],
                bt_row("Weight", 656), bt_row("Height", 641), bt_row("BMI", 626)]
        pdf, d = booktabs_doc(f"tr_header_{flagged}", rows, rules=(700, 671))
        snapshot = d.tables[0].data.model_dump()
        counts, entry = run_text_rows(pdf, d)
        assert d.tables[0].data.model_dump() == snapshot and not counts.get("text_rows_joined"), (flagged, entry)
        assert not entry.get("text_rows_candidates"), (flagged, entry)


def test_text_rows_larger_gap_kept() -> None:
    """A label-only line at the table's row distance (15 pt) is a row of its own."""
    rows = [BT_HEAD, bt_row("Liver and", 668), [(0, [("biliary", 75, 653)])], bt_row("Ovarian", 638),
            bt_row("Gastric", 623)]
    pdf, d = booktabs_doc("tr_gap", rows)
    snapshot = d.tables[0].data.model_dump()
    counts, entry = run_text_rows(pdf, d)
    assert d.tables[0].data.model_dump() == snapshot and entry["text_rows_reasons"] == {"gap": 1}, entry


# ------------------------------------------------------------------ table_merge row structure

def pv(page: int, t: float, b: float) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=72, t=t, r=540, b=b, coord_origin=CoordOrigin.BOTTOMLEFT),
                          charspan=(0, 1))


def plain_table(d: DoclingDocument, rows: list[list[str]], prov: ProvenanceItem, header_rows: int = 1,
                full_width: tuple[int, ...] = ()):
    n_cols = max(len(r) for r in rows)
    cells = []
    for r, row in enumerate(rows):
        if r in full_width:
            cells.append(TableCell(text=row[0], start_row_offset_idx=r, end_row_offset_idx=r + 1,
                                   start_col_offset_idx=0, end_col_offset_idx=n_cols, col_span=n_cols,
                                   row_section=True))
            continue
        cells += [TableCell(text=x, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                            end_col_offset_idx=c + 1, column_header=r < header_rows)
                  for c, x in enumerate(row) if x]
    return d.add_table(data=TableData(num_rows=len(rows), num_cols=n_cols, table_cells=cells), prov=prov)


def test_repeated_section_row_dropped_before_cut_row_join() -> None:
    d = DoclingDocument(name="t")
    for p in (1, 2):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    head = ["Risk", "Summary", "Mitigation"]
    plain_table(d, [head, ["Study Intervention: Drug"], ["CRS", "A known toxicity of the drug class",
                                                         "Participants will be hospitalised after the first dose"]],
                pv(1, 720, 72), full_width=(1,))
    plain_table(d, [head, ["Study Intervention: Drug"], ["", "", "and monitored daily for seven days"],
                    ["Infections", "Common in this population", "See Section 6.5"]], pv(2, 720, 400), full_width=(1,))
    log = {}
    assert tm.merge_split_tables(d, ProtocolSettings(join_cut_rows="auto"), log) == 1
    t = d.tables[0]
    texts = [c.text for c in t.data.table_cells]
    assert texts.count("Study Intervention: Drug") == 1, texts
    assert "Participants will be hospitalised after the first dose and monitored daily for seven days" in texts
    assert t.data.num_rows == 4  # header, section, CRS, Infections
    assert log["repeated_section_rows"][0]["rows"] == 1
    assert log["joins"][0]["joined"] is True


def test_section_row_not_dropped_when_different() -> None:
    d = DoclingDocument(name="t")
    for p in (1, 2):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    head = ["Risk", "Summary", "Mitigation"]
    plain_table(d, [head, ["Study Intervention: Drug"], ["CRS", "x", "y"]], pv(1, 720, 72), full_width=(1,))
    plain_table(d, [head, ["Study Procedures"], ["Biopsy", "x", "y"]], pv(2, 720, 400), full_width=(1,))
    tm.merge_split_tables(d)
    texts = [c.text for c in d.tables[0].data.table_cells]
    assert "Study Procedures" in texts and texts.count("Study Intervention: Drug") == 1


def test_misflagged_header_row_kept_on_merge() -> None:
    """A continuation page whose first body row the model flagged as header: only header rows
    repeating the previous page's header are dropped; the body row (with its marks) stays."""
    d = DoclingDocument(name="t")
    for p in (1, 2):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    head = ["Activity", "Screening", "Day 1"]
    plain_table(d, [head, ["Consent", "X", ""], ["Exam", "X", "X"]], pv(1, 720, 72))
    plain_table(d, [head, ["Vital signs", "X", "X"], ["Labs", "", "X"]], pv(2, 720, 400), header_rows=2)
    log = {}
    assert tm.merge_split_tables(d, log=log) == 1
    t = d.tables[0]
    labels = [c.text for c in t.data.table_cells if c.start_col_offset_idx == 0]
    assert labels == ["Activity", "Consent", "Exam", "Vital signs", "Labs"], labels
    assert not any(c.column_header for c in t.data.table_cells if c.text == "Vital signs")
    assert log["kept_header_rows"][0]["rows"] == 1


def stacked_header_doc() -> DoclingDocument:
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    rows = [["Number of participants", "20-39", "40-64"], ["Minimum events", "2", "3"],
            ["Number of participants", "20-27", "28-35"], ["Minimum events grade 4", "4", "5"]]
    t = plain_table(d, rows, pv(1, 700, 600))
    for c in t.data.table_cells:  # TableFormer left the row-label header cell unflagged
        if (c.start_row_offset_idx, c.start_col_offset_idx) == (0, 0):
            c.column_header = False
    return d


def test_inner_header_rows_flagged() -> None:
    d = stacked_header_doc()
    log = {}
    assert tm.mark_inner_header_rows(d, log) == 1
    assert log["inner_header_rows"][0]["row"] == 2
    t = d.tables[0]
    assert all(c.column_header for c in t.data.table_cells if c.start_row_offset_idx == 2)
    assert not any(c.column_header for c in t.data.table_cells if c.start_row_offset_idx in (1, 3))
    assert tm.leading_header_rows(t) == 1


# ------------------------------------------------------------------ chunk row serializer

def serialize(d: DoclingDocument, index: int = 0) -> tuple[list[str], list[str]]:
    from chunk_protocol import ProtocolSerializerProvider, ProtocolTableSerializer

    ser = ProtocolSerializerProvider().get_serializer(d)
    return ProtocolTableSerializer.columns_and_rows(d.tables[index], d, ser)


def test_stacked_header_columns_switch() -> None:
    d = stacked_header_doc()
    tm.mark_inner_header_rows(d)
    cols, rows = serialize(d)
    assert rows == ["Minimum events: 20-39 = 2; 40-64 = 3",
                    "Number of participants: 20-27 | 28-35",
                    "Minimum events grade 4: 20-27 = 4; 28-35 = 5"], rows
    # also without the flags (the serializer detects the repeated row label itself)
    cols2, rows2 = serialize(stacked_header_doc())
    assert rows2 == rows


def soa_doc() -> DoclingDocument:
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    C = lambda text, r, c, rs=1, cs=1, h=False: TableCell(  # noqa: E731
        text=text, start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
        end_col_offset_idx=c + cs, row_span=rs, col_span=cs, column_header=h)
    cells = [C("Activity", 0, 0, rs=2, h=True), C("Screening", 0, 1, rs=2, h=True),
             C("Treatment Period (Each 28 Day Cycle)", 0, 2, cs=2, h=True), C("Notes\nDay 8 is Cycle 1 only", 0, 4, rs=2, h=True),
             C("Day 1", 1, 2, h=True), C("Day 8", 1, 3, h=True),
             C("Procedures – Note: all assessments within 72 hours before dosing", 2, 0, cs=5),
             C("Exam", 3, 0), C("X", 3, 1), C("X", 3, 2), C("See 8.2", 3, 4),
             C("Labs", 4, 0), C("X", 4, 1), C("X", 4, 3),
             C("AE monitoring", 5, 0), C("Assess continuously", 5, 1, cs=3)]
    d.add_table(data=TableData(num_rows=6, num_cols=5, table_cells=cells), prov=pv(1, 700, 500))
    return d


def test_rows_groups_sections_and_notes() -> None:
    cols, rows = serialize(soa_doc())
    assert cols == ["Activity", "Screening", "Treatment Period (Each 28 Day Cycle) [Day 1 | Day 8]",
                    "Notes (header note: Day 8 is Cycle 1 only)"], cols
    assert rows == [
        "Section: Procedures – Note: all assessments within 72 hours before dosing",
        "Procedures › Exam: Screening = X; Treatment Period (Each 28 Day Cycle) [Day 1 = X]; Notes = See 8.2",
        "Procedures › Labs: Screening = X; Treatment Period (Each 28 Day Cycle) [Day 8 = X]",
        "Procedures › AE monitoring: Screening to Treatment Period (Each 28 Day Cycle) - Day 8 = Assess continuously",
    ], rows


def test_row_contract_label_and_cells_on_one_line() -> None:
    """retrieve.py and the evaluation rely on 'Label: Column = value' lines: the row label and every
    cell of the row on the same line, the label before the first ': '."""
    from chunk_protocol import ProtocolSerializerProvider

    d = soa_doc()
    ser = ProtocolSerializerProvider().get_serializer(d)
    text = ser.serialize(item=d.tables[0]).text
    lines = text.splitlines()
    exam = [ln for ln in lines if "Exam:" in ln]
    assert len(exam) == 1 and all(v in exam[0] for v in ("X", "See 8.2"))
    assert exam[0].partition(": ")[0].endswith("Exam")
    assert lines[0].startswith("Columns: ")


def test_two_column_table_unchanged() -> None:
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    plain_table(d, [["Abbreviation", "Term"], ["AE", "adverse event"], ["CRS", "cytokine release syndrome"]],
                pv(1, 700, 600))
    cols, rows = serialize(d)
    assert cols == ["Abbreviation", "Term"] and rows == ["AE: adverse event", "CRS: cytokine release syndrome"]


# ------------------------------------------------------------------ validate_output table QA

def test_table_marks_counted() -> None:
    from validate_output import table_checks

    pdf = pdfium.PdfDocument.new()
    page = Page(pdf, 612, 792)
    draw_soa(page)
    pdf = save(pdf, [page], "marks")
    d = doc_with_table(fused_soa_cells(), 3, 3, (70, 653, 324, 702))
    out = table_checks(d, pdf)
    assert (out["marks_pdf"], out["marks_doc"], out["marks_missing"]) == (4, 4, 0), out
    assert out["lattice_final"] == {"disagree": 1}, out
    d.tables[0].data.table_cells[-1].text = "X"  # one mark lost
    out = table_checks(d, pdf, lattice=False)
    assert (out["marks_missing"], out["tables_missing_marks"]) == (1, 1), out


# ------------------------------------------------------------------ runner

def main() -> None:
    tests = [(name, fn) for name, fn in globals().items() if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok {name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed} tests passed" + (f", {failed} failed" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
