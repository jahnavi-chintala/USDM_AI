"""Text-layer reconciliation: repair Docling's text assembly with pdfium character evidence.

Docling builds item text from docling-parse cells. Three deterministic defects
survive into the Markdown and the RAG chunks (reports/audit/extraction.md, E1-E4),
and the PDF itself holds the evidence to repair each one:

  scripts        docling-parse splits cells at font changes and Docling joins them
                 with spaces: "1.0 x 10 9 /L", "Response a", "HbA 1c", "3 rd".
  hyphens        Docling's sanitize_text / _merge_elements drop a hyphen that ends a
                 line ("EGFRdirected", "72738"); table cells keep it with an added
                 space ("28- day", "Cycles 1- 3").
  merged words   docling-parse has no space where the PDF draws none, although the
                 gap is a word gap and pdfium generates a space there ("WHOARE").

`reconcile_text()` matches every text item and table cell that has provenance to
the pdfium characters inside its box (grown by one line when the box covers too
little of the text, as TableFormer cell boxes sometimes do). The match is a
character-sequence alignment (quotes, dashes, mapped Symbol/Wingdings glyphs and
ligatures are folded; list markers and other text in the box stay unmatched), so it
does not depend on how Docling normalised the text. An item whose alignment covers
less than `min_alignment` of its characters is skipped and logged. Lines are taken
from glyph geometry, not from pdfium's line breaks (pdfium also breaks around raised
glyphs). Only three kinds of edit are made, each between aligned neighbours, and only
on geometric evidence:

  scripts   a run of glyphs smaller than the line's dominant size AND raised (or
            lowered) from its dominant baseline is written per the shared
            convention below; a run set off by a space and touching the next word
            is that word's prefix ("of ^11C-labeled");
  hyphens   a hyphen glyph that ends a PDF line, attached to the preceding word, is
            re-inserted where Docling emitted the joined form ("- " when a space
            glyph follows it: "pre- and post-dose"), and the space Docling added
            after a line-end dash is removed ("28-day"). Never across a word split
            by typographic hyphenation (the document writes the joined form
            elsewhere and never the hyphenated one: "evero- limus" -> "everolimus"),
            and across two layout boxes only when the document writes the compound
            hyphenated elsewhere (Docling's _merge_elements also drops soft hyphens
            there, and pdfium reports soft and hard line-end hyphens alike);
  spaces    a space pdfium has between two words on one line, at a gap wider than
            kerning, is inserted where Docling has none ("WHO ARE").

No word lists and no protocol-specific strings: the size/rise/gap limits are the
typographic definitions of a super/subscript (Word draws them at about 2/3 size,
raised by about 1/3 em) and of a word gap, not values fitted to a document.

Shared convention (the table code consumes it):
  footnote/reference markers  "^" + marker attached to the preceding token
                              ("Response^a", "X^b,c", "Day 1^†", "criterion 5^1");
  exponents                   "10^9/L", "×10^12/L", "m^2", "1.73 m^2";
  ordinals and trademarks     joined without a caret ("3rd", "PAXLOVID™", "Kineret®");
  subscripts                  joined without a marker ("HbA1c", "AUC0-24").

`pair_label_values()` joins "Label:" items standing in a label column with the value
items printed on the same line to their right ("Protocol Number: C1234567"), detected
from the layout alone (no label list).

Both functions keep a decisions log (item objects, pages, edit types; never text),
in the style of protocol_postprocess: item objects are turned into final refs by
protocol_postprocess._finalise.
"""

from __future__ import annotations

import ctypes
import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from statistics import median

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from docling_core.types.doc import (
    BoundingBox,
    ContentLayer,
    CoordOrigin,
    DocItemLabel,
    DoclingDocument,
    PictureItem,
    ProvenanceItem,
    TableItem,
    TextItem,
)

# ---------------------------------------------------------------- typographic limits

SCRIPT_MAX_SIZE = 0.85
"""A super/subscript glyph is at most this share of its line's dominant size.
Word and LaTeX draw scripts at 0.58-0.70 of the base size; small caps and
mixed-font labels on one baseline are excluded by the rise test, not by size."""
SUP_MIN_RISE = 0.15
"""A superscript's baseline is raised by at least this share of the dominant size
(Word raises by about 1/3 em, LaTeX by 0.35-0.45 em)."""
SUB_MIN_DROP = 0.08
"""A subscript's baseline is lowered by at least this share (Word ~0.14 em, LaTeX ~0.15-0.25)."""
RAISED_MIN_RISE = 0.2
"""A run of FULL-size glyphs raised by at least this share is a footnote marker when it reads as one
("Phase^a", "X^c", "only)^b"): some documents raise the marker's baseline without shrinking it (Word's
"raised by 3 pt" character position). Above SUP_MIN_RISE because there is no size evidence; lines are
split at 0.6 em, which bounds the rise from above."""
RAISED_MAX_GAP = 0.5
"""...and it follows its word on the same line at most this far (a word space is ~0.25-0.33 em)."""
KERN = 0.1
"""Glyph advances closer than this share of the font size touch: kerning and tracking stay
below ~0.1 em, the narrowest word space is ~0.2 em (gaps are measured between advance
boxes, so side bearings do not count)."""
ANCHOR = 2
"""Aligned characters required on each side of an edit (the character itself included)."""

_debug_runs: list | None = None  # set to a list to collect skipped script runs (local diagnosis only)

