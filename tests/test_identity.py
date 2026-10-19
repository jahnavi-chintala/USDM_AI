"""Self-test for identity.py (the identity record with the source item of every value): `python tests/test_identity.py`.

Public protocols only (output_public/<name>/protocol.json + chunks/stats.json; one whose output is absent is skipped
with a note), plus small made-up documents. No network, no pytest.

Checks: build_identity reproduces the stored stats.json values (titles, acronym, identifiers, version) and the sponsor
names; every Source resolves to its item (ref -> item, text contained in the item's or table row's text, page, layer,
section); doc_metadata / registry_identifiers keep their outputs and with_refs only adds ref / row; a meta of None
gives the same record as the stored meta; synthetic cases for the methods of each rule, a cover label whose value is
on the next line (two refs), "property of Eli Lilly and Company", a history table, and stored values the rules do not
produce (method "stored", located in the first item holding the value).
"""

import copy
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docling_core.types.doc import (BoundingBox, ContentLayer, DocItemLabel, DoclingDocument, ProvenanceItem,
                                    RefItem, Size, TableCell, TableData, TableItem)

import parents
from chunk_protocol import doc_metadata, registry_identifiers
from identity import build_identity

PUBLIC = {  # name -> expected sponsor name
    "LZZT_CDISC_Pilot": "Eli Lilly and Company", "Lilly_NCT03421379": "Eli Lilly and Company",
    "Alexion_NCT04573309": "Alexion Pharmaceuticals, Inc", "Bayer_NCT04304508": "Bayer AG",
    "PrECOG_NCT01797120": "PrECOG, LLC", "Sanofi_NCT03637764": "Sanofi",
}
FIELDS = ("official_title", "brief_title", "acronym", "detected_title", "sponsor", "sponsor_protocol_id", "nct",
          "eudract", "eu_ct", "ind", "who_utn", "version_label", "amendment", "version_date")
KINDS = ("sponsor_protocol_id", "nct", "eudract", "eu_ct", "ind", "who_utn")
SOURCE_KEYS = {"page", "layer", "text", "ref", "refs", "row", "section_id"}
METHODS = {"cover-page label", "title page", "running header", "labelled pattern", "header/footer frequency",
           "history table", "registry pattern", "copyright line", "'property of' line",
           "company-name pattern on the title pages (unverified)", "stored"}
W, H = 612.0, 792.0


# --------------------------------------------------------------------------- helpers

def norm(s: str | None) -> str:
    return " ".join((s or "").split())


_LOADED: dict[str, tuple | None] = {}
_SECTION: dict[int, tuple] = {}  # id(doc) -> (doc, function); the document is kept so its id stays unique


def load(name: str):
    """(doc, stats["document"], protocol.json path) of a public output, or None when it is not there."""
    if name not in _LOADED:
        base = ROOT / "output_public" / name
        path, stats = base / "protocol.json", base / "chunks" / "stats.json"
        if path.exists() and stats.exists():
            _LOADED[name] = (DoclingDocument.load_from_json(path),
                             json.loads(stats.read_text(encoding="utf-8"))["document"], path)
        else:
            print(f"  note: {name}: output_public output absent, skipped")
            _LOADED[name] = None
    return _LOADED[name]


def section_fn(doc):
    """parents.section_index(doc).section_of as an id function (computed once per document)."""
    if id(doc) not in _SECTION:
        index = parents.section_index(doc)
        _SECTION[id(doc)] = (doc, lambda ref, index=index: parents._sid(index.section_of(ref)))
    return _SECTION[id(doc)][1]


def row_text(item: TableItem, row: int) -> str:
    return " ".join(norm(c.text) for c in {id(c): c for c in item.data.grid[row]}.values() if c.text)


def check_source(doc, src: dict, section_of, where: str) -> None:
    """The source's ref resolves, its text is contained in the item (or table row), page and layer match."""
    assert set(src) == SOURCE_KEYS, (where, sorted(src))
    assert src["layer"] in ("body", "furniture", "table"), (where, src["layer"])
    assert src["refs"] and src["refs"][0] == src["ref"], (where, src["refs"], src["ref"])
    texts = []
    for k, ref in enumerate(src["refs"]):
        item = RefItem(cref=ref).resolve(doc)
        row = src["row"] if k == 0 else None
        if isinstance(item, TableItem):
            assert row is not None and src["layer"] == "table", (where, ref, row)
            texts.append(row_text(item, row))
        else:
            assert row is None and src["layer"] == ("furniture" if item.content_layer == ContentLayer.FURNITURE
                                                    else "body"), (where, ref, src["layer"])
            texts.append(norm(item.text))
        if k == 0:
            pages = {p.page_no for p in item.prov}
            assert src["page"] in pages or (src["page"] is None and not pages), (where, src["page"], pages)
    if len(src["refs"]) == 1:
        assert src["text"].strip("…").strip() in texts[0], (where, src["text"], texts[0])
    else:  # a value on the line after its label: the items' texts joined
        assert src["text"] == " / ".join(texts), (where, src["text"], texts)
    assert src["section_id"] == section_of(src["ref"]), (where, src["section_id"])


