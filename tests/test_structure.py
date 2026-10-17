"""Self-test for structure.py on small synthetic DoclingDocuments.

Run: `python tests/test_structure.py`. Covers: stable section ids, front matter "S0", minor headings
kept inline, roles from heading synonyms and from content cues, scheme-aware numbering (Roman,
"Section N", letters), TOC parsing and level-1 consensus, and that assign_heading_levels equals
protocol_postprocess.fix_heading_levels whenever the PDF outline is used; children() of a fresh index
used from several threads at once.
"""

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size,
                                    TableCell, TableData)

import protocol_postprocess as pp
import structure as S
from protocol_settings import ProtocolSettings

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_postprocess import FakePdf  # noqa: E402  (outline-only stand-in for pypdfium2.PdfDocument)


def pv(page: int = 1, t: float = 700, b: float = 690, l: float = 72, r: float = 540) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.BOTTOMLEFT),
                          charspan=(0, 1))


class Builder:
    """Builds a document from (kind, text, level) events, one item per 30 points, 20 items per page."""

    def __init__(self, pages: int = 6):
        self.d = DoclingDocument(name="t")
        for p in range(1, pages + 1):
            self.d.add_page(page_no=p, size=Size(width=612, height=792))
        self.n = 0

    def _pv(self):
        page, k = 1 + self.n // 20, self.n % 20
        self.n += 1
        return pv(page, t=740 - 30 * k, b=730 - 30 * k)

    def h(self, text: str, level: int = 1):
        return self.d.add_heading(text=text, level=level, prov=self._pv())

    def t(self, text: str):
        return self.d.add_text(label=DocItemLabel.TEXT, text=text, prov=self._pv())

    def lst(self, *items: str):
        g = self.d.add_list_group()
        for x in items:
            self.d.add_list_item(text=x, parent=g, prov=self._pv())
        return g

    def table(self, rows: list[list[str]], label=DocItemLabel.TABLE, caption: str | None = None):
        cells = [TableCell(text=x, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                           end_col_offset_idx=c + 1, column_header=r == 0)
                 for r, row in enumerate(rows) for c, x in enumerate(row)]
        tab = self.d.add_table(data=TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells),
                               prov=self._pv(), label=label)
        if caption:
            cap = self.d.add_text(label=DocItemLabel.CAPTION, text=caption, prov=self._pv(), parent=tab)
            tab.captions.append(cap.get_ref())
        return tab


def protocol(extra_before_intro: bool = False) -> DoclingDocument:
    b = Builder()
    b.t("Sponsor: ACME Pharma")
    b.h("TITLE PAGE")
    b.t("Protocol ACME-001")
    if extra_before_intro:
        b.h("Protocol Amendment Summary of Changes")
        b.t("Amendment 2 changes the dose.")
    b.h("1. INTRODUCTION", 1)
    b.t("Background text.")
    b.h("1.1. Background", 2)
    b.h("Disease Overview", 3)
    b.t("The disease.")
    b.h("2. STUDY POPULATION", 1)
    b.h("2.1. Inclusion Criteria", 2)
    b.h("Age and Sex:", 3)
    b.lst("Participants aged 18 years or older.", "Any sex.")
    b.h("a. Female participants:", 3)
    b.t("Not pregnant.")
    b.h("2.2. Exclusion Criteria", 2)
    b.h("Medical Conditions:", 3)
    b.lst("Active infection.")
    b.h("Other Exclusions:", 3)
    b.lst("Prior participation.")
    b.h("3. STATISTICAL CONSIDERATIONS", 1)
    b.h("3.1. Sample Size Determination", 2)
    b.t("120 participants.")
    b.h("3.2. Safety Analyses", 2)
    b.t("Descriptive.")
    return b.d


# ---------------------------------------------------------------- ids, S0, coverage