_HYPHENS = "-\u2010\u2011"  # glyphs Docling drops at a line end (it only drops "-")
_DASHES = _HYPHENS + "\u2012\u2013\u2014\u2212"  # line-end dashes after which an added space is removed
_SOFT_HYPHEN = "\u00ad"
_PDFIUM_HYPHEN = 0x0002  # pdfium reports a hyphen it took for line-end hyphenation as U+0002
_RUN_TRIM = ",.;: "
_CLOSERS = ".,;:!?)]}%'\"\u2019\u201d"  # may end a word that a space or hyphen follows
_ATTACH = _CLOSERS + "/"  # may follow a script run without a space ("10^9/L", "m^2)")
_LINE_START_SYMBOLS = "*†‡§¶‖"  # a raised marker that starts a line ("^a" footnote) - not a bullet
# What a full-size raised run must read as: comma-separated markers, each one lower-case letter, a
# number of at most two digits or a short run of reference symbols ("a", "a,i", "12", "†").
_MARKER_RUN = re.compile(r"^(?:[a-z]|\d{1,2}|[*†‡§¶#]{1,3})(?:,(?:[a-z]|\d{1,2}|[*†‡§¶#]{1,3}))*$")
_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u2212": "-", "\u00ad": "-", "\ufffe": "-", "\x02": "-",
    "\u2022": "·", "\u2219": "·", "\u2044": "/", "\u00a0": " ",
}
# Multi-letter superscript runs that Unicode defines as one sign ("TM" -> "™", "SM" -> "℠").
_SUPER_SIGNS = {}
for _cp in (0x2120, 0x2122, 0x1F16A, 0x1F16B, 0x1F16C):
    _dec = unicodedata.decomposition(chr(_cp)).split()
    if _dec and _dec[0] == "<super>":
        _SUPER_SIGNS["".join(chr(int(h, 16)) for h in _dec[1:])] = chr(_cp)


def _symbol_map() -> dict[str, str]:
    """Symbol/Wingdings private-use -> Unicode, as protocol_postprocess maps them (lazy: no import cycle)."""
    try:
        from protocol_postprocess import _SYMBOL_FONT
        return dict(_SYMBOL_FONT)
    except Exception:  # noqa: BLE001 - folding still works without it (PUA chars stay unmatched)
        return {}


# ---------------------------------------------------------------- pdfium characters

@dataclass(slots=True, eq=False)
class _Char:
    ch: str            # Unicode ("" if none); a pdfium line-end hyphen is reported as "-"
    gen: bool          # generated by pdfium (space or line break, no glyph)
    flagged: bool      # pdfium took it for a line-end hyphen
    size: float        # effective font size (Tf x text-matrix scale)
    l: float
    b: float
    r: float
    t: float
    x: float           # origin = baseline point
    y: float
    upright: bool      # unrotated, unskewed text (baselines comparable)
    line: object       # line key (page, n); BREAK/GAP sentinels have their own
    adv_l: float = 0.0  # loose box: the glyph's advance (origin to next origin), no side bearings
    adv_r: float = 0.0

    @property
    def space(self) -> bool:
        return self.ch == "" or self.ch.isspace()

    @property
    def hspace(self) -> bool:
        """A space on the line (not a line break, not a sentinel)."""
        return self.ch != "" and self.ch.isspace() and self.ch not in "\r\n"


_GAP = _Char("", True, False, 0, 0, 0, 0, 0, 0, 0, False, ("gap",))      # other text in between
_BREAK = _Char("\n", True, False, 0, 0, 0, 0, 0, 0, 0, False, ("break",))  # next provenance box


