"""Structured Schedule of Activities (SoA) from a converted protocol (final DoclingDocument).

Consumer: the agent that builds USDM 4 data (SoA -> Encounter, Activity, ScheduledActivityInstance, Timing
windows, Epoch hints, Condition from footnotes). Everything is generic: no sponsor-specific strings.

API
  extract_soa(doc, index=None) -> list[dict]       one dict per SoA (see the contract below)
  soa_lookup(soas, visit=None, activity=None)      matching cells with their labels (case/abbreviation tolerant)
  to_markdown(soa)                                 a compact display table plus the footnotes

Contract (one dict per SoA; merged multi-page tables are one table; tables linked as continuation chains
(table_merge.continuation_chains) and adjacent SoA tables that continue each other are combined):
  {"table_id", "table_ids", "caption", "section_id", "pages",
   "visits": [{"id": "V1", "label", "header_path", "window", "timing", "column",
               + "table_id", "epoch_hint", "footnote_markers", "header_cells", "header_text", "label_composed"}],
   "activities": [{"id": "A1", "label", "section", "notes", "footnote_markers", "row", + "table_id"}],
   "cells": [{"activity", "visit", "mark", "text", "qualifier", "footnote_markers"}],
   "footnotes": {"a": str, ...}, "header_notes": [str], "section_rows": [{"table_id", "row"}], "warnings": [str]}
  label = the leaf header text; epoch_hint = the outermost group header spanning >= 2 columns; timing = the
  timing expressions of the header path ('Cycle 1, D8'; bare numbers take the stub's unit: 'W2 15' under
  'Week [W] Study day' -> 'W2 Day 15'), else a pure timing in the window row ('Day -28 to Day -1');
  header_cells = the grid cells behind header_path, parallel to it (same length and order), for quoting the PDF: {"text"
  as printed (whitespace-collapsed, footnote markers and a header note after a newline kept), "table_id", "row" and
  "column" (the grid row and column where the cell starts), "unit" (the word added to a bare number: 'Hour', 'Days';
  else null), "unit_from" ({"text", "column"} of the label-column cell that names the unit, in the cell's row or,
  for a cell starting in a visit-window row, in its first row below that holds the header path; else null)},
  taken from the table the visit is taken from; header_text = the leaf cell's text (null without a header path);
  label_composed = a unit was added to the leaf part, so the label is not the cell's own text ('Hours' | '0' ->
  'Hour 0': quote header_text and unit_from.text, not the label).
  column/row/table_id = where the visit/activity first appears. Only scheduled cells are listed (no blanks or
  dashes); a cell spanning several visits is listed for each; mark = an X-like glyph (also 'X----X' or an
  arrow drawn across visits), qualifier = the other text ('Cycle 1-3', 'if necessary' of 'X (if necessary)').
  An activity's footnote_markers include those of its section row.

Detection: tables in sections whose role is "soa" (structure.SectionIndex, when given) with a relaxed threshold;
otherwise mark-heavy grids (>= 6 columns, >= 15% of the non-empty body cells are marks, >= 4 marks) with
visit-like header tokens. The next table (same or next page, no new caption) continues the SoA when it repeats
the visits ('rows': more activities), has no header and the same columns, repeats the activities with other
visits ('cols': a horizontally split SoA), or is chained by table_merge.

Structure of one table (all from the grid):
  * header rows: the leading run of flagged rows plus unflagged header-like rows (>= 2 filled cells with
    digits / visit words, or a visit/window stub label) before the first row with a mark;
  * label columns: column 0 plus following columns of non-mark text (or an empty stub column holding unit
    words, e.g. 'VISIT' / 'WEEK') whose header has no digits; a bare number in a header row is prefixed with
    the unit word of that row's stub ('Days' | '-21' -> 'Day -21'); a header cell's text after a newline is
    a header note;
  * notes columns: a header starting with a keyword (notes, comments, protocol section, reference, ...) or,
    as the last column, long text; never a visit;
  * the visit-window row: a header-block row whose stub says 'window' (without a day/week unit) or whose
    cells are window expressions ('+-3D', '-1/+3D') -> visits[].window; windows written inside a visit header
    ('D1 (+-1)', 'W2 15+-4') go to visits[].window too;
  * adjacent grid columns under one header cell whose entries never conflict are one visit;
  * section rows: one cell across the table, a row repeating one text, or (in tables without such rows) a
    label-only row followed by a filled row -> activities[].section; the label-only ones are also listed in
    section_rows (grid row of each source table), so the chunk serializer writes them as sections too;
  * header rows repeated inside the body (each page repeats the header) are dropped.
Footnote markers: '^a' / '^b,c' (text_reconcile's convention), symbols ('X*', 'Day 1*', 'Phase £ (...)', a
literal '^'), and, when the letter is a footnote key, bare letters ('X c', 'Xa', 'Screening a', 'b AP00').
Footnotes come from the table's attached footnotes, the marker-led paragraphs that follow the table (until a
heading or another table; a footnote ending with ':' takes its list), and marker-led text in the notes column
or header notes (kept only for markers the table uses); styles '^a Text', 'a. Text', 'a.Text', 'a) Text',
'a: Text', 'a Text', '*Text', 'X^a = Text' are accepted. Markers left without a footnote are warned about.

  python soa.py show NAME|PATH [--json]     print the SoA(s) of a Docling JSON (public name, MAIN, or a path)
  python soa.py eval [NAME ...] [-v]        measure against the USDM keys (usdm_gold.soa_compare_study)
  python soa.py private                     P1..Pn: aggregate numbers only
"""
from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

# =========================================================================== text primitives

MARK_GLYPHS = "Xx✓✔√•●■☑⬤▪◼✕"
LINE_GLYPHS = "-–—‒―─━═=~_"
ARROW_GLYPHS = "→⟶⇒⇓↓⇩⬇←⟵⇐↔⟷⇔▶►"
SYMBOL_MARKERS = "*†‡§¶#‖£¥¤"
_SYM = re.escape(SYMBOL_MARKERS)
_EMPTY = {"", "-", "–", "—", "n/a", "na", "(-)", "0", "no", "none", "not applicable", "/"}

_MARKER_ITEM = rf"(?:[A-Za-z]{{1,2}}|\d{{1,2}}|[{_SYM}]{{1,3}})"
_CARET = re.compile(rf"\^({_MARKER_ITEM}(?:\s*,\s*{_MARKER_ITEM})*)(?![A-Za-z0-9])")
_TRAIL_SYM = re.compile(rf"(?<=\S)\s*([{_SYM}]{{1,3}})\s*$")
_LEAD_SYM = re.compile(rf"^([{_SYM}]{{1,3}})(?=\s*\S)")
_MID_SYM = re.compile(rf"(?<=\S)\s+([{_SYM}]{{1,3}})(?=\s+\S)")
_LETTERS = r"[a-z](?:\s*,\s*[a-z])*"
_TRAIL_LETTERS = re.compile(rf"(?<=\S)\s+({_LETTERS})\s*$")
_MID_LETTERS = re.compile(rf"(?<=\S)\s+([a-z](?:\s*,\s*[a-z])+|[b-hj-z])(?=\s+\S)")
_LEAD_LETTERS = re.compile(rf"^({_LETTERS})\s+(?=[A-Z0-9(])")
_GLUED_MARK = re.compile(rf"^([{re.escape(MARK_GLYPHS)}])({_LETTERS})$")

_WINDOW = re.compile(
    r"(?:±|\+\s*/\s*[-−–]|\+\s*or\s*-)\s*\d+(?:\.\d+)?\s*(?:%|[A-Za-z]+\b)?"           # ±3D, ± 3 days, +/-2, ±10%
    r"|[-−–]\s*\d+(?:\.\d+)?\s*[A-Za-z]*\s*/\s*\+\s*\d+(?:\.\d+)?\s*(?:[A-Za-z]+\b)?"  # -1/+3D
    r"|\(\s*[-−–+]\s*\d+[^)]*\)"                                                       # (-1 day), (+7)
    r"|(?<=[\dA-Za-z])\s*\+\s*\d+(?:\.\d+)?\s*(?:days?|d|weeks?|wks?|w|hours?|hrs?|h|min)?\b"  # ET+7, 379+7
    r"|[≤≥]\s*\d+\s*[A-Za-z]*"                                                         # ≤14D
    r"|\[\s*[-−+±]?\s*\d[^\]]*\]")                                                     # [-24h, SOI]
_WINDOW_WORD = re.compile(r"(?i)\b(?:window|allowed|tolerance)\b")
_UNIT_WORD = re.compile(r"(?i)\b(?:study\s+)?(days?|weeks?|wks?|months?|years?|cycles?|periods?|hours?|hrs?|"
                        r"visits?|time\s*points?)\b")