def sources_of(ident: dict):
    for key, f in ident["fields"].items():
        if f and f["source"]:
            yield key, f["source"]


def prov(page: int, t: float = 50, b: float = 70) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=50, t=t, r=300, b=b), charspan=(0, 0))


def make_doc(items: list[tuple[str, str, int]], pages: int = 3,
             name: str = "syn") -> tuple[DoclingDocument, list[str]]:
    """A document from (kind, text, page): kind h = heading, t = text, header, footer; the items' refs."""
    doc = DoclingDocument(name=name)
    for p in range(1, pages + 1):
        doc.add_page(page_no=p, size=Size(width=W, height=H))
    refs = []
    for kind, text, page in items:
        if kind == "h":
            item = doc.add_heading(text, prov=prov(page))
        elif kind == "t":
            item = doc.add_text(DocItemLabel.TEXT, text, prov=prov(page))
        else:
            label = DocItemLabel.PAGE_HEADER if kind == "header" else DocItemLabel.PAGE_FOOTER
            item = doc.add_text(label, text, prov=prov(page), content_layer=ContentLayer.FURNITURE)
        refs.append(item.self_ref)
    return doc, refs


def add_table(doc: DoclingDocument, rows: list[list[str]], page: int) -> TableItem:
    cells = [TableCell(text=t, start_row_offset_idx=r, end_row_offset_idx=r + 1, start_col_offset_idx=c,
                       end_col_offset_idx=c + 1, column_header=r == 0)
             for r, row in enumerate(rows) for c, t in enumerate(row)]
    return doc.add_table(data=TableData(num_rows=len(rows), num_cols=len(rows[0]), table_cells=cells),
                         prov=prov(page))


def running_header_doc(headers: list[str], footers: list[str], title_page: list[tuple[str, str]], pages: int = 6):
    """Headers / footers on every page ("#" = page number) before the page-1 items, as in test_downstream."""
    items: list[tuple[str, str, int]] = []
    for p in range(1, pages + 1):
        items += [("header", t.replace("#", str(p)), p) for t in headers]
        items += [("footer", t.replace("#", str(p)), p) for t in footers]
    items += [(k, t, 1) for k, t in title_page]
    return make_doc(items, pages)


TITLE = ("t", "A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With Condition Y")


def field(ident: dict, key: str) -> dict:
    f = ident["fields"][key]
    assert f is not None, key
    return f


# --------------------------------------------------------------------------- public protocols

def test_public_values_equal_stats() -> None:
    """Values equal stats.json's; sponsor names; every rule reproduces its stored value (no "stored" method)."""
    n = 0
    for name, sponsor in PUBLIC.items():
        if (got := load(name)) is None:
            continue
        doc, stored, _ = got
        ident = build_identity(doc, stored, section_of=section_fn(doc))
        json.dumps(ident)  # plain JSON types only
        assert ident["schema"] == 1 and list(ident["fields"]) == list(FIELDS), name
        cover = ident["cover_page"]
        assert (ident["fields"]["detected_title"] or {}).get("value") == stored["title"], name
        official = cover["official_title"]["value"] if "official_title" in cover else stored["title"]
        assert (ident["fields"]["official_title"] or {}).get("value") == official, name
        brief = cover["brief_title"]["value"] if "brief_title" in cover else None
        assert (ident["fields"]["brief_title"] or {}).get("value") == brief, name
        acronym = cover["acronym"]["value"] if "acronym" in cover else stored["acronym"]
        assert (ident["fields"]["acronym"] or {}).get("value") == acronym, name
        ids = stored["identifiers"]
        for kind in KINDS:
            assert (ident["fields"][kind] or {}).get("value") == ids[kind], (name, kind)
        for key in ("version_label", "amendment", "version_date"):
            assert (ident["fields"][key] or {}).get("value") == stored[key], (name, key)
        sp = field(ident, "sponsor")
        assert sp["value"] == sponsor and sp["verbatim"] and sp["source"], (name, sp["value"])
        assert ident["others"] == (ids.get("others") or {}), name
        for key, f in ident["fields"].items():
            if f:
                assert f["method"] in METHODS and f["method"] != "stored", (name, key, f["method"])
                assert set(f) == ({"value", "method", "source", "verbatim"} if key == "sponsor"
                                  else {"value", "method", "source"}), (name, key, sorted(f))
        n += 1
    print(f"  {n} public documents")


def test_public_sources_resolve() -> None:
    """Every Source with a ref resolves to an item (or table row) that holds the source text; every identifier
    the stored stats name has its source item."""
    n_sources = n_ids = n_found = 0
    for name in PUBLIC:
        if (got := load(name)) is None:
            continue
        doc, stored, _ = got
        sec = section_fn(doc)
        ident = build_identity(doc, stored, section_of=sec)
        n_doc = n_doc_ids = 0
        for key, src in sources_of(ident):
            check_source(doc, src, sec, f"{name}.{key}")
            n_doc += 1
            n_doc_ids += key in KINDS
        for key, c in ident["cover_page"].items():
            assert set(c) == {"value", "page", "text", "refs"} and c["refs"], (name, key)
        found = [k for k in KINDS if stored["identifiers"][k]]
        assert n_doc_ids == len(found), (name, found)  # a found identifier always has its source item
        assert all(ident["fields"][k]["source"]["ref"] for k in found), name
        print(f"  {name}: {n_doc} sources checked, identifiers {n_doc_ids}/{len(found)}")
        n_sources, n_ids, n_found = n_sources + n_doc, n_ids + n_doc_ids, n_found + len(found)
    print(f"  total: {n_sources} sources checked, identifier sources {n_ids}/{n_found}")


