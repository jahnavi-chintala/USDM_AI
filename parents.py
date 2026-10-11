"""Parent-child retrieval: a non-overlapping structural partition of a protocol into parents.

Children are the existing chunks (chunk_protocol.py, unchanged). Parents are built by a separate walk
over the same DoclingDocument and its section tree (reports/audit/structure_chunking.md §3, §7):

  * a section whose whole subtree is <= MAX tokens is one parent ("section");
  * otherwise its own text (the items before its first subsection) is packed into balanced windows
    <= MAX at item boundaries ("window", the heading path carried), and its subsections recurse;
  * adjacent siblings below MIN are merged while the result stays <= MAX ("merged");
  * a single unit above MAX (a big table) is its own parent ("table").

Items are grouped into chunk-aligned atoms first (consecutive items that share a chunk are never
split), so every chunk maps to exactly one parent. Parent text is Markdown: headings as "#" lines,
list items with their own markers, tables in Markdown exactly as tables.jsonl writes them, figures as
labelled blocks (as retrieve.ContextBuilder shows them).

Section tree: structure.build_section_index(doc) when structure.py is importable (ICH M11 roles);
until then FallbackSectionIndex builds the same interface from the heading items, with soft role hints
from heading names only.

Also here: the eval_v1 environment factory (`eval_v1.py run ... --env-factory parents:env_factory`),
which plugs the parent-aware ContextBuilder mode (retrieve.py) and parent units into eval_v1's DocEnv.
Everything is local; nothing is sent anywhere.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("parents")

MIN_TOKENS = 300
MAX_TOKENS = 1500
TABLE_WHOLE_MAX = 2000  # a table parent this small is always shown whole (context assembly)
FRONT_ID = "s-front"
JOIN_TOKENS = 2  # estimated tokens per block separator in a rendered parent


# =========================================================================== section index (fallback)

@dataclass
class Section:
    """One node of the section tree (the structure.py contract)."""
    section_id: str
    number: str | None
    title: str
    level: int
    parent_id: str | None
    heading_ref: str | None
    item_refs: list[str] = field(default_factory=list)  # own items in reading order, excluding subsections
    pages: list[int] = field(default_factory=list)
    role: str | None = None
    role_score: float = 0.0
    minor_headings: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# Soft role hints from heading names (ICH M11-aligned role names). A hint, never a rule: structure.py
# replaces this with a scored classifier. Sections without a hint inherit their parent's role.
ROLE_HINTS = [
    ("abbreviations", r"\babbreviations?\b|\bglossary\b|\bacronyms?\b|\bdefinitions? of terms\b"),
    ("amendment_history", r"\bamendments?\b|\bsummary of changes\b|\b(document|revision) history\b"),
    ("synopsis", r"\bsynopsis\b"),
    ("schema", r"\bschema\b|\bstudy (design )?diagram\b"),
    ("soa", r"\bschedules? of (activities|assessments|events|procedures)\b|\btime and events\b|\bflow ?chart\b"),
    ("objectives_endpoints", r"\bobjectives?\b|\bendpoints?\b"),
    ("estimands", r"\bestimands?\b"),
    ("inclusion", r"\binclusion criteria\b"),
    ("exclusion", r"\bexclusion criteria\b"),
    ("population", r"\bstudy population\b|\beligibility\b|\bselection of (study )?(participants|patients|subjects)\b"),
    ("interventions", r"\bstudy (intervention|treatment|drug)s?\b|\binvestigational (medicinal )?product\b|\btreatments? administered\b"),
    ("statistics", r"\bstatistic"),
    ("design", r"\b(study|overall|trial) design\b"),
    ("introduction", r"^introduction\b|\bbackground\b|\brationale\b"),
    ("appendix_other", r"^appendi(x|ces)\b|\bsupporting documentation\b"),
]
_ROLE_RX = [(role, re.compile(rx, re.I)) for role, rx in ROLE_HINTS]
_NUM_PREFIX = re.compile(r"^\s*(?:(?:section|appendix)\s+)?[\dIVXLC]+(?:\.\d+)*\.?\s*[:.\-–]?\s*", re.I)


def role_hint(title: str) -> str | None:
    t = _NUM_PREFIX.sub("", title or "").strip()
    for role, rx in _ROLE_RX:
        if rx.search(t):
            return role
    return None


class FallbackSectionIndex:
    """structure.SectionIndex's interface, built from the heading items alone (no role classifier)."""

    source = "fallback"

    def __init__(self, sections: list[Section], ref_map: dict[str, str] | None = None):
        self.sections = list(sections)
        self.by_id = {s.section_id: s for s in self.sections}
        self._kids: dict[str | None, list[str]] = defaultdict(list)
        for s in self.sections:
            self._kids[s.parent_id if s.parent_id in self.by_id else None].append(s.section_id)
        self._of = dict(ref_map or {})
        if not self._of:
            for s in self.sections:
                if s.heading_ref:
                    self._of[s.heading_ref] = s.section_id
                for r in s.item_refs:
                    self._of.setdefault(r, s.section_id)

    def section_of(self, ref: str) -> Section | None:
        sid = self._of.get(ref)
        return self.by_id.get(sid) if sid else None

    def children(self, section_id: str | None) -> list[Section]:
        return [self.by_id[i] for i in self._kids.get(section_id, [])]

    def path(self, section_id: str) -> list[Section]:
        out, s = [], self.by_id.get(section_id)
        while s is not None:
            out.append(s)
            s = self.by_id.get(s.parent_id) if s.parent_id else None
        return out[::-1]

    def to_json(self) -> dict:
        return {"source": self.source, "sections": [s.to_json() for s in self.sections], "ref_map": self._of}

    @classmethod
    def from_json(cls, data: dict) -> "FallbackSectionIndex":
        return cls([Section(**s) for s in data["sections"]], data.get("ref_map"))


