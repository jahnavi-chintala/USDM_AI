"""Self-test for parents.py and the parent-aware ContextBuilder mode: `python tests/test_parents.py`.

A small made-up protocol ("Zorvex", no real document text), chunked by hand (section-bounded, a table
split row-wise into parts), a word-count tokenizer, and a ContextBuilder assembled from fakes (no
embeddings, no network). Checks: partition invariants (non-overlap, every chunk in exactly one parent,
size bounds except table parents, windows carry their heading) with the fallback index and, when
importable, structure.py's index; greedy fill within the budget; degradation order (parent, +-1
neighbours, chunk alone, dropped; table whole / rows +-1); near-duplicate suppression (synopsis vs
body); the identity block, with the fields a model chose in the stored identity record (chunks/identity.json) laid over
the stats values; section-level small-to-big; the identifier patterns.
"""

import copy
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docling_core.types.doc import (BoundingBox, ContentLayer, CoordOrigin, DocItemLabel, DoclingDocument, ListItem,
                                    ProvenanceItem, Size, TableCell, TableData, TableItem)

import identity
import parents
import retrieve
from chunk_protocol import (IDENTIFIER_PATTERNS, ProtocolSerializerProvider, ProtocolTableSerializer,
                            registry_identifiers)
from retrieve import Block, ContextBuilder, Hit, Result, identity_block

MIN, MAX = 20, 60  # partition limits in words (the test tokenizer counts words)
SHA = "feedface00112233"  # the made-up Docling JSON's source_sha256 in stats.json and a stored identity record
WORDS = lambda s: len(s.split())  # noqa: E731

BODY_DESIGN = ("The Zorvex trial is a randomized open label study of zorvexumab in adults with quuxoma "
               "comparing two dose levels across two cohorts with a common follow up period")


# --------------------------------------------------------------------------- fixtures

def pv(page: int) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=72, t=700, r=540, b=690,
                                                         coord_origin=CoordOrigin.BOTTOMLEFT), charspan=(0, 1))


def sentence(topic: str, n: int) -> str:
    return " ".join(f"{topic}{i}" for i in range(n)) + "."


def make_doc() -> DoclingDocument:
    d = DoclingDocument(name="zorvex")
    for p in range(1, 10):
        d.add_page(page_no=p, size=Size(width=612, height=792))
        d.add_text(DocItemLabel.PAGE_HEADER, "ZX-4417 Zorvex NCT01234567", prov=pv(p),
                   content_layer=ContentLayer.FURNITURE)
    d.add_text(DocItemLabel.TEXT, "Zorvex Study Protocol ZX-4417", prov=pv(1))
    d.add_text(DocItemLabel.TEXT, "EudraCT number: 2019-003431-33", prov=pv(1))
    d.add_text(DocItemLabel.TEXT, "EU CT number: 2022-500123-12-00", prov=pv(1))
    d.add_text(DocItemLabel.TEXT, "US IND Number: 140,453", prov=pv(1))
    d.add_heading("1. Protocol Summary", level=1, prov=pv(2))
    d.add_heading("1.1. Synopsis", level=2, prov=pv(2))
    d.add_text(DocItemLabel.TEXT, BODY_DESIGN + ". Enrollment closes after 120 participants are dosed.", prov=pv(2))
    d.add_text(DocItemLabel.TEXT, sentence("syn", 30), prov=pv(2))
    d.add_heading("2. Introduction", level=1, prov=pv(3))
    d.add_text(DocItemLabel.TEXT, sentence("intro", 8), prov=pv(3))
    d.add_heading("3. Objectives", level=1, prov=pv(3))
    d.add_text(DocItemLabel.TEXT, sentence("obj", 8), prov=pv(3))
    d.add_heading("4. Study Design", level=1, prov=pv(4))
    for k in range(5):  # own text > MAX: windows
        d.add_text(DocItemLabel.TEXT, sentence(f"des{k}x", 25), prov=pv(4))
    d.add_heading("4.1. Overall Design", level=2, prov=pv(4))
    d.add_text(DocItemLabel.TEXT, BODY_DESIGN, prov=pv(4))
    d.add_heading("5. Study Population", level=1, prov=pv(5))
    d.add_heading("5.1. Inclusion Criteria", level=2, prov=pv(5))
    group = d.add_list_group()
    for i in range(1, 13):
        d.add_list_item(f"Criterion {i} " + sentence(f"inc{i}x", 8), marker=f"{i}.", parent=group, prov=pv(5))
    d.add_heading("5.2. Exclusion Criteria", level=2, prov=pv(6))
    group = d.add_list_group()
    for i in range(1, 5):
        d.add_list_item(f"Exclusion {i} " + sentence(f"exc{i}x", 6), marker=f"{i}.", parent=group, prov=pv(6))
    d.add_heading("6. Schedule of Activities", level=1, prov=pv(7))
    rows = [["Activity", "Screening", "Day 1", "Day 8"]] + [[f"Assessment {r}", "X", "", "X"] for r in range(1, 25)]
    cells = [TableCell(text=x, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                       end_col_offset_idx=c + 1, column_header=r == 0)
             for r, row in enumerate(rows) for c, x in enumerate(row)]
    d.add_table(data=TableData(num_rows=len(rows), num_cols=4, table_cells=cells), prov=pv(7))
    d.add_heading("7. Dose Levels", level=1, prov=pv(8))
    rows = [["Dose level", "Dose"], ["Level 1", "40 mg"], ["Level -1", "20 mg"]]
    cells = [TableCell(text=x, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                       end_col_offset_idx=c + 1, column_header=r == 0)
             for r, row in enumerate(rows) for c, x in enumerate(row)]
    d.add_table(data=TableData(num_rows=3, num_cols=2, table_cells=cells), prov=pv(8))
    d.add_text(DocItemLabel.TEXT, sentence("dose", 12), prov=pv(8))
    d.add_heading("8. References", level=1, prov=pv(9))
    d.add_text(DocItemLabel.TEXT, "Results of an earlier trial (NCT07654321) informed the dose.", prov=pv(9))
    return d


