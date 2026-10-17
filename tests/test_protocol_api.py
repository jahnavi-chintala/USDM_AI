"""Self-test for protocol_api.py: `python tests/test_protocol_api.py` (no pytest).

Public protocols only: output_public/LZZT_CDISC_Pilot and output_public/Lilly_NCT03421379 (their USDM keys are in
public_protocols/); the identity checks run on all six public outputs. Offline: OPENROUTER_OFFLINE=1 is forced, so search() runs on an eval question whose query
embedding and rerank are cached (reports/eval/public/<name>.json), an uncached question must raise OfflineCacheMiss,
and a fake client (embed_fn / rerank_fn) covers the uncached path without any network call.

Checks: the source-sha guard; every tool and its provenance; the sponsor's "property of" line and source; document_info
and search's identity block read the identity record (identity.py), not a search: every identifier source and every
identity-block ref is the item holding the quoted line, Lilly's running header is its protocol id source, document_info
values equal the stored ones, the record is built once and each call returns its own copy; outline ids; read_section of the inclusion / exclusion roles holds every criterion of the USDM key (usdm_gold's reader);
continuation cursors lose nothing and keep with_blocks; find_sections hints; table chains are joined; get_soa with and
without soa.py; quote_check positive / normalised / table / cross-item / furniture / negative / section-scoped /
elided (in order, within 2,000 characters; with 3+ parts a later occurrence in the window completes a chain) / figure
OCR text pages / uncapped count by layer / document text before AI-generated text at any tier / header-footer slots;
the reading-order repair (Lilly p. 26); tool schemas are valid JSON Schema; call_tool never raises and types its own
errors; the CLI.
"""

import copy
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import protocol_api as api  # noqa: E402

LZZT = ROOT / "output_public" / "LZZT_CDISC_Pilot"
LILLY = ROOT / "output_public" / "Lilly_NCT03421379"
ALEXION = ROOT / "output_public" / "Alexion_NCT04573309"
PUBLIC = {  # the six public protocols -> the sponsor name document_info gives
    "LZZT_CDISC_Pilot": "Eli Lilly and Company", "Lilly_NCT03421379": "Eli Lilly and Company",
    "Alexion_NCT04573309": "Alexion Pharmaceuticals, Inc", "Bayer_NCT04304508": "Bayer AG",
    "PrECOG_NCT01797120": "PrECOG, LLC", "Sanofi_NCT03637764": "Sanofi",
}
SOURCE_KEYS = {"page", "layer", "text", "ref", "refs", "row", "section_id"}
PROV_KEYS = {"section_id", "section_number", "title", "pages", "refs"}
CASES = []
_DOCS: dict[str, api.ProtocolDocument] = {}


def case(fn):
    CASES.append(fn)
    return fn


def doc(path: Path) -> api.ProtocolDocument:
    if str(path) not in _DOCS:
        _DOCS[str(path)] = api.ProtocolDocument.open(path)
    return _DOCS[str(path)]


def has_prov(d: dict, where: str = "") -> None:
    assert isinstance(d.get("provenance"), dict), f"{where}: no provenance in {sorted(d)}"
    missing = PROV_KEYS - set(d["provenance"])
    assert not missing, f"{where}: provenance lacks {missing}"


def public(*paths: Path) -> bool:
    """True when these output_public/ folders exist; otherwise the test says it is skipped."""
    missing = [p.name for p in paths if not (p / "chunks" / "stats.json").is_file()]
    if missing:
        print(f"   (skipped: output_public/{', '.join(missing)} absent)")
    return not missing


def raises(exc, fn, *a, **kw) -> Exception:
    try:
        fn(*a, **kw)
    except exc as e:  # noqa: PERF203
        return e
    raise AssertionError(f"{fn.__name__} did not raise {exc.__name__}")


def cached_question(name: str) -> str:
    """An eval question of reports/eval/public/<name>.json whose query embedding and rerank are both cached."""
    from retrieve import CANDIDATES_PER_RANKER, RERANK_MODEL, Retriever

    qs = json.loads((ROOT / "reports" / "eval" / "public" / f"{name}.json").read_text(encoding="utf-8"))
    r = Retriever(ROOT / "output_public" / name / "chunks" / "chunks.jsonl")
    qc = json.loads((r.cache_dir / "query_embedding_cache.json").read_text(encoding="utf-8"))
    rc = json.loads((r.cache_dir / "rerank_cache.json").read_text(encoding="utf-8"))
    model = r.meta["model"]
    cached = []
    for q in qs:
        text = r.query_text(r.expand(q["question"])[0])
        key = hashlib.sha256(f"{model}|{text}".encode()).hexdigest()
        if key not in qc:
            continue
        hy = r.search(q["question"], np.asarray(qc[key], dtype=np.float32), k=len(r.chunks), mode="hybrid")
        pool = [h.chunk for h in hy.hits if h.dense_rank <= CANDIDATES_PER_RANKER or h.bm25_rank <= CANDIDATES_PER_RANKER]
        rk = hashlib.sha256(json.dumps([RERANK_MODEL, hy.expanded_query, [c["content_hash"] for c in pool]]).encode()).hexdigest()
        if rk in rc:
            cached.append(q["question"])
    if not cached:
        raise AssertionError(f"no eval question of {name} has a cached embedding and rerank")
    return next((q for q in cached if "study arms" in q), cached[0])


def key_criteria(name: str, category: str) -> list:
    import usdm_gold

    st = usdm_gold.load_study(name)
    return [c for c in st.of("EligibilityCriterion") if category in (c.attrs.get("category") or "").lower()]


_FAKE_DOCS: dict[str, api.ProtocolDocument] = {}
_FAKE_SEARCH: dict[str, dict] = {}


def public_docs() -> dict[str, Path]:
    """The public protocols whose output_public/ folder is there (a missing one is named and skipped)."""
    base = ROOT / "output_public"
    present = {n: base / n for n in PUBLIC if (base / n / "chunks" / "stats.json").is_file()}
    if len(present) < len(PUBLIC):
        print(f"   (output_public/{', '.join(sorted(set(PUBLIC) - set(present)))} absent: not checked)")
    return present


def keep_order(query: str, chunks: list, model: str) -> list[tuple[int, float]]:
    """A stand-in reranker: keeps the retrieval order."""
    return [(i, 1.0 / (i + 1)) for i in range(len(chunks))]


def fake_doc(name: str) -> api.ProtocolDocument:
    """A public protocol opened with stand-in embedding / rerank functions: search runs without any network."""
    if name not in _FAKE_DOCS:
        path = ROOT / "output_public" / name
        dim = np.load(path / "chunks" / "embeddings.npy", mmap_mode="r").shape[1]

        def embed_fn(text):
            seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
            return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)

        _FAKE_DOCS[name] = api.ProtocolDocument.open(path, embed_fn=embed_fn, rerank_fn=keep_order)
    return _FAKE_DOCS[name]


def fake_search(name: str) -> dict:
    if name not in _FAKE_SEARCH:
        _FAKE_SEARCH[name] = fake_doc(name).search("What is the planned number of participants?", k=1)
    return _FAKE_SEARCH[name]