def test_ids_stable_and_front_matter() -> None:
    d = protocol()
    a, b = S.build_section_index(d), S.build_section_index(protocol())
    assert [s.section_id for s in a.sections] == [s.section_id for s in b.sections]
    ids = [s.section_id for s in a.sections]
    assert ids[0] == "S0" and {"S1", "S1.1", "S2", "S2.1", "S2.2", "S3", "S3.1", "S3.2"} <= set(ids), ids
    unnumbered = [s for s in a.sections if s.section_id.startswith("S~")]
    assert all(len(s.section_id.split("-")[0]) == 10 and s.section_id.endswith("-1") for s in unnumbered), unnumbered
    # front matter: the sponsor line before the first heading
    first_text = next(t for t in d.texts if t.text.startswith("Sponsor"))
    assert a.section_of(first_text.self_ref) == "S0" and a.by_id["S0"].level == 0
    # adding a section elsewhere keeps the other ids
    c = S.build_section_index(protocol(extra_before_intro=True))
    assert {s.section_id for s in a.sections} <= {s.section_id for s in c.sections}
    # every body item maps to exactly one section; item_refs are disjoint
    seen = [r for s in a.sections for r in s.item_refs]
    assert len(seen) == len(set(seen))
    for it, _ in d.iterate_items(with_groups=True):
        if it.self_ref != "#/body":
            assert a.section_of(it.self_ref), it.self_ref
    # S0 exists even without front-matter items
    b2 = Builder()
    b2.h("1. INTRODUCTION")
    b2.t("x")
    idx = S.build_section_index(b2.d)
    assert idx.sections[0].section_id == "S0" and idx.sections[0].item_refs == []


def test_tree_api_and_json_round_trip() -> None:
    d = protocol()
    idx = S.build_section_index(d)
    assert [s.section_id for s in idx.children("S2")] == ["S2.1", "S2.2"]
    assert [s.section_id for s in idx.path("S2.1")] == ["S2", "S2.1"]
    assert [s.section_id for s in idx.children(None)][:2] != []  # top level
    assert idx.by_id["S2.1"].parent_id == "S2" and idx.by_id["S2.1"].level == 2
    again = S.SectionIndex.from_json(json.dumps(idx.to_json()))
    assert again.to_json() == idx.to_json()
    li = next(t for t in d.texts if t.text.startswith("Participants aged"))
    assert again.section_of(li.self_ref) == "S2.1" == idx.section_of(li.self_ref)
    assert idx.section_of(li.parent.cref) == "S2.1"  # the list group too
    assert idx.section_of("#/texts/99999") is None


def test_children_from_threads_on_a_fresh_index() -> None:
    # children() builds its parent -> children map on first use: threads that call it on a fresh index at the same
    # time must all get complete results (never a map another thread is still filling)
    sections = [S.Section("S0", None, "Front matter", 0, None, None)]
    want: dict[str | None, list[str]] = {None: [], "S0": []}
    for i in range(1, 41):
        sections.append(S.Section(f"S{i}", str(i), f"{i}. Chapter", 1, None, None))
        want[None].append(f"S{i}")
        want[f"S{i}"] = [f"S{i}.{j}" for j in range(1, 6)]
        for j in range(1, 6):
            sections.append(S.Section(f"S{i}.{j}", f"{i}.{j}", f"{i}.{j}. Part", 2, f"S{i}", None))
            want[f"S{i}.{j}"] = []
    ids = list(want)[::-1]  # the last-filled entries first
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # switch threads as often as possible, also while the map is being built
    try:
        for trial in range(30):
            idx = S.SectionIndex(list(sections))
            start = threading.Barrier(8)
            got: list[dict | None] = [None] * 8

            def work(t: int, idx=idx, start=start, got=got) -> None:
                start.wait()
                got[t] = {sid: [s.section_id for s in idx.children(sid)] for sid in ids}

            threads = [threading.Thread(target=work, args=(t,)) for t in range(8)]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            for t, g in enumerate(got):
                assert g == want, (trial, t, None if g is None else [k for k in ids if g[k] != want[k]][:3])
    finally:
        sys.setswitchinterval(interval)


def test_table_and_picture_children_map_to_section() -> None:
    b = Builder()
    b.h("1. SCHEDULE OF ACTIVITIES")
    tab = b.table([["Procedure", "V1"], ["ECG", "X"]], caption="Table 1. SoA")
    pic = b.d.add_picture(prov=b._pv())
    inner = b.d.add_text(label=DocItemLabel.TEXT, text="Text in figure: Screening", parent=pic, prov=pv())
    idx = S.build_section_index(b.d)
    sid = idx.by_id["S1"].section_id
    assert idx.section_of(tab.self_ref) == sid and idx.section_of(tab.captions[0].cref) == sid
    assert idx.section_of(pic.self_ref) == sid and idx.section_of(inner.self_ref) == sid
    assert idx.by_id["S1"].item_refs == [tab.self_ref, pic.self_ref]