def owned_refs(doc: Any) -> dict[str, str]:
    """Captions/footnotes of tables and figures, and figure children -> the owning item's ref."""
    owned: dict[str, str] = {}
    for it in [*doc.tables, *doc.pictures]:
        for r in [*it.captions, *it.footnotes]:
            owned[r.cref] = it.self_ref
    for pic in doc.pictures:
        for child, _ in doc.iterate_items(root=pic, with_groups=True, traverse_pictures=True):
            if child is not pic:
                owned.setdefault(child.self_ref, pic.self_ref)
    return owned


def build_fallback_index(doc: Any) -> FallbackSectionIndex:
    """Sections from the (level-fixed) heading items; body items before the first heading form a
    "Front matter" section (as chunk_protocol labels those chunks)."""
    from docling_core.types.doc import ContentLayer, SectionHeaderItem, TitleItem

    from chunk_protocol import FRONT_MATTER, accepted_section_numbers

    accepted = accepted_section_numbers(doc)
    owned = owned_refs(doc)
    sections: list[Section] = []
    stack: list[Section] = []
    ref_map: dict[str, str] = {}
    used: set[str] = set()
    front: Section | None = None

    def pages_of(item) -> list[int]:
        return sorted({p.page_no for p in getattr(item, "prov", None) or []})

    for item, _ in doc.iterate_items(with_groups=False, traverse_pictures=False,
                                     included_content_layers={ContentLayer.BODY}):
        ref = item.self_ref
        if ref in owned:
            continue
        if isinstance(item, SectionHeaderItem | TitleItem):
            level = item.level if isinstance(item, SectionHeaderItem) else 0
            while stack and stack[-1].level >= level:
                stack.pop()
            number = accepted.get(ref)
            sid = f"s-{number}" if number else f"s-u{len(sections):03d}"
            if sid in used:
                sid = f"{sid}~{len(sections):03d}"
            used.add(sid)
            sec = Section(sid, number, " ".join(item.text.split()), level,
                          stack[-1].section_id if stack else None, ref, [], pages_of(item))
            sections.append(sec)
            stack.append(sec)
            ref_map[ref] = sid
            continue
        if stack:
            target = stack[-1]
        else:
            if front is None:
                front = Section(FRONT_ID, None, FRONT_MATTER, 1, None, None, role="title_page", role_score=0.5)
                sections.append(front)
            target = front
        target.item_refs.append(ref)
        target.pages = sorted(set(target.pages) | set(pages_of(item)))
        ref_map[ref] = target.section_id
    for ref, owner in owned.items():
        if owner in ref_map:
            ref_map[ref] = ref_map[owner]
    by_id = {s.section_id: s for s in sections}
    for s in sections:  # reading order = pre-order: a parent's role is known before its children's
        if s.role:
            continue
        hint = role_hint(s.title)
        parent = by_id.get(s.parent_id) if s.parent_id else None
        s.role, s.role_score = (hint, 0.5) if hint else ((parent.role, parent.role_score * 0.8) if parent and parent.role
                                                          else (None, 0.0))
    return FallbackSectionIndex(sections, ref_map)