def item_problem(d: api.ProtocolDocument, ref: str, page: int | None, layer: str | None, quoted: str,
                 row: int | None = None) -> str:
    """"ok" when the item at ref is on the page, in the layer (body / furniture / table) and holds the quoted line
    (a table: the row's text, or every row's when no row is given); else what differs."""
    from docling_core.types.doc import ContentLayer, TableItem

    item = d._resolve(ref)
    if isinstance(item, TableItem):
        rows = [row] if row is not None else range(len(item.data.grid))
        text = " ".join(" ".join(c.text.split()) for r in rows for c in {id(c): c for c in item.data.grid[r]}.values()
                        if c.text)
        got = "table"
    else:
        text = " ".join(item.text.split())
        got = "furniture" if item.content_layer == ContentLayer.FURNITURE else "body"
    problems = []
    if page is not None and page not in api._pages_of(item):
        problems.append(f"page {api._pages_of(item)} != {page}")
    if layer is not None and got != layer:
        problems.append(f"layer {got} != {layer}")
    line = " ".join(quoted.split()).strip("…").strip()
    if line not in text and line.replace(" | ", " ") not in text.replace(" | ", " "):
        problems.append("the item does not hold the quoted line")
    return "; ".join(problems) or "ok"


def identity_lines(text: str) -> list[tuple[str, int | None, str, str]]:
    """(label, page, layer, quoted line) of every '(source ...)' line of search's identity block."""
    out = []
    for line in text.splitlines():
        m = re.search(r'\(source(?: p\. (\d+))?(?:, (header/footer|table))?: "(.+)"\)$', line)
        if m:
            out.append((line.split(":", 1)[0], int(m.group(1)) if m.group(1) else None,
                        {"header/footer": "furniture", "table": "table"}.get(m.group(2), "body"), m.group(3)))
    return out


def coverage(text: str, markdown_tokens: set) -> float:
    from usdm_gold import content_tokens

    ct = content_tokens(text)
    return sum(t in markdown_tokens for t in ct) / max(1, len(ct))


# =========================================================================== open / identity

@case
def test_open_and_source_sha_guard():
    d = doc(LZZT)
    stats = json.loads((LZZT / "chunks" / "stats.json").read_text(encoding="utf-8"))
    assert d.source_sha256 == stats["document"]["source_sha256"]
    assert d.doc_path.name == "protocol.json" and d.chunks_dir.name == "chunks"
    assert api.ProtocolDocument.open(LZZT / "chunks").out_dir == LZZT  # a chunks folder is accepted too
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "chunks").mkdir()
        shutil.copy(LZZT / "chunks" / "stats.json", tmp / "chunks" / "stats.json")
        (tmp / "protocol.json").write_bytes((LZZT / "protocol.json").read_bytes() + b" ")  # a different JSON
        e = raises(RuntimeError, api.ProtocolDocument.open, tmp)
        assert "is not the JSON these chunks were made from" in str(e)
        raises(FileNotFoundError, api.ProtocolDocument.open, tmp / "nothing_here")


@case
def test_document_info():
    info = doc(LZZT).document_info()
    json.dumps(info)
    has_prov(info, "document_info")
    ids = info["identifiers"]
    assert ids["sponsor_protocol_id"]["value"] == "H2Q-MC-LZZT"
    assert ids["sponsor_protocol_id"]["source"]["page"] == 1
    assert ids["sponsor_protocol_id"]["source"]["text"] == "Protocol H2Q-MC-LZZT(c)"
    assert ids["sponsor_protocol_id"]["source"]["ref"].startswith("#/texts/")
    assert ids["nct"] is None
    assert "Xanomeline" in info["titles"]["official_title"]["value"]
    assert "Lilly" in info["sponsor"]["name"]
    assert info["page_count"] == 97 and info["section_count"] == len(doc(LZZT).index.sections)
    assert info["soa_table_count"] >= 1 and all(t["table_id"].startswith("#/tables/") for t in info["soa_tables"])
    assert "inclusion" in info["roles_found"] and "Sponsor protocol number: H2Q-MC-LZZT" in info["identity_block"]

    info = doc(LILLY).document_info()
    nct = info["identifiers"]["nct"]
    assert nct["value"] == "NCT03421379" and "NCT03421379" in nct["source"]["text"]
    item = doc(LILLY)._resolve(nct["source"]["ref"])
    assert "NCT03421379" in item.text  # the source ref points at the verbatim line
    assert info["version"]["date"]["value"] == "05-Dec-2017"
    assert info["cover_page"]["approval_date"]["value"] == "05-Dec-2017"
    assert "Eli Lilly" in info["sponsor"]["name"]
    if repaired_in_postprocessing(LILLY):  # the moves are in the document; nothing left to repair at read time
        assert info["reading_order_repairs"] == []
    else:
        assert any(r["to_section"] == "S6.1" for r in info["reading_order_repairs"])


def repaired_in_postprocessing(path: Path) -> bool:
    """structure.repair_reading_order ran in post-processing (it moved Lilly p. 26 into 6.1)."""
    dec = json.loads((path / "protocol.decisions.json").read_text(encoding="utf-8"))
    if "reading_order" not in dec:
        return False
    assert any(e.get("to_section") == "S6.1" and e["action"] == "moved" for e in dec["reading_order"]), \
        dec["reading_order"]
    return True


@case
def test_sponsor_property_line_and_source():
    # "and" / "&" inside the name leave the space the next word needs
    import identity

    for text, name in (("This document is the property of Eli Lilly and Company.", "Eli Lilly and Company"),
                       ("Property of the Acme & Sons Group, all rights reserved", "Acme & Sons Group")):
        assert identity._PROPERTY.search(text).group("name").strip(" .,") == name, text
    if not public(LILLY, ALEXION):
        return
    d = doc(LILLY)
    sp = d.document_info()["sponsor"]
    assert sp["name"] == sp["value"] == "Eli Lilly and Company" and sp["method"] == "'property of' line"
    src = sp["source"]
    assert set(src) == SOURCE_KEYS
    assert src["layer"] == "body" and src["section_id"] == d._section_of(src["ref"]) is not None
    assert src["refs"] == [src["ref"]] and src["row"] is None and "property of Eli Lilly and Company" in src["text"]
    assert sp["provenance"]["refs"] == src["refs"] and sp["provenance"]["pages"] == [src["page"]]
    assert sp["provenance"]["section_id"] == src["section_id"]
    a = doc(ALEXION)
    sp = a.document_info()["sponsor"]
    assert sp["method"] == "cover-page label" and sp["source"]["layer"] == "body"
    assert sp["source"]["section_id"] == a._section_of(sp["source"]["ref"]) is not None
    assert sp["name"] == "Alexion Pharmaceuticals, Inc" and sp["name"] in sp["value"] != sp["name"]  # value: verbatim
    # a sponsor line in a page header/footer is read from the furniture layer
    from docling_core.types.doc import ContentLayer

    item = d._resolve(src["ref"])
    saved = item.content_layer
    try:
        item.content_layer = ContentLayer.FURNITURE
        loc = identity.detect_sponsor(d.doc, {})["loc"]
        assert (loc["ref"], loc["layer"]) == (src["ref"], "furniture")
    finally:
        item.content_layer = saved


@case
def test_document_info_sources_resolve_and_identity_block_refs():
    """Every identifier source and every search identity-block ref is the item holding the quoted line (page, layer,
    item text): the record names the item a value was read from, no search for it; 16/16 and 16/16 on the public set."""
    docs = public_docs()
    if not docs:
        return
    from retrieve import IDENTITY_FIELDS

    n_sources = n_lines = n_ok_sources = n_ok_lines = 0
    for name in docs:
        d = fake_doc(name)
        info = d.document_info()
        sourced = [k for k, _ in IDENTITY_FIELDS if info["identifiers"][k]]
        for key in sourced:
            v = info["identifiers"][key]
            assert v["source"] and set(v["source"]) == SOURCE_KEYS and v["method"] and v["label"], (name, key)
            n_sources += 1
            src = v["source"]
            bad = item_problem(d, src["ref"], src["page"], src["layer"], src["text"], src["row"])
            n_ok_sources += bad == "ok"
            assert bad == "ok", (name, key, src["ref"], bad)
        ident = fake_search(name)["identity"]
        lines = identity_lines(ident["text"])
        assert len(lines) == len(sourced), (name, lines, sourced)  # one "(source ...)" line per identifier
        refs = []
        for (label, page, layer, quoted), key in zip(lines, sourced):
            ref = info["identifiers"][key]["source"]["ref"]  # the line's ref is its identifier's source item
            bad = item_problem(d, ref, page, layer, quoted)
            n_lines += 1
            n_ok_lines += bad == "ok"
            assert bad == "ok", (name, label, ref, bad)
            refs.append(ref)
        assert ident["provenance"]["refs"] == list(dict.fromkeys(refs)), name
        first = next((s for s in (d._section_of(r) for r in refs) if s), None)
        assert ident["provenance"]["section_id"] == first, name
    print(f"   identifier sources {n_ok_sources}/{n_sources}, identity-block lines {n_ok_lines}/{n_lines}")
    assert n_sources == n_ok_sources and n_lines == n_ok_lines and n_sources == n_lines > 0


