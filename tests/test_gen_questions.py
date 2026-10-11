"""Self-test for gen_questions.py on a small made-up protocol: `python tests/test_gen_questions.py`.

No network: the HTTP function is replaced by a fake client, and OPENROUTER_OFFLINE=1 is
checked to refuse a real call. The document and PDF text are invented ("Zorvex" study).
"""

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem,
                                    Size, TableCell, TableData)

import gen_questions as gq

os.environ["OPENROUTER_API_KEY"] = "test-key-not-real"  # never read the real key from .env in tests
REAL_POST = gq.post_with_retry


# --------------------------------------------------------------------------- fixtures

def pv(page: int, t: float = 700) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=72, t=t, r=540, b=t - 10,
                                                         coord_origin=CoordOrigin.BOTTOMLEFT), charspan=(0, 1))


def table(doc: DoclingDocument, rows: list[list[str]], page: int, label=DocItemLabel.TABLE):
    cells = [TableCell(text=x, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                       end_col_offset_idx=c + 1, column_header=r == 0)
             for r, row in enumerate(rows) for c, x in enumerate(row)]
    return doc.add_table(data=TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells),
                         prov=pv(page), label=label)


def make_doc() -> DoclingDocument:
    d = DoclingDocument(name="zorvex")
    for p in (1, 2, 3):
        d.add_page(page_no=p, size=Size(width=612, height=792))
    d.add_text(DocItemLabel.TEXT, "Zorvex Study Protocol ZX-4417", prov=pv(1))
    d.add_heading("TABLE OF CONTENTS", level=1, prov=pv(1))
    table(d, [["Section", "Page"], ["1. Study Design", "1"], ["2. Eligibility", "2"]], 1,
          label=DocItemLabel.DOCUMENT_INDEX)
    d.add_heading("1. Study Design", level=1, prov=pv(1))
    d.add_text(DocItemLabel.TEXT, "The Zorvex trial enrolls approximately 120 participants across two cohorts.",
               prov=pv(1))
    group = d.add_list_group()
    d.add_list_item("Cohort Alpha receives 40 mg zorvexumab once weekly.", marker="a.", parent=group, prov=pv(1))
    d.add_list_item("Cohort Beta receives 80 mg zorvexumab every two weeks.", marker="b.", parent=group,
                    prov=pv(1))
    d.add_heading("2. Eligibility", level=1, prov=pv(2))
    d.add_heading("2.1. Inclusion Criteria", level=2, prov=pv(2))
    group = d.add_list_group()
    d.add_list_item("Age of at least 21 years at screening.", marker="1.", parent=group, prov=pv(2))
    d.add_list_item("Measurable quuxoma disease per local criteria.", marker="2.", parent=group, prov=pv(2))
    d.add_text(DocItemLabel.TEXT, "All participants must provide written consent before any study procedure.",
               prov=pv(2))
    table(d, [["Dose Level", "Zorvexumab dose"], ["Level 1", "40 mg"], ["Level -1", "20 mg"]], 2)
    d.add_heading("3. Schedule", level=1, prov=pv(3))
    d.add_text(DocItemLabel.TEXT, "Screening visits occur within 28 days before the first dose of zorvexumab.",
               prov=pv(3))
    return d


# What the PDF text layer holds: same words, other line breaks, a hyphenated line end on page 2.
PDF_PAGES = [
    ["Zorvex Study Protocol ZX-4417", "TABLE OF CONTENTS", "1. Study Design ........ 1",
     "2. Eligibility ........ 2", "1. Study Design", "The Zorvex trial enrolls approximately 120",
     "participants across two cohorts.", "a. Cohort Alpha receives 40 mg zorvexumab once weekly.",
     "b. Cohort Beta receives 80 mg zorvexumab every two weeks."],
    ["2. Eligibility", "2.1. Inclusion Criteria", "1. Age of at least 21 years at screening.",
     "2. Measurable quuxoma disease per local criteria.", "All partici-",
     "pants must provide written consent before any study procedure.", "Dose Level Zorvexumab dose",
     "Level 1 40 mg", "Level -1 20 mg"],
    ["3. Schedule", "Screening visits occur within 28 days before the first dose of zorvexumab."],
]