_TIMING_UNIT = re.compile(r"(?i)\b(?:days?|weeks?|wks?|months?|years?|cycles?|hours?|hrs?)\b")
_TIMING = re.compile(
    r"(?i)\b(?:days?|weeks?|wks?|months?|years?|cycles?|periods?|hours?|hrs?|mins?|minutes?)\s*[-−–]?\s*\.?\d"
    r"|\b[CDWPM]\s?[-−–]?\d+(?:\.\d+)?\b|\b[CP]\d+\s*D\s*[-−–]?\d+\b"
    r"|\b\d+(?:\.\d+)?\s*(?:days?|weeks?|months?|years?|hours?|hrs?|h|min)\b")
_BARE_TIME = re.compile(r"^[-−–]?\d*\.?\d+[A-Za-z]?(?:\s*(?:to|through|-|–|—|→)\s*[-−–]?\d*\.?\d+)?$")
_VISIT_HDR = re.compile(r"(?i)\b(visit|days?|weeks?|wk|cycles?|screening|baseline|period|follow|treatment|"
                        r"eot|eos|end of|wash|randomi[sz]ation|unscheduled|inpatient|outpatient|month|"
                        r"hours?|time ?points?)\b|\b[vdwc]-?\d")
_NOTES_HDR = re.compile(r"(?i)^\W*(?:notes?|comments?|remarks?|protocol sections?|see sections?|references?|"
                        r"sections?|details|instructions?)\b")


def collapse(s: str | None) -> str:
    return " ".join((s or "").split())


def _norm(s: str) -> str:
    """Comparison key: NFKC, casefold, markers and non-alphanumerics removed."""
    s = _CARET.sub("", unicodedata.normalize("NFKC", s or ""))
    s = re.sub(r"(?<![A-Za-z0-9])[-−–](?=\.?\d)", " minus", s)  # 'Day -1' != 'Day 1'
    return re.sub(r"[\W_]+", "", s.casefold())


_DASHES = str.maketrans({ch: "-" for ch in "‐‑‒–—−"})


def _words(s: str) -> list[str]:
    s = unicodedata.normalize("NFKC", (s or "").replace("^", " ")).casefold().translate(_DASHES)
    return re.findall(r"[a-z]+|(?<![0-9])-?\d+(?:\.\d+)?|\d+(?:\.\d+)?", s)


def mark_split(core: str) -> tuple[bool, str | None]:
    """(is a mark, qualifier) of a cell text with its footnote markers removed.

    'X' / 'X X' / 'X------X' / an arrow or line span -> (True, None); 'X (if necessary)' -> (True, 'if
    necessary'); 'Cycle 1-3' -> (False, 'Cycle 1-3'); empty or a dash -> (False, None)."""
    t = collapse(core)
    if t.casefold() in _EMPTY:
        return False, None
    body = t.replace(" ", "")
    if body and all(ch in MARK_GLYPHS or ch in LINE_GLYPHS or ch in ARROW_GLYPHS for ch in body):
        if any(ch in MARK_GLYPHS or ch in ARROW_GLYPHS for ch in body) or len(body) >= 3:
            return True, None   # 'X', 'X X', 'X-----X', an arrow or a line drawn across the visits
        return False, None      # '--'
    m = re.match(rf"^([{re.escape(MARK_GLYPHS)}])(?:\s*[:,;]\s*|\s+|(?=\())(.+)$", t)
    # a mark with a qualifier ('X (if necessary)'); a bullet before running text is a list, not a mark
    if m and len(t) <= 120 and (m.group(1) in "Xx✓✔√☑" or m.group(2).lstrip().startswith("(")):
        q = m.group(2).strip()
        if q.startswith("(") and q.endswith(")") and q.count("(") == 1:
            q = q[1:-1].strip()
        return True, q or None
    return False, t


def split_markers(text: str, keys: set[str] | frozenset = frozenset(), mark_letters: bool = True
                  ) -> tuple[str, list[str]]:
    """(text without footnote markers, markers in order). Caret markers ('^a', '^b,c'), trailing symbols
    ('X*') always; bare lowercase letters only when every letter is a known footnote key, except right after a
    lone mark ('Xa', 'X c', 'X d,k') when mark_letters."""
    t = collapse(text)
    out: list[str] = []

    def add(group: str) -> None:
        for m in re.split(r"\s*,\s*", group.strip()):
            if m and m not in out:
                out.append(m)

    def caret(m: re.Match) -> str:
        grp, pre = m.group(1), t[:m.start()]
        # exponents: '10^9/L', 'm^2', 'x10^12'
        if grp.isdigit() and (pre[-1:].isdigit() or re.search(r"(?:^|[\s(/])(?:c?m|mm|km|x|×)$", pre)):
            return m.group(0)
        add(grp)
        return " "

    t = collapse(_CARET.sub(caret, t))
    if re.search(r"\^(?=\s|$)", t):  # a literal caret used as a footnote symbol ('Section 5.6^')
        add("^")
        t = collapse(re.sub(r"\^(?=\s|$)", " ", t))
    m = _MID_SYM.search(t)
    while m:  # a symbol standing alone between words ('Continuation Phase £ (until ...)')
        add(m.group(1))
        t = collapse(t[:m.start()] + " " + t[m.end():])
        m = _MID_SYM.search(t)
    g = _GLUED_MARK.match(t.replace(" ", "")) if mark_letters and len(t) <= 8 else None
    if g:
        add(g.group(2))
        return g.group(1), out
    for _ in range(4):
        m = _TRAIL_SYM.search(t)
        if m:
            add(m.group(1))
            t = t[:m.start()].rstrip()
            continue
        m = _TRAIL_LETTERS.search(t)
        if m:
            letters = set(re.split(r"\s*,\s*", m.group(1)))
            core = t[:m.start()].strip()
            after_mark = mark_letters and core in MARK_GLYPHS and len(core) == 1
            if (keys and letters <= keys) or after_mark:
                add(m.group(1))
                t = core
                continue
        break
    m = _LEAD_SYM.match(t)
    if m and len(t) <= 40:
        add(m.group(1))
        t = t[m.end():].strip()
    if keys:
        m = _LEAD_LETTERS.match(t)
        if m and len(t) <= 30 and set(re.split(r"\s*,\s*", m.group(1))) <= keys:
            add(m.group(1))
            t = t[m.end():].strip()

        def mid(m: re.Match) -> str:
            letters = set(re.split(r"\s*,\s*", m.group(1)))
            if letters <= keys:
                add(m.group(1))
                return ""
            return m.group(0)

        t = _MID_LETTERS.sub(mid, t)
    t = re.sub(r"([(\[])\s+", r"\1", re.sub(r"\s+([,.;:)\]])", r"\1", collapse(t)))
    return t, out


_FN_PATTERNS = [
    re.compile(rf"^\^(?P<m>{_MARKER_ITEM}(?:\s*,\s*{_MARKER_ITEM})*)(?![A-Za-z0-9])\s*[.):]?\s*(?P<b>\S.*)$", re.S),
    re.compile(rf"^[{re.escape(MARK_GLYPHS)}]\^?(?P<m>[a-z]{{1,2}}|\d{{1,2}}|[{_SYM}]{{1,3}})\s*[=:]\s*(?P<b>\S.*)$", re.S),
    re.compile(r"^(?P<m>[a-z])\s*[.):]\s*(?P<b>\S.*)$", re.S),
    re.compile(r"^(?P<m>\^)\s+(?P<b>\S.*)$", re.S),   # a literal caret used as a footnote symbol
    re.compile(r"^\(?(?P<m>[a-z])\)\s*(?P<b>\S.*)$", re.S),
    re.compile(r"^(?P<m>[a-z])\s+(?P<b>[A-Z0-9(\[<>≤≥±\"'].*)$", re.S),
    re.compile(rf"^(?P<m>[{_SYM}]{{1,3}})\s*(?P<b>\S.*)$", re.S),
]
_ABBREV_LINE = re.compile(r"(?i)^\s*(?:abbreviations?|key|legend|note|notes)\s*[:.]")


_FN_LOOSE = re.compile(r"^(?P<m>[a-z])\s+(?P<b>\S.*)$", re.S)