@case
def test_lilly_running_header_is_the_protocol_id_source():
    """Lilly's page-1 running header "I8R-JE-IGBJ(a) Clinical Pharmacology Protocol" (#/texts/0) is the source of the
    protocol id, not the title paragraph (#/texts/1) that holds the id too; the identity block is not placed in
    Appendix 1 (a table row there holds an id-like string)."""
    if not public(LILLY):
        return
    d = fake_doc("Lilly_NCT03421379")
    info = d.document_info()
    pid = info["identifiers"]["sponsor_protocol_id"]
    assert pid["value"] == "I8R-JE-IGBJ"
    assert pid["source"]["ref"] == "#/texts/0" and pid["source"]["layer"] == "furniture" and pid["source"]["page"] == 1
    assert pid["source"]["text"] == "I8R-JE-IGBJ(a) Clinical Pharmacology Protocol"
    assert pid["source"]["section_id"] is None  # a page header belongs to no section
    assert "I8R-JE-IGBJ" in d._resolve("#/texts/0").text
    assert info["sponsor"]["name"] == "Eli Lilly and Company"
    prov = fake_search("Lilly_NCT03421379")["identity"]["provenance"]
    assert prov["refs"][0] == "#/texts/0" and prov["title"] != "Appendix 1"
    assert prov["section_id"] == d._section_of(info["identifiers"]["nct"]["source"]["ref"]) is not None


@case
def test_document_info_values_equal_the_stored_values():
    """Titles, identifiers, version, sponsor and cover page are the values of the stored stats.json and the cover
    page on the six public protocols (the source items changed, the values did not)."""
    docs = public_docs()
    if not docs:
        return
    from retrieve import IDENTITY_FIELDS, identity_block

    methods = {"cover-page label", "title page", "running header", "labelled pattern", "header/footer frequency",
               "history table", "registry pattern"}  # a rule's method; never "stored" on the public outputs
    for name in docs:
        d = fake_doc(name)
        info, stored = d.document_info(), d.meta
        ids, cover = stored["identifiers"], info["cover_page"]
        t = info["titles"]
        assert (t["detected_title"] or {}).get("value") == stored["title"], name
        assert (t["official_title"] or {}).get("value") == (cover["official_title"]["value"] if "official_title" in cover
                                                            else stored["title"]), name
        assert (t["brief_title"] or {}).get("value") == (cover["brief_title"]["value"] if "brief_title" in cover
                                                         else None), name
        assert (t["acronym"] or {}).get("value") == (cover["acronym"]["value"] if "acronym" in cover
                                                     else stored["acronym"]), name
        for key, label in IDENTITY_FIELDS:
            v = info["identifiers"][key]
            assert (v or {}).get("value") == ids[key], (name, key)
            if v:
                assert v["label"] == label and v["method"] in methods and v["source"], (name, key, v["method"])
        ver = info["version"]
        assert (ver["label"], ver["amendment"]) == (stored["version_label"], stored["amendment"]), name
        assert (ver["date"] or {}).get("value") == stored["version_date"], name
        for key, field_name in (("cover_amendment", "amendment"), ("cover_version", "version"),
                                ("cover_approval_date", "approval_date")):
            assert (ver[key] or {}).get("value") == (cover[field_name]["value"] if field_name in cover else None), \
                (name, key)
        for v in [*t.values(), ver["date"]]:  # every sourced title / date: value, method, source
            if v:
                assert set(v) == {"value", "method", "source"} and v["method"] in methods, (name, v)
                assert v["source"] is None or set(v["source"]) == SOURCE_KEYS, (name, v)
        assert info["sponsor"]["name"] == PUBLIC[name], name
        assert info["sponsor"]["name"] in info["sponsor"]["value"], name
        assert info["other_identifiers_cited"] == (ids.get("others") or {}), name
        assert info["identity_block"] == identity_block(stored, ids).text, name
        for key, c in cover.items():  # the cover page: label values read from the items they name
            assert set(c) == {"value", "page", "text", "refs"} and c["refs"], (name, key)
            assert c["text"] == " / ".join(" ".join(d._resolve(r).text.split()) for r in c["refs"]), (name, key)
        for key in ("cover_amendment", "cover_version", "cover_approval_date"):
            v = ver[key]
            if v:  # the shape of the cover values: no method, no row
                src = v["source"]
                assert set(v) == {"value", "source"}, (name, key)
                assert set(src) == {"page", "layer", "text", "refs", "ref", "section_id"}, (name, key)
                assert src["layer"] == "body" and src["ref"] == src["refs"][0], (name, key)
                assert src["section_id"] == d._section_of(src["ref"]), (name, key)
        assert info["provenance"]["pages"] and info["provenance"]["refs"], name


@case
def test_document_info_computed_once_and_copied():
    """The identity record is built once per open document (also for search); document_info is built once and
    each call returns its own copy with the warnings of that moment."""
    matrix = np.load(LZZT / "chunks" / "embeddings.npy")
    d = api.ProtocolDocument.open(LZZT, embed_fn=lambda text: matrix[7], rerank_fn=keep_order)
    real, builds, opening_warnings = api.build_identity, [], list(d.warnings)

    def counting(*args, **kwargs):
        builds.append(kwargs.get("section_of"))
        return real(*args, **kwargs)

    api.build_identity = counting
    try:
        first = d.document_info()
        second = d.document_info()
        assert first == second and first is not second
        assert d.search("uncached question about the study design", k=4)["identity"]["provenance"]["refs"]
        assert len(builds) == 1 and builds[0] == d._section_of  # the repaired section assignment, not the default
    finally:
        api.build_identity = real
    # a warning added between two calls (other tools append them) appears in the second
    d.warnings.append("a warning added between the calls")
    third = d.document_info()
    assert third["warnings"] == first["warnings"] + ["a warning added between the calls"]
    assert {k: v for k, v in third.items() if k != "warnings"} == {k: v for k, v in first.items() if k != "warnings"}
    assert first["warnings"] == second["warnings"] == opening_warnings
    # mutating a returned result (values, nested lists, whole keys) does not change the next one
    snapshot = copy.deepcopy(third)
    third["warnings"].append("x")
    third["titles"]["official_title"]["value"] = "changed"
    third["titles"]["official_title"]["source"]["refs"].append("#/texts/99999")
    third["identifiers"]["sponsor_protocol_id"]["source"]["text"] = "changed"
    third["sponsor"]["provenance"]["refs"].clear()
    third["cover_page"].clear()
    third["soa_tables"].clear()
    third["provenance"]["pages"].append(999)
    del third["identity_block"]
    assert d.document_info() == snapshot
    # one result holds no list twice: changing the refs of one value cannot change another's
    seen: list[int] = []

    def list_ids(x: object) -> None:
        if isinstance(x, dict):
            for v in x.values():
                list_ids(v)
        elif isinstance(x, list):
            seen.append(id(x))
            for v in x:
                list_ids(v)

    list_ids(d.document_info())
    assert len(seen) == len(set(seen))