def write_pdf(path: Path, pages: list[list[str]]) -> None:
    """A minimal valid PDF (Helvetica, one text line per entry) that pdfium can read."""
    esc = lambda s: s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")  # noqa: E731
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{' '.join(f'{4 + 2 * i} 0 R' for i in range(len(pages)))}] "
               f"/Count {len(pages)} >>",
               "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"]
    for i, lines in enumerate(pages):
        stream = "BT /F1 10 Tf 14 TL 72 740 Td " + " ".join(f"({esc(l)}) Tj T*" for l in lines) + " ET"
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Resources << /Font << /F1 3 0 R >> >> /Contents {5 + 2 * i} 0 R >>")
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
    buf, offsets = bytearray(b"%PDF-1.4\n"), []
    for k, body in enumerate(objects, start=1):
        offsets.append(len(buf))
        buf += f"{k} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(buf)
    buf += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    buf += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    buf += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(buf))


@contextlib.contextmanager
def workspace():
    """A temporary private target ("P9") with the made-up document and PDF."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_doc().save_as_json(root / "doc.json")
        write_pdf(root / "doc.pdf", PDF_PAGES)
        target = gq.Target("P9", root / "doc.json", root / "doc.pdf", root / "eval" / "synthetic.json",
                           root / "eval" / "synthetic_cache", private=True)
        try:
            yield target
        finally:
            gq.post_with_retry = REAL_POST


def question(q, cls="Administration", cat="intervention", kind="lookup", evidence=(), facts=(), **kw):
    return {"question": q, "usdm_class": cls, "usdm_attributes": ["dose"], "category": cat, "kind": kind,
            "answerable": kind != "unanswerable",
            "evidence": [{"quote": a, "page": b, "section": "x"} for a, b in evidence],
            "facts": [{"text": t, "alts": [], "critical": True, "value": v, "unit": u, "comparator": "="}
                      for t, v, u in facts],
            "complete_list": kw.get("complete_list", False), "forbidden": kw.get("forbidden", []),
            "absent_terms": kw.get("absent_terms", []), "notes": ""}


GOOD = question("What weekly amount of the study drug is planned for the first group?",
                evidence=[("Cohort Alpha receives 40 mg zorvexumab once weekly.", 1)], facts=[("40 mg", 40, "mg")])
CANNED = [
    GOOD,
    question("How old must someone be to join, and which disease state is needed?", cls="EligibilityCriterionItem",
             cat="eligibility", kind="multi_evidence",
             evidence=[("Age of at least 21 years at screening.", 1),  # stated page wrong: really page 2
                       ("Measurable quuxoma disease per local criteria.", 2)],
             facts=[("21 years", 21, "years"), ("measurable quuxoma disease", None, None)]),
    question("Before any procedure, what must enrollees supply?", cls="EligibilityCriterionItem", cat="eligibility",
             evidence=[("participants must provide written consent", 2)], facts=[("written consent", None, None)]),
    question("What is the reduced level dose of the investigational drug?", evidence=[("20 mg", 2)],
             facts=[("20 mg", 20, "mg")]),  # table cell quote
    question("How many weeks apart are doses for the second group?",
             evidence=[("Cohort Beta receives 60 mg zorvexumab every three weeks.", 1)],  # not in the PDF
             facts=[("3 weeks", 3, "weeks")]),
    question("Does Cohort Alpha receives 40 mg zorvexumab once weekly in this trial?",  # copies the quote
             evidence=[("Cohort Alpha receives 40 mg zorvexumab once weekly.", 1)], facts=[("yes", None, None)]),
    question("What weekly amount of the study drug is planned for the first group ?",  # duplicate of GOOD
             evidence=[("Cohort Alpha receives 40 mg zorvexumab once weekly.", 1)], facts=[("40 mg", 40, "mg")]),
    question("What dose does Cohort Gamma receive at its first visit?", kind="unanswerable",
             absent_terms=["Cohort Gamma"], forbidden=[{"text": "40 mg", "why": "Cohort Alpha's dose"}]),
    question("What dose does Cohort Beta receive at its loading visit?", kind="unanswerable",
             absent_terms=["Cohort Beta"]),  # the term is in the document: not verifiably unanswerable
    question("Which class is this question tagged with, really?", cls="NotAUsdmClass",
             evidence=[("20 mg", 2)], facts=[("x", None, None)]),  # malformed
]


class FakeResponse:
    def __init__(self, data: dict):
        self.data = data

    def json(self) -> dict:
        return self.data


class FakeClient:
    """Stands in for retrieve.post_with_retry; records every request body."""

    def __init__(self, questions=CANNED, fail_with: Exception | None = None, reject: tuple[str, ...] = ()):
        self.questions, self.fail_with, self.reject = questions, fail_with, reject
        self.bodies: list[dict] = []

    def __call__(self, url, body, key, **kw):
        assert url == gq.URL and key == "test-key-not-real"
        self.bodies.append(body)
        if self.fail_with is not None:
            raise self.fail_with
        for param in self.reject:  # a provider that rejects these request parameters
            if param in body:
                raise RuntimeError(f"OpenRouter 400: {param} is not supported for this model")
        system = body["messages"][0]["content"]
        content = (json.dumps({"answer": "40 mg once weekly"}) if system == gq.ROUNDTRIP_PROMPT
                   else "```json\n" + json.dumps({"questions": self.questions}) + "\n```")
        return FakeResponse({"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 1000, "completion_tokens": 500}})


def run(target, **opts) -> tuple[dict, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        stats = gq.run_doc(target, gq.Options(workers=1, **opts))
    return stats, out.getvalue()


# --------------------------------------------------------------------------- tests

def test_units_follow_sections_and_skip_toc() -> None:
    doc = make_doc()
    units = gq.build_units(doc)
    assert len(units) == 1, [u.tokens for u in units]
    text = units[0].text
    assert "Section | Page" not in text and "........" not in text  # TOC table excluded
    assert "[page 1]" in text and "[page 2]" in text and "[page 3]" in text
    assert "| Dose Level | Zorvexumab dose |\n|---|---|\n| Level 1 | 40 mg |" in text
    assert "a. Cohort Alpha receives 40 mg zorvexumab once weekly." in text
    assert "## 2.1. Inclusion Criteria" in text and units[0].pages == [1, 2, 3]
    items = {it.text: it for it in units[0].items}
    assert items["1. Age of at least 21 years at screening."].path == ("2. Eligibility", "2.1. Inclusion Criteria")
    assert items["1. Age of at least 21 years at screening."].number == "2.1"
    assert items["1. Age of at least 21 years at screening."].section == "2.1. Inclusion Criteria"

    old = gq.MIN_UNIT_CONTENT
    gq.MIN_UNIT_CONTENT = 1  # at this tiny cap every group is "small"
    try:
        small = gq.build_units(doc, cap=40)  # split at section and item boundaries
    finally:
        gq.MIN_UNIT_CONTENT = old
    assert len(small) > 1
    assert all(u.tokens <= 40 or len(u.items) == 1 for u in small)
    assert all(any(it.kind != "heading" for it in u.items) for u in small)  # never headings only
    flat = [it.text for u in small for it in u.items if it.kind != "heading"]
    assert flat == [it.text for it in units[0].items if it.kind != "heading"]  # nothing lost, order kept
    mid = next(u for u in small if u.items[0].kind == "list" and "Cohort" in u.items[0].text)  # starts mid-section
    assert mid.path == ("1. Study Design",) and "Section path: 1. Study Design" in gq.user_prompt(mid, 5, False)
    assert gq.select_units(small, 2) == [small[0], small[len(small) // 2]]


def test_big_table_split_between_rows_with_header() -> None:
    d = DoclingDocument(name="big")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    d.add_heading("1. Schedule of Activities", level=1, prov=pv(1))
    table(d, [["Procedure", "Day 1", "Day 8"]] + [[f"Procedure number {i}", "X", ""] for i in range(60)], 1)
    units = gq.build_units(d, cap=120)
    parts = [it for u in units for it in u.items if it.kind == "table"]
    assert len(parts) > 2
    for part in parts:
        lines = part.text.splitlines()
        assert "| Procedure | Day 1 | Day 8 |" in lines and "|---|---|---|" in lines
        assert part.tokens <= 120
    rows = [l for p in parts for l in p.text.splitlines() if l.startswith("| Procedure number")]
    assert len(rows) == 60 and len(set(rows)) == 60


def test_prompt_and_request_body() -> None:
    unit = gq.build_units(make_doc())[0]
    user = gq.user_prompt(unit, 5, twin=True)
    assert unit.text in user and "3 to 5 answerable questions" in user and "unanswerable twin" in user
    assert "unanswerable twin" not in gq.user_prompt(unit, 5, twin=False)
    system = gq.system_prompt()
    assert all(c in system for c in gq.USDM_CLASSES) and all(c in system for c in gq.CATEGORIES)
    body = gq.request_body("anthropic/claude-sonnet-5.5", system, user, gq.QUESTION_SCHEMA)
    assert body["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
    assert body["temperature"] == 0 and body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["response_format"]["type"] == "json_schema" and body["response_format"]["json_schema"]["strict"]
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "response_format" not in gq.request_body("m", system, user, None)


def test_cache_key() -> None:
    key = gq.gen_key("model-a", "unit text", 5, True)
    assert key == gq.gen_key("model-a", "unit text", 5, True)
    assert len({key, gq.gen_key("model-b", "unit text", 5, True), gq.gen_key("model-a", "unit text!", 5, True),
                gq.gen_key("model-a", "unit text", 4, True), gq.gen_key("model-a", "unit text", 5, False)}) == 5
    old = gq.PROMPT_VERSION
    try:
        gq.PROMPT_VERSION = "syn-test"
        assert gq.gen_key("model-a", "unit text", 5, True) != key
    finally:
        gq.PROMPT_VERSION = old


def test_quote_verification() -> None:
    with workspace() as target:
        pdf = gq.PdfText.from_pdf(target.pdf_path)
        assert len(pdf) == 3
        assert pdf.find("Cohort Alpha receives 40 mg zorvexumab once weekly.", 1) == 1
        assert pdf.find("Cohort  Alpha\nreceives 40 mg", 3) == 1  # whitespace; wrong page -> neighbour
        assert pdf.find("approximately 120 participants across two cohorts", 1) == 1  # across a line break
        assert pdf.find("All participants must provide written consent", 2) == 2  # hyphenated line end
        assert pdf.find("Cohort Alpha receives 60 mg", 1) is None
        assert pdf.find("Screening visits occur within 28 days", 1) == 3  # 2 pages away
        assert pdf.find("Level 1", 2) == 2 and pdf.find("Level 1", 3) == 2
        assert not pdf.on_page("Level 1 4", 2)  # token boundaries: "4" is not "40"
    # pdfium quirks: Symbol-font glyphs, line-end hyphen markers, superscripts joined to the base
    raw = gq.PdfText(["ANC 1.0 × 109\r\n/L and partici\x02\r\npants agree; a sub￾set of dose￾escalation"])
    assert raw.on_page("ANC ≥1.0 × 10 9 /L", 1) and raw.on_page("participants agree", 1)
    assert raw.on_page("a subset", 1)  # too short for the spacing-free fallback: joined on token level
    assert raw.on_page("dose-escalation", 1) and raw.on_page("of dose escalation", 1)
    assert not raw.on_page("ANC 2.0", 1)


def test_verify_quotes_fixes_pages_sections_and_modality() -> None:
    with workspace() as target:
        unit = gq.build_units(make_doc())[0]
        pdf = gq.PdfText.from_pdf(target.pdf_path)
        c = gq.normalise(CANNED[1])
        ok, fixed = gq.verify_quotes(c, unit, pdf)
        assert ok and fixed == 1 and [e["page"] for e in c["evidence"]] == [2, 2]
        assert {e["section"] for e in c["evidence"]} == {"2.1. Inclusion Criteria"} and c["modality"] == ["list"]
        c = gq.normalise(CANNED[3])
        assert gq.verify_quotes(c, unit, pdf)[0] and c["modality"] == ["table"]
        assert not gq.verify_quotes(gq.normalise(CANNED[4]), unit, pdf)[0]


def test_normalise() -> None:
    assert gq.normalise(CANNED[-1]) is None  # unknown USDM class
    assert gq.normalise({**GOOD, "evidence": []}) is None  # answerable without evidence
    c = gq.normalise({**GOOD, "kind": "multi_evidence"})
    assert c["kind"] == "lookup"  # one quote cannot be multi-evidence
    c = gq.normalise({**CANNED[7], "evidence": [{"quote": "x", "page": 1, "section": ""}]})
    assert c["kind"] == "unanswerable" and c["evidence"] == [] and c["absent_terms"] == ["Cohort Gamma"]
    c = gq.normalise({**GOOD, "facts": [{"text": "40 mg", "value": "40", "critical": False, "comparator": "eq"}]})
    assert c["facts"][0] == {"text": "40 mg", "alts": [], "critical": True, "value": 40, "unit": None,
                             "comparator": None}


def test_leakage_filter() -> None:
    quote = "Cohort Alpha receives 40 mg zorvexumab once weekly."
    assert gq.leaks("How often is the 40 mg zorvexumab once weekly dose given?", [quote])
    assert not gq.leaks("What weekly amount of the study drug is planned for the first group?", [quote])
    assert not gq.leaks("Cohort Alpha receives what?", [quote])  # 3 shared words only


def test_dedupe() -> None:
    a = {"question": "What is the planned weekly dose for Cohort Alpha?"}
    b = {"question": "What is the planned weekly dose for the Cohort Alpha?"}  # near-identical
    c = {"question": "What is the planned dose for Cohort Beta every two weeks?"}
    assert gq.jaccard(a["question"], b["question"]) >= 0.8 > gq.jaccard(a["question"], c["question"])
    assert gq.dedupe([a, b, c]) == [a, c]


def test_class_cap_keeps_rarer_kinds() -> None:
    cands = [{"usdm_class": "Administration", "kind": "lookup", "i": i} for i in range(8)]
    cands += [{"usdm_class": "Administration", "kind": "multi_evidence", "i": 8},
              {"usdm_class": "Objective", "kind": "lookup", "i": 9}]
    kept = gq.class_cap(cands, 0.25)  # cap = ceil(0.25 * 10) = 3
    assert [c["i"] for c in kept] == [0, 1, 8, 9]
    assert gq.class_cap(cands, 1.0) == cands


def test_unanswerable_terms_checked_against_document() -> None:
    with workspace() as target:
        text = gq.DocText(gq.PdfText.from_pdf(target.pdf_path), make_doc())
        assert text.contains("Cohort Beta") and text.contains("zorvexumab") and not text.contains("Cohort Gamma")
        assert gq.absent(gq.normalise(CANNED[7]), text)
        assert not gq.absent(gq.normalise(CANNED[8]), text)
        assert not gq.absent({**gq.normalise(CANNED[7]), "absent_terms": []}, text)  # unverifiable


def test_parse_and_fact_matching() -> None:
    assert gq.parse_questions('Here you go:\n```json\n{"questions": [{"q": 1}]}\n```') == [{"q": 1}]
    assert gq.parse_questions('[{"q": 2}]') == [{"q": 2}]
    try:
        gq.parse_questions("no json here")
        raise AssertionError("parsed text without JSON")
    except ValueError:
        pass
    fact = {"text": "40 mg", "alts": ["forty milligrams"], "value": 40, "unit": "mg"}
    assert gq.fact_in(fact, "Cohort Alpha gets 40 mg weekly") and gq.fact_in(fact, "forty milligrams")
    assert gq.fact_in({**fact, "text": "40 mg/kg"}, "a dose of 40")  # numeric value
    assert not gq.fact_in(fact, "400 mg weekly")


def test_end_to_end_private_doc_prints_numbers_only() -> None:
    with workspace() as target:
        client = FakeClient()
        gq.post_with_retry = client
        stats, out = run(target)
        assert stats["new_calls"] == 1 and len(client.bodies) == 1
        body = client.bodies[0]
        assert body["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
        assert body["temperature"] == 0 and "Cohort Alpha receives" in body["messages"][1]["content"]
        assert stats["funnel"] == {"generated": 10, "well_formed": 9, "quotes_verified": 8, "no_leakage": 7,
                                   "unanswerable_verified": 6, "deduped": 5, "class_capped": 5}, stats["funnel"]
        records = json.loads(target.out_file.read_text(encoding="utf-8"))
        assert [r["id"] for r in records] == [f"P9-SYN-{n:04d}" for n in range(1, 6)]
        assert all(r["split"] == "dev" and r["source"] == "synthetic" and r["doc"] == "P9" for r in records)
        multi = next(r for r in records if r["kind"] == "multi_evidence")
        assert [e["page"] for e in multi["evidence"][0]["all_of"]] == [2, 2]
        assert multi["answer"]["facts"][0]["id"] == "f1" and multi["modality"] == ["list"]
        twin = next(r for r in records if not r["answerable"])
        assert twin["kind"] == "unanswerable" and twin["evidence"] == [] and twin["answer"]["facts"] == []
        assert twin["answer"]["forbidden"] == [{"text": "40 mg", "why": "Cohort Alpha's dose"}]
        assert set(records[0]) == {"id", "doc", "split", "source", "usdm", "category", "kind", "modality",
                                   "question", "answerable", "evidence", "answer", "notes"}

        # Nothing from the document, the questions or the quotes reaches stdout.
        secrets = ["Zorvex", "zorvexumab", "ZX-4417", "Alpha", "Gamma", "quuxoma", "Inclusion", "Screening visits",
                   "consent", "28 days", "20 mg", "doc.json", "doc.pdf", target.out_file.parent.as_posix(),
                   str(target.out_file.parent)]
        secrets += [q["question"] for q in CANNED] + [e["quote"] for q in CANNED for e in q["evidence"]]
        leaked = [s for s in secrets if s.lower() in out.lower()]
        assert not leaked, leaked
        assert "P9-SYN-0001 .. P9-SYN-0005" in out and "funnel:" in out

        # Second run: everything from the cache, no API call, same output.
        cached = list(target.cache_dir.glob("*.json"))
        assert len(cached) == 1 and cached[0].stem == gq.gen_key(gq.DEFAULT_MODEL, gq.build_units(make_doc())[0].text,
                                                                 5, True)
        gq.post_with_retry = FakeClient(fail_with=AssertionError("cache miss"))
        stats2, _ = run(target)
        assert stats2["new_calls"] == 0 and json.loads(target.out_file.read_text(encoding="utf-8")) == records


def test_roundtrip_filter() -> None:
    with workspace() as target:
        client = FakeClient()
        gq.post_with_retry = client
        stats, out = run(target, roundtrip=True)
        # the fake answers "40 mg once weekly": only the 40 mg question and the twin survive
        assert stats["funnel"]["roundtrip_passed"] == 2 and stats["roundtrip_calls"] == 4
        assert sum(gq.ROUNDTRIP_PROMPT == b["messages"][0]["content"] for b in client.bodies) == 4
        assert all(b["provider"] == gq.PROVIDER and b["temperature"] == 0 for b in client.bodies)


def test_dry_run_makes_no_calls() -> None:
    with workspace() as target:
        gq.post_with_retry = FakeClient(fail_with=AssertionError("API called in a dry run"))
        stats, out = run(target, dry_run=True)
        assert stats["calls"] == 1 and stats["tokens_in"] > 0 and stats["tokens_out"] > 0
        assert not target.out_file.exists() and not target.cache_dir.exists()
        assert "plan only" in out and "Zorvex" not in out


def test_private_errors_hide_messages() -> None:
    with workspace() as target:
        gq.post_with_retry = FakeClient(fail_with=RuntimeError("OpenRouter 500: echo of zorvexumab text"))
        stats, out = run(target)
        assert "zorvexumab" not in out and "OpenRouter 500" not in out
        assert stats["errors"] and all(k.startswith("RuntimeError at gen_questions.py:") for k in stats["errors"])
        assert json.loads(target.out_file.read_text(encoding="utf-8")) == []


def test_400_retried_without_rejected_parameters() -> None:
    body = gq.request_body("m", "s", "u", gq.QUESTION_SCHEMA)
    try:
        for reject, expected in ((("temperature",), ["temperature"]),
                                 (("response_format",), ["response_format"]),
                                 (("temperature", "response_format"), ["temperature", "response_format"])):
            client = FakeClient(reject=reject)
            gq.post_with_retry = client
            content, usage, dropped = gq.chat(body)
            assert dropped == expected and len(client.bodies) == len(expected) + 1, (reject, dropped)
            assert all(b["provider"] == gq.PROVIDER for b in client.bodies) and gq.parse_questions(content)
            assert all(k not in client.bodies[-1] for k in expected)
            assert all(k in client.bodies[-1] for k in ("temperature", "response_format") if k not in expected)
        gq.post_with_retry = FakeClient(fail_with=RuntimeError("OpenRouter 401: bad key"))
        try:
            gq.chat(body)
            raise AssertionError("a 401 was retried")
        except RuntimeError as exc:
            assert "401" in str(exc)
    finally:
        gq.post_with_retry = REAL_POST


def test_offline_guard_refuses_real_calls() -> None:
    old = os.environ.get("OPENROUTER_OFFLINE")
    os.environ["OPENROUTER_OFFLINE"] = "1"
    try:
        gq.chat(gq.request_body("m", "s", "u", None))
        raise AssertionError("a real API call was attempted")
    except RuntimeError as exc:
        assert "OPENROUTER_OFFLINE" in str(exc)
    finally:
        if old is None:
            os.environ.pop("OPENROUTER_OFFLINE")
        else:
            os.environ["OPENROUTER_OFFLINE"] = old


def test_targets() -> None:
    import test_protocols

    main = gq.resolve_target("MAIN")
    assert not main.private and main.out_file == gq.ROOT / "reports" / "eval" / "gold" / "MAIN_synthetic.json"
    assert main.cache_dir == gq.ROOT / "reports" / "eval" / "synthetic_cache"
    saved = test_protocols.protocols, test_protocols.doc_json
    test_protocols.protocols = lambda: [("P1", Path("made-up.pdf"))]  # never list the real folder in tests
    test_protocols.doc_json = lambda pid: None
    try:
        p1 = gq.resolve_target("P1")
        assert p1.private and p1.pdf_path == Path("made-up.pdf")
        eval_dir = gq.ROOT / "output_tests" / "P1" / "eval"
        assert p1.out_file == eval_dir / "synthetic.json" and p1.cache_dir.parent == eval_dir
        for bad in ("P7", "X1"):
            try:
                gq.resolve_target(bad)
                raise AssertionError(bad)
            except (KeyError, ValueError):
                pass
        _, out = run(p1)
        assert out.strip().endswith("not converted")
    finally:
        test_protocols.protocols, test_protocols.doc_json = saved


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
