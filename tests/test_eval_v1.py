"""Self-test for eval_v1.py on tiny synthetic fixtures (made-up text, no network):
`python tests/test_eval_v1.py`."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval_v1 as ev
from eval_v1 import (ChunkUnits, Ctx, DocEnv, Retrieval, Unit, abstained, answer_metrics, bootstrap_ci,
                     compare_records, compact, evaluate_doc, group_scores, match_level, mcnemar_p, normalize,
                     numeric_exact, prep, ranked_units, retrieval_metrics, summarize, unverified_numbers,
                     validate_gold)


def lvl(quote: str, text: str) -> str | None:
    return match_level(prep(quote), prep(text))


def question(qid: str, evidence: list, answerable: bool = True, facts: list | None = None, **extra) -> dict:
    q = {"id": qid, "doc": "SYN", "split": "dev", "usdm": {"class": "EligibilityCriterion", "attributes": ["text"]},
         "category": "eligibility", "kind": "lookup" if answerable else "unanswerable", "modality": ["text"],
         "question": f"synthetic question {qid}?", "answerable": answerable,
         "evidence": [{"all_of": [{"quote": x, "page": 1, "section": "1"} for x in g]} for g in evidence],
         "answer": {"facts": facts or [], "complete_list": False, "forbidden": []}, "notes": ""}
    q.update(extra)
    return q


def fact(fid: str, text: str | None, critical: bool = False, value=None, unit=None, comparator=None, alts=None):
    return {"id": fid, "text": text, "alts": alts or [], "critical": critical, "value": value, "unit": unit,
            "comparator": comparator}


# --------------------------------------------------------------------------- normalisation

def test_normalisation_superscripts() -> None:
    converted = "b. Zorblets ≥30 × 10 9 /L (support allowed if completed 5 days before dosing); and"
    assert lvl("Zorblets ≥30 × 109/L", converted) == "nospace"  # PDF layer: flattened superscript
    assert lvl("Zorblets ≥30 × 10^9/L", converted) == "nospace"  # caret notation
    assert lvl("Zorblets ≥30 × 10⁹/L", converted) == "nospace"  # real superscript (NFKC)
    assert lvl("zorblets ≥30 x 10 9 /l", converted) == "exact"  # case, × vs x
    assert lvl("Zorblets ≥80 × 109/L", converted) is None  # a different number never matches


def test_normalisation_hyphens_spaces_dashes() -> None:
    # PDF text layer keeps the line-end hyphen and a line break; the converted text joined the word.
    assert lvl("refractory to one flumo-\r\nmodulatory agent", "refractory to one flumomodulatory agent") == "nospace"
    assert lvl("participants must have a gribble", "participantsmust have a gribble") == "nospace"  # lost space
    assert lvl("Days 1–7 of each cycle", "Days 1-7 of each cycle") == "exact"  # en dash
    assert lvl("Days 1 — 7", "days 1 - 7") == "exact"  # em dash
    assert lvl("Snorkel’s syndrome", "Snorkel's syndrome") == "exact"  # curly apostrophe
    assert lvl("“primed” dose", '"primed" dose') == "exact"
    assert lvl("score ≧2", "score ≥2") == "exact" and lvl("score >=2", "score ≥2") == "exact"
    assert lvl("score ⩽ 2", "score ≤ 2") == "exact"
    assert lvl("Wibble  Performance\nStatus", "the wibble performance status is") == "exact"
    assert normalize("A B­C") == "a bc" and compact("x - y z") == "xyz"


def test_fuzzy_fallback() -> None:
    text = "Hospitalisation is required for at least 3 days (~72 hr) after the first priming dose."
    assert lvl("Hospitalization is required for at least 3 days (~72 hr) after the first", text) == "fuzzy"
    assert match_level(prep("Hospitalization is required for at least 3 days"), prep(text), fuzzy=False) is None
    # numbers must match even when the words do
    assert lvl("Hospitalisation is required for at least 9 days (~72 hr) after the first", text) is None
    # short quotes are never fuzzy-matched
    assert lvl("zorp ≥40%", "zorp ≥45%") is None
    # the same words scattered far apart do not make a fuzzy match
    pad = " filler words here" * 10 + " "
    far = pad.join(["hospitalisation", "required", "dose", "priming", "first"])
    assert lvl("first priming dose required hospitalisation", far) is None


# --------------------------------------------------------------------------- evidence scoring

def test_all_of_vs_alternatives() -> None:
    # alternative 0 needs quotes 0 AND 1; alternative 1 is quote 2 alone
    groups = [[0, 1], [2]]
    ranks = [1, 3, None]
    assert group_scores(groups, ranks, 1) == (True, 0.5, False)
    assert group_scores(groups, ranks, 3) == (True, 1.0, True)
    assert group_scores(groups, [None, 2, 4], 3) == (True, 0.5, False)
    assert group_scores(groups, [None, None, 4], 5) == (True, 1.0, True)  # the alternative suffices
    texts = ["the gribble threshold is 4 units", "unrelated text", "and the wibble limit is 9 units",
             "a duplicate: gribble threshold is 4 units and wibble limit is 9 units"]
    q = question("q1", [["gribble threshold is 4 units", "wibble limit is 9 units"]])
    groups, quotes = ev.flatten(q)
    m, first = retrieval_metrics("s2_chunk", groups, quotes, texts)
    assert first == 1 and m["s2_chunk_mrr"] == 1.0
    assert m["s2_chunk_hit@1"] and m["s2_chunk_recall@1"] == 0.5 and not m["s2_chunk_group@1"]
    assert m["s2_chunk_group@3"] and m["s2_chunk_recall@3"] == 1.0
    assert ev.group_in_texts(groups, quotes, texts[3:])  # both quotes inside one text
    assert not ev.group_in_texts(groups, quotes, texts[:1])


def test_ranked_units_dedup() -> None:
    class TableUnits:
        name = "fake"

        def unit(self, chunk):
            if chunk["chunk_id"] in ("t1", "t2"):  # two parts of one table -> the whole table
                return Unit(frozenset({"#/tables/0"}), "WHOLE TABLE: row a | row b")
            return Unit(frozenset({chunk["chunk_id"]}), chunk["text"])

    ranked = [{"chunk_id": "t2", "text": "row b"}, {"chunk_id": "x", "text": "text x"},
              {"chunk_id": "t1", "text": "row a"}, {"chunk_id": "y", "text": "text y"}]
    assert ranked_units(ranked, TableUnits()) == ["WHOLE TABLE: row a | row b", "text x", "text y"]
    assert ranked_units(ranked, ChunkUnits()) == ["row b", "text x", "row a", "text y"]


# --------------------------------------------------------------------------- table evidence (cells)

def syn_table() -> ev.TTable:
    """A small schedule: header row, row labels, X cells, a label spanning two rows (rows 3-4)."""
    T = ev.TCell
    zorp = T("Zorp test", 3, 0, row_span=2)
    grid = [
        [T("Activity", 0, 0, header=True), T("Screening", 0, 1, header=True), T("Day 1", 0, 2, header=True),
         T("Day 8", 0, 3, header=True), T("Notes", 0, 4, header=True)],
        [T("Wibble scan", 1, 0), T("X", 1, 1), T("X", 1, 2), T("", 1, 3), T("See Section 8.2", 1, 4)],
        [T("Gribble panel", 2, 0), T("X", 2, 1), T("", 2, 2), T("X", 2, 3), T("See Section 8.3", 2, 4)],
        [zorp, T("X", 3, 1), T("", 3, 2), T("", 3, 3), T("Cycle 1 only", 3, 4)],
        [zorp, T("", 4, 1), T("X", 4, 2), T("X", 4, 3), T("Cycles 2 and later", 4, 4)],
    ]
    return ev.TTable("#/tables/7", grid)


# the same table as chunk rows ("Label: Column = value; ...") and as Markdown
TABLE_ROWS = ("Columns: Activity | Screening | Day 1 | Day 8 | Notes\n"
              "Wibble scan: Screening = X; Day 1 = X; Notes = See Section 8.2\n"
              "Gribble panel: Screening = X; Day 8 = X; Notes = See Section 8.3\n"
              "Zorp test: Screening = X; Notes = Cycle 1 only\n"
              "Zorp test: Day 1 = X; Day 8 = X; Notes = Cycles 2 and later")
TABLE_MD = ("| Activity | Screening | Day 1 | Day 8 | Notes |\n|---|---|---|---|---|\n"
            "| Wibble scan | X | X |  | See Section 8.2 |\n| Gribble panel | X |  | X | See Section 8.3 |\n"
            "| Zorp test | X |  |  | Cycle 1 only |\n| Zorp test |  | X | X | Cycles 2 and later |")


def cells_level(quote: str, text: str, index: ev.TableIndex) -> tuple:
    return ev.quote_ranks(ev.quote_specs([quote], index), [text])[0]


def test_table_cell_matching() -> None:
    index = ev.TableIndex([syn_table()])
    row_quote = "Wibble scan X X See Section 8.2"  # a row as the PDF reads it, label included
    locs, in_table = index.locate(row_quote)
    assert in_table and len(locs) == 1
    loc = locs[0]
    assert loc.table == "#/tables/7" and loc.order == "row"
    assert [(r, c) for r, c, _, _ in loc.cells] == [(1, 0), (1, 1), (1, 2), (1, 4)]  # the empty cell is skipped
    assert loc.records[0][0] == "Wibble scan"
    # not contiguous in either serialization (strict misses), but every cell is on the label's line
    assert cells_level(row_quote, TABLE_ROWS, index) == (None, 1, 1)
    assert cells_level(row_quote, TABLE_MD, index) == (None, 1, 1)
    assert cells_level(row_quote, "Wibble scan X X See Section 8.2", index)[0] == 1  # strict still wins
    # the label is only on the "Rows:" line, the cells are on another row's line -> no
    other_row = ("Rows: Wibble scan, Gribble panel\n"
                 "Gribble panel: Screening = X; Day 1 = X; Notes = See Section 8.2")
    assert cells_level(row_quote, other_row, index)[1] is None
    # cell "X" is a whole cell: the x of "exam" does not count; a missing cell fails
    assert cells_level(row_quote, "Wibble scan: exam on Day 1; See Section 8.2", index)[1] is None
    assert cells_level(row_quote, TABLE_ROWS.replace("Section 8.2", "Section 8.4"), index)[1] is None

    # a quote within a row without its label: the row label is required in the text
    bare = "X X See Section 8.2"
    assert len(index.locate(bare)[0]) == 1
    assert cells_level(bare, "Wibble scan: Screening = X; Day 1 = X; Notes = See Section 8.2", index)[1] == 1
    assert cells_level(bare, "Gribble panel: Screening = X; Day 1 = X; Notes = See Section 8.2", index)[1] is None

    # a label spanning two rows (printed once in the PDF): optional in the run, still the row's anchor
    span_quote = "X X Cycles 2 and later"
    (sl,), _ = index.locate(span_quote)
    assert [(r, c) for r, c, _, _ in sl.cells] == [(4, 2), (4, 3), (4, 4)] and sl.records[0][0] == "Zorp test"
    assert cells_level(span_quote, TABLE_ROWS, index)[1] == 1 and cells_level(span_quote, TABLE_MD, index)[1] == 1
    assert cells_level("Zorp test X Cycle 1 only", TABLE_ROWS, index)[1] == 1

    # column-major reading (down the Screening column): header anywhere, each row's label with its X
    col_quote = "Screening X X X"
    (cl,), _ = index.locate(col_quote)
    assert cl.order == "col" and [lab for lab, _ in cl.records] == ["Wibble scan", "Gribble panel", "Zorp test"]
    assert [f.text for f in cl.free] == ["Screening"]
    assert cells_level(col_quote, TABLE_ROWS, index)[1] == 1 and cells_level(col_quote, TABLE_MD, index)[1] == 1
    no_gribble = "\n".join(ln for ln in TABLE_ROWS.splitlines() if not ln.startswith("Gribble"))
    assert cells_level(col_quote, no_gribble, index)[1] is None  # a touched row is missing

    # cut first and last cells; a single-cell quote needs no cell matching
    cut = "scan X X See Sect"
    (cu,), _ = index.locate(cut)
    assert [f.whole for _, fs in cu.records for f in fs] == [False, True, True, False]
    assert cells_level(cut, TABLE_ROWS, index)[1] == 1
    assert index.locate("See Section 8.3") == ([], True)
    assert index.locate("not in any table at all") == ([], False)
    # ranks: the first text holding the quote at each level
    assert ev.quote_ranks(ev.quote_specs([row_quote], index), ["unrelated", TABLE_MD, row_quote]) == [(3, 2, 2)]


def test_docling_table_adapter() -> None:
    from docling_core.types.doc import DoclingDocument, TableCell, TableData

    def cell(text, r, c, rs=1, header=False):
        return TableCell(text=text, start_row_offset_idx=r, end_row_offset_idx=r + rs, start_col_offset_idx=c,
                         end_col_offset_idx=c + 1, row_span=rs, column_header=header)

    doc = DoclingDocument(name="syn")
    doc.add_table(data=TableData(num_rows=3, num_cols=3, table_cells=[
        cell("Visit", 0, 0, header=True), cell("Day 1", 0, 1, header=True), cell("Day 8", 0, 2, header=True),
        cell("Flurb  count", 1, 0, rs=2), cell("X", 1, 1), cell("X", 1, 2), cell("X", 2, 1), cell("", 2, 2)]))
    (t,) = ev.docling_tables(doc)
    assert t.ref == "#/tables/0" and t.grid[2][0].text == "Flurb count" and t.grid[2][0].row_span == 2
    assert t.header_row(0) and t.label(2) == "Flurb count" and t.label(0) is None
    locs, _ = ev.TableIndex([t]).locate("Flurb count X X")
    assert len(locs) == 1 and ev.loc_in(locs[0], prep("| Flurb count | X | X |"))


def test_table_quotes_end_to_end() -> None:
    """Primary (cells) vs strict levels through evaluate_doc, on chunks in the row format."""
    chunks = [{"chunk_id": "t1", "text": TABLE_ROWS}, {"chunk_id": "x1", "text": "Unrelated text on snorkels."}]
    env = DocEnv(doc="SYN", chunks=chunks, doc_text="Activity Screening Day 1\n...", tables=[syn_table()],
                 pipeline=FakePipeline(chunks), units=ChunkUnits(), assembler=FakeAssembler())
    q = question("t1", [["Wibble scan X X See Section 8.2"]], question="wibble scan schedule?",
                 modality=["table"])
    (r,) = evaluate_doc([q], env, k=1)
    m = r["m"]
    assert m["s0_doc"] and not m["s0_doc_strict"] and r["info"]["quote_levels"] == {"cells": 1}
    assert m["s0_chunks"] and not m["s0_chunks_strict"] and m["s0_chunks_fuzzy"]
    assert m["s1_pool"] and not m["s1_pool_strict"]
    assert m["s2_chunk_recall@1"] == 1.0 and m["s2_chunk_recall@5_strict"] == 0.0
    assert m["s3_sufficient"] and not m["s3_sufficient_strict"] and m["s3_sufficient_fuzzy"]
    assert r["stage_fail"] is None and r["info"]["table_quotes"] == 1
    assert r["info"]["quote_cells"][0]["cells"] == [[1, 0], [1, 1], [1, 2], [1, 4]]
    (rn,) = evaluate_doc([q], env, k=1, numbers_only=True)
    assert "cell_texts" not in rn["info"]["quote_cells"][0] and "wibble" not in json.dumps(rn).lower()


# --------------------------------------------------------------------------- key terms

def test_key_terms_and_or_numbers() -> None:
    t = prep("Participants need platelets ≥ 25 × 10 9 /L and ANC ≥1.0 × 109/L on Day 4 [2].")
    assert ev.key_terms_found([["platelet", "PLT"], ["≥25"], ["10^9/L"]], t)  # AND of OR-groups
    assert ev.key_terms_found([["PLT", "platelets"]], t)  # any alternative of a group
    assert not ev.key_terms_found([["platelet"], ["hemoglobin", "Hb"]], t)  # one group fails -> no
    assert not ev.key_terms_found([], t) and not ev.key_terms_found([[]], t)
    # exponent spellings are one spelling
    for s in ("25 × 10^9/L", "25 x 10 9 /L", "25×109/L", "25 ×10⁹/L", "25 10⁹/L", "25x10**9/L", "25 × 10⁹/L"):
        assert ev.term_in("25 × 10^9/L", prep(s)), s
        assert ev.term_in("10⁹/L", prep(s)) and ev.term_in("×10^9/L", prep(s)), s
    assert not ev.term_in("25 × 10^9/L", prep("250 × 10^9/L"))
    assert not ev.term_in("10^9/L", prep("10^6/L"))
    # numbers on token boundaries
    assert ev.term_in("4", prep("on Day 4 only")) and ev.term_in("4", prep("Days 1-4"))
    for s in ("40 mg", "Day 14", "1.4 mg", "4.5 mg", "x 104"):
        assert not ev.term_in("4", prep(s)), s
    assert ev.term_in("4 mg", prep("4 mg once")) and not ev.term_in("4 mg", prep("24 mg once"))
    assert not ev.term_in("10", prep("10^9/L")) and not ev.term_in("9", prep("10⁹/L"))
    assert ev.term_in("≥1.0", prep("ANC ≧ 1.0")) and ev.term_in("≤2", prep("ECOG <= 2"))
    assert ev.term_in("Grade 3–4", prep("grade 3-4 events")) and ev.term_in("Sponsor’s", prep("sponsor's"))
    # words: start on a boundary; short terms whole words (plural ok)
    assert not ev.term_in("PR", prep("progression")) and ev.term_in("PR", prep("a PR or better"))
    assert ev.term_in("CR", prep("two CRs")) and ev.term_in("hospitali", prep("hospitalization"))


def test_key_terms_facts_and_forbidden() -> None:
    summary_fact = fact("f1", "Platelet threshold: at least 25 × 10^9/L at screening (support allowed)", critical=True)
    summary_fact["key_terms"] = [["platelet"], ["25"], ["10^9/L"]]
    plain = fact("f2", "wibble scan")
    q = question("k1", [["x"]], facts=[summary_fact, plain])
    q["answer"]["forbidden"] = [{"text": "this exact sentence never appears", "why": "dose-mod threshold",
                                 "key_terms": [["50"], ["10^9/L"]]},
                                {"text": "Day 40", "why": "other day"}]
    ans = "Platelets must be ≥25×10⁹/L [1]; a wibble scan is done on Day 4."
    m = answer_metrics(q, ans, context="ctx")
    assert m["s4_fact_recall"] == 1.0 and m["s4_critical_ok"]  # key_terms, not the summary sentence
    assert m["s4_forbidden_hit"] is False  # "50" absent; "Day 40" is not "Day 4"
    bad = answer_metrics(q, "Platelets ≥50 × 10 9 /L; wibble scan.", context="ctx")
    assert bad["s4_forbidden_hit"] is True and bad["s4_fact_recall"] == 0.5  # 25 missing
    assert answer_metrics(q, "Platelets ≥250 × 10^9/L; wibble scan", "ctx")["s4_critical_ok"] is False
    # without key_terms (or with an empty list) the text/alts matching applies
    summary_fact["key_terms"] = []
    assert answer_metrics(q, ans, context="ctx")["s4_fact_recall"] == 0.5
    # S3 uses the same rule (facts in the context)
    summary_fact["key_terms"] = [["platelet"], ["25"], ["10^9/L"]]
    assert ev.fact_metrics("s3", q["answer"]["facts"], "Platelets ≥ 25 × 10 9 /L")["s3_fact_recall"] == 0.5
    # validation of the shape
    assert validate_gold([q])[0] == []
    for badkt in ("platelet", [["ok"], []], [["ok", 3]], [[" "]]):
        q2 = question("k2", [["a long enough quote here"]], facts=[dict(plain, key_terms=badkt)])
        assert any("key_terms" in e for e in validate_gold([q2])[0]), badkt
    q3 = question("k3", [["a long enough quote here"]], facts=[plain])
    q3["answer"]["forbidden"] = [{"key_terms": "50"}]
    assert any("forbidden[0].key_terms" in e for e in validate_gold([q3])[0])
    assert ev.gold_counts([q])["facts"]["with_key_terms"] == 1


# --------------------------------------------------------------------------- answers

def test_answer_metrics() -> None:
    facts = [fact("f1", "Zorblets ≥30 × 10^9/L", critical=True, value="30", unit="10^9/L", comparator="≥"),
             fact("f2", "wibble scan", alts=["WS"]), fact("f3", "PK")]
    q = question("q", [["x"]], facts=facts)
    q["answer"]["forbidden"] = [{"text": "50 × 10^9/L", "why": "other threshold"}]
    ans = "Participants need a zorblet count of at least 30 × 10⁹/L [2] and a WS [1]."
    m = answer_metrics(q, ans, context="Zorblets ≥30 × 10 9 /L ... wibble scan (WS) ...")
    assert m["s4_fact_recall"] == 2 / 3, m  # "PK" absent (and "pk" inside a word would not count)
    assert m["s4_critical_ok"] and m["s4_critical_recall"] == 1.0
    assert m["s4_numeric_exact"] == 1.0 and m["s4_forbidden_hit"] is False
    assert m["s4_false_abstention"] is False and m["s4_unverified_numbers"] == 0
    bad = answer_metrics(q, "A count of 50 × 10^9/L is needed; also spkg.", context="nothing numeric")
    assert bad["s4_forbidden_hit"] and bad["s4_critical_ok"] is False and bad["s4_numeric_exact"] == 0.0
    assert bad["s4_unverified_numbers"] == 2  # 50 and 109 are not in the context
    assert not numeric_exact(facts[0], prep("at least 300 × 10^9/L"))  # 30 != 300
    assert not numeric_exact(facts[0], prep("30 × 10^9/L"))  # comparator missing
    # complete lists: item-level precision
    q2 = question("q2", [["x"]], facts=[fact("a", "wibble"), fact("b", "gribble")])
    q2["answer"]["complete_list"] = True
    m2 = answer_metrics(q2, "Assessments:\n- Wibble test [1]\n- Gribble panel\n- Zonk survey\n", "ctx")
    assert m2["s4_list_precision"] == 2 / 3 and m2["s4_extra_items"] == 1 and m2["s4_fact_recall"] == 1.0


def test_unanswerable_handling() -> None:
    q = question("u1", [], answerable=False)
    assert ev.flatten(q) == ([], [])
    assert abstained("The context does not specify a maximum dose for the zorp cohort.")
    assert abstained("This information is not provided in the context.")
    assert abstained('{"status": "not_found", "claims": []}')
    assert not abstained('{"status": "answered", "claims": [{"text": "x"}]}')
    assert not abstained("The maximum dose is 12 mg [1].")
    assert answer_metrics(q, "The context does not contain this information.", "ctx")["s4_abstain_correct"] is True
    m = answer_metrics(q, "The maximum dose is 12 mg.", "ctx")
    assert m["s4_abstain_correct"] is False and "s4_false_abstention" not in m
    qa = question("a1", [["x"]], facts=[fact("f", "12 mg")])
    assert answer_metrics(qa, "The context does not specify the dose.", "ctx")["s4_false_abstention"] is True
    # an answer that mentions a gap but still gives the fact is not a false abstention
    assert answer_metrics(qa, "Not specified for cohort B; cohort A gets 12 mg.", "ctx")["s4_false_abstention"] is False


def test_unverified_numbers() -> None:
    ctx = "[1] Text | 5.1 | p. 4\nZorblets ≥30 × 10 9 /L; dosing on Days 1-7."
    assert unverified_numbers("Zorblets ≥30 × 10⁹/L on Days 1-7 [1].", ctx) == 0
    assert unverified_numbers("1. Zorblets ≥35 on Day 8 [1, 2]", ctx) == 2  # enumerator and citation ignored


# --------------------------------------------------------------------------- end to end on a fake pipeline

CHUNKS = [
    {"chunk_id": "c1", "text": "Inclusion 4. Zorblets ≥30 × 10 9 /L (support allowed)."},
    {"chunk_id": "c2", "text": "Inclusion 5. Wibble performance status ≤2."},
    {"chunk_id": "c3", "text": "Table 3 part 1: Gribble level 1 = 4 units"},
    {"chunk_id": "c4", "text": "Table 3 part 2: Gribble level 2 = 9 units"},
    {"chunk_id": "c5", "text": "Background on snorkels and other unrelated material."},
]
DOC_TEXT = "\n".join(c["text"] for c in CHUNKS) + "\nA sentence the chunker lost: the flurb window is 3 days."


class FakePipeline:
    """Ranks chunks by shared words with the question; ids listed in `missing` are cache misses."""
    name = "fake"

    def __init__(self, chunks, missing=()):
        self.chunks, self.missing, self.embed_misses = chunks, set(missing), 0

    def prepare(self, questions):
        self.embed_misses = sum(q["id"] in self.missing for q in questions)

    def retrieve(self, q):
        if q["id"] in self.missing:
            return Retrieval("miss:embedding")
        words = set(normalize(q["question"]).split())
        ranked = sorted(self.chunks, key=lambda c: -len(words & set(normalize(c["text"]).split())))
        return Retrieval("ok", pool=ranked[:4], ranked=ranked[:4], payload=ranked)


class TableUnits:
    name = "fake_units"

    def unit(self, chunk):
        if chunk["chunk_id"] in ("c3", "c4"):
            return Unit(frozenset({"table3"}), CHUNKS[2]["text"] + "\n" + CHUNKS[3]["text"])
        return Unit(frozenset({chunk["chunk_id"]}), chunk["text"])


class FakeAssembler:
    name = "fake_ctx"

    def build(self, q, retrieval, k):
        text = "\n\n".join(c["text"] for c in retrieval.ranked[:k])
        return Ctx(text, len(text.split()), {"blocks": k})


class FakeAnswerer:
    name = "fake_answers"

    def __init__(self, answers):
        self.answers = answers

    def get(self, question, context):
        return self.answers.get(question)


def gold_fixture() -> list[dict]:
    return [
        question("e1", [["Zorblets ≥30 × 109/L"]], question="zorblets count needed?",
                 facts=[fact("f1", None, critical=True, value="30", unit="10^9/L", comparator="≥")]),
        question("e2", [["Gribble level 1 = 4 units", "Gribble level 2 = 9 units"]],
                 question="gribble level units table 3 part 1", kind="multi_evidence", modality=["table"]),
        question("e3", [["the flurb window is 3 days"]], question="flurb window?"),  # lost by the chunker
        question("e4", [["Wibble performance status ≤2"]], question="wibble performance status?"),
        question("u1", [], answerable=False, question="what is the snorkel dose?"),
    ]


def fake_env(doc, chunks_path, opts, missing=("e4",)):
    answers = {"zorblets count needed?": "At least 30 × 10⁹/L [1].",
               "what is the snorkel dose?": "The context does not specify a snorkel dose.",
               "gribble level units table 3 part 1": "Level 1 is 4 units and level 2 is 9 units."}
    return DocEnv(doc=doc, chunks=CHUNKS, doc_text=DOC_TEXT, pipeline=FakePipeline(CHUNKS, missing),
                  units=TableUnits(), assembler=FakeAssembler(), answerer=FakeAnswerer(answers))


def fake_env_worse(doc, chunks_path, opts):
    env = fake_env(doc, chunks_path, opts)
    env.answerer = FakeAnswerer({"zorblets count needed?": "At least 50 × 10⁹/L.",
                                 "what is the snorkel dose?": "The snorkel dose is 12 mg."})
    return env


def test_end_to_end_fake_pipeline() -> None:
    gold = gold_fixture()
    assert validate_gold(gold)[0] == []
    recs = {r["id"]: r for r in evaluate_doc(gold, fake_env("SYN", None, None), k=1, answers=True)}
    e1, e2, e3, e4, u1 = (recs[i] for i in ("e1", "e2", "e3", "e4", "u1"))
    assert e1["m"]["s0_doc"] and e1["m"]["s0_chunks"] and e1["m"]["s2_chunk_recall@1"] == 1.0
    assert e1["m"]["s3_sufficient"] and e1["m"]["s4_critical_ok"] and e1["stage_fail"] is None
    # all_of over two table parts: only one part in the top-1 chunk, but the unit (whole table) has both
    assert e2["m"]["s2_chunk_group@1"] is False and e2["m"]["s2_unit_group@1"] is True
    assert e2["m"]["s2_prebudget_group"] is True and e2["m"]["s3_sufficient"] is False
    assert e2["stage_fail"] == "S3_context"  # k=1 context holds only one part
    assert e3["m"]["s0_doc"] is True and e3["m"]["s0_chunks"] is False and e3["stage_fail"] == "S0_chunking"
    assert e4["status"] == "miss:embedding" and e4["stage_fail"] == "cache_miss" and "s2_chunk_hit@1" not in e4["m"]
    assert u1["m"]["s4_abstain_correct"] is True and "s0_doc" not in u1["m"] and u1["stage_fail"] is None
    assert "top_chunks" in e1["info"]
    s = summarize(list(recs.values()), {"run": "t", "split": "dev", "k": 1})
    assert s["misses"]["embedding"] == ["e4"] and s["metrics"]["s0_doc"]["n"] == 4
    assert s["by"]["kind"]["unanswerable"]["n"] == 1 and s["attribution"]["S0_chunking"] == ["e3"]
    num = evaluate_doc(gold, fake_env("SYN", None, None), k=1, answers=True, numbers_only=True)
    assert all("top_chunks" not in r["info"] for r in num)
    assert "zorblet" not in json.dumps(num).lower()  # no question/quote/answer text in records


# --------------------------------------------------------------------------- statistics and gates

def test_bootstrap_determinism() -> None:
    diffs = [0, 0, 1, 0, -1, 0, 1, 1, 0, 0, 0, 1, 0, 0, -1, 0, 0, 1, 0, 0]
    a = bootstrap_ci(diffs, n_boot=10_000, seed=7)
    assert a == bootstrap_ci(diffs, n_boot=10_000, seed=7)
    assert a[0] <= sum(diffs) / len(diffs) <= a[1]
    assert bootstrap_ci([0.0] * 10) == (0.0, 0.0)
    clusters = ["P1"] * 10 + ["P2"] * 10
    c = bootstrap_ci(diffs, clusters, n_boot=2_000, seed=7)
    assert c == bootstrap_ci(diffs, clusters, n_boot=2_000, seed=7)
    assert abs(mcnemar_p(0, 5) - 0.0625) < 1e-12 and mcnemar_p(3, 3) == 1.0 and mcnemar_p(0, 0) == 1.0


def rec(qid: str, **m) -> dict:
    return {"id": qid, "doc": "SYN", "m": m}


def gate(res: dict, start: str) -> str:
    return next(g["result"] for g in res["gates"] if g["gate"].startswith(start))


def test_compare_gates() -> None:
    n = 40
    a = [rec(f"q{i}", **{"s2_chunk_recall@5": 1.0, "s3_sufficient": True, "s3_critical_ok": True})
         for i in range(n)]
    same = compare_records(a, [dict(r, m=dict(r["m"])) for r in a], n_boot=2_000)
    assert gate(same, "evidence recall@5") == "pass" and gate(same, "context sufficiency") == "pass"
    assert gate(same, "no new critical") == "pass" and gate(same, "fact recall") == "n/a"
    # B loses evidence on 6 of 40 questions and a critical fact on q3 -> both gates fail
    b = [rec(f"q{i}", **{"s2_chunk_recall@5": 0.0 if i < 6 else 1.0, "s3_sufficient": i >= 6,
                         "s3_critical_ok": i != 3}) for i in range(n)]
    worse = compare_records(a, b, n_boot=2_000)
    assert gate(worse, "evidence recall@5") == "FAIL" and gate(worse, "context sufficiency") == "FAIL"
    assert gate(worse, "no new critical") == "FAIL"
    row = worse["metrics"]["s3_sufficient"]
    assert row["binary"] and row["newly_failing"] == [f"q{i}" for i in range(6)] and row["mcnemar_p"] < 0.05
    assert "mcnemar_p" not in worse["metrics"]["s2_chunk_recall@5"]
    # with only 40 questions even one lost question fails: the CI admits a drop of more than 2 points
    one = compare_records(a, [rec(f"q{i}", **{"s2_chunk_recall@5": 0.0 if i == 0 else 1.0,
                                              "s3_sufficient": True, "s3_critical_ok": True}) for i in range(n)],
                          n_boot=2_000)
    assert gate(one, "evidence recall@5") == "FAIL" and gate(one, "context sufficiency") == "pass"
    # improvements pass; abstention gates are absolute on run B
    better = compare_records(b, a, n_boot=2_000)
    assert all(g["result"] in ("pass", "n/a") for g in better["gates"])
    ua = [rec(f"u{i}", s4_abstain_correct=True, s4_fact_recall=1.0) for i in range(10)]
    ub = [rec(f"u{i}", s4_abstain_correct=i != 0, s4_fact_recall=1.0) for i in range(10)]
    assert gate(compare_records(ua, ub, n_boot=500), "s4_abstain_correct") == "pass"  # 9/10 = the 90% floor
    ub2 =[rec(f"u{i}", s4_abstain_correct=i > 1, s4_fact_recall=1.0) for i in range(10)]
    assert gate(compare_records(ua, ub2, n_boot=500), "s4_abstain_correct") == "FAIL"


def test_validate() -> None:
    good = question("g1", [["a long enough quote here"]])
    errs, warns = validate_gold([good, dict(good)])
    assert any("duplicate id" in e for e in errs)
    bad = question("b1", [["quote"]], answerable=False)  # unanswerable with evidence
    bad["split"] = "train"
    del bad["usdm"]
    errs, warns = validate_gold([bad])
    assert any("unanswerable question with evidence" in e for e in errs)
    assert any("split" in e for e in errs) and any("usdm" in e for e in errs)
    assert any("short quote" in w for w in warns)
    assert validate_gold({"not": "a list"})[0]
    counts = ev.gold_counts(gold_fixture())
    assert counts["kind"]["unanswerable"] == 1 and counts["evidence"]["all_of_groups"] == 1


def test_cli_run_and_compare() -> None:
    """run (twice, via --env-factory) and compare through main(), into a temporary runs folder."""
    tmp = Path(tempfile.mkdtemp())
    old_runs = ev.RUNS
    try:
        ev.RUNS = tmp / "runs"
        gold = tmp / "gold.json"
        gold.write_text(json.dumps(gold_fixture()), encoding="utf-8")
        assert ev.main(["validate", "--gold", str(gold)]) == 0
        assert ev.main(["run", "--gold", str(gold), "--k", "1", "--answers", "--name", "A", "--chunks", "x",
                        "--env-factory", "test_eval_v1:fake_env"]) == 0
        assert ev.main(["run", "--gold", str(gold), "--k", "1", "--answers", "--name", "B", "--chunks", "x",
                        "--numbers-only", "--env-factory", "test_eval_v1:fake_env_worse"]) == 0
        assert ev.main(["run", "--gold", str(gold), "--plan", "--answers", "--chunks", "x",
                        "--env-factory", "test_eval_v1:fake_env"]) == 0
        lines = (ev.RUNS / "A" / "results.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5 and json.loads((ev.RUNS / "A" / "summary.json").read_text())["questions"] == 5
        assert "gold" not in json.loads((ev.RUNS / "B" / "summary.json").read_text())  # numbers-only: no path
        rc = ev.main(["compare", str(ev.RUNS / "A"), str(ev.RUNS / "B"), "--n-boot", "500"])
        assert rc == 1  # B: wrong critical number and no abstention -> gates fail
        report = json.loads((ev.RUNS / "B" / "compare_vs_A.json").read_text())
        assert report["verdict"] == "FAIL"
        assert report["metrics"]["s4_critical_ok"]["newly_failing"] == ["e1"]
    finally:
        ev.RUNS = old_runs
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- rescore

TABLE_CHUNK = {"chunk_id": "c6", "text": TABLE_ROWS}


def fake_env_tables(doc, chunks_path, opts):
    """fake_env plus a table (chunk c6, Docling grid) and a fixed set of answers."""
    env = fake_env(doc, chunks_path, opts, missing=())
    env.chunks = CHUNKS + [TABLE_CHUNK]
    env.pipeline = FakePipeline(env.chunks)
    env.tables = [syn_table()]
    env.answerer.answers["wibble scan: screening schedule?"] = "Wibble scan: Screening and Day 1 [1]."
    return env


def gold_tables() -> list[dict]:
    g = gold_fixture()
    g.append(question("t1", [["Wibble scan X X See Section 8.2"]], question="wibble scan: screening schedule?",
                      modality=["table"], facts=[fact("f1", "The wibble scan is done at Screening and on Day 1",
                                                      critical=True)]))
    return g


def test_rescore_determinism() -> None:
    tmp = Path(tempfile.mkdtemp())
    old_runs = ev.RUNS
    try:
        ev.RUNS = tmp / "runs"
        gold = tmp / "gold.json"
        gold.write_text(json.dumps(gold_tables()), encoding="utf-8")
        assert ev.main(["run", "--gold", str(gold), "--k", "1", "--answers", "--name", "A", "--chunks", "x",
                        "--split", "all", "--env-factory", "test_eval_v1:fake_env_tables"]) == 0
        assert ev.main(["rescore", "A"]) == 0  # -> A_rescored, same gold and scorer
        assert ev.main(["rescore", "A", "--name", "A_again"]) == 0

        def recs(name):
            return [json.loads(x) for x in (ev.RUNS / name / "results.jsonl").read_text(encoding="utf-8").splitlines()]

        a, r1, r2 = recs("A"), recs("A_rescored"), recs("A_again")
        assert a == r1 == r2  # nothing changed -> identical records, in the same order
        s1 = json.loads((ev.RUNS / "A_rescored" / "summary.json").read_text(encoding="utf-8"))
        assert s1["rescored_from"] == "A" and s1["metrics"] == json.loads(
            (ev.RUNS / "A" / "summary.json").read_text(encoding="utf-8"))["metrics"]
        assert s1["args"]["env_factory"] == "test_eval_v1:fake_env_tables" and s1["k"] == 1
        t1 = next(r for r in r1 if r["id"] == "t1")
        assert t1["m"]["s3_sufficient"] and not t1["m"]["s3_sufficient_strict"]
        assert t1["m"]["s4_fact_recall"] == 0.0  # the summary sentence is not in the answer
        # a gold change (key_terms for the summary fact) needs no re-run: rescore with the new gold
        g = gold_tables()
        g[-1]["answer"]["facts"][0]["key_terms"] = [["wibble scan"], ["screening"], ["day 1"]]
        gold2 = tmp / "gold2.json"
        gold2.write_text(json.dumps(g), encoding="utf-8")
        assert ev.main(["rescore", "A", "--gold", str(gold2), "--name", "A_kt"]) == 0
        kt = {r["id"]: r for r in recs("A_kt")}
        assert kt["t1"]["m"]["s4_fact_recall"] == 1.0 and kt["t1"]["stage_fail"] is None
        assert {i: r["m"] for i, r in kt.items() if i != "t1"} == {r["id"]: r["m"] for r in a if r["id"] != "t1"}
        # questions the gold no longer has are dropped
        gold3 = tmp / "gold3.json"
        gold3.write_text(json.dumps(g[:-1]), encoding="utf-8")
        assert ev.main(["rescore", "A", "--gold", str(gold3), "--name", "A_less"]) == 0
        assert [r["id"] for r in recs("A_less")] == [r["id"] for r in a if r["id"] != "t1"]
        assert ev.main(["rescore", "no_such_run"]) == 2
    finally:
        ev.RUNS = old_runs
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- LLM judge (fake client only)

class FakeJudgeClient:
    """Stands in for OpenRouter: records request bodies, answers with canned contents in turn."""

    def __init__(self, contents):
        self.contents = list(contents)
        self.bodies = []

    def complete(self, body):
        self.bodies.append(body)
        content = self.contents[min(len(self.bodies), len(self.contents)) - 1]
        return {"choices": [{"message": {"content": content}}], "provider": "FakeProvider"}


def verdict_json(*pairs, extra=()):
    return json.dumps({"facts": [{"id": i, "verdict": v} for i, v in pairs], "extra_claims": list(extra)})


class offline_unset:
    """Temporarily clear OPENROUTER_OFFLINE (the client is fake: nothing leaves the process)."""

    def __enter__(self):
        self.old = os.environ.pop("OPENROUTER_OFFLINE", None)

    def __exit__(self, *exc):
        if self.old is not None:
            os.environ["OPENROUTER_OFFLINE"] = self.old


def test_judge_fake_client() -> None:
    tmp = Path(tempfile.mkdtemp())
    try:
        cache = tmp / "judge_cache.json"
        q = question("j1", [["x"]], facts=[fact("f1", "Zorblets ≥30 × 10^9/L", critical=True),
                                           fact("f2", "wibble scan", alts=["WS"])])
        ans = "At least 30 × 10⁹/L zorblets; no scan is needed; dosing is weekly."
        client = FakeJudgeClient([verdict_json(("f1", "supported"), ("f2", "contradicted"),
                                               extra=["dosing is weekly"])])
        with offline_unset():
            j = ev.Judge(ev.JUDGE_MODEL, cache, allow_api=True, client=client)
            status, res = j.judge(q, ans)
        assert status == "ok" and res["verdicts"] == ["supported", "contradicted"]
        assert res["extra_claims"] == ["dosing is weekly"] and len(client.bodies) == 1
        body = client.bodies[0]
        assert body["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
        assert body["temperature"] == 0 and body["model"] == ev.JUDGE_MODEL
        assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"]
        user = json.loads(body["messages"][1]["content"])
        assert [f["id"] for f in user["reference_facts"]] == ["f1", "f2"] and user["answer"] == ans
        assert "temperature" not in ev.Judge("openai/gpt-5.2", cache, client=client).body(q, ans)
        # cached on disk: a new judge object answers from the cache without calling the client
        client2 = FakeJudgeClient([verdict_json(("f1", "missing"), ("f2", "missing"))])
        with offline_unset():
            j2 = ev.Judge(ev.JUDGE_MODEL, cache, allow_api=True, client=client2)
            assert j2.judge(q, ans) == ("ok", res) and client2.bodies == []
            # the key covers the answer, the facts, the model and the prompt version
            assert j2.judge(q, ans + " Also X.")[0] == "ok" and len(client2.bodies) == 1
            q_changed = json.loads(json.dumps(q))
            q_changed["answer"]["facts"][1]["text"] = "gribble scan"
            assert j2.key(q_changed, ans) != j2.key(q, ans)
            assert ev.Judge("other/model", cache).key(q, ans) != j2.key(q, ans)
        # no API without --allow-api, and never under OPENROUTER_OFFLINE=1
        client3 = FakeJudgeClient([verdict_json(("f1", "supported"), ("f2", "supported"))])
        assert ev.Judge("m", tmp / "c3.json", allow_api=False, client=client3).judge(q, ans) == ("miss", None)
        os.environ["OPENROUTER_OFFLINE"], old = "1", os.environ.get("OPENROUTER_OFFLINE")
        try:
            assert ev.Judge("m", tmp / "c3.json", allow_api=True, client=client3).judge(q, ans) == ("miss", None)
        finally:
            if old is None:
                del os.environ["OPENROUTER_OFFLINE"]
            else:
                os.environ["OPENROUTER_OFFLINE"] = old
        assert client3.bodies == [] and not (tmp / "c3.json").exists()
        # strict JSON: invalid replies are retried, then an error (never cached)
        bad = FakeJudgeClient(["not json", verdict_json(("f1", "supported")),
                               verdict_json(("f1", "supported"), ("f2", "maybe"))])
        with offline_unset():
            jb = ev.Judge("m", tmp / "c4.json", allow_api=True, client=bad)
            assert jb.judge(q, ans) == ("error:invalid_json", None) and len(bad.bodies) == ev.JUDGE_ATTEMPTS
        assert jb.cache == {}
        assert ev.parse_judge("```json\n" + verdict_json(("a", "missing")) + "\n```", ["a"])["verdicts"] == ["missing"]
        assert ev.parse_judge(json.dumps({"facts": [], "extra_claims": [], "note": 1}), []) is None
        assert ev.parse_judge(verdict_json(("a", "supported"), ("a", "missing")), ["a"]) is None
        # metrics and agreement
        m = ev.judge_metrics(q["answer"]["facts"], ["supported", "contradicted"], ["x"], [True, True])
        assert m["s4j_fact_recall"] == 0.5 and m["s4j_critical_ok"] and m["s4j_contradicted"]
        assert m["s4j_extra_claims"] == 1 and m["s4j_agreement"] == 0.5
        assert abs(ev.cohen_kappa([(True, True), (True, False), (False, False), (False, False)]) - 0.5) < 1e-12
        assert ev.cohen_kappa([(True, True)] * 3) is None and ev.cohen_kappa([]) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_judge_in_run() -> None:
    """evaluate_doc + summarize with a judge on the fake pipeline; the CLI flag without API = misses."""
    tmp = Path(tempfile.mkdtemp())
    old_runs = ev.RUNS
    try:
        gold = gold_fixture()
        client = FakeJudgeClient([verdict_json(("f1", "supported")),
                                  verdict_json(("f1", "missing"))])
        with offline_unset():
            judge = ev.Judge(ev.JUDGE_MODEL, tmp / "jc.json", allow_api=True, client=client)
            recs = evaluate_doc(gold, fake_env("SYN", None, None, missing=()), k=1, answers=True, judge=judge)
        by = {r["id"]: r for r in recs}
        assert by["e1"]["judge_status"] == "ok" and by["e1"]["m"]["s4j_fact_recall"] == 1.0
        assert by["e1"]["info"]["fact_found"] == [True] and by["e1"]["info"]["judge_verdicts"] == ["supported"]
        assert by["u1"]["judge_status"] == "skipped"  # no facts to judge
        s = summarize(recs, {"run": "t", "split": "dev", "k": 1})
        ja = s["judge_agreement"]
        assert ja["facts"] == 1 and ja["agreement"] == 1.0 and ja["status"]["ok"] == 1
        assert "table" in ja["by_modality"] or "text" in ja["by_modality"]
        # through the CLI: offline -> the cache is used, misses are listed, nothing is called
        ev.RUNS = tmp / "runs"
        gpath = tmp / "gold.json"
        gpath.write_text(json.dumps(gold), encoding="utf-8")
        assert ev.main(["run", "--gold", str(gpath), "--k", "1", "--answers", "--name", "J", "--chunks", "x",
                        "--env-factory", "test_eval_v1:fake_env", "--judge", "--judge-cache",
                        str(tmp / "jc.json"), "--allow-api"]) == 0
        sj = json.loads((ev.RUNS / "J" / "summary.json").read_text(encoding="utf-8"))
        assert sj["judge"] == ev.JUDGE_MODEL and sj["judge_api_calls"] == 0
        assert sj["metrics"]["s4j_fact_recall"]["n"] == 1  # e1 from the cache; e2 has no facts
        assert ev.main(["rescore", "J", "--judge-cache", str(tmp / "jc.json")]) == 0
        sr = json.loads((ev.RUNS / "J_rescored" / "summary.json").read_text(encoding="utf-8"))
        assert sr["judge"] == ev.JUDGE_MODEL and sr["metrics"]["s4j_fact_recall"] == sj["metrics"]["s4j_fact_recall"]
    finally:
        ev.RUNS = old_runs
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