def test_public_metadata_unchanged() -> None:
    """doc_metadata / registry_identifiers give the stored values; with_refs=True only adds ref / row."""
    for name in PUBLIC:
        if (got := load(name)) is None:
            continue
        doc, stored, path = got
        meta = doc_metadata(doc, path)
        want = {k: v for k, v in stored.items() if k != "identifiers"}
        assert json.dumps(meta, ensure_ascii=False) == json.dumps(want, ensure_ascii=False), name
        ids = registry_identifiers(doc, meta)
        assert json.dumps(ids, ensure_ascii=False) == json.dumps(stored["identifiers"], ensure_ascii=False), name
        with_refs = registry_identifiers(doc, meta, with_refs=True)
        stripped = json.loads(json.dumps(with_refs))
        for src in stripped["sources"].values():
            assert "ref" in src and "row" in src, name
            del src["ref"], src["row"]
        assert json.dumps(stripped, ensure_ascii=False) == json.dumps(ids, ensure_ascii=False), name
        for kind, src in with_refs["sources"].items():
            assert src["ref"].startswith(("#/texts/", "#/tables/")), (name, kind)
            assert (src["row"] is not None) == (src["layer"] == "table"), (name, kind, src)


def test_public_meta_none_gives_same_record() -> None:
    for name in PUBLIC:
        if (got := load(name)) is None:
            continue
        doc, stored, _ = got
        sec = section_fn(doc)
        assert build_identity(doc, section_of=sec) == build_identity(doc, stored, section_of=sec), name
    # the default section_of is parents.section_index(doc).section_of
    got = load("LZZT_CDISC_Pilot")
    if got:
        doc, stored, _ = got
        assert build_identity(doc, stored) == build_identity(doc, stored, section_of=section_fn(doc))


# --------------------------------------------------------------------------- synthetic documents

def test_cover_label_value_on_next_line() -> None:
    doc, refs = make_doc([
        ("t", "Official Title: A Phase 3 Study of Drug X in Adults With Condition Y", 1),
        ("t", "Short Title: Drug X in Condition Y", 1),
        ("t", "Study Acronym: DRUGX-3", 1),
        ("t", "Sponsor Name:", 1),
        ("t", "Acme Pharma, Inc., 12 Main Street, Springfield, 12345", 1),
    ])
    ident = build_identity(doc, section_of=lambda ref: "S1")
    cover = ident["cover_page"]
    assert cover["sponsor"] == {"value": "Acme Pharma, Inc., 12 Main Street, Springfield, 12345", "page": 1,
                                "text": "Sponsor Name: / Acme Pharma, Inc., 12 Main Street, Springfield, 12345",
                                "refs": [refs[3], refs[4]]}, cover["sponsor"]
    sp = field(ident, "sponsor")
    assert sp["value"] == "Acme Pharma, Inc" and sp["verbatim"] == cover["sponsor"]["value"]
    assert sp["method"] == "cover-page label"
    assert sp["source"] == {"page": 1, "layer": "body", "text": cover["sponsor"]["text"], "ref": refs[3],
                            "refs": [refs[3], refs[4]], "row": None, "section_id": "S1"}, sp["source"]
    off = field(ident, "official_title")
    assert off["value"] == "A Phase 3 Study of Drug X in Adults With Condition Y"
    assert off["method"] == "cover-page label"
    assert off["source"]["ref"] == refs[0] and off["source"]["refs"] == [refs[0]] and off["source"]["layer"] == "body"
    assert field(ident, "brief_title")["source"]["ref"] == refs[1]
    acr = field(ident, "acronym")
    assert acr["value"] == "DRUGX-3" and acr["method"] == "cover-page label" and acr["source"]["ref"] == refs[2]
    # the detected title is the title-page rule's, not the cover label's
    det = field(ident, "detected_title")
    assert det["method"] == "title page" and det["source"]["ref"] == refs[0]
    for key, src in sources_of(ident):
        check_source(doc, src, lambda ref: "S1", key)