def parse_footnotes(text: str, loose: bool = False) -> list[tuple[str, str]]:
    """Marker-led footnote definitions in one text: [(marker, body)]; consecutive letters inside one text
    ('a. First. b. Second.') are split. [] when the text does not start with a marker. `loose` also accepts
    'c lower-case text' (for texts inside the table, kept only when the table uses the marker)."""
    t = collapse(text)
    for pat in _FN_PATTERNS + ([_FN_LOOSE] if loose else []):
        m = pat.match(t)
        if not m:
            continue
        markers = [x for x in re.split(r"\s*,\s*", m.group("m")) if x]
        body = m.group("b").strip()
        out: list[tuple[str, str]] = []
        last = markers[-1]
        if len(last) == 1 and last.isalpha() and last.islower():
            # 'a. First. b. Second' / 'a First ^b Second': split at the next letters in sequence
            nxt = chr(ord(last) + 1)
            while nxt <= "z":
                sm = re.search(rf"(?:\s\^?|\^){nxt}(?:\s*[.):]\s*|\s+)(?=[A-Z0-9(≤≥<>±])", body)
                if not sm or sm.start() < 8:
                    break
                out += [(mk, body[:sm.start()].strip()) for mk in markers]
                markers, body = [nxt], body[sm.end():].strip()
                nxt = chr(ord(nxt) + 1)
        out += [(mk, body) for mk in markers]
        return out
    return []


def window_parts(text: str) -> tuple[list[str], str]:
    """(window expressions, the text without them) of a header text: 'D1 (±1)' -> (['±1'], 'D1'); a unit
    written after the window stays with the number before it: '90±7 days' -> (['±7 days'], '90 days')."""
    text = text or ""
    wins = [collapse(m.group(0)).strip(" ()") for m in _WINDOW.finditer(text)]

    def repl(m: re.Match) -> str:
        unit = re.search(r"([A-Za-z]{3,})\s*$", m.group(0))
        if unit and re.search(r"\d\s*$", text[:m.start()]) and _TIMING_UNIT.fullmatch(unit.group(1)):
            return " " + unit.group(1)
        return " "

    rest = _WINDOW.sub(repl, text)
    rest = re.sub(r"\(\s*\)|\[\s*\]", " ", rest)
    return [w for w in wins if w], collapse(rest).strip(" ,;")


_UNIT_NAMES = [("Day", r"days?|d"), ("Week", r"weeks?|wks?|w"), ("Month", r"months?|m"), ("Cycle", r"cycles?|c"),
               ("Hour", r"hours?|hrs?|h"), ("Year", r"years?|y")]


def _stub_units(stub: str) -> list[str]:
    """Timing units named by a header row's stub, in order ('Week [W] Study day ...' -> ['Week', 'Day'])."""
    out = []
    for m in re.finditer(r"(?i)\b(days?|weeks?|wks?|months?|cycles?|hours?|hrs?|years?)\b", stub or ""):
        u = next(n for n, pat in _UNIT_NAMES if re.fullmatch(pat, m.group(1).casefold()))
        if u not in out:
            out.append(u)
    return out


def _bare_numbers_with_unit(text: str, stub: str) -> str:
    """Numbers a header cell writes without a unit take the stub's unit that the cell does not already
    write ('W2 15' under 'Week [W] Study day ...' -> 'W2 Day 15')."""
    units = _stub_units(stub)
    if not units:
        return text
    explicit = {n for n, pat in _UNIT_NAMES
                if re.search(rf"(?i)\b(?:{pat})\s*[-−–]?\d|\b\d+(?:\.\d+)?\s*(?:{pat})\b", text)}
    free = [u for u in units if u not in explicit]
    if not free:
        return text
    unit = free[-1]
    return re.sub(r"(?<![A-Za-z\d.\-−–+/])([-−–]?\d+(?:\.\d+)?)(?![\d.]*\s*(?:[A-Za-z%]))",
                  lambda m: f"{unit} {m.group(1)}", text)


def _timing_of(part: str, leaf: bool, stub: str = "") -> str | None:
    """The timing expression of one header-path part ('Day 8', 'D-28 to D-15', 'C1D1', '3 to 14 days'); a
    group header's parenthesis ('Treatment Period (Each 28 Day Cycle)') is not a visit timing."""
    _, rest = window_parts(part)
    rest = _bare_numbers_with_unit(rest, stub)
    head = collapse(re.sub(r"\([^)]*\)", " ", rest))
    if _TIMING.search(head) or _BARE_TIME.match(head):
        return head
    if leaf:
        for inner in re.findall(r"\(([^)]*)\)", rest):
            if _TIMING.search(inner):
                return collapse(inner)
    return None


def _unit_of(stub: str) -> str | None:
    hits = _UNIT_WORD.findall(stub or "")
    if not hits:
        return None
    u = hits[-1].casefold()
    u = re.sub(r"\s+", " ", u)
    if u.startswith("time"):
        return None
    base = {"wk": "week", "wks": "week", "hr": "hour", "hrs": "hour"}.get(u, u.rstrip("s") if u != "days" else "day")
    return base.capitalize()


def _unit_word(part: str, unit: str | None) -> str | None:
    """The word _with_unit puts before a bare number ('Day'; 'Days' for a range), None when it adds nothing."""
    core = split_markers(part, frozenset(), mark_letters=False)[0]
    if unit and _BARE_TIME.match(core):
        plural = bool(re.search(r"\d\s*(?:to|through|-|–|—|→)\s*[-−–]?\d", core)) and unit != "Visit"
        return f"{unit}{'s' if plural else ''}"
    return None


def _with_unit(part: str, unit: str | None) -> str:
    """'-21' under a 'Days' stub -> 'Day -21'; '2-3' -> 'Days 2-3'; '4^g' under 'Visit number' -> 'Visit 4^g'."""
    word = _unit_word(part, unit)
    if word is None:
        return part
    part = re.sub(r"(?<![\d.])\.(?=\d)", "0.", part)  # '-.3' -> '-0.3'
    return f"{word} {part}"


# =========================================================================== one table -> part

@dataclass
class _Part:
    table: Any
    ref: str
    pages: list[int]
    ncols: int
    visits: list[dict]            # col, parts (raw header path), meta, hcells (one header cell per part), window_raw
    activities: list[dict]        # row, label_raw, section_raw, notes_raw, cells {col: raw}, blank
    header_rows: list[int]
    label_cols: list[int]
    notes_cols: list[int]
    header_notes: list[str]
    header_texts: list[str]       # every raw header text (for markers)
    notes_texts: list[str]        # notes-column texts (for marker-led footnotes)
    sections: list[str]
    spanning_sections: bool       # the table writes sections as rows across it
    has_header: bool = True
    caption: str = ""
    warnings: list[str] = field(default_factory=list)


def _cell_text(cell) -> str:
    return (getattr(cell, "text", "") or "").strip()


def _core(text: str) -> str:
    """Cell text with markers removed for detection (no footnote keys known yet)."""
    return split_markers(text, frozenset(), True)[0]


def _is_mark(text: str) -> bool:
    return bool(text) and mark_split(_core(text))[0]


def table_stats(t) -> dict:
    """Detection numbers of one table: columns, marks among the non-empty body cells (each cell once, spans
    not repeated), whether the rows above the first mark hold visit-like tokens."""
    cells = list(t.data.table_cells)
    body = [c for c in cells if c.start_col_offset_idx >= 1 and not c.column_header and _cell_text(c)]
    marks = [c for c in body if _is_mark(_cell_text(c))]
    first_mark = min((c.start_row_offset_idx for c in marks), default=t.data.num_rows)
    head = " ".join(_cell_text(c) for c in cells if c.start_row_offset_idx < max(1, min(first_mark, 6)))
    return {"ncols": t.data.num_cols, "nonempty": len(body), "marks": len(marks),
            "share": len(marks) / len(body) if body else 0.0, "visit_header": bool(_VISIT_HDR.search(head))}


def is_soa_table(t, role_soa: bool = False) -> bool:
    """A mark-heavy grid with a visit-like header; inside a section whose role is "soa", a weaker grid."""
    if getattr(t.label, "value", str(t.label)) != "table" or not t.data.num_rows or t.data.num_cols < 3:
        return False
    s = table_stats(t)
    if s["ncols"] >= 6 and s["marks"] >= 4 and s["share"] >= 0.15 and s["visit_header"]:
        return True
    return role_soa and s["ncols"] >= 4 and s["marks"] >= 3 and s["share"] >= 0.03


def _row_is_section(row: list, texts: list[str], ncols: int) -> bool:
    """One cell across the table, or one text repeated over (nearly) every column."""
    filled = [(c, x) for c, x in zip(row, texts) if x]
    if not filled or ncols < 2:
        return False
    if len({id(c) for c, _ in filled}) == 1:
        c = filled[0][0]
        return c.start_col_offset_idx == 0 and c.end_col_offset_idx >= ncols
    vals = [x for _, x in filled]
    return len(set(vals)) == 1 and len(vals) >= ncols - 1


