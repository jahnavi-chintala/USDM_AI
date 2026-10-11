"""Abbreviation glossary of a DoclingDocument, found by content, not by layout.

Used by chunk_protocol.py (writes abbreviations.json and glossary.json) and by
retrieval (Glossary.find_in_text). Three sources, in order of trust:

  table      Every table is scored: for the best (short, long) column pair,
             the share of data rows whose short cell looks like an
             abbreviation and whose long cell matches it letter by letter
             (acronym_match). Most rows of a real list match (ICANS = Immune
             effector Cell-Associated Neurotoxicity Syndrome) while data tables
             (doses, "Grade | Description", objectives) do not. A heading such
             as Abbreviations / Acronyms / Glossary, or a header cell such as
             "Abbreviation", lowers the threshold but is not required. Once a
             table is accepted, all its rows are taken: real lists also hold
             entries no letter rule explains (BID = twice a day). 2- and
             3-column layouts (abbreviation | term [| definition], term |
             abbreviation) are found by the column-pair search; header rows
             come from Docling's column_header flags (or a header-word row when
             a table has no flags); a table that directly follows an accepted
             one with the same columns is a page-split continuation.
  text_list  The same idea for lists written as lines or list items
             ("AE: adverse event", "AE - adverse event", "AE<tab>adverse
             event", "AE  adverse event"): a section is accepted when most of
             its lines parse as matching pairs (fewer under such a heading).
  inline     Schwartz & Hearst (2003, PSB) "long form (SF)" / "SF (long
             form)" over body text and table cells, a faithful port of their
             reference implementation (ExtractAbbrev.java). Its output is then
             filtered for protocols: the short form must contain a capital
             letter (drops "eg", "ie", units). Inline definitions only fill
             gaps; where they disagree with the list, the list wins and the
             inline variants are kept as alternatives.

Pages of rows in a table merged across pages are recovered from the cell
boxes (each page's cells restart near the top).
"""

import json
import math
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from docling_core.types.doc import (
    ContentLayer,
    DocItemLabel,
    DoclingDocument,
    PictureItem,
    SectionHeaderItem,
    TableItem,
    TextItem,
    TitleItem,
)

HEADING = re.compile(r"\b(abbreviations?|acronyms?|glossary|nomenclature)\b", re.I)
HEADER_CELL = re.compile(r"^(abbreviations?|acronyms?|short forms?|terms?|definitions?|meanings?|"
                         r"descriptions?|explanations?|full (names?|forms?|terms?))$", re.I)
STOPWORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or", "per",
             "the", "to", "with", "without", "versus", "vs"}
GREEK = {"α": "a", "β": "b", "γ": "g", "δ": "d", "ε": "e", "κ": "k", "λ": "l", "μ": "m",
         "τ": "t", "ω": "o", "Α": "A", "Β": "B", "Γ": "G", "Δ": "D"}
# Two-letter English words: as abbreviations they only count outside capitalised text.
ENGLISH_2 = {"AM", "AN", "AS", "AT", "BE", "BY", "DO", "GO", "HE", "IF", "IN", "IS", "IT", "ME",
             "MY", "NO", "OF", "OK", "ON", "OR", "SO", "TO", "UP", "US", "WE"}
ROMAN = re.compile(r"^(I|II|III|VI|VII|VIII|IX|XI|XII)$")
SKIP_LABELS = {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}
TABLE_MIN_FRAC, TABLE_MIN_FRAC_HINTED, TABLE_CONT_FRAC = 0.5, 0.3, 0.3


# --------------------------------------------------------------------------- records

@dataclass
class Entry:
    abbr: str
    definition: str
    source: str                  # "table" | "text_list" | "inline"
    page: int | None = None
    ref: str | None = None       # self_ref of the table / text item
    note: str | None = None      # third column of a 3-column table (description)
    alternatives: list[dict] = field(default_factory=list)  # other definitions seen