# ---------------------------------------------------------------- minor headings

def test_minor_headings_inline() -> None:
    d = protocol()
    idx = S.build_section_index(d)
    inc = idx.by_id["S2.1"]
    texts = {t.self_ref: t.text for t in d.texts}
    assert [texts[r] for r in inc.minor_headings] == ["Age and Sex:", "a. Female participants:"]
    assert not idx.children("S2.1")  # no tree nodes for labels
    exc = idx.by_id["S2.2"]
    assert [texts[r] for r in exc.minor_headings] == ["Medical Conditions:", "Other Exclusions:"]
    assert all(r in inc.item_refs for r in inc.minor_headings)  # inline, in reading order


def test_label_runs_and_repeated_labels() -> None:
    b = Builder()
    b.h("1. APPENDICES", 1)
    b.h("1.1. CRS Management", 2)
    b.t("Overview.")
    b.h("Grade 1 CRS:", 3)
    b.h("Fever", 3)
    b.t("Antipyretics.")
    b.h("Grade 2 CRS:", 3)
    b.h("Hypotension", 3)
    b.t("Fluids.")
    b.h("Hypoxia", 3)
    b.t("Oxygen.")
    b.h("Grade 3 CRS:", 3)
    b.h("Hypotension", 3)
    b.t("Vasopressor.")
    b.h("Hypoxia", 3)
    b.t("High-flow oxygen.")
    b.h("1.2. ICANS", 2)
    b.t("x")
    idx = S.build_section_index(b.d)
    crs = idx.by_id["S1.1"]
    assert not idx.children("S1.1"), [s.title for s in idx.children("S1.1")]
    assert len(crs.minor_headings) == 8 and len(crs.item_refs) == 14  # every grade keeps its context
    assert [s.section_id for s in idx.children("S1")] == ["S1.1", "S1.2"]


def test_criteria_label_with_role_is_a_node_once() -> None:
    b = Builder()
    b.h("3 Selection of Patients", 1)
    b.h("3.1 Eligibility Criteria", 2)
    b.h("Inclusion Criteria:", 3)
    b.lst("Age >= 18.")
    b.h("Exclusion Criteria:", 3)
    b.lst("Pregnancy.")
    idx = S.build_section_index(b.d)
    kids = idx.children("S3.1")
    assert [(s.title, s.role) for s in kids] == [("Inclusion Criteria:", "inclusion"),
                                                 ("Exclusion Criteria:", "exclusion")], kids
    assert idx.by_id["S3"].role == "population" and idx.by_id["S3.1"].role == "eligibility"


def test_colon_label_never_outranks_numbered_heading() -> None:
    # Labels printed like chapter titles (same level as the numbered heading above them) nest under it.
    b = Builder()
    b.h("1. Protocol Synopsis", 1)
    b.h("Title of Study:", 1)
    b.t("A Phase 3 study.")
    b.h("Objectives/Endpoints:", 1)
    b.t("Primary: treatment success.")
    b.h("2. Exclusion Criteria", 1)
    b.lst("Pregnancy.")
    b.h("Other Exclusions:", 1)
    b.lst("Prior participation.")
    b.h("3. Statistical Considerations", 1)
    b.t("120 participants.")
    idx = S.build_section_index(b.d)
    obj = next(s for s in idx.sections if s.title == "Objectives/Endpoints:")
    assert obj.parent_id == "S1" and obj.role == "synopsis", (obj.parent_id, obj.role)
    texts = {t.self_ref: t.text for t in b.d.texts}
    assert "Other Exclusions:" in [texts[r] for r in idx.by_id["S2"].minor_headings]
    assert [s.section_id for s in idx.children(None)] == ["S1", "S2", "S3"], idx.children(None)
    # the post-processing step writes the same nesting into the item levels (for the chunker's paths)
    log: dict = {}
    assert S.nest_colon_labels(b.d, log) == 2
    lv = {t.text: t.level for t in b.d.texts if t.label == DocItemLabel.SECTION_HEADER}
    assert lv["Objectives/Endpoints:"] == 2 and lv["Other Exclusions:"] == 2 and lv["Title of Study:"] == 1
    assert all(set(e) == {"item", "page", "level"} for e in log["colon_labels_nested"])
    assert S.build_section_index(b.d).to_json() == idx.to_json() and S.nest_colon_labels(b.d) == 0


