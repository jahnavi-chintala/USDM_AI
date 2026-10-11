"""Self-test for the downstream generalisation fixes (chunk_protocol, figure_vlm, validate_output,
retrieve, eval_retrieval) on small synthetic documents: `python tests/test_downstream.py`.

No network and no protocol content: header strings are made-up examples of common formats.
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from docling_core.types.doc import (BoundingBox, ContentLayer, DocItemLabel, DoclingDocument,
                                    ProvenanceItem, Size)
from docling_core.types.doc.document import (PictureClassificationMetaField,
                                             PictureClassificationPrediction, PictureMeta)
from docling_core.types.doc.labels import PictureClassificationLabel

from chunk_protocol import (ProtocolSerializerProvider, accepted_section_numbers, chunk_id_base,
                            doc_metadata, doc_prefix, dropped_figure, figure_parts, first_row_is_header,
                            heading_paths, looks_like_acronym, section_of)
from eval_retrieval import is_answer
from figure_vlm import (DIAGRAM_CLASSES, PROMPT_VERSION, SKIP_CLASSES, FigureResult, attach_cached,
                        document_key, skip_figure)
from validate_output import PAGE_NUMBER, _furniture_lines, _norm, aggregate

W, H = 612.0, 792.0


def prov(page: int, l: float = 50, t: float = 50, r: float = 300, b: float = 70) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=l, t=t, r=r, b=b), charspan=(0, 0))


def document(name: str, headers: list[str], title_page: list[tuple[str, str]], pages: int = 6,
             footers: list[str] = ()) -> DoclingDocument:
    """Running headers/footers on every page ("#" = page number); title page items (kind, text)."""
    doc = DoclingDocument(name=name)
    for p in range(1, pages + 1):
        doc.add_page(page_no=p, size=Size(width=W, height=H))
        for text in headers:
            doc.add_text(DocItemLabel.PAGE_HEADER, text.replace("#", str(p)), prov=prov(p),
                         content_layer=ContentLayer.FURNITURE)
        for text in footers:
            doc.add_text(DocItemLabel.PAGE_FOOTER, text.replace("#", str(p)), prov=prov(p, t=760, b=780),
                         content_layer=ContentLayer.FURNITURE)
    for kind, text in title_page:
        if kind == "h":
            doc.add_heading(text, prov=prov(1))
        else:
            doc.add_text(DocItemLabel.TEXT, text, prov=prov(1))
    return doc


def metadata(doc: DoclingDocument, explicit: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"{doc.name}.json"
        doc.save_as_json(path)
        return doc_metadata(doc, path, explicit)


TITLE = ("t", "A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With Condition Y")

# (headers, footers, title page, expected protocol_id, version_label, amendment, version_date)
HEADER_STYLES = [
    (["PF-00000000 (Drugname)", "Protocol C1234567", "Final Protocol Amendment 4, 5 January 2021"], [],
     [("h", "STUDYNAME-3"), TITLE], "C1234567", "Amendment 4", "4", "5 January 2021"),
    (["Protocol No.: MK-1234-005 Version 3.0", "05-Jan-2021"], ["Confidential", "Page # of 6"],
     [("h", "CLINICAL STUDY PROTOCOL"), TITLE], "MK-1234-005", "Version 3.0", None, "05-Jan-2021"),
    (["Clinical Study Protocol D1234C00002", "Edition 4"], ["2021-01-05"],
     [("h", "KEYNOTE-999"), TITLE], "D1234C00002", "Edition 4", None, "2021-01-05"),
    (["CABC123A2301", "Amendment 2"], ["March 14, 2021", "#"],
     [TITLE], "CABC123A2301", "Amendment 2", "2", "March 14, 2021"),
    (["Study 20180123"], ["05Jan2021 | Page #"],
     [("h", "Sponsor Name Inc"), TITLE], "20180123", None, None, "05Jan2021"),
    (["GO12345", "Version 5"], [], [TITLE], "GO12345", "Version 5", None, None),
    ([], ["Page #"], [("h", "PROTOCOL"), TITLE], None, None, None, None),
]


def test_metadata_header_formats() -> None:
    ids = set()
    for i, (heads, foots, title_page, pid, label, amendment, date) in enumerate(HEADER_STYLES):
        meta = metadata(document(f"doc{i}", heads, title_page, footers=foots))
        got = (meta["protocol_id"], meta["version_label"], meta["amendment"], meta["version_date"])
        assert got == (pid, label, amendment, date), (i, got)
        prefix = doc_prefix(meta)
        assert "None" not in prefix and "None" not in chunk_id_base(meta), (i, prefix)
        ids.add(f"{chunk_id_base(meta)}-0000")
    assert len(ids) == len(HEADER_STYLES), ids  # chunk ids never collide across documents


def test_metadata_running_header_prefix_unchanged() -> None:
    heads, foots, title_page = HEADER_STYLES[0][:3]
    meta = metadata(document("rh", heads, title_page, footers=foots))
    assert meta["acronym"] == "STUDYNAME-3" and meta["title"] == TITLE[1]
    assert doc_prefix(meta) == "STUDYNAME-3 protocol C1234567 (Amendment 4, 5 January 2021)"
    assert chunk_id_base(meta) == "C1234567-A4"  # legacy id format kept when id + amendment known


def test_metadata_fallbacks() -> None:
    meta = metadata(document("bare", [], [("h", "CLINICAL STUDY PROTOCOL"), TITLE]))
    assert meta["acronym"] is None and doc_prefix(meta) == "bare protocol"
    assert chunk_id_base(meta).startswith("doc-")
    meta = metadata(document("ex", [], [TITLE]), {"protocol_id": "ABC-001", "amendment": "3"})
    assert doc_prefix(meta) == "ex protocol ABC-001 (Amendment 3)" and chunk_id_base(meta) == "ABC-001-A3"
    assert looks_like_acronym("NOVA-3") and looks_like_acronym("CheckMate 227")
    assert not looks_like_acronym("CONFIDENTIAL") and not looks_like_acronym("Clinical Study Protocol")


def test_section_number_uses_accepted_numbers() -> None:
    doc = DoclingDocument(name="sec")
    doc.add_page(page_no=1, size=Size(width=W, height=H))
    # Sections 1-10 as in a real protocol: an accepted number must follow the previous one.
    sections = ([(f"{n}. SECTION {n}", 1) for n in range(1, 10)] + [("10. APPENDICES", 1)]
                + [(f"10.{n}. Appendix {n}", 2) for n in range(1, 4)])
    for text, level in sections + [("10.4. Appendix 4: Contraception", 2), ("3. Postmenopausal female:", 3)]:
        doc.add_heading(text, level=level, prov=prov(1))
    para = doc.add_text(DocItemLabel.TEXT, "Body text.", prov=prov(1))
    serializer = ProtocolSerializerProvider().get_serializer(doc)
    path = heading_paths(doc, serializer)[para.self_ref]
    headings = [h.text for h in path]
    assert section_of(headings) == "3"  # legacy rule: any numbered breadcrumb
    assert section_of(headings, path, accepted_section_numbers(doc)) == "10.4"


def picture(doc: DoclingDocument, cls: str, conf: float, area_share: float, page: int = 1):
    side = (area_share * W * H) ** 0.5
    pic = doc.add_picture(prov=prov(page, l=100, t=100, r=100 + side, b=100 + side))
    pic.meta = PictureMeta(classification=PictureClassificationMetaField(
        predictions=[PictureClassificationPrediction(class_name=cls, confidence=conf)]))
    return pic


def test_figure_classes_and_skipping() -> None:
    labels = {l.value for l in PictureClassificationLabel}
    assert DIAGRAM_CLASSES <= labels and SKIP_CLASSES <= labels
    doc = DoclingDocument(name="fig")
    doc.add_page(page_no=1, size=Size(width=W, height=H))
    logo = picture(doc, "logo", 0.99, 0.02)
    unsure = picture(doc, "logo", 0.40, 0.02)
    big = picture(doc, "icon", 0.95, 0.20)
    chart = picture(doc, "flow_chart", 0.98, 0.20)
    assert skip_figure(logo, doc)
    assert not skip_figure(unsure, doc) and not skip_figure(big, doc) and not skip_figure(chart, doc)
    assert dropped_figure(logo, doc, figure_parts(logo, doc))
    doc.add_text(DocItemLabel.TEXT, "Text in figure: Screening; Treatment", parent=logo, prov=prov(1))
    assert not dropped_figure(logo, doc, figure_parts(logo, doc))  # OCR text is never dropped


def test_figure_cache_needs_same_document() -> None:
    model = "test:model"
    doc = document("figdoc", ["Protocol X1234567"], [TITLE])
    pic = picture(doc, "flow_chart", 0.98, 0.20)
    b = pic.prov[0].bbox.to_bottom_left_origin(H)
    entry = FigureResult(model=model, prompt_version=PROMPT_VERSION, description="A schema.", page=1,
                         bbox=[b.l, b.t, b.r, b.b])
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp)
        for name, key in (("other", "0123456789abcdef"), ("legacy", None)):
            entry.doc_key = key
            (cache / f"{name}.json").write_text(json.dumps(entry.__dict__), encoding="utf-8")
        assert attach_cached(doc, model, cache) == {"described": 0, "mermaid": 0, "missing": 1}
        entry.doc_key = document_key(doc)
        (cache / "same.json").write_text(json.dumps(entry.__dict__), encoding="utf-8")
        assert attach_cached(doc, model, cache) == {"described": 1, "mermaid": 0, "missing": 0}
    other = document("figdoc", ["Protocol Y7654321"], [TITLE])  # same layout, different title page
    assert document_key(other) != document_key(doc)


def test_validate_aggregate_and_furniture() -> None:
    report = [{"page": 1, "coverage": 1.0, "pdf_words": 100, "missing": [], "expected": False},
              {"page": 2, "coverage": 0.9, "pdf_words": 100, "missing": ["secret", "words"], "expected": False},
              {"page": 3, "coverage": 0.1, "pdf_words": 50, "missing": ["toc"], "expected": True}]
    out = aggregate(report)
    assert out == {"coverage": 0.95, "pdf_words": 200, "furniture_share": 0.0, "pages_checked": 2,
                   "toc_pages_skipped": 1, "pages_below_threshold": 1, "lowest_page_coverage": 0.9,
                   "pages_below": [2]}
    flat = json.dumps(out)
    assert "secret" not in flat and "toc" not in flat.replace("toc_pages_skipped", "")
    # Odd/even running headers (each on half the pages) and numbered footers are furniture.
    bands = [{_norm(f"Odd header {p}" if p % 2 else "Even header"), _norm(f"Page {p} of 10")}
             for p in range(1, 11)]
    assert _furniture_lines(bands) == {"Odd header #", "Even header", "Page # of #"}
    assert all(PAGE_NUMBER.fullmatch(s) for s in ("12", "Page 12", "Page 3 of 140", "page 3 of 9"))
    assert not PAGE_NUMBER.fullmatch("12 mg")


def chunks_folder(root: Path, json_name: str, shas: list[str]) -> Path:
    out = root / "chunks"
    out.mkdir(parents=True)
    recs = [{"chunk_id": f"c-{i}", "content_hash": str(i), "source_sha256": s, "embed_text": "x",
             "text": "x", "chunk_type": "text", "doc_item_refs": [], "headings": [], "pages": [1]}
            for i, s in enumerate(shas)]
    (out / "chunks.jsonl").write_text("\n".join(json.dumps(r) for r in recs), encoding="utf-8")
    (out / "embeddings_meta.json").write_text(json.dumps({"chunk_ids": [r["chunk_id"] for r in recs]}),
                                              encoding="utf-8")
    np.save(out / "embeddings.npy", np.zeros((len(recs), 4), dtype=np.float32))
    (out / "stats.json").write_text(json.dumps({"document": {"source_json": json_name,
                                                             "source_sha256": shas[0]}}), encoding="utf-8")
    (out / "tables.jsonl").write_text("", encoding="utf-8")
    return out


def test_context_builder_checks_source_sha() -> None:
    import hashlib

    from retrieve import ContextBuilder, Retriever, source_json

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        doc_path = root / "doc.json"
        DoclingDocument(name="doc").save_as_json(doc_path)
        right = hashlib.sha256(doc_path.read_bytes()).hexdigest()[:16]
        chunks = chunks_folder(root / "a", "doc.json", [right, right])
        (root / "a" / "doc.json").write_bytes(doc_path.read_bytes())
        assert source_json(chunks) == root / "a" / "doc.json"
        wrong = chunks_folder(root / "b", "doc.json", ["0" * 16])
        (root / "b" / "doc.json").write_bytes(doc_path.read_bytes())
        retriever = Retriever(wrong / "chunks.jsonl")
        try:
            ContextBuilder(retriever)
            raise AssertionError("ContextBuilder accepted a JSON with another sha")
        except RuntimeError as exc:
            assert "not the JSON these chunks were made from" in str(exc)
        mixed = chunks_folder(root / "c", "doc.json", [right, "1" * 16])
        try:
            Retriever(mixed / "chunks.jsonl")
            raise AssertionError("Retriever accepted chunks of two documents")
        except RuntimeError as exc:
            assert "mixes chunks of 2 source documents" in str(exc)


def test_first_row_header_gate() -> None:
    assert first_row_is_header([["Parameter", "Day 1", "Day 8"], ["ALT", "x", "x"], ["AST", "x", ""]])
    assert not first_row_is_header([["Intervention Name", "Drug X"], ["Type", "Biologic"], ["Dose", "1 mg"]])
    assert not first_row_is_header([["0", "Fully active.", "a"], ["1", "b", "c"], ["2", "d", "e"]])
    assert not first_row_is_header([["Visit", "Visit", "Day"], ["a", "b", "c"], ["d", "e", "f"]])


def test_rows_line_capped() -> None:
    from docling_core.transforms.chunker.tokenizer.base import BaseTokenizer

    from chunk_protocol import ROWS_LINE_BUDGET, ProtocolChunker

    class Words(BaseTokenizer):  # one token per whitespace-separated word
        def count_tokens(self, text: str) -> int:
            return len(text.split())

        def get_max_tokens(self) -> int:
            return 512

        def get_tokenizer(self):
            return None

    chunker = ProtocolChunker(tokenizer=Words())
    short = chunker._rows_line(["Amendment 1", "Amendment 2"])
    assert short == "Rows: Amendment 1, Amendment 2"
    long = [" ".join(["word"] * 15) + f" {i}" for i in range(4)]
    line = chunker._rows_line(long)
    assert len(line.split()) <= ROWS_LINE_BUDGET and line.startswith("Rows: "), line


def test_long_row_label_repeated_shortened() -> None:
    from chunk_protocol import LONG_ROW_LABEL, row_label_matches, short_label

    label = "Participants who discontinue study intervention for any reason " * 4  # > 150 chars
    label = label.strip()
    assert len(label) > LONG_ROW_LABEL
    short = short_label(label)
    assert short.endswith("…") and len(short) <= 61 and label.startswith(short[:-1])
    row = f"{label}: Visit 1 = X; Visit 2 = X"
    assert row_label_matches(short, row) and row_label_matches(label, row)
    assert short_label("Amendment 2") == "Amendment 2"
    assert not row_label_matches("Amendment 2", "Amendment 20: ...")

def test_eval_answer_checker() -> None:
    chunk = {"text": "The primary endpoint is ORR.", "section_number": "3.1", "pages": [30, 31]}
    assert is_answer(chunk, {"contains": ["primary endpoint"]})
    assert is_answer(chunk, {"sections": ["3"]}) and not is_answer(chunk, {"sections": ["31"]})
    assert is_answer(chunk, {"contains": ["primary endpoint"], "page": 31})
    assert not is_answer(chunk, {"contains": ["primary endpoint"], "page": [5, 6]})
    assert is_answer(chunk, {"page": [30]}) and not is_answer(chunk, {"page": 2})



def test_front_matter_refs() -> None:
    from chunk_protocol import front_matter_refs

    d = DoclingDocument(name="t")
    sponsor = d.add_text(label=DocItemLabel.TEXT, text="Sponsor line above the title")
    d.add_heading(text="1. Introduction", level=1)
    body = d.add_text(label=DocItemLabel.TEXT, text="Body text")
    refs = front_matter_refs(d)
    assert sponsor.self_ref in refs and body.self_ref not in refs

if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
