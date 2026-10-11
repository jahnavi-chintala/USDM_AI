"""Self-test for glossary.py on small synthetic documents: `python tests/test_glossary.py`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_core.types.doc import (BoundingBox, DocItemLabel, DoclingDocument, ProvenanceItem,
                                    TableCell, TableData)

from glossary import Entry, Glossary, acronym_match, build_glossary, parse_line, schwartz_hearst


def table(doc: DoclingDocument, rows: list[list[str]], header_rows: int = 0, page: int = 1, top: float = 100):
    cells = [TableCell(text=t, start_row_offset_idx=r, end_row_offset_idx=r + 1,
                       start_col_offset_idx=c, end_col_offset_idx=c + 1, column_header=r < header_rows,
                       bbox=BoundingBox(l=10 + 100 * c, t=top + 15 * r, r=90 + 100 * c, b=top + 15 * r + 10))
             for r, row in enumerate(rows) for c, t in enumerate(row)]
    prov = ProvenanceItem(page_no=page, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 0))
    return doc.add_table(data=TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells), prov=prov)


ABBR = [["AE", "adverse event"], ["ICANS", "immune effector cell-associated neurotoxicity syndrome"],
        ["mAb", "monoclonal antibody"], ["β-hCG", "beta-human chorionic gonadotropin"],
        ["BID", "twice a day"], ["CAR-T", "chimeric antigen receptor T-cell therapy"],
        ["LVEF", "left ventricular ejection fraction"]]


def test_acronym_match() -> None:
    assert acronym_match("ICANS", "Immune effector Cell-Associated Neurotoxicity Syndrome")
    assert acronym_match("IgG", "immunoglobulin G")
    assert acronym_match("AEs", "adverse events")
    assert acronym_match("CO2", "carbon dioxide")
    assert acronym_match("β-hCG", "beta-human chorionic gonadotropin")
    assert acronym_match("CTCAE", "Common Terminology Criteria for Adverse Events")
    assert not acronym_match("BID", "twice a day")
    assert not acronym_match("Grade 1", "Mild; asymptomatic")
    assert not acronym_match("100 mg", "Dose to be administered subcutaneously")


def test_two_column_flagged_header_and_split() -> None:
    doc = DoclingDocument(name="t")
    doc.add_heading("Appendix 9: Supporting Information")  # no "abbreviation" hint
    table(doc, [["Abbreviation", "Term"], *ABBR[:4]], header_rows=1, page=10)
    table(doc, [["Abbreviation", "Term"], *ABBR[4:]], header_rows=0, page=11)  # continuation, header unflagged
    g = build_glossary(doc)
    assert g.definitions() == dict(ABBR), g.definitions()
    assert g.entries["AE"].page == 10 and g.entries["LVEF"].page == 11
    assert all(e.source == "table" for e in g.entries.values()) and len(g.table_refs) == 2


def test_no_flags_first_row_is_data() -> None:
    doc = DoclingDocument(name="t")
    doc.add_heading("List of Abbreviations")
    table(doc, ABBR)  # no header row at all: the first row must be kept
    assert build_glossary(doc).definitions() == dict(ABBR)


def test_three_column_and_swapped() -> None:
    doc = DoclingDocument(name="t")
    doc.add_heading("Glossary of Terms")
    rows = [["Abbreviation", "Term", "Definition"],
            ["CRS", "cytokine release syndrome", "A systemic inflammatory response"],
            ["DLT", "dose-limiting toxicity", "Toxicity that prevents dose escalation"],
            ["ORR", "objective response rate", "Proportion with PR or better"]]
    table(doc, rows, header_rows=1)
    g = build_glossary(doc)
    assert g.entries["CRS"].definition == "cytokine release syndrome"
    assert g.entries["CRS"].note == "A systemic inflammatory response"
    doc = DoclingDocument(name="t")
    doc.add_heading("Section 12")
    table(doc, [["Term", "Abbreviation"], ["pharmacokinetics", "PK"], ["overall survival", "OS"],
                ["progression-free survival", "PFS"], ["serious adverse event", "SAE"]], header_rows=1)
    assert build_glossary(doc).definitions() == {"PK": "pharmacokinetics", "OS": "overall survival",
                                                 "PFS": "progression-free survival", "SAE": "serious adverse event"}


def test_data_tables_rejected() -> None:
    doc = DoclingDocument(name="t")
    doc.add_heading("6.6 Dose Modifications")
    table(doc, [["Dose", "Remarks"], ["100 mg", "Starting dose"], ["50 mg", "First reduction"],
                ["25 mg", "Second reduction"]], header_rows=1)
    table(doc, [["Grade", "Description"], ["1", "MILD adverse event"], ["2", "MODERATE adverse event"],
                ["3", "SEVERE adverse event"], ["4", "LIFE-THREATENING consequences"]], header_rows=1)
    table(doc, [["Stage", "ISS"], ["I", "Serum beta-2 microglobulin <3.5 mg/L"], ["II", "Not stage I or III"],
                ["III", "Serum beta-2 microglobulin ≥5.5 mg/L"]], header_rows=1)
    table(doc, [["Objectives", "Endpoints"], ["Primary", "Primary"], ["To assess ORR", "ORR by BICR"],
                ["Secondary", "Secondary"], ["To assess safety", "AEs and laboratory abnormalities"]], header_rows=1)
    table(doc, [["Parameter", "Value"], ["ECOG PS", "0 or 1"], ["LVEF", "≥40%"], ["ANC", "≥1.0 × 10⁹/L"],
                ["Platelets", "≥25 × 10⁹/L"]], header_rows=1)
    g = build_glossary(doc)
    assert not g.table_refs and not g.entries, g.definitions()


def test_text_line_list() -> None:
    doc = DoclingDocument(name="t")
    doc.add_heading("Acronyms")
    for line in ["AE: adverse event", "CRS – cytokine release syndrome", "PK\tpharmacokinetics",
                 "BID twice a day"]:
        doc.add_text(label=DocItemLabel.TEXT, text=line)
    lst = doc.add_list_group()
    doc.add_list_item("ECOG  Eastern Cooperative Oncology Group", parent=lst)
    g = build_glossary(doc)
    assert g.definitions() == {"AE": "adverse event", "CRS": "cytokine release syndrome",
                               "PK": "pharmacokinetics", "BID": "twice a day",
                               "ECOG": "Eastern Cooperative Oncology Group"}, g.definitions()
    assert {e.source for e in g.entries.values()} == {"text_list"}
    # Without a heading the same lines are found by letter matching (BID is then skipped).
    doc = DoclingDocument(name="t")
    doc.add_heading("Appendix 3")
    for line in ["AE adverse event", "CRS cytokine release syndrome", "DLT dose-limiting toxicity",
                 "ORR objective response rate", "OS overall survival", "BID twice a day"]:
        doc.add_text(label=DocItemLabel.TEXT, text=line)
    assert set(build_glossary(doc).definitions()) == {"AE", "CRS", "DLT", "ORR", "OS"}
    assert parse_line("Note: participants must fast") is None


def test_inline_and_merge() -> None:
    assert schwartz_hearst("Patients with cytokine release syndrome (CRS) were treated.") == \
        [("CRS", "cytokine release syndrome")]
    assert schwartz_hearst("Monitor ICANS (immune effector cell-associated neurotoxicity syndrome) daily.") == \
        [("ICANS", "immune effector cell-associated neurotoxicity syndrome")]
    assert schwartz_hearst("Assessed at baseline (Day 1), per protocol (eg, weekly).") == []
    doc = DoclingDocument(name="t")
    doc.add_heading("1. Introduction")
    doc.add_text(label=DocItemLabel.TEXT, text="Left ventricular ejection fraction (LVEF) is measured by "
                 "echocardiogram (ECHO). Adverse events (AEs) are graded per CTCAE (Common Terminology "
                 "Criteria for Adverse Events). Visits are weekly (ie, every 7 days) in Cohort A (CA).")
    doc.add_heading("Abbreviations")
    table(doc, [["Abbreviation", "Term"], ["AE", "adverse event"], ["LVEF", "left ventricular ejection function"],
                ["CRS", "cytokine release syndrome"]], header_rows=1)
    g = build_glossary(doc)
    assert g.entries["LVEF"].source == "table" and g.entries["LVEF"].definition.endswith("function")
    assert g.entries["LVEF"].alternatives[0]["definition"] == "Left ventricular ejection fraction"
    assert not g.entries["AE"].alternatives  # "Adverse events" is the same definition (plural)
    assert g.entries["ECHO"].source == "inline" and g.entries["ECHO"].definition == "echocardiogram"
    assert g.entries["CTCAE"].definition == "Common Terminology Criteria for Adverse Events"
    assert "ie" not in g.entries and "AEs" not in g.entries


def test_find_in_text() -> None:
    g = Glossary({a: Entry(a, d, "table") for a, d in [
        ("AE", "adverse event"), ("C", "Cycle"), ("OR", "objective response"), ("US", "United States"),
        ("ISS", "International Staging System"), ("R-ISS", "Revised ISS"), ("CAR", "chimeric antigen receptor"),
        ("CAR-T", "CAR T-cell therapy"), ("PK", "pharmacokinetic(s)"), ("mAb", "monoclonal antibody")]})
    found = g.find_in_text("Report AEs and R-ISS stage; prior CAR-T or a mAb. PK on C1D1. Grade C. us or ae")
    assert found == {"AE": "adverse event", "R-ISS": "Revised ISS", "CAR-T": "CAR T-cell therapy",
                     "mAb": "monoclonal antibody", "PK": "pharmacokinetic(s)"}, found
    assert g.find_in_text("AND/OR WHERE REQUIRED") == {}
    assert g.find_in_text("CONTACT US TODAY") == {}
    assert g.find_in_text("best OR rate in the US") == {"OR": "objective response", "US": "United States"}
    assert g.find_in_text("staged by ISS") == {"ISS": "International Staging System"}


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