# =========================================================================== outline / find

@case
def test_outline():
    for path in (LZZT, LILLY):
        d = doc(path)
        o = d.outline()
        has_prov(o, "outline")
        secs = o["sections"]
        ids = [s["id"] for s in secs]
        assert len(ids) == len(set(ids)) == o["total_sections"] == len(d.index.sections)
        by = {s["id"]: s for s in secs}
        for s in secs:
            assert {"id", "number", "title", "level", "role", "pages", "tokens", "own_tokens", "children"} <= set(s)
            assert set(s["children"]) <= set(ids)
            assert s["tokens"] == s["own_tokens"] + sum(by[k]["tokens"] for k in s["children"])
        assert o["total_tokens"] > 20000
        top = d.outline(max_level=1, with_roles=False)
        assert all(s["level"] <= 1 for s in top["sections"]) and "role" not in top["sections"][0]
        assert all(set(s["children"]) <= {x["id"] for x in top["sections"]} for s in top["sections"])
    assert next(s for s in doc(LZZT).outline()["sections"] if s["id"] == "S3.4.2.1")["role"] == "inclusion"


@case
def test_outline_section_and_parent_ids():
    if not public(LZZT):
        return
    d = doc(LZZT)
    secs = d.outline()["sections"]
    for s in secs:  # section_id / parent_id as in every other tool; id / parent stay (the demo UI reads them)
        assert s["section_id"] == s["id"] and s["parent_id"] == s["parent"], s["id"]
        assert s["parent_id"] == d.index.by_id[s["section_id"]].parent_id
    assert next(s for s in secs if s["section_id"] == "S3.4.2.1")["parent_id"] == "S3.4.2"


@case
def test_find_sections():
    d = doc(LZZT)
    r = d.find_sections(role="inclusion")
    assert r["sections"][0]["section_id"] == "S3.4.2.1" and r["sections"][0]["root_of_role"] is True
    has_prov(r["sections"][0], "find_sections")
    assert d.find_sections(query="exclusion criteria")["sections"][0]["section_id"] == "S3.4.2.2"
    hit = d.find_sections(query="3.4.2.1")["sections"][0]
    assert hit["section_id"] == "S3.4.2.1" and hit["score"] >= 1.0
    assert d.find_sections(role="soa")["count"] >= 1
    assert doc(LILLY).find_sections(query="Section 6.2")["sections"][0]["section_id"] == "S6.2"
    assert doc(LILLY).find_sections(query="schedule of activities")["sections"][0]["section_id"] == "S2"
    raises(api.ProtocolAPIError, d.find_sections, role="no_such_role")
    raises(api.ProtocolAPIError, d.find_sections)


@case
def test_find_sections_hint():
    if not public(LZZT):
        return
    d = doc(LZZT)
    # sections with the role exist, the query matches none of them: say so, and how to list them
    n = sum(s.role == "inclusion" for s in d.index.sections)
    r = d.find_sections(role="inclusion", query="zqxv nothing matches")
    assert r["count"] == 0 and n >= 1
    assert r["hint"] == (f"no section with role 'inclusion' matches the query 'zqxv nothing matches'; {n} "
                         "section(s) have this role: call find_sections with role='inclusion' and no query to list them")
    # no section has the role: where its content usually sits (with or without a query), as before
    present = {s.role for s in d.index.sections}
    absent = [x for x in api.ROLE_FALLBACK if x not in present]
    assert absent, "LZZT has a section for every role of ROLE_FALLBACK"
    want = f"no section has role {absent[0]!r}; its content usually sits under {' / '.join(api.ROLE_FALLBACK[absent[0]])}"
    for query in (None, "zqxv nothing matches"):
        r = d.find_sections(role=absent[0], query=query)
        assert r["count"] == 0 and r["hint"] == want, query
    for role in (x for x in api.ROLES if x not in present and x not in api.ROLE_FALLBACK):
        assert "hint" not in d.find_sections(role=role), role


# =========================================================================== read_section

@case
def test_read_section_criteria_complete_against_usdm_key():
    from usdm_gold import content_tokens

    for name, path in (("LZZT", LZZT), ("Lilly", LILLY)):
        d = doc(path)
        for category in ("inclusion", "exclusion"):
            r = d.read_section(category)
            has_prov(r, f"{name} {category}")
            assert r["complete"] and r["cursor"] is None and r["mode"] == "role"
            assert f"{{#{r['section_id']}}}" in r["markdown"]
            toks = set(content_tokens(r["markdown"]))
            crit = key_criteria(name, category)
            assert crit, f"{name}: no {category} criteria in the key"
            low = [(c.name, round(coverage(c.text, toks), 2)) for c in crit if coverage(c.text, toks) < 0.95]
            assert not low, f"{name} {category}: key criteria missing from read_section: {low}"
            for b in r["blocks"]:
                assert b["section_id"] and isinstance(b["pages"], list) and "chars" in b
                a, z = b["chars"]
                assert r["markdown"][a:z].strip()
    r = doc(LZZT).read_section("S3.4.2.1")
    assert [ln.split(" ", 1)[0] for ln in r["markdown"].splitlines() if ln.startswith("[")] == \
        [f"[{i}]" for i in range(1, 9)]  # the protocol's own markers, in order
    # Lilly p. 26: [1b], [2], [3] were read before heading 6 by the converter; the repair puts them back in 6.1
    # (post-processing moves them in the document; outputs converted before that are repaired at read time)
    r = doc(LILLY).read_section("inclusion")
    if not repaired_in_postprocessing(LILLY):
        moved = [b for b in r["blocks"] if b.get("moved_from") == "S5.5"]
        assert {b["ref"] for b in moved} >= {"#/groups/4", "#/groups/5"}
        assert any("reading-order repair" in w for w in r["warnings"])
    assert r["markdown"].index("[1a]") < r["markdown"].index("[1b]") < r["markdown"].index("[3a]")
    assert "male patients" not in doc(LILLY).read_section("S5.5")["markdown"]


@case
def test_read_section_cursor_and_resolution():
    d = doc(LILLY)
    full = d.read_section("S2")
    parts, cur = [], None
    r = d.read_section("S2", max_tokens=600)
    parts.append(r)
    while r["cursor"]:
        r = d.read_section(cursor=r["cursor"], max_tokens=600)
        parts.append(r)
    assert len(parts) >= 2
    got = [b["block"] for p in parts for b in p["blocks"]]
    assert got == list(range(full["part"]["total_blocks"])), "continuation lost or repeated blocks"
    for p in parts:
        assert p["blocks"][-1]["kind"] != "heading" or p is parts[-1], "a part ends on a bare heading"
        assert p["tokens"] <= 600 + 50 or p["warnings"], "a part exceeds max_tokens without a warning"
    assert not parts[0]["complete"] and parts[0]["next"]["arguments"]["cursor"] == parts[0]["cursor"]
    # LZZT exclusion in parts: every key criterion still present when the parts are joined
    from usdm_gold import content_tokens

    d = doc(LZZT)
    r, md = d.read_section("exclusion", max_tokens=800), []
    md.append(r["markdown"])
    while r["cursor"]:
        r = d.read_section(cursor=r["cursor"], max_tokens=800)
        assert r["markdown"].lstrip().startswith(("<!--", "#")), "a continued part starts without a heading"
        md.append(r["markdown"])
    assert len(md) >= 3
    toks = set(content_tokens("\n".join(md)))
    assert all(coverage(c.text, toks) >= 0.95 for c in key_criteria("LZZT", "exclusion"))
    raises(api.NotFoundError, d.read_section, cursor="S3.4.2.2|1|9999")
    raises(api.NotFoundError, d.read_section, cursor="garbage")
    assert d.read_section("3.4.2.1")["section_id"] == "S3.4.2.1"  # a section number
    assert d.read_section("Section 3.4.2.2")["section_id"] == "S3.4.2.2"
    raises(api.NotFoundError, d.read_section, "zzzz qqqq")
    design = d.read_section("design")  # role mode keeps the design family, lists the rest
    assert "S3.1" in [s["section_id"] for s in design["sections"]]
    assert "S3.4" in [s["section_id"] for s in design["excluded_subsections"]]
    one = d.read_section("S3.4.2", include_subsections=False)
    assert [s["section_id"] for s in one["sections"]] == ["S3.4.2"]
    if not d.find_sections(role="estimands")["count"]:  # no estimands section: falls back, says so
        est = d.read_section("estimands")
        assert est["section_id"] in d._role_roots("objectives_endpoints")
        assert any("objectives_endpoints" in w for w in est["warnings"])


