"""Self-test for protocol_postprocess.py / table_merge.py on small synthetic documents.

Run: `python tests/test_postprocess.py`. Each test builds the failure mode found
by the generalisation audit (reports/generalisation_audit.md, item in the name).
Coordinates are PDF points with a bottom-left origin on 612 x 792 pages.
"""

import json
from collections import Counter
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_core.types.doc import (BoundingBox, ContentLayer, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem,
                                    Size, TableCell, TableData)

import protocol_postprocess as pp
import table_merge as tm
from protocol_settings import ProtocolSettings


def pv(page: int = 1, t: float = 700, b: float = 690, l: float = 72, r: float = 540) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.BOTTOMLEFT),
                          charspan=(0, 1))


def new_doc(pages: int = 3) -> DoclingDocument:
    d = DoclingDocument(name="t")
    for p in range(1, pages + 1):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    return d


def add_table(d: DoclingDocument, rows: list[list[str]], prov: ProvenanceItem, header_rows: int = 1,
              label=DocItemLabel.TABLE, parent=None):
    cells = [TableCell(text=x, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                       end_col_offset_idx=c + 1, column_header=r < header_rows)
             for r, row in enumerate(rows) for c, x in enumerate(row)]
    return d.add_table(data=TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells),
                       prov=prov, label=label, parent=parent)


def text(d: DoclingDocument, s: str, prov: ProvenanceItem, label=DocItemLabel.TEXT, parent=None):
    return d.add_text(label=label, text=s, prov=prov, parent=parent)


class FakeBookmark:
    def __init__(self, level: int, title: str, page: int | None):
        self.level, self.title, self.page = level, title, page

    def get_title(self) -> str:
        return self.title

    def get_dest(self):
        page = self.page

        class Dest:
            def get_index(self):
                return page - 1

        return Dest() if page else None


# FakePdf has no character geometry, rules or images: the steps that need a real PDF are off.
NO_GEOMETRY = ProtocolSettings(reconcile_text=False, pair_label_values=False, table_lattice="off",
                               figure_reocr=False)


class FakePdf:
    """Just enough of pypdfium2.PdfDocument: outline, text range, bounded text."""

    def __init__(self, pages: int = 3, toc: list[tuple[int, str, int | None]] = (), bounded: str = ""):
        self.n, self.toc, self.bounded = pages, [FakeBookmark(*b) for b in toc], bounded

    def __len__(self) -> int:
        return self.n

    def get_toc(self):
        return iter(self.toc)

    def __getitem__(self, i):
        outer = self

        class Page:
            def get_textpage(self):
                class TextPage:
                    def get_text_range(self):
                        return ""

                    def get_text_bounded(self, *box):
                        return outer.bounded

                return TextPage()

        return Page()


# ---------------------------------------------------------------- A4 / A20 figures

def test_a4_figure_caption_kept() -> None:
    d = new_doc(1)
    pic = d.add_picture(prov=pv(t=700, b=300, l=50, r=550))
    cap = text(d, "Figure 1. Study Schema", pv(t=710, b=700, l=50, r=300), DocItemLabel.CAPTION, parent=pic)
    pic.captions.append(cap.get_ref())
    text(d, "Screening", pv(t=650, b=640, l=60, r=150), parent=pic)
    text(d, "Treatment", pv(t=650, b=640, l=400, r=500), parent=pic)
    assert pp.flatten_picture_text(d) == 1
    assert pic.caption_text(d) == "Figure 1. Study Schema"
    flat = [c.resolve(d).text for c in pic.children if c.cref != pic.captions[0].cref]
    assert flat == ["Text in figure: Screening; Treatment"], flat


def test_a20_figure_order() -> None:
    # Two boxes side by side, each with two lines: read box by box.
    d = new_doc(1)
    pic = d.add_picture(prov=pv(t=700, b=500, l=50, r=550))
    for s, t, l in [("B top", 680, 350), ("A top", 680, 60), ("A bottom", 600, 60), ("B bottom", 600, 350)]:
        text(d, s, pv(t=t, b=t - 10, l=l, r=l + 150), parent=pic)
    pp.flatten_picture_text(d)
    assert d.texts[-1].text == "Text in figure: A top; A bottom; B top; B bottom", d.texts[-1].text
    # A tall, top-down flow chart (boxes offset sideways) reads top to bottom, not by columns.
    d = new_doc(1)
    pic = d.add_picture(prov=pv(t=750, b=100, l=200, r=420))
    for s, t, l in [("Step 1", 740, 210), ("Step 2", 600, 300), ("Step 3", 450, 210), ("Step 4", 300, 300)]:
        text(d, s, pv(t=t, b=t - 10, l=l, r=l + 60), parent=pic)
    pp.flatten_picture_text(d)
    assert d.texts[-1].text == "Text in figure: Step 1; Step 2; Step 3; Step 4", d.texts[-1].text