def _parse_table(t, doc) -> _Part | None:
    grid = t.data.grid
    if not grid:
        return None
    nrows, ncols = len(grid), t.data.num_cols
    raw = [[_cell_text(c) for c in row] for row in grid]
    texts = [[collapse(x) for x in row] for row in raw]
    pages = sorted({p.page_no for p in t.prov})
    warnings: list[str] = []
    section_row = [_row_is_section(grid[r], texts[r], ncols) for r in range(nrows)]

    def uniq(r: int, cols) -> list[tuple[int, Any]]:
        out, seen = [], set()
        for c in cols:
            cell = grid[r][c]
            if id(cell) not in seen:
                seen.add(id(cell))
                out.append((c, cell))
        return out

    first_mark = next((r for r in range(nrows) if any(_is_mark(texts[r][c]) for c, cell in uniq(r, range(1, ncols))
                                                      if cell.start_row_offset_idx == r)), nrows)
    flagged = [any(grid[r][c].column_header for c in range(1, ncols)) for r in range(nrows)]

    def header_like(r: int) -> bool:
        vals = [texts[r][c] for c, _ in uniq(r, range(1, ncols)) if texts[r][c] and texts[r][c] != texts[r][0]]
        if len(vals) < 2 or section_row[r]:
            return False
        stub = texts[r][0]
        if _VISIT_HDR.search(stub) or _WINDOW_WORD.search(stub):
            return True
        dig = sum(bool(re.search(r"\d", v) or _VISIT_HDR.search(v)) for v in vals)
        return dig >= 0.5 * len(vals) and all(len(v) <= 60 for v in vals)

    header_rows: list[int] = []
    for r in range(min(first_mark, nrows)):
        if section_row[r] or not (flagged[r] or header_like(r)):
            break
        header_rows.append(r)
    n_head = len(header_rows)
    has_header = bool(header_rows)
    body_rows = list(range(n_head, nrows))

    # ---- label columns: column 0, then non-mark text columns (or an empty stub column) without digits above
    label_cols = [0]
    for c in range(1, min(3, ncols - 1)):
        hdr_parts = [texts[r][c] for r in header_rows if grid[r][c] is not grid[r][0] and texts[r][c]]
        hdr = " ".join(hdr_parts)
        if re.search(r"\d", hdr):
            break
        vals = [texts[r][c] for r in body_rows if not section_row[r]]
        ne = [v for v in vals if v]
        if not ne:
            # an empty stub column holding the header rows' unit words ('VISIT' / 'WEEK'); else a layout gap
            if (hdr_parts and all(_UNIT_WORD.fullmatch(x) for x in hdr_parts)) or not has_header:
                label_cols.append(c)
                continue
            break
        if _VISIT_HDR.search(hdr):
            break
        if sum(_is_mark(v) for v in ne) / len(ne) < 0.15 and len(ne) >= 0.3 * len(vals):
            label_cols.append(c)
        else:
            break

    def stub_cells(r: int) -> list[tuple[int, str]]:
        """(grid column, text) of the label-column cells that start in row r, a repeated text once."""
        out: list[tuple[int, str]] = []
        for lc in label_cols:
            cell = grid[r][lc]
            if cell.start_row_offset_idx == r and texts[r][lc] and texts[r][lc] not in [x for _, x in out]:
                out.append((cell.start_col_offset_idx, texts[r][lc]))
        return out

    def stub_of(r: int) -> str:
        return " ".join(x for _, x in stub_cells(r))

    def unit_from(r: int) -> dict | None:
        """The stub cell of row r that holds the unit word _unit_of(stub_of(r)) takes (the last one named)."""
        cells = stub_cells(r)
        hits = list(_UNIT_WORD.finditer(" ".join(x for _, x in cells)))
        if not hits:
            return None
        pos, start = hits[-1].start(1), 0
        for col, x in cells:
            if pos < start + len(x):
                return {"text": x, "column": col}
            start += len(x) + 1
        return None

    # ---- the visit-window row(s) of the header block
    def is_window_row(r: int) -> bool:
        stub = stub_of(r) or texts[r][0]
        if _WINDOW_WORD.search(stub):
            return not re.search(r"(?i)\b(?:study\s+)?(?:day|week|month)s?\b", stub)
        if r == header_rows[0]:
            return False
        vals = [texts[r][c] for c, cell in uniq(r, range(1, ncols)) if c not in label_cols and texts[r][c]
                and cell.start_row_offset_idx == r]
        if len(vals) < 2:
            return False
        win = [v for v in vals if _WINDOW.search(v)]
        resid = [v for v in vals if re.search(r"[A-Za-z0-9]{2,}", window_parts(v)[1])]
        return len(win) >= 0.6 * len(vals) and len(resid) <= 0.34 * len(vals)

    window_rows = [r for r in header_rows if is_window_row(r)]
    path_rows = [r for r in header_rows if r not in window_rows]

    # ---- header path per column (a cell's text after its first newline is a header note)
    col_parts: dict[int, list[str]] = {}
    col_meta: dict[int, list[tuple[bool, str]]] = {}   # per part: (a group header over >= 2 columns, row stub)
    col_hcells: dict[int, list[dict]] = {}   # per part: the header cell it was built from (where, unit added)
    col_notes: dict[int, list[str]] = {}
    header_texts: list[str] = []
    for c in range(ncols):
        parts, meta, hcells, notes, seen = [], [], [], [], set()
        for r in path_rows:
            cell = grid[r][c]
            if id(cell) in seen:
                continue
            seen.add(id(cell))
            if cell.start_col_offset_idx == 0 and cell.end_col_offset_idx >= ncols:
                if c == 0 and texts[r][c]:
                    header_texts.append(texts[r][c])  # a title across the table
                continue
            if c not in label_cols and cell.start_col_offset_idx in label_cols:
                continue  # the stub cell spanning into this column
            blocks = [collapse(b) for b in raw[r][c].split("\n") if b.strip()]
            if not blocks:
                continue
            if c > 0 or cell.start_col_offset_idx == c:
                header_texts.extend(blocks)
            name = blocks[0]
            if len(blocks) > 1:
                notes.append(" ".join(blocks[1:]))
            if not re.search(r"[A-Za-z0-9]", name):
                continue
            unit = None
            if c not in label_cols:
                stub_unit = _unit_of(stub_of(r))
                unit, name = _unit_word(name, stub_unit), _with_unit(name, stub_unit)
            if parts and _norm(parts[-1]) == _norm(name):
                continue
            parts.append(name)
            meta.append((cell.end_col_offset_idx - cell.start_col_offset_idx >= 2, stub_of(r)))
            hcells.append({"text": texts[r][c], "table_id": t.self_ref, "row": cell.start_row_offset_idx,
                           "column": cell.start_col_offset_idx, "unit": unit,
                           "unit_from": unit_from(r) if unit else None})
        col_parts[c], col_meta[c], col_hcells[c], col_notes[c] = parts, meta, hcells, notes

    # ---- notes columns: a header keyword, or long text in the last column
    def long_text(c: int) -> bool:
        vals = [texts[r][c] for r in body_rows if texts[r][c] and grid[r][c].start_col_offset_idx == c
                and grid[r][c].end_col_offset_idx == c + 1]
        if len(vals) < 2:
            return False
        return sum(len(v) > 25 for v in vals) / len(vals) > 0.5 and sum(_is_mark(v) for v in vals) / len(vals) < 0.1

    notes_cols = []
    for c in range(1, ncols):
        if c in label_cols:
            continue
        if any(_NOTES_HDR.match(_core(x)) for x in col_parts[c]) or \
                (c == ncols - 1 and not _TIMING.search(" ".join(col_parts[c])) and long_text(c)):
            notes_cols.append(c)

    def col_empty(c: int) -> bool:
        return not col_parts[c] and not any(texts[r][c] and grid[r][c].start_col_offset_idx == c
                                            for r in range(nrows))

    visit_cols = [c for c in range(1, ncols) if c not in label_cols and c not in notes_cols and not col_empty(c)]
    header_notes = list(dict.fromkeys(n for c in range(ncols) for n in col_notes.get(c, [])))

    # adjacent columns under one and the same header cell chain whose body entries never conflict are one
    # visit that the grid split in two
    def same_header(a: int, b: int) -> bool:
        return bool(path_rows) and any(texts[r][a] for r in path_rows) and \
            all(grid[r][a] is grid[r][b] or not (texts[r][a] or texts[r][b]) for r in path_rows)

    def complementary(a: int, b: int) -> bool:
        for r in body_rows:
            x, y = texts[r][a], texts[r][b]
            if x and y and grid[r][a] is not grid[r][b] and x != y and \
                    x.casefold() not in _EMPTY and y.casefold() not in _EMPTY:
                return False
        return True

    col_groups: list[list[int]] = []
    for c in visit_cols:
        if col_groups and col_groups[-1][-1] == c - 1 and same_header(col_groups[-1][0], c) and \
                all(complementary(x, c) for x in col_groups[-1]):
            col_groups[-1].append(c)
        else:
            col_groups.append([c])

    visits = []
    for cols in col_groups:
        c = cols[0]
        wins = []
        for r in window_rows:
            for cc in cols:
                cell, v = grid[r][cc], texts[r][cc]
                if v and cell.start_row_offset_idx == r and v.casefold() not in _EMPTY and v not in wins:
                    wins.append(v)
        header_texts.extend(wins)
        visits.append({"col": c, "cols": cols, "parts": col_parts[c], "meta": col_meta[c], "hcells": col_hcells[c],
                       "window_raw": " ".join(wins) or None})

    # ---- body rows: repeated headers dropped, sections, activities
    head_keys = [tuple(_norm(x) for x in texts[r]) for r in header_rows]

    def repeats_header(r: int) -> bool:
        key = tuple(_norm(x) for x in texts[r])
        for h in head_keys:
            filled = sum(1 for x in h if x)
            if key == h or (filled >= 3 and sum(a == b and a != "" for a, b in zip(key, h)) >= 0.8 * filled):
                return True
        return False

    def label_of(r: int) -> str:
        parts = []
        for lc in label_cols:
            x = texts[r][lc]
            if x and x not in parts:
                parts.append(x)
        return " ".join(parts)

    spanning = any(section_row[r] for r in body_rows)
    activities, sections, section = [], [], None
    for r in body_rows:
        if not any(texts[r]) or repeats_header(r):
            continue
        if section_row[r]:
            section = texts[r][0] or next((x for x in texts[r] if x), "")
            sections.append(section)
            continue
        cells = {}
        for vis in visits:
            vals, seen_cells = [], set()
            for c in vis["cols"]:
                v = texts[r][c]
                if v and v.casefold() not in _EMPTY and id(grid[r][c]) not in seen_cells and v not in vals:
                    seen_cells.add(id(grid[r][c]))
                    vals.append(v)
            if vals:
                cells[vis["col"]] = " ".join(vals)
        notes = list(dict.fromkeys(texts[r][c] for c in notes_cols if texts[r][c]))
        label = label_of(r)
        if not label and (cells or notes):
            prev = activities[-1] if activities else None
            if prev and not (set(prev["cells"]) & set(cells)):
                prev["cells"].update(cells)
                prev["notes_raw"] = "; ".join(x for x in (prev["notes_raw"], *notes) if x) or None
                prev["blank"] = False
                warnings.append(f"unlabelled row {r} joined to the row above")
                continue
            warnings.append(f"unlabelled row {r}")
        activities.append({"row": r, "label_raw": label, "section_raw": section, "cells": cells,
                           "notes_raw": "; ".join(notes) or None, "blank": not cells and not notes})
    notes_texts = [texts[r][c] for c in notes_cols for r in body_rows if texts[r][c]]
    notes_texts += [n for c in notes_cols for n in col_notes.get(c, []) + col_parts[c][1:]]
    try:
        caption = t.caption_text(doc) or ""
    except Exception:
        caption = ""
    return _Part(t, t.self_ref, pages, ncols, visits, activities, header_rows, label_cols, notes_cols,
                 header_notes, header_texts, notes_texts, sections, spanning, has_header, caption, warnings)