def section_index(doc: Any) -> Any:
    """structure.build_section_index(doc) when structure.py is importable, else the fallback index."""
    try:
        import structure  # noqa: F401 - written by the structure step; optional until it lands
    except ImportError:
        return build_fallback_index(doc)
    try:
        return structure.build_section_index(doc)
    except Exception as exc:  # noqa: BLE001 - never lose the parents over a structure bug; say so loudly
        log.warning("structure.build_section_index failed (%s); using the fallback section index",
                    type(exc).__name__)
        return build_fallback_index(doc)


# ----------------------------------------------------------------- index adapters (Section objects or ids)

def _sid(x: Any) -> str | None:
    if x is None:
        return None
    return x if isinstance(x, str) else getattr(x, "section_id", None)


def _ids(xs: Any) -> list[str]:
    return [i for i in (_sid(x) for x in (xs or [])) if i]


# =========================================================================== tokens

class TokenCounter:
    """count(text) / many(texts) over a docling BaseTokenizer, an HF tokenizer or a plain function."""

    def __init__(self, tokenizer: Any):
        self._batch = None
        if hasattr(tokenizer, "count_tokens"):  # docling BaseTokenizer: count_tokens = len(tokenize(text))
            self._fn = tokenizer.count_tokens
            get = getattr(tokenizer, "get_tokenizer", None)
            inner = get() if callable(get) else None
            if callable(inner) and hasattr(inner, "convert_tokens_to_ids"):
                self._batch = lambda ts: [len(x) for x in inner(ts, add_special_tokens=False)["input_ids"]]
        elif callable(tokenizer) and hasattr(tokenizer, "convert_tokens_to_ids"):  # HF tokenizer
            # no special tokens, exactly as docling's count_tokens: chunk_protocol (docling tokenizer) and
            # ContextBuilder (HF tokenizer) must partition identically
            self._fn = lambda s: len(tokenizer(s, add_special_tokens=False)["input_ids"])
            self._batch = lambda ts: [len(x) for x in tokenizer(ts, add_special_tokens=False)["input_ids"]]
        elif callable(tokenizer):
            self._fn = tokenizer
        else:
            raise TypeError("tokenizer must be a docling tokenizer, an HF tokenizer or a function")

    def count(self, text: str) -> int:
        return int(self._fn(text)) if text else 0

    def many(self, texts: list[str]) -> list[int]:
        texts = list(texts)
        if not texts:
            return []
        if self._batch is not None:
            out = [0] * len(texts)
            idx = [i for i, t in enumerate(texts) if t]
            for i, n in zip(idx, self._batch([texts[i] for i in idx]) if idx else []):
                out[i] = n
            return out
        return [self.count(t) for t in texts]


# =========================================================================== parents

@dataclass
class Parent:
    parent_id: str
    kind: str  # section | merged | window | table
    section_ids: list[str]
    breadcrumb: str
    role: str | None
    roles: list[str]
    pages: list[int]
    tokens: int
    doc_item_refs: list[str]
    text: str
    chunk_ids: list[str] = field(default_factory=list)
    table_ids: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class _Unit:
    ref: str
    kind: str  # text | list | table | figure | label
    text: str
    section: str
    pages: tuple
    tokens: int = 0


@dataclass
class _Atom:
    units: list[int]
    section: str
    tokens: int = 0
    tables: list[str] = field(default_factory=list)
    sections: frozenset = frozenset()


@dataclass
class _P:
    """A parent during partitioning: segments of (section id, atom ids, starts the section)."""
    kind: str
    segments: list[tuple[str, list[int], bool]]
    est: int