@case
def test_read_section_next_keeps_with_blocks():
    if not public(LILLY):
        return
    d = doc(LILLY)
    for with_blocks in (False, True):
        r = d.read_section("S2", max_tokens=600, with_blocks=with_blocks)
        assert r["next"]["arguments"] == {"cursor": r["cursor"], "max_tokens": 600, "with_blocks": with_blocks}
        assert ("blocks" in d.read_section(**r["next"]["arguments"])) is with_blocks
    # an agent that passes "next" back as it is: every part of a with_blocks=False read comes without blocks
    parts = [d.call_tool("read_section", {"section": "S2", "max_tokens": 600, "with_blocks": False})]
    while parts[-1]["cursor"]:
        parts.append(d.call_tool(parts[-1]["next"]["tool"], parts[-1]["next"]["arguments"]))
    assert len(parts) >= 2 and not any("blocks" in p for p in parts)


@case
def test_read_section_tables_and_figures():
    r = doc(LZZT).read_section("soa")
    assert "| Informed consent |" in r["markdown"]
    assert any(b["kind"] == "table" and b["table_id"] == "#/tables/12" for b in r["blocks"])
    d = doc(LILLY)
    sid = d._section_of("#/pictures/0")
    r = d.read_section(sid, include_subsections=False)
    fig = next(b for b in r["blocks"] if b["kind"] == "figure")
    assert fig["figure_id"] == "#/pictures/0" and fig["pages"] == [24]
    assert "Text in figure (OCR)" in r["markdown"] and "Figure IGBJ.1" in r["markdown"]
    assert fig.get("image", "").endswith(".png")


# =========================================================================== tables / SoA

@case
def test_get_table():
    d = doc(LZZT)
    t = d.get_table("#/tables/12")
    has_prov(t, "get_table")
    assert t["provenance"]["refs"] == ["#/tables/12"] and t["provenance"]["pages"] == [53]
    assert t["markdown"] == d.tables["#/tables/12"]["markdown"]
    f = t["fragments"][0]
    assert len(f["grid"]) == f["rows"] == 30 and all(len(row) == f["cols"] == 9 for row in f["grid"])
    assert t["role"] == "soa"
    q = d.get_table("Laboratory Tests Performed at Admission")
    assert q["table_id"] == "#/tables/9" and q["candidates"][0]["table_id"] == "#/tables/9"
    assert d.get_table("9")["table_id"] == "#/tables/9"
    rows = d.get_table("#/tables/9", format="rows")
    assert rows["fragments"][0]["row_lines"] and "markdown" not in rows
    assert doc(LILLY).get_table("study treatments")["table_id"] == "#/tables/5"
    raises(api.NotFoundError, d.get_table, "#/tables/999")
    raises(api.ProtocolAPIError, d.get_table, "#/tables/9", format="xml")
    # a page-split chain (tables.jsonl "chain") comes back as one table
    saved = {k: dict(d.tables[k]) for k in ("#/tables/12", "#/tables/13")}
    try:
        for k in saved:
            d.tables[k]["chain"] = ["#/tables/12", "#/tables/13"]
        c = d.get_table("#/tables/13")
        assert c["table_ids"] == ["#/tables/12", "#/tables/13"] and c["provenance"]["pages"] == [53, 54]
        assert "(Continued on the next page" in c["markdown"] and c["rows"] == 60
    finally:
        d.tables.update(saved)


@case
def test_get_soa():
    d = doc(LILLY)
    r = d.get_soa()
    has_prov(r, "get_soa")
    if r["available"]:
        assert r["count"] >= 1
        for s in r["soas"]:
            has_prov(s, "soa")
            assert s["visits"] and s["activities"] and isinstance(s.get("markdown"), str)
        assert d.get_soa() ["soas"][0]["visits"] == r["soas"][0]["visits"]  # cached
        json.dumps(d.call_tool("get_soa", {"visit": r["soas"][0]["visits"][0]["label"]}))
    else:
        assert "soa.py" in r["reason"] and r["soa_tables"]
    saved = sys.modules.get("soa")
    sys.modules["soa"] = None  # import soa -> ImportError: the "not available" result
    try:
        r = d.get_soa()
        assert r["available"] is False and "soa.py" in r["reason"]
        assert [t["table_id"] for t in r["soa_tables"]] == ["#/tables/3"]
        has_prov(r, "get_soa unavailable")
    finally:
        if saved is None:
            del sys.modules["soa"]
        else:
            sys.modules["soa"] = saved


# =========================================================================== search

@case
def test_search_offline_cached_uncached_and_fake_client():
    from retrieve import PRODUCTION_BUDGET, PRODUCTION_K, Retriever, production_builder
    from eval_retrieval import cached_query_vectors

    d = doc(LZZT)
    q = cached_question("LZZT_CDISC_Pilot")
    r = d.search(q, rendered=True)
    has_prov(r, "search")
    assert "Sponsor protocol number: H2Q-MC-LZZT" in r["identity"]["text"]
    has_prov(r["identity"], "identity block")
    assert r["blocks"] and len(r["hits"]) == PRODUCTION_K and r["tokens"] <= PRODUCTION_BUDGET
    for b in r["blocks"]:
        has_prov(b, "search block")
        assert b["text"].strip() and b["kind"] and b["provenance"]["refs"], b["kind"]
    # the same context as the production path (retrieve.production_builder) on the same cached vector
    ret = Retriever(LZZT / "chunks" / "chunks.jsonl")
    vec = cached_query_vectors([ret.query_text(ret.expand(q)[0])], ret.meta["model"],
                               ret.cache_dir / "query_embedding_cache.json")[0]
    ctx = production_builder(ret).build(ret.search(q, vec, k=PRODUCTION_K, mode="rerank"))
    assert r["context"] == ctx.render()
    # uncached question: a clear offline error, and call_tool turns it into an error result
    e = raises(api.OfflineCacheMiss, d.search, "How many unicorns are enrolled on the moon?")
    assert "OPENROUTER_OFFLINE=1" in str(e) and "query embedding" in str(e)
    err = d.call_tool("search", {"query": "How many unicorns are enrolled on the moon?"})
    assert err["error_type"] == "OfflineCacheMiss"
    # a fake embedding client: the rerank is then uncached -> clear error; with a fake reranker it works
    matrix = np.load(LZZT / "chunks" / "embeddings.npy")
    fake = api.ProtocolDocument.open(LZZT, embed_fn=lambda text: matrix[7])
    e = raises(api.OfflineCacheMiss, fake.search, "uncached question about the study design")
    assert "rerank" in str(e)
    fake = api.ProtocolDocument.open(LZZT, embed_fn=lambda text: matrix[7],
                                     rerank_fn=lambda query, chunks, model: [(i, 1.0 / (i + 1)) for i in range(len(chunks))])
    r = fake.search("uncached question about the study design", k=4)
    assert r["blocks"] and len(r["hits"]) == 4 and all(b["provenance"]["refs"] for b in r["blocks"])