class _Page:
    """pdfium characters of one page in stream order, with line keys."""

    def __init__(self, pdf: pdfium.PdfDocument, page_no: int):
        page = pdf[page_no - 1]
        self.rotated = bool(page.get_rotation())
        cl, cb, _, _ = page.get_cropbox()
        self.dx, self.dy = cl, cb
        tp = page.get_textpage()
        n = tp.count_chars()
        l, r, b, t = (ctypes.c_double() for _ in range(4))
        x, y = ctypes.c_double(), ctypes.c_double()
        m = pdfium_c.FS_MATRIX()
        loose = pdfium_c.FS_RECTF()
        chars: list[_Char] = []
        line = 0
        prev: _Char | None = None  # previous glyph
        broke = False  # pdfium generated a line break since the previous glyph
        for i in range(n):
            u = pdfium_c.FPDFText_GetUnicode(tp, i)
            gen = bool(pdfium_c.FPDFText_IsGenerated(tp, i))
            flagged = pdfium_c.FPDFText_IsHyphen(tp, i) == 1
            ch = "-" if (flagged and u == _PDFIUM_HYPHEN) else (chr(u) if u else "")
            if gen and ch in "\r\n":
                chars.append(_Char(ch, True, False, 0, 0, 0, 0, 0, 0, 0, False, (page_no, line)))
                broke = True
                continue
            pdfium_c.FPDFText_GetCharBox(tp, i, l, r, b, t)
            pdfium_c.FPDFText_GetCharOrigin(tp, i, x, y)
            pdfium_c.FPDFText_GetMatrix(tp, i, m)
            scale = abs(m.a * m.d - m.b * m.c) ** 0.5 or 1.0
            size = pdfium_c.FPDFText_GetFontSize(tp, i) * scale
            upright = m.a > 0 and m.d > 0 and abs(m.b) < 1e-3 * m.a and abs(m.c) < 1e-3 * m.d
            pdfium_c.FPDFText_GetLooseCharBox(tp, i, loose)
            c = _Char(ch, gen, flagged, size, l.value, b.value, r.value, t.value, x.value, y.value, upright,
                      (page_no, line), min(loose.left, loose.right), max(loose.left, loose.right))
            if not gen and not c.space:
                # Lines are geometric: pdfium also breaks its text around a raised glyph
                # ("10" "9" "\r\n" "/L") and drops the break after a hyphen it took for
                # hyphenation. A glyph starts a new line when its baseline is more than
                # 0.6 em from the previous glyph's (scripts are shifted by 0.15-0.45 em,
                # lines by at least 1 em) or it moves back left of the previous glyph.
                if prev is None:
                    new_line = False
                elif upright and prev.upright:
                    em = max(c.size, prev.size)
                    new_line = abs(c.y - prev.y) > 0.6 * em or c.x + 0.5 * em < prev.x
                else:  # rotated text: pdfium's own breaks
                    new_line = broke or prev.flagged or upright != prev.upright
                if new_line:
                    line += 1
                    c.line = (page_no, line)
                prev, broke = c, False
            chars.append(c)
        tp.close()
        self.chars = chars
        # glyphs sorted by vertical centre, for box queries
        real = [k for k, c in enumerate(chars) if not c.gen and not c.space]
        real.sort(key=lambda k: (chars[k].b + chars[k].t) / 2)
        self._by_y = real
        self._ys = [(chars[k].b + chars[k].t) / 2 for k in real]

    def select(self, bbox: BoundingBox, height: float, tol: float = 1.0) -> list[int]:
        """Stream indices of the glyphs whose centre lies in `bbox` (Docling coordinates)."""
        bl = bbox.to_bottom_left_origin(height) if bbox.coord_origin != CoordOrigin.BOTTOMLEFT else bbox
        x0, x1 = min(bl.l, bl.r) + self.dx - tol, max(bl.l, bl.r) + self.dx + tol
        y0, y1 = min(bl.b, bl.t) + self.dy - tol, max(bl.b, bl.t) + self.dy + tol
        lo, hi = bisect_left(self._ys, y0), bisect_right(self._ys, y1)
        out = []
        for k in self._by_y[lo:hi]:
            c = self.chars[k]
            if x0 <= (c.l + c.r) / 2 <= x1:
                out.append(k)
        out.sort()
        return out

    def sequence(self, selected: list[int]) -> list[_Char]:
        """Selected glyphs in stream order with the whitespace between them; GAP where
        another item's glyphs come in between."""
        seq: list[_Char] = []
        for a, b in zip(selected, selected[1:] + [None]):
            seq.append(self.chars[a])
            if b is None:
                break
            between = self.chars[a + 1:b]
            if all(c.gen or c.space for c in between):
                seq.extend(between)
            else:
                seq.append(_GAP)
        return seq


class _Pages:
    def __init__(self, doc: DoclingDocument, pdf: pdfium.PdfDocument):
        self.doc, self.pdf, self.cache = doc, pdf, {}

    def get(self, page_no: int) -> _Page | None:
        if page_no not in self.cache:
            ok = 1 <= page_no <= len(self.pdf)
            self.cache[page_no] = _Page(self.pdf, page_no) if ok else None
        return self.cache[page_no]

    def height(self, page_no: int) -> float:
        page = self.doc.pages.get(page_no)
        return page.size.height if page is not None else self.pdf[page_no - 1].get_size()[1]


# ---------------------------------------------------------------- document evidence for hyphens

class _HyphenEvidence:
    """Words of the PDF text layer: does a split "L|R" occur elsewhere joined or hyphenated?"""

    def __init__(self, pdf: pdfium.PdfDocument):
        self.pdf, self._words, self._pairs, self._suspended = pdf, None, None, None
        self.auto_hyphenation = False

    def _load(self) -> None:
        words, pairs, suspended, splits = Counter(), Counter(), Counter(), []
        for i in range(len(self.pdf)):
            tp = self.pdf[i].get_textpage()
            text = tp.get_text_range()
            tp.close()
            # Line-end hyphenations come out as "L\ufffeR" or "L-\r\nR": never as a token below.
            for line in text.replace("\ufffe", "\ufffe\n").splitlines():
                for tok in re.findall(r"[^\W_]+(?:[-\u2010\u2011][^\W_]+)*", line):
                    parts = re.split(r"[-\u2010\u2011]", tok.lower())
                    words.update(parts if len(parts) == 1 else [])
                    pairs.update(zip(parts, parts[1:]))
                suspended.update((a.lower(), b.lower()) for a, b in
                                 re.findall(r"([^\W_]+)[-\u2010\u2011] +([^\W_]+)", line))
            splits += re.findall(r"([^\W_]+)(?:\ufffe|[-\u2010\u2011]\r?\n)([^\W_]+)", text)
        self._words, self._pairs, self._suspended = words, pairs, suspended
        # Does this document hyphenate words at line ends (LaTeX, Word with auto-hyphenation)?
        # Judged from the line-end splits whose usage elsewhere is known: then a split
        # word without such evidence is taken as hyphenation too.
        known = Counter()
        for left, right in splits:
            if left.isalpha() and right.isalpha() and right[0].islower() and min(len(left), len(right)) >= 2:
                lw, rw = left.lower(), right.lower()
                if self._pairs[(lw, rw)]:
                    known["compound"] += 1
                elif self._words[lw + rw]:
                    known["typographic"] += 1
        self.auto_hyphenation = known["typographic"] >= 3 and known["typographic"] > known["compound"]

    def typographic(self, left: str, right: str) -> bool:
        """A word split by hyphenation: the document never writes "left-right" within a line,
        and writes "leftright" elsewhere - or hyphenates words at line ends in general; the
        split leaves at least two letters on each side, as every hyphenation algorithm does."""
        if len(left) < 2 or len(right) < 2:
            return False
        if self._words is None:
            self._load()
        left, right = left.lower(), right.lower()
        if self._pairs[(left, right)]:
            return False
        return self._words[left + right] > 0 or self.auto_hyphenation

    def hyphenated(self, left: str, right: str) -> bool:
        """The document writes "left-right" somewhere within a line."""
        if self._words is None:
            self._load()
        return self._pairs[(left.lower(), right.lower())] > 0

    def suspended(self, left: str, right: str) -> bool:
        """The document writes "left- right" (a suspended hyphen) within a line, never "left-right"."""
        if self._words is None:
            self._load()
        key = (left.lower(), right.lower())
        return self._suspended[key] > 0 and self._pairs[key] == 0


