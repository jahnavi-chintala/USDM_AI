"""Inventory of the protocol tools' result fields (docs/api_reference.md, "5. Protocol tools").

Surfaces:
  tool:<name>   the result JSON of each of the 9 tools of protocol_api.TOOLS (document_info, outline,
                find_sections, read_section, get_table, get_soa, search, abbreviation, quote_check)
  tool-error    the {"error", ...} result that ProtocolDocument.call_tool returns instead of raising

Each public output (output_public/<name>) that exists is opened with ProtocolDocument.open and deterministic fake
embed_fn / rerank_fn, so `search` runs offline with its real result shape (only the ranking is fake). Every tool
is called through call_tool with argument variants that reach its optional fields (role / number / title / cursor
reads, every table, caption queries, SoA filters, both glossary directions, every quote_check tier and layer,
elisions) and with invalid arguments for the error results. One extra open without the fake functions runs a
`search` whose query is in no cache, to observe OfflineCacheMiss: OPENROUTER_OFFLINE is forced to 1 here, so that
call raises before any request is made. Nothing is written; only field names are printed.

    python scripts/api_inventory_tools.py [--json]

ERROR_TYPES is filled by inventory(): tool name -> the error_type values observed (None: call_tool's own checks).
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"  # a search cache miss raises OfflineCacheMiss instead of calling OpenRouter
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import api_inventory_common as common  # noqa: E402

PUBLIC = ("Alexion_NCT04573309", "Bayer_NCT04304508", "Lilly_NCT03421379", "LZZT_CDISC_Pilot",
          "PrECOG_NCT01797120", "Sanofi_NCT03637764")
TOOL_NAMES = ("document_info", "outline", "find_sections", "read_section", "get_table", "get_soa", "search",
              "abbreviation", "quote_check")

# Maps whose keys are data, per tool (their keys become '*'):
#   document_info.cover_page               cover-page field name (identity._COVER_FIELDS) -> value
#   document_info.roles_found              role name -> outermost section ids
#   document_info.other_identifiers_cited  identifier kind (chunk_protocol.registry_identifiers "others") -> values
#   outline.roles                          role name -> what the USDM agent finds under it (structure.ROLE_INFO)
#   get_soa soas[].footnotes, lookup[].footnotes   footnote marker -> footnote text (soa.py)
DYNAMIC = {
    "document_info": ("cover_page", "roles_found", "other_identifiers_cited"),
    "outline": ("roles",),
    "get_soa": ("soas[].footnotes", "lookup[].footnotes"),
}

ERROR_TYPES: dict[str, set] = {}

_WORDS = re.compile(r"\w+")
_NO_MATCH = "Zqxjv wokhgf plorrb"  # a phrase no protocol contains


def _fake_embed(dim: int):
    import numpy as np

    def embed(text: str):
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")
        return np.random.default_rng(seed).standard_normal(dim).astype("float32")

    return embed


def _fake_rerank(query: str, chunks: list[dict], model: str) -> list[tuple[int, float]]:
    """(candidate position, score) best first: the share of the query's words in the chunk text."""
    q = {w.lower() for w in _WORDS.findall(query)}
    scored = []
    for i, c in enumerate(chunks):
        words = {w.lower() for w in _WORDS.findall(c.get("embed_text") or c.get("text") or "")}
        scored.append((i, round(len(q & words) / (len(q) or 1), 6)))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored


class _Calls:
    """call_tool with every result's paths merged into the inventory."""

    def __init__(self) -> None:
        self.inv: dict[str, set[str]] = {}

    def __call__(self, doc, name: str, args: dict | None = None) -> dict | None:
        res = doc.call_tool(name, dict(args or {}))
        if isinstance(res, dict) and "error" in res:
            common.merge(self.inv, "tool-error", common.paths(res))
            ERROR_TYPES.setdefault(name, set()).add(res.get("error_type"))
            return None
        common.merge(self.inv, f"tool:{name}", common.paths(res, DYNAMIC.get(name, ())))
        return res


def _words_of(text: str, n: int) -> str:
    return " ".join(text.split()[:n])


def _subtree(sections: list[dict], sid: str) -> set[str]:
    kids: dict[str | None, list[str]] = {}
    for s in sections:
        kids.setdefault(s["parent"], []).append(s["id"])
    out, todo = set(), [sid]
    while todo:
        x = todo.pop()
        out.add(x)
        todo += kids.get(x, [])
    return out


