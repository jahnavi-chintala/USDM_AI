"""Document access layer (tools) for an LLM agent that builds USDM 4 data from ONE converted protocol.

The agent asks enumeration questions ("all inclusion criteria", "every visit of the SoA") and must be able to
trace and verify every value it extracts, so every tool returns complete content with provenance:

    provenance = {"section_id", "section_number", "title", "pages", "refs"}

(section of the structure.py index, PDF page numbers, Docling item refs such as "#/texts/85" / "#/tables/3").

    doc = ProtocolDocument.open("output_public/LZZT_CDISC_Pilot")   # folder with the Docling JSON + chunks/
    doc.document_info()                      titles, acronym, sponsor protocol id, registry ids (with their verbatim
                                             source line), version/amendment/date, sponsor, page/section/SoA counts;
                                             each value with its method and the item it was read from (identity.py;
                                             chunks/identity.json, else the rules), and the rule value a model's
                                             choice replaced ("rules")
    doc.outline(max_level=None)              section tree: section_id, number, title, level, ICH M11 role, pages, tokens,
                                             parent_id, children
    doc.find_sections(role=None, query=None) sections by role and/or fuzzy title match, ranked
    doc.read_section("inclusion")            complete Markdown of a section (id, number or role), tables as Markdown,
                                             figures with OCR text + AI description (labelled), lists with their own
                                             markers; page markers "<!-- p. 12 -->" and section ids "{#S5.1}" inline;
                                             per-block provenance; a continuation cursor instead of silent truncation
    doc.get_table("#/tables/3" | "caption words")   whole table: Markdown + grid + merged cells + footnotes, chains joined
    doc.get_soa()                            soa.extract_soa(doc, index) (+ Markdown), cached; "not available" until soa.py exists
    doc.search("question", k=PRODUCTION_K)   dense + BM25 candidates, reranked, production parent-aware context
                                             (retrieve.production_builder): ranked blocks + the identity block (its
                                             Title line: the detected title, or the official title when the identity
                                             model step's verified choice replaced it)
    doc.abbreviation("AE" | "adverse event") glossary lookup in both directions
    doc.quote_check("verbatim text", section_id=None)   does the text occur in the document? where?
    doc.tool_schemas() / doc.call_tool(name, args)       JSON-Schema tool definitions and a dispatcher

Everything is local except search(): it embeds the question (qwen3-embedding-8b) and reranks the candidates
(qwen3-reranker-8b) via OpenRouter with zero-data-retention routing; both are cached in the chunks folder
(query_embedding_cache.json, rerank_cache.json). With OPENROUTER_OFFLINE=1 a cache miss raises OfflineCacheMiss.
Tests or other callers may pass embed_fn / rerank_fn to ProtocolDocument.open (no network).

The Docling JSON is verified against the chunks' source sha (as retrieve.ContextBuilder does): item refs are
positional, so a regenerated JSON would silently return other text.

CLI: python protocol_api.py OUT_DIR info|outline|find Q|section ID|table ID|soa|search "q"|quote "text"|abbr T|schemas
Usage guide for the USDM agent: docs/protocol_api.md.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import hashlib
import json
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from identity import MODEL_METHOD, build_identity, load_record
from structure import ROLE_INFO, ROLES, SectionIndex, _norm, _tokens as _title_tokens, _w, heading_role
from tokenizer_cache import shared_tokenizer

log = logging.getLogger("protocol_api")

API_VERSION = "1"
TOKENIZER = "Qwen/Qwen3-Embedding-8B"  # the embedding model's tokenizer, as everywhere else in the pipeline
DEFAULT_TOOL_MAX_TOKENS = 16000  # read_section via call_tool: longer content comes with a continuation cursor
QUOTE_CONTEXT = 160  # characters of verbatim text kept on each side of a quote_check match
MAX_LOCATIONS = 10
ELIDED_GAP = 2000  # quote_check "a … b": b starts at most this many characters (stream offsets) after a ends

# A role a protocol may not have as its own section, and where its content then lives.
ROLE_FALLBACK = {
    "inclusion": ["eligibility"], "exclusion": ["eligibility"], "eligibility": ["inclusion", "exclusion"],
    "estimands": ["objectives_endpoints"], "schema": ["design"], "sample_size": ["statistics"],
    "interim_analysis": ["statistics"], "dose_modification": ["interventions"],
    "concomitant_therapy": ["interventions"], "assessments_efficacy": ["assessments"],
    "assessments_safety": ["assessments"], "pk_pd_biomarkers": ["assessments"], "screen_failures": ["population"],
    "lifestyle": ["population"],
}
# read_section(role): subsections of these roles stay with a section of the key role (others are listed as excluded).
ROLE_FAMILY = {
    "population": {"eligibility", "inclusion", "exclusion", "lifestyle", "screen_failures"},
    "eligibility": {"inclusion", "exclusion"},
    "objectives_endpoints": {"estimands"},
    "design": {"schema"},
    "synopsis": {"schema", "soa"},
    "interventions": {"dose_modification", "concomitant_therapy"},
    "assessments": {"assessments_efficacy", "assessments_safety", "pk_pd_biomarkers", "ae_reporting"},
    "statistics": {"sample_size", "interim_analysis", "estimands"},
}


class ProtocolAPIError(Exception):
    """A request the document cannot answer as asked (bad arguments, unknown role, ...)."""


class NotFoundError(ProtocolAPIError, LookupError):
    """No section / table / cursor matches the request."""


class SearchUnavailable(ProtocolAPIError, RuntimeError):
    """search() needs embeddings (embed_chunks.py embed) that this folder does not have."""


class OfflineCacheMiss(RuntimeError):
    """OPENROUTER_OFFLINE=1 and the query embedding or rerank result is not cached."""


# =========================================================================== small helpers

def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _pages_of(item: Any) -> list[int]:
    return sorted({p.page_no for p in getattr(item, "prov", None) or []})


def _page_marker(pages: list[int]) -> str:
    return f"<!-- p. {pages[0]} -->" if pages[0] == pages[-1] else f"<!-- pp. {pages[0]}-{pages[-1]} -->"


def _jsonable(x: Any) -> Any:
    """A JSON-serialisable copy (tuples/sets -> lists, numpy scalars -> numbers, other objects -> str)."""
    return json.loads(json.dumps(x, ensure_ascii=False, default=lambda o: o.tolist() if hasattr(o, "tolist")
                                 else sorted(o) if isinstance(o, (set, frozenset)) else str(o)))


def _offline() -> bool:
    """OPENROUTER_OFFLINE as everywhere else (1 / true / yes / on): a cache miss is OfflineCacheMiss."""
    from retrieve import offline

    return offline()


@dataclass
class _Blk:
    """One rendered unit of a section: its heading, a paragraph, a whole list, a table, a figure, a label."""
    kind: str  # heading | text | list | table | figure | label | footnote | caption | formula | toc_table
    text: str
    section_id: str
    ref: str | None
    pages: list[int]
    leaf_refs: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    tokens: int = -1


@dataclass
class _Seg:
    """A piece of verbatim document text for quote_check (one text item or one table row)."""
    text: str
    ref: str
    pages: list[int]
    layer: str  # body | furniture | ai_generated
    kind: str  # the Docling label, or table_row / figure_text / figure_description
    row: int | None = None


class _Stream:
    """Normalised segments joined into one string; a match maps back to its segments by offset."""

    def __init__(self, segs: list[_Seg], fn: Callable[[str], str], sep: str):
        self.segs = segs
        self.starts: list[int] = []
        self.lens: list[int] = []
        parts, pos = [], 0
        for s in segs:
            t = fn(s.text)
            self.starts.append(pos)
            self.lens.append(len(t))
            parts.append(t)
            pos += len(t) + len(sep)
        self.text = sep.join(parts)

    def find_all(self, needle: str) -> list[tuple[int, int]]:
        out, i = [], self.text.find(needle)
        while i >= 0:
            out.append((i, i + len(needle)))
            i = self.text.find(needle, i + 1)
        return out

    def span_segments(self, a: int, b: int) -> list[int]:
        i0 = max(0, bisect.bisect_right(self.starts, a) - 1)
        i1 = max(i0, bisect.bisect_right(self.starts, max(a, b - 1)) - 1)
        return [i for i in range(i0, i1 + 1) if self.lens[i] > 0 or i == i0]


_LAYERS = ("body", "furniture", "ai_generated")  # quote_check's text streams, in this order
_WS = re.compile(r"\s+")
_COMPACT_DROP = re.compile(r"[^\w≥≤<>=%±]|_")
_WORD = re.compile(r"[^\W_]+")
_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…)\s*")
_MD_NOISE = re.compile(r"<!--.*?-->|\{#S[^}\s]*\}")


def _exact_form(s: str) -> str:
    return _WS.sub(" ", s).strip()


def _norm_form(s: str) -> str:
    from keyterm_match import normalize

    return normalize(s)


def _compact_form(s: str) -> str:
    return _COMPACT_DROP.sub("", _norm_form(s))


def _quote_forms(quote: str) -> dict[str, str]:
    """The quote at each match tier, without Markdown page markers / section ids / heading marks / table pipes."""
    q = _MD_NOISE.sub(" ", quote)
    q = re.sub(r"(?m)^\s*#{1,6}\s+", "", q).strip().strip("|").strip()
    return {"exact": _exact_form(q), "normalized": _norm_form(q), "compact": _compact_form(q)}


def _sequences(st: _Stream, needles: list[str]) -> list[tuple[int, int]]:
    """(start of the first needle, end of the last) of the in-order sequences of the needles in one stream, tight and
    never overlapping. First-needle occurrences are tried in order: from one, each next needle tries its occurrences
    starting at or after the end of the previous one and at most ELIDED_GAP characters after it, in order, and takes
    the first from which the remaining needles complete the chain (a depth-first search over these windows, decided
    once per occurrence, from the last needle back). A complete chain starts at the LATEST first-needle occurrence
    that ends by the chosen second needle's start (from there the chain picks the same later occurrences); the next
    sequence starts at or after its end."""
    occ = [st.find_all(n) for n in needles]
    if not all(occ):
        return []
    starts = [[a for a, _ in o] for o in occ]
    # last[k][j]: the end of the last needle of the chain that continues from occurrence j of needle k (None: none
    # completes); ok[k][j]: the first occurrence >= j of needle k from which a chain completes (None: no such one)
    last: list[list[int | None]] = [[] for _ in needles]
    ok: list[list[int | None]] = [[] for _ in needles]

    def link(k: int, end: int) -> int | None:
        """The first occurrence of needle k inside the window after `end` from which the chain completes."""
        j = ok[k][bisect.bisect_left(starts[k], end)]
        return j if j is not None and starts[k][j] - end <= ELIDED_GAP else None

    for k in range(len(needles) - 1, 0, -1):
        if k == len(needles) - 1:
            last[k] = [b for _, b in occ[k]]
        else:
            for _, b in occ[k]:
                j = link(k + 1, b)
                last[k].append(None if j is None else last[k + 1][j])
        ok[k] = [None] * (len(occ[k]) + 1)
        for j in range(len(occ[k]) - 1, -1, -1):
            ok[k][j] = j if last[k][j] is not None else ok[k][j + 1]
    first_starts, first_ends = starts[0], [b for _, b in occ[0]]
    out, i = [], 0
    while i < len(first_starts):
        j = link(1, first_ends[i])
        if j is None:
            i += 1
            continue
        end = last[1][j]
        out.append((first_starts[bisect.bisect_right(first_ends, starts[1][j]) - 1], end))
        i = bisect.bisect_left(first_starts, end)
    return out


def _listed(matches: list[tuple], k: int) -> list[tuple]:
    """The matches (layer, ...) quote_check lists, at most k: body before furniture, as found; when both layers
    matched and k >= 4, at least min(2, furniture matches) page header/footer ones, in place of the last body ones."""
    body = [m for m in matches if m[0] == "body"]
    furniture = [m for m in matches if m[0] == "furniture"]
    n = min(len(body), k - min(2, len(furniture)) if body and furniture and k >= 4 else k)
    return body[:n] + furniture[:k - n]


# =========================================================================== the document

class ProtocolDocument:
    """One converted protocol: Docling JSON + chunks/ (chunks.jsonl, parents.jsonl, sections.json, tables.jsonl,
    glossary.json, stats.json; embeddings for search). Open with ProtocolDocument.open(out_dir).
    Not thread-safe: use one lock per document, as service.app_service does (the document, its section index and the
    search builder share lazily built state)."""

    def __init__(self, out_dir: Path, chunks_dir: Path, doc_path: Path, doc: Any, stats: dict, *,
                 embed_fn: Callable[[str], Any] | None = None,
                 rerank_fn: Callable[[str, list[dict], str], list[tuple[int, float]]] | None = None,
                 tokenizer: str = TOKENIZER):
        from chunk_protocol import ProtocolSerializerProvider
        from parents import owned_refs

        self.out_dir, self.chunks_dir, self.doc_path, self.doc = out_dir, chunks_dir, doc_path, doc
        self.stats = stats
        self.meta: dict = stats.get("document") or {}
        self.source_sha256: str | None = self.meta.get("source_sha256")
        self.warnings: list[str] = []
        self.index = self._load_section_index()
        # The stored identity record (chunks/identity.json) is loaded once, here, so a file that cannot be used is
        # warned about from the first tool call on; the rules' record is built later, only if it is needed. It must
        # belong to this Docling JSON and to these stats.json values ("stale": chunk_protocol.py was run again).
        self._stored_identity, reason = load_record(chunks_dir, self.source_sha256, self.meta)
        if self._stored_identity is None and reason != "missing":
            self.warnings.append(f"chunks/identity.json not used ({reason}): the identity was rebuilt with the rules")
        self.glossary = None
        gpath = next((p for p in (chunks_dir / "glossary.json", chunks_dir / "abbreviations.json") if p.exists()), None)
        if gpath is not None:
            from glossary import Glossary

            self.glossary = Glossary.load(gpath)
        tfile = chunks_dir / "tables.jsonl"
        self.tables: dict[str, dict] = ({t["table_id"]: t for t in map(json.loads, tfile.read_text(
            encoding="utf-8").splitlines()) if t} if tfile.exists() else {})
        self._owned = owned_refs(doc)
        self.serializer = ProtocolSerializerProvider().get_serializer(doc)
        self._tokenizer_name = tokenizer
        self._tok = None
        self._embed_fn, self._rerank_fn = embed_fn, rerank_fn
        self._blocks: dict[str, list[_Blk]] = {}
        self._sizes: dict[str, tuple[int, int]] | None = None
        self._streams: dict[str, list[_Stream]] | None = None
        self._words: tuple[list[str], list[int], list[_Seg]] | None = None
        self._identity: dict | None = None  # the identity record: the stored one, else the rules' (built on first use)
        self._info: dict | None = None  # document_info's result (without the call-time warnings)
        self._soa: list[dict] | None = None
        self._builder = None
        self._qcache: dict | None = None
        self._moves_cache: dict[str, tuple[str, str]] | None = None
        self._moved_refs: dict[str, str] = {}

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(cls, out_dir: str | Path, *, embed_fn: Callable[[str], Any] | None = None,
             rerank_fn: Callable[[str, list[dict], str], list[tuple[int, float]]] | None = None,
             tokenizer: str = TOKENIZER) -> "ProtocolDocument":
        """Load one converted protocol. out_dir holds the Docling JSON and chunks/ (output/, output_public/<name>/);
        a chunks folder itself is accepted too. Raises if the JSON is not the one the chunks were made from."""
        from docling_core.types.doc import DoclingDocument

        out = Path(out_dir)
        if (out / "chunks" / "stats.json").is_file():
            chunks = out / "chunks"
        elif (out / "stats.json").is_file():
            chunks, out = out, out.parent
        else:
            raise FileNotFoundError(f"{out}: no chunks/stats.json; run chunk_protocol.py on the converted JSON first")
        stats = json.loads((chunks / "stats.json").read_text(encoding="utf-8"))
        meta = stats.get("document") or {}
        name = meta.get("source_json")
        if not name:
            raise FileNotFoundError(f"{chunks / 'stats.json'} names no source_json")
        path = next((p for p in (out / name, chunks / name, Path(name)) if p.is_file()), None)
        if path is None:
            raise FileNotFoundError(f"source JSON {name!r} of {chunks} not found in {out}")
        sha = _sha16(path)
        want = meta.get("source_sha256")
        if want and sha != want:
            raise RuntimeError(f"{path} (sha {sha}) is not the JSON these chunks were made from (sha {want}); "
                               "re-run chunk_protocol.py or restore the JSON")
        cfile = chunks / "chunks.jsonl"
        if cfile.is_file():
            with open(cfile, encoding="utf-8") as fh:
                first = fh.readline().strip()
            csha = json.loads(first).get("source_sha256") if first else None
            if csha and csha != sha:
                raise RuntimeError(f"{cfile} was made from another JSON (sha {csha}) than {path} (sha {sha})")
        doc = DoclingDocument.load_from_json(path)
        return cls(out, chunks, path, doc, stats, embed_fn=embed_fn, rerank_fn=rerank_fn, tokenizer=tokenizer)

    def _load_section_index(self):
        """The section index stored in chunks/sections.json: the one the chunk records were made with (it equals
        parents.section_index(doc) for the same code). Absent, unreadable or of another format: rebuilt from the
        document (with a warning unless the file is absent)."""
        from parents import FallbackSectionIndex, section_index

        f = self.chunks_dir / "sections.json"
        if not f.exists():
            return section_index(self.doc)
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "version" in data:
                return SectionIndex.from_json(data)
            if isinstance(data, dict) and "source" in data:
                return FallbackSectionIndex.from_json(data)
        except Exception:  # noqa: BLE001 - unreadable, or not a format this code writes
            pass
        self.warnings.append("sections.json could not be loaded; the section index was rebuilt from the Docling JSON")
        return section_index(self.doc)

    # ------------------------------------------------------------------ tokens
    @property
    def tokenizer(self):
        if self._tok is None:
            self._tok = shared_tokenizer(self._tokenizer_name)  # one per process, shared with the context builders
        return self._tok

    def count_tokens(self, text: str) -> int:
        """Tokens of the Qwen3 tokenizer (no special tokens)."""
        return len(self.tokenizer(text, add_special_tokens=False)["input_ids"]) if text else 0

    def _count_blocks(self, blocks: list[_Blk]) -> None:
        todo = [b for b in blocks if b.tokens < 0]
        if not todo:
            return
        texts = [b.text for b in todo]
        idx = [i for i, t in enumerate(texts) if t]
        counts = self.tokenizer([texts[i] for i in idx], add_special_tokens=False)["input_ids"] if idx else []
        for b in todo:
            b.tokens = 0
        for i, ids in zip(idx, counts):
            todo[i].tokens = len(ids)

    # ------------------------------------------------------------------ index helpers
    def _resolve(self, ref: str):
        from docling_core.types.doc import RefItem

        return RefItem(cref=ref).resolve(self.doc)

    def _section_of(self, ref: str | None) -> str | None:
        """Section id of any item ref (after the reading-order repair, see _moves)."""
        from parents import _sid

        if not ref:
            return None
        self._moves()
        return self._moved_refs.get(ref) or _sid(self.index.section_of(ref))

    def _children(self, sid: str | None) -> list[str]:
        from parents import _ids

        return _ids(self.index.children(sid))

    def _subtree(self, sid: str) -> list[str]:
        out = [sid]
        for k in self._children(sid):
            out += self._subtree(k)
        return out

    def _path(self, sid: str) -> list[str]:
        out, s = [], self.index.by_id.get(sid)
        while s is not None:
            out.append(s.section_id)
            s = self.index.by_id.get(s.parent_id) if s.parent_id else None
        return out[::-1]

    def _breadcrumb(self, sid: str) -> str:
        return " > ".join(self.index.by_id[s].title for s in self._path(sid) if s in self.index.by_id)

    def _prov(self, sid: str | None, pages: Any = (), refs: Any = (), **extra: Any) -> dict:
        s = self.index.by_id.get(sid) if sid else None
        out = {"section_id": sid, "section_number": getattr(s, "number", None), "title": getattr(s, "title", None),
               "pages": sorted({int(p) for p in pages if p is not None}),
               "refs": [r for r in dict.fromkeys(refs) if r]}
        out.update(extra)
        return out

    def _sec_summary(self, sid: str) -> dict:
        s = self.index.by_id[sid]
        return {"section_id": sid, "number": s.number, "title": s.title, "level": s.level, "role": s.role,
                "pages": [s.pages[0], s.pages[-1]] if s.pages else []}

    def _role_roots(self, role: str) -> list[str]:
        by = self.index.by_id
        return [s.section_id for s in self.index.sections if s.role == role
                and not (s.parent_id and s.parent_id in by and by[s.parent_id].role == role)]

    # ------------------------------------------------------------------ rendering
    def _heading_block(self, s: Any) -> _Blk:
        if s.section_id == "S0" or not s.heading_ref:
            text = f"# {s.title} (text before the first heading) {{#{s.section_id}}}"
            return _Blk("heading", text, s.section_id, None, [])
        level = max(1, min(6, int(s.level or 1)))
        try:
            pages = _pages_of(self._resolve(s.heading_ref))
        except Exception:  # noqa: BLE001
            pages = []
        return _Blk("heading", f"{'#' * level} {s.title} {{#{s.section_id}}}", s.section_id, s.heading_ref, pages,
                    [s.heading_ref])

    def _section_blocks(self, sid: str) -> list[_Blk]:
        """The section's heading and its own units (subsections excluded), rendered; cached."""
        if sid not in self._blocks:
            s = self.index.by_id[sid]
            out = [self._heading_block(s)]
            for ref, moved_from in self._section_units(sid):
                if ref in self._owned:  # a caption / footnote / figure text: rendered with its owner
                    continue
                try:
                    item = self._resolve(ref)
                except Exception:  # noqa: BLE001 - a dangling ref in the index
                    self.warnings.append(f"{sid}: item {ref} could not be resolved")
                    continue
                blk = self._render_unit(item, sid)
                if blk is not None and blk.text.strip():
                    if moved_from:
                        blk.extra["moved_from"] = moved_from
                    out.append(blk)
            self._blocks[sid] = out
        return self._blocks[sid]

    # ------------------------------------------------------------------ reading-order repair
    def _anchor(self, ref_or_item: Any) -> tuple[int, float, float, float] | None:
        """(page, top, left, page width) of a unit's first provenance box, top-left origin."""
        from structure import _anchor

        return _anchor(self.doc, self._resolve(ref_or_item) if isinstance(ref_or_item, str) else ref_or_item)

    def _moves(self) -> dict[str, tuple[str, str]]:
        """Units the converter's reading order put BEFORE a heading although they lie BELOW it on the same page:
        unit ref -> (from, to section); structure.reading_order_moves. Post-processing already moves them in
        the document (structure.repair_reading_order), so this finds nothing on current outputs; it repairs
        documents converted before that step at read time. The section index and chunks are not changed here;
        read_section, outline and provenance use the repaired assignment, and every move is reported
        (document_info "reading_order_repairs", read_section warnings)."""
        if getattr(self, "_moves_cache", None) is None:
            from structure import reading_order_moves

            # The converter's decisions log has a "reading_order" entry once post-processing ran the repair;
            # units it left in place (moves that would not land in the target section) stay where they are.
            decisions = self.doc_path.with_name(self.doc_path.stem + ".decisions.json")
            try:
                repaired = "reading_order" in json.loads(decisions.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                repaired = False
            moves = {} if repaired else reading_order_moves(self.doc, self.index, self._owned)
            self._moves_cache = moves
            self._moved_refs: dict[str, str] = {}
            for ref, (_, to) in moves.items():
                item = self._resolve(ref)
                self._moved_refs[ref] = to
                for x, _ in self.doc.iterate_items(root=item, with_groups=True, traverse_pictures=True):
                    self._moved_refs[x.self_ref] = to
            if moves:
                log.info("reading-order repair: %d units moved", len(moves))
        return self._moves_cache

    def reading_order_repairs(self) -> list[dict]:
        out = []
        for ref, (frm, to) in self._moves().items():
            a = self._anchor(ref)
            out.append({"ref": ref, "page": a[0] if a else None, "from_section": frm, "to_section": to})
        return out

    def _section_units(self, sid: str) -> list[tuple[str, str | None]]:
        """(unit ref, moved_from) in reading order, with displaced units inserted where they lie on the page."""
        moves = self._moves()
        s = self.index.by_id[sid]
        out: list[tuple[str, str | None]] = [(r, None) for r in getattr(s, "item_refs", None) or []
                                             if not (r in moves and moves[r][0] == sid)]
        for ref, (frm, to) in moves.items():
            if to != sid:
                continue
            a = self._anchor(ref)
            pos = len(out)
            for i, (o, _) in enumerate(out):
                try:
                    ao = self._anchor(o)
                except Exception:  # noqa: BLE001
                    ao = None
                if ao and a and (ao[0] > a[0] or (ao[0] == a[0] and ao[1] > a[1])):
                    pos = i
                    break
            out.insert(pos, (ref, frm))
        return out

    def _render_unit(self, item: Any, sid: str) -> _Blk | None:
        from docling_core.types.doc import (GroupItem, ListItem, PictureItem, SectionHeaderItem, TableItem, TextItem,
                                            TitleItem)

        if isinstance(item, GroupItem):
            lines: list[str] = []
            refs: list[str] = []
            pages: set[int] = set()
            if item.label.value in ("list", "ordered_list"):
                n = self._list_lines(item.children, 0, lines, refs, pages)
                return _Blk("list", "\n".join(lines), sid, item.self_ref, sorted(pages), refs, {"items": n})
            parts = []
            for leaf, _ in self.doc.iterate_items(root=item, with_groups=False, traverse_pictures=False):
                if isinstance(leaf, TextItem) and leaf.text.strip() and leaf.self_ref not in self._owned:
                    parts.append(_clean(leaf.text))
                    refs.append(leaf.self_ref)
                    pages |= set(_pages_of(leaf))
            sep = " " if item.label.value == "inline" else "\n\n"
            return _Blk("text", sep.join(parts), sid, item.self_ref, sorted(pages), refs)
        if isinstance(item, TableItem):
            return self._table_block(item, sid)
        if isinstance(item, PictureItem):
            return self._figure_block(item, sid)
        pages = _pages_of(item)
        if isinstance(item, (SectionHeaderItem, TitleItem)):  # a minor heading kept inline (structure.py)
            return _Blk("label", f"**{_clean(item.text)}**", sid, item.self_ref, pages, [item.self_ref])
        if isinstance(item, ListItem):
            return _Blk("list", f"{item.marker or '-'} {_clean(item.text)}".strip(), sid, item.self_ref, pages,
                        [item.self_ref], {"items": 1})
        if isinstance(item, TextItem):
            text = item.text.strip()
            kind = {"footnote": "footnote", "caption": "caption", "formula": "formula"}.get(item.label.value, "text")
            return _Blk(kind, text, sid, item.self_ref, pages, [item.self_ref]) if text else None
        return None

    def _list_lines(self, children: list, depth: int, lines: list[str], refs: list[str], pages: set[int]) -> int:
        """Nested list rendering with the protocol's own markers ("[1]", "a)", "1."); returns the item count."""
        from docling_core.types.doc import ContentLayer, GroupItem, ListItem, PictureItem, TableItem, TextItem

        n = 0
        for ch_ref in children or []:
            try:
                ch = ch_ref.resolve(self.doc)
            except Exception:  # noqa: BLE001
                continue
            if getattr(ch, "content_layer", ContentLayer.BODY) != ContentLayer.BODY or ch.self_ref in self._owned:
                continue
            if isinstance(ch, ListItem):
                lines.append(("  " * depth + f"{ch.marker or '-'} {_clean(ch.text)}").rstrip())
                refs.append(ch.self_ref)
                pages |= set(_pages_of(ch))
                n += 1 + self._list_lines(ch.children, depth + 1, lines, refs, pages)
            elif isinstance(ch, GroupItem):
                n += self._list_lines(ch.children, depth + (0 if depth else 1), lines, refs, pages)
            elif isinstance(ch, (TableItem, PictureItem)):
                blk = self._render_unit(ch, "")
                if blk is not None:
                    lines.append(blk.text)
                    refs.append(ch.self_ref)
                    pages |= set(blk.pages)
            elif isinstance(ch, TextItem) and ch.text.strip():
                lines.append("  " * depth + _clean(ch.text))
                refs.append(ch.self_ref)
                pages |= set(_pages_of(ch))
        return n

    def _table_markdown(self, item: Any) -> str:
        from chunk_protocol import ProtocolTableSerializer

        t = self.tables.get(item.self_ref)
        if t and t.get("markdown"):
            return t["markdown"]
        return ProtocolTableSerializer("markdown").serialize(item=item, doc_serializer=self.serializer,
                                                             doc=self.doc).text

    def _glossary_tables(self) -> set[str]:
        return set(getattr(self.glossary, "table_refs", None) or [])

    def _table_block(self, item: Any, sid: str) -> _Blk:
        from docling_core.types.doc import DocItemLabel

        ref, pages = item.self_ref, _pages_of(item)
        if item.label == DocItemLabel.DOCUMENT_INDEX:
            return _Blk("toc_table", f"(Table of contents with {item.data.num_rows} rows: omitted here; use outline())",
                        sid, ref, pages, [ref], {"table_id": ref})
        md = self._table_markdown(item) or "(empty table)"
        extra: dict = {"table_id": ref, "rows": item.data.num_rows, "cols": item.data.num_cols}
        chain = (self.tables.get(ref) or {}).get("chain")
        if chain and len(chain) > 1:
            extra["chain"] = chain
            md += (f"\n\n(One fragment of a page-split table {', '.join(chain)}; get_table('{ref}') returns all "
                   "fragments together.)")
        if ref in self._glossary_tables():
            extra["glossary"] = True
        return _Blk("table", md, sid, ref, pages, [ref], extra)

    def _figure_block(self, pic: Any, sid: str) -> _Blk | None:
        from chunk_protocol import dropped_figure, figure_blocks, figure_parts, figure_title

        f = figure_parts(pic, self.doc)
        if dropped_figure(pic, self.doc, f):
            return None
        parts = [f"**{figure_title(pic, f)}**"]
        for label, text in figure_blocks(f):
            if label == "Description":  # every figure description comes from a vision model
                label = "Description (AI-generated; verify against the image)"
            parts.append(f"{label}:\n{text}")
        if len(parts) == 1:
            parts.append("(no caption text, OCR text or description is available for this figure)")
        extra: dict = {"figure_id": pic.self_ref, "ai_generated": bool(f["description"] or f["mermaid"])}
        uri = str(pic.image.uri) if getattr(pic, "image", None) is not None and pic.image.uri else ""
        if uri and not uri.startswith("data:"):
            extra["image"] = uri.replace("\\", "/")
        leaves = [pic.self_ref] + [c.cref for c in pic.children]
        return _Blk("figure", "\n\n".join(parts), sid, pic.self_ref, _pages_of(pic), leaves, extra)

    def _sizes_all(self) -> dict[str, tuple[int, int]]:
        """section id -> (own tokens incl. heading, subtree tokens), Qwen3 tokenizer."""
        if self._sizes is None:
            blocks = [b for s in self.index.sections for b in self._section_blocks(s.section_id)]
            self._count_blocks(blocks)
            own = {s.section_id: sum(b.tokens + 2 for b in self._section_blocks(s.section_id))
                   for s in self.index.sections}
            sizes: dict[str, tuple[int, int]] = {}

            def sub(sid: str) -> int:
                if sid not in sizes:
                    sizes[sid] = (own[sid], own[sid] + sum(sub(k) for k in self._children(sid)))
                return sizes[sid][1]

            for s in self.index.sections:
                sub(s.section_id)
            self._sizes = sizes
        return self._sizes

    # ================================================================== tools
    # ------------------------------------------------------------------ document_info
    def _identity_record(self) -> dict:
        """The identity record of the protocol: every identity value with the item it was read from. The stored
        chunks/identity.json when it belongs to this Docling JSON (the rules' values and the fields a model chose and
        the code verified; loaded and warned about when the document is opened), else the rules' record
        (rules_identity_record), built on first use. The same record serves document_info, the identity block of
        search and the refs of its "(source ...)" lines."""
        if self._identity is None:
            self._identity = (self._stored_identity if self._stored_identity is not None
                              else self.rules_identity_record())
        return self._identity

    def rules_identity_record(self) -> dict:
        """The identity record built with the rules (identity.build_identity with this module's section
        assignment), never the stored chunks/identity.json: what the identity stage starts from."""
        return build_identity(self.doc, self.meta, section_of=self._section_of)

    def document_info(self) -> dict:
        """Identity of the protocol for StudyTitle / StudyIdentifier / StudyVersion / sponsor Organization, each
        value with the rule that found it (method) and the item it was read from (page, layer, text, item ref, the
        refs the text spans, the table row, section): the identity record, no search. Computed once; every call
        returns its own copy with the document's warnings at that moment.

        A value's method is the rule that found it, or "model selection, verified in the cited lines": a value a model
        chose in the identity stage and the code found in the lines it cited (source). Such a value also has "rules":
        {"value", "method"}, the rule value it replaced (both None when the rules found none); the key is absent on a
        value the rules found. The record is chunks/identity.json when it belongs to this Docling JSON, else the rules'
        record (a warning says why when the file is there but unusable, from the first call of any tool on).

        version: "label" and "amendment" are the plain values; "label_detail" and "amendment_detail" (None when there is
        none) are the same fields as the titles are: value, method, source, and "rules" for a model's choice."""
        if self._info is None:
            self._info = self._build_document_info()
        out = copy.deepcopy(self._info)
        out["warnings"] = list(self.warnings)
        return out

    def _build_document_info(self) -> dict:
        """document_info's result from the identity record (without the call-time warnings)."""
        from retrieve import IDENTITY_FIELDS, identity_block, identity_overlay

        meta = self.meta
        ids = meta.get("identifiers")
        if ids is None:  # chunks written before identifiers were detected
            from chunk_protocol import registry_identifiers

            ids = registry_identifiers(self.doc, meta)
        record = self._identity_record()
        fields, cover = record["fields"], record["cover_page"]
        prov_refs: list[str] = []
        prov_pages: set[int] = set()

        def sourced(key: str, track: bool = True) -> dict | None:
            f = fields[key]
            if not f:
                return None
            src = f["source"]
            if src is not None:
                src = {**src, "refs": list(src["refs"])}
                if track and src["ref"]:
                    prov_refs.append(src["ref"])
                if track and src["page"]:
                    prov_pages.add(src["page"])
            out = {"value": f["value"], "method": f["method"], "source": src}
            if f.get("rules") is not None:  # a model's choice: the rule value it replaced
                out["rules"] = copy.deepcopy(f["rules"])
            return out

        def cover_value(key: str) -> dict | None:
            c = cover.get(key)
            if not c:
                return None
            ref = c["refs"][0]
            prov_refs.append(ref)
            prov_pages.add(c["page"])
            return {"value": c["value"], "source": {"page": c["page"], "layer": "body", "text": c["text"],
                                                    "refs": list(c["refs"]), "ref": ref,
                                                    "section_id": self._section_of(ref)}}

        titles = {key: sourced(key) for key in ("official_title", "brief_title", "acronym", "detected_title")}
        identifiers = {}
        for key, label in IDENTITY_FIELDS:
            identifiers[key] = sourced(key)
            if identifiers[key]:
                identifiers[key]["label"] = label
        version = {
            "label": (fields["version_label"] or {}).get("value"),
            "amendment": (fields["amendment"] or {}).get("value"),
            # the same two fields with method and source (and "rules" for a model's choice); not in the provenance
            "label_detail": sourced("version_label", track=False),
            "amendment_detail": sourced("amendment", track=False),
            "date": sourced("version_date"),
            "cover_amendment": cover_value("amendment"), "cover_version": cover_value("version"),
            "cover_approval_date": cover_value("approval_date"),
        }
        sponsor, sp = None, fields["sponsor"]
        if sp:  # the name, the sponsor as written, the rule and the item it was read from
            src = {**sp["source"], "refs": list(sp["source"]["refs"])}
            sponsor = {"name": sp["value"], "value": sp["verbatim"], "method": sp["method"], "source": src,
                       "provenance": self._prov(self._section_of(src["ref"]), [src["page"]], src["refs"])}
            if sp.get("rules") is not None:
                sponsor["rules"] = copy.deepcopy(sp["rules"])
        soa_tables = self._soa_tables()
        roles: dict[str, list[str]] = {}
        for r in ROLES:
            roots = self._role_roots(r)
            if roots:
                roles[r] = roots
        title_secs = roles.get("title_page") or ["S0"]
        return {
            "api_version": API_VERSION,
            "document": meta.get("doc_name"), "out_dir": self.out_dir.as_posix(), "source_json": self.doc_path.name,
            "source_sha256": self.source_sha256,
            "titles": titles,
            "identifiers": identifiers,
            "other_identifiers_cited": copy.deepcopy(record["others"]),
            "version": version,
            "sponsor": sponsor,
            "cover_page": copy.deepcopy(cover),
            "page_count": len(self.doc.pages),
            "section_count": len(self.index.sections),
            "soa_table_count": len(soa_tables),
            "soa_tables": soa_tables,
            "roles_found": roles,
            "reading_order_repairs": self.reading_order_repairs(),
            "identity_block": identity_block(*identity_overlay(meta, ids, record)).text,
            "provenance": self._prov(title_secs[0], prov_pages or {1}, prov_refs),
        }

    def _soa_tables(self) -> list[dict]:
        from docling_core.types.doc import DocItemLabel

        out = []
        for t in self.doc.tables:
            if t.label == DocItemLabel.DOCUMENT_INDEX:
                continue
            sid = self._section_of(t.self_ref)
            if sid and self.index.by_id[sid].role == "soa":
                out.append({"table_id": t.self_ref, "section_id": sid, "pages": _pages_of(t),
                            "rows": t.data.num_rows, "cols": t.data.num_cols, "caption": t.caption_text(self.doc) or None})
        return out

    # ------------------------------------------------------------------ outline
    def outline(self, max_level: int | None = None, with_roles: bool = True) -> dict:
        """The section tree in document order: section_id, number, title, level, role, pages [first, last], tokens
        of the whole subtree and of the section's own text, parent_id, children ids, tables/figures directly in the
        section. "id" and "parent" repeat section_id and parent_id (deprecated aliases)."""
        sizes = self._sizes_all()
        keep = {s.section_id for s in self.index.sections if max_level is None or (s.level or 0) <= max_level}
        out = []
        for s in self.index.sections:
            if s.section_id not in keep:
                continue
            blocks = self._section_blocks(s.section_id)
            e: dict[str, Any] = {"id": s.section_id, "section_id": s.section_id, "number": s.number, "title": s.title,
                                 "level": s.level}
            if with_roles:
                e["role"] = s.role
                e["role_score"] = round(float(s.role_score or 0), 2)
            e["pages"] = [s.pages[0], s.pages[-1]] if s.pages else []
            e["tokens"], e["own_tokens"] = sizes[s.section_id][1], sizes[s.section_id][0]
            e["parent"] = e["parent_id"] = s.parent_id
            e["children"] = [k for k in self._children(s.section_id) if k in keep]
            nt = sum(b.kind == "table" for b in blocks)
            nf = sum(b.kind == "figure" for b in blocks)
            if nt:
                e["tables"] = nt
            if nf:
                e["figures"] = nf
            out.append(e)
        tops = self._children(None)
        if "S0" in sizes and "S0" not in tops:
            tops = ["S0"] + tops
        present = [r for r in ROLES if any(s.role == r for s in self.index.sections)]
        res = {"sections": out, "count": len(out), "total_sections": len(self.index.sections),
               "page_count": len(self.doc.pages), "total_tokens": sum(sizes[t][1] for t in tops if t in sizes)}
        if with_roles:
            res["roles"] = {r: ROLE_INFO[r][2] for r in present}  # what the USDM agent finds under each role
        res["provenance"] = self._prov(None, [], [], title="whole document (section tree)")
        res["warnings"] = list(self.warnings)
        return res

    # ------------------------------------------------------------------ find_sections
    def find_sections(self, role: str | None = None, query: str | None = None, limit: int = 10) -> dict:
        """Sections by ICH M11 role and/or fuzzy title match, best first. Role matches list the outermost section
        of the role first ("root"); a query also matches section numbers ("5.1", "Section 5.1")."""
        if not role and not (query or "").strip():
            raise ProtocolAPIError("find_sections needs a role or a query")
        if role and role not in ROLES:
            raise ProtocolAPIError(f"unknown role {role!r}; roles: {', '.join(ROLES)}")
        sizes = self._sizes_all()
        cands = [s for s in self.index.sections if not role or s.role == role]
        scored = []
        if query and query.strip():
            q = query.strip()
            qt = _title_tokens(q)
            qn = _norm(q)
            num = re.match(r"(?i)^(?:section|§)?\s*(\d+(?:\.\d+)*)\.?$", q)
            qrole, qrs = heading_role(q)
            roots_q = set(self._role_roots(qrole)) if qrole else set()
            for s in cands:
                if num and s.number == num.group(1):
                    score = 1.0
                else:
                    cover, dice, sub = self._fuzzy(qt, qn, s.title)
                    title = 0.45 * cover + 0.35 * dice + 0.2 * sub
                    score = title
                    if qrole and s.role == qrole:  # the query names a role ("study treatments" -> interventions)
                        score = max(score, 0.85 * qrs * (0.6 + 0.4 * float(s.role_score or 0)))
                        score += 0.03 * (s.section_id in roots_q)
                    score += 0.05 * title  # ties: the better title match first
                if score >= 0.2:
                    scored.append((score, s))
            scored.sort(key=lambda x: -x[0])
        else:
            roots = set(self._role_roots(role))
            scored = sorted(((float(s.role_score or 0) + (1.0 if s.section_id in roots else 0.0), s) for s in cands),
                            key=lambda x: -x[0])
        roots = set(self._role_roots(role)) if role else set()
        results = []
        for score, s in scored[:limit]:
            results.append({"section_id": s.section_id, "number": s.number, "title": s.title, "level": s.level,
                            "role": s.role, "role_score": round(float(s.role_score or 0), 2),
                            "score": round(min(score, 2.0), 3), "root_of_role": s.section_id in roots if role else None,
                            "path": self._breadcrumb(s.section_id), "tokens": sizes[s.section_id][1],
                            "provenance": self._prov(s.section_id, s.pages, [s.heading_ref] if s.heading_ref else [])})
        out: dict[str, Any] = {"role": role, "query": query, "count": len(results), "sections": results,
                               "provenance": results[0]["provenance"] if results else self._prov(None)}
        if role and not results and cands:  # the role exists; the query matched none of its sections
            out["hint"] = (f"no section with role {role!r} matches the query {query!r}; {len(cands)} section(s) have "
                           f"this role: call find_sections with role={role!r} and no query to list them")
        elif role and not results and ROLE_FALLBACK.get(role):
            out["hint"] = (f"no section has role {role!r}; its content usually sits under "
                           f"{' / '.join(ROLE_FALLBACK[role])}")
        return out

    @staticmethod
    def _fuzzy(qt: list[str], qn: str, text: str) -> tuple[float, float, float]:
        tt = set(_title_tokens(text))
        qs = set(qt)
        if not qs or not tt:
            return 0.0, 0.0, 0.0
        wq, wt = sum(_w(t) for t in qs), sum(_w(t) for t in tt)
        wi = sum(_w(t) for t in qs & tt)
        return wi / wq, 2 * wi / (wq + wt), 1.0 if qn and qn in _norm(text) else 0.0

    # ------------------------------------------------------------------ read_section
    def _resolve_section(self, spec: str) -> tuple[str, list[str], list[str]]:
        """(mode, root section ids, warnings) for a section id, a section number, a role name or a title."""
        spec = (spec or "").strip()
        if not spec:
            raise ProtocolAPIError("read_section needs a section id, number or role (or a cursor)")
        by = self.index.by_id
        if spec in by:
            return "section", [spec], []
        if spec.lower() in ROLES:
            role = spec.lower()
            roots = self._role_roots(role)
            if roots:
                notes = [f"{len(roots)} sections have role {role!r}; all are returned in document order"] \
                    if len(roots) > 1 else []
                return "role", roots, notes
            for fb in ROLE_FALLBACK.get(role, []):
                roots = self._role_roots(fb)
                if roots:
                    return "role", roots, [f"no section has role {role!r}; returning role {fb!r} instead"]
            raise NotFoundError(f"no section has role {role!r}"
                                + (f" (nor {', '.join(ROLE_FALLBACK[role])})" if ROLE_FALLBACK.get(role) else "")
                                + "; use find_sections(query=...) or search()")
        m = re.match(r"(?i)^(?:section|§)?\s*(\d+(?:\.\d+)*)\.?$", spec)
        if m and f"S{m.group(1)}" in by:
            return "section", [f"S{m.group(1)}"], []
        hits = self.find_sections(query=spec, limit=5)["sections"]
        if hits and hits[0]["score"] >= 0.6:
            return "section", [hits[0]["section_id"]], [
                f"{spec!r} is not a section id, number or role; read the best title match "
                f"{hits[0]['section_id']} {hits[0]['title']!r} (score {hits[0]['score']})"]
        sugg = ", ".join(f"{h['section_id']} {h['title']!r}" for h in hits) or "none"
        raise NotFoundError(f"no section {spec!r} (not an id, number or role); closest titles: {sugg}")

    def read_section(self, section: str | None = None, include_subsections: bool = True,
                     max_tokens: int | None = None, *, role: str | None = None, cursor: str | None = None,
                     with_blocks: bool = True) -> dict:
        """Complete Markdown of a section (by id "S5.1", number "5.1", or role "inclusion"), with its subsections.

        Tables are Markdown (caption, header, every row, "Notes:" footnotes); figures show caption, OCR text and
        the AI-generated description/Mermaid (labelled as such); lists keep the protocol's own markers and
        nesting. Inline markers: section headings end with "{#<section id>}", "<!-- p. N -->" precedes content
        starting on a new page. In role mode every outermost section of the role is returned (document order),
        with subsections of the same role family; other subsections are listed under "excluded_subsections".
        When the content exceeds max_tokens, the result ends at a block boundary and "cursor" continues it
        (read_section(cursor=...)); nothing is cut silently. A single block larger than max_tokens is returned
        whole with a warning."""
        warnings: list[str] = []
        start = 0
        if cursor:
            try:
                key, inc, pos = cursor.rsplit("|", 2)
                start, include_subsections = int(pos), inc == "1"
            except ValueError:
                raise NotFoundError(f"invalid cursor {cursor!r}") from None
            spec = key
        else:
            spec = role or section
        mode, roots, notes = self._resolve_section(spec or "")
        warnings += notes
        role_name = spec.lower() if mode == "role" else None
        sids: list[str] = []
        excluded: list[dict] = []

        def walk(sid: str, fam: set) -> None:  # pre-order = document order
            sids.append(sid)
            for k in self._children(sid):
                if self.index.by_id[k].role in fam:
                    walk(k, fam)
                else:
                    excluded.append(self._sec_summary(k))

        for r in roots:
            if not include_subsections:
                sids.append(r)
            elif mode == "role":
                root_role = self.index.by_id[r].role
                walk(r, {root_role, None} | ROLE_FAMILY.get(root_role or "", set()))
            else:
                sids += self._subtree(r)
        sids = list(dict.fromkeys(sids))
        pieces = [b for sid in sids for b in self._section_blocks(sid)]
        self._count_blocks(pieces)
        if start < 0 or start >= len(pieces):
            raise NotFoundError(f"cursor position {start} is outside the {len(pieces)} blocks of {spec!r}")
        total = sum(b.tokens + 2 for b in pieces)
        end = len(pieces)
        if max_tokens is not None and max_tokens > 0:
            used, end = 0, start
            while end < len(pieces):
                g_end = end  # headings stick to the first content block after them
                while g_end < len(pieces) and pieces[g_end].kind == "heading":
                    g_end += 1
                g_end = min(len(pieces), g_end + 1)
                t = sum(pieces[i].tokens + 8 for i in range(end, g_end))  # + separators and page markers
                if end > start and used + t > max_tokens:
                    break
                if end == start and t > max_tokens:
                    warnings.append(f"blocks {start}-{g_end - 1} ({t} tokens: a heading with its first block, or one "
                                    f"block) exceed max_tokens {max_tokens}; returned whole, never cut")
                used += t
                end = g_end
        window = pieces[start:end]
        moved_in = [b for b in window if b.extra.get("moved_from")]
        if moved_in:
            pages_in = sorted({p for b in moved_in for p in b.pages[:1]})
            warnings.append(
                f"reading-order repair: {len(moved_in)} block(s) on p. {', '.join(map(str, pages_in))} "
                f"({', '.join(b.ref for b in moved_in)}) lie below this section's heading on the page but the "
                f"converter read them before it (they were in {', '.join(sorted({b.extra['moved_from'] for b in moved_in}))});"
                " they are shown where they stand on the page")
        moved_out = [(r, to) for r, (frm, to) in self._moves().items() if frm in sids and to not in sids]
        if moved_out:
            warnings.append(f"reading-order repair: {len(moved_out)} unit(s) of this content lie below a later "
                            f"heading on the page and are shown under that section: "
                            + ", ".join(f"{r} -> {to}" for r, to in moved_out))
        md, metas = self._compose(window, start, continued=start > 0)
        next_cursor = f"{spec}|{int(include_subsections)}|{end}" if end < len(pieces) else None
        pages = sorted({p for b in window for p in b.pages})
        refs = [b.ref for b in window if b.ref]
        prov = self._prov(roots[0], pages, refs)
        if len(roots) > 1:
            prov["section_ids"] = roots
        out: dict[str, Any] = {
            "request": {"section": spec, "include_subsections": include_subsections, "max_tokens": max_tokens},
            "mode": mode, "role": role_name, "section_id": roots[0], "matched_sections": roots,
            "provenance": prov,
            "sections": [self._sec_summary(s) for s in dict.fromkeys(b.section_id for b in window)],
            "markdown": md,
            "tokens": self.count_tokens(md),
            "total_tokens": total,
            "complete": start == 0 and end == len(pieces),
            "part": {"first_block": start, "end_block": end, "total_blocks": len(pieces)},
            "cursor": next_cursor,
        }
        if next_cursor:
            out["next"] = {"tool": "read_section", "arguments": {"cursor": next_cursor,
                                                                 **({"max_tokens": max_tokens} if max_tokens else {}),
                                                                 "with_blocks": with_blocks}}
        if excluded:
            out["excluded_subsections"] = excluded
        if with_blocks:
            out["blocks"] = metas
        out["warnings"] = warnings
        return out

    def _compose(self, window: list[_Blk], offset: int, continued: bool) -> tuple[str, list[dict]]:
        parts: list[str] = []
        metas: list[dict] = []
        pos = 0
        cur_page = None

        def add(text: str) -> tuple[int, int]:
            nonlocal pos
            if parts:
                pos += 2
            a = pos
            parts.append(text)
            pos += len(text)
            return a, pos

        if continued and window and window[0].kind != "heading":
            s = self.index.by_id[window[0].section_id]
            add(f"{'#' * max(1, min(6, int(s.level or 1)))} {s.title} (continued) {{#{s.section_id}}}")
        for i, b in enumerate(window):
            if b.pages and b.pages[0] != cur_page:
                add(_page_marker(b.pages))
                cur_page = b.pages[-1]
            elif b.pages:
                cur_page = b.pages[-1]
            a, z = add(b.text)
            meta = {"block": offset + i, "kind": b.kind, "section_id": b.section_id, "pages": b.pages, "ref": b.ref,
                    "chars": [a, z]}
            if b.kind == "list":
                meta["item_refs"] = b.leaf_refs
            for k, v in b.extra.items():
                meta[k] = v
            metas.append(meta)
        return "\n\n".join(parts), metas

    # ------------------------------------------------------------------ tables
    def _table_query(self, query: str) -> tuple[str, list[dict]]:
        from docling_core.types.doc import DocItemLabel

        q = query.strip()
        qt, qn = _title_tokens(q), _norm(q)
        cands = []
        for t in self.doc.tables:
            if t.label == DocItemLabel.DOCUMENT_INDEX:
                continue
            meta = self.tables.get(t.self_ref) or {}
            sid = self._section_of(t.self_ref)
            heads = meta.get("headings") or (self._breadcrumb(sid).split(" > ") if sid else [])
            grid = t.data.grid or []
            head_text = " ".join(dict.fromkeys(_clean(c.text) for row in grid[:2] for c in row if c.text))
            caption = t.caption_text(self.doc) or ""
            text = " ".join([caption, " ".join(heads[-2:]), head_text])
            cover, dice, sub = self._fuzzy(qt, qn, text)
            cap = self._fuzzy(qt, qn, caption) if caption else (0, 0, 0)
            score = max(0.7 * cover + 0.1 * dice + 0.2 * sub, 0.5 * cap[0] + 0.3 * cap[1] + 0.2 * cap[2])
            cands.append({"table_id": t.self_ref, "score": round(score, 3), "caption": caption or None,
                          "section_id": sid, "pages": _pages_of(t), "rows": t.data.num_rows, "cols": t.data.num_cols})
        cands.sort(key=lambda c: -c["score"])
        if not cands or cands[0]["score"] < 0.2:
            raise NotFoundError(f"no table matches {query!r}; closest: "
                                + (", ".join(f"{c['table_id']} ({c['caption'] or 'no caption'})" for c in cands[:5])
                                   or "none"))
        return cands[0]["table_id"], cands[:5]

    def get_table(self, table: str, format: str = "all") -> dict:
        """A whole table by id ("#/tables/3", "3") or by caption/heading words. A page-split chain comes as one
        table (all fragments). format: all (Markdown + grid + merged cells), markdown, grid, rows (one
        self-describing line per row: "Label: Column = value; ...", as the retriever indexes tables)."""
        from chunk_protocol import ProtocolTableSerializer, _cell_text, _count_header_rows, first_row_is_header

        if format not in ("all", "markdown", "grid", "rows"):
            raise ProtocolAPIError("format must be all, markdown, grid or rows")
        spec = str(table).strip()
        candidates = None
        m = re.match(r"^(?:#/tables/)?(\d+)$", spec)
        if m:
            tid = f"#/tables/{m.group(1)}"
            if int(m.group(1)) >= len(self.doc.tables):
                raise NotFoundError(f"no table {tid} (the document has {len(self.doc.tables)} tables)")
        else:
            tid, candidates = self._table_query(spec)
        chain = [r for r in ((self.tables.get(tid) or {}).get("chain") or [tid])]
        if tid not in chain:
            chain = [tid]
        fragments, texts, footnotes = [], [], []
        for r in chain:
            item = self._resolve(r)
            md = self._table_markdown(item)
            texts.append(md)
            grid = item.data.grid or []
            cells = [[_cell_text(c, self.doc, self.serializer) for c in row] for row in grid]
            has_flags = any(c.column_header for c in item.data.table_cells)
            n_head = _count_header_rows(item) if has_flags else int(first_row_is_header(cells)) if cells else 0
            notes = [_clean(fr.resolve(self.doc).text) for fr in item.footnotes]
            footnotes += notes
            frag: dict[str, Any] = {"table_id": r, "pages": _pages_of(item), "rows": item.data.num_rows,
                                    "cols": item.data.num_cols, "header_rows": n_head,
                                    "caption": item.caption_text(self.doc) or None, "footnotes": notes}
            if format in ("all", "grid"):
                frag["grid"] = cells
            if format == "all":
                frag["merged_cells"] = [
                    {"row": c.start_row_offset_idx, "col": c.start_col_offset_idx, "row_span": c.row_span,
                     "col_span": c.col_span, "text": _cell_text(c, self.doc, self.serializer),
                     "header": bool(c.column_header)}
                    for c in item.data.table_cells if (c.row_span or 1) > 1 or (c.col_span or 1) > 1]
            if format == "rows":
                cols, rows = ProtocolTableSerializer.columns_and_rows(item, self.doc, self.serializer)
                frag["columns"], frag["row_lines"] = cols, rows
            fragments.append(frag)
        if len(texts) > 1:
            texts = [f"{t}\n\n(Continued on the next page; the columns are laid out differently there.)"
                     for t in texts[:-1]] + texts[-1:]
        sid = self._section_of(chain[0])
        pages = sorted({p for f in fragments for p in f["pages"]})
        first = fragments[0]
        out: dict[str, Any] = {
            "table_id": chain[0], "table_ids": chain, "caption": first["caption"],
            "section": self._breadcrumb(sid) if sid else None,
            "role": self.index.by_id[sid].role if sid else None,
            "glossary": chain[0] in self._glossary_tables(),
            "provenance": self._prov(sid, pages, chain),
            "rows": sum(f["rows"] for f in fragments), "cols": max(f["cols"] for f in fragments),
            "footnotes": footnotes,
        }
        if format in ("all", "markdown"):
            out["markdown"] = "\n\n".join(texts)
        out["fragments"] = fragments
        if candidates:
            out["matched_by"] = "caption/heading query"
            out["candidates"] = candidates
        return out

    # ------------------------------------------------------------------ SoA
    def get_soa(self, visit: str | None = None, activity: str | None = None, with_markdown: bool = True,
                refresh: bool = False) -> dict:
        """The structured Schedule of Activities (soa.extract_soa: visits, activities, cells, footnotes), each
        with provenance and Markdown; visit/activity filter via soa.soa_lookup. Until soa.py exists the result
        says "available": false and lists the SoA sections and tables to read instead."""
        import importlib

        try:
            soa_mod = importlib.import_module("soa")
        except ImportError as exc:
            return self._soa_unavailable(f"soa.py (structured SoA extraction) is not available yet ({exc})")
        except Exception as exc:  # noqa: BLE001 - a broken soa.py must not break the other tools
            return self._soa_unavailable(f"soa.py failed to import: {type(exc).__name__}: {exc}")
        if not hasattr(soa_mod, "extract_soa"):
            return self._soa_unavailable("soa.py has no extract_soa(doc, index)")
        if self._soa is None or refresh:
            try:
                self._soa = list(soa_mod.extract_soa(self.doc, self.index) or [])
            except Exception as exc:  # noqa: BLE001
                return self._soa_unavailable(f"soa.extract_soa failed: {type(exc).__name__}: {exc}")
        soas = []
        for s in self._soa:
            d = dict(s)
            tids = s.get("table_ids") or ([s["table_id"]] if s.get("table_id") else [])
            d["provenance"] = self._prov(s.get("section_id") or (self._section_of(tids[0]) if tids else None),
                                         s.get("pages") or [], tids)
            if with_markdown and hasattr(soa_mod, "to_markdown"):
                try:
                    d["markdown"] = soa_mod.to_markdown(s)
                except Exception as exc:  # noqa: BLE001
                    d["markdown_error"] = f"{type(exc).__name__}: {exc}"
            soas.append(d)
        out: dict[str, Any] = {"available": True, "count": len(soas), "soas": soas,
                               "provenance": soas[0]["provenance"] if soas else self._soa_provenance()}
        if not soas:
            out["warnings"] = ["soa.extract_soa found no Schedule of Activities table"]
        if (visit or activity) and hasattr(soa_mod, "soa_lookup"):
            try:
                out["lookup"] = soa_mod.soa_lookup(self._soa, visit=visit, activity=activity)
            except Exception as exc:  # noqa: BLE001
                out["lookup_error"] = f"{type(exc).__name__}: {exc}"
        return out

    def _soa_provenance(self) -> dict:
        roots = self._role_roots("soa")
        tables = self._soa_tables()
        pages = {p for t in tables for p in t["pages"]} or set(self.index.by_id[roots[0]].pages if roots else [])
        return self._prov(roots[0] if roots else None, pages, [t["table_id"] for t in tables])

    def _soa_unavailable(self, reason: str) -> dict:
        return {"available": False, "reason": reason,
                "soa_sections": [self._sec_summary(s) for s in self._role_roots("soa")],
                "soa_tables": self._soa_tables(),
                "hint": "read_section('soa') returns the SoA section(s) with every table as Markdown and the "
                        "footnotes; get_table(table_id, format='rows') gives one line per activity row",
                "provenance": self._soa_provenance()}

    # ------------------------------------------------------------------ search
    def _search_builder(self):
        if self._builder is None:
            if not ((self.chunks_dir / "embeddings.npy").is_file() and (self.chunks_dir / "embeddings_meta.json").is_file()):
                raise SearchUnavailable(f"{self.chunks_dir} has no embeddings; run embed_chunks.py embed "
                                        "(read_section / get_table / quote_check work without them)")
            from retrieve import Retriever, production_builder

            r = Retriever(self.chunks_dir / "chunks.jsonl")
            if r.source_sha256 and self.source_sha256 and r.source_sha256 != self.source_sha256:
                raise RuntimeError(f"chunks.jsonl (sha {r.source_sha256}) and stats.json (sha {self.source_sha256}) "
                                   "describe different source documents")
            original = r.rerank

            def guarded(query: str, chunks: list[dict], model: str):
                if self._rerank_fn is not None:
                    return [(int(i), float(s)) for i, s in self._rerank_fn(query, chunks, model)]
                if _offline():
                    key = hashlib.sha256(json.dumps([model, query, [c["content_hash"] for c in chunks]]).encode()).hexdigest()
                    cf = Path(r.cache_dir) / "rerank_cache.json"
                    cache = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
                    if key not in cache:
                        raise OfflineCacheMiss(f"OPENROUTER_OFFLINE=1 and the rerank of {query[:80]!r} is not cached "
                                               f"in {cf}; unset OPENROUTER_OFFLINE to call OpenRouter (ZDR)")
                return original(query, chunks, model)

            r.rerank = guarded
            # the builder reuses this document's objects (same JSON, SHA-checked, and its stored identity record)
            # instead of loading them again
            self._builder = production_builder(r, self.doc_path, doc=self.doc, serializer=self.serializer,
                                               tok=self.tokenizer, index=self.index, tables=self.tables,
                                               identity_record=self._stored_identity)
        return self._builder

    def _query_vector(self, text: str, query: str):
        import numpy as np

        if self._embed_fn is not None:
            v = np.asarray(self._embed_fn(text), dtype=np.float32)
            return v / (np.linalg.norm(v) or 1.0)
        r = self._builder.retriever
        model = r.meta["model"]
        cf = self.chunks_dir / "query_embedding_cache.json"
        key = hashlib.sha256(f"{model}|{text}".encode()).hexdigest()
        if self._qcache is None or key not in self._qcache:
            self._qcache = json.loads(cf.read_text(encoding="utf-8")) if cf.exists() else {}
        if key in self._qcache:
            return np.asarray(self._qcache[key], dtype=np.float32)
        if _offline():
            raise OfflineCacheMiss(f"OPENROUTER_OFFLINE=1 and the query embedding of {query[:80]!r} is not cached in "
                                   f"{cf}; unset OPENROUTER_OFFLINE to embed it via OpenRouter (ZDR), or use "
                                   "read_section / find_sections / quote_check, which are local")
        from eval_retrieval import cached_query_vectors

        vec = cached_query_vectors([text], model, cf)[0]
        self._qcache = None
        return vec

    def search_context(self, query: str, k: int | None = None):
        """(retrieve.Result, retrieve.Context) of the production path: glossary expansion, query embedding (cached),
        dense + BM25 candidates, rerank (cached), parent-aware context (k=PRODUCTION_K, 12K tokens)."""
        from retrieve import PRODUCTION_K

        if not (query or "").strip():
            raise ProtocolAPIError("search needs a query")
        b = self._search_builder()
        r = b.retriever
        expanded, _ = r.expand(query)
        vec = self._query_vector(r.query_text(expanded), query)
        res = r.search(query, vec, k=k or PRODUCTION_K, mode="rerank")
        return res, b.build(res)

    def search(self, query: str, k: int | None = None, rendered: bool = False) -> dict:
        """Semantic search for scattered facts. Returns the production context: the identity block (title, sponsor
        id, registry ids, version; the Title line is the detected title, or the official title when the identity
        model step's verified choice replaced it) and the ranked blocks (whole sections/parents, tables, retrieved
        passages) with kind, text, section, pages, item refs; the glossary definitions of abbreviations used.
        rendered=True adds the context string exactly as an answering model sees it."""
        from retrieve import BLOCK_LABELS, PRODUCTION_K

        res, ctx = self.search_context(query, k)
        b = self._builder
        chunk_by_id = {c["chunk_id"]: c for c in b.retriever.chunks}
        expanded = iter(ctx.expanded)
        identity, blocks = None, []
        for n, blk in enumerate(ctx.blocks, start=1):
            refs = self._block_refs(blk, expanded, chunk_by_id)
            sids = list(dict.fromkeys(s for s in (self._section_of(r) for r in refs) if s))
            prov = self._prov(sids[0] if sids else None, blk.pages, refs)
            if len(sids) > 1:
                prov["section_ids"] = sids
            entry = {"n": n, "kind": blk.kind, "label": BLOCK_LABELS.get(blk.kind, blk.kind), "section": blk.section,
                     "pages": blk.pages, "text": blk.text, "tokens": blk.tokens, "note": blk.note,
                     "chunk_ids": blk.chunk_ids, "table_ids": blk.table_ids or ([blk.table_id] if blk.table_id else []),
                     "parent_id": blk.parent_id, "provenance": prov}
            if blk.kind == "document" and identity is None:
                identity = entry
            else:
                blocks.append(entry)
        hits = [{"rank": i, "chunk_id": h.chunk["chunk_id"], "score": round(float(h.score), 4),
                 "dense_rank": h.dense_rank, "bm25_rank": h.bm25_rank, "section_id": h.chunk.get("section_id"),
                 "pages": h.chunk.get("pages")} for i, h in enumerate(res.hits, start=1)]
        out: dict[str, Any] = {
            "query": query, "expanded_query": res.expanded_query, "k": k or PRODUCTION_K, "budget": b.budget,
            "identity": identity, "blocks": blocks, "hits": hits,
            "definitions": [{"abbreviation": a, "definition": d, "inferred_from_text": a in ctx.inferred}
                            for a, d in sorted(ctx.definitions.items())],
            "tokens": ctx.tokens, "expanded_sections": ctx.expanded, "dropped": ctx.dropped,
            "near_duplicates": ctx.duplicates, "degraded": ctx.fallbacks,
            "provenance": blocks[0]["provenance"] if blocks else (identity or {}).get("provenance", self._prov(None)),
        }
        if rendered:
            out["context"] = ctx.render()
        return out

    def _block_refs(self, blk: Any, expanded: Any, chunk_by_id: dict) -> list[str]:
        st = self._builder.store
        if blk.kind == "document":  # one ref per "(source ...)" line: the item the identifier was read from
            from retrieve import IDENTITY_FIELDS

            fields = self._identity_record()["fields"]
            sources = [(fields[key] or {}).get("source") for key, _ in IDENTITY_FIELDS]
            return [src["ref"] for src in sources if src and src["ref"]]
        if blk.kind == "section_full":
            sid = next(expanded, None)
            return list(st.section_block(sid)["refs"]) if sid else []
        if blk.parent_id and blk.kind in ("parent", "merged", "window", "table_whole") and blk.parent_id in st.by_id:
            return list(st.by_id[blk.parent_id].doc_item_refs)
        refs = [r for cid in blk.chunk_ids for r in (chunk_by_id.get(cid) or {}).get("doc_item_refs", [])]
        refs += blk.table_ids or ([blk.table_id] if blk.table_id else [])
        return list(dict.fromkeys(refs))

    # ------------------------------------------------------------------ abbreviations
    def abbreviation(self, term: str) -> dict:
        """Glossary lookup both ways: "AE" -> its definition(s); "adverse event" -> the abbreviation(s). Entries come
        from the abbreviations table/list or from inline "long form (SF)" definitions (source "inline")."""
        import difflib

        term = (term or "").strip()
        if not term:
            raise ProtocolAPIError("abbreviation needs a term")
        entries = self.glossary.entries if self.glossary else {}
        keys = [term] if term in entries else [k for k in entries if k.casefold() == term.casefold()]
        if not keys and len(term) > 2 and term.endswith("s") and term[:-1] in entries:
            keys = [term[:-1]]
        matches = [self._abbr_entry(k, "abbreviation", 1.0) for k in keys]
        nt = _norm(term)
        tt = set(_title_tokens(term))
        rev = []
        for k, e in entries.items():
            if k in keys:
                continue
            defs = [e.definition] + [a.get("definition", "") for a in e.alternatives or [] if isinstance(a, dict)]
            best = 0.0
            for d in defs:
                nd = _norm(d)
                if not nd:
                    continue
                if nd == nt:
                    s = 1.0
                elif len(nt) >= 4 and (nt in nd or nd in nt):
                    s = 0.85
                else:
                    dt = set(_title_tokens(d))
                    s = 0.8 * (2 * len(tt & dt) / (len(tt) + len(dt))) if tt and dt else 0.0
                best = max(best, s)
            if best >= 0.6:
                rev.append(self._abbr_entry(k, "definition", best))
        rev.sort(key=lambda m: -m["score"])
        matches += rev[:10]
        out: dict[str, Any] = {"term": term, "found": bool(matches), "matches": matches,
                               "provenance": matches[0]["provenance"] if matches else self._prov(None)}
        if not matches:
            out["did_you_mean"] = difflib.get_close_matches(term, list(entries), n=5, cutoff=0.7)
            out["hint"] = "not in the glossary; quote_check(term) shows whether and where it occurs in the text"
        return out

    def _abbr_entry(self, abbr: str, match: str, score: float) -> dict:
        e = self.glossary.entries[abbr]
        sid = self._section_of(e.ref) if e.ref else None
        return {"abbreviation": abbr, "definition": e.definition, "match": match, "score": round(score, 3),
                "source": e.source, "note": e.note, "alternatives": e.alternatives or [],
                "provenance": self._prov(sid, [e.page] if e.page else [], [e.ref] if e.ref else [])}

    # ------------------------------------------------------------------ quote_check
    def _segments(self) -> dict[str, list[_Seg]]:
        from docling_core.types.doc import (ContentLayer, ListItem, PictureItem, SectionHeaderItem, TableItem,
                                            TextItem)

        from chunk_protocol import figure_parts

        body: list[_Seg] = []
        ai: list[_Seg] = []
        captions = {r.cref for it in [*self.doc.tables, *self.doc.pictures] for r in [*it.captions, *it.footnotes]}
        pic_of = {ch.self_ref: p for p in self.doc.pictures  # figure text item -> its picture
                  for ch, _ in self.doc.iterate_items(root=p, with_groups=False, traverse_pictures=True)
                  if ch is not p and ch.self_ref not in captions}
        for item, _ in self.doc.iterate_items(with_groups=False, traverse_pictures=True,
                                              included_content_layers={ContentLayer.BODY}):
            ref = item.self_ref
            if isinstance(item, TableItem):
                pages = _pages_of(item)
                for r, row in enumerate(item.data.grid or []):
                    seen, cells = set(), []
                    for c in row:
                        if id(c) in seen:
                            continue
                        seen.add(id(c))
                        t = _clean(c.text)
                        if t:
                            cells.append(t)
                    if cells:
                        body.append(_Seg(" | ".join(cells), ref, pages, "body", "table_row", r))
            elif isinstance(item, PictureItem):
                f = figure_parts(item, self.doc)
                for d in f["description"]:
                    ai.append(_Seg(d, ref, _pages_of(item), "ai_generated", "figure_description"))
                for m in f["mermaid"]:
                    ai.append(_Seg(m, ref, _pages_of(item), "ai_generated", "figure_mermaid"))
            elif isinstance(item, TextItem) and item.text.strip():
                text = item.text
                if isinstance(item, ListItem) and item.marker:
                    text = f"{item.marker} {text}"
                kind = "figure_text" if ref in pic_of else (
                    "section_header" if isinstance(item, SectionHeaderItem) else item.label.value)
                # flattened figure OCR text items have no provenance (prov []): the picture's pages
                pages = _pages_of(item) or (_pages_of(pic_of[ref]) if kind == "figure_text" else [])
                body.append(_Seg(text, ref, pages, "body", kind))
        furniture = [_Seg(t.text, t.self_ref, _pages_of(t), "furniture", t.label.value)
                     for t in sorted((t for t in self.doc.texts if t.content_layer == ContentLayer.FURNITURE
                                      and t.text.strip()), key=lambda t: (_pages_of(t) or [10 ** 6])[0])]
        return {"body": body, "furniture": furniture, "ai_generated": ai}

    def _text_streams(self) -> dict[str, list[_Stream]]:
        if self._streams is None:
            segs = self._segments()
            self._streams = {tier: [_Stream(segs[layer], fn, sep) for layer in _LAYERS]
                             for tier, fn, sep in (("exact", _exact_form, " "), ("normalized", _norm_form, " "),
                                                   ("compact", _compact_form, ""))}
        return self._streams

    def _matches(self, quote: str) -> tuple[str | None, list[tuple[str, _Stream, int, int]]]:
        """(match tier, every match as (layer, stream, start, end): body, then furniture, then ai_generated) of a
        quote at the first tier with a match in the body or furniture, else the first with one in AI-generated text:
        exact (whitespace only), normalized (case, dashes, quotes, superscript carets, Unicode), compact (letters/digits
        only; quotes of >= 12 such characters)."""
        streams = self._text_streams()
        forms = _quote_forms(quote)
        ai_only: tuple[str | None, list] = (None, [])
        for tier in ("exact", "normalized", "compact"):
            needle = forms[tier]
            if tier == "compact" and len(needle) < 12:  # too short to ignore punctuation and spacing safely
                break
            if not needle:
                continue
            found = [(layer, st, a, b) for layer, st in zip(_LAYERS, streams[tier]) for a, b in st.find_all(needle)]
            if any(m[0] != "ai_generated" for m in found):
                return tier, found
            if found and ai_only[0] is None:
                ai_only = (tier, found)
        return ai_only

    def _elided_matches(self, parts: list[str]) -> tuple[str | None, list[tuple[str, _Stream, int, int]]]:
        """(match tier, every sequence as (layer, stream, start of the first part, end of the last)) of an elided
        quote's parts: in one layer stream, at one tier, in order and close together (_sequences). The first tier
        with a sequence in the body or furniture wins, else the first with one in AI-generated text; compact only
        when every part has >= 12 letters/digits."""
        streams = self._text_streams()
        forms = [_quote_forms(p) for p in parts]
        ai_only: tuple[str | None, list] = (None, [])
        for tier in ("exact", "normalized", "compact"):
            needles = [f[tier] for f in forms]
            if tier == "compact" and min(map(len, needles)) < 12:
                break
            if not all(needles):
                continue
            found = [(layer, st, a, b) for layer, st in zip(_LAYERS, streams[tier]) for a, b in _sequences(st, needles)]
            if any(m[0] != "ai_generated" for m in found):
                return tier, found
            if found and ai_only[0] is None:
                ai_only = (tier, found)
        return ai_only

    def _find(self, quote: str, max_locations: int = MAX_LOCATIONS) -> tuple[str | None, list[dict], int]:
        """(match tier, the first max_locations locations, total count) of a quote (_matches)."""
        tier, found = self._matches(quote)
        return tier, [self._location(st, a, b, tier) for _, st, a, b in found[:max_locations]], len(found)

    def _location(self, st: _Stream, a: int, b: int, tier: str) -> dict:
        idx = st.span_segments(a, b)
        segs = [st.segs[i] for i in idx]
        refs = list(dict.fromkeys(s.ref for s in segs))
        sids = list(dict.fromkeys(x for x in (self._section_of(r) for r in refs) if x))
        pages = sorted({p for s in segs for p in s.pages})
        # verbatim context: the matched segments' own text, trimmed around the (approximate) match position
        text = " ".join(_clean(s.text) for s in segs)
        if len(text) > 2 * QUOTE_CONTEXT + (b - a):
            first_len = max(1, st.lens[idx[0]])
            ratio = len(_clean(segs[0].text)) / first_len
            rel = max(0, a - st.starts[idx[0]])
            lo = max(0, int(rel * ratio) - QUOTE_CONTEXT)
            hi = min(len(text), int((rel + (b - a)) * ratio) + QUOTE_CONTEXT)
            text = ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")
        prov = self._prov(sids[0] if sids else None, pages, refs)
        if len(sids) > 1:
            prov["section_ids"] = sids
        loc = {"match": tier, "layer": segs[0].layer, "kind": segs[0].kind, "text": text, "provenance": prov}
        rows = [s.row for s in segs if s.row is not None]
        if rows:
            loc["table_rows"] = rows
        return loc

    def _closest(self, quote: str, allowed: set[str] | None) -> dict | None:
        """The body window with the highest share of the quote's words (bag of words), when >= 0.6."""
        if self._words is None:
            segs = self._segments()["body"]
            words, owner = [], []
            for i, s in enumerate(segs):
                for w in _WORD.findall(_norm_form(s.text)):
                    words.append(w)
                    owner.append(i)
            self._words = (words, owner, segs)
        words, owner, segs = self._words
        q = Counter(_WORD.findall(_norm_form(quote)))
        n = sum(q.values())
        if n < 3 or len(words) < n:
            return None
        win: Counter = Counter()
        overlap, best, best_end = 0, -1, 0
        for i, w in enumerate(words):
            if w in q and win[w] < q[w]:
                overlap += 1
            win[w] += 1
            if i >= n:
                y = words[i - n]
                win[y] -= 1
                if y in q and win[y] < q[y]:
                    overlap -= 1
            if i >= n - 1 and overlap > best:
                if allowed is None or self._section_of(segs[owner[i]].ref) in allowed:
                    best, best_end = overlap, i
        if best / n < 0.6:
            return None
        idx = sorted(set(owner[best_end - n + 1: best_end + 1]))
        chosen = [segs[i] for i in idx]
        refs = list(dict.fromkeys(s.ref for s in chosen))
        sids = list(dict.fromkeys(x for x in (self._section_of(r) for r in refs) if x))
        text = " ".join(_clean(s.text) for s in chosen)
        if len(text) > 900:
            text = text[:900] + "…"
        return {"similarity": round(best / n, 3), "text": text,
                "provenance": self._prov(sids[0] if sids else None, {p for s in chosen for p in s.pages}, refs)}

    def quote_check(self, quote: str, section_id: str | None = None, max_locations: int = MAX_LOCATIONS) -> dict:
        """Does this text occur in the document (verbatim up to whitespace / case / dashes / quotes / superscript
        carets; "compact" ignores all punctuation and spacing)? Every match is counted (count; count_by_layer: body /
        page headers-footers); at most max_locations are listed (section, pages, item refs, verbatim context), body
        first; up to 2 listed places are kept for header/footer matches when both layers match (max_locations >= 4).
        With section_id only matches inside that section (and its subsections) count; others are listed as
        found_elsewhere. Text found only in an AI-generated figure description is NOT document text
        (found_in_ai_text); a match there never hides a normalized or compact match in the document text. "..." / "…"
        in the quote elides text: the parts must occur in this order, each within ELIDED_GAP (2,000) characters after
        the end of the previous one; each tight, non-overlapping sequence counts once ("parts" has each part's own
        result).
        When nothing is found, "closest" shows the most similar passage (share of the quote's words)."""
        quote = quote or ""
        if len(_compact_form(quote)) < 2:
            raise ProtocolAPIError("quote_check needs a quote of at least 2 letters or digits")
        allowed = None
        if section_id:
            if section_id not in self.index.by_id:
                raise NotFoundError(f"no section {section_id!r}")
            allowed = set(self._subtree(section_id))
        parts = [p for p in _ELLIPSIS.split(quote) if len(_compact_form(p)) >= 2]
        if len(parts) < 2:
            tier, found = self._matches(quote)
            return self._quote_result(quote, section_id, allowed, tier, found, max_locations, quote)
        results = [self.quote_check(p, section_id, max_locations=3) for p in parts]
        tier, found = self._elided_matches(parts)
        out = self._quote_result(quote, section_id, allowed, tier, found, max_locations, " ".join(parts))
        prov = out.pop("provenance")
        out["match"] = "elided" if out["found"] else None
        out["parts"] = [{"part": p, "found": r["found"], "match": r["match"], "locations": r["locations"]}
                        for p, r in zip(parts, results)]
        out["provenance"] = prov if out["locations"] else next(
            (r["provenance"] for r in results if r["found"]), self._prov(section_id))
        return out

    def _quote_result(self, quote: str, section_id: str | None, allowed: set[str] | None, tier: str | None,
                      found: list[tuple[str, _Stream, int, int]], max_locations: int, closest_of: str) -> dict:
        """quote_check's answer from its matches (_matches / _elided_matches). Every match is counted; a location
        (context text, provenance) is built only for the listed ones."""
        inside, outside, ai = [], [], []
        for m in found:
            layer, st, a, b = m
            if layer == "ai_generated":
                ai.append(m)
            elif allowed is None or not allowed.isdisjoint(self._section_of(st.segs[i].ref)
                                                           for i in st.span_segments(a, b)):
                inside.append(m)
            else:
                outside.append(m)
        listed = [self._location(st, a, b, tier) for _, st, a, b in _listed(inside, max_locations)]
        out: dict[str, Any] = {"quote": quote, "section_id": section_id, "found": bool(inside),
                               "match": tier if inside else None, "count": len(inside),
                               "count_by_layer": {k: sum(m[0] == k for m in inside) for k in ("body", "furniture")},
                               "locations": listed}
        if len(inside) > len(listed):
            out["more_locations"] = len(inside) - len(listed)
        if outside:
            out["found_elsewhere"] = [self._location(st, a, b, tier) for _, st, a, b in _listed(outside, max_locations)]
        if ai:
            out["found_in_ai_text"] = [self._location(st, a, b, tier) for _, st, a, b in ai[:3]]
            out["warning"] = ("matched text in an AI-generated figure description/diagram, which is not document "
                              "text; cite the figure's OCR text or the body instead")
        if not inside:
            closest = self._closest(closest_of, allowed)
            if closest:
                out["closest"] = closest
        out["provenance"] = listed[0]["provenance"] if listed else self._prov(section_id)
        return out

    # ================================================================== tool layer
    def tool_schemas(self, style: str = "anthropic") -> list[dict]:
        return tool_schemas(style)

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        """Run one tool by name with JSON arguments; errors come back as {"error", "error_type", ...} (never raised),
        so an agent loop can pass the result straight back to the model. error_type: UnknownTool, InvalidArguments
        (the arguments against the tool's schema) or the type of the exception the tool raised."""
        args = dict(args or {})
        spec = next((t for t in TOOLS if t["name"] == name), None)
        if spec is None:
            return {"error": f"unknown tool {name!r}", "error_type": "UnknownTool", "tools": [t["name"] for t in TOOLS]}
        try:
            import jsonschema

            jsonschema.validate(args, spec["input_schema"])
        except ImportError:
            pass
        except Exception as exc:  # noqa: BLE001 - jsonschema.ValidationError
            return {"error": f"invalid arguments for {name}: {getattr(exc, 'message', exc)}",
                    "error_type": "InvalidArguments", "tool": name}
        try:
            if name == "read_section":
                if not args.get("section") and not args.get("cursor"):
                    raise ProtocolAPIError("read_section needs 'section' (id, number or role) or 'cursor'")
                args.setdefault("max_tokens", DEFAULT_TOOL_MAX_TOKENS)
            result = getattr(self, spec["method"])(**args)
        except Exception as exc:  # noqa: BLE001 - the agent gets the message, the loop keeps running
            if not isinstance(exc, (ProtocolAPIError, LookupError, ValueError, RuntimeError)):
                log.exception("tool %s failed", name)
            return {"error": str(exc), "error_type": type(exc).__name__, "tool": name}
        return _jsonable(result)


# =========================================================================== tool definitions

_ROLE_LIST = ", ".join(ROLES)
TOOLS: list[dict] = [
    {"name": "document_info", "method": "document_info",
     "description": "Identity of the protocol (StudyTitle, StudyIdentifier, StudyVersion, sponsor): official/brief "
                    "title, acronym, sponsor protocol number, registry ids (NCT, EudraCT, EU CT, IND, WHO UTN) each "
                    "with the verbatim line it was found in, version/amendment and date, sponsor name, cover-page "
                    "label/value pairs, page/section/SoA-table counts and the outermost section per ICH M11 role. "
                    f"A value with method '{MODEL_METHOD}' was chosen by a model and found in the lines it cited "
                    "(source); its 'rules' is the rule value it replaced (value and method, both null when the "
                    "rules found none). version.label and version.amendment are plain values; version.label_detail "
                    "and version.amendment_detail give the same fields with method, source and 'rules'. "
                    "Call it first.",
     "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "outline", "method": "outline",
     "description": "The section tree in document order: section id, number, title, level, ICH M11 role, first/last "
                    "page, token size of the whole subsection tree and of the section's own text, children ids, "
                    "and counts of tables/figures. Use it to plan which sections to read.",
     "input_schema": {"type": "object", "properties": {
         "max_level": {"type": "integer", "minimum": 0, "description": "Only sections up to this depth (1 = top level)."},
         "with_roles": {"type": "boolean", "default": True}},
         "additionalProperties": False}},
    {"name": "find_sections", "method": "find_sections",
     "description": "Find sections by ICH M11 role and/or a fuzzy title query (also a section number like '5.1'). "
                    f"Roles: {_ROLE_LIST}. Returns ranked sections with id, title, role, path and size.",
     "input_schema": {"type": "object", "properties": {
         "role": {"type": "string", "enum": list(ROLES)},
         "query": {"type": "string", "description": "Title words, e.g. 'exclusion criteria', or a number '6.2'."},
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
         "additionalProperties": False}},
    {"name": "read_section", "method": "read_section",
     "description": "Complete Markdown of a section and its subsections: every paragraph, list item (own markers "
                    "and nesting), table (as Markdown with notes) and figure (OCR text; AI-generated descriptions "
                    "are labelled). 'section' is a section id ('S5.1'), number ('5.1') or role ('inclusion', "
                    "'exclusion', 'objectives_endpoints', 'soa', 'interventions', 'amendment_history', ...). "
                    "Headings carry '{#section_id}', '<!-- p. N -->' marks pages. If 'cursor' is returned the "
                    "content continues: call again with that cursor. Use this for enumerations (all criteria, all "
                    "objectives): it is complete, unlike search.",
     "input_schema": {"type": "object", "properties": {
         "section": {"type": "string", "description": "Section id, number or role name."},
         "include_subsections": {"type": "boolean", "default": True},
         "max_tokens": {"type": "integer", "minimum": 200, "default": DEFAULT_TOOL_MAX_TOKENS,
                        "description": "Size of one part; longer content continues via cursor."},
         "cursor": {"type": "string", "description": "Continuation cursor from a previous read_section result."},
         "with_blocks": {"type": "boolean", "default": True,
                         "description": "Include per-block provenance (kind, section, pages, item refs)."}},
         "additionalProperties": False}},
    {"name": "get_table", "method": "get_table",
     "description": "One whole table by id ('#/tables/3') or by caption/heading words ('study treatments'): Markdown, "
                    "cell grid, merged cells, footnotes, page-split fragments joined. format='rows' gives one "
                    "'Row label: Column = value; ...' line per row (good for wide SoA tables).",
     "input_schema": {"type": "object", "properties": {
         "table": {"type": "string"},
         "format": {"type": "string", "enum": ["all", "markdown", "grid", "rows"], "default": "all"}},
         "required": ["table"], "additionalProperties": False}},
    {"name": "get_soa", "method": "get_soa",
     "description": "The structured Schedule of Activities: visits (label, window, timing), activities, the "
                    "activity x visit cells with marks and footnote markers, footnote texts, with provenance and a "
                    "Markdown rendering. A visit's 'label' may be composed ('Hour 0' from the stub 'Hours' and the "
                    "cell '0'): quote_check its header_text (header_cells holds the header cells as printed) and, "
                    "for a composed label, the unit_from text, without section_id, not the label; a header cell "
                    "of fewer than 2 letters or digits cannot be checked alone: its table_id, row and column "
                    "locate it in get_table's grid. Optional visit/activity filter. If 'available' is false, read "
                    "the SoA with read_section('soa') and get_table(...).",
     "input_schema": {"type": "object", "properties": {
         "visit": {"type": "string"}, "activity": {"type": "string"},
         "with_markdown": {"type": "boolean", "default": True}},
         "additionalProperties": False}},
    {"name": "search", "method": "search",
     "description": "Semantic search for scattered facts (a dose, a window, a definition stated somewhere in the "
                    "body). Returns the identity block (its Title line is the detected title, or the official title "
                    "when the identity model step's verified choice replaced it) and ranked context blocks (whole "
                    "sections or parents, tables, passages) with section, pages and item refs. Not for complete "
                    "lists: use read_section.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "A natural-language question."},
         "k": {"type": "integer", "minimum": 1, "maximum": 20, "description": "Retrieved passages (default 8)."},
         "rendered": {"type": "boolean", "default": False,
                      "description": "Also return 'context': the context string exactly as an answering model sees it."}},
         "required": ["query"], "additionalProperties": False}},
    {"name": "abbreviation", "method": "abbreviation",
     "description": "Glossary lookup in both directions: an abbreviation ('AE') -> definition(s), or a long form "
                    "('adverse event') -> abbreviation(s), with the page/section where it is defined.",
     "input_schema": {"type": "object", "properties": {"term": {"type": "string"}},
                      "required": ["term"], "additionalProperties": False}},
    {"name": "quote_check", "method": "quote_check",
     "description": "Verify an extracted value: does this verbatim text occur in the document? Returns found, match "
                    "type (exact / normalized / compact), count of every match (count_by_layer: body / page "
                    f"headers-footers) and up to {MAX_LOCATIONS} locations (section, pages, item refs, verbatim "
                    "context; up to 2 listed places are kept for header/footer matches when both layers match); "
                    "with section_id only that section counts. '...' or '…' elides text: the parts must occur in "
                    f"this order, each within {ELIDED_GAP:,} characters of the previous one. Call it for every quote "
                    "you attach to a USDM value; if not found, fix the quote using 'closest'.",
     "input_schema": {"type": "object", "properties": {
         "quote": {"type": "string"},
         "section_id": {"type": "string", "description": "Restrict to this section and its subsections."}},
         "required": ["quote"], "additionalProperties": False}},
]