class ParentStore:
    """The partition of one document into parents, plus section-subtree rendering for context assembly."""

    def __init__(self, doc: Any, index: Any, chunks: list[dict], tokenizer: Any,
                 min_tokens: int = MIN_TOKENS, max_tokens: int = MAX_TOKENS, serializer: Any = None):
        from chunk_protocol import ProtocolSerializerProvider

        self.doc, self.index = doc, index
        self.min, self.max = min_tokens, max_tokens
        self.counter = TokenCounter(tokenizer)
        self.serializer = serializer or ProtocolSerializerProvider().get_serializer(doc)
        self.glossary_tables = {r for c in chunks if c.get("chunk_type") == "glossary"
                                for r in c.get("doc_item_refs") or [] if r.startswith("#/tables/")}
        self.checks: dict[str, Any] = {}
        self._tree()
        self._units()
        self._atoms(chunks)
        base = chunks[0]["chunk_id"].rsplit("-", 1)[0] if chunks and chunks[0].get("chunk_id") else "doc"
        self._partition_all(base)
        self._map_chunks(chunks)
        self._section_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ tree
    def _tree(self) -> None:
        secs = list(self.index.sections)
        self.sec = {s.section_id: s for s in secs}
        self.kids: dict[str | None, list[str]] = {}
        for s in secs:
            kids = [k for k in _ids(self.index.children(s.section_id)) if k in self.sec]
            self.kids[s.section_id] = kids
        listed_kids = {k for ks in self.kids.values() for k in ks}
        self.kids[None] = [s.section_id for s in secs
                           if s.section_id not in listed_kids and (not s.parent_id or s.parent_id not in self.sec)]
        self.order: list[str] = []

        def walk(sid: str) -> None:
            self.order.append(sid)
            for k in self.kids.get(sid, []):
                walk(k)

        for top in self.kids[None]:
            walk(top)
        seen = set(self.order)
        self.order += [s.section_id for s in secs if s.section_id not in seen]  # unreachable nodes, if any
        self.up: dict[str, str | None] = {s: None for s in self.order}
        for p, ks in self.kids.items():
            for k in ks:
                if p is not None:
                    self.up[k] = p
        self.pos = {s: i for i, s in enumerate(self.order)}

    def path(self, sid: str) -> list[str]:
        out, s = [], sid
        while s is not None:
            out.append(s)
            s = self.up.get(s)
        return out[::-1]

    def subtree(self, sid: str) -> list[str]:
        out = [sid]
        for k in self.kids.get(sid, []):
            out += self.subtree(k)
        return out

    def lca(self, sids: list[str]) -> str | None:
        paths = [self.path(s) for s in dict.fromkeys(sids) if s in self.sec]
        if not paths:
            return None
        common = None
        for level in zip(*paths):
            if len(set(level)) != 1:
                break
            common = level[0]
        return common

    def role(self, sid: str | None) -> str | None:
        s = self.sec.get(sid) if sid else None
        return getattr(s, "role", None) if s is not None else None

    def breadcrumb(self, sid: str) -> str:
        return " > ".join(self.sec[s].title for s in self.path(sid) if self.sec[s].title)

    def _heading_line(self, sid: str, cont: bool = False) -> str:
        s = self.sec[sid]
        if not s.heading_ref or not s.title:
            return ""
        return f"{'#' * min(max(int(s.level or 1), 1), 6)} {s.title}{' (continued)' if cont else ''}"

    # ------------------------------------------------------------------ units
    def _units(self) -> None:
        from docling_core.types.doc import (DocItemLabel, GroupItem, ListItem, PictureItem, RefItem,
                                            SectionHeaderItem, TableItem, TextItem, TitleItem)

        from chunk_protocol import (ProtocolTableSerializer, dropped_figure, figure_blocks, figure_parts,
                                    figure_title)

        owned = owned_refs(self.doc)
        heading_refs = {s.heading_ref for s in self.sec.values() if getattr(s, "heading_ref", None)}
        md = ProtocolTableSerializer("markdown")
        self.units: list[_Unit] = []
        self.unit_of: dict[str, int] = {}
        self.own_units: dict[str, list[int]] = defaultdict(list)
        self.skipped: Counter = Counter()
        for sid in self.order:
            for ref in getattr(self.sec[sid], "item_refs", None) or []:
                try:
                    item = RefItem(cref=ref).resolve(self.doc)
                except Exception:  # noqa: BLE001 - a dangling ref in the index
                    self.skipped["dangling"] += 1
                    continue
                leaves = ([x for x, _ in self.doc.iterate_items(root=item, with_groups=False, traverse_pictures=False)]
                          if isinstance(item, GroupItem) else [item])
                for leaf in leaves:
                    lref = leaf.self_ref
                    if lref in self.unit_of or lref in owned or lref in heading_refs:
                        continue
                    pages = tuple(sorted({p.page_no for p in getattr(leaf, "prov", None) or []}))
                    if isinstance(leaf, TableItem):
                        if leaf.label == DocItemLabel.DOCUMENT_INDEX:
                            self.skipped["toc"] += 1
                            continue
                        text = md.serialize(item=leaf, doc_serializer=self.serializer, doc=self.doc).text
                        kind = "table"
                    elif isinstance(leaf, PictureItem):
                        f = figure_parts(leaf, self.doc)
                        if dropped_figure(leaf, self.doc, f):
                            self.skipped["figure_dropped"] += 1
                            continue
                        text = "\n\n".join([figure_title(leaf, f)] + [f"{lb}:\n{tx}" for lb, tx in figure_blocks(f)])
                        kind = "figure"
                    elif isinstance(leaf, SectionHeaderItem | TitleItem):
                        text, kind = " ".join(leaf.text.split()), "label"  # a minor heading kept inline
                    elif isinstance(leaf, ListItem):
                        text, kind = f"{leaf.marker or '-'} {leaf.text}".strip(), "list"
                    elif isinstance(leaf, TextItem):
                        text, kind = leaf.text.strip(), "text"
                    else:
                        continue
                    if not text.strip():
                        self.skipped["empty"] += 1
                        continue
                    self.unit_of[lref] = len(self.units)
                    self.own_units[sid].append(len(self.units))
                    self.units.append(_Unit(lref, kind, text, sid, pages))
        for u, n in zip(self.units, self.counter.many([u.text for u in self.units])):
            u.tokens = n
        self.head_tokens = dict(zip(self.order, self.counter.many([self._heading_line(s) or "" for s in self.order])))

    # ------------------------------------------------------------------ atoms (chunk-aligned)
    def _atoms(self, chunks: list[dict]) -> None:
        n = len(self.units)
        joined = [False] * (n + 1)  # joined[i]: unit i belongs to the same atom as unit i-1
        multi = 0
        for c in chunks:
            idx = sorted({self.unit_of[r] for r in c.get("doc_item_refs") or [] if r in self.unit_of})
            if not idx:
                continue
            for i in range(idx[0] + 1, idx[-1] + 1):
                joined[i] = True
            if len({self.units[i].section for i in idx}) > 1:
                multi += 1
        for i, u in enumerate(self.units):  # a minor heading belongs to what follows it
            if u.kind == "label":
                if i + 1 < n and self.units[i + 1].section == u.section:
                    joined[i + 1] = True
                elif i > 0 and self.units[i - 1].section == u.section:
                    joined[i] = True
        self.atoms: list[_Atom] = []
        self.atom_of_unit: list[int] = [0] * n
        for i in range(n):
            if i == 0 or not joined[i]:
                self.atoms.append(_Atom([], self.units[i].section))
            self.atoms[-1].units.append(i)
            self.atom_of_unit[i] = len(self.atoms) - 1
        texts = []
        for a in self.atoms:
            a.tables = [self.units[i].ref for i in a.units if self.units[i].kind == "table"]
            a.sections = frozenset(self.units[i].section for i in a.units)
            texts.append(self._render_units(a.units))
        for a, t in zip(self.atoms, self.counter.many(texts)):
            a.tokens = t
        self.own_atoms: dict[str, list[int]] = defaultdict(list)
        for k, a in enumerate(self.atoms):
            self.own_atoms[a.section].append(k)
        self.checks["chunks_spanning_sections"] = multi
        self.checks["atoms_spanning_sections"] = sum(len(a.sections) > 1 for a in self.atoms)

    def _render_units(self, idx: list[int]) -> str:
        blocks: list[str] = []
        prev = None
        for i in idx:
            u = self.units[i]
            if u.kind == "list" and prev == "list":
                blocks[-1] += "\n" + u.text
            else:
                blocks.append(u.text)
            prev = u.kind
        return "\n\n".join(blocks)

    # ------------------------------------------------------------------ partition
    def _est_atoms(self, atoms: list[int]) -> int:
        return sum(self.atoms[a].tokens + JOIN_TOKENS for a in atoms)

    def _subtree_est(self, sid: str) -> int:
        if not hasattr(self, "_sub_est"):
            self._sub_est: dict[str, int] = {}
        if sid not in self._sub_est:
            self._sub_est[sid] = (self._est_atoms(self.own_atoms.get(sid, [])) + self.head_tokens.get(sid, 0)
                                  + JOIN_TOKENS + sum(self._subtree_est(k) for k in self.kids.get(sid, [])))
        return self._sub_est[sid]

    def _subtree_has_atoms(self, sid: str) -> bool:
        return any(self.own_atoms.get(s) for s in self.subtree(sid))

    def _pack(self, atoms: list[int], limit: int) -> list[list[int]]:
        """Own-text atoms -> balanced windows <= limit at atom boundaries; an atom > limit stands alone."""
        total = self._est_atoms(atoms)
        if total <= limit:
            return [atoms] if atoms else []
        target = min(limit, total / math.ceil(total / limit) * 1.15)
        out: list[list[int]] = []
        cur: list[int] = []
        t = 0
        for a in atoms:
            at = self.atoms[a].tokens + JOIN_TOKENS
            if at > limit:
                if cur:
                    out.append(cur)
                out.append([a])
                cur, t = [], 0
                continue
            if cur and t + at > target:
                out.append(cur)
                cur, t = [], 0
            cur.append(a)
            t += at
        if cur:
            out.append(cur)
        return out

    def _partition(self, sid: str | None) -> list[_P]:
        if sid is not None and self._subtree_est(sid) <= self.max:
            if not self._subtree_has_atoms(sid):
                return []
            segs = [(s, list(self.own_atoms.get(s, [])), True) for s in self.subtree(sid)]
            return [_P("section", segs, self._subtree_est(sid))]
        items: list[_P] = []
        if sid is not None:
            head = self.head_tokens.get(sid, 0) + JOIN_TOKENS
            for w, win in enumerate(self._pack(self.own_atoms.get(sid, []), self.max - head)):
                big = len(win) == 1 and self.atoms[win[0]].tokens + JOIN_TOKENS > self.max - head
                kind = "table" if big and self.atoms[win[0]].tables else "window"
                items.append(_P(kind, [(sid, win, w == 0)], self._est_atoms(win) + head))
        for k in self.kids.get(sid, []):
            items += self._partition(k)
        i = 0  # merge tiny siblings (never into a table parent), preferring the next sibling
        while i < len(items):
            p = items[i]
            if p.est < self.min and p.kind != "table":
                nxt = items[i + 1] if i + 1 < len(items) else None
                prv = items[i - 1] if i > 0 else None
                if nxt is not None and nxt.kind != "table" and p.est + nxt.est <= self.max:
                    items[i:i + 2] = [_P("merged", p.segments + nxt.segments, p.est + nxt.est)]
                    continue
                if prv is not None and prv.kind != "table" and p.est + prv.est <= self.max:
                    items[i - 1:i + 1] = [_P("merged", prv.segments + p.segments, prv.est + p.est)]
                    i -= 1
                    continue
            i += 1
        return items

    def _render_segments(self, segments: list[tuple[str, list[int], bool]]) -> tuple[str, list[str], list[int]]:
        parts: list[str] = []
        refs: list[str] = []
        pages: set[int] = set()
        from docling_core.types.doc import RefItem

        for sid, atoms, first in segments:
            line = self._heading_line(sid, cont=not first)
            sec = self.sec[sid]
            if line:
                parts.append(line)
                if first and sec.heading_ref:
                    refs.append(sec.heading_ref)
                    try:
                        pages |= {p.page_no for p in RefItem(cref=sec.heading_ref).resolve(self.doc).prov}
                    except Exception:  # noqa: BLE001
                        pass
            units = [u for a in atoms for u in self.atoms[a].units]
            if units:
                parts.append(self._render_units(units))
                refs += [self.units[u].ref for u in units]
                pages |= {p for u in units for p in self.units[u].pages}
        return "\n\n".join(p for p in parts if p), refs, sorted(pages)

    def _partition_all(self, base: str) -> None:
        raw = [p for p in self._partition(None) if any(atoms for _, atoms, _ in p.segments)]
        texts, metas = [], []
        for p in raw:
            text, refs, pages = self._render_segments(p.segments)
            texts.append(text)
            metas.append((refs, pages))
        tokens = self.counter.many(texts)
        self.parents: list[Parent] = []
        self.parent_of_atom: dict[int, str] = {}
        self.segments: dict[str, list[tuple[str, list[int], bool]]] = {}
        for n, (p, text, (refs, pages), tok) in enumerate(zip(raw, texts, metas, tokens), start=1):
            pid = f"{base}-P{n:03d}"
            sids = list(dict.fromkeys(s for s, _, _ in p.segments))
            lca = self.lca(sids)
            if lca is not None and lca in sids:  # rooted at one section (section, window, intro + subsections)
                crumb = self.breadcrumb(lca)
            else:  # siblings merged: the common path, then the first and last member
                tops = list(dict.fromkeys(self._child_on_path(lca, s) for s in sids))
                names = [self.sec[t].title for t in tops if t in self.sec]
                span = names[0] if len(names) == 1 else f"{names[0]} … {names[-1]}"
                crumb = f"{self.breadcrumb(lca)} > {span}" if lca else span
            roles = list(dict.fromkeys(r for r in (self.role(s) for s in sids) if r))
            content = [s for s, atoms, _ in p.segments if atoms]
            role = self.role(content[0]) if content else (roles[0] if roles else None)
            tables = [t for _, atoms, _ in p.segments for a in atoms for t in self.atoms[a].tables]
            self.parents.append(Parent(pid, p.kind, sids, crumb, role, roles, pages, tok, refs, text,
                                       [], tables))
            self.segments[pid] = p.segments
            for _, atoms, _ in p.segments:
                for a in atoms:
                    self.parent_of_atom[a] = pid
        self.by_id = {p.parent_id: p for p in self.parents}
        self.checks["atoms_without_parent"] = sum(1 for a in range(len(self.atoms)) if a not in self.parent_of_atom)

    def _child_on_path(self, anc: str | None, sid: str) -> str:
        path = self.path(sid)
        if anc is None:
            return path[0]
        i = path.index(anc) if anc in path else -1
        return path[i + 1] if 0 <= i < len(path) - 1 else sid

    # ------------------------------------------------------------------ chunks -> parents
    def _map_chunks(self, chunks: list[dict]) -> None:
        self.parent_of_chunk: dict[str, str] = {}
        self.section_of_chunk: dict[str, str | None] = {}
        multi, unmapped = [], []
        for c in chunks:
            cid = c.get("chunk_id")
            refs = c.get("doc_item_refs") or []
            pids = list(dict.fromkeys(self.parent_of_atom.get(self.atom_of_unit[self.unit_of[r]])
                                      for r in refs if r in self.unit_of))
            pids = [p for p in pids if p]
            sec = next((self.units[self.unit_of[r]].section for r in refs if r in self.unit_of), None)
            if sec is None and refs:
                s = self.index.section_of(refs[0])
                sec = _sid(s)
            if not pids:  # no item of the chunk is a unit: the parent holding its section's first atom
                unmapped.append(cid)
                for s in ([sec] if sec else []) + ([self.up.get(sec)] if sec else []):
                    sub = [a for x in self.subtree(s) for a in self.own_atoms.get(x, [])] if s else []
                    if sub:
                        pids = [self.parent_of_atom.get(min(sub))]
                        break
            if len(pids) > 1:
                multi.append(cid)
            if pids and pids[0]:
                self.parent_of_chunk[cid] = pids[0]
                self.by_id[pids[0]].chunk_ids.append(cid)
            self.section_of_chunk[cid] = sec
        self.checks.update({"chunks": len(chunks), "chunks_mapped": len(self.parent_of_chunk),
                            "chunks_in_several_parents": len(multi), "chunks_without_unit": len(unmapped)})
        self._multi, self._unmapped = multi, unmapped

    def annotate(self, chunks: list[dict]) -> None:
        """Write section_id, role and parent_id into each chunk record (metadata only)."""
        for c in chunks:
            sid = self.section_of_chunk.get(c.get("chunk_id"))
            c["section_id"] = sid
            c["role"] = self.role(sid)
            c["parent_id"] = self.parent_of_chunk.get(c.get("chunk_id"))

    def parent_of(self, chunk: dict) -> Parent | None:
        """The chunk's parent in this partition (its stored parent_id only if this store never saw it)."""
        pid = self.parent_of_chunk.get(chunk.get("chunk_id")) or chunk.get("parent_id")
        return self.by_id.get(pid) if pid else None

    def chunk_role(self, chunk: dict) -> str | None:
        sid = self.section_of_chunk.get(chunk.get("chunk_id"))
        return self.role(sid) if sid else chunk.get("role")

    # ------------------------------------------------------------------ section subtrees (small-to-big)
    def section_est(self, sid: str) -> int:
        return self._subtree_est(sid)

    def section_block(self, sid: str) -> dict:
        """The whole subtree of a section: Markdown text, tokens, pages, refs, the parents fully inside."""
        if sid not in self._section_cache:
            segs = [(s, list(self.own_atoms.get(s, [])), True) for s in self.subtree(sid)]
            text, refs, pages = self._render_segments(segs)
            inside = set(self.subtree(sid))
            within = [p.parent_id for p in self.parents if set(p.section_ids) <= inside]
            self._section_cache[sid] = {"section_id": sid, "text": text, "tokens": self.counter.count(text),
                                        "pages": pages, "refs": refs, "parents": within,
                                        "breadcrumb": self.breadcrumb(sid)}
        return self._section_cache[sid]

    def level(self, sid: str) -> int:
        return len(self.path(sid))

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict:
        toks = sorted(p.tokens for p in self.parents)
        pct = lambda q: toks[min(len(toks) - 1, int(q * len(toks)))] if toks else 0  # noqa: E731
        return {
            "parents": len(self.parents), "min_tokens": self.min, "max_tokens": self.max,
            "kinds": dict(Counter(p.kind for p in self.parents)),
            "tokens": {"min": toks[0] if toks else 0, "median": pct(0.5), "p90": pct(0.9),
                       "max": toks[-1] if toks else 0, "total": sum(toks)},
            "under_min": sum(t < self.min for t in toks),
            "over_max_non_table": sum(p.tokens > self.max and p.kind != "table" for p in self.parents),
            "sections": len(self.sec), "units": len(self.units), "atoms": len(self.atoms),
            "skipped_items": dict(self.skipped), "index": getattr(self.index, "source", type(self.index).__module__),
            **self.checks,
        }