def test_quoted_headings_in_revision_history_inline() -> None:
    # A list of changes quotes the protocol's section titles; they are not sections of the protocol.
    b = Builder()
    b.h("Revision History", 1)
    b.h("Amendment 2", 2)
    b.t("The following changes were made.")
    b.h("5.2.1 Study Drug Administration", 2)
    b.t("Old text replaced by new text.")
    b.h("3.1 Eligibility Criteria", 3)
    b.t("Criterion 4 removed.")
    b.h("1. Introduction", 1)
    b.t("Background.")
    b.h("1.1 Rationale", 2)
    b.h("2. Objectives", 1)
    idx = S.build_section_index(b.d)
    titles = [s.title for s in idx.sections]
    assert "5.2.1 Study Drug Administration" not in titles and "3.1 Eligibility Criteria" not in titles, titles
    history = next(s for s in idx.sections if s.title == "Revision History")
    assert any(s.title == "Amendment 2" for s in idx.subtree(history.section_id))  # an unnumbered heading stays
    texts = {t.self_ref: t.text for t in b.d.texts}
    inline = [texts[r] for s in idx.subtree(history.section_id) for r in s.minor_headings]
    assert inline == ["5.2.1 Study Drug Administration", "3.1 Eligibility Criteria"], inline
    removed = next(t for t in b.d.texts if t.text == "Criterion 4 removed.")
    assert idx.section_of(removed.self_ref) in {s.section_id for s in idx.subtree(history.section_id)}
    assert {"S1", "S1.1", "S2"} <= set(idx.by_id) and idx.by_id["S1"].role == "introduction"


# ---------------------------------------------------------------- reading-order repair

def test_reading_order_repair() -> None:
    d = DoclingDocument(name="t")
    for p in range(1, 7):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    text = lambda s, page, t: d.add_text(label=DocItemLabel.TEXT, text=s, prov=pv(page, t, t - 10))  # noqa: E731
    head = lambda s, page, t, lv=1: d.add_heading(text=s, level=lv, prov=pv(page, t, t - 10))  # noqa: E731
    head("1. Inclusion Criteria", 1, 740)
    text("Criterion 1.", 1, 700)
    # p2: the end of a list read before "2.", although it lies below "2." and its first text -> a unit move
    cont = text("Exclusion 2 continued.", 2, 400)
    h2 = head("2. Exclusion Criteria", 2, 740)
    t2 = text("Exclusion 1.", 2, 700)
    # p3: an unnumbered section below "3." and its text, read before "3." -> moves as a block
    blk = head("Study Design Overview", 3, 500)
    bt = text("Overview text.", 3, 470)
    h3 = head("3. Synopsis", 3, 740)
    s3t = text("Synopsis text.", 3, 700)
    # p4: a centred chapter heading read after its first sub-section -> the heading moves up
    h41 = head("4.1 Analysis", 4, 500, 2)
    t41 = text("Analysis text.", 4, 470)
    h4 = head("4. Methods", 4, 740)
    # p5-6: a minor heading that would climb out of 5.1 again stays where it is
    head("5. Results", 5, 740)
    text("Results text.", 5, 700)
    note = head("Note:", 6, 400, 2)
    head("5.1 Tables", 6, 740, 2)
    text("Tables text.", 6, 700)

    order_before = [r.cref for r in d.body.children]
    assert set(S.reading_order_moves(d)) == {cont.self_ref, note.self_ref}  # protocol_api's (units only)
    log: dict = {}
    assert S.repair_reading_order(d, log) == 3
    order = [r.cref for r in d.body.children]
    assert sorted(order) == sorted(order_before)  # nothing added or lost
    pos = {r: i for i, r in enumerate(order)}
    assert pos[h2.self_ref] < pos[t2.self_ref] < pos[cont.self_ref]
    assert pos[h3.self_ref] < pos[s3t.self_ref] < pos[blk.self_ref] < pos[bt.self_ref]
    assert pos[h4.self_ref] < pos[h41.self_ref] < pos[t41.self_ref]
    assert order.index(note.self_ref) == order_before.index(note.self_ref)  # undone
    idx = S.build_section_index(d)
    assert idx.section_of(cont.self_ref) == "S2" and idx.by_id["S4.1"].parent_id == "S4"
    rank = [s.heading_ref for s in idx.sections]
    assert rank.index(h3.self_ref) < rank.index(blk.self_ref)
    acts = sorted((e["kind"], e["action"], e.get("reason"), e["units"]) for e in log["reading_order"])
    assert acts == [("heading", "moved", None, 2), ("section", "moved", None, 2), ("unit", "moved", None, 1),
                    ("unit", "skipped", "not_in_target_after_move", 1)], acts
    assert all(set(e) <= {"item", "page", "kind", "from_section", "to_section", "units", "action", "reason"}
               for e in log["reading_order"])
    assert set(S.reading_order_moves(d, sections=True)) == {note.self_ref}  # only the undone one is left
    assert S.repair_reading_order(d, {}) == 0  # idempotent