def tool_schemas(style: str = "anthropic") -> list[dict]:
    """Tool definitions: style "anthropic" ({name, description, input_schema}) or "openai" ({type: function,
    function: {name, description, parameters}})."""
    out = []
    for t in TOOLS:
        if style == "anthropic":
            out.append({"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]})
        elif style == "openai":
            out.append({"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                         "parameters": t["input_schema"]}})
        else:
            raise ValueError("style must be 'anthropic' or 'openai'")
    return json.loads(json.dumps(out))


# =========================================================================== CLI

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Protocol document access layer (tools for a USDM agent).")
    p.add_argument("out_dir", help="converted protocol folder (Docling JSON + chunks/)")
    p.add_argument("command", choices=["info", "outline", "find", "section", "table", "soa", "search", "quote",
                                       "abbr", "schemas"])
    p.add_argument("arg", nargs="?", help="section id/number/role, table id/query, query, quote, term")
    p.add_argument("--max-tokens", type=int, help="section: part size (continuation cursor beyond it)")
    p.add_argument("--cursor", help="section: continue from this cursor")
    p.add_argument("--max-level", type=int, help="outline: depth limit")
    p.add_argument("--role", help="find: ICH M11 role")
    p.add_argument("--section", help="quote: restrict to this section id")
    p.add_argument("--format", default="all", help="table: all|markdown|grid|rows")
    p.add_argument("-k", type=int, help="search: retrieved passages")
    p.add_argument("--style", default="anthropic", help="schemas: anthropic|openai")
    p.add_argument("--md", action="store_true", help="section/table/soa/search: print only the Markdown/context")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.command == "schemas":
        print(json.dumps(tool_schemas(a.style), indent=1, ensure_ascii=False))
        return 0
    doc = ProtocolDocument.open(a.out_dir)
    need = {"section", "table", "search", "quote", "abbr"}
    if a.command in need and not (a.arg or (a.command == "section" and a.cursor)):
        p.error(f"{a.command} needs an argument")
    if a.command == "info":
        res = doc.document_info()
    elif a.command == "outline":
        res = doc.outline(max_level=a.max_level)
    elif a.command == "find":
        res = doc.find_sections(role=a.role, query=a.arg)
    elif a.command == "section":
        res = doc.read_section(a.arg, max_tokens=a.max_tokens, cursor=a.cursor)
    elif a.command == "table":
        res = doc.get_table(a.arg, format=a.format)
    elif a.command == "soa":
        res = doc.get_soa()
    elif a.command == "search":
        res = doc.search(a.arg, k=a.k, rendered=a.md)
    elif a.command == "quote":
        res = doc.quote_check(a.arg, section_id=a.section)
    else:
        res = doc.abbreviation(a.arg)
    if a.md:
        text = (res.get("markdown") or res.get("context")
                or "\n\n".join(s.get("markdown", "") for s in res.get("soas", [])))
        if text:
            print(text)
            return 0
    print(json.dumps(_jsonable(res), indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
