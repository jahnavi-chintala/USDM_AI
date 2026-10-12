"""Self-test for soa.py (structured Schedule of Activities) on hand-built DoclingDocuments.

Run: `python tests/test_soa.py` (no pytest). Covers: a multi-row header with a group header, the visit-window
row, the notes column (with a header note), full-width and label-only section rows, a header row repeated on a
later page, footnote markers in cells / labels / headers resolved against attached and following footnotes in
several styles, a continuation chain (a column missing on the next page), a horizontally split SoA (more visits
on the next page), windows and stub units inside headers, detection through a "soa" section role, lookups.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size,
                                    TableCell, TableData)

import soa


# ------------------------------------------------------------------ builders

def C(text: str, r: int, c: int, rs: int = 1, cs: int = 1, h: bool = False) -> TableCell:
    return TableCell(text=text, start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                     end_col_offset_idx=c + cs, row_span=rs, col_span=cs, column_header=h)


def pv(page: int, t: float, b: float) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=72, t=t, r=720, b=b, coord_origin=CoordOrigin.BOTTOMLEFT),
                          charspan=(0, 1))


def new_doc(pages: int = 1) -> DoclingDocument:
    d = DoclingDocument(name="t")
    for p in range(1, pages + 1):
        d.add_page(page_no=p, size=Size(width=792, height=612))
    return d


def add_table(d: DoclingDocument, cells: list[TableCell], prov: ProvenanceItem, footnotes: list[str] = ()):
    n_rows = max(c.end_row_offset_idx for c in cells)
    n_cols = max(c.end_col_offset_idx for c in cells)
    t = d.add_table(data=TableData(num_rows=n_rows, num_cols=n_cols, table_cells=cells), prov=prov)
    for f in footnotes:
        item = d.add_text(label=DocItemLabel.FOOTNOTE, text=f, parent=t)
        t.footnotes.append(item.get_ref())
    return t


def grid_rows(rows: list[list[str]], start: int = 0, header: int = 0) -> list[TableCell]:
    """Single-span cells from a list of rows (empty strings skipped)."""
    return [C(x, start + r, c, h=r < header) for r, row in enumerate(rows) for c, x in enumerate(row) if x]


def main_soa_doc() -> DoclingDocument:
    """Two header rows (a group over three days), a window row, a notes column with a header note, sections
    across the table, a header row repeated on a later page, markers in every style, footnotes attached and
    after the table, then a heading and an unrelated list that must not become footnotes."""
    d = new_doc()
    cells = [
        C("Activity", 0, 0, rs=2, h=True), C("Screening", 0, 1, rs=2, h=True),
        C("Treatment Period (Each 28 Day Cycle)", 0, 2, cs=3, h=True), C("EOT", 0, 5, rs=2, h=True),
        C("Notes/Protocol Section\nDay 4 Visit is Cycle 1 only", 0, 6, rs=3, h=True),
        C("Day 1", 1, 2, h=True), C("Day 4", 1, 3, h=True), C("Day 8^a", 1, 4, h=True),
        # the visit-window row (unflagged body row, as after a page merge)
        C("Visit Window", 2, 0), C("Day -28 to Day -1", 2, 1), C("±3D", 2, 2), C("-1/+3D", 2, 3), C("± 3D", 2, 4),
        C("≤14D post-final dose", 2, 5),
        C("Clinical Procedures/Assessments - Note: within 72 hours prior to dosing", 3, 0, cs=7),
        C("Physical exam", 4, 0), C("X", 4, 1), C("X", 4, 2), C("X", 4, 5), C("See Section 8.2.2", 4, 6),
        C("Vital signs^b", 5, 0), C("X", 5, 1), C("X", 5, 2), C("Cycle 1", 5, 3), C("Cycles 1-3", 5, 4), C("X", 5, 5),
        C("AE monitoring", 6, 0), C("Assess continuously", 6, 1, cs=4), C("X", 6, 5), C("See Section 8.3", 6, 6),
        # page 2 repeats the header row
        C("Activity", 7, 0), C("Screening", 7, 1), C("Day 1", 7, 2), C("Day 4", 7, 3), C("Day 8^a", 7, 4),
        C("EOT", 7, 5), C("Notes/Protocol Section", 7, 6),
        C("Laboratory", 8, 0, cs=7),
        C("Hematology", 9, 0), C("X c", 9, 1), C("X", 9, 2), C("Xd", 9, 4), C("X^z", 9, 5),
        C("Pregnancy test e", 10, 0), C("X", 10, 1), C("X*", 10, 5), C("*Only if WOCBP", 10, 6),
    ]
    add_table(d, cells, pv(1, 560, 60), footnotes=["Abbreviations: EOT = end of treatment", "^a Day 8 is optional."])
    for txt in ("b. Vital signs include temperature and blood pressure.", "c Fasting sample required.",
                "d.Only in Cycle 1.", "e) Women of childbearing potential only."):
        d.add_text(label=DocItemLabel.TEXT, text=txt, prov=pv(1, 50, 40))
    d.add_heading(text="2. Introduction", level=1)
    d.add_text(label=DocItemLabel.TEXT, text="z. Not a footnote of the table.")
    return d


def by_label(items: list[dict], label: str) -> dict:
    return next(x for x in items if x["label"] == label)


def cell(s: dict, activity: str, visit: str) -> dict | None:
    a, v = by_label(s["activities"], activity)["id"], by_label(s["visits"], visit)["id"]
    return next((c for c in s["cells"] if c["activity"] == a and c["visit"] == v), None)


# ------------------------------------------------------------------ the main synthetic SoA

def test_visits_from_multirow_header_and_window_row() -> None:
    [s] = soa.extract_soa(main_soa_doc())
    assert [v["label"] for v in s["visits"]] == ["Screening", "Day 1", "Day 4", "Day 8", "EOT"], s["visits"]
    d1 = by_label(s["visits"], "Day 1")
    assert d1["header_path"] == ["Treatment Period (Each 28 Day Cycle)", "Day 1"]
    assert d1["epoch_hint"] == "Treatment Period (Each 28 Day Cycle)" and d1["timing"] == "Day 1"
    assert d1["window"] == "±3D" and d1["column"] == 2
    scr = by_label(s["visits"], "Screening")
    assert scr["window"] == "Day -28 to Day -1" and scr["timing"] == "Day -28 to Day -1" and scr["epoch_hint"] is None
    assert by_label(s["visits"], "EOT")["window"] == "≤14D post-final dose"
    assert by_label(s["visits"], "Day 8")["footnote_markers"] == ["a"]
    assert [v["id"] for v in s["visits"]] == ["V1", "V2", "V3", "V4", "V5"]
    # neither the window row nor the repeated header row is an activity
    labels = [a["label"] for a in s["activities"]]
    assert labels == ["Physical exam", "Vital signs", "AE monitoring", "Hematology", "Pregnancy test"], labels


def test_notes_column_and_header_note() -> None:
    [s] = soa.extract_soa(main_soa_doc())
    assert not any("Notes" in v["label"] for v in s["visits"])
    assert by_label(s["activities"], "Physical exam")["notes"] == "See Section 8.2.2"
    assert s["header_notes"] == ["Day 4 Visit is Cycle 1 only"]


def test_sections_qualifiers_and_spans() -> None:
    [s] = soa.extract_soa(main_soa_doc())
    pe = by_label(s["activities"], "Physical exam")
    assert pe["section"].startswith("Clinical Procedures/Assessments")
    assert by_label(s["activities"], "Hematology")["section"] == "Laboratory"
    c = cell(s, "Vital signs", "Day 4")
    assert c["mark"] is False and c["qualifier"] == "Cycle 1"
    assert cell(s, "Vital signs", "Day 8")["qualifier"] == "Cycles 1-3"
    assert cell(s, "Physical exam", "Day 4") is None
    ae = [cell(s, "AE monitoring", v) for v in ("Screening", "Day 1", "Day 4", "Day 8")]
    assert all(x and x["qualifier"] == "Assess continuously" and not x["mark"] for x in ae), ae
    assert cell(s, "AE monitoring", "EOT")["mark"] is True


def test_footnote_markers_resolved_in_all_styles() -> None:
    [s] = soa.extract_soa(main_soa_doc())
    fn = s["footnotes"]
    assert fn["a"] == "Day 8 is optional."                                   # attached, '^a Text'
    assert fn["b"].startswith("Vital signs include")                         # following, 'b. Text'
    assert fn["c"] == "Fasting sample required." and fn["d"] == "Only in Cycle 1."  # 'c Text', 'd.Text'
    assert fn["e"].startswith("Women")                                       # 'e) Text'
    assert "*" in fn and "z" not in fn                                       # notes column; after the heading: no
    assert by_label(s["activities"], "Vital signs")["footnote_markers"] == ["b"]
    assert by_label(s["activities"], "Pregnancy test")["footnote_markers"] == ["e"]
    h = cell(s, "Hematology", "Screening")
    assert (h["mark"], h["qualifier"], h["footnote_markers"], h["text"]) == (True, None, ["c"], "X c")
    assert cell(s, "Hematology", "Day 8")["footnote_markers"] == ["d"]
    assert cell(s, "Pregnancy test", "EOT")["footnote_markers"] == ["*"]
    assert any(w == "unresolved footnote markers: z" for w in s["warnings"]), s["warnings"]


def test_lookup_and_markdown() -> None:
    [s] = soa.extract_soa(main_soa_doc())
    hits = soa.soa_lookup([s], visit="C1D8")
    assert {h["visit"] for h in hits} == {"Day 8"} and {h["activity"] for h in hits} == {
        "Vital signs", "AE monitoring", "Hematology"}
    assert {h["visit"] for h in soa.soa_lookup([s], visit="end of treatment")} == {"EOT"}
    vit = soa.soa_lookup([s], activity="vital SIGNS")
    assert len(vit) == 5 and vit[0]["footnotes"] == {"b": s["footnotes"]["b"]}
    [h] = soa.soa_lookup([s], visit="day 4", activity="vital")
    assert h["qualifier"] == "Cycle 1" and h["window"] == "-1/+3D" and h["section"].startswith("Clinical")
    md = soa.to_markdown(s)
    assert "| Activity | Screening | Day 1 | Day 4 | Day 8 | EOT | Notes |" in md
    assert "| **Laboratory** |" in md and "| _Window_ | Day -28 to Day -1 |" in md
    assert "- a: Day 8 is optional." in md


# ------------------------------------------------------------------ several tables -> one SoA

def chain_doc() -> DoclingDocument:
    """Page 1: 6 columns; page 2: the same header without the last visit column. table_merge cannot merge
    them (the column counts differ) and links them as a continuation chain."""
    d = new_doc(2)
    head = ["Activity", "Screening", "Day 1", "Day 8", "EOT", "LTFU"]
    add_table(d, grid_rows([head, ["Informed consent", "X", "", "", "", ""], ["Physical exam", "X", "X", "X", "X", ""],
                            ["Survival status", "", "", "", "X", "X"]], header=1), pv(1, 560, 40))
    add_table(d, grid_rows([head[:5], ["Hematology", "X", "X", "X", "X"], ["Urinalysis", "X", "", "X", ""]],
                           header=1), pv(2, 560, 300))
    d.add_text(label=DocItemLabel.TEXT, text="Page footer text", prov=pv(2, 200, 190))
    return d


def test_continuation_chain_combined() -> None:
    from table_merge import continuation_chains
    d = chain_doc()
    assert continuation_chains(d) == [["#/tables/0", "#/tables/1"]]
    soas = soa.extract_soa(d)
    assert len(soas) == 1, [s["table_ids"] for s in soas]
    s = soas[0]
    assert s["table_ids"] == ["#/tables/0", "#/tables/1"] and s["pages"] == [1, 2]
    assert [v["label"] for v in s["visits"]] == ["Screening", "Day 1", "Day 8", "EOT", "LTFU"]
    assert [a["label"] for a in s["activities"]] == ["Informed consent", "Physical exam", "Survival status",
                                                     "Hematology", "Urinalysis"]
    assert cell(s, "Urinalysis", "Day 8")["mark"] and cell(s, "Hematology", "EOT")["mark"]
    assert by_label(s["activities"], "Hematology")["table_id"] == "#/tables/1"


def test_horizontal_split_combined_and_stub_units() -> None:
    """Two pages with the same activities and more visits; the stub column names the header rows
    ('VISIT' / 'WEEK'); a row empty on page 1 is filled on page 2 (an activity, not a section)."""
    d = new_doc(2)
    for page, (visits, weeks, rows) in enumerate([
            (["1", "2", "3", "4"], ["-2", "-.3", "0", "2"],
             [["Consent", "X", "", "", ""], ["ECG", "X", "", "X", "X"], ["Survey", "", "", "", ""],
              ["ADAS-Cog", "P", "", "X", ""]]),
            (["5", "7", "ET", "RT"], ["4", "6", "", ""],
             [["Consent", "", "", "", ""], ["ECG", "X", "X", "X", ""], ["Survey", "", "X", "X", "X"],
              ["ADAS-Cog", "X", "", "Xa", "X"]])], start=1):
        cells = [C("VISIT", 0, 1, h=True)] + [C(x, 0, 2 + k, h=True) for k, x in enumerate(visits)]
        cells += [C("ACTIVITY", 1, 0), C("WEEK", 1, 1)] + [C(x, 1, 2 + k) for k, x in enumerate(weeks) if x]
        cells += [C(x, 2 + r, 0 if k == 0 else k + 1) for r, row in enumerate(rows) for k, x in enumerate(row) if x]
        add_table(d, cells, pv(page, 560, 100))
        d.add_text(label=DocItemLabel.TEXT, text="X^a = Performed if diabetic.", prov=pv(page, 90, 80))
    [s] = soa.extract_soa(d)
    assert s["table_ids"] == ["#/tables/0", "#/tables/1"]
    assert [v["header_path"] for v in s["visits"]][:2] == [["Visit 1", "Week -2"], ["Visit 2", "Week -0.3"]]
    assert [v["label"] for v in s["visits"]] == ["Week -2", "Week -0.3", "Week 0", "Week 2", "Week 4", "Week 6",
                                                 "ET", "RT"]
    assert by_label(s["visits"], "Week 2")["timing"] == "Week 2" and by_label(s["visits"], "RT")["timing"] is None
    assert [a["label"] for a in s["activities"]] == ["Consent", "ECG", "Survey", "ADAS-Cog"]
    assert all(a["section"] is None for a in s["activities"])
    assert cell(s, "Survey", "Week 6")["mark"] and cell(s, "ECG", "Week 2")["mark"] and cell(s, "ECG", "Week 4")["mark"]
    p = cell(s, "ADAS-Cog", "Week -2")
    assert (p["mark"], p["qualifier"]) == (False, "P")
    assert cell(s, "ADAS-Cog", "ET")["footnote_markers"] == ["a"] and s["footnotes"]["a"] == "Performed if diabetic."


def test_label_only_section_rows() -> None:
    """Without sections across the table, a label-only row followed by a filled row is a section; a
    label-only row followed by another one stays an (empty) activity."""
    d = new_doc()
    rows = [["Procedure", "Screening", "Day 1", "Day 8", "Day 15", "EOT"],
            ["Eligibility", "", "", "", "", ""], ["Informed consent", "X", "", "", "", ""],
            ["Inclusion criteria", "X", "X", "", "", ""],
            ["PD sampling (see manual)", "", "", "", "", ""], ["Safety", "", "", "", "", ""],
            ["Vital signs", "X", "X", "X", "X", "X"]]
    add_table(d, grid_rows(rows, header=1), pv(1, 560, 100))
    [s] = soa.extract_soa(d)
    acts = {a["label"]: a["section"] for a in s["activities"]}
    assert acts == {"Informed consent": "Eligibility", "Inclusion criteria": "Eligibility",
                    "PD sampling (see manual)": "Eligibility", "Vital signs": "Safety"}, acts
    assert s["section_rows"] == [{"table_id": "#/tables/0", "row": 1}, {"table_id": "#/tables/0", "row": 5}]
    # The chunk serializer writes the same rows as sections and carries them into the row labels below.
    from chunk_protocol import ProtocolSerializerProvider, ProtocolTableSerializer, soa_section_rows

    assert soa_section_rows(d) == {"#/tables/0": {1, 5}}
    _, lines = ProtocolTableSerializer.columns_and_rows(d.tables[0], d, ProtocolSerializerProvider().get_serializer(d))
    assert lines[:2] == ["Section: Eligibility", "Eligibility › Informed consent: Screening = X"], lines
    assert "Section: Safety" in lines and lines[-1].startswith("Safety › Vital signs: ")
    assert "Eligibility › PD sampling (see manual)" in lines  # an empty activity stays a row under its section


def test_label_only_rows_with_sections_across_not_carried() -> None:
    """With sections across the table, soa.py keeps a label-only row as an activity, and so does the chunker."""
    d = new_doc()
    rows = [["Procedure", "Screening", "Day 1", "Day 8", "Day 15", "EOT"],
            ["Tumor imaging (see 8.1)", "", "", "", "", ""], ["Informed consent", "X", "", "", "", ""],
            ["Vital signs", "X", "X", "X", "X", "X"], ["ECG", "X", "", "X", "", "X"]]
    cells = grid_rows(rows[:1], header=1) + [C("Clinical assessments", 1, 0, cs=6)] + grid_rows(rows[1:], start=2)
    add_table(d, cells, pv(1, 560, 100))
    [s] = soa.extract_soa(d)
    assert s["section_rows"] == [] and by_label(s["activities"], "Tumor imaging (see 8.1)")["section"] == \
        "Clinical assessments"
    from chunk_protocol import soa_section_rows

    assert soa_section_rows(d) == {}


def test_header_windows_stub_units_and_split_column() -> None:
    """Windows written inside the header ('D1 (±1)', 'W2 15±4'), a stub naming week and study day, a
    bullet-led notes header spanning the header rows, and one visit split over two grid columns."""
    d = new_doc()
    cells = [C("Evaluation", 0, 0, rs=3, h=True), C("Treatment", 0, 1, cs=4, h=True), C("FUP (90±7 days)", 0, 5, rs=3, cs=2, h=True),
             C("• Notes: all visits within the window\n• Fasting before labs", 0, 7, rs=3, h=True),
             C("Cycle 1", 1, 1, cs=3, h=True), C("Cycle 2", 1, 4, h=True),
             C("D1 (±1)", 2, 1, h=True), C("D8 (± 1)", 2, 2, h=True), C("D15", 2, 3, h=True), C("D1 (± 2)", 2, 4, h=True),
             C("Week [W] Study day and allowed time window", 3, 0), C("W0 1", 3, 1), C("W1 8±1", 3, 2),
             C("W2 15±1", 3, 3), C("W3 22±2", 3, 4),
             C("Vital signs", 4, 0), C("X", 4, 1), C("X", 4, 2), C("X", 4, 3), C("X", 4, 4), C("X", 4, 5),
             C("Labs", 5, 0), C("X", 5, 1), C("X", 5, 3), C("X", 5, 6), C("Fasting", 5, 7),
             C("ECG", 6, 0), C("X", 6, 1), C("X", 6, 4)]
    add_table(d, cells, pv(1, 560, 100))
    [s] = soa.extract_soa(d)
    vs = s["visits"]
    assert [v["label"] for v in vs] == ["W0 1", "W1 8±1", "W2 15±1", "W3 22±2", "FUP (90±7 days)"], vs
    d8 = by_label(vs, "W1 8±1")
    assert d8["window"] == "± 1" and d8["timing"] == "Cycle 1, D8, W1 Day 8", d8
    assert d8["epoch_hint"] == "Treatment" and by_label(vs, "W3 22±2")["timing"] == "Cycle 2, D1, W3 Day 22"
    assert d8["header_path"] == ["Treatment", "Cycle 1", "D8 (± 1)", "W1 8±1"]
    fup = by_label(vs, "FUP (90±7 days)")
    assert fup["window"] == "±7 days" and fup["timing"] == "90 days" and fup["column"] == 5
    assert cell(s, "Labs", "FUP (90±7 days)")["mark"] and cell(s, "Vital signs", "FUP (90±7 days)")["mark"]
    assert by_label(s["activities"], "Labs")["notes"] == "Fasting"


def test_role_soa_section_and_detection() -> None:
    """A 5-column grid is not an SoA by itself; inside a 'Schedule of Activities' section it is. A 6-column
    table of numbers is never one."""
    import structure
    d = new_doc()
    d.add_heading(text="1.3. Schedule of Activities", level=2)
    add_table(d, grid_rows([["Procedure", "Screening", "Day 1", "Day 8", "EOT"], ["Consent", "X", "", "", ""],
                            ["Exam", "X", "X", "", "X"], ["Labs", "X", "", "X", "X"]], header=1), pv(1, 560, 300))
    d.add_heading(text="9. Statistics", level=1)
    add_table(d, grid_rows([["N", "20-39", "40-64", "65-79", "80-89", "90+"], ["Events", "2", "3", "4", "5", "6"],
                            ["Rate", "1.0", "2.0", "3.0", "4.0", "5.0"]], header=1), pv(1, 280, 100))
    assert soa.extract_soa(d) == []
    idx = structure.build_section_index(d)
    soas = soa.extract_soa(d, idx)
    assert len(soas) == 1 and soas[0]["table_id"] == "#/tables/0", soas
    assert soas[0]["section_id"] == idx.section_of("#/tables/0") is not None


# ------------------------------------------------------------------ primitives

def test_marker_and_footnote_primitives() -> None:
    sm = soa.split_markers
    assert sm("X^b,c") == ("X", ["b", "c"])
    assert sm("X d,k") == ("X", ["d", "k"]) and sm("Xa") == ("X", ["a"])
    assert sm("Height^l, weight") == ("Height, weight", ["l"])
    assert sm("10^9/L") == ("10^9/L", []) and sm("m^2") == ("m^2", [])
    assert sm("Screening a") == ("Screening a", [])
    assert sm("Screening a", frozenset("ab")) == ("Screening", ["a"])
    assert sm("Atezolizumab j Sample ID", frozenset("j")) == ("Atezolizumab Sample ID", ["j"])
    assert sm("Cycle 1 Day 1*") == ("Cycle 1 Day 1", ["*"]) and sm("Phase £ (until PD)") == ("Phase (until PD)", ["£"])
    assert sm("End of Treatment Section 5.6^") == ("End of Treatment Section 5.6", ["^"])
    ms = soa.mark_split
    assert ms("X") == (True, None) and ms("X X") == (True, None) and ms("X------X") == (True, None)
    assert ms("⇓=====") == (True, None) and ms("-") == (False, None) and ms("--") == (False, None)
    assert ms("X (if necessary)") == (True, "if necessary") and ms("Cycle 1-3") == (False, "Cycle 1-3")
    assert ms("• Notes: all visits within the window") == (False, "• Notes: all visits within the window")
    pf = soa.parse_footnotes
    assert pf("a. First note") == [("a", "First note")] and pf("a.First note") == [("a", "First note")]
    assert pf("a First note") == [("a", "First note")] and pf("^a first note") == [("a", "first note")]
    assert pf("a) First") == [("a", "First")] and pf("a: ≤ 4 weeks") == [("a", "≤ 4 weeks")]
    assert pf("*Every cycle") == [("*", "Every cycle")] and pf("X^b = Performed by phone") == [("b", "Performed by phone")]
    assert pf("^ End of treatment visit") == [("^", "End of treatment visit")]
    assert pf("a. First note here. b. Second note here.") == [("a", "First note here."), ("b", "Second note here.")]
    assert pf("a patient may withdraw") == [] and pf("Abbreviations: AE = adverse event") == []
    assert pf("c evaluation not applicable", loose=True) == [("c", "evaluation not applicable")]
    assert soa.window_parts("D1 (±1)") == (["±1"], "D1")
    assert soa.window_parts("Within 28±2 days after last dose") == (["±2 days"], "Within 28 days after last dose")


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