# ---------------------------------------------------------------- A5 headings

def levels(titles: list[str], pdf=None, settings=None) -> tuple[list[int], DoclingDocument]:
    d = new_doc(3)
    for i, t in enumerate(titles):
        d.add_heading(text=t, level=1, prov=pv(page=1 + i // 4, t=700 - 50 * (i % 4), b=690 - 50 * (i % 4)))
    pp.fix_heading_levels(d, pdf, settings)
    return [h.level for h in d.texts], d


def test_a5_stray_large_number() -> None:
    lv, d = levels(["21 CFR Part 11 Compliance", "1. INTRODUCTION", "1.1. Background", "2. OBJECTIVES"])
    assert lv == [1, 1, 2, 1], lv
    assert list(pp.section_numbers(d).values()) == ["1", "1.1", "2"]


def test_a5_dot_zero_and_no_dot() -> None:
    lv, d = levels(["1.0 INTRODUCTION", "1.1 Background", "2.0 OBJECTIVES", "2.1 Primary"])
    assert lv == [1, 2, 1, 2], lv
    assert list(pp.section_numbers(d).values()) == ["1", "1.1", "2", "2.1"]
    lv, _ = levels(["1 INTRODUCTION", "1.1 Background", "2 OBJECTIVES"])
    assert lv == [1, 2, 1], lv


def test_a5_local_list_heading_rejected() -> None:
    titles = ["1. INTRODUCTION", "2. DESIGN", "3. APPENDICES", "3.1. Appendix 1", "3.1.1. Japan",
              "2. HBV Monitoring", "3.2. Appendix 2"]
    lv, d = levels(titles)
    assert lv == [1, 1, 1, 2, 3, 4, 2], lv
    nums = pp.section_numbers(d)
    assert list(nums.values()) == ["1", "2", "3", "3.1", "3.1.1", "3.2"], nums
    # The accepted numbers survive a JSON round trip (they are re-derived, not stored).
    again = DoclingDocument.model_validate(json.loads(json.dumps(d.export_to_dict())))
    assert pp.section_numbers(again) == nums


def test_a5_appendix_pattern() -> None:
    lv, _ = levels(["1. STATISTICS", "2. APPENDICES", "Appendix A Abbreviations", "Lab Tests", "Appendix B Labs"])
    assert lv == [1, 1, 2, 3, 2], lv
    lv, _ = levels(["1. INTRODUCTION", "2. METHODS", "Appendix A Abbreviations", "Lab Tests"])
    assert lv == [1, 1, 1, 2], lv


def test_a5_outline_levels() -> None:
    titles = ["ACME-1", "1 INTRODUCTION", "1.1 Background", "Study Design Overview", "Details", "2 METHODS"]
    toc = [(0, "/", 1), (1, "ACME-1", 1), (0, "1 INTRODUCTION", 1), (1, "1.1 Background", 1),
           (1, "Study Design Overview", 1), (0, "2 METHODS", 2), (1, "Table 1. Labs", 2)]
    log = {}
    d = new_doc(3)
    for i, t in enumerate(titles):
        d.add_heading(text=t, level=1, prov=pv(page=1 + i // 5, t=700 - 50 * (i % 5), b=690 - 50 * (i % 5)))
    pp.fix_heading_levels(d, FakePdf(toc=toc), log=log)
    assert log["heading_source"] == "outline", log
    # "Study Design Overview" is a sibling of 1.1 per the outline (numbering alone nests it at 3).
    assert [h.level for h in d.texts] == [1, 1, 2, 2, 3, 1], [h.level for h in d.texts]
    lv, _ = levels(titles, FakePdf(toc=[(0, "Something else", 1), (0, "Other", 2), (0, "1 INTRODUCTION", 1)]))
    assert lv == [1, 1, 2, 3, 3, 1], lv  # too few bookmarks match: numbering


# ---------------------------------------------------------------- A8 glyphs

OLD_MAP = {"": "•", "": "▪", "": "α", "": "β", "": "γ", "": "δ",
           "": "μ", "": "≥", "": "≤", "": "±", "": "×", "": "→"}


def test_a8_symbol_glyphs() -> None:
    assert all(pp._SYMBOL_FONT[k] == v for k, v in OLD_MAP.items())
    symbol = {"": "κ", "": "λ", "": "≠", "": "°", "": "≈", "": "Ω",
              "": "Δ", "": "π", "": "σ", "": "∞", "": "√", "": "∑",
              "": "∈", "": "⇒", "": "∅", "": "×", "": "÷", "": "−",
              "": "…", "": "θ"}
    wingdings = {"": "✓", "": "✗", "": "☑", "": "☒", "": "①"}
    for k, v in {**symbol, **wingdings}.items():
        assert pp._SYMBOL_FONT[k] == v, (hex(ord(k)), pp._SYMBOL_FONT.get(k), v)
    d = new_doc(1)
    item = text(d, "/ FLC ratio  1; temp C;  done ", pv())
    log = {}
    assert pp.normalize_symbol_glyphs(d, log=log) == 1
    assert item.text == "κ/λ FLC ratio ≠ 1; temp °C; ✓ done ", item.text
    assert log["unknown_pua"] == {"U+E123": 1}
    pp.normalize_symbol_glyphs(d, ProtocolSettings(unknown_pua="replace"))
    assert item.text.endswith("�")


# ---------------------------------------------------------------- A9 / A10 table footnotes and titles

def table_page(d, page: int, caption: str | None = None, rows=(("Test", "Unit"), ("Hb", "g/dL"))):
    if caption:
        d.add_heading(text=caption, prov=pv(page, t=720, b=710))
    return add_table(d, [list(r) for r in rows], pv(page, t=700, b=600))


def test_a9_body_text_not_footnote() -> None:
    d = new_doc(1)
    t = table_page(d, 1, "Table 1. Hematology")
    text(d, "Note that sites must also report results to the sponsor.", pv(t=595, b=585))
    text(d, "eGFR is calculated with CKD-EPI for all participants.", pv(t=580, b=570))
    stats = pp.resolve_repeated_table_titles(d)
    assert stats["footnotes_attached"] == 0 and not t.footnotes, stats


def test_a9_footnotes_attached_geometrically() -> None:
    d = new_doc(2)
    t = table_page(d, 1, "Table 1. Hematology")
    text(d, "a. Fasting sample", pv(t=596, b=586))
    text(d, "b. Central laboratory for", pv(t=584, b=574))
    text(d, "Notes: values are local", pv(t=572, b=562))
    text(d, "c. Unrelated lettered item far below", pv(t=300, b=290))  # not directly below
    stats = pp.resolve_repeated_table_titles(d)
    assert stats["footnotes_attached"] == 3, stats
    assert [f.resolve(d).text for f in t.footnotes] == ["a. Fasting sample", "b. Central laboratory for",
                                                        "Notes: values are local"]
    # Lowercase continuation: only as the first item on the next page, after an unfinished footnote.
    d = new_doc(2)
    t = table_page(d, 1, "Table 1. Hematology")
    text(d, "a. Central laboratory for", pv(t=596, b=586))
    text(d, "all participants at screening", pv(2, t=720, b=710))
    text(d, "pH is measured at every visit.", pv(2, t=700, b=690))
    pp.resolve_repeated_table_titles(d)
    assert [f.resolve(d).text for f in t.footnotes] == ["a. Central laboratory for all participants at screening"]
    assert any(x.text.startswith("pH") for x in d.texts)


def test_a9_symbol_markers_detected() -> None:
    d = new_doc(1)
    t = table_page(d, 1, "Table 1. Hematology")
    text(d, "* Data cut-off 1 March", pv(t=596, b=586))
    text(d, "† Central laboratory", pv(t=584, b=574))
    log = {}
    pp.resolve_repeated_table_titles(d, log=log)
    assert log["footnote_markers"] == ["symbol"], log
    assert len(t.footnotes) == 2


def test_a10_restarted_numbering_keeps_caption() -> None:
    d = new_doc(3)
    t1 = table_page(d, 1, "Table 1. Hematology")
    t2 = table_page(d, 3, "Table 1. Dose levels", rows=(("Level", "Dose"), ("-1", "10 mg")))
    stats = pp.resolve_repeated_table_titles(d)
    assert stats["repeated_titles_removed"] == 0 and stats["captions_restored"] == 2, stats
    assert t1.caption_text(d) == "Table 1. Hematology" and t2.caption_text(d) == "Table 1. Dose levels"


def test_a10_repeat_on_next_page_removed() -> None:
    d = new_doc(2)
    t1 = table_page(d, 1, "TABLE 2: Laboratory Tests")
    table_page(d, 2, "TABLE 2: Laboratory Tests (continued)", rows=(("Test", "Unit"), ("Plt", "10^9/L")))
    log = {}
    stats = pp.resolve_repeated_table_titles(d, log=log)
    assert stats == {"repeated_titles_removed": 1, "captions_restored": 1, "footnotes_attached": 0}, stats
    assert log["removed_titles"][0]["reason"] == "repeated_heading"
    assert t1.caption_text(d) == "TABLE 2: Laboratory Tests"
    assert tm.merge_split_tables(d) == 1  # the repeat no longer blocks the merge


def test_a10_title_below_and_body_sentence() -> None:
    d = new_doc(2)
    t1 = add_table(d, [["A", "B"], ["1", "2"]], pv(1, t=700, b=600))
    cap = text(d, "Table 1. Captioned below", pv(1, t=590, b=580), DocItemLabel.CAPTION, parent=t1)
    t1.captions.append(cap.get_ref())
    t2 = add_table(d, [["C", "D"], ["3", "4"]], pv(2, t=700, b=600))
    d.add_heading(text="Table 2. Also below", prov=pv(2, t=590, b=580))
    text(d, "Table 3 shows the doses for each cohort.", pv(2, t=500, b=490))
    t3 = add_table(d, [["E", "F"], ["5", "6"]], pv(2, t=480, b=400))
    log = {}
    pp.resolve_repeated_table_titles(d, log=log)
    assert log["caption_position"] == "below"
    assert t2.caption_text(d) == "Table 2. Also below" and not t3.captions


# ---------------------------------------------------------------- A12 TOC

def test_a12_toc_entries_only() -> None:
    d = new_doc(3)
    d.add_heading(text="TABLE OF CONTENTS", prov=pv(1, t=740, b=730))
    add_table(d, [["1 INTRODUCTION ........ 2"], ["2 METHODS ........ 3"]], pv(1, t=720, b=600), header_rows=0,
              label=DocItemLabel.DOCUMENT_INDEX)
    text(d, "2.1 Study Design ............. 3", pv(1, t=590, b=580))
    text(d, "This protocol is confidential and may not be disclosed.", pv(1, t=500, b=490))
    d.add_heading(text="1 INTRODUCTION", prov=pv(2))
    d.add_heading(text="2 METHODS", prov=pv(3))
    d.add_heading(text="2.1 Study Design", prov=pv(3, t=650, b=640))
    pp.fix_heading_levels(d)
    log = {}
    assert pp.rebuild_toc(d, log=log) == 3  # "1 INTRODUCTION" style TOC is recognised
    assert log["toc_items_deleted"] == 1 and log["deleted_toc_items"][0]["reason"] == "toc_entry", log
    assert any(t.text.startswith("This protocol") for t in d.texts)


# ---------------------------------------------------------------- A13 / A14 page-split tables

def split_table(first_rows, cont_rows, prev_bottom: float = 80, join="auto", header_rows=1):
    d = new_doc(3)
    a = add_table(d, first_rows, pv(1, t=700, b=prev_bottom))
    b = add_table(d, cont_rows, pv(2, t=720, b=500), header_rows=header_rows)
    text(d, "Body text after the table.", pv(2, t=480, b=72))  # sets the body bottom margin
    log = {}
    merged = tm.merge_split_tables(d, ProtocolSettings(join_cut_rows=join), log)
    return a, merged, log


HEAD = ["Panel", "Test", "Comment"]


def test_a13_short_subrow_not_joined() -> None:
    a, merged, log = split_table([HEAD, ["Hematology", "Hemoglobin", "X"]], [HEAD, ["", "Platelets", "X"]])
    assert merged == 1 and a.data.num_rows == 3, a.data.num_rows
    assert log["joins"][0] == {"table": a, "page": 2, "joined": False, "reason": "no_text_evidence"}


def test_a13_cut_row_joined() -> None:
    long = "Weekly during the first cycle and then"
    a, merged, log = split_table([HEAD, ["Hematology", "Hemoglobin", long]], [HEAD, ["", "", "every 4 weeks"]])
    assert a.data.num_rows == 2 and log["joins"][0]["reason"] == "lowercase_start"
    cell = [c for c in a.data.table_cells if c.start_row_offset_idx == 1 and c.start_col_offset_idx == 2][0]
    assert cell.text == f"{long} every 4 weeks"
    a, _, log = split_table([HEAD, ["Hematology", "Hemoglobin", long]], [HEAD, ["", "", "every 4 weeks"]],
                            join="off")
    assert a.data.num_rows == 3 and log["joins"][0]["reason"] == "disabled"


def test_a14_headerless_needs_page_break() -> None:
    rows = [["Parameter", "Result"], ["ALT", "normal"]]
    _, merged, log = split_table(rows, [["AST", "normal"]], prev_bottom=400, header_rows=0)
    assert merged == 0 and log["merges"][0]["reason"] == "no_header_not_at_page_break", log
    _, merged, log = split_table(rows, [["AST", "normal"]], prev_bottom=80, header_rows=0)
    assert merged == 1 and log["merges"][0]["reason"] == "no_header"


def test_a14_column_count_logged() -> None:
    _, merged, log = split_table([HEAD, ["a", "b", "c"]], [["Panel", "Test"], ["d", "e"]])
    assert merged == 0 and log["merges"][0]["reason"] == "cols_differ"


def fragments(*tables: list[list[str]]) -> tuple[DoclingDocument, list]:
    """One table fragment per page, each cut by the page break (the last ends mid-page)."""
    d = new_doc(len(tables) + 1)
    items = []
    for p, rows in enumerate(tables, start=1):
        last = p == len(tables)
        items.append(add_table(d, rows, pv(p, t=720, b=500 if last else 80)))
    text(d, "Body text after the table.", pv(len(tables), t=480, b=72))
    return d, items


def cell_at(t, row: int, col: int) -> str:
    return [c.text for c in t.data.table_cells if c.start_row_offset_idx == row and c.start_col_offset_idx == col][0]


SOA3 = ["Visit", "Study Day", "Assessment"]
COLLAPSE = ProtocolSettings(collapse_split_columns=True)
SOA4 = ["Visit", "Study", "Day", "Assessment"]  # "Study Day" split into two columns


def test_split_column_in_next_table_merged() -> None:
    d, (a, _) = fragments([SOA3, ["Screening", "-28", "ECG"]], [SOA4, ["Week 2", "Day", "15", "Labs"]])
    log = {}
    assert tm.merge_split_tables(d, COLLAPSE, log=log) == 1
    assert log["merges"][0]["reason"] == "split_cols:header_text", log["merges"]
    assert a.data.num_cols == 3 and a.data.num_rows == 3
    assert [cell_at(a, 2, c) for c in range(3)] == ["Week 2", "Day 15", "Labs"]
    assert tm.continuation_chains(d, collapse_split=True) == []


def test_split_column_chained_by_default() -> None:
    # Without ruling-line evidence the "split" page may be the correct one (TableFormer
    # fused columns elsewhere), so by default the fragments are linked, not collapsed.
    d, (a, b) = fragments([SOA3, ["Screening", "-28", "ECG"]], [SOA4, ["Week 2", "Day", "15", "Labs"]])
    log = {}
    assert tm.merge_split_tables(d, log=log) == 0 and log["merges"][0]["reason"] == "cols_differ"
    assert a.data.num_cols == 3 and b.data.num_cols == 4
    assert tm.continuation_chains(d) == [[a.self_ref, b.self_ref]]


def test_split_column_in_anchor_merged() -> None:
    # The anchor (already two pages long) has the split column; the third fragment does not.
    d, (a, _, _) = fragments([SOA4, ["Screening", "Day", "-28", "ECG"]], [SOA4, ["Day 1", "Day", "1", "Dosing"]],
                             [SOA3, ["Week 2", "15", "Labs"]])
    log = {}
    assert tm.merge_split_tables(d, COLLAPSE, log=log) == 2
    assert [m["reason"] for m in log["merges"]] == ["header_text", "split_cols:header_text"], log["merges"]
    assert a.data.num_cols == 3 and a.data.num_rows == 4 and len(d.tables) == 1
    assert [cell_at(a, 0, c) for c in range(3)] == ["Visit", "Study Day", "Assessment"]
    assert [cell_at(a, r, 1) for r in (1, 2, 3)] == ["Day -28", "Day 1", "15"]
    assert cell_at(a, 3, 2) == "Labs" and all(c.col_span == 1 for c in a.data.table_cells)


VIS3 = ["Visit Name", "Study Day", "Assessment Type"]
VIS2 = ["Visit Name", "Assessment Type"]  # "Study Day" missing on this page


def test_missing_column_not_merged_but_chained() -> None:
    d, (a, b) = fragments([VIS3, ["Screening", "-28", "ECG"]], [VIS2, ["Week 2", "Labs"]])
    log = {}
    assert tm.merge_split_tables(d, log=log) == 0
    assert log["merges"][0]["reason"] == "cols_differ"
    before = json.dumps(d.export_to_dict())
    assert tm.continuation_chains(d) == [[a.self_ref, b.self_ref]]
    assert json.dumps(d.export_to_dict()) == before  # pure


def test_header_mismatch_no_chain() -> None:
    d, _ = fragments([VIS3, ["Screening", "-28", "ECG"]], [["Drug Name", "Dose Level"], ["X", "10 mg"]])
    tm.merge_split_tables(d)
    assert tm.continuation_chains(d) == []


def test_three_fragment_chain() -> None:
    d, items = fragments([VIS3, ["Screening", "-28", "ECG"]], [VIS2, ["Week 2", "Labs"]],
                         [VIS3, ["Week 4", "29", "ECG"]])
    assert tm.merge_split_tables(d) == 0
    assert tm.continuation_chains(d) == [[t.self_ref for t in items]]
    # A non-table item between two fragments breaks the chain.
    d, items = fragments([VIS3, ["Screening", "-28", "ECG"]], [VIS2, ["Week 2", "Labs"]])
    text(d, "Unrelated paragraph.", pv(3, t=700, b=690))
    add_table(d, [VIS3, ["Week 4", "29", "ECG"]], pv(3, t=680, b=500))
    assert tm.continuation_chains(d) == [[t.self_ref for t in items]]


def test_missing_short_column_next_to_long_header_not_merged() -> None:
    # The joined headers of the last columns are 0.9-similar only because the notes header is long.
    notes = "Notes " + "describing the procedures performed at each visit in detail " * 3  # < 200 chars
    d, (a, b) = fragments([["Visit Name", "Week 4", notes], ["Screening", "X", "Fasting"]],
                          [["Visit Name", "Week 4", "End of Tx", notes], ["Week 8", "X", "X", "Labs"]])
    assert tm.merge_split_tables(d, COLLAPSE) == 0
    assert tm.continuation_chains(d, collapse_split=True) == [[a.self_ref, b.self_ref]]


def test_group_header_over_two_columns_not_collapsed() -> None:
    # One page: "Treatment Cycles" spans two visit columns; the next page has only one under it.
    d, (a, b) = fragments([["Visit", "Treatment Cycles", "Treatment Cycles", "Notes"], ["ECG", "X", "X", "-"]],
                          [["Visit", "Treatment Cycles", "Notes"], ["Labs", "X", "-"]])
    hdr = [c for c in a.data.table_cells if c.start_row_offset_idx == 0]
    a.data.table_cells = [c for c in a.data.table_cells if not (c.start_row_offset_idx == 0
                                                                and c.start_col_offset_idx == 2)]
    span = hdr[1]
    span.col_span, span.end_col_offset_idx = 2, 3
    assert tm.merge_split_tables(d, COLLAPSE) == 0
    assert a.data.num_cols == 4


def test_collapse_joins_cells_with_different_row_spans() -> None:
    # Split column: one half has a 2-row header cell, the other a header cell in each row.
    d, (a,) = fragments([["Visit", "Cy", "cle 1", "Notes"], ["", "", "D1", ""], ["ECG", "X", "", "-"]])
    for c in a.data.table_cells:
        c.column_header = c.start_row_offset_idx < 2
    a.data.table_cells = [c for c in a.data.table_cells if (c.start_row_offset_idx, c.start_col_offset_idx) != (1, 1)]
    tall = [c for c in a.data.table_cells if (c.start_row_offset_idx, c.start_col_offset_idx) == (0, 1)][0]
    tall.row_span, tall.end_row_offset_idx = 2, 2
    tm._collapse_columns(a, [(1, 3)])
    slots = Counter((r, c) for x in a.data.table_cells for r in range(x.start_row_offset_idx, x.end_row_offset_idx)
                    for c in range(x.start_col_offset_idx, x.end_col_offset_idx))
    assert a.data.num_cols == 3 and max(slots.values()) == 1, slots
    assert cell_at(a, 0, 1) == "Cy cle 1 D1" and cell_at(a, 2, 1) == "X"


def _box(l, t, r, b):
    return BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.TOPLEFT)


def _overlap_table(tall_rows, tall_box, small_row, small_text, small_box):
    """3x2 table; column 1 has a cell spanning tall_rows plus a 1-row cell in small_row."""
    d = new_doc(1)
    t = add_table(d, [["Visit", "Notes"], ["V1", "x"], ["V2", "y"]], pv(1))
    ys = {0: (100, 112), 1: (114, 126), 2: (128, 140)}
    for c in t.data.table_cells:
        c.bbox = _box(10 + 50 * c.start_col_offset_idx, ys[c.start_row_offset_idx][0],
                      50 + 50 * c.start_col_offset_idx, ys[c.start_row_offset_idx][1])
    t.data.table_cells = [c for c in t.data.table_cells if c.start_col_offset_idx == 0]
    lo, hi = tall_rows
    t.data.table_cells.append(TableCell(text="Notes for the first visits", start_row_offset_idx=lo,
                                        end_row_offset_idx=hi, row_span=hi - lo, start_col_offset_idx=1,
                                        end_col_offset_idx=2, bbox=tall_box))
    t.data.table_cells.append(TableCell(text=small_text, start_row_offset_idx=small_row,
                                        end_row_offset_idx=small_row + 1, start_col_offset_idx=1,
                                        end_col_offset_idx=2, bbox=small_box))
    return d, t


def _slots(t):
    return Counter((r, c) for x in t.data.table_cells for r in range(x.start_row_offset_idx, x.end_row_offset_idx)
                   for c in range(x.start_col_offset_idx, x.end_col_offset_idx))


def test_overlap_duplicate_fragment_dropped() -> None:
    d, t = _overlap_table((1, 3), _box(60, 114, 100, 140), 2, "first visits", _box(60, 129, 64, 132))
    log = {}
    assert pp.fix_overlapping_cells(d, log) == 1 and log["overlapping_cells"][0]["action"] == "dropped_duplicate"
    assert max(_slots(t).values()) == 1 and len([c for c in t.data.table_cells if c.start_col_offset_idx == 1]) == 1


def test_overlap_span_shrunk_to_its_box() -> None:
    # The spanning cell's box covers only row 1; a different cell sits in row 2 below it.
    d, t = _overlap_table((1, 3), _box(60, 114, 100, 126), 2, "Other text", _box(60, 128, 100, 140))
    assert pp.fix_overlapping_cells(d) == 1
    assert max(_slots(t).values()) == 1
    texts = {c.text for c in t.data.table_cells}
    assert {"Notes for the first visits", "Other text"} <= texts


def test_overlap_shrunk_header_frees_header_cell() -> None:
    # Header "Notes" spans rows 0-1 but its box covers row 0 only; row 1 is a header row
    # ("Day 1") with an untagged cell under Notes, which must count as header afterwards.
    d, t = _overlap_table((0, 2), _box(60, 100, 100, 112), 1, "Day 4 is Cycle 1 only", _box(60, 114, 100, 126))
    for c in t.data.table_cells:
        c.column_header = c.start_row_offset_idx < 2 and c.text != "Day 4 is Cycle 1 only"
    assert pp.fix_overlapping_cells(d) == 1 and max(_slots(t).values()) == 1
    assert tm._header_rows(t) == 2


def test_overlap_unclear_kept() -> None:
    # Boxes overlap vertically and texts differ: nothing is changed, the case is logged.
    d, t = _overlap_table((1, 3), _box(60, 114, 100, 140), 2, "Other text", _box(60, 128, 100, 140))
    log = {}
    assert pp.fix_overlapping_cells(d, log) == 0 and [e["action"] for e in log["overlapping_cells"]] == ["kept"]
    assert len(t.data.table_cells) == 5


# ---------------------------------------------------------------- A16 / A17 / A25

def test_a16_side_note_not_absorbed() -> None:
    d = new_doc(1)
    g = d.add_group(label="list")
    li = d.add_list_item(text="Criterion 9", marker="9.", parent=g, prov=pv(t=700, b=650, l=72, r=300))
    text(d, "Side note box", pv(t=690, b=680, l=250, r=550))  # starts inside the item, mostly beside it
    assert pp.absorb_split_list_lines(d, FakePdf()) == 0
    text(d, "Cohort B: wide line", pv(t=680, b=670, l=90, r=320))
    assert pp.absorb_split_list_lines(d, FakePdf(bounded="9. Criterion 9  2 lines")) == 1
    assert li.text == "Criterion 9  2 lines"


def test_a16_glyphs_fixed_after_list_repair() -> None:
    d = new_doc(1)
    g = d.add_group(label="list")
    li = d.add_list_item(text="Criterion 9", marker="9.", parent=g, prov=pv(t=700, b=650, l=72, r=300))
    text(d, "Cohort B: wide line", pv(t=680, b=670, l=90, r=320))
    counts = pp.postprocess(d, FakePdf(1, bounded="9. Criterion 9 age  18"), NO_GEOMETRY)
    assert counts["list_items_repaired"] == 1 and li.text == "Criterion 9 age ≥ 18", li.text


def test_hyphen_split_paragraph_joined() -> None:
    # The layout model cut a paragraph into two items at a line ending with a hyphen.
    d = new_doc(1)
    a = text(d, "Participants must not have received prior EGFR-", pv(t=700, b=688))
    text(d, "directed therapy within 4 weeks.", pv(t=685, b=673, r=300))  # the next line, 3 pt below
    text(d, "A new paragraph.", pv(t=650, b=638))
    log = {}
    assert pp.join_hyphen_split_items(d, log) == 1
    assert a.text == "Participants must not have received prior EGFR- directed therapy within 4 weeks.", a.text
    assert [t.text for t in d.texts] == [a.text, "A new paragraph."]
    assert a.prov[0].bbox.b == 673 and a.prov[0].bbox.t == 700  # one box over both lines
    assert log["hyphen_joins"] == [{"item": a, "page": 1}]
    # a list item continued on its next line (no marker of its own) is joined too
    d = new_doc(1)
    g = d.add_group(label="list")
    li = d.add_list_item(text="Have received Days 1-", marker="", parent=g, prov=pv(t=700, b=688, l=108))
    d.add_list_item(text="7 of treatment", marker="", parent=g, prov=pv(t=685, b=673, l=72))
    assert pp.join_hyphen_split_items(d) == 1 and li.text == "Have received Days 1- 7 of treatment"


def test_hyphen_split_not_joined() -> None:
    cases = [
        (("Range 1-", pv(t=700, b=688)), ("3 more text", pv(t=670, b=658))),                  # a gap: new paragraph
        (("Range 1-", pv(t=700, b=688, r=200)), ("3 side note", pv(t=685, b=673, l=300))),    # beside, not below
        (("Range 1 -", pv(t=700, b=688)), ("3 more text", pv(t=685, b=673))),                 # a dash, not attached
        (("Range 1-", pv(t=700, b=688)), ("(3) more text", pv(t=685, b=673))),                # not a word
        (("Range 1-", pv(t=700, b=688)), ("3 more text", pv(2, t=780, b=768))),               # next page
    ]
    for (sa, pa), (sb, pb) in cases:
        d = new_doc(2)
        text(d, sa, pa)
        text(d, sb, pb)
        assert pp.join_hyphen_split_items(d) == 0, (sa, sb)
    # never past an item with its own list marker
    d = new_doc(1)
    g = d.add_group(label="list")
    d.add_list_item(text="first item ends EGFR-", marker="a)", parent=g, prov=pv(t=700, b=688))
    d.add_list_item(text="directed agents", marker="b)", parent=g, prov=pv(t=685, b=673))
    assert pp.join_hyphen_split_items(d) == 0
    # a heading below is never joined
    d = new_doc(1)
    text(d, "Range 1-", pv(t=700, b=688))
    d.add_heading(text="3 Results", prov=pv(t=685, b=673))
    assert pp.join_hyphen_split_items(d) == 0


def test_single_page_headers_restored_unless_noise() -> None:
    d = new_doc(4)

    def furn(s: str, page: int, label=DocItemLabel.PAGE_HEADER):
        return d.add_text(label=label, text=s, prov=pv(page, t=780, b=770), content_layer=ContentLayer.FURNITURE)

    for p in (2, 3, 4):
        furn("CONFIDENTIAL Clinical Study Protocol No. ABC 1234", p)
        furn(f"Page {p} of 4", p, DocItemLabel.PAGE_FOOTER)
    nct = furn("Study ABC-12 NCT #: NCT01234567", 1)  # title-page identifier: content
    fragment = furn("CONFIDENTIAL Clinical Study Protocol", 1)  # the running header, split differently
    paging = furn("Version 1.0 Page: 1 of 4", 1)  # a page-number line
    field = furn("Data Monitoring Committee: Yes", 1, DocItemLabel.PAGE_FOOTER)
    log = {}
    assert pp.restore_unique_headers(d, log) == 2
    assert nct.content_layer == field.content_layer == ContentLayer.BODY
    assert fragment.content_layer == paging.content_layer == ContentLayer.FURNITURE
    assert log["restored_headers"] == [{"page": 1, "reason": "single_page_header"}] * 2


def test_a17_running_header_heading_kept() -> None:
    d = new_doc(3)
    for p in (1, 2, 3):
        d.add_text(label=DocItemLabel.PAGE_HEADER, text="SCHEDULE OF ACTIVITIES", prov=pv(p, t=780, b=770),
                   content_layer=ContentLayer.FURNITURE)
    h = d.add_heading(text="SCHEDULE OF ACTIVITIES", prov=pv(2, t=600, b=590))
    stray = text(d, "SCHEDULE OF ACTIVITIES", pv(3, t=785, b=775))  # in the top band
    log = {}
    assert pp.demote_stray_furniture(d, log=log) == 1
    assert h.content_layer == ContentLayer.BODY and stray.content_layer == ContentLayer.FURNITURE


def test_a25_real_figure_over_table_kept() -> None:
    d = new_doc(1)
    add_table(d, [["Visit", "Day"], ["Screening", "-28"]], pv(t=700, b=400))
    shadow = d.add_picture(prov=pv(t=700, b=400))
    text(d, "Screening", pv(t=690, b=680), parent=shadow)
    real = d.add_picture(prov=pv(t=650, b=450))
    text(d, "Randomisation", pv(t=640, b=630), parent=real)
    log = {}
    assert pp.drop_table_shadow_pictures(d, log) == 1
    assert len(d.pictures) == 1 and log["kept_overlapping_pictures"][0]["missing_words"] == 1
    assert log["shadow_pictures"][0]["area_fraction"] > 0


# ---------------------------------------------------------------- decisions log and settings

def test_decisions_log_has_no_text() -> None:
    d = new_doc(2)
    table_page(d, 1, "Table 1. Secret Hematology Panel")
    text(d, "a. Secret footnote", pv(t=596, b=586))
    table_page(d, 2, "Table 1. Secret Hematology Panel")
    d.add_heading(text="1. Secret Introduction", prov=pv(2, t=500, b=490))
    d.add_heading(text="7. Secret stray number", prov=pv(2, t=400, b=390))
    counts = pp.postprocess(d, FakePdf(2), NO_GEOMETRY)
    dump = json.dumps(pp.postprocess.last_decisions)
    assert "Secret" not in dump and json.dumps(counts)
    dec = pp.postprocess.last_decisions
    assert counts["tables_merged"] == 1 and dec["merges"][0]["table"] == "#/tables/0", dec["merges"]
    assert len(dec["footnotes"]) == 1 and len(dec["removed_titles"]) == 1 and len(dec["demoted_headings"]) == 1


def test_settings_profile() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "profile.json"
        p.write_text(json.dumps({"join_cut_rows": "off", "footnote_markers": [r"\*"]}), encoding="utf-8")
        s = ProtocolSettings.load(p)
        assert s.join_cut_rows == "off" and s.footnote_markers == [r"\*"] and s.heading_source == "auto"
        p.write_text(json.dumps({"join_cut_row": "off"}), encoding="utf-8")
        try:
            ProtocolSettings.load(p)
        except ValueError:
            pass
        else:
            raise AssertionError("unknown key accepted")
    assert ProtocolSettings.load(None) == ProtocolSettings()


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