def make_chunks(doc: DoclingDocument, size: int = 30) -> list[dict]:
    """Section-bounded chunks of <= size words over the fallback index; tables split into row parts."""
    index = parents.build_fallback_index(doc)
    ser = ProtocolSerializerProvider().get_serializer(doc)
    chunks: list[dict] = []

    def add(refs, text, kind, sec):
        chunks.append({"chunk_id": f"T-A1-{len(chunks):04d}", "doc_item_refs": refs, "text": text,
                       "chunk_type": kind, "headings": [sec.title], "pages": [1]})

    for sec in index.sections:
        cur_refs, cur_text = [], []
        for ref in sec.item_refs:
            item = next(it for it, _ in doc.iterate_items() if it.self_ref == ref)
            if isinstance(item, TableItem):
                if cur_refs:
                    add(cur_refs, "\n".join(cur_text), "text", sec)
                    cur_refs, cur_text = [], []
                cols, rows = ProtocolTableSerializer.columns_and_rows(item, doc, ser)
                prefix = ProtocolTableSerializer.rows_prefix("", cols)
                step = 8 if len(rows) > 8 else len(rows)
                for i in range(0, len(rows), step):
                    add([ref], prefix + "\n".join(rows[i:i + step]), "table", sec)
                continue
            text = f"{item.marker} {item.text}" if isinstance(item, ListItem) else item.text
            if cur_refs and WORDS(" ".join(cur_text)) + WORDS(text) > size:
                add(cur_refs, "\n".join(cur_text), "text", sec)
                cur_refs, cur_text = [], []
            cur_refs.append(ref)
            cur_text.append(text)
        if cur_refs:
            add(cur_refs, "\n".join(cur_text), "text", sec)
    return chunks


def indexes(doc):
    out = [("fallback", parents.build_fallback_index(doc))]
    try:
        import structure
    except ImportError:
        return out
    out.append(("structure", structure.build_section_index(doc)))
    return out


# --------------------------------------------------------------------------- partition

def check_partition(doc, index, chunks) -> parents.ParentStore:
    store = parents.ParentStore(doc, index, chunks, WORDS, MIN, MAX)
    ps = store.parents
    # non-overlap: every unit (non-heading item) in exactly one parent
    seen: dict[str, str] = {}
    headings = {s.heading_ref for s in index.sections if s.heading_ref}
    for p in ps:
        for r in p.doc_item_refs:
            if r in headings:
                continue
            assert r not in seen, f"{r} in {seen[r]} and {p.parent_id}"
            seen[r] = p.parent_id
    assert set(seen) == {u.ref for u in store.units}, "every unit is in some parent"
    # coverage: every chunk in exactly one parent
    assert store.checks["chunks_mapped"] == len(chunks) and store.checks["chunks_in_several_parents"] == 0
    listed = [c for p in ps for c in p.chunk_ids]
    assert sorted(listed) == sorted(c["chunk_id"] for c in chunks) and len(listed) == len(set(listed))
    for c in chunks:
        p = store.parent_of(c)
        assert all(r in p.doc_item_refs for r in c["doc_item_refs"]), "a chunk's items lie in its parent"
    # size bounds: <= MAX unless a table parent; table parents hold one big table
    for p in ps:
        if p.kind == "table":
            assert p.tokens > MAX and len(p.table_ids) == 1
        else:
            assert p.tokens <= MAX, (p.parent_id, p.kind, p.tokens)
    assert sum(p.tokens < MIN for p in ps) <= 2, "tiny parents are merged where a sibling allows"
    kinds = {p.kind for p in ps}
    assert {"section", "window", "merged", "table"} <= kinds, kinds
    # windows carry their heading path; later windows say "(continued)"
    wins = [p for p in ps if p.kind == "window" and "Study Design" in p.breadcrumb]
    assert len(wins) >= 2 and wins[0].text.startswith("# 4. Study Design")
    assert any("(continued)" in w.text.splitlines()[0] for w in wins[1:])
    assert [p.parent_id for p in ps] == sorted(p.parent_id for p in ps)
    return store


def test_partition_invariants() -> None:
    doc = make_doc()
    chunks = make_chunks(doc)
    for name, index in indexes(doc):
        store = check_partition(doc, index, chunks)
        stats = store.stats()
        assert stats["over_max_non_table"] == 0 and stats["chunks_without_unit"] == 0, (name, stats)