# =========================================================================== abbreviations / quotes

@case
def test_abbreviation():
    d = doc(LZZT)
    r = d.abbreviation("AD")
    has_prov(r, "abbreviation")
    assert r["found"] and r["matches"][0]["definition"] == "Alzheimer's disease"
    assert r["matches"][0]["provenance"]["pages"] == [5]
    r = d.abbreviation("alzheimer's disease")
    assert r["matches"][0]["abbreviation"] == "AD" and r["matches"][0]["match"] == "definition"
    assert d.abbreviation("ad")["matches"][0]["abbreviation"] == "AD"  # case-insensitive
    r = d.abbreviation("ZZQX")
    assert not r["found"] and "hint" in r
    r = doc(LILLY).abbreviation("AE")
    assert r["found"] and "adverse event" in r["matches"][0]["definition"].lower()
    assert any(m["abbreviation"] == "AE" for m in doc(LILLY).abbreviation("adverse event")["matches"])


@case
def test_quote_check():
    d = doc(LZZT)
    r = d.quote_check("Males and postmenopausal females at least 50 years of age.")
    has_prov(r, "quote_check")
    assert r["found"] and r["match"] == "exact" and r["count"] == 1
    loc = r["locations"][0]
    assert loc["provenance"]["section_id"] == "S3.4.2.1" and loc["provenance"]["pages"] == [11]
    assert loc["provenance"]["refs"] == ["#/texts/85"] and loc["layer"] == "body" and loc["kind"] == "list_item"
    assert "[1] Males and postmenopausal" in loc["text"]
    r = d.quote_check("MALES and postmenopausal   females at least 50 years of age")
    assert r["found"] and r["match"] == "normalized"
    r = d.quote_check("Hachinski Ischemic Scale score of ≤ 4(Attachment LZZT.8)")
    assert r["found"] and r["match"] in ("normalized", "compact")
    r = d.quote_check("Alupent® tablets (metaproterenol) | 2 weeks")
    assert r["found"] and r["locations"][0]["kind"] == "table_row" and r["locations"][0]["table_rows"] == [0]
    assert r["locations"][0]["provenance"]["refs"] == ["#/tables/2"]
    r = d.quote_check("allows adequate follow-up. [8] A reliable caregiver who is in frequent")
    assert r["found"] and r["locations"][0]["provenance"]["refs"] == ["#/texts/105", "#/texts/106"]
    r = d.quote_check("Document Page 1")
    assert r["found"] and r["locations"][0]["layer"] == "furniture"
    r = d.quote_check("Females between 12 and 17 years of age with asthma")
    assert not r["found"] and r["match"] is None and r["count"] == 0
    r = d.quote_check("Males and postmenopausal females at least 60 years of age.")
    assert not r["found"] and r["closest"]["provenance"]["refs"] == ["#/texts/85"] and r["closest"]["similarity"] >= 0.8
    r = d.quote_check("MMSE score of 10 to 23", section_id="S3.4.2.1")
    assert r["found"]
    r = d.quote_check("MMSE score of 10 to 23", section_id="S2")
    assert not r["found"] and r["found_elsewhere"][0]["provenance"]["section_id"] == "S3.4.2.1"
    r = d.quote_check("Males and postmenopausal females … MMSE score of 10 to 23")
    assert r["found"] and r["match"] == "elided" and len(r["parts"]) == 2
    assert not d.quote_check("Males and postmenopausal females ... unicorn riders on the moon")["found"]
    raises(api.ProtocolAPIError, d.quote_check, "!")
    raises(api.NotFoundError, d.quote_check, "MMSE", section_id="S999")
    # provenance follows the reading-order repair (Lilly p. 26)
    r = doc(LILLY).quote_check("male patients: agree to use an effective method of contraception")
    assert r["found"] and r["locations"][0]["provenance"]["section_id"] == "S6.1"


@case
def test_quote_check_figure_text_pages():
    if not public(LZZT, LILLY):
        return
    d = doc(LILLY)
    item = d._resolve("#/texts/822")
    assert item.prov == [] and item.parent.cref == "#/pictures/0"  # flattened figure OCR text: no pages of its own
    loc = d.quote_check(" ".join(item.text.split())[:60])["locations"][0]
    assert loc["kind"] == "figure_text" and loc["provenance"]["refs"] == ["#/texts/822"]
    assert loc["provenance"]["pages"] == [24] == api._pages_of(d._resolve("#/pictures/0"))  # the picture's
    for path in (LZZT, LILLY):
        figs = [s for s in doc(path)._segments()["body"] if s.kind == "figure_text"]
        assert figs and all(s.pages for s in figs), path.name


@case
def test_quote_check_count_uncapped_by_layer():
    if not public(LZZT, LILLY):
        return
    d = doc(LZZT)
    exact = d._text_streams()["exact"]
    want = {"body": exact[0].text.count("the"), "furniture": exact[1].text.count("the")}
    r = d.quote_check("the")
    assert r["count"] == sum(want.values()) > 200 and r["count_by_layer"] == want  # no cap at 200 per layer
    assert len(r["locations"]) == api.MAX_LOCATIONS and r["more_locations"] == r["count"] - api.MAX_LOCATIONS
    s = d.quote_check("the", section_id="S3")
    assert 0 < s["count"] < r["count"] and s["count_by_layer"] == {"body": s["count"], "furniture": 0}
    assert s["more_locations"] == s["count"] - api.MAX_LOCATIONS and len(s["found_elsewhere"]) == api.MAX_LOCATIONS
    none = d.quote_check("zqxv completely absent text here")
    assert none["count"] == 0 and none["count_by_layer"] == {"body": 0, "furniture": 0}
    # AI-generated figure descriptions are not counted (the public outputs have none: one is added here)
    d = api.ProtocolDocument.open(LZZT)
    segs = d._segments()
    ai = api._Seg("Flow chart: zqxv unicorn visit. Males and postmenopausal females at least 50 years of age.",
                  "#/pictures/0", [5], "ai_generated", "figure_description")
    d._segments = lambda: {**segs, "ai_generated": [ai]}
    r = d.quote_check("zqxv unicorn visit")
    assert not r["found"] and r["count"] == 0 and r["count_by_layer"] == {"body": 0, "furniture": 0}
    assert [x["layer"] for x in r["found_in_ai_text"]] == ["ai_generated"] and r["warning"]
    r = d.quote_check("Males and postmenopausal females at least 50 years of age")
    assert r["found"] and r["count"] == 1 and r["count_by_layer"] == {"body": 1, "furniture": 0}
    assert len(r["found_in_ai_text"]) == 1
    r = d.quote_check("Flow chart … unicorn visit")  # an elided quote only in AI text
    assert not r["found"] and r["count"] == 0 and r["found_in_ai_text"] and r["warning"]