def test_cover_label_followed_by_a_label() -> None:
    """A label with no value on its own line takes the next line only when that line is not a label itself (a cover
    label, or a short line ending with a colon): "Approval Date:" then "Sponsor Signatory:" gives no approval date."""
    long_value = "A Phase 3, Randomized, Double-Blind Study of Drug X Versus Placebo in Adults With Condition Y:"
    doc, refs = make_doc([
        ("t", "Approval Date:", 1),
        ("t", "Sponsor Signatory:", 1),                         # a label of its own: not the approval date
        ("t", "Sponsor Name:", 1),
        ("t", "Legal Registered Address:", 1),                  # a cover label: not the sponsor
        ("t", "Regulatory Agency Identifier Number(s):", 1),    # not the address
        ("t", "Protocol Number:", 1),
        ("t", "ZX-4417", 1),                                    # a value on the next line still works
        ("t", "Short Title:", 1),
        ("t", "Drug X in Condition Y: the DRUGX-3 study", 1),   # a colon inside a value
        ("t", "Official Title:", 1),
        ("t", long_value, 1),                                   # over 60 characters before its colon: a value
    ])
    cover = build_identity(doc, section_of=lambda ref: None)["cover_page"]
    assert set(cover) == {"sponsor_protocol_number", "brief_title", "official_title"}, sorted(cover)
    assert cover["sponsor_protocol_number"] == {"value": "ZX-4417", "page": 1, "text": "Protocol Number: / ZX-4417",
                                                "refs": [refs[5], refs[6]]}, cover["sponsor_protocol_number"]
    assert cover["brief_title"]["value"] == "Drug X in Condition Y: the DRUGX-3 study"
    assert cover["official_title"]["value"] == long_value and cover["official_title"]["refs"] == [refs[9], refs[10]]
    # the public protocols: no cover-page value is a label ("Approval Date:" / "Sponsor Signatory:" on Alexion)
    for name in PUBLIC:
        if (got := load(name)) is None:
            continue
        doc, stored, _ = got
        cover = build_identity(doc, stored, section_of=section_fn(doc))["cover_page"]
        assert not [k for k, c in cover.items() if c["value"].rstrip().endswith((":", "\uff1a"))], (name, sorted(cover))
        if name == "Alexion_NCT04573309":
            assert "approval_date" not in cover and cover["sponsor"]["value"] == "Alexion Pharmaceuticals, Inc."
            assert cover["official_title"]["value"].endswith("Treated with ALXN1840"), cover["official_title"]


def test_property_of_company_line() -> None:
    doc, refs = make_doc([("h", "CLINICAL STUDY PROTOCOL", 1),
                          ("t", "This is the property of Eli Lilly and Company and may not be reproduced.", 1)])
    sp = field(build_identity(doc, section_of=lambda ref: None), "sponsor")
    assert sp["value"] == "Eli Lilly and Company" and sp["verbatim"] == "Eli Lilly and Company", sp
    assert sp["method"] == "'property of' line" and sp["source"]["ref"] == refs[1] and sp["source"]["layer"] == "body"
    # a footer line: the source layer is the item's
    doc, refs = make_doc([("t", "Study text", 1), ("footer", "Property of Eli Lilly and Company", 2)])
    sp = field(build_identity(doc, section_of=lambda ref: None), "sponsor")
    assert sp["value"] == "Eli Lilly and Company" and sp["source"]["layer"] == "furniture", sp
    assert sp["source"]["ref"] == refs[1] and sp["source"]["page"] == 2
    # "&" joins names too; a lowercase word ends the name
    doc, _ = make_doc([("t", "Property of Smith & Nephew Group is protected.", 1)])
    assert field(build_identity(doc, section_of=lambda ref: None), "sponsor")["value"] == "Smith & Nephew Group"


def test_copyright_and_company_pattern() -> None:
    doc, refs = make_doc([("t", "Copyright 2020 by Zorvex Therapeutics. All rights reserved.", 1)])
    sp = field(build_identity(doc, section_of=lambda ref: None), "sponsor")
    assert sp["method"] == "copyright line" and sp["value"].startswith("Zorvex Therapeutics"), sp
    assert sp["source"]["ref"] == refs[0]
    doc, refs = make_doc([("t", "Study drug supplied by Zorvex Therapeutics to all sites.", 1)])
    sp = field(build_identity(doc, section_of=lambda ref: None), "sponsor")
    assert sp["method"] == "company-name pattern on the title pages (unverified)", sp
    assert sp["value"] == "Zorvex Therapeutics" and sp["source"]["ref"] == refs[0]
    doc, _ = make_doc([("t", "Nothing to see here, a plain sentence.", 1)])
    assert build_identity(doc, section_of=lambda ref: None)["fields"]["sponsor"] is None