# ---------------------------------------------------------------- roles

def test_roles_from_heading_synonyms() -> None:
    cases = {
        "Schedule of Assessments": "soa", "Time and Events Schedule": "soa", "Trial Population": "population",
        "Selection of Patients": "population", "Statistical Methods": "statistics",
        "Sample Size Justification": "sample_size", "Glossary of Terms": "abbreviations",
        "Revision History": "amendment_history", "Document History": "amendment_history",
        "Objectives and Endpoints": "objectives_endpoints", "Trial Objectives and Associated Estimands":
            "objectives_endpoints", "Study Intervention(s) Administered": "interventions",
        "Prohibited Medications": "concomitant_therapy", "Discontinuation of Study Treatment": "discontinuation",
        "Adverse Events and Serious Adverse Events": "ae_reporting", "Pharmacokinetics": "pk_pd_biomarkers",
        "Bibliography": "references", "List of Abbreviations": "abbreviations", "Interim Analyses": "interim_analysis",
        "Investigator's Agreement": "title_page", "Lifestyle Restrictions": "lifestyle", "Screen Failures": "screen_failures",
    }
    for title, role in cases.items():
        for text in (title, f"7.2 {title}", f"IV. {title.upper()}", f"Appendix 3: {title}"):
            got = S.heading_role(text)
            assert got[0] == role and got[1] >= S.ROLE_MIN, (text, got)
    assert S.heading_role("Renal Impairment") == (None, 0.0)
    assert S.heading_role("Inclusion of Prior Therapy in the Design")[1] < S.ROLE_MIN  # a word, not the section


def test_roles_inheritance_and_constraints() -> None:
    idx = S.build_section_index(protocol())
    r = {s.section_id: (s.role, s.role_score) for s in idx.sections}
    assert r["S1"] == ("introduction", 1.0)
    disease = next(s for s in idx.sections if s.title == "Disease Overview")
    assert disease.role == "introduction" and 0 < disease.role_score < 1  # inherited
    assert r["S2.1"][0] == "inclusion" and r["S2.2"][0] == "exclusion" and r["S2"][0] == "population"
    assert r["S3.1"][0] == "sample_size"
    assert r["S3.2"][0] == "statistics"  # "Safety Analyses" stays statistical inside statistics
    assert idx.sections[0].role == "title_page" and idx.by_id[next(
        s.section_id for s in idx.sections if s.title == "TITLE PAGE")].role == "title_page"
    # a synopsis keeps its subsections (summary copies) as synopsis, but not its SoA
    b = Builder()
    b.h("1. PROTOCOL SUMMARY")
    b.h("1.1. Synopsis", 2)
    b.h("Objectives and Endpoints", 3)
    b.t("Primary: ORR.")
    b.h("1.2. Schedule of Activities", 2)
    b.t("See table.")
    i2 = S.build_section_index(b.d)
    obj = next(s for s in i2.sections if s.title == "Objectives and Endpoints")
    assert obj.role == "synopsis" and i2.by_id["S1.2"].role == "soa"