# ---------------------------------------------------------------- alignment

def _fold(c: str, symbols: dict[str, str]) -> str:
    if "\ue000" <= c <= "\uf8ff":
        c = symbols.get(c, c)
    c = _FOLD.get(c, c)
    return unicodedata.normalize("NFKC", c)


def _skip(c: str) -> bool:
    return c.isspace() or c == "^" or c in "\u200b\u200c\u200d\ufeff"


@dataclass
class _Alignment:
    text: str
    seq: list[_Char]
    dns: list[int]          # text indices of the aligned (non-space) characters, in order
    d2j: dict[int, int]     # text index -> seq position (fully matched characters only)
    left_ok: dict[int, bool]
    right_ok: dict[int, bool]
    score: float


def _align(text: str, seq: list[_Char], symbols: dict[str, str]) -> _Alignment:
    df, dsrc = [], []
    dns = [i for i, c in enumerate(text) if not _skip(c)]
    for i in dns:
        for fc in _fold(text[i], symbols):
            if not fc.isspace():
                df.append(fc)
                dsrc.append(i)
    pf, psrc = [], []
    for j, c in enumerate(seq):
        if c.gen or c.space:
            continue
        for fc in _fold(c.ch, symbols):
            if not fc.isspace():
                pf.append(fc)
                psrc.append(j)
    if not df:
        return _Alignment(text, seq, dns, {}, {}, {}, 0.0)
    sm = SequenceMatcher(None, "".join(df), "".join(pf), autojunk=False)
    match = [-1] * len(df)
    block_of = [None] * len(df)
    for a, b, size in sm.get_matching_blocks():
        for t in range(size):
            match[a + t] = b + t
            block_of[a + t] = (a, a + size)
    d2j, left_ok, right_ok = {}, {}, {}
    per_glyph = Counter(psrc)
    pos = 0
    for i in dns:
        k0 = pos
        while pos < len(dsrc) and dsrc[pos] == i:
            pos += 1
        ks = range(k0, pos)
        if not ks or any(match[k] < 0 for k in ks):
            continue
        js = {psrc[match[k]] for k in ks}
        if len(js) != 1:
            continue
        j = js.pop()
        # the glyph must be matched completely by this character (no ligature halves)
        if per_glyph[j] != len(ks):
            continue
        d2j[i] = j
        start, end = block_of[ks[0]]
        left_ok[i] = ks[0] - start + 1 >= ANCHOR or start == 0
        start, end = block_of[ks[-1]]
        right_ok[i] = end - ks[-1] >= ANCHOR or end == len(df)
    score = sum(m >= 0 for m in match) / len(df)
    return _Alignment(text, seq, dns, d2j, left_ok, right_ok, score)


# ---------------------------------------------------------------- script runs

def _line_groups(seq: list[_Char]) -> list[list[int]]:
    groups: dict[object, list[int]] = {}
    for j, c in enumerate(seq):
        if not c.gen and not c.space:
            groups.setdefault(c.line, []).append(j)
    return list(groups.values())


def _script_kinds(seq: list[_Char]) -> dict[int, str]:
    """seq position -> "sup" | "sub" for glyphs smaller than and off the line's dominant baseline;
    "raised" for full-size glyphs at least RAISED_MIN_RISE above it (markers only, see _script_edits)."""
    kinds: dict[int, str] = {}
    for js in _line_groups(seq):
        upright = [j for j in js if seq[j].upright and seq[j].size > 0]
        if len(upright) < 2:
            continue
        weight = Counter(round(seq[j].size, 1) for j in upright if seq[j].ch.isalnum())
        if not weight:
            continue
        dom = max(weight, key=lambda s: (weight[s], s))
        base = median(seq[j].y for j in upright if abs(seq[j].size - dom) <= 0.03 * dom)
        for j in upright:
            c = seq[j]
            rise = (c.y - base) / dom
            if c.size > SCRIPT_MAX_SIZE * dom:
                if rise >= RAISED_MIN_RISE:
                    kinds[j] = "raised"
                continue
            if rise >= SUP_MIN_RISE:
                kinds[j] = "sup"
            elif rise <= -SUB_MIN_DROP:
                kinds[j] = "sub"
    return kinds