def test_running_header_methods_and_refs() -> None:
    """The item each value was read from, with the method of the rule: pages 1-3 running header, labelled
    pattern, header/footer frequency."""
    # style 1: id, amendment and date from separate header items
    doc, refs = running_header_doc(
        ["PF-00000000 (Drugname)", "Protocol C1234567", "Final Protocol Amendment 4, 5 January 2021"], [],
        [("h", "STUDYNAME-3"), TITLE])
    sec = lambda ref: "S0"  # noqa: E731
    ident = build_identity(doc, section_of=sec)
    pid, label, amend, date = (field(ident, k) for k in ("sponsor_protocol_id", "version_label", "amendment",
                                                          "version_date"))
    assert (pid["value"], label["value"], amend["value"], date["value"]) == ("C1234567", "Amendment 4", "4",
                                                                            "5 January 2021")
    assert pid["method"] == label["method"] == amend["method"] == date["method"] == "running header"
    assert pid["source"]["ref"] == refs[1] and pid["source"]["layer"] == "furniture" and pid["source"]["page"] == 1
    assert label["source"] == amend["source"] == date["source"]
    assert label["source"]["ref"] == refs[2] and label["source"]["section_id"] == "S0"
    acr = field(ident, "acronym")
    assert acr["value"] == "STUDYNAME-3" and acr["method"] == "title page" and acr["source"]["layer"] == "body"
    assert acr["source"]["page"] == 1 and acr["source"]["section_id"] == "S0"
    assert field(ident, "detected_title")["value"] == TITLE[1]
    for key, src in sources_of(ident):
        check_source(doc, src, sec, key)
    # style 2: labelled id and version in a header, date in a footer
    doc, refs = running_header_doc(["Protocol No.: MK-1234-005 Version 3.0", "05-Jan-2021"],
                                   ["Confidential", "Page # of 6"], [("h", "CLINICAL STUDY PROTOCOL"), TITLE])
    ident = build_identity(doc, section_of=sec)
    assert (field(ident, "sponsor_protocol_id")["value"], field(ident, "sponsor_protocol_id")["method"]) == \
        ("MK-1234-005", "labelled pattern")
    assert (field(ident, "version_label")["value"], field(ident, "version_label")["method"]) == \
        ("Version 3.0", "labelled pattern")
    assert field(ident, "version_label")["source"]["ref"] == refs[0]  # the page-1 header "Protocol No.: ... 3.0"
    assert field(ident, "version_date")["value"] == "05-Jan-2021" and \
        field(ident, "version_date")["method"] == "labelled pattern"
    assert field(ident, "version_date")["source"]["ref"] == refs[1] and ident["fields"]["amendment"] is None
    for key, src in sources_of(ident):
        check_source(doc, src, sec, key)
    # style 3: an unlabelled id repeated in the headers; the date in a footer
    doc, refs = running_header_doc(["CABC123A2301", "Amendment 2"], ["March 14, 2021", "#"], [TITLE])
    ident = build_identity(doc, section_of=sec)
    assert (field(ident, "sponsor_protocol_id")["value"], field(ident, "sponsor_protocol_id")["method"]) == \
        ("CABC123A2301", "header/footer frequency")
    assert field(ident, "sponsor_protocol_id")["source"]["ref"] == refs[0]
    assert field(ident, "version_date")["value"] == "March 14, 2021"
    assert field(ident, "version_date")["source"]["layer"] == "furniture"
    for key, src in sources_of(ident):
        check_source(doc, src, sec, key)


def test_header_value_spanning_two_items() -> None:
    """A running-header pattern that crosses two header items lists both."""
    doc, refs = make_doc([("header", "Final Protocol", 1), ("header", "Amendment", 1), ("header", "9", 1),
                          ("t", "x", 1)], pages=1)
    ident = build_identity(doc, section_of=lambda ref: None)
    label = field(ident, "version_label")
    assert label["value"] == "Amendment 9" and label["method"] == "running header", label
    assert label["source"]["refs"] == [refs[1], refs[2]] and label["source"]["ref"] == refs[1]
    assert label["source"]["text"] == "Amendment / 9"
    check_source(doc, label["source"], lambda ref: None, "span")


def test_history_table_version() -> None:
    doc, refs = make_doc([("h", "PROTOCOL", 1), ("t", "Drug X in adults with condition Y, a long study title here", 1)],
                         pages=3)
    table = add_table(doc, [["Version", "Date", "Summary"], ["Original protocol", "5 Jan 2020", "Initial release"],
                            ["Amendment 1", "3 Mar 2021", "Changed the dose"]], page=2)
    ident = build_identity(doc, section_of=lambda ref: "S3")
    label, amend, date = (field(ident, k) for k in ("version_label", "amendment", "version_date"))
    assert (label["value"], amend["value"], date["value"]) == ("Amendment 1", "1", "3 Mar 2021")
    assert label["method"] == amend["method"] == date["method"] == "history table"
    src = label["source"]
    assert src == amend["source"] == date["source"]
    assert src["layer"] == "table" and src["ref"] == table.self_ref and src["row"] == 2 and src["page"] == 2
    assert src["section_id"] == "S3" and "Amendment 1" in src["text"] and "3 Mar 2021" in src["text"]
    check_source(doc, src, lambda ref: "S3", "history")
    # an unamended protocol: only the "Original protocol" row
    doc, _ = make_doc([("t", "Plain page one text", 1)], pages=2)
    add_table(doc, [["Version", "Date"], ["Original protocol", "5 Jan 2020"]], page=2)
    ident = build_identity(doc, section_of=lambda ref: None)
    assert field(ident, "version_label")["value"] == "Original protocol" and ident["fields"]["amendment"] is None
    assert field(ident, "version_date")["value"] == "5 Jan 2020"