@case
def test_quote_check_document_text_before_ai_text():
    if not public(LZZT):
        return
    # a single quote: the first tier with a match in the document text wins, although an earlier tier matches an
    # AI-generated figure description; only when no tier matches document text is the AI match reported (not found)
    d = api.ProtocolDocument.open(LZZT)
    q, ai = "Zqxv Unicorn Visit at Week 4", "Flow chart: Zqxv Unicorn Visit at Week 4, then follow-up."

    def check(body: str) -> dict:
        segs = {"body": [api._Seg(body, "#/texts/90001", [3], "body", "text")], "furniture": [],
                "ai_generated": [api._Seg(ai, "#/pictures/0", [5], "ai_generated", "figure_description")]}
        d._segments, d._streams, d._words = (lambda: segs), None, None
        return d.quote_check(q)

    for body, tier in (("Each participant attends the ZQXV UNICORN VISIT AT WEEK 4.", "normalized"),
                       ("Each participant attends the zqxv-unicorn (visit) at week 4.", "compact"),
                       ("Each participant attends the Zqxv Unicorn Visit at Week 4.", "exact")):
        r = check(body)
        assert r["found"] and r["match"] == tier and r["count"] == 1, (tier, r["found"], r["match"])
        assert r["count_by_layer"] == {"body": 1, "furniture": 0} and r["locations"][0]["layer"] == "body"
        assert [(x["layer"], x["match"]) for x in r["found_in_ai_text"]] == [("ai_generated", tier)] and r["warning"]
    r = check("Each participant attends the screening visit.")  # only the AI text holds it
    assert not r["found"] and r["match"] is None and r["count"] == 0 and r["locations"] == []
    assert [(x["layer"], x["match"]) for x in r["found_in_ai_text"]] == [("ai_generated", "exact")] and r["warning"]


@case
def test_quote_check_furniture_slots():
    if not public(LILLY):
        return
    d = doc(LILLY)
    q = "I8R-JE-IGBJ"  # 11 times in the body, 72 times in page headers/footers
    r = d.quote_check(q)
    nb, nf = r["count_by_layer"]["body"], r["count_by_layer"]["furniture"]
    assert nb >= 9 and nf >= 2 and r["count"] == nb + nf
    every = d.quote_check(q, max_locations=1000)["locations"]
    assert [x["layer"] for x in every] == ["body"] * nb + ["furniture"] * nf
    assert r["locations"] == every[:api.MAX_LOCATIONS - 2] + every[nb:nb + 2]  # 2 furniture ones replace the last body ones
    assert r["more_locations"] == r["count"] - api.MAX_LOCATIONS
    assert d.quote_check(q, max_locations=3)["locations"] == every[:3]  # fewer than 4 slots: as found
    # found_elsewhere follows the same rule (a section without the identifier: every match is elsewhere)
    sid = next(s.section_id for s in d.index.sections
               if not d._children(s.section_id) and d.quote_check(q, section_id=s.section_id)["count"] == 0)
    e = d.quote_check(q, section_id=sid)
    assert [x["layer"] for x in e["found_elsewhere"]] == ["body"] * (api.MAX_LOCATIONS - 2) + ["furniture"] * 2


@case
def test_quote_check_elided_in_order():
    a, b, c = "Males and postmenopausal females", "MMSE score of 10 to 23", "Alupent"
    if public(LZZT):
        d = doc(LZZT)
        r = d.quote_check(f"{a} … {b}")
        assert r["found"] and r["match"] == "elided" and r["count"] == 1 and r["section_id"] is None
        assert r["count_by_layer"] == {"body": 1, "furniture": 0} and len(r["locations"]) == 1
        loc = r["locations"][0]  # one location from the first part's start to the last part's end
        assert loc["provenance"]["refs"][0] == "#/texts/85" and len(loc["provenance"]["refs"]) >= 3
        assert a in loc["text"] and b in loc["text"] and r["provenance"] == loc["provenance"]
        assert [(p["part"], p["found"]) for p in r["parts"]] == [(a, True), (b, True)]
        # both parts occur once (at every tier), close together, but not in this order
        e, compact = "Large vessel strokes", d._text_streams()["compact"][0].text
        assert compact.count(api._compact_form(a)) == compact.count(api._compact_form(e)) == 1
        assert 0 < compact.find(api._compact_form(e)) - compact.find(api._compact_form(a)) < 2000
        assert d.quote_check(f"{a} … {e}")["found"]
        r = d.quote_check(f"{e} ... {a}")
        assert not r["found"] and r["match"] is None and r["count"] == 0 and all(p["found"] for p in r["parts"])
        # not found: the passage closest to the parts joined
        r = d.quote_check("Males and postmenopausal females at least … 60 years of age")
        assert not r["found"] and r["closest"]["provenance"]["refs"] == ["#/texts/85"]
        assert r["closest"] == d._closest("Males and postmenopausal females at least 60 years of age", None)
        # in order, but more than 2,000 characters apart
        norm, na, nc = d._text_streams()["normalized"][0].text, api._norm_form(a), api._norm_form(c)
        assert norm.count(na) == norm.count(nc) == 1 and norm.find(nc) - (norm.find(na) + len(na)) > 2000
        r = d.quote_check(f"{a} [...] {c}")  # (no compact tier: "Alupent" has fewer than 12 letters)
        assert not r["found"] and all(p["found"] for p in r["parts"])
        # section_id: sequences outside the section are found_elsewhere
        r = d.quote_check(f"{a} … {b}", section_id="S3.4.2.1")
        assert r["found"] and r["section_id"] == "S3.4.2.1" and r["count"] == 1
        r = d.quote_check(f"{a} … {b}", section_id="S2")
        assert not r["found"] and r["section_id"] == "S2" and r["count"] == 0
        assert [x["provenance"]["section_id"] for x in r["found_elsewhere"]] == ["S3.4.2.1"]
        # three objectives start with the same words, one ends so: one tight sequence, in that objective only
        r = d.quote_check("To assess the dose-dependent … assessment (see Attachment LZZT.2).")
        assert r["count"] == 1 and [x["provenance"]["refs"] for x in r["locations"]] == [["#/texts/38"]]
        # an elided result follows the listing rules: every sequence counted, header/footer slots, more_locations
        d = api.ProtocolDocument.open(LZZT)
        seg = [api._Seg("alpha beta gamma delta", f"#/texts/9{i:04d}", [i + 1], "body" if i < 9 else "furniture",
                        "text") for i in range(12)]
        d._segments = lambda: {"body": seg[:9], "furniture": seg[9:], "ai_generated": []}
        r = d.quote_check("alpha beta … gamma delta")
        assert r["found"] and r["count"] == 12 and r["count_by_layer"] == {"body": 9, "furniture": 3}
        assert [x["layer"] for x in r["locations"]] == ["body"] * (api.MAX_LOCATIONS - 2) + ["furniture"] * 2
        assert r["more_locations"] == 12 - api.MAX_LOCATIONS and r["provenance"] == r["locations"][0]["provenance"]

    def stream(*texts: str) -> api._Stream:
        return api._Stream([api._Seg(t, f"#/texts/{i}", [1], "body", "text") for i, t in enumerate(texts)],
                           api._exact_form, " ")

    ab, gd = "alpha beta", "gamma delta"
    # the 2,000-character rule at its boundary (stream offsets from the end of one part to the start of the next)
    assert api._sequences(stream(ab, "z" * 1998, gd), [ab, gd]) == [(0, 2021)]
    assert api._sequences(stream(ab, "z" * 1999, gd), [ab, gd]) == []
    assert api._sequences(stream(ab, "z" * 48, gd), [gd, ab]) == []
    # tight, never overlapping: part-1 occurrences before one later part give one sequence, from the last of them
    st = stream("alpha beta one", "alpha beta two", "alpha beta three", gd)
    assert api._sequences(st, [ab, gd]) == [(st.text.rindex(ab), len(st.text))]
    st = stream(ab, "alpha beta", gd, ab, "epsilon zeta")  # the third part-1 occurrence lies inside the sequence
    assert api._sequences(st, [ab, gd, "epsilon zeta"]) == [(len(ab) + 1, len(st.text))]
    st = stream(ab, gd, "filler text", ab, gd)  # two separate places
    assert api._sequences(st, [ab, gd]) == [(0, len(f"{ab} {gd}")), (st.text.rindex(ab), len(st.text))]
    # 3 or more parts: when a part's first occurrence in the window completes no chain, a later one inside the
    # window can. A, B 1 character later, a second B 1,507 after A, C 1,502 after the second B (3,008 after the first)
    ez, et = "epsilon zeta", "eta theta"
    st = stream(ab, gd, "z" * 1493, gd, "z" * 1500, ez)
    b1, b2, c = st.text.index(gd), st.text.rindex(gd), st.text.index(ez)
    assert (b1 - len(ab), b2 - len(ab), c - (b2 + len(gd)), c - (b1 + len(gd))) == (1, 1507, 1502, 3008)
    assert api._sequences(st, [ab, gd, ez]) == [(0, len(st.text))]
    st = stream(ab, gd, "z" * 1480, ab, gd, "z" * 1500, ez)  # the anchor: the last A that ends before the chosen B
    assert api._sequences(st, [ab, gd, ez]) == [(st.text.rindex(ab), len(st.text))]
    st = stream(ab, gd, ez, "z" * 1200, ez, "z" * 1200, et)  # a later third part, the fourth from it
    assert api._sequences(st, [ab, gd, ez, et]) == [(0, len(st.text))]

    def spelled_out(text: str, needles: list[str], later: bool = True) -> list[tuple[int, int]]:
        """The rule as stated: from each first-needle occurrence, each next needle tries its occurrences inside the
        window in order and takes the first that completes the chain (later=False: only its first occurrence there,
        the rule before this fix); the anchor is the last first-needle occurrence ending by the chosen second needle;
        the next anchor starts at or after the sequence's end."""
        occ = [[m.start() for m in re.finditer(f"(?={re.escape(n)})", text)] for n in needles]

        def chain(k: int, end: int) -> list[int] | None:
            if k == len(needles):
                return []
            for s in [s for s in occ[k] if s >= end][:None if later else 1]:
                if s - end > api.ELIDED_GAP:
                    break
                rest = chain(k + 1, s + len(needles[k]))
                if rest is not None:
                    return [s, *rest]
            return None

        out, i = [], 0
        while i < len(occ[0]):
            got = chain(1, occ[0][i] + len(needles[0]))
            if got is None:
                i += 1
                continue
            end = got[-1] + len(needles[-1])
            out.append((max(f for f in occ[0] if f + len(needles[0]) <= got[0]), end))
            i = next((j for j, f in enumerate(occ[0]) if f >= end), len(occ[0]))
        return out

    rng, changed = random.Random(11), 0
    for _ in range(300):  # random streams of the parts and filler: 2 parts as before, 3 or more as spelled out
        st = stream(*[rng.choice([ab, gd, ez, et]) if rng.random() < 0.6 else "z" * rng.randrange(1300)
                      for _ in range(rng.randrange(2, 16))])
        for needles in ([ab, gd], [gd, ab], [ab, ab], [ab, gd, ez], [ab, gd, ab], [ab, gd, ez, et]):
            got = api._sequences(st, needles)
            assert got == spelled_out(st.text, needles), (needles, st.text)
            if len(needles) == 2:
                assert got == spelled_out(st.text, needles, later=False), (needles, st.text)
            else:
                changed += got != spelled_out(st.text, needles, later=False)
    assert changed > 0  # the streams do exercise the later occurrences