def test_chunk_aligned_atoms() -> None:
    """A window boundary never splits a chunk: chunks spanning a would-be cut keep their items together."""
    doc = make_doc()
    chunks = make_chunks(doc, size=55)  # bigger chunks: several design paragraphs per chunk
    store = check_partition(doc, parents.build_fallback_index(doc), chunks)
    for c in chunks:
        owners = {store.parent_of_atom[store.atom_of_unit[store.unit_of[r]]] for r in c["doc_item_refs"]}
        assert len(owners) == 1


def test_annotate_and_build_parents() -> None:
    doc = make_doc()
    chunks = make_chunks(doc)
    texts = [c["text"] for c in chunks]
    ps = parents.build_parents(doc, parents.build_fallback_index(doc), chunks, WORDS, MIN, MAX)
    assert [c["text"] for c in chunks] == texts, "metadata only"
    ids = {p.parent_id for p in ps}
    assert all(c["parent_id"] in ids and c["section_id"] for c in chunks)
    syn = [c for c in chunks if "syn0" in c["text"]]
    assert syn and syn[0]["role"] == "synopsis"
    assert json.loads(json.dumps(ps[0].to_json()))["parent_id"] == ps[0].parent_id


def test_fallback_index_interface() -> None:
    doc = make_doc()
    idx = parents.build_fallback_index(doc)
    front = idx.sections[0]
    assert front.section_id == parents.FRONT_ID and len(front.item_refs) == 4
    design = next(s for s in idx.sections if s.title == "4. Study Design")
    assert [s.title for s in idx.children(design.section_id)] == ["4.1. Overall Design"]
    sub = idx.children(design.section_id)[0]
    assert [s.section_id for s in idx.path(sub.section_id)] == [design.section_id, sub.section_id]
    assert idx.section_of(sub.item_refs[0]).section_id == sub.section_id
    roles = {s.title: s.role for s in idx.sections}
    assert roles["1.1. Synopsis"] == "synopsis" and roles["5.1. Inclusion Criteria"] == "inclusion"
    assert roles["6. Schedule of Activities"] == "soa"
    again = parents.FallbackSectionIndex.from_json(json.loads(json.dumps(idx.to_json())))
    assert [s.section_id for s in again.sections] == [s.section_id for s in idx.sections]


# --------------------------------------------------------------------------- identifiers

def test_identifier_patterns() -> None:
    nct, eu, eudract, ind = (IDENTIFIER_PATTERNS[k] for k in ("nct", "eu_ct", "eudract", "ind"))
    assert [m.group(1) for m in nct.finditer("NCT #: NCT04573309; NCT1234567")] == ["NCT04573309"]
    assert [m.group(1) for m in eudract.finditer("EudraCT: 2020-001104-41")] == ["2020-001104-41"]
    assert not list(eudract.finditer("EU CT 2022-500123-12-00")), "an EU CT number is not a EudraCT number"
    assert [m.group(1) for m in eu.finditer("EU CT 2022-500123-12-00")] == ["2022-500123-12-00"]
    assert [m.group(1) for m in eu.finditer("2023-512345-27-01-02")] == ["2023-512345-27-01-02"]
    assert not list(eudract.finditer("dated 2019-03-12")) and not list(eu.finditer("2019-03-12"))
    for text, want in (("IND: 119006 Approval Date:", "119006"), ("US IND Number: 140,453", "140,453"),
                       ("IND number: 138488", "138488"), ("IND No. 12345", "12345"), ("IND #: 123456", "123456")):
        assert [m.group(1) for m in ind.finditer(text)] == [want], text
    assert not list(ind.finditer("INDICATION 123456")) and not list(ind.finditer("IND 2019"))


def test_registry_identifiers() -> None:
    doc = make_doc()
    ids = registry_identifiers(doc, {"protocol_id": "ZX-4417"})
    assert ids["nct"] == "NCT01234567" and ids["sources"]["nct"]["layer"] == "furniture"
    assert ids["others"]["nct"] == ["NCT07654321"], "a cited trial on page 9 is not the study's NCT"
    assert ids["eudract"] == "2019-003431-33" and ids["eu_ct"] == "2022-500123-12-00"
    assert ids["ind"] == "140,453" and ids["sources"]["ind"]["text"] == "US IND Number: 140,453"
    assert ids["sponsor_protocol_id"] == "ZX-4417"
    assert ids["sources"]["sponsor_protocol_id"]["text"] == "Zorvex Study Protocol ZX-4417"
    none = registry_identifiers(doc, {})
    assert none["sponsor_protocol_id"] is None  # no "Protocol Number:" label in this document


# --------------------------------------------------------------------------- context builder (parent mode)

class FakeTok:
    """HF-tokenizer-like: words are tokens."""

    def __call__(self, text, **_):
        if isinstance(text, list):
            return {"input_ids": [t.split() for t in text]}
        return {"input_ids": text.split()}

    def convert_tokens_to_ids(self, tokens):
        return tokens