def test_roles_from_content_cues() -> None:
    b = Builder()
    b.h("Document Overview")
    b.table([["Document", "Version", "Date"], ["Original protocol", "1.0", "12 March 2021"],
             ["Amendment 1", "2.0", "4 June 2022"]])
    b.h("TABLE OF CONTENTS")
    b.table([["1. INTRODUCTION ........ 5"], ["2. METHODS ........ 7"]], label=DocItemLabel.DOCUMENT_INDEX)
    b.h("1. INTRODUCTION")
    b.t("x")
    b.h("2. STUDY PROCEDURES")
    b.h("2.1. Visit Plan", 2)
    rows = [["Procedure", "Screening", "Day 1", "Week 2", "Week 4", "EOT"]]
    for p in ("Informed consent", "ECG", "Vital signs", "Hematology", "PK sample"):
        rows.append([p, "X", "X", "", "X", "X"])
    b.table(rows)
    b.h("3. APPENDIX TERMS")
    b.table([["Abbreviation", "Definition"], ["AE", "adverse event"], ["ECG", "electrocardiogram"],
             ["PK", "pharmacokinetics"], ["SAE", "serious adverse event"], ["BMI", "body mass index"],
             ["ICF", "informed consent form"]])
    idx = S.build_section_index(b.d)
    role = {s.title: s.role for s in idx.sections}
    assert role["Document Overview"] == "amendment_history", role
    assert role["TABLE OF CONTENTS"] == "toc"
    assert role["2.1. Visit Plan"] == "soa", role
    assert role["3. APPENDIX TERMS"] == "abbreviations", role
    assert S.table_cues(b.d.tables[2]) == {"soa"}


def test_amendment_summary_quotes_stay_in_history() -> None:
    b = Builder()
    b.h("Revision History", 1)
    b.h("Brief Protocol Synopsis - Patient Population", 1)
    b.h("Was:", 1)
    b.t("old text")
    b.h("3.1 Eligibility Criteria - Inclusion", 2)
    b.t("changed")
    b.h("1 Introduction", 1)
    b.t("body")
    b.h("2 Objectives", 1)
    b.h("3 Selection of Patients", 1)
    b.h("3.1 Eligibility Criteria", 2)
    b.t("criteria")
    idx = S.build_section_index(b.d)
    quoted = [s for s in idx.sections if s.title.startswith(("Brief Protocol", "3.1 Eligibility Criteria -"))]
    assert all(s.role == "amendment_history" for s in quoted), [(s.title, s.role) for s in quoted]
    # the longest successor chain numbers the body, not the quoted "3.1"
    assert idx.by_id["S3.1"].title == "3.1 Eligibility Criteria" and idx.by_id["S3.1"].role == "eligibility"
    assert idx.by_id["S1"].role == "introduction"


# ---------------------------------------------------------------- numbering schemes

def test_parse_label() -> None:
    cases = {"4.2.1 Design": ("arabic", (4, 2, 1)), "1.0 INTRODUCTION": ("arabic", (1,)),
             "IV. STUDY DESIGN": ("roman", (4,)), "Section 4: Study Design": ("section", (4,)),
             "SECTION IV - DESIGN": ("section", (4,)), "B. Overall Design": ("alpha", (2,)),
             "Appendix A Abbreviations": ("appendix", ()), "Protocol Attachment LZZT.1 Schedule": ("appendix", ()),
             "a. Female participants:": ("list", ()), "(b) Other": ("list", ()), "Study Design": None}
    for text, want in cases.items():
        lb = S.parse_label(text)
        got = None if lb is None else (lb.kind, lb.values)
        assert got == want, (text, got)
    assert S.parse_label("Protocol Attachment LZZT.1 Schedule").key == "LZZT.1"
    assert S._rest("10.2 Appendix 2: Clinical Laboratory Tests") == "Clinical Laboratory Tests"