def build_parents(doc: Any, index: Any, chunks: list[dict], tokenizer: Any,
                  min_tokens: int = MIN_TOKENS, max_tokens: int = MAX_TOKENS) -> list[Parent]:
    """The structural partition; writes parent_id, section_id and role into each chunk record."""
    store = ParentStore(doc, index, chunks, tokenizer, min_tokens, max_tokens)
    store.annotate(chunks)
    return store.parents


# =========================================================================== context helpers

_WORD = re.compile(r"[^\W_]+")


def shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = _WORD.findall((text or "").casefold())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


def containment(a: set, b: set) -> float:
    """Share of a's shingles that are also in b."""
    return len(a & b) / len(a) if a else 0.0


_ENUMERATION = re.compile(
    r"\b(?:list|lists|listed|all|enumerate|complete|full)\b"
    # "each visit", "every criterion" -- not a frequency ("every 3 weeks", "each day")
    r"|\b(?:every|each)\s+(?!\d|other\b|(?:day|week|month|year|cycle|hour|minute|dose|time)s?\b)\w"
    r"|\b(?:what|which)\s+(?:are|were)\b|\bwhich\s+\w+s\b|\bhow many\b"
    r"|\b(?:criteria|objectives|endpoints|encounters|visits|activities|assessments|procedures|arms|epochs"
    r"|amendments|interventions|cohorts|periods|estimands|schedule)\b", re.I)