# =========================================================================== footnotes around a table

def _following_footnote_texts(doc, tables: list, group_refs: set[str], max_items: int = 60) -> list[str]:
    """Marker-led texts after each table of the group in reading order, until a heading, another table or a
    run of other items; a footnote ending with ':' takes the lines that follow it (its list)."""
    from docling_core.types.doc import SectionHeaderItem, TableItem, TitleItem

    order = [it for it, _ in doc.iterate_items(with_groups=False)]
    pos = {it.self_ref: i for i, it in enumerate(order)}
    out: list[str] = []
    seen: set[str] = set()
    for t in tables:
        i = pos.get(t.self_ref)
        if i is None:
            continue
        found, misses, cont = False, 0, False
        for it in order[i + 1:i + 1 + max_items]:
            if isinstance(it, TableItem):
                if it.self_ref in group_refs:
                    continue
                break
            if isinstance(it, (SectionHeaderItem, TitleItem)):
                break
            txt = collapse(getattr(it, "text", "") or "")
            if not txt or it.self_ref in seen:
                continue
            if parse_footnotes(txt):
                seen.add(it.self_ref)
                out.append(txt)
                found, misses, cont = True, 0, txt.endswith(":")
                continue
            if cont and out and not _ABBREV_LINE.match(txt):
                out[-1] = f"{out[-1]} {txt}"
                seen.add(it.self_ref)
                continue
            cont = False
            misses += 1
            if misses >= (10 if found else 6):
                break
    return out


def _table_texts(p: _Part, include_notes: bool = True) -> list[str]:
    out = list(p.header_texts) + list(p.sections)
    for a in p.activities:
        out.append(a["label_raw"])
        out.extend(a["cells"].values())
        if include_notes and a["notes_raw"]:
            out.append(a["notes_raw"])
    return out


def _collect_footnotes(doc, parts: list[_Part], group_refs: set[str]) -> dict[str, str]:
    fn: dict[str, str] = {}

    def add(text: str) -> None:
        for m, body in parse_footnotes(text):
            if m not in fn and body:
                fn[m] = body

    for p in parts:
        for ref in getattr(p.table, "footnotes", []) or []:
            try:
                add(ref.resolve(doc).text or "")
            except Exception:
                continue
    for txt in _following_footnote_texts(doc, [p.table for p in parts], group_refs):
        add(txt)
    # marker-led definitions inside the table (notes column, header notes): only for markers the table uses
    cand: dict[str, str] = {}
    for p in parts:
        for txt in p.notes_texts + p.header_notes:
            for m, body in parse_footnotes(txt, loose=True):
                if m not in fn and m not in cand and len(body) >= 3:
                    cand[m] = body
    if cand:
        keys = frozenset(cand) | frozenset(fn)
        used: set[str] = set()
        for p in parts:
            for txt in _table_texts(p, include_notes=False):
                used.update(split_markers(txt, keys)[1])
        fn.update({m: body for m, body in cand.items() if m in used})
    return fn


# =========================================================================== grouping parts into SoAs

def _visit_key(v: dict) -> str:
    return "|".join(k for k in (_norm(_core(x)) for x in v["parts"]) if k)


def _act_key(a: dict) -> str:
    return _norm(_core(a["label_raw"]))


def _visit_overlap(a: _Part, b: _Part) -> float:
    ka = {_visit_key(v) for v in a.visits if v["parts"]}
    kb = [_visit_key(v) for v in b.visits if v["parts"]]
    if len(kb) < 2 or not ka:
        return 0.0
    return sum(k in ka for k in kb) / len(kb)


def _activity_overlap(a: _Part, b: _Part) -> float:
    ka = Counter(_act_key(x) for x in a.activities if _act_key(x))
    kb = [_act_key(x) for x in b.activities if _act_key(x)]
    if len(kb) < 3:
        return 0.0
    return sum((Counter(kb) & ka).values()) / len(kb)


def _continues(prev: _Part, nxt: _Part, chained: bool) -> str | None:
    """How nxt continues prev: 'rows' (more activities under the same visits), 'cols' (more visits for the
    same activities) or None. Tables on the same or the next page only; a new caption starts a new table."""
    if not prev.pages or not nxt.pages or nxt.pages[0] - prev.pages[-1] not in (0, 1):
        return None
    if nxt.caption and _norm(nxt.caption) != _norm(prev.caption) and not chained:
        return None
    vo = _visit_overlap(prev, nxt)
    if vo >= 0.8:
        return "rows"
    if not nxt.has_header and nxt.ncols == prev.ncols:
        return "rows"
    if vo < 0.5 and _activity_overlap(prev, nxt) >= 0.6:
        return "cols"
    return "rows" if chained else None


def _chains(doc) -> list[list[str]]:
    try:
        from table_merge import continuation_chains
        return continuation_chains(doc)
    except Exception:
        return []


# =========================================================================== assembly

def _ident(lst: list, obj) -> int:
    return next(i for i, x in enumerate(lst) if x is obj)


