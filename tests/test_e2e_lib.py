"""Offline tests of scripts/e2e_lib.py (plain script, no pytest): made-up PDFs, encrypted ones included (pypdfium2
opens them with the right password only and extracts the made-up text), quote extraction, scoring helpers,
statistics, derived OpenRouter counts and the numbers-only scrubbing.

    .venv\\Scripts\\python.exe tests\\test_e2e_lib.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pypdfium2 as pdfium  # noqa: E402

import e2e_lib as lib  # noqa: E402


def _text(pdf) -> str:
    parts = []
    for i in range(len(pdf)):
        page = pdf[i]
        tp = page.get_textpage()
        parts.append(tp.get_text_range())
        tp.close()
        page.close()
    return "\n".join(parts)


def _password_error(data: bytes, password: str | None) -> bool:
    try:
        pdf = pdfium.PdfDocument(data, password=password)
    except pdfium.PdfiumError as exc:
        return getattr(exc, "err_code", None) == 4  # FPDF_ERR_PASSWORD (what service.registry checks)
    pdf.close()
    return False


# ---------------------------------------------------------------- RC4 and the security handler

def test_rc4_known_vectors() -> None:
    assert lib.rc4(b"Key", b"Plaintext").hex() == "bbf316e8d940af0ad3"
    assert lib.rc4(b"Wiki", b"pedia").hex() == "1021bf0420"
    assert lib.rc4(b"Secret", b"Attack at dawn").hex() == "45a01f645fc35b383552544b9bf5"


def test_plain_pdf_opens_and_has_the_text() -> None:
    data = lib.make_pdf(lib.synthetic_protocol("T-PLAIN"))
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    pdf = pdfium.PdfDocument(data)
    try:
        assert len(pdf) == 2
        text = _text(pdf)
        assert f"Synthetic Protocol {lib.SYNTHETIC_TAG} T-PLAIN" in text
        assert "Volunteers who enjoy reading synthetic documents" in text
        page = pdf[1]
        images = [o for o in page.get_objects() if o.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE]
        assert len(images) == 1
        w, h = images[0].get_px_size()
        assert (w, h) == (480, 320)
        page.close()
    finally:
        pdf.close()


def _check_encrypted(revision: int) -> None:
    pw = "made-up-pw-123"
    data = lib.make_pdf(lib.synthetic_protocol(f"T-R{revision}"), user_password=pw, revision=revision)
    assert b"/Encrypt" in data and b"Synthetic Protocol" not in data  # the text is encrypted
    assert _password_error(data, None), "opened without a password"
    assert _password_error(data, "wrong-password"), "opened with a wrong password"
    pdf = pdfium.PdfDocument(data, password=pw)
    try:
        assert len(pdf) == 2
        text = _text(pdf)
        assert f"Synthetic Protocol {lib.SYNTHETIC_TAG} T-R{revision}" in text
        assert "Figure 1: Invented counts per schedule" in text
        page = pdf[1]
        images = [o for o in page.get_objects() if o.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE]
        bitmap = images[0].get_bitmap()  # the decrypted image stream decodes
        assert (bitmap.width, bitmap.height) == (480, 320)
        bitmap.close()
        page.close()
    finally:
        pdf.close()


def test_encrypted_rc4_revision_3_128_bit() -> None:
    _check_encrypted(3)


def test_encrypted_rc4_revision_2_40_bit() -> None:
    _check_encrypted(2)


def test_owner_password_differs() -> None:
    data = lib.make_pdf(lib.synthetic_protocol("T-OWNER", with_figure=False), user_password="u-pw",
                        owner_password="o-pw")
    pdf = pdfium.PdfDocument(data, password="u-pw")
    assert "T-OWNER" in _text(pdf)
    pdf.close()
    pdf = pdfium.PdfDocument(data, password="o-pw")  # the owner password opens it too
    assert "T-OWNER" in _text(pdf)
    pdf.close()


def test_same_arguments_same_bytes_and_distinct_codes() -> None:
    a = lib.make_pdf(lib.synthetic_protocol("T-A"), user_password="pw")
    assert a == lib.make_pdf(lib.synthetic_protocol("T-A"), user_password="pw")
    assert a != lib.make_pdf(lib.synthetic_protocol("T-B"), user_password="pw")


def test_truncated_and_not_a_pdf() -> None:
    data = lib.make_pdf(lib.synthetic_protocol("T-TRUNC"))
    cut = lib.truncated(data, 0.4)
    assert cut.startswith(b"%PDF-") and len(cut) < len(data)
    try:
        pdfium.PdfDocument(cut).close()
        opened = True
    except pdfium.PdfiumError:
        opened = False
    assert not opened
    assert b"%PDF-" not in lib.NOT_A_PDF[:1024] and len(lib.NOT_A_PDF) > 1024


# ---------------------------------------------------------------- quotes

def test_clean_line_and_strip_marker() -> None:
    assert lib.clean_line("## 5.1. Inclusion Criteria {#S5.1}") == "5.1. Inclusion Criteria"
    assert lib.clean_line("<!-- p. 31 --> **b. Female participants:**") == "b. Female participants:"
    assert lib.clean_line(r"a\_b \*c\* x") == "a_b *c* x"
    assert lib.strip_marker("1. Participants aged ≥18 at signing.") == "Participants aged ≥18 at signing."
    assert lib.strip_marker("· Presence of Kayser Fleischer rings,") == "Presence of Kayser Fleischer rings,"
    assert lib.strip_marker("[2b] Have a body mass index") == "Have a body mass index"
    assert lib.strip_marker("a) Female participants") == "Female participants"
    assert lib.strip_marker("I 05. Are able to") == "Are able to"
    assert lib.strip_marker("- 3. Nested item") == "Nested item"
    assert lib.strip_marker("HIV 1 infection") == "HIV 1 infection"  # not a marker
    assert lib.strip_marker("Participants who agree") == "Participants who agree"


def test_section_quotes_from_blocks() -> None:
    md_parts = ["## 5.1. Inclusion Criteria {#S5.1}", "Age",
                "1. Participants aged at least 18 years.\n· Presence of something visible here,",
                "| Objective | Endpoint |\n|---|---|\n| To assess the made-up thing well | Change in the score |"]
    md, blocks, pos = "", [], 0
    kinds = ["heading", "text", "list", "table"]
    for kind, part in zip(kinds, md_parts):
        if md:
            md += "\n\n"
            pos += 2
        blocks.append({"kind": kind, "section_id": "S5.1", "chars": [pos, pos + len(part)]})
        md += part
        pos += len(part)
    result = {"markdown": md, "blocks": blocks}
    quotes = lib.section_quotes(result, "inclusion")
    assert [q.text for q in quotes] == ["Participants aged at least 18 years.", "Presence of something visible here,"]
    assert all(q.section_id == "S5.1" and q.verbatim for q in quotes)
    with_tables = lib.section_quotes(result, "objectives_endpoints", tables=True, min_words=3)
    assert "To assess the made-up thing well" in [q.text for q in with_tables]
    assert "Change in the score" in [q.text for q in with_tables]
    assert "Objective" not in [q.text for q in with_tables]  # fewer than 3 words


def test_table_cells() -> None:
    md = "| A | B \\| C |\n|:--|--:|\n| one <br> two | |"
    assert lib.table_cells(md) == ["A", "B | C", "one two"]


def test_visit_and_identifier_quotes() -> None:
    soa = {"soas": [{"visits": [{"label": "Day 1", "header_path": ["Period 1", "Day 1"]},
                                {"label": "Period 1 Day 2", "header_path": ["Period 1", "Day 2"]},
                                {"label": "Day 1", "header_path": ["Day 1"]}, {"label": "", "header_path": []}]}]}
    quotes = lib.visit_quotes(soa)
    assert [(q.text, q.verbatim) for q in quotes] == [("Day 1", False), ("Period 1 Day 2", False)]
    info = {"identifiers": {"nct": {"value": "NCT00000001", "source": {"text": "NCT #: NCT00000001"}},
                            "sponsor_protocol_id": None}}
    assert [q.text for q in lib.identifier_quotes(info)] == ["NCT00000001", "NCT #: NCT00000001"]


# ---------------------------------------------------------------- scoring

def test_window_cover() -> None:
    hay = "a b c the quick brown fox jumps d e".split()
    assert lib.window_cover("quick brown fox".split(), hay) == 1.0
    assert lib.window_cover("quick red fox".split(), hay) == 2 / 3
    assert lib.window_cover([], hay) == 1.0


class _Ent:
    def __init__(self, text, **attrs):
        self.text, self.attrs, self.name, self.id = text, attrs, "", "x"


class _Study:
    def __init__(self, ents):
        self.ents = ents

    def of(self, cls):
        return self.ents.get(cls, [])


def test_key_identifiers_and_compare() -> None:
    study = _Study({"StudyIdentifier": [
        _Ent("NCT01234567", scope_name="ClinicalTrials.gov", scope="", scope_type="Registry"),
        _Ent("ABC-123", scope_name="Made-up Pharma", scope="", scope_type="Clinical Study Sponsor"),
        _Ent("2020-001104-41", scope_name="EMA", scope="", scope_type="Regulatory Agency")]})
    ids = lib.key_identifiers(study)
    assert ids == {"nct": "NCT01234567", "sponsor_protocol_id": "ABC-123", "eudract": "2020-001104-41"}
    info = {"identifiers": {"nct": {"value": "NCT01234567"}, "sponsor_protocol_id": {"value": "abc-123"},
                            "eudract": None}}
    cmp = lib.compare_identifiers(ids, info)
    assert cmp["nct"][2] and cmp["sponsor_protocol_id"][2] and not cmp["eudract"][2]


def test_criteria_found_with_a_fake_key() -> None:
    study = _Study({"EligibilityCriterion": [
        _Ent("Participants aged at least 18 years at screening", category="Inclusion Criteria", identifier="1"),
        _Ent("Known allergy to purple bananas", category="Exclusion Criteria", identifier="1")]})
    found = lib.criteria_found(study, "inclusion", "1. Participants aged at least 18 years at screening.")
    assert found == [("1", 1.0)]
    assert lib.criteria_found(study, "exclusion", "nothing relevant") == [("1", 0.0)]
    assert lib.texts_found(["Participants aged at least 18 years"], "participants aged at least 18 years") == (1, 1)


# ---------------------------------------------------------------- measurements and output

def test_summarize_and_sizes() -> None:
    assert lib.summarize([3, 1, 2]) == {"n": 3, "median": 2.0, "max": 3.0}
    assert lib.summarize([]) == {"n": 0, "median": 0.0, "max": 0.0}
    assert lib.parse_mem_usage("1.5GiB / 8GiB") == int(1.5 * 1024 ** 3)
    assert lib.parse_mem_usage("512MiB / 8GiB") == 512 * 1024 ** 2
    assert lib.parse_mem_usage("10kB / 1GB") == 10000
    assert lib.parse_mem_usage("--") is None
    assert lib.gib(None) == "-" and lib.gib(2 * 1024 ** 3) == "2.00 GiB"


def test_derived_requests() -> None:
    job = {"stages": [{"name": "figures", "detail": {"described_now": 3, "failed": 1, "already_cached": 9}},
                      {"name": "embed", "detail": {"embedded": 65, "cached": 10}}]}
    assert lib.derived_requests(job) == {"figures": 4, "embed_batches": 3, "embedded_chunks": 65}
    assert lib.derived_requests({"stages": [{"name": "embed", "status": "skipped_offline"}]}) == \
        {"figures": 0, "embed_batches": 0, "embedded_chunks": 0}
    assert lib.stage_seconds({"stages": [{"name": "convert", "seconds": 12.5}]}) == {"convert": 12.5}


def test_scrub() -> None:
    line = "DELETE /documents/" + "ab" * 32 + " -> HTTP 409 document_busy (job " + "cd" * 16 + ")"
    assert lib.scrub(line) == "DELETE /documents/<id> -> HTTP 409 document_busy (job <id>)"
    assert lib.scrub("HTTP 200, 12 parts") == "HTTP 200, 12 parts"


# ---------------------------------------------------------------- runner

def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    print(f"{len(tests) - failed} passed" + (f", {failed} failed" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