def enumeration_like(question: str) -> bool:
    """A question asking for a list ("all inclusion criteria", "which activities at visit X")."""
    return bool(_ENUMERATION.search(question or ""))


# =========================================================================== eval_v1 integration

class ParentUnits:
    """eval_v1 unit expander: a child chunk -> its whole parent (S2 parent-level recall)."""

    name = "parents"

    def __init__(self, builder: Any):
        self.store = builder.store

    def unit(self, chunk: dict):
        from eval_v1 import Unit

        p = self.store.parent_of(chunk)
        if p is None:
            return Unit(frozenset([chunk.get("chunk_id")]), chunk.get("text") or "")
        return Unit(frozenset([p.parent_id]), p.text)


class ParentAssembler:
    """eval_v1 assembler: retrieve.ContextBuilder in parent mode on the top-k hits."""

    def __init__(self, builder: Any):
        self.builder = builder
        self.name = (f"parents(budget={builder.budget}, identity={builder.identity}, "
                     f"section_expand={builder.section_expand})")

    def build(self, q: dict, retrieval: Any, k: int):
        from eval_v1 import Ctx
        from retrieve import Result

        res = retrieval.payload
        ctx = self.builder.build(Result(res.query, res.expanded_query, res.definitions, res.hits[:k]))
        return Ctx(ctx.render(), ctx.tokens, {
            "blocks": len(ctx.blocks), "fallbacks": len(ctx.fallbacks), "dropped": len(ctx.dropped),
            "duplicates": len(ctx.duplicates), "expanded": len(ctx.expanded), "parents": len(ctx.parents)})