def _split(text: str | None, keys) -> tuple[str, list[str]]:
    return split_markers(text, keys, mark_letters=False) if text else ("", [])


def _build(doc, parts: list[_Part], modes: list[str], index) -> dict:
    fn = _collect_footnotes(doc, parts, {p.ref for p in parts})
    keys = frozenset(fn)
    warnings: list[str] = []

    # ---- visits: 'rows' parts map onto the visits so far by header path (a column missing on one page is
    # inserted after the previous matched visit; a header-less page maps by column); 'cols' parts append
    vlist: list[dict] = []
    for pi, p in enumerate(parts):
        mode, anchor = modes[pi], -1
        prev_by_col = {col: rec for rec in vlist for spi, col in rec["src"] if spi == pi - 1}
        for v in p.visits:
            rec = None
            if pi and mode == "rows":
                key = _visit_key(v)
                if key:
                    cands = [w for w in vlist if w["key"] == key]
                    rec = next((w for w in cands if _ident(vlist, w) > anchor), cands[0] if cands else None)
                elif not p.has_header:
                    rec = prev_by_col.get(v["col"])
            if rec is None:
                rec = {"key": _visit_key(v), "v": v, "part": pi, "src": [], "window_raw": v["window_raw"]}
                vlist.insert(anchor + 1 if (pi and mode == "rows" and anchor >= 0) else len(vlist), rec)
            elif not rec["window_raw"] and v["window_raw"]:
                rec["window_raw"] = v["window_raw"]
            rec["src"].append((pi, v["col"]))
            anchor = _ident(vlist, rec)
    vmap = {(spi, col): i for i, w in enumerate(vlist) for spi, col in w["src"]}

    visits_out, unnamed = [], []
    for i, w in enumerate(vlist):
        v, p = w["v"], parts[w["part"]]
        path, meta, hcells, markers = [], [], [], []
        for x, mt, hc in zip(v["parts"], v["meta"], v["hcells"]):
            clean, ms = split_markers(x, keys, mark_letters=False)
            markers += [m for m in ms if m not in markers]
            if clean and (not path or _norm(path[-1]) != _norm(clean)):
                path.append(clean)
                meta.append(mt)
                hcells.append(hc)
        label = path[-1] if path else ""
        if not label:
            unnamed.append(f"{p.ref}#c{v['col']}")
        if w["window_raw"]:
            window, ms = split_markers(w["window_raw"], keys, mark_letters=False)
            markers += [m for m in ms if m not in markers]
        else:
            wins: list[str] = []
            for x in path:
                wins += [y for y in window_parts(x)[0] if y.replace(" ", "") not in {w.replace(" ", "") for w in wins}]
            window = "; ".join(wins)
        timing_parts: list[str] = []
        for k, (x, (_, stub)) in enumerate(zip(path, meta)):
            tm = _timing_of(x, leaf=(k == len(path) - 1), stub=stub)
            if tm and tm not in timing_parts:
                timing_parts.append(tm)
        timing = ", ".join(timing_parts) or None
        if timing is None and window and _TIMING.search(window) and not _WINDOW.search(window):
            timing = window
        epoch = next((x for x, (grp, _) in zip(path[:-1], meta[:-1]) if grp), None)
        visits_out.append({"id": f"V{i + 1}", "label": label, "header_path": path, "window": window or None,
                           "timing": timing, "column": v["col"], "table_id": p.ref, "epoch_hint": epoch,
                           "footnote_markers": markers, "header_cells": hcells,
                           "header_text": hcells[-1]["text"] if hcells else None,
                           "label_composed": bool(hcells and hcells[-1]["unit"])})
    if unnamed:
        warnings.append(f"unnamed visit columns: {len(unnamed)} ({', '.join(unnamed[:8])})")
    dups = [k for k, n in Counter(w["key"] for w in vlist if w["key"]).items() if n > 1]
    if dups:
        warnings.append(f"duplicate visit headers: {len(dups)}")

    # ---- activities: 'cols' parts map onto the rows so far by label (in order); others append
    alist: list[dict] = []
    for pi, p in enumerate(parts):
        cursor, used = 0, set()
        for a in p.activities:
            rec = None
            if pi and modes[pi] == "cols":
                key = _act_key(a)
                cands = [k for k, x in enumerate(alist) if x["key"] == key and k not in used]
                k = next((k for k in cands if k >= cursor), cands[0] if cands else None)
                rec = alist[k] if k is not None else None
            if rec is None:
                rec = {"key": _act_key(a), "a": a, "src": [], "notes": None, "blank": True, "part": pi}
                alist.append(rec)
            rec["src"].append((pi, a))
            rec["notes"] = rec["notes"] or a["notes_raw"]
            rec["blank"] = rec["blank"] and a["blank"]
            k = _ident(alist, rec)
            used.add(k)
            cursor = k + 1

    # ---- label-only rows: in tables without sections across the table, a row with no entry anywhere that is
    # followed by a filled row is a section heading for the rows below it
    any_spanning = any(p.spanning_sections for p in parts)
    kept, cur, section_rows = [], None, []
    for k, rec in enumerate(alist):
        a = rec["a"]
        if not any_spanning:
            if rec["blank"] and k + 1 < len(alist) and not alist[k + 1]["blank"]:
                cur = a["label_raw"]
                section_rows += [{"table_id": parts[pi].ref, "row": pa["row"]} for pi, pa in rec["src"]]
                continue
            rec["section_raw"] = cur
        else:
            rec["section_raw"] = a["section_raw"]
        kept.append(rec)
    alist = kept

    def resolved(m: str, notes: str | None) -> bool:
        return m in fn or (m[0] in SYMBOL_MARKERS and bool(notes) and m in notes)

    activities_out, cells_out, unresolved = [], [], set()
    for i, rec in enumerate(alist):
        a = rec["a"]
        label, ms = _split(a["label_raw"], keys)
        section, sms = _split(rec["section_raw"], keys)
        markers = list(dict.fromkeys(ms + sms))
        notes = rec["notes"]
        activities_out.append({"id": f"A{i + 1}", "label": label, "section": section or None, "notes": notes,
                               "footnote_markers": markers, "row": a["row"], "table_id": parts[rec["part"]].ref})
        unresolved.update(m for m in markers if not resolved(m, notes))
        seen = set()
        for pi, pa in rec["src"]:
            for col, raw in pa["cells"].items():
                vi = vmap.get((pi, col))
                if vi is None or vi in seen:
                    continue
                core, ms = split_markers(raw, keys, mark_letters=True)
                mark, qual = mark_split(core)
                if not mark and qual is None:
                    continue
                if qual:
                    qual, ms2 = split_markers(qual, keys, mark_letters=False)
                    ms = list(dict.fromkeys(ms + ms2))
                seen.add(vi)
                cells_out.append({"activity": f"A{i + 1}", "visit": f"V{vi + 1}", "mark": mark, "text": raw,
                                  "qualifier": qual or None, "footnote_markers": ms})
                unresolved.update(m for m in ms if not resolved(m, notes))
    for v in visits_out:
        unresolved.update(m for m in v["footnote_markers"] if m not in fn)
    cells_out.sort(key=lambda c: (int(c["activity"][1:]), int(c["visit"][1:])))
    if unresolved:
        warnings.append(f"unresolved footnote markers: {', '.join(sorted(unresolved))}")
    for p in parts:
        warnings += [f"{p.ref}: {w}" for w in p.warnings]
    section_id = None
    if index is not None:
        try:
            section_id = index.section_of(parts[0].ref)
        except Exception:
            section_id = None
    return {"table_id": parts[0].ref, "table_ids": [p.ref for p in parts],
            "caption": next((p.caption for p in parts if p.caption), None),
            "section_id": section_id, "pages": sorted({pg for p in parts for pg in p.pages}),
            "visits": visits_out, "activities": activities_out, "cells": cells_out, "footnotes": fn,
            "header_notes": list(dict.fromkeys(n for p in parts for n in p.header_notes)),
            "section_rows": section_rows, "warnings": warnings}