class FakeRetriever:
    def __init__(self, chunks, cache_dir):
        self.chunks = chunks
        self.cache_dir = Path(cache_dir)

    def find_abbreviations(self, text):
        return {"SoA": "Schedule of Activities"} if "Schedule" in text else {}

    def definition_source(self, abbr):
        return "table"


def make_builder(doc, chunks, tmp, budget, **opts) -> ContextBuilder:
    meta = {"title": "A made-up study of zorvexumab", "protocol_id": "ZX-4417", "version_label": "Amendment 1",
            "version_date": "1 January 2026"}
    meta["identifiers"] = registry_identifiers(doc, meta)
    (Path(tmp) / "stats.json").write_text(json.dumps({"document": meta}), encoding="utf-8")
    b = object.__new__(ContextBuilder)  # no Docling JSON / HF tokenizer / tables.jsonl on disk
    b.retriever = FakeRetriever(chunks, tmp)
    b.doc = doc
    b.serializer = ProtocolSerializerProvider().get_serializer(doc)
    b.tok = FakeTok()
    b.tables, b.chain_of, b.glossary_tables = {}, {}, set()
    b.budget, b.mode, b.identity, b.section_expand = budget, "parents", True, True
    b.table_whole_max, b.dup_threshold, b.parent_bonus = 40, 0.5, 0.02
    b.residual_threshold = 0.9
    b.parent_min, b.parent_max = MIN, MAX
    b._store, b._identity = None, None
    for k, v in opts.items():
        setattr(b, k, v)
    return b


def hits_for(chunks, preds, scores=None) -> list[Hit]:
    out = []
    for i, pred in enumerate(preds):
        c = next(c for c in chunks if pred(c))
        out.append(Hit(c, scores[i] if scores else 0.9 - 0.1 * i, i + 1, i + 1))
    return out


def build(b, query, hits):
    return b.build(Result(query, query, {}, hits))


def test_identity_block() -> None:
    doc = make_doc()
    with tempfile.TemporaryDirectory() as tmp:
        b = make_builder(doc, make_chunks(doc), tmp, 2000)
        ctx = build(b, "What is the NCT number?", hits_for(b.retriever.chunks, [lambda c: "intro0" in c["text"]]))
        first = ctx.blocks[0]
        assert first.kind == "document" and "NCT01234567" in first.text
        assert '"ZX-4417 Zorvex NCT01234567"' in first.text and "header/footer" in first.text
        assert "Version: Amendment 1, 1 January 2026" in first.text
        assert ctx.render().startswith("[1] Document identity")
        b.identity = False
        assert build(b, "q", hits_for(b.retriever.chunks, [lambda c: "intro0" in c["text"]])).blocks[0].kind != "document"
    blk = identity_block({}, {"nct": None, "sources": {}})
    assert "ClinicalTrials.gov (NCT) number: none found" in blk.text


STORED_META = {"title": "A made-up study of zorvexumab", "acronym": "ZVX", "protocol_id": "ZX-4417",
               "version_label": "Amendment 1", "version_date": "1 January 2026"}
STORED_IDS = {"sponsor_protocol_id": "ZX-4417", "nct": None, "eudract": "2019-003431-33", "eu_ct": None, "ind": None,
              "who_utn": None, "others": {"nct": ["NCT07654321"]},
              "sources": {"sponsor_protocol_id": {"page": 1, "layer": "body", "text": "Zorvex Study Protocol ZX-4417"},
                          "eudract": {"page": 1, "layer": "table", "text": "EudraCT number: 2019-003431-33"}}}


def stored_field(value, method, source=None, **extra) -> dict:
    return {"value": value, "method": method, "source": source, **extra}


def model_field(value, source, rules=None, **extra) -> dict:
    """A field as the identity stage stores one a model chose (MODEL_METHOD, a source, "rules" = the rule field it
    replaced); the source is given as {"page", "layer", "text"} and carries the keys of a stored source besides."""
    src = {**source, "ref": "#/texts/1", "refs": ["#/texts/1"], "row": None, "section_id": "S1"} if source else None
    return {"value": value, "method": identity.MODEL_METHOD, "source": src,
            "rules": rules or {"value": None, "method": None}, **extra}