# =========================================================================== tool layer / CLI

@case
def test_tool_schemas_and_call_tool():
    import jsonschema

    for style in ("anthropic", "openai"):
        tools = api.tool_schemas(style)
        json.dumps(tools)
        names = [t["name"] if style == "anthropic" else t["function"]["name"] for t in tools]
        assert len(names) == len(set(names)) == 9
        for t in tools:
            schema = t["input_schema"] if style == "anthropic" else t["function"]["parameters"]
            jsonschema.Draft202012Validator.check_schema(schema)
            jsonschema.Draft7Validator.check_schema(schema)
            assert schema["type"] == "object" and schema.get("additionalProperties") is False
            if style == "openai":
                assert t["type"] == "function" and t["function"]["description"]
    d = doc(LZZT)
    calls = {
        "document_info": {}, "outline": {"max_level": 1}, "find_sections": {"role": "exclusion"},
        "read_section": {"section": "inclusion"}, "get_table": {"table": "#/tables/12", "format": "markdown"},
        "get_soa": {}, "abbreviation": {"term": "AD"},
        "quote_check": {"quote": "MMSE score of 10 to 23"},
        "search": {"query": cached_question("LZZT_CDISC_Pilot")},
    }
    assert set(calls) == {t["name"] for t in api.tool_schemas()}
    for name, args in calls.items():
        res = d.call_tool(name, args)
        assert "error" not in res, (name, res.get("error"))
        json.dumps(res)
        has_prov(res, f"call_tool {name}")
    assert d.call_tool("read_section", {"section": "inclusion"})["request"]["max_tokens"] == api.DEFAULT_TOOL_MAX_TOKENS
    assert "error" in d.call_tool("read_section", {})
    assert "invalid arguments" in d.call_tool("quote_check", {"quote": "x", "bogus": 1})["error"]
    assert "invalid arguments" in d.call_tool("find_sections", {"role": "no_such_role"})["error"]
    assert "unknown tool" in d.call_tool("delete_everything", {})["error"]
    assert d.call_tool("get_table", {"table": "#/tables/999"})["error_type"] == "NotFoundError"


@case
def test_search_schema_rendered():
    import jsonschema

    schema = next(t for t in api.tool_schemas() if t["name"] == "search")["input_schema"]
    assert schema["properties"]["rendered"] == {
        "type": "boolean", "default": False,
        "description": "Also return 'context': the context string exactly as an answering model sees it."}
    jsonschema.validate({"query": "dose", "rendered": True}, schema)
    if not public(LZZT):
        return
    r = doc(LZZT).call_tool("search", {"query": cached_question("LZZT_CDISC_Pilot"), "rendered": True})
    assert "error" not in r and r["context"].strip(), r.get("error")


@case
def test_quote_check_description():
    desc = next(t for t in api.tool_schemas() if t["name"] == "quote_check")["description"]
    for words in ("count of every match", "count_by_layer", f"up to {api.MAX_LOCATIONS} locations",
                  "up to 2 listed places are kept for header/footer matches when both layers match", "in this order",
                  "within 2,000 characters"):
        assert words in desc, words


@case
def test_call_tool_error_types():
    if not public(LZZT):
        return
    d = doc(LZZT)
    assert d.call_tool("delete_everything", {}) == {"error": "unknown tool 'delete_everything'",
                                                    "error_type": "UnknownTool", "tools": [t["name"] for t in api.TOOLS]}
    for name, args in (("quote_check", {"quote": 5}), ("quote_check", {"quote": "x", "bogus": 1}),
                       ("find_sections", {"role": "no_such_role"})):
        r = d.call_tool(name, args)
        assert set(r) == {"error", "error_type", "tool"} and (r["error_type"], r["tool"]) == ("InvalidArguments", name)
        assert r["error"].startswith(f"invalid arguments for {name}: "), r["error"]


@case
def test_cli():
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "OPENROUTER_OFFLINE": "1"}

    def run(*args: str) -> str:
        p = subprocess.run([sys.executable, str(ROOT / "protocol_api.py"), str(LZZT), *args], capture_output=True,
                           text=True, encoding="utf-8", env=env, timeout=600)
        assert p.returncode == 0, p.stderr[-2000:]
        return p.stdout

    info = json.loads(run("info"))
    assert info["identifiers"]["sponsor_protocol_id"]["value"] == "H2Q-MC-LZZT"
    md = run("section", "3.4.2.1", "--md")
    assert "[8] A reliable caregiver" in md and "{#S3.4.2.1}" in md
    assert json.loads(run("quote", "MMSE score of 10 to 23"))["found"] is True


def main() -> int:
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