def _runs(seq: list[_Char], kinds: dict[int, str]) -> list[tuple[str, list[int]]]:
    """Maximal same-line runs of script glyphs (spaces inside allowed), trimmed of edge punctuation."""
    runs, cur, kind, line = [], [], None, None
    for j, c in enumerate(seq):
        if c is _GAP or c is _BREAK:
            if cur:
                runs.append((kind, cur))
            cur = []
            continue
        if c.gen or c.space:
            continue  # a space inside a run ("a, b") does not end it
        k = kinds.get(j)
        if cur and (k != kind or c.line != line):
            runs.append((kind, cur))
            cur = []
        if k is not None:
            cur, kind, line = cur + [j], k, c.line
    if cur:
        runs.append((kind, cur))
    out = []
    for kind, js in runs:
        while js and seq[js[0]].ch in _RUN_TRIM:
            js = js[1:]
        while js and seq[js[-1]].ch in _RUN_TRIM:
            js = js[:-1]
        if js and any(seq[j].ch.isalnum() or unicodedata.category(seq[j].ch) in ("So", "Po") for j in js):
            out.append((kind, js))
    return out


def _ordinal_suffix(number: str) -> str:
    n = int(number)
    if n % 100 in (11, 12, 13):
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


# ---------------------------------------------------------------- edits

@dataclass
class _Edit:
    start: int
    end: int
    repl: str
    kind: str


def _gap(a: _Char, b: _Char) -> float:
    """Horizontal gap between two glyphs' advance boxes on one line (inf for rotated text)."""
    if not (a.upright and b.upright):
        return float("inf")
    return b.adv_l - a.adv_r


def _prev_real(seq: list[_Char], j: int) -> int | None:
    for k in range(j - 1, -1, -1):
        c = seq[k]
        if c is _GAP:
            return None
        if not c.gen and not c.space:
            return k
    return None


def _next_real(seq: list[_Char], j: int) -> int | None:
    for k in range(j + 1, len(seq)):
        c = seq[k]
        if c is _GAP:
            return None
        if not c.gen and not c.space:
            return k
    return None


def _script_edits(al: _Alignment, counts: Counter) -> list[_Edit]:
    text, seq = al.text, al.seq
    j2d = {j: i for i, j in al.d2j.items()}
    order = {i: n for n, i in enumerate(al.dns)}
    edits = []

    def unaligned(why: str, reals: list[int]) -> None:
        counts["script_unaligned"] += 1
        if _debug_runs is not None:
            _debug_runs.append((why, text, "".join(seq[j].ch for j in reals)))

    kinds = _script_kinds(seq)
    for kind, js in _runs(seq, kinds):
        reals = [j for j in js if not seq[j].gen and not seq[j].space]
        if any(j not in j2d for j in reals):
            unaligned("glyph", reals)
            continue
        ds_list = [j2d[j] for j in reals]
        if any(order[b] - order[a] != 1 for a, b in zip(ds_list, ds_list[1:])):
            unaligned("order", reals)
            continue
        ds, de = ds_list[0], ds_list[-1]
        jp, jn = _prev_real(seq, reals[0]), _next_real(seq, reals[-1])
        n0, n1 = order[ds], order[de]
        ip = al.dns[n0 - 1] if n0 > 0 else None
        inx = al.dns[n1 + 1] if n1 + 1 < len(al.dns) else None
        # both neighbours must be the PDF's neighbours (or absent on both sides)
        if (ip is not None and (jp is None or al.d2j.get(ip) != jp)) or (ip is None and jp is not None):
            unaligned("before", reals)
            continue
        if inx is not None and jn is not None and al.d2j.get(inx) != jn:
            unaligned("after", reals)
            continue
        run = "".join(text[i] for i in range(ds, de + 1) if not text[i].isspace() and text[i] != "^")
        starts_line = jp is None or seq[jp].line != seq[reals[0]].line
        if starts_line and not all(ch.isalnum() or ch in _LINE_START_SYMBOLS or ch in ",-" for ch in run):
            continue  # a raised symbol starting a line is a bullet or a box, not a marker
        if kind == "raised":
            # Full-size raised glyphs have no size evidence: only a marker-shaped run right after a
            # word or closing sign on its line, which itself sits on the baseline ("Phase^a", "X^c").
            first = seq[reals[0]]
            if (starts_line or not _MARKER_RUN.match(run) or kinds.get(jp) is not None
                    or not (seq[jp].ch.isalnum() or seq[jp].ch in _CLOSERS)
                    or _gap(seq[jp], first) > RAISED_MAX_GAP * max(first.size, seq[jp].size)):
                continue
        if kind == "sub":
            marker, label = "", "subscript"
        elif all(unicodedata.category(ch) == "So" for ch in run):
            marker, label = "", "trademark"
        elif run in _SUPER_SIGNS:
            marker, label, run = "", "trademark", _SUPER_SIGNS[run]
        else:
            number = re.search(r"(?<![\w.])(\d+)$", text[:ip + 1]) if ip is not None else None
            if number and not text[ip + 1:ds].strip(" ^") and run.lower() == _ordinal_suffix(number.group(1)):
                marker, label = "", "ordinal"
            else:
                marker, label = "^", "superscript" if kind == "sup" else "raised_marker"
        last, nxt = seq[reals[-1]], seq[jn] if jn is not None else None
        same_line_after = inx is not None and nxt is not None and nxt.line == last.line
        space_after = any(c.hspace for c in seq[reals[-1] + 1:jn]) if same_line_after else True
        touching = same_line_after and not space_after and _gap(last, nxt) < KERN * max(last.size, nxt.size)
        space_before = jp is not None and any(c.hspace for c in seq[jp + 1:reals[0]])
        d_after = text[de + 1:inx] if inx is not None else ""
        # A raised run set off by a space before and touching the next word is a prefix of
        # that word ("of ^11C-labeled"); otherwise it attaches to the preceding token, unless
        # it starts a PDF line (a marker opening a note: "^a Within 42 days").
        prefix = marker == "^" and space_before and touching and text[inx].isalnum()
        start = ip + 1 if (ip is not None and not starts_line and not prefix) else ds
        if start == ds and ds > 0 and text[ds - 1] == "^":
            start = ds - 1  # already marked (a second pass)
        repl = marker + run
        end = de + 1
        # After the run the PDF decides whether a space follows (same line only): Docling's
        # space goes when the next glyph touches the run and attaches ("10^9/L", "m^2)",
        # "Height^l,"); a word or a sign stays separated ("ET^c +2 weeks").
        if same_line_after and not starts_line:
            if touching and d_after and not d_after.strip() and (text[inx] in _ATTACH or prefix):
                end = inx
            elif space_after and not d_after:
                repl += " "
        if text[start:end] == repl:
            continue
        edits.append(_Edit(start, end, repl, label))
    return edits