def test_identity_overlay() -> None:
    meta, ids = copy.deepcopy(STORED_META), copy.deepcopy(STORED_IDS)
    text = identity_block(meta, ids).text
    # no record, an empty record and a record of rules only (their values differ from the stats on purpose): equal
    # copies, not the arguments
    header = {"page": 5, "layer": "furniture", "text": "x"}
    rules_only = {"fields": {
        "official_title": stored_field("Another title", "cover-page label"), "acronym": stored_field("ANO", "stored"),
        "sponsor_protocol_id": stored_field("ZX-0000", "labelled pattern", header),
        "nct": stored_field("NCT00000009", "registry pattern", {"page": 2, "layer": "body", "text": "y"}),
        "version_label": stored_field("Amendment 9", "history table"),
        "version_date": stored_field("9 Sep 2029", "stored"),
        "sponsor": stored_field("Someone", "copyright line", None, verbatim="Someone")}}
    for record in (None, {}, {"fields": {}}, {"fields": None}, rules_only):
        got_meta, got_ids = retrieve.identity_overlay(meta, ids, record)
        assert (got_meta, got_ids) == (meta, ids) and got_meta is not meta and got_ids is not ids
        assert got_ids["sources"] is not ids["sources"] and got_ids["others"] is not ids["others"]
        assert identity_block(got_meta, got_ids).text == text
    got_meta, got_ids = retrieve.identity_overlay({}, {"nct": None, "sources": {}}, rules_only)  # empty stats
    assert (got_meta, got_ids) == ({}, {"nct": None, "sources": {}})
    assert retrieve.identity_overlay({}, {}, None) == ({}, {})  # no "sources" key stays absent
    # the fields a model chose replace the stats values; nothing else does
    body = lambda t: {"page": 1, "layer": "body", "text": t}  # noqa: E731
    record = {"fields": {
        **rules_only["fields"],
        "official_title": model_field("The title a model chose", body("Title line"),
                                      {"value": "A made-up study of zorvexumab", "method": "title page"}),
        "acronym": model_field("ZVX-2", body("Acronym line")),
        "version_label": model_field("Amendment 2", body("Version line")),
        "version_date": model_field("2 February 2026", body("Date line")),
        "sponsor_protocol_id": model_field("ZX-4418", {"page": 3, "layer": "furniture", "text": "ZX-4418 header"},
                                           {"value": "ZX-4417", "method": "labelled pattern"}),
        "nct": model_field("NCT01234567", {"page": 2, "layer": "table", "text": "NCT01234567 row"}),
        "eudract": model_field("2020-000001-11", None, {"value": "2019-003431-33", "method": "registry pattern"}),
        "who_utn": model_field("U1111-1234-5678", body("UTN line")),
        "ind": model_field("", body("empty values are not a choice")),
        "eu_ct": {**model_field("2022-500123-12-00", body("EU CT line")), "method": "registry pattern"},  # a rule's
    }}
    got_meta, got_ids = retrieve.identity_overlay(meta, ids, record)
    assert (meta, ids) == (STORED_META, STORED_IDS)  # the arguments are untouched
    assert got_meta == {**STORED_META, "title": "The title a model chose", "acronym": "ZVX-2",
                        "version_label": "Amendment 2", "version_date": "2 February 2026"}
    replaced = ("sources", "sponsor_protocol_id", "nct", "eudract", "who_utn")
    assert {k: v for k, v in got_ids.items() if k not in replaced} == {
        k: v for k, v in STORED_IDS.items() if k not in replaced}
    assert (got_ids["sponsor_protocol_id"], got_ids["nct"], got_ids["eudract"], got_ids["who_utn"]) == (
        "ZX-4418", "NCT01234567", "2020-000001-11", "U1111-1234-5678")
    assert got_ids["sources"] == {  # {"page", "layer", "text"} only; the source of a replaced value goes with it
        "sponsor_protocol_id": {"page": 3, "layer": "furniture", "text": "ZX-4418 header"},
        "nct": {"page": 2, "layer": "table", "text": "NCT01234567 row"},
        "who_utn": body("UTN line")}
    assert got_ids["ind"] is None and got_ids["eu_ct"] is None  # an empty model value and a rule's value: no change
    lines = identity_block(got_meta, got_ids).text.splitlines()
    none_found = "none found in the document text (body, tables, headers/footers)"
    assert lines == [
        "Title: The title a model chose", "Acronym: ZVX-2",
        'Sponsor protocol number: ZX-4418 (source p. 3, header/footer: "ZX-4418 header")',
        'ClinicalTrials.gov (NCT) number: NCT01234567 (source p. 2, table: "NCT01234567 row")',
        "EudraCT number: 2020-000001-11", f"EU CT number: {none_found}", f"IND number: {none_found}",
        'WHO Universal Trial Number: U1111-1234-5678 (source p. 1: "UTN line")',
        "Version: Amendment 2, 2 February 2026"], lines
    assert identity_block(got_meta, got_ids).pages == [1, 2, 3]
    # without a "sources" key in the stats' identifiers, the model's sources create it
    got_meta, got_ids = retrieve.identity_overlay({}, {"nct": None}, {"fields": {"nct": record["fields"]["nct"]}})
    assert got_ids == {"nct": "NCT01234567",
                       "sources": {"nct": {"page": 2, "layer": "table", "text": "NCT01234567 row"}}}


def builder_with_sha(doc, chunks, tmp) -> ContextBuilder:
    """A fresh builder over a stats.json with a source_sha256: a stored identity record belongs to a Docling JSON."""
    b = make_builder(doc, chunks, tmp, 2000)
    stats = Path(tmp) / "stats.json"
    data = json.loads(stats.read_text(encoding="utf-8"))
    data["document"]["source_sha256"] = SHA
    stats.write_text(json.dumps(data), encoding="utf-8")
    return b