def extract_soa(doc, index=None) -> list[dict]:
    """Every Schedule of Activities in `doc`, one dict per SoA (see the module docstring for the contract).
    `index` (structure.SectionIndex, optional) lets tables in "soa" sections pass with a weaker mark grid and
    fills section_id."""
    from docling_core.types.doc import TableItem

    soa_sections: set[str] = set()
    if index is not None:
        try:
            soa_sections = {s.section_id for s in index.with_role("soa")}
        except Exception:
            soa_sections = set()

    def in_soa_section(ref: str) -> bool:
        try:
            return bool(soa_sections) and index.section_of(ref) in soa_sections
        except Exception:
            return False

    tables = [it for it, _ in doc.iterate_items(with_groups=False) if isinstance(it, TableItem)]
    chained = {pair for ch in _chains(doc) for pair in zip(ch, ch[1:])}
    cache: dict[str, _Part | None] = {}

    def part_of(t) -> _Part | None:
        if t.self_ref not in cache:
            try:
                cache[t.self_ref] = _parse_table(t, doc)
            except Exception:  # noqa: BLE001  one odd table must not stop the extraction
                cache[t.self_ref] = None
        return cache[t.self_ref]

    out, i = [], 0
    while i < len(tables):
        t = tables[i]
        if not is_soa_table(t, in_soa_section(t.self_ref)):
            i += 1
            continue
        p = part_of(t)
        if p is None or not p.visits or not p.activities:
            i += 1
            continue
        parts, modes, j = [p], ["first"], i + 1
        while j < len(tables):
            nt = tables[j]
            q = part_of(nt)
            if q is None or not q.activities:
                break
            link = (parts[-1].ref, nt.self_ref) in chained
            mode = _continues(parts[-1], q, link)
            if mode is None or not (link or mode == "rows" or is_soa_table(nt, in_soa_section(nt.self_ref))):
                break
            parts.append(q)
            modes.append(mode)
            j += 1
        try:
            out.append(_build(doc, parts, modes, index))
        except Exception as exc:  # noqa: BLE001  fail soft: the caller still sees the table and why it is empty
            out.append({"table_id": parts[0].ref, "table_ids": [q.ref for q in parts], "caption": parts[0].caption or None,
                        "section_id": None, "pages": sorted({pg for q in parts for pg in q.pages}), "visits": [],
                        "activities": [], "cells": [], "footnotes": {}, "header_notes": [], "section_rows": [],
                        "warnings": [f"extraction failed: {type(exc).__name__}"]})
        i = j
    return out


# =========================================================================== lookup and display

def _match_tokens(needle: list[str], hay: list[str], min_prefix: int = 3) -> bool:
    """Every needle token in order inside hay. Words also match as prefixes ('exam' ~ 'examination'); a
    single letter before a number is a compact unit ('C1D8' ~ 'Cycle 1 Day 8', 'W4' ~ 'Week 4') whose number
    must follow it directly; numbers must be equal (signs included)."""
    j, k = 0, 0
    while k < len(needle):
        x = needle[k]
        compact = len(x) <= 2 and x.isalpha() and k + 1 < len(needle) and needle[k + 1][:1] in "-0123456789"
        found = False
        while j < len(hay):
            y = hay[j]
            j += 1
            if compact:
                if y.startswith(x) and j < len(hay) and hay[j] == needle[k + 1]:
                    j += 1
                    k += 1
                    found = True
                    break
                continue
            if x == y or (x[:1].isalpha() and len(x) >= min_prefix and y.startswith(x)):
                found = True
                break
        if not found:
            return False
        k += 1
    return True


def _initials(words: list[str]) -> str:
    return "".join(w[0] for w in words if w[:1].isalpha())


def _text_match(needle: str, *hays: str) -> bool:
    """needle inside one of hays: words in order (prefix tolerant), or an abbreviation of it ('FU' ~
    'Follow-up', 'end of treatment' ~ 'EOT')."""
    nw = _words(needle)
    if not nw:
        return False
    compact = "".join(nw)
    for h in hays:
        hw = _words(h)
        if not hw:
            continue
        if _match_tokens(nw, hw):
            return True
        if len(nw) == 1 and len(compact) >= 2 and compact.isalpha() and _initials(hw) == compact:
            return True
        if len(nw) >= 2 and len(_initials(nw)) >= 2 and _initials(nw) in hw:
            return True
    return False


def _visit_tier(v: dict, needle: str) -> int | None:
    """How well a visit matches: 0 its label, 1 one header-path part (or its timing), 2 the whole path,
    3 the numbers ('C1D8' ~ a 'Day 8' column of a cycle table); None no match. Windows are ignored."""
    parts = [window_parts(p)[1] for p in v["header_path"]]
    nw = _words(needle)
    if nw and nw == _words(window_parts(v["label"])[1]):
        return 0
    if any(_text_match(needle, p) for p in parts + [v.get("timing") or ""]):
        return 1
    if _text_match(needle, " ".join(parts)):
        return 2
    if _visit_matches_numeric(v, needle):
        return 3
    return None


def _visit_matches_numeric(v: dict, needle: str) -> bool:
    try:
        from usdm_gold import time_values
    except Exception:
        return False
    nt = {u: s for u, s in time_values(needle).items() if u}
    vt = {u: s for u, s in time_values(v.get("timing") or window_parts(v["label"])[1]).items() if u}
    shared = [u for u in nt if u in vt]
    return bool(shared) and all(nt[u] & vt[u] for u in shared) and ("day" in shared or set(nt) <= set(vt))


def soa_lookup(soas: list[dict], visit: str | None = None, activity: str | None = None) -> list[dict]:
    """Cells of the SoA(s) at matching visits and/or activities, case and abbreviation tolerant ('C1D8',
    'Day -1', 'EOT', 'end of treatment', 'ecg'). Per SoA the best-matching visits are kept (label, then one
    header part, then the whole header path, then the numbers). Each hit: table_id, visit_id, visit,
    visit_path, window, timing, activity_id, activity, section, notes, mark, text, qualifier,
    footnote_markers (cell + activity), footnotes (marker -> text)."""
    if isinstance(soas, dict):
        soas = [soas]
    out = []
    for s in soas:
        vs = s["visits"]
        if visit:
            tiers = {v["id"]: t for v in vs if (t := _visit_tier(v, visit)) is not None}
            best = min(tiers.values(), default=None)
            vids = {k for k, t in tiers.items() if t == best}
        else:
            vids = {v["id"] for v in vs}
        aids = {a["id"] for a in s["activities"] if (not activity or _text_match(activity, a["label"]))}
        if activity and not aids:
            aids = {a["id"] for a in s["activities"] if _text_match(activity, a["section"] or "")}
        vby = {v["id"]: v for v in vs}
        aby = {a["id"]: a for a in s["activities"]}
        for c in s["cells"]:
            if c["visit"] in vids and c["activity"] in aids:
                v, a = vby[c["visit"]], aby[c["activity"]]
                ms = list(dict.fromkeys(c["footnote_markers"] + a["footnote_markers"]))
                out.append({"table_id": s["table_id"], "visit_id": v["id"], "visit": v["label"],
                            "visit_path": v["header_path"], "window": v["window"], "timing": v["timing"],
                            "activity_id": a["id"], "activity": a["label"], "section": a["section"],
                            "notes": a["notes"], "mark": c["mark"], "text": c["text"], "qualifier": c["qualifier"],
                            "footnote_markers": ms, "footnotes": {m: s["footnotes"][m] for m in ms
                                                                  if m in s["footnotes"]}})
    return out


def _md(s: str | None) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def to_markdown(soa: dict, max_notes: int = 80) -> str:
    """A compact Markdown view: one column per visit (label), optional period/window rows, section rows in
    bold, cells 'X' / qualifier with ^markers, the notes column, then the footnotes."""
    vs, acts = soa["visits"], soa["activities"]
    lines = []
    title = soa.get("caption") or "Schedule of Activities"
    lines.append(f"**{_md(title)}** (pages {', '.join(map(str, soa['pages']))})")
    has_notes = any(a["notes"] for a in acts)
    head = ["Activity"] + [_md(v["label"]) or f"(col {v['column']})" for v in vs] + (["Notes"] if has_notes else [])
    lines.append("| " + " | ".join(head) + " |")
    lines.append("|" + "---|" * len(head))
    if any(v["epoch_hint"] for v in vs):
        lines.append("| _Period_ | " + " | ".join(_md(v["epoch_hint"]) for v in vs) + (" |  |" if has_notes else " |"))
    if any(v["window"] for v in vs):
        lines.append("| _Window_ | " + " | ".join(_md(v["window"]) for v in vs) + (" |  |" if has_notes else " |"))
    grid = {(c["activity"], c["visit"]): c for c in soa["cells"]}
    section = None
    for a in acts:
        if a["section"] and a["section"] != section:
            section = a["section"]
            lines.append(f"| **{_md(section)}** |" + " |" * (len(head) - 1))
        row = [_md(a["label"]) + (f"^{','.join(a['footnote_markers'])}" if a["footnote_markers"] else "")]
        for v in vs:
            c = grid.get((a["id"], v["id"]))
            if not c:
                row.append("")
                continue
            txt = "X" if c["mark"] else ""
            if c["qualifier"]:
                txt = f"{txt} ({c['qualifier']})" if txt else c["qualifier"]
            if c["footnote_markers"]:
                txt += "^" + ",".join(c["footnote_markers"])
            row.append(_md(txt))
        if has_notes:
            n = a["notes"] or ""
            row.append(_md(n if len(n) <= max_notes else n[:max_notes].rsplit(" ", 1)[0] + " …"))
        lines.append("| " + " | ".join(row) + " |")
    if soa["footnotes"]:
        lines.append("")
        lines += [f"- {m}: {_md(t)}" for m, t in soa["footnotes"].items()]
    if soa.get("header_notes"):
        lines.append("")
        lines += [f"- header note: {_md(n)}" for n in soa["header_notes"]]
    return "\n".join(lines)