def _boundary_edits(al: _Alignment, evidence: _HyphenEvidence, locked: set[int],
                    counts: Counter) -> list[_Edit]:
    """Line-end hyphens and lost spaces, between consecutive aligned characters."""
    text, seq = al.text, al.seq
    edits = []
    for i1, i2 in zip(al.dns, al.dns[1:]):
        if i1 not in al.d2j or i2 not in al.d2j or i1 in locked or i2 in locked:
            continue
        if not (al.left_ok.get(i1) and al.right_ok.get(i2)):
            continue
        j1, j2 = al.d2j[i1], al.d2j[i2]
        if j2 <= j1:
            continue
        between = seq[j1 + 1:j2]
        if any(c is _GAP for c in between):
            continue
        glyphs = [c for c in between if not c.gen and not c.space]
        d_between = text[i1 + 1:i2]
        new_line = seq[j2].line != seq[j1].line
        word_left = text[i1].isalnum()
        word_right = text[i2].isalnum()
        if not glyphs and not d_between and not new_line:
            # (c) merged words: pdfium has a space on this line, Docling has none (between a
            # word or closing punctuation and a word; not before dot leaders or symbols)
            spaces = [c for c in between if c.hspace]
            a, b = seq[j1], seq[j2]
            if (spaces and word_right and (word_left or text[i1] in _CLOSERS)
                    and _gap(a, b) >= KERN * max(a.size, b.size)):
                kind = "space_restored" if all(c.gen for c in spaces) else "space_glyph_restored"
                edits.append(_Edit(i1 + 1, i1 + 1, " ", kind))
            continue
        if not word_right:
            continue  # hyphen edits join words only ("28-day", "727-38"), never a rule of dashes
        if (len(glyphs) == 1 and not d_between and (glyphs[0].ch in _HYPHENS or glyphs[0].flagged)
                and (word_left or text[i1] in _CLOSERS)):
            # (b) a hyphen Docling dropped: attached to the left word, last glyph of its line
            h = glyphs[0]
            k = between.index(h)
            after = between[k + 1:]
            if between[:k] or h.ch == _SOFT_HYPHEN or not (seq[j2].line != h.line):
                continue
            left = re.search(r"[^\W_]+$", text[:i1 + 1])
            right = re.match(r"[^\W_]+", text[i2:])
            lw, rw = (left.group(0) if left else ""), (right.group(0) if right else "")
            if lw.isalpha() and rw.isalpha() and rw[0].islower() and evidence.typographic(lw, rw):
                counts["hyphen_kept_joined"] += 1
                if _debug_runs is not None:
                    _debug_runs.append(("kept_joined", text, lw + "|" + rw))
                continue
            if any(c is _BREAK for c in after) and not evidence.hyphenated(lw, rw):
                # Joined across two layout boxes, Docling's _merge_elements also drops a soft
                # hyphen (pdfium reports both as U+0002); within a box only "-" is dropped.
                counts["hyphen_skipped_across_boxes"] += 1
                continue
            # "pre- and post-dose": a space glyph follows the hyphen (pdfium drops that space
            # when it takes the hyphen for hyphenation; the document's own usage decides then)
            suspended = any(c.hspace and not c.gen for c in after) or evidence.suspended(lw, rw)
            edits.append(_Edit(i1 + 1, i1 + 1, "- " if suspended else "-", "hyphen_restored"))
            continue
        if (not glyphs and d_between and not d_between.strip() and new_line and text[i1] in _DASHES
                and (seq[j1].ch in _DASHES or seq[j1].flagged)):
            # (b') "28- day": the dash ends its PDF line, attached on the left, no space after it
            prev_i = i1 - 1
            if prev_i < 0 or not (text[prev_i].isalnum() or text[prev_i] in _CLOSERS) or prev_i not in al.d2j:
                continue
            jp = al.d2j[prev_i]
            if jp >= j1 or any(c.hspace or c is _GAP for c in seq[jp + 1:j1]):
                continue
            if any(c.hspace and not c.gen for c in between):
                continue  # "pre- and post-dose": a real space follows the dash
            left = re.search(r"[^\W_]+$", text[:i1])
            right = re.match(r"[^\W_]+", text[i2:])
            if (text[i1] in _HYPHENS and left and right and left.group(0).isalpha() and right.group(0).isalpha()
                    and right.group(0)[0].islower() and evidence.typographic(left.group(0), right.group(0))):
                # a word hyphenated at a narrow cell's line end ("evero- limus"): the document
                # writes it joined everywhere else
                edits.append(_Edit(i1, i2, "", "hyphen_joined"))
                continue
            edits.append(_Edit(i1 + 1, i2, "", "hyphen_unspaced"))
    return edits