@dataclass
class Glossary:
    entries: dict[str, Entry] = field(default_factory=dict)
    table_refs: list[str] = field(default_factory=list)  # accepted abbreviation tables

    def definitions(self) -> dict[str, str]:
        """{abbr: definition}, the abbreviations.json format."""
        return {a: e.definition for a, e in self.entries.items()}

    def to_json(self) -> dict:
        return {"tables": self.table_refs,
                "entries": {a: {k: v for k, v in asdict(e).items() if k != "abbr"}
                            for a, e in self.entries.items()}}

    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        """Read glossary.json, or a plain {abbr: definition} abbreviations.json."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if "entries" not in data:
            return cls({a: Entry(a, d, "table") for a, d in data.items()})
        return cls({a: Entry(a, **e) for a, e in data["entries"].items()}, data.get("tables", []))

    def find_in_text(self, text: str) -> dict[str, str]:
        """{abbr: definition} for the glossary abbreviations written in `text`.

        Case-sensitive whole-token match, longest first (R-ISS before ISS, CAR-T
        before CAR), plural "s" allowed after a capital (AEs). One-character
        abbreviations (C = Cycle) never match; two-character ones only when
        written in capitals and not inside capitalised text ("AND/OR", "CONTACT
        US"), and not at all if they contain a lower-case letter.
        """
        found = {}
        for m in _pattern(tuple(self.entries)).finditer(text):
            abbr = m.group(0)
            if abbr not in self.entries:
                abbr = abbr[:-1]  # plural
            if _alnum_len(abbr) <= 2 and not _short_ok(abbr, text, m.start(), m.end(), self.entries):
                continue
            found.setdefault(abbr, self.entries[abbr].definition)
        return found


def _alnum_len(s: str) -> int:
    return sum(c.isalnum() for c in s)


@lru_cache(maxsize=8)
def _pattern(abbrs: tuple[str, ...]) -> re.Pattern:
    keys = sorted((a for a in abbrs if _alnum_len(a) >= 2), key=len, reverse=True)
    alt = "|".join(re.escape(a) + ("s?" if a[-1].isupper() else "") for a in keys)
    return re.compile(rf"(?<!\w)(?:{alt})(?!\w)" if keys else r"(?!x)x")


def _short_ok(abbr: str, text: str, start: int, end: int, known: dict) -> bool:
    if _alnum_len(abbr) < 2 or any(c.islower() for c in abbr):
        return False
    prev = re.search(r"([A-Za-z]+)[^A-Za-z]*$", text[:start])
    nxt = re.match(r"[^A-Za-z]*([A-Za-z]+)", text[end:])
    for m in (prev, nxt):
        word = m.group(1) if m else ""
        if word.isupper() and (abbr in ENGLISH_2 or (len(word) >= 3 and word not in known)):
            return False
    return True


# --------------------------------------------------------------------------- acronym matching

def looks_like_abbreviation(s: str) -> bool:
    """Short, acronym-shaped: ICANS, mAb, eCRF, CO2, Q2W, β-hCG, EORTC QLQ-C30, C."""
    s = s.strip()
    if not s or len(s) > 20 or len(s.split()) > 3 or not (s[0].isalnum() or s[0] in GREEK):
        return False
    if not any(c.isalpha() for c in s):
        return False
    if ROMAN.match(s):  # stage / grade labels (I, II, III)
        return False
    if len(s) == 1:
        return s.isupper()
    upper = sum(c.isupper() for c in s)
    return upper >= 2 or any(c.isupper() for c in s[1:]) or (upper and any(c.isdigit() for c in s))


def _greek(s: str) -> str:
    return "".join(GREEK.get(c, c) for c in s)


def acronym_match(sf: str, lf: str, min_start_share: float = 0.5) -> bool:
    """Do the short form's characters spell out the long form?

    Every letter of the short form must occur in the long form in order; the
    first one must start the first word (leading stopwords may be skipped) and
    at least `min_start_share` of them must start a word, a hyphenated part or
    an inner capital (mAb: m, a start words, b is inside "antibody").
    Digits may be unmatched (CO2 = carbon dioxide); Greek letters count as
    their Latin initial (β-hCG = beta-human chorionic gonadotropin); a plural
    "s" after a capital is ignored (AEs).
    """
    sf, lf = _greek(sf.strip()), _greek(" ".join(lf.split()))
    if re.search(r"[A-Z]s$", sf):
        sf = sf[:-1]
    chars = [c.lower() for c in sf if c.isalnum()]
    letters = sum(c.isalpha() for c in chars)
    if not letters or len(lf) <= len(sf):
        return False
    low = lf.lower()
    starts = [i == 0 or not lf[i - 1].isalnum() or (lf[i].isupper() and lf[i - 1].islower())
              for i in range(len(lf))]
    first_ok = []  # start positions of words up to and including the first non-stopword
    for w in re.finditer(r"[^\W_]+", lf):
        first_ok.append(w.start())
        if w.group(0).lower() not in STOPWORDS:
            break
    while chars and chars[0].isdigit():  # leading digits (rare) are optional
        chars.pop(0)
    n, m = len(chars), len(low)

    @lru_cache(maxsize=None)
    def best(i: int, j: int) -> int:  # max word-start matches for chars[i:] in low[j:]; -1 = none
        if i == n:
            return 0
        res = best(i + 1, j) if chars[i].isdigit() else -1
        for k in range(j, m):
            if low[k] == chars[i]:
                rest = best(i + 1, k + 1)
                if rest >= 0:
                    res = max(res, rest + starts[k])
        return res

    strong = max((1 + r for k in first_ok if low[k] == chars[0] and (r := best(1, k + 1)) >= 0),
                 default=-1)
    return strong >= max(1, math.ceil(letters * min_start_share))


# --------------------------------------------------------------------------- Schwartz-Hearst

def _sh_best_long_form(sf: str, lf: str) -> str | None:
    """findBestLongForm: match short-form characters right to left in the long form."""
    s, l = len(sf) - 1, len(lf) - 1
    while s >= 0:
        c = sf[s].lower()
        if not c.isalnum():
            s -= 1
            continue
        # The first short-form character must start a word.
        while (l >= 0 and lf[l].lower() != c) or (s == 0 and l > 0 and lf[l - 1].isalnum()):
            l -= 1
        if l < 0:
            return None
        l -= 1
        s -= 1
    return lf[lf.rfind(" ", 0, l + 1) + 1:]


def _sh_pair(sf: str, lf: str) -> tuple[str, str] | None:
    """extractAbbrPair: the best long form, if it passes the paper's size limits."""
    if len(sf) < 2:
        return None
    best = _sh_best_long_form(sf, lf)
    if best is None:
        return None
    lf_words = len([t for t in re.split(r"[ \t\n\r\f-]+", best) if t])
    sf_chars = _alnum_len(sf)
    if (len(best) < len(sf) or f"{sf} " in best or best.endswith(sf)
            or lf_words > sf_chars * 2 or lf_words > sf_chars + 5 or sf_chars > 10):
        return None
    return sf, best


def _sh_valid_short_form(sf: str) -> bool:
    return any(c.isalpha() for c in sf) and (sf[:1].isalnum() or sf[:1] == "(")


def schwartz_hearst(text: str) -> list[tuple[str, str]]:
    """(short form, long form) pairs defined in `text` ("long form (SF)" or "SF (long form)")."""
    pairs, sent = [], " ".join(text.split())
    op = sent.find(" (")
    while op > -1:
        op += 1
        cp = sent.find(")", op)
        if cp < 0:
            break
        # Long-form candidate: back to the previous sentence or clause boundary.
        cut = max(sent.rfind(". ", 0, op), sent.rfind(", ", 0, op))
        lf = sent[(cut + 2 if cut > -1 else 0):op]
        sf = sent[op + 1:cp]
        if len(sf) > 1 and len(lf) > 1:
            if "(" in sf and (ncp := sent.find(")", cp + 1)) > -1:  # nested parentheses
                sf, cp = sent[op + 1:ncp], ncp
            for sep in (", ", "; "):
                if (k := sf.find(sep)) > -1:
                    sf = sf[:k]
            if len(sf.split()) > 2 or len(sf) > len(lf):  # "SF (long form)"
                k = sent.rfind(" ", 0, op - 2)
                lf, sf = sf, sent[k + 1:op - 1]
                if not any(c.isupper() for c in sf):
                    sf = ""
            sf, lf = sf.strip(), lf.strip()
            if _sh_valid_short_form(sf) and (pair := _sh_pair(sf, lf)):
                pairs.append(pair)
        sent = sent[cp + 1:]
        op = sent.find(" (")
    return pairs


# --------------------------------------------------------------------------- document scan

def _clean(s: str) -> str:
    return " ".join(s.split())


def _header_rows(table: TableItem, grid: list[list[str]]) -> int:
    """Leading rows flagged column_header by Docling; else one header-word row."""
    cells = table.data.grid
    if any(c.column_header for c in table.data.table_cells):
        n = 0
        for row in cells:
            filled = [c for c in row if c.text.strip()]
            if not filled or not all(c.column_header for c in filled):
                break
            n += 1
        return n
    first = [t for t in grid[0] if t] if grid else []
    return 1 if first and all(HEADER_CELL.match(t) for t in first) else 0


def _row_pages(table: TableItem) -> list[int | None]:
    """Page of each row; a merged multi-page table restarts near the top on each page."""
    pages = [p.page_no for p in table.prov]
    if len(pages) <= 1:
        return [pages[0] if pages else None] * table.data.num_rows
    tops = [min((c.bbox.t for c in table.data.table_cells
                 if c.start_row_offset_idx == r and c.bbox is not None), default=None)
            for r in range(table.data.num_rows)]
    out, k, last = [], 0, None
    for t in tops:
        if t is not None and last is not None and t < last - 1 and k < len(pages) - 1:
            k += 1  # TOPLEFT y moved back up: next page
        last = t if t is not None else last
        out.append(pages[k])
    return out


def _table_score(grid: list[list[str]], pair: tuple[int, int] | None = None
                 ) -> tuple[float, int, tuple[int, int]]:
    """(matching share, matching rows, best (short, long) column pair)."""
    n_cols = len(grid[0]) if grid else 0
    pairs = [pair] if pair else [(a, b) for a in range(n_cols) for b in range(n_cols) if a != b]
    best = (0.0, 0, (0, 1))
    rows = [r for r in grid if any(r)]
    for a, b in pairs:
        hits = sum(1 for r in rows if r[a] != r[b] and looks_like_abbreviation(r[a])
                   and acronym_match(r[a], r[b]))
        if rows and (hits / len(rows), hits) > best[:2]:
            best = (hits / len(rows), hits, (a, b))
    return best


_SEP_LINE = re.compile(r"^(?P<sf>[^\s:=][^:=\t]{0,19}?)\s*(?:\t|:|=|\s[–—-]\s|\s{2,})\s*(?P<lf>\S.*)$")


def parse_line(line: str, lenient: bool = False) -> tuple[str, str, bool] | None:
    """(abbreviation, definition, letters match) of a glossary line, or None.

    Explicit separators (tab, ':', '=', spaced dash, 2+ spaces) first; otherwise
    the split after 1-3 words whose letters spell out the rest. `lenient`
    (inside an abbreviations section) also accepts a non-matching first word.
    """
    line = line.strip().lstrip("•●▪◦*-– ").strip()
    if m := _SEP_LINE.match(line):
        sf, lf = m.group("sf").strip(), m.group("lf").strip()
        if looks_like_abbreviation(sf) and len(lf) > len(sf):
            return sf, lf, acronym_match(sf, lf)
    words = line.split()
    for k in range(1, min(3, len(words) - 1) + 1):
        sf, lf = " ".join(words[:k]), " ".join(words[k:])
        if looks_like_abbreviation(sf) and acronym_match(sf, lf):
            return sf, lf, True
    if lenient and len(words) > 1 and looks_like_abbreviation(words[0]):
        return words[0], " ".join(words[1:]), False
    return None


def _page(item) -> int | None:
    return item.prov[0].page_no if getattr(item, "prov", None) else None


def _same_definition(a: str, b: str) -> bool:
    key = lambda s: re.sub(r"[^a-z0-9]", "", re.sub(r"\((s|es|ly)\)", r"\1", _greek(s).lower()))  # noqa: E731
    ka, kb = key(a), key(b)
    return ka == kb or ka.rstrip("s") == kb.rstrip("s")


def build_glossary(doc: DoclingDocument) -> Glossary:
    """Abbreviation lists (tables, text lines) first; inline definitions fill gaps."""
    listed: list[Entry] = []
    table_refs: list[str] = []
    sections: list[tuple[bool, list]] = [(False, [])]  # (abbreviation heading?, text items)
    inline_src: list[tuple[str, object]] = []          # (text, item) for Schwartz-Hearst
    heading_hint = False
    prev_table: tuple[TableItem, tuple[int, int]] | None = None  # accepted, nothing since

    for item, _ in doc.iterate_items(with_groups=False, included_content_layers={ContentLayer.BODY}):
        if item.label in SKIP_LABELS:
            continue
        if isinstance(item, SectionHeaderItem | TitleItem):
            heading_hint = bool(HEADING.search(item.text))
            sections.append((heading_hint, []))
            prev_table = None
            inline_src.append((item.text, item))
            continue
        if isinstance(item, TableItem):
            if item.label == DocItemLabel.DOCUMENT_INDEX:
                prev_table = None
                continue
            grid = [[_clean(c.text) for c in row] for row in item.data.grid]
            if not grid:
                continue
            n_head = _header_rows(item, grid)
            body = grid[n_head:]
            hinted = heading_hint or any(HEADER_CELL.match(t) for r in grid[:n_head] for t in r if t)
            pair = None
            if prev_table and prev_table[0].data.num_cols == item.data.num_cols:
                # Page-split continuation: same columns, may repeat the header row unflagged.
                prev_grid = prev_table[0].data.grid
                if not n_head and body and " ".join(body[0]) == " ".join(_clean(c.text) for c in prev_grid[0]):
                    body = body[1:]
                frac, hits, pair = _table_score(body, prev_table[1])
                accepted = hits >= 1 and frac >= TABLE_CONT_FRAC
            else:
                frac, hits, pair = _table_score(body)
                accepted = (hits >= 2 and frac >= TABLE_MIN_FRAC_HINTED) if hinted else \
                           (hits >= 3 and frac >= TABLE_MIN_FRAC)
            if not accepted:
                prev_table = None
                for row in grid:  # cells are body text for inline definitions
                    inline_src += [(t, item) for t in dict.fromkeys(row) if "(" in t]
                continue
            prev_table = (item, pair)
            table_refs.append(item.self_ref)
            a, b = pair
            pages = _row_pages(item)[n_head:]
            if len(pages) != len(body):  # continuation header row dropped above
                pages = pages[-len(body):] if body else []
            for row, page in zip(body, pages):
                sf, lf = row[a], row[b]
                if not sf or not lf or sf == lf:
                    continue
                note = " ".join(t for c, t in enumerate(row) if c not in pair and t and t not in (sf, lf))
                listed.append(Entry(sf, lf, "table", page, item.self_ref, note or None))
            continue
        if isinstance(item, PictureItem) or not isinstance(item, TextItem) or not item.text.strip():
            continue
        if item.label not in (DocItemLabel.CAPTION, DocItemLabel.FOOTNOTE):
            prev_table = None
        sections[-1][1].append(item)
        inline_src.append((item.text, item))

    # Text-line lists.
    text_list_refs = set()
    for hinted, items in sections:
        lines = [(ln, it) for it in items for ln in it.text.splitlines() if ln.strip()]
        if not lines:
            continue
        parsed = [(p, it) for ln, it in lines if (p := parse_line(ln, lenient=hinted))]
        hits = sum(p[2] for p, _ in parsed)
        if hinted:
            ok = hits >= 2 and len(parsed) / len(lines) >= 0.5
        else:
            ok = hits >= 5 and hits / len(lines) >= 0.6
        if ok:
            for (sf, lf, _), it in parsed:
                listed.append(Entry(sf, lf, "text_list", _page(it), it.self_ref))
                text_list_refs.add(it.self_ref)

    entries: dict[str, Entry] = {}
    for e in listed:
        if e.abbr not in entries:
            entries[e.abbr] = e
        elif not _same_definition(entries[e.abbr].definition, e.definition):
            _add_alternative(entries[e.abbr], e)

    # Inline definitions: most frequent long form per short form.
    found: dict[str, list[Entry]] = {}
    for text, item in inline_src:
        if item.self_ref in text_list_refs or item.self_ref in table_refs:
            continue
        for sf, lf in schwartz_hearst(text):
            if not _plausible_inline(sf, lf):
                continue
            if sf not in entries and sf.endswith("s") and sf[:-1] in entries:
                sf = sf[:-1]  # plural of a listed abbreviation
            found.setdefault(sf, []).append(Entry(sf, lf, "inline", _page(item), item.self_ref))
    for sf, seen in found.items():
        groups: list[list[Entry]] = []
        for e in seen:
            g = next((g for g in groups if _same_definition(g[0].definition, e.definition)), None)
            (g.append(e) if g else groups.append([e]))
        groups.sort(key=len, reverse=True)  # stable: ties keep first-seen order
        if sf in entries:
            for g in groups:
                if not _same_definition(entries[sf].definition, g[0].definition):
                    _add_alternative(entries[sf], g[0], len(g))
            continue
        top = groups[0][0]
        entry = Entry(sf, top.definition, "inline", top.page, top.ref)
        for g in groups[1:]:
            _add_alternative(entry, g[0], len(g))
        entries[sf] = entry
    return Glossary(entries, table_refs)


def _plausible_inline(sf: str, lf: str) -> bool:
    """Protocol filter on Schwartz-Hearst pairs (not part of their algorithm).

    The short form must be abbreviation-shaped in every word and start with a
    letter (drops "eg", "ie", "pts", "Cohort A", "5 g/L"), and its first letter
    must start the long form's first content word (drops "SD = Change/Stable
    Disease", "IR = neuropathy/immune-related"). Single-word expansions stay
    (ECG = electrocardiogram).
    """
    return (looks_like_abbreviation(sf) and (sf[0].isalpha() or sf[0] in GREEK)
            and all(looks_like_abbreviation(w) for w in sf.split())
            and acronym_match(sf, lf, min_start_share=0))


def _add_alternative(entry: Entry, other: Entry, count: int = 1) -> None:
    if any(_same_definition(a["definition"], other.definition) for a in entry.alternatives):
        return
    entry.alternatives.append({"definition": other.definition, "source": other.source,
                               "page": other.page, "ref": other.ref, "count": count})


if __name__ == "__main__":
    import sys

    g = build_glossary(DoclingDocument.load_from_json(sys.argv[1]))
    from collections import Counter

    print(f"{len(g.entries)} entries {dict(Counter(e.source for e in g.entries.values()))}; "
          f"tables {g.table_refs}")
