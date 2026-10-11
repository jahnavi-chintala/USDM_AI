"""Self-test for protocol_api.py: `python tests/test_protocol_api.py` (no pytest).

Public protocols only: output_public/LZZT_CDISC_Pilot and output_public/Lilly_NCT03421379 (their USDM keys are in
public_protocols/). Offline: OPENROUTER_OFFLINE=1 is forced, so search() runs on an eval question whose query
embedding and rerank are cached (reports/eval/public/<name>.json), an uncached question must raise OfflineCacheMiss,
and a fake client (embed_fn / rerank_fn) covers the uncached path without any network call.

Checks: the source-sha guard; every tool and its provenance; read_section of the inclusion / exclusion roles holds
every criterion of the USDM key (usdm_gold's reader); continuation cursors lose nothing; table chains are joined;
get_soa with and without soa.py; quote_check positive / normalised / table / cross-item / furniture / negative /
section-scoped / elided; the reading-order repair (Lilly p. 26); tool schemas are valid JSON Schema; call_tool never
raises; the CLI.
"""

import hashlib
import json
import os
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