def test_identity_block_reads_the_stored_record() -> None:
    """The Document block of a context takes the fields a model chose from chunks/identity.json (the stats values
    otherwise); no file, a file of rules only, one of another Docling JSON, one built from other stats.json values
    (stale), a broken one and one of another schema give the block of today, token count included; the context is the
    block first, then the passages."""
    doc = make_doc()
    hit = lambda b: hits_for(b.retriever.chunks, [lambda c: "intro0" in c["text"]])  # noqa: E731
    with tempfile.TemporaryDirectory() as tmp:
        chunks = make_chunks(doc)
        b = builder_with_sha(doc, chunks, tmp)
        today = b._identity_block()
        assert not (Path(tmp) / identity.RECORD_FILE).exists()
        meta = json.loads((Path(tmp) / "stats.json").read_text(encoding="utf-8"))["document"]
        assert today.text == identity_block(meta, meta["identifiers"]).text
        plain = build(b, "What is the NCT number?", hit(b)).render()
        rules = {k: stored_field(**v) for k, v in {  # the rules' record of this made-up document
            "official_title": {"value": meta["title"], "method": "title page"},
            "nct": {"value": "NCT01234567", "method": "registry pattern",
                    "source": {"page": 1, "layer": "furniture", "text": "ZX-4417 Zorvex NCT01234567"}},
            # a rule's value is never laid over the stats: this one differs on purpose, the block stays
            "acronym": {"value": "ANO", "method": "stored"},
            "version_label": {"value": "Amendment 9", "method": "history table"},
        }.items()}
        for what, write in (
                ("rules only", lambda: identity.write_record(tmp, {"fields": rules}, source_sha256=SHA, meta=meta)),
                ("another document", lambda: identity.write_record(
                    tmp, {"fields": {"acronym": model_field("OTHER", None)}}, source_sha256="0123456789abcdef",
                    meta=meta, model="a/model", prompt_version="x", llm={})),
                ("other stats values (stale)", lambda: identity.write_record(
                    tmp, {"fields": {"acronym": model_field("STALE", None)}}, source_sha256=SHA,
                    meta={**meta, "version_label": "Amendment 99"}, model="a/model", prompt_version="x", llm={})),
                ("not JSON", lambda: (Path(tmp) / identity.RECORD_FILE).write_text("{not json", encoding="utf-8")),
                ("another schema", lambda: (Path(tmp) / identity.RECORD_FILE).write_text(
                    json.dumps({"schema": 99, "fields": {}}), encoding="utf-8"))):
            write()
            b = builder_with_sha(doc, chunks, tmp)
            got = b._identity_block()
            assert got.text == today.text and got.tokens == today.tokens and got.pages == today.pages, what
            assert build(b, "What is the NCT number?", hit(b)).render() == plain, what
        # a model's fields
        cited = "Results of an earlier trial (NCT07654321) informed the dose."
        fields = {
            "official_title": model_field("The title a model chose", {"page": 1, "layer": "body", "text": "Zorvex"}),
            "acronym": model_field("ZVX-2", {"page": 1, "layer": "body", "text": "Zorvex Study Protocol ZX-4417"}),
            "nct": model_field("NCT07654321", {"page": 9, "layer": "body", "text": cited},
                               {"value": "NCT01234567", "method": "registry pattern"}),
        }
        identity.write_record(tmp, {"fields": fields}, source_sha256=SHA, meta=meta, model="a/model",
                              prompt_version="x", llm={})
        b = builder_with_sha(doc, chunks, tmp)
        got = b._identity_block()
        lines = got.text.splitlines()
        assert lines[:2] == ["Title: The title a model chose", "Acronym: ZVX-2"]
        assert f'ClinicalTrials.gov (NCT) number: NCT07654321 (source p. 9: "{cited}")' in lines
        assert "NCT01234567" not in got.text
        gone = [x for x in today.text.splitlines() if x not in lines]  # the rules' title and NCT lines are replaced
        assert len(gone) == 2 and gone[0] == "Title: A made-up study of zorvexumab"
        assert gone[1].startswith("ClinicalTrials.gov (NCT) number: NCT01234567 (source ")
        assert "Sponsor protocol number: ZX-4417" in got.text and "Version: Amendment 1, 1 January 2026" in got.text
        assert 9 in got.pages and got.tokens == b.count(got.render(0)) != today.tokens
        ctx = build(b, "What is the NCT number?", hit(b))
        assert ctx.blocks[0].kind == "document" and ctx.blocks[0].text == got.text
        assert ctx.render().startswith(f"[1] Document identity (title page, headers/footers) | Document identity | "
                                       f"pp. {min(got.pages)}-9\n")
        # the builder keeps its own block: a later call returns a copy of it
        got.text = "changed"
        assert b._identity_block().text == "\n".join(lines)