def _levels(titles: list[str], entries=None, styles=None) -> list[int]:
    return S.multi_signal_levels(titles, [1 + i // 4 for i in range(len(titles))], entries or [], styles, None,
                                 ProtocolSettings(heading_source="numbering"), {})


def test_roman_and_section_word_normalised() -> None:
    gt = [1, 2, 3, 2, 1, 2]
    roman = ["I. INTRODUCTION", "1.1 Background", "1.1.1 Disease", "1.2 Rationale", "II. DESIGN", "2.1 Overall"]
    word = ["Section 1: INTRODUCTION", "1.1 Background", "1.1.1 Disease", "1.2 Rationale", "Section 2: DESIGN",
            "2.1 Overall"]
    assert _levels(roman) == gt and _levels(word) == gt
    # today's rule flattens the Roman level 1 under the previous section
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    for t in roman:
        d.add_heading(text=t, level=1, prov=pv())
    pp.fix_heading_levels(d, None, ProtocolSettings(heading_source="numbering"))
    assert [h.level for h in d.texts] != gt
    # contextual template: I. / A. / 1.
    ctx = ["I. INTRODUCTION", "A. Background", "1. Disease", "2. Burden", "B. Rationale", "II. DESIGN", "A. Overall"]
    assert _levels(ctx) == [1, 2, 3, 3, 2, 1, 2], _levels(ctx)
    # section ids from Roman numbers on a levelled document
    d2 = DoclingDocument(name="t")
    d2.add_page(page_no=1, size=Size(width=612, height=792))
    for t, lv in zip(roman, gt):
        d2.add_heading(text=t, level=lv, prov=pv())
    assert list(S.section_numbers(d2).values()) == ["1", "1.1", "1.1.1", "1.2", "2", "2.1"]


def test_level_offset_numbers() -> None:
    d = DoclingDocument(name="t")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    for t, lv in [("PROTOCOL ACME-1", 1), ("1 INTRODUCTION", 2), ("1.1 Background", 3), ("2 DESIGN", 2)]:
        d.add_heading(text=t, level=lv, prov=pv())
        d.add_text(label=DocItemLabel.TEXT, text="x", prov=pv())
    nums, k = S.section_numbers(d, return_offset=True)
    assert k == 1 and list(nums.values()) == ["1", "1.1", "2"]
    idx = S.build_section_index(d)
    assert idx.by_id["S1"].parent_id is None and idx.by_id["S1.1"].parent_id == "S1"


# ---------------------------------------------------------------- TOC and consensus

def test_toc_parse_wrapped_and_appendix_lines() -> None:
    lines = [("Table of Contents", 250, 2), ("Section Page", 500, 2),
             ("1. Introduction......................5", 72, 2),
             ("4.2. Demographics and Patient Characteristics Measured at", 90, 2),
             ("Baseline ..........41", 100, 2),
             ("Protocol Attachment LZZT.1.", 72, 3), ("Schedule of Events", 72, 3),
             ("Table 3. Lab tests ........ 33", 72, 3), ("2. Objectives 7", 72, 3)]
    es = S._parse_toc_lines(lines)
    got = [(e.text, e.page) for e in es]
    assert got == [("1. Introduction", 5), ("4.2. Demographics and Patient Characteristics Measured at Baseline", 41),
                   ("Protocol Attachment LZZT.1. Schedule of Events", None), ("2. Objectives", 7)], got
    assert es[2].label.kind == "appendix" and es[1].x == 90


def test_level1_consensus_rejects_list_number() -> None:
    # "3. Orientation" inside an appendix follows "2. OBJECTIVES" numerically, but the TOC lists only
    # 1-2 and the level-1 headings are centred while it is not.
    titles = ["1. INTRODUCTION", "1.1 Background", "2. OBJECTIVES", "2.1 Primary", "Appendix A Rating Scale",
              "Instructions", "3. Orientation", "4. Word Recall", "Appendix B Survey"]
    toc = S._parse_toc_lines([("1. INTRODUCTION ....... 1", 72, 1), ("1.1 Background ....... 1", 90, 1),
                              ("2. OBJECTIVES ....... 1", 72, 1), ("2.1 Primary ....... 2", 90, 1),
                              ("Appendix A Rating Scale ....... 2", 72, 1), ("Appendix B Survey ....... 3", 72, 1)])
    l1, l2 = ("arial", True, False, 14.0, True, True), ("arial", True, False, 12.0, False, False)
    styles = [l1, l2, l1, l2, l1, l2, l2, l2, l1]
    log: dict = {}
    lv = S.multi_signal_levels(titles, [1, 1, 1, 2, 2, 2, 3, 3, 3], toc, styles, None,
                               ProtocolSettings(heading_source="numbering"), log)
    assert lv == [1, 2, 1, 2, 1, 2, 2, 2, 1], lv
    assert log["heading_sources"]["level1_rejected"] == 2  # "3." and "4."
    assert any(d["reason"] == "level1_unconfirmed" for d in log["heading_disagreements"])
    # without a TOC the single numbering source stands (today's behaviour)
    assert S.multi_signal_levels(titles, [1] * 9, [], styles, None, ProtocolSettings(heading_source="numbering"),
                                 {})[6] == 1


def test_toc_rescues_out_of_sequence_heading() -> None:
    # the layout read "3.1" before its parent "3." (same page): numbering alone demotes "3."
    titles = ["1. A", "2. B", "3.1 C1", "3. C", "3.2 C2", "4. D"]
    pages = [1, 2, 3, 3, 4, 5]
    toc = S._parse_toc_lines([(f"{t} ........ {p}", 72 if "." not in t.split()[0][:-1] else 90, 1)
                              for t, p in [("1. A", 1), ("2. B", 2), ("3. C", 3), ("3.1 C1", 3), ("3.2 C2", 4),
                                           ("4. D", 5)]])
    s = ProtocolSettings(heading_source="numbering")
    assert S.multi_signal_levels(titles, pages, [], None, None, s, {})[3] != 1
    log: dict = {}
    assert S.multi_signal_levels(titles, pages, toc, None, None, s, log) == [1, 1, 2, 1, 2, 1]
    assert log["heading_sources"]["toc_rescued"] == 2  # "3." and then "3.2" (3.1 came first)


def test_unnumbered_template_levels_from_indented_toc() -> None:
    titles = ["INTRODUCTION", "Background", "Disease", "DESIGN", "Overall Design"]
    toc = S._parse_toc_lines([("INTRODUCTION ....... 1", 72, 1), ("Background ....... 1", 90, 1),
                              ("Disease ....... 1", 108, 1), ("DESIGN ....... 2", 72, 1),
                              ("Overall Design ....... 2", 90, 1)])
    lv = S.multi_signal_levels(titles, [1, 1, 1, 2, 2], toc, None, None, ProtocolSettings(heading_source="numbering"),
                               {})
    assert lv == [1, 2, 3, 1, 2], lv


# ---------------------------------------------------------------- equality with today's outline path

def _heading_doc(titles: list[str]) -> DoclingDocument:
    d = DoclingDocument(name="t")
    for p in range(1, 4):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    for i, t in enumerate(titles):
        d.add_heading(text=t, level=1, prov=pv(page=1 + i // 5, t=700 - 50 * (i % 5), b=690 - 50 * (i % 5)))
    return d


def test_outline_results_equal_fix_heading_levels() -> None:
    titles = ["ACME-1", "1 INTRODUCTION", "1.1 Background", "Study Design Overview", "Details", "2 METHODS"]
    toc = [(0, "/", 1), (1, "ACME-1", 1), (0, "1 INTRODUCTION", 1), (1, "1.1 Background", 1),
           (1, "Study Design Overview", 1), (0, "2 METHODS", 2), (1, "Table 1. Labs", 2)]
    for settings in (ProtocolSettings(), ProtocolSettings(heading_source="outline")):
        a, b = _heading_doc(titles), _heading_doc(titles)
        la, lb = {}, {}
        pp.fix_heading_levels(a, FakePdf(toc=toc), settings, la)
        n = S.assign_heading_levels(b, FakePdf(toc=toc), settings, lb)
        assert [h.level for h in a.texts] == [h.level for h in b.texts] == [1, 1, 2, 2, 3, 1]
        assert n == sum(h.level != 1 for h in b.texts) and lb["heading_source"] == "outline" == la["heading_source"]
        assert lb["outline"] == la["outline"]
    # ordinary Arabic documents without outline or TOC: the same levels as today's numbering rule
    for titles in (["21 CFR Part 11 Compliance", "1. INTRODUCTION", "1.1. Background", "2. OBJECTIVES"],
                   ["1.0 INTRODUCTION", "1.1 Background", "2.0 OBJECTIVES", "2.1 Primary"],
                   ["1. INTRODUCTION", "2. DESIGN", "3. APPENDICES", "3.1. Appendix 1", "3.1.1. Japan",
                    "2. HBV Monitoring", "3.2. Appendix 2"],
                   ["1. STATISTICS", "2. APPENDICES", "Appendix A Abbreviations", "Lab Tests", "Appendix B Labs"]):
        a, b = _heading_doc(titles), _heading_doc(titles)
        pp.fix_heading_levels(a, None, ProtocolSettings())
        S.assign_heading_levels(b, None, ProtocolSettings())
        assert [h.level for h in a.texts] == [h.level for h in b.texts], titles
        assert pp.section_numbers(a) == S.section_numbers(b)


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