def _body_texts(doc) -> list:
    """Body paragraphs (label text, >= 25 words) outside figures, in document order."""
    from docling_core.types.doc import ContentLayer, TextItem

    in_pictures = {ch.self_ref for p in doc.doc.pictures
                   for ch, _ in doc.doc.iterate_items(root=p, with_groups=False, traverse_pictures=True)
                   if ch is not p}
    out = []
    for item, _ in doc.doc.iterate_items(with_groups=False, included_content_layers={ContentLayer.BODY}):
        if (isinstance(item, TextItem) and item.label.value == "text"
                and item.self_ref not in in_pictures and len(item.text.split()) >= 25):
            out.append(item)
    return out


def _probe(doc, call: _Calls) -> None:
    from protocol_api import ROLE_FALLBACK
    from structure import ROLES

    # ---- document_info, outline
    call(doc, "document_info")
    out = call(doc, "outline")
    call(doc, "outline", {"max_level": 1})
    call(doc, "outline", {"max_level": 2, "with_roles": False})
    call(doc, "outline", {"max_level": 0})
    call(doc, "document_info", {"section": "S1"})  # schema: no arguments
    call(doc, "outline", {"max_level": -1})  # schema minimum
    sections = out["sections"]
    by_id = {s["id"]: s for s in sections}
    present = [r for r in ROLES if any(s.get("role") == r for s in sections)]
    roots = {r: [s["id"] for s in sections if s.get("role") == r
                 and not (s["parent"] in by_id and by_id[s["parent"]].get("role") == r)] for r in present}
    absent_fb = [r for r in ROLE_FALLBACK if r not in present and any(f in present for f in ROLE_FALLBACK[r])]
    absent = [r for r in ROLE_FALLBACK if r not in present and not any(f in present for f in ROLE_FALLBACK[r])]
    numbered = [s for s in sections if s.get("number")]
    tops = [s["id"] for s in sections if s["parent"] is None]

    # ---- find_sections
    for r in present:
        call(doc, "find_sections", {"role": r})
    for r in (absent_fb + absent)[:2]:
        call(doc, "find_sections", {"role": r})
    for s in [s for s in sections if (s.get("level") or 0) >= 2][:3]:
        call(doc, "find_sections", {"query": re.sub(r"^[\d.]+\s*", "", s["title"]) or s["title"]})
    if numbered:
        call(doc, "find_sections", {"query": numbered[min(3, len(numbered) - 1)]["number"]})
        call(doc, "find_sections", {"query": f"Section {numbered[0]['number']}", "limit": 2})
    if present:
        call(doc, "find_sections", {"role": present[0], "query": "criteria", "limit": 50})
    call(doc, "find_sections", {"query": "study treatments"})
    call(doc, "find_sections", {"query": _NO_MATCH})
    call(doc, "find_sections", {})  # ProtocolAPIError
    call(doc, "find_sections", {"role": "no_such_role"})  # schema enum

    # ---- read_section: every top-level section completely (cursor followed), roles, numbers, titles, parts
    def read_all(args: dict) -> dict | None:
        res = call(doc, "read_section", args)
        first, n = res, 0
        while res and res.get("cursor") and n < 40:
            res = call(doc, "read_section", {"cursor": res["cursor"]})
            n += 1
        return first

    for sid in tops:
        read_all({"section": sid})
    for r in present:
        read_all({"section": r})
    for r in absent_fb[:2]:
        call(doc, "read_section", {"section": r})
    for r in absent[:1]:
        call(doc, "read_section", {"section": r})  # NotFoundError
    if numbered:
        call(doc, "read_section", {"section": numbered[min(5, len(numbered) - 1)]["number"]})
    for s in [s for s in sections if (s.get("level") or 0) >= 2][:2]:
        call(doc, "read_section", {"section": re.sub(r"^[\d.]+\s*", "", s["title"]) or s["title"]})
    big = max(sections, key=lambda s: s.get("tokens") or 0)
    part = call(doc, "read_section", {"section": big["id"], "max_tokens": 200})
    if part and part.get("cursor"):
        part2 = call(doc, "read_section", {"cursor": part["cursor"], "max_tokens": 200})
        if part2 and part2.get("cursor"):
            call(doc, "read_section", {"cursor": part2["cursor"]})
    call(doc, "read_section", {"section": big["id"], "include_subsections": False, "with_blocks": False})
    call(doc, "read_section", {"section": big["id"], "include_subsections": False, "max_tokens": 200})
    call(doc, "read_section", {})  # ProtocolAPIError
    call(doc, "read_section", {"section": _NO_MATCH})  # NotFoundError
    call(doc, "read_section", {"cursor": "no cursor"})  # NotFoundError
    call(doc, "read_section", {"cursor": f"{big['id']}|1|999999"})  # NotFoundError: outside the blocks
    call(doc, "read_section", {"section": big["id"], "max_tokens": 10})  # schema minimum

    # ---- get_table: every table, every format on a chained / glossary table, caption queries
    n_tables = len(doc.doc.tables)
    for i in range(n_tables):
        call(doc, "get_table", {"table": f"#/tables/{i}"})
    chained = [t for t, m in doc.tables.items() if len(m.get("chain") or []) > 1]
    pick = chained[0] if chained else (f"#/tables/{n_tables - 1}" if n_tables else None)
    if pick:
        for fmt in ("markdown", "grid", "rows"):
            call(doc, "get_table", {"table": pick, "format": fmt})
        call(doc, "get_table", {"table": pick.rsplit("/", 1)[1], "format": "all"})
    captions = [t.caption_text(doc.doc) for t in doc.doc.tables]
    for cap in [c for c in captions if c and len(c.split()) >= 3][:2]:
        call(doc, "get_table", {"table": _words_of(re.sub(r"^Table\s+\S+\s*", "", cap), 4)})
        call(doc, "get_table", {"table": _words_of(cap, 5), "format": "rows"})
    call(doc, "get_table", {"table": "study treatments"})
    call(doc, "get_table", {"table": _NO_MATCH})  # NotFoundError
    call(doc, "get_table", {"table": f"#/tables/{n_tables + 1000}"})  # NotFoundError
    call(doc, "get_table", {"table": "1", "format": "csv"})  # schema enum
    call(doc, "get_table", {})  # schema required

    # ---- get_soa
    soa = call(doc, "get_soa")
    call(doc, "get_soa", {"with_markdown": False})
    if soa and soa.get("soas"):
        s0 = soa["soas"][0]
        v = next((v["label"] for v in s0["visits"] if v.get("label")), None)
        a = next((a["label"] for a in s0["activities"] if a.get("label")), None)
        if v:
            call(doc, "get_soa", {"visit": v})
        if a:
            call(doc, "get_soa", {"activity": a})
        if v and a:
            call(doc, "get_soa", {"visit": v, "activity": a, "with_markdown": False})
    call(doc, "get_soa", {"visit": _NO_MATCH})
    call(doc, "get_soa", {"visit": 3})  # schema type

    # ---- search (fake embedding and rerank: real shape, fake ranking)
    abbrs = [k for k in (doc.glossary.entries if doc.glossary else {}) if len(k) >= 3 and k.isupper()]
    call(doc, "search", {"query": "What is the primary endpoint of the study?"})
    call(doc, "search", {"query": "Which visits include a physical examination and laboratory tests?", "k": 20})
    if abbrs:
        call(doc, "search", {"query": f"When is the {abbrs[0]} assessed?", "k": 3})
    call(doc, "search", {"query": "What is the planned number of participants?", "k": 1})
    call(doc, "search", {"query": "   "})  # ProtocolAPIError
    call(doc, "search", {"query": "dose", "rendered": True})  # not in the schema
    call(doc, "search", {"query": "dose", "k": 0})  # schema minimum

    # ---- abbreviation
    entries = doc.glossary.entries if doc.glossary else {}
    keys = list(entries)
    with_alt = [k for k, e in entries.items() if e.alternatives]
    for k in (keys[:1] + with_alt[:1]):
        call(doc, "abbreviation", {"term": k})
        call(doc, "abbreviation", {"term": entries[k].definition})
    if keys:
        call(doc, "abbreviation", {"term": keys[0].lower()})
        call(doc, "abbreviation", {"term": keys[-1] + "s"})
        call(doc, "abbreviation", {"term": keys[0] + "Q"})
    call(doc, "abbreviation", {"term": "adverse event"})
    call(doc, "abbreviation", {"term": _NO_MATCH})
    call(doc, "abbreviation", {"term": " "})  # ProtocolAPIError
    call(doc, "abbreviation", {})  # schema required

    # ---- quote_check: tiers, layers, section restriction, elision, closest
    texts = _body_texts(doc)
    if texts:
        t = texts[len(texts) // 2]
        words = t.text.split()
        q = " ".join(words[2:14])
        res = call(doc, "quote_check", {"quote": q})
        call(doc, "quote_check", {"quote": q.upper()})  # normalized
        call(doc, "quote_check", {"quote": re.sub(r"[\W_]+", "", q)})  # compact
        call(doc, "quote_check", {"quote": f"{' '.join(words[0:6])} ... {' '.join(words[-6:])}"})  # elided
        call(doc, "quote_check", {"quote": f"{' '.join(words[0:6])} … {_NO_MATCH}"})  # elided, a part missing
        changed = words[2:14]
        changed[5] = "zqxv"
        call(doc, "quote_check", {"quote": " ".join(changed)})  # not found: closest
        sid = (res or {}).get("provenance", {}).get("section_id")
        if sid in by_id:
            call(doc, "quote_check", {"quote": q, "section_id": sid})
            other = next((x for x in tops if sid not in _subtree(sections, x)), None)
            if other:
                call(doc, "quote_check", {"quote": q, "section_id": other})  # found_elsewhere
                call(doc, "quote_check", {"quote": " ".join(changed), "section_id": other})
    furniture = [x for x in doc.doc.texts if x.content_layer.value == "furniture" and len(x.text.split()) >= 2]
    if furniture:
        call(doc, "quote_check", {"quote": furniture[0].text})
    for tb in doc.doc.tables[:3]:
        grid = tb.data.grid or []
        for row in grid[1:3]:
            cells = list(dict.fromkeys(" ".join(c.text.split()) for c in row if c.text.strip()))
            if len(cells) >= 2:
                call(doc, "quote_check", {"quote": " | ".join(cells[:2])})
                break
    from chunk_protocol import figure_parts

    for pic in doc.doc.pictures:
        f = figure_parts(pic, doc.doc)
        for d in f["description"] + f["mermaid"]:
            w = d.split()
            if len(w) >= 16:
                call(doc, "quote_check", {"quote": " ".join(w[8:16])})
                break
        else:
            continue
        break
    for pic in doc.doc.pictures:
        f = figure_parts(pic, doc.doc)
        if f["texts"]:
            call(doc, "quote_check", {"quote": f["texts"][0]})
            break
    call(doc, "quote_check", {"quote": "the study"})  # many locations
    call(doc, "quote_check", {"quote": "!!"})  # ProtocolAPIError
    call(doc, "quote_check", {"quote": "study", "section_id": "S999.999"})  # NotFoundError
    call(doc, "quote_check", {"quote": "study", "max_locations": 3})  # not in the schema

    # ---- an unknown tool
    call(doc, "no_such_tool", {})
    return None


def inventory() -> dict[str, set[str]]:
    import numpy as np

    from protocol_api import ProtocolDocument

    ERROR_TYPES.clear()
    call = _Calls()
    level = logging.root.manager.disable
    logging.disable(logging.CRITICAL)  # tool logs can quote document text
    try:
        offline_done = False
        for name in PUBLIC:
            out = ROOT / "output_public" / name
            if not (out / "chunks" / "stats.json").is_file():
                continue
            emb = out / "chunks" / "embeddings.npy"
            dim = int(np.load(emb, mmap_mode="r").shape[1]) if emb.is_file() else 8
            doc = ProtocolDocument.open(out, embed_fn=_fake_embed(dim), rerank_fn=_fake_rerank)
            _probe(doc, call)
            del doc
            if not offline_done and emb.is_file():
                # the real client with OPENROUTER_OFFLINE=1: a query in no cache raises OfflineCacheMiss
                real = ProtocolDocument.open(out)
                call(real, "search", {"query": f"{_NO_MATCH} inventory probe"})
                offline_done = True
                del real
    finally:
        logging.disable(level)
    return call.inv


if __name__ == "__main__":
    common.print_inventory(inventory(), "--json" in sys.argv)