def _apply(text: str, edits: list[_Edit]) -> tuple[str, Counter]:
    done = Counter()
    last = len(text) + 1
    for e in sorted(edits, key=lambda e: (e.start, e.end), reverse=True):
        if e.end > last:
            continue  # overlaps an edit already applied
        text = text[:e.start] + e.repl + text[e.end:]
        last = e.start
        done[e.kind] += 1
    return text, done


def _reconcile(al: _Alignment, evidence: _HyphenEvidence, counts: Counter) -> tuple[str, Counter]:
    script = _script_edits(al, counts)
    locked = set()
    for e in script:
        locked.update(range(e.start - 1, e.end + 1))
    edits = script + _boundary_edits(al, evidence, locked, counts)
    return _apply(al.text, edits)


def _sequence(pages: _Pages, provs: list[tuple[int, BoundingBox]], grow: float = 0.0) -> list[_Char] | None:
    seq: list[_Char] = []
    for page_no, bbox in provs:
        page = pages.get(page_no)
        if page is None or page.rotated:
            return None
        if grow:
            up = 1 if bbox.coord_origin == CoordOrigin.BOTTOMLEFT else -1
            bbox = BoundingBox(l=bbox.l - grow, r=bbox.r + grow, t=bbox.t + up * grow, b=bbox.b - up * grow,
                               coord_origin=bbox.coord_origin)
        sel = page.select(bbox, pages.height(page_no))
        if not sel:
            continue
        if seq:
            seq.append(_BREAK)
        seq.extend(page.sequence(sel))
    return seq


def _best_alignment(text: str, pages: _Pages, provs: list[tuple[int, BoundingBox]], symbols: dict[str, str],
                    min_alignment: float) -> _Alignment | None:
    """Align with the glyphs in the item's boxes; when that covers too little of the text
    (a cell box TableFormer drew short of its last line), retry with the boxes grown by
    one line (1 em of the glyphs found) - extra glyphs from neighbours stay unmatched."""
    seq = _sequence(pages, provs)
    al = _align(text, seq, symbols) if seq else None
    if al is None or al.score < min_alignment:
        sizes = [c.size for c in seq or [] if not c.gen and not c.space and c.size > 0]
        grown = _sequence(pages, provs, grow=median(sizes) if sizes else 10.0)
        if grown:
            al2 = _align(text, grown, symbols)
            if al is None or al2.score > al.score:
                al = al2
    return al


def reconcile_text(doc: DoclingDocument, pdf: pdfium.PdfDocument, log: dict | None = None,
                   min_alignment: float = 0.9) -> dict:
    """Superscripts/subscripts, dropped line-end hyphens and lost spaces, from pdfium geometry.

    Every text item (body, furniture, list items, captions, footnotes) and every table
    cell with a box is aligned with the pdfium glyphs inside that box; see the module
    docstring for the three edits and their evidence. Returns counts per edit type;
    the log gets one entry per edited or skipped item: {"item", "page", "cell"
    (tables), "edits" | "reason"} - never text.
    """
    log = log if log is not None else {}
    entries = log.setdefault("text_reconcile", [])
    counts: Counter = Counter()
    pages = _Pages(doc, pdf)
    evidence = _HyphenEvidence(pdf)
    symbols = _symbol_map()

    def handle(owner, text: str, provs: list[tuple[int, BoundingBox]], cell=None) -> str | None:
        kind = "cells" if cell is not None else "items"
        al = _best_alignment(text, pages, provs, symbols, min_alignment)
        if al is None:
            counts[f"{kind}_without_glyphs"] += 1
            return None
        counts[f"{kind}_checked"] += 1
        entry = {"item": owner, "page": provs[0][0]}
        if cell is not None:
            entry["cell"] = cell
        if al.score < min_alignment:
            counts[f"{kind}_skipped_low_alignment"] += 1
            entries.append({**entry, "reason": "low_alignment", "score": round(al.score, 2)})
            return None
        new, done = _reconcile(al, evidence, counts)
        if not done:
            return None
        counts.update(done)
        entries.append({**entry, "edits": dict(done)})
        return new

    for item in doc.texts:
        if not item.prov or not item.text.strip():
            continue
        new = handle(item, item.text, [(p.page_no, p.bbox) for p in item.prov])
        if new is not None:
            item.text = new
    for table in doc.tables:
        table_pages = [p.page_no for p in table.prov]
        if not table_pages:
            continue
        for cell in table.data.table_cells:
            if cell.bbox is None or not cell.text.strip():
                continue
            pos = [cell.start_row_offset_idx, cell.start_col_offset_idx]
            # before page-split tables are merged a table lies on one page; after, try each
            page_no = table_pages[0]
            if len(set(table_pages)) > 1:
                best = None
                for p in dict.fromkeys(table_pages):
                    seq = _sequence(pages, [(p, cell.bbox)])
                    score = _align(cell.text, seq, symbols).score if seq else -1.0
                    if best is None or score > best[0]:
                        best = (score, p)
                page_no = best[1]
            new = handle(table, cell.text, [(page_no, cell.bbox)], cell=pos)
            if new is not None:
                cell.text = new
    return dict(counts)