# =========================================================================== evaluation against USDM keys

def _as_grid(soa: dict):
    """usdm_gold.SoaGrid view of one extracted SoA: row 0 = visit labels, one row per activity, column 0 the
    activity label, one column per visit; a cell holds the raw text of a scheduled cell."""
    import usdm_gold as ug
    vs, acts = soa["visits"], soa["activities"]
    vidx = {v["id"]: k + 1 for k, v in enumerate(vs)}
    aidx = {a["id"]: k + 1 for k, a in enumerate(acts)}
    cells = [[""] * (len(vs) + 1) for _ in range(len(acts) + 1)]
    cells[0] = ["Activity"] + [v["label"] for v in vs]
    for a in acts:
        cells[aidx[a["id"]]][0] = a["label"]
    for c in soa["cells"]:
        cells[aidx[c["activity"]]][vidx[c["visit"]]] = c["text"] or ("X" if c["mark"] else (c["qualifier"] or ""))
    col_desc, col_time = {}, {}
    for v in vs:
        k = vidx[v["id"]]
        col_desc[k] = " | ".join(v["header_path"] + ([v["timing"]] if v["timing"] and v["timing"] not in
                                                      v["header_path"] else []))
        tv: dict = {}
        for part in v["header_path"] + ([v["timing"]] if v["timing"] else []):
            for u, vals in ug.time_values(part).items():
                tv.setdefault(u, set()).update(vals)
        col_time[k] = tv
    row_label = {aidx[a["id"]]: a["label"] for a in acts if a["label"]}
    marks = sum(1 for c in soa["cells"] if c["mark"])
    return ug.SoaGrid(soa["table_id"], soa["pages"][0] if soa["pages"] else 0, cells, [0], [0],
                      list(range(1, len(vs) + 1)), col_desc, col_time, row_label, marks)


def compare_with_key(name: str, soas: list[dict]) -> dict:
    """usdm_gold.soa_compare_study on the extracted SoAs (its alignment and cell scoring, unchanged), plus
    encounter/activity precision and window coverage."""
    import usdm_gold as ug
    grids = [_as_grid(s) for s in soas if s["visits"] and s["activities"]]
    st = ug.load_study(name)
    orig = ug.find_soa_tables
    ug.find_soa_tables = lambda doc, min_marks=4: grids  # reuse the comparison as is, on our grids
    try:
        rep = ug.soa_compare_study(st, {}, None)
    finally:
        ug.find_soa_tables = orig
    al = rep["alignment"]
    vis_cols = {(c["table"], c["col"]) for e in al["encounters"] for c in e["columns"]}
    act_rows = {(r["table"], r["row"]) for a in al["activities"] for r in a["rows"]}
    n_vis = sum(len(g.visit_cols) for g in grids)
    n_act = sum(len(g.row_label) for g in grids)
    rep["encounter_precision"] = round(len(vis_cols) / n_vis, 3) if n_vis else 0.0
    rep["activity_precision"] = round(len(act_rows) / n_act, 3) if n_act else 0.0
    # window coverage: key visits with a Timing window, aligned to one of our visits -> has our visit a window?
    kv = {v.id: v for v in ug.key_visits(st)}
    by_table = {s["table_id"]: s for s in soas}
    want = got = 0
    for e in al["encounters"]:
        k = kv.get(e["id"])
        if not k or not k.window or not e["columns"]:
            continue
        want += 1
        c = e["columns"][0]
        s = by_table.get(c["table"])
        if s and 1 <= c["col"] <= len(s["visits"]) and s["visits"][c["col"] - 1]["window"]:
            got += 1
    rep["window_coverage"] = (got, want)
    rep["n_visits"], rep["n_activities"] = n_vis, n_act
    return rep


def _load(name_or_path: str):
    from docling_core.types.doc import DoclingDocument
    p = Path(name_or_path)
    if not p.exists():
        if name_or_path == "MAIN":
            from project_paths import main_json
            p = main_json()
        else:
            import usdm_gold as ug
            p = ROOT / "output_public" / ug.resolve_name(name_or_path, need_key=False) / "protocol.json"
    if p.is_dir():
        p = p / "protocol.json"
    return DoclingDocument.load_from_json(p)


def _index(doc):
    try:
        from structure import build_section_index
        return build_section_index(doc)
    except Exception:
        return None


def summary_numbers(soas: list[dict]) -> dict:
    return {"tables": len(soas), "source_tables": sum(len(s["table_ids"]) for s in soas),
            "visits": sum(len(s["visits"]) for s in soas),
            "visits_with_window": sum(1 for s in soas for v in s["visits"] if v["window"]),
            "activities": sum(len(s["activities"]) for s in soas),
            "marks": sum(1 for s in soas for c in s["cells"] if c["mark"]),
            "qualifier_cells": sum(1 for s in soas for c in s["cells"] if c["qualifier"]),
            "unnamed_visits": sum(1 for s in soas for v in s["visits"] if not v["label"]),
            "notes_activities": sum(1 for s in soas for a in s["activities"] if a["notes"]),
            "footnotes": sum(len(s["footnotes"]) for s in soas),
            "unresolved_markers": sum(len(w.split(": ", 1)[1].split(", ")) for s in soas for w in s["warnings"]
                                      if w.startswith("unresolved footnote markers")),
            "cell_markers": sum(len(c["footnote_markers"]) for s in soas for c in s["cells"])}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv.pop(0) if argv else "eval"
    if cmd == "show":
        doc = _load(argv[0])
        soas = extract_soa(doc, _index(doc))
        if "--json" in argv:
            print(json.dumps(soas, ensure_ascii=False, indent=1))
            return 0
        for s in soas:
            print(f"== {s['table_ids']} pages {s['pages']} section {s['section_id']}: {len(s['visits'])} visits, "
                  f"{len(s['activities'])} activities, {len(s['cells'])} cells")
            for v in s["visits"]:
                print(f"   {v['id']:4s} {v['label']!r:28s} path={v['header_path']} window={v['window']!r} "
                      f"timing={v['timing']!r} fn={v['footnote_markers']}")
            print(to_markdown(s))
            for w in s["warnings"]:
                print("   warning:", w)
        return 0
    if cmd == "eval":
        import usdm_gold as ug
        verbose = any(a in ("-v", "--verbose") for a in argv)
        names = [ug.resolve_name(n) for n in argv if not n.startswith("-")] or sorted(ug.key_paths())
        for n in names:
            doc = _load(n)
            soas = extract_soa(doc, _index(doc))
            rep = compare_with_key(n, soas)
            c = rep["cells"]
            wg, ww = rep["window_coverage"]
            print(f"{n:22s} tables {len(soas)} ({sum(len(s['table_ids']) for s in soas)} src)  "
                  f"enc R {rep['encounter_recall']:.3f} P {rep['encounter_precision']:.3f}  "
                  f"act R {rep['activity_recall']:.3f} P {rep['activity_precision']:.3f}  "
                  f"SAI P {c['precision']:.3f} R {c['recall']:.3f} F1 {c['f1']:.3f} (e2e R {c['end_to_end_recall']:.3f})"
                  f"  windows {wg}/{ww}")
            if verbose:
                ug.print_soa_report(rep, True)
        return 0
    if cmd == "private":
        try:
            from docling_core.types.doc import DoclingDocument

            from test_protocols import final_doc_json
            for pid in ("P1", "P2", "P3"):
                jp = final_doc_json(pid)
                if jp is None:
                    print(pid, "missing")
                    continue
                doc = DoclingDocument.load_from_json(jp)
                soas = extract_soa(doc, _index(doc))
                print(pid, summary_numbers(soas))
        except BaseException as exc:  # numbers only: never echo document text
            import traceback
            tb = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).parent == ROOT]
            print("error", type(exc).__name__, "at", ", ".join(f"{Path(f.filename).name}:{f.lineno}" for f in tb[-3:]))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