def test_identity_record_given_to_the_builder() -> None:
    """ContextBuilder(identity_record=...) (production_builder passes it on) replaces the builder's own read of
    chunks/identity.json: a record is used and the file is not read, None means "no usable record" (the stats values),
    not given means the builder loads the file itself; a mode clone keeps the record."""
    doc = make_doc()
    with tempfile.TemporaryDirectory() as tmp:
        chunks = make_chunks(doc)
        builder_with_sha(doc, chunks, tmp)  # stats.json with a source_sha256
        (Path(tmp) / "doc.json").write_text("{}", encoding="utf-8")  # any file: no retriever sha to check it against
        retriever = FakeRetriever(chunks, tmp)
        retriever.source_sha256 = None
        ser = ProtocolSerializerProvider().get_serializer(doc)
        src = {"page": 1, "layer": "body", "text": "Zorvex Study Protocol ZX-4417"}
        on_disk = {"fields": {"acronym": model_field("FILE-ACR", src)}}
        given = {"fields": {"acronym": model_field("GIVEN-ACR", src)}}
        meta = json.loads((Path(tmp) / "stats.json").read_text(encoding="utf-8"))["document"]
        identity.write_record(tmp, on_disk, source_sha256=SHA, meta=meta, model="a/model", prompt_version="x",
                              llm={})

        shared = dict(doc=doc, serializer=ser, tok=FakeTok(), tables={})

        def block(**kw) -> str:
            return ContextBuilder(retriever, Path(tmp) / "doc.json", **shared, **kw)._identity_block().text

        today = identity_block(meta, meta["identifiers"]).text.splitlines()
        assert block().splitlines() == today[:1] + ["Acronym: FILE-ACR"] + today[1:]  # not given: loads the file
        assert block(identity_record=None).splitlines() == today  # given, none usable: the file is not read
        assert "Acronym: GIVEN-ACR" in block(identity_record=given) and "FILE-ACR" not in block(identity_record=given)
        b = retrieve.production_builder(retriever, Path(tmp) / "doc.json", identity_record=given, **shared)
        clone = b.with_mode("chunks")  # made before the block is first built
        assert b.mode == "parents" and "Acronym: GIVEN-ACR" in b._identity_block().text
        assert "Acronym: GIVEN-ACR" in clone._identity_block().text
        loads = retrieve.production_builder(retriever, Path(tmp) / "doc.json", **shared)
        assert "Acronym: FILE-ACR" in loads._identity_block().text


def test_greedy_fill_within_budget() -> None:
    doc = make_doc()
    with tempfile.TemporaryDirectory() as tmp:
        chunks = make_chunks(doc)
        preds = [lambda c: "des0x0" in c["text"], lambda c: "inc5x0" in c["text"], lambda c: "Assessment 9:" in c["text"],
                 lambda c: "obj0" in c["text"], lambda c: "des4x0" in c["text"], lambda c: "dose0" in c["text"]]
        for budget in (60, 90, 120, 160, 220, 300, 400, 800):
            b = make_builder(doc, chunks, tmp, budget)
            ctx = build(b, "dose and design", hits_for(chunks, preds))
            assert ctx.tokens <= budget, (budget, ctx.tokens)
            assert ctx.tokens == sum(b_.tokens for b_ in ctx.blocks) + (len("- SoA: Schedule of Activities".split())
                                                                       if ctx.definitions else 0)
            keys = [x.parent_id for x in ctx.blocks if x.parent_id]
            assert len(keys) == len(set(keys)), "a parent appears once"
        big = build(make_builder(doc, chunks, tmp, 5000), "dose and design", hits_for(chunks, preds))
        assert not big.dropped and not big.fallbacks


def test_degradation_order() -> None:
    doc = make_doc()
    with tempfile.TemporaryDirectory() as tmp:
        chunks = make_chunks(doc)
        hit = hits_for(chunks, [lambda c: "des2x0" in c["text"]])
        # A larger MAX makes "4. Study Design" one section parent of 6 chunks, so +-1 is a strict subset.
        b = make_builder(doc, chunks, tmp, 10_000, parent_max=200)
        ident = b._identity_block().tokens
        store = b.store
        p = store.parent_of(hit[0].chunk)
        assert p.kind == "section" and p.breadcrumb == "4. Study Design"
        mine = [c for c in chunks if store.parent_of(c) is p]
        assert len(mine) >= 4, "the parent holds the neighbouring chunks"
        ctx = build(b, "design", hit)
        assert [x.kind for x in ctx.blocks] == ["document", "parent"] and "retrieved: «des2x0" in ctx.blocks[1].note
        kinds = []
        for budget in range(ident + 5, ident + p.tokens + 40, 3):
            b.budget = budget
            ctx = build(b, "design", hit)
            kinds.append(ctx.blocks[-1].kind if len(ctx.blocks) > 1 else "dropped")
            assert ctx.tokens <= budget
            if kinds[-1] == "neighbours":
                assert [c for c in ctx.blocks[-1].chunk_ids] == [c["chunk_id"] for c in mine[1:4]]
        order = ["dropped", "child", "neighbours", "parent"]
        seen = [k for i, k in enumerate(kinds) if i == 0 or kinds[i - 1] != k]
        assert seen == order, seen
        assert build(b, "design", hit).fallbacks == [] and b.budget >= ident + p.tokens
        b.budget = ident + p.tokens - 5
        assert build(b, "design", hit).fallbacks, "a degraded parent is reported"