# ---------------------------------------------------------------- cover-page label/value pairs

_LABEL = re.compile(r"^\s*\S.{0,58}:\s*$")


def _tl(doc: DoclingDocument, prov: ProvenanceItem) -> BoundingBox:
    return prov.bbox.to_top_left_origin(doc.pages[prov.page_no].size.height)


def pair_label_values(doc: DoclingDocument, log: dict | None = None, max_page: int | None = None,
                      min_pairs: int = 2) -> int:
    """Join "Label:" items with the value printed on the same line to their right.

    Title and cover pages lay out identifiers as a left column of labels ("Protocol
    Number:", "US IND Number:") and a right column of values; Docling makes each a
    separate item and its reading order emits the labels, then the values. The same
    layout recurs in synopses, contact blocks and abbreviation lists ("AE:" | "adverse
    event"). A page (all pages, or the first `max_page`) qualifies when at least
    `min_pairs` labels (short body items ending in ":") have a value item whose first
    line shares their first line (tops within half a line) to their right, and those
    labels stand in a column (each shares its left edge with another paired label -
    not a stray "Note:").
    Each such label item then takes "Label: Value" (the nearest item to its right and
    items closely continuing it, never the next label), its box becomes the union and
    the value items are deleted, so the pair sits at the label's place in reading
    order. No label text is hard-coded.

    A text item rather than a key-value table: "Protocol Number: C1234567" stays one
    searchable, quotable line in the Markdown and in every chunk (the USDM agent
    quotes it as evidence), and a 2-column table would be taken up by the table
    steps (merging, header detection) and serialised with a fake header row.
    """
    log = log if log is not None else {}
    entries = log.setdefault("label_value_pairs", [])
    by_page: dict[int, list[TextItem]] = {}
    for t in doc.texts:
        if (len(t.prov) == 1 and (max_page is None or t.prov[0].page_no <= max_page)
                and t.content_layer == ContentLayer.BODY
                and t.label in (DocItemLabel.TEXT, DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE,
                                DocItemLabel.PARAGRAPH)
                and t.text.strip()
                and not isinstance(t.parent.resolve(doc), (TableItem, PictureItem))):
            by_page.setdefault(t.prov[0].page_no, []).append(t)
    merged = 0
    for page_no, items in sorted(by_page.items()):
        boxes = {id(t): _tl(doc, t.prov[0]) for t in items}
        labels = [t for t in items if _LABEL.match(t.text) and any(ch.isalpha() for ch in t.text)]
        label_ids = {id(t) for t in labels}
        heights = [b.b - b.t for b in boxes.values() if b.b > b.t]
        line = median(heights) if heights else 10.0  # loose items are mostly one line here
        pairs = []
        for lab in labels:
            lb = boxes[id(lab)]
            right = [t for t in items if t is not lab and boxes[id(t)].l >= lb.r - 1
                     and abs(boxes[id(t)].t - lb.t) <= line / 2]
            right.sort(key=lambda t: boxes[id(t)].l)
            values, edge = [], None
            for t in right:
                # the value: the nearest item, and items continuing it closely; not the next
                # label, nor a separate note further along the line ("Total number of pages")
                if id(t) in label_ids or (edge is not None and boxes[id(t)].l - edge > 2 * line):
                    break
                values.append(t)
                edge = boxes[id(t)].r
            if values:
                pairs.append((lab, values))
        if len(pairs) < min_pairs:
            continue
        # labels form a column: each paired label shares its left edge with another one
        tol = line / 2
        lefts = [boxes[id(lab)].l for lab, _ in pairs]
        col = [sum(abs(a - b) <= tol for b in lefts) >= 2 for a in lefts]
        if sum(col) < min_pairs:
            continue
        for (lab, values), in_col in zip(pairs, col):
            if not in_col:
                continue
            text = " ".join([lab.text.strip()] + [" ".join(v.text.split()) for v in values])
            box = BoundingBox.enclosing_bbox([lab.prov[0].bbox] + [v.prov[0].bbox for v in values])
            prov = ProvenanceItem(page_no=page_no, bbox=box, charspan=(0, len(text)))
            if lab.label == DocItemLabel.TEXT:
                lab.text = lab.orig = text
                lab.prov[0] = prov
                pair, gone = lab, values
            else:
                # a label the layout model called a heading or title is a field, not a section:
                # the pair becomes plain text in its place, so it opens no outline node
                pair = doc.insert_text(sibling=lab, label=DocItemLabel.TEXT, text=text, orig=text, prov=prov,
                                       content_layer=lab.content_layer, after=True)
                gone = [lab, *values]
            doc.delete_items(node_items=gone)
            merged += 1
            entries.append({"item": pair, "page": page_no, "values": len(values)})
    return merged