def test_identifier_sources_have_refs() -> None:
    """A registry id found in a running-text line and in a table row: the item / the table with its row."""
    doc, refs = make_doc([
        ("t", "Registered at ClinicalTrials.gov, identifier NCT01234567 as the study's own.", 1),
        ("t", "Protocol Number: ZX-4417", 1)], pages=2)
    table = add_table(doc, [["Registry", "Number"], ["EudraCT number", "2019-003431-33"]], page=2)
    sec = lambda ref: "S" + ref.split("/")[-1]  # noqa: E731
    ident = build_identity(doc, section_of=sec)
    nct, eud, pid = field(ident, "nct"), field(ident, "eudract"), field(ident, "sponsor_protocol_id")
    assert nct["source"]["ref"] == refs[0] and nct["source"]["row"] is None and nct["method"] == "registry pattern"
    assert nct["source"]["section_id"] == "S0"
    assert eud["source"]["layer"] == "table" and eud["source"]["ref"] == table.self_ref and eud["source"]["row"] == 1
    assert eud["source"]["text"] == "EudraCT number 2019-003431-33" and eud["source"]["section_id"] == "S0"
    assert pid["value"] == "ZX-4417" and pid["method"] == "labelled pattern", pid
    assert pid["source"]["ref"] == refs[1]
    for key, src in sources_of(ident):
        check_source(doc, src, sec, key)


def test_stored_value_the_rules_do_not_produce() -> None:
    """A stored value that the current rules do not give (an older output, a command-line override): method
    "stored", the source is the first item holding it, preferring the stored identifier source's page / layer."""
    doc, refs = make_doc([
        ("t", "Study of a new drug in adults", 1),                       # 0: no value in it
        ("t", "Reference ZX-4417 appears in the title page text", 1),   # 1: first item holding the id (body, p. 1)
        ("t", "Notes on Amendment 7 of this document", 2),             # 2: holds the stored version
        ("t", "Body line on page 3 also mentions ZX-4417", 3),          # 3: the id again (body, p. 3)
        ("header", "Header ZX-4417 page 3", 3),                         # 4: the id again (furniture, p. 3)
    ], pages=4)
    table = add_table(doc, [["Registry", "Number"], ["Registered", "NCT00000000"]], page=4)
    meta = {
        "doc_name": "syn", "protocol_id": "ZX-4417", "acronym": None, "title": None, "amendment": "7",
        "version_date": "9 Sep 2019", "version_label": "Amendment 7",
        "identifiers": {"sponsor_protocol_id": "ZX-4417", "nct": "NCT00000000", "eu_ct": None,
                        "eudract": "2019-003431-33", "ind": None, "who_utn": None,
                        "sources": {"sponsor_protocol_id": {"page": 3, "layer": "furniture", "text": "Header ZX-4417"},
                                    "nct": {"page": 4, "layer": "table", "text": "Registered NCT00000000"}}},
    }
    sec = lambda ref: "S1"  # noqa: E731
    ident = build_identity(doc, meta, section_of=sec)
    pid = field(ident, "sponsor_protocol_id")
    assert pid["value"] == "ZX-4417" and pid["method"] == "stored", pid
    assert pid["source"]["ref"] == refs[4] and pid["source"]["layer"] == "furniture" and pid["source"]["page"] == 3
    label, amend = field(ident, "version_label"), field(ident, "amendment")
    assert label["value"] == "Amendment 7" and label["method"] == "stored" and label["source"]["ref"] == refs[2], label
    assert amend["method"] == "stored" and amend["source"]["ref"] == refs[2]
    nct = field(ident, "nct")  # a table row: the table's ref and the row index
    assert nct["method"] == "stored" and nct["source"]["ref"] == table.self_ref and nct["source"]["row"] == 1
    assert (nct["source"]["layer"], nct["source"]["page"]) == ("table", 4)
    assert nct["source"]["text"] == "Registered NCT00000000"
    assert field(ident, "eudract")["method"] == "stored" and field(ident, "eudract")["source"] is None  # in no item
    assert field(ident, "version_date")["value"] == "9 Sep 2019" and field(ident, "version_date")["source"] is None
    for key, src in sources_of(ident):
        check_source(doc, src, sec, key)
    # the stored source's page and layer decide among several items; without one, the first in document order
    meta["identifiers"]["sources"] = {"sponsor_protocol_id": {"page": 3, "layer": "body", "text": "x"}}
    assert field(build_identity(doc, meta, section_of=sec), "sponsor_protocol_id")["source"]["ref"] == refs[3]
    meta["identifiers"]["sources"] = {}
    assert field(build_identity(doc, meta, section_of=sec), "sponsor_protocol_id")["source"]["ref"] == refs[1]
    # a stored value the rules do reproduce keeps its rule's method
    doc, refs = running_header_doc(["Protocol C1234567", "Final Protocol Amendment 4, 5 January 2021"], [],
                                   [("h", "STUDYNAME-3"), TITLE])
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "syn.json"
        doc.save_as_json(path)
        meta = doc_metadata(doc, path)
    meta["identifiers"] = registry_identifiers(doc, meta)
    ident = build_identity(doc, meta, section_of=sec)
    assert all(f["method"] != "stored" for f in ident["fields"].values() if f)
    assert ident == build_identity(doc, section_of=sec)
    meta["version_date"] = "6 January 2021"  # one value overridden
    ident = build_identity(doc, meta, section_of=sec)
    assert field(ident, "version_date")["method"] == "stored" and field(ident, "version_date")["source"] is None
    assert field(ident, "version_label")["method"] == "running header"