def make_env(doc: str, chunks_path: Any, opts: Any, **builder_opts: Any):
    """eval_v1.default_env with the parent-aware assembler and parent units plugged in."""
    import eval_v1

    env = eval_v1.default_env(doc, chunks_path, opts)
    base = getattr(env.assembler, "builder", None)
    if base is None:
        return env
    builder = base.with_mode("parents", **builder_opts)
    env.units = ParentUnits(builder)
    env.assembler = ParentAssembler(builder)
    stats = builder.store.stats()
    env.notes.append(f"parents: {stats['parents']} ({stats['kinds']}), index {stats['index']}, chunks mapped "
                     f"{stats['chunks_mapped']}/{stats['chunks']}, in several parents {stats['chunks_in_several_parents']}")
    return env


def env_factory(doc: str, chunks_path: Any, opts: Any):
    """`--env-factory parents:env_factory`: parents, 8K budget, identity block, section small-to-big."""
    return make_env(doc, chunks_path, opts, budget=8000)


def env_factory_12k(doc: str, chunks_path: Any, opts: Any):
    return make_env(doc, chunks_path, opts, budget=12000)


def env_factory_noid(doc: str, chunks_path: Any, opts: Any):
    """Ablation: no identity block."""
    return make_env(doc, chunks_path, opts, budget=8000, identity=False)


def env_factory_nosec(doc: str, chunks_path: Any, opts: Any):
    """Ablation: no section-level small-to-big."""
    return make_env(doc, chunks_path, opts, budget=8000, section_expand=False)