def test_table_policy() -> None:
    doc = make_doc()
    with tempfile.TemporaryDirectory() as tmp:
        chunks = make_chunks(doc)
        b = make_builder(doc, chunks, tmp, 10_000)
        one = hits_for(chunks, [lambda c: "Assessment 9:" in c["text"]])
        p = b.store.parent_of(one[0].chunk)
        assert p.kind == "table" and p.tokens > b.table_whole_max
        ctx = build(b, "When is assessment 9 done?", one)
        rows = ctx.blocks[-1]
        assert rows.kind == "table_window", "a big table with one retrieved part: rows +-1"
        labels = [ln.split(":")[0] for ln in rows.text.splitlines() if ln.startswith("Assessment ")]
        # the retrieved part holds rows 9-16; +-1 row adds 8 and 17
        assert labels == [f"Assessment {i}" for i in range(8, 18)], labels
        two = hits_for(chunks, [lambda c: "Assessment 9:" in c["text"], lambda c: "Assessment 1:" in c["text"]])
        ctx = build(b, "When is assessment 9 done?", two)
        whole = ctx.blocks[-1]
        assert whole.kind == "table_whole" and "| Assessment 24 |" in whole.text, "two parts retrieved: whole"
        b.budget = ctx.tokens - 10  # the whole table no longer fits: rows 1-17 (+-1 row) instead
        ctx = build(b, "When is assessment 9 done?", two)
        assert ctx.blocks[-1].kind == "table_window" and ctx.fallbacks and ctx.tokens <= b.budget
        labels = [ln.split(":")[0] for ln in ctx.blocks[-1].text.splitlines() if ln.startswith("Assessment ")]
        assert labels == [f"Assessment {i}" for i in range(1, 18)], labels
        b.budget = 10_000
        small = hits_for(chunks, [lambda c: "Level -1" in c["text"]])
        ctx = build(b, "dose levels", small)
        assert ctx.blocks[-1].kind in ("parent", "merged") and "| Level -1 | 20 mg |" in ctx.blocks[-1].text


def test_near_duplicates_prefer_body() -> None:
    doc = make_doc()
    with tempfile.TemporaryDirectory() as tmp:
        chunks = make_chunks(doc)
        b = make_builder(doc, chunks, tmp, 10_000)
        syn = lambda c: c["text"].startswith("The Zorvex trial") and b.store.chunk_role(c) == "synopsis"  # noqa: E731
        body = lambda c: c["text"].startswith("The Zorvex trial") and b.store.chunk_role(c) != "synopsis"  # noqa: E731
        hits = hits_for(chunks, [syn, body], scores=[0.95, 0.90])
        assert b.store.parent_of(hits[0].chunk) is not b.store.parent_of(hits[1].chunk)
        ctx = build(b, "What is the design?", hits)
        roles = [b.store.by_id[x.parent_id].role for x in ctx.blocks if x.parent_id and x.kind != "residual"]
        assert "synopsis" not in roles and ctx.duplicates, (roles, ctx.duplicates)
        texts = [x.text for x in ctx.blocks if x.parent_id]
        assert sum(t.count("The Zorvex trial is a randomized") for t in texts) == 1
        rest = [x for x in ctx.blocks if x.kind == "residual"]
        assert len(rest) == 1 and rest[0].text == "Enrollment closes after 120 participants are dosed.",             "the synopsis copy's own extra sentence is kept"
        assert parents.containment(parents.shingles("a b c d e f"), parents.shingles("x a b c d e f y")) == 1.0
        assert parents.containment(set(), {("a",)}) == 0.0


def test_section_expansion() -> None:
    doc = make_doc()
    with tempfile.TemporaryDirectory() as tmp:
        chunks = make_chunks(doc)
        b = make_builder(doc, chunks, tmp, 10_000)
        hits = hits_for(chunks, [lambda c: "inc1x0" in c["text"], lambda c: "inc11x0" in c["text"]])
        pa, pb = (b.store.parent_of(h.chunk) for h in hits)
        assert pa is not pb, "the inclusion list is split over two parents"
        ctx = build(b, "List all inclusion criteria.", hits)
        full = [x for x in ctx.blocks if x.kind == "section_full"]
        assert full and ctx.expanded and all(f"Criterion {i} " in full[0].text for i in range(1, 13))
        assert [x.kind for x in ctx.blocks] == ["document", "section_full"], "covered parents are not repeated"
        ctx = build(b, "What does criterion 1 require?", hits)
        assert not ctx.expanded, "not an enumeration question"
        b.budget = b._identity_block().tokens + 70  # the whole section does not fit
        ctx = build(b, "List all inclusion criteria.", hits)
        assert not ctx.expanded and ctx.tokens <= b.budget
        one = hits_for(chunks, [lambda c: "inc1x0" in c["text"]])
        b.budget = 10_000
        assert not build(b, "List all inclusion criteria.", one).expanded, "a single hit never expands"
    for q, want in (("List the inclusion criteria.", True), ("Which activities are performed at Day 8?", True),
                    ("What dose is given every 3 weeks?", False), ("What is the NCT number?", False),
                    ("At which visits is the ECG done?", True), ("What happens at each visit?", True)):
        assert parents.enumeration_like(q) == want, q


def test_chunk_mode_unchanged() -> None:
    blk = Block("text", "body", "1. Intro", [3], ["c1"])
    assert blk.render(2) == "[2] Text | 1. Intro | p. 3\nbody"
    blk = Block("table_rows", "rows", "", [3, 5], ["c1"])
    assert blk.render(1).startswith("[1] Table (retrieved rows only; complete table exceeded the budget) | Front matter | pp. 3-5")


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