def test_stored_short_value_is_a_whole_token() -> None:
    """A short stored value does not attach to an item that merely contains it (the digit 4 is in NCT03421379);
    the stored amendment is located through the stored version label, so both fields name the same item."""
    doc, refs = make_doc([
        ("t", "Registered as NCT03421379 and ZX-4412 on the title page", 1),   # 0: contains 4 and 9, never alone
        ("t", "Dose level 4 opens in cohort B", 1),                            # 1: "4" alone, unrelated to the version
        ("t", "Notes on Amendment 4 of this document", 2),                    # 2: holds "Amendment 4"
        ("t", "Version 4.0 is the layout version", 3),                         # 3: "4" alone as well, later
    ])
    sec = lambda ref: "S1"  # noqa: E731
    meta = {"doc_name": "syn", "protocol_id": None, "acronym": None, "title": None, "amendment": "4",
            "version_date": None, "version_label": "Amendment 4",
            "identifiers": {**{k: None for k in KINDS}, "sources": {}}}
    ident = build_identity(doc, meta, section_of=sec)
    label, amend = field(ident, "version_label"), field(ident, "amendment")
    assert label["method"] == amend["method"] == "stored"
    assert label["source"]["ref"] == refs[2] and amend["source"] == label["source"], (label, amend)
    check_source(doc, amend["source"], sec, "amendment")
    # no label: the number itself is searched, as a whole token (not the digit inside NCT03421379 / ZX-4412)
    ident = build_identity(doc, {**meta, "version_label": None}, section_of=sec)
    assert ident["fields"]["version_label"] is None
    assert field(ident, "amendment")["source"]["ref"] == refs[1], field(ident, "amendment")
    # a label the document does not hold: both fields have no source (not the item holding the digit 9)
    ident = build_identity(doc, {**meta, "amendment": "9", "version_label": "Amendment 9"}, section_of=sec)
    assert field(ident, "version_label")["source"] is None and field(ident, "amendment")["source"] is None
    ident = build_identity(doc, {**meta, "amendment": "9", "version_label": None}, section_of=sec)
    assert field(ident, "amendment")["source"] is None
    # other stored values: "ZX-44" is not in "ZX-4412", "ZX-4412" is
    for stored_id, ref in (("ZX-44", None), ("ZX-4412", refs[0])):
        ids = {**meta["identifiers"], "sponsor_protocol_id": stored_id}
        ident = build_identity(doc, {**meta, "protocol_id": stored_id, "identifiers": ids}, section_of=sec)
        pid = field(ident, "sponsor_protocol_id")
        assert pid["method"] == "stored" and (pid["source"] or {}).get("ref") == ref, pid


def test_minimal_document_and_section_of() -> None:
    doc, _ = make_doc([("t", "Nothing here", 1)], pages=1)
    ident = build_identity(doc, section_of=lambda ref: None)
    assert ident["schema"] == 1 and list(ident["fields"]) == list(FIELDS)
    assert all(v is None for v in ident["fields"].values()), ident["fields"]
    assert ident["cover_page"] == {} and ident["others"] == {}
    json.dumps(ident)
    # section_of is used for every source; None stays None
    doc, refs = make_doc([("t", "Official Title: A Phase 3 Study of Drug X in Adults With Condition Y", 1)], pages=1)
    assert field(build_identity(doc, section_of=lambda r: "S9"), "official_title")["source"]["section_id"] == "S9"
    assert field(build_identity(doc, section_of=lambda r: None), "official_title")["source"]["section_id"] is None


def test_others_listed() -> None:
    doc, _ = make_doc([("t", "Protocol Number: ZX-4417", 1), ("t", "Registered NCT01234567 for the study", 1)],
                      pages=30)
    for p in range(10, 20):
        doc.add_text(DocItemLabel.TEXT, "Compare with NCT07654321 and NCT07654322", prov=prov(p))
    ident = build_identity(doc, section_of=lambda r: None)
    assert ident["others"] == {"nct": ["NCT07654321", "NCT07654322"]}, ident["others"]
    assert field(ident, "nct")["value"] == "NCT01234567"


def test_stored_record_round_trip() -> None:
    """write_record / load_record: deterministic bytes, LF line ends, no temporary file left; the meta stamp right
    after source_sha256; a record of another Docling JSON, of other stats.json values (or without a stamp: "stale",
    checked last), another schema or shape, a broken file or a missing one is refused with its reason."""
    from identity import (FIELDS as RECORD_FIELDS, MODEL_METHOD, RECORD_FILE, SCHEMA, load_record, meta_stamp,
                          write_record)
    from pipeline_version import PIPELINE_VERSION

    doc, _ = make_doc([("t", "Official Title: A Phase 3 Study of Drug X in Adults With Condition Y", 1),
                       ("t", "Protocol Number: ZX-4417", 1)], pages=1)
    rec = build_identity(doc, section_of=lambda r: None)
    meta = {"doc_name": "syn", "version_label": "Amendment 4", "identifiers": {"nct": None, "sources": {}},
            "title": "A Phase 3 Study of Drug X \u2013 adults"}
    stamp = hashlib.sha256(json.dumps(meta, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    assert meta_stamp(meta) == stamp and meta_stamp(dict(reversed(list(meta.items())))) == stamp
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        assert load_record(d, "abc") == (None, "missing")
        assert load_record(d, "abc", meta) == (None, "missing")
        path = write_record(d, rec, source_sha256="abc", meta=meta)
        first = path.read_bytes()
        assert path.name == RECORD_FILE and b"\r" not in first and [p.name for p in d.iterdir()] == [RECORD_FILE]
        write_record(d, rec, source_sha256="abc", meta=copy.deepcopy(meta))
        assert path.read_bytes() == first
        data, why = load_record(d, "abc")
        assert why is None and data["schema"] == SCHEMA and data["pipeline_version"] == PIPELINE_VERSION
        assert list(data)[:3] == ["schema", "source_sha256", "meta_sha256"] and data["meta_sha256"] == stamp
        assert data["model"] is None and data["prompt_version"] is None and data["llm"] is None
        assert {k: data[k] for k in ("fields", "cover_page", "others")} == json.loads(json.dumps(
            {k: rec[k] for k in ("fields", "cover_page", "others")}))
        assert list(data["fields"]) == list(RECORD_FIELDS)
        assert load_record(d, "other") == (None, "other_document")
        assert load_record(d, None) == (None, "other_document")
        # the stats.json values: the same ones load, other ones (or a record without the stamp) are stale
        assert load_record(d, "abc", copy.deepcopy(meta)) == (data, None)
        assert load_record(d, "abc", {**meta, "version_label": "Amendment 5"}) == (None, "stale")
        assert load_record(d, "abc", {}) == (None, "stale")
        assert load_record(d, "other", {**meta, "version_label": "x"}) == (None, "other_document")  # checked first
        unstamped = {k: v for k, v in data.items() if k != "meta_sha256"}
        path.write_text(json.dumps(unstamped), encoding="utf-8")
        assert load_record(d, "abc", meta) == (None, "stale") and load_record(d, "abc")[1] is None
        path.write_text(json.dumps({**unstamped, "schema": SCHEMA + 1}), encoding="utf-8")
        assert load_record(d, "abc", meta) == (None, "schema")  # the shape before the stamp
        # with a model: prompt_version and llm are kept, a model field carries its method
        chosen = json.loads(json.dumps(rec))
        chosen["fields"]["acronym"] = {"value": "ZX", "method": MODEL_METHOD, "source": None,
                                       "rules": {"value": None, "method": None}}
        write_record(d, chosen, source_sha256="abc", meta=meta, model="openrouter:x/y", prompt_version="1",
                     llm={"accepted": ["acronym"]})
        data, _ = load_record(d, "abc", meta)
        assert data["model"] == "openrouter:x/y" and data["prompt_version"] == "1"
        assert data["llm"] == {"accepted": ["acronym"]} and data["fields"]["acronym"]["method"] == MODEL_METHOD
        def with_field(key, value):
            return json.dumps({**data, "fields": {**data["fields"], key: value}})

        good_source = data["fields"]["official_title"]["source"]
        assert good_source is not None
        for broken, why in (("{not json", "unreadable"), (json.dumps([1]), "schema"),
                            (json.dumps({**data, "schema": SCHEMA + 1}), "schema"),
                            (json.dumps({**data, "fields": {"acronym": None}}), "schema"),
                            (json.dumps({**data, "others": []}), "schema"),
                            (json.dumps({**data, "others": {"nct": "NCT01234567"}}), "schema"),
                            (json.dumps({**data, "cover_page": {"phase": "3"}}), "schema"),
                            (with_field("acronym", "ZX"), "schema"),
                            (with_field("acronym", {"value": "", "method": "m", "source": None}), "schema"),
                            (with_field("acronym", {"value": "ZX", "method": "m", "source": {"page": 1}}), "schema"),
                            (with_field("acronym", {"value": "ZX", "method": "m", "source": None, "rules": "x"}),
                             "schema"),
                            (with_field("sponsor", {"value": "Acme", "method": "m", "source": good_source}), "schema"),
                            (with_field("sponsor", {"value": "Acme", "method": "m", "source": None,
                                                    "verbatim": "Acme"}), "schema")):
            path.write_text(broken, encoding="utf-8")
            assert load_record(d, "abc") == (None, why), broken[:80]
        # a well-formed model sponsor is accepted
        path.write_text(with_field("sponsor", {"value": "Acme", "method": MODEL_METHOD, "source": good_source,
                                               "verbatim": "Acme", "rules": {"value": None, "method": None}}),
                        encoding="utf-8")
        assert load_record(d, "abc")[1] is None


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
