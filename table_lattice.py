"""Ruling-line lattice: verify TableFormer's table grid against the PDF, and
correct it on fully ruled tables ("models propose, the PDF verifies").

TableFormer sees a table crop at 448 px, so narrow Schedule-of-Activities
columns can fuse and text near a row rule can land in the wrong row. Born-
digital protocols draw their tables with vector ruling lines, which do not
depend on resolution. For every raw (per-page, pre-merge) table fragment:

1. Ruling lines come from the page's path objects (pypdfium2): thin filled
   rectangles (Word) and axis-aligned stroked segments, inside Form XObjects
   too, mapped into Docling's page frame (crop-box origin, /Rotate applied).
   Collinear pieces are joined (dashed rules, rules drawn cell by cell).
   Invisible (white / transparent) paint is ignored, as are small closed
   boxes (check boxes), text-hugging strokes not joined to vertical rules at
   both ends (underlines, strike-throughs) and tiny unattached vertical marks.
2. The lattice: rule positions are clustered into column/row boundaries;
   grid slots with no rule between them merge (spanning cells).
3. TableFormer's words (the whitespace tokens of its cell texts, so text
   reconciliation and glyph mapping are kept) are located in the pdfium text
   layer: each cell's text is aligned character by character with the text
   layer inside its box; a word not found there is searched in the rest of
   the table; a word found nowhere takes its neighbour's place.
4. Agreement: every TableFormer cell's words fall in one lattice cell, no
   lattice cell collects words of two TableFormer cells, and positions and
   spans (empty rows/columns compacted away) are equal.
5. A disagreeing table is rebuilt from the lattice only when it is fully
   ruled: rules separate at least `min_rule_coverage` of the pairs of
   adjacent TableFormer cells (an unruled, booktabs or partly ruled table
   proves nothing and stays on TableFormer, flagged). Rows, columns and spans
   come from the lattice; each word goes to the lattice cell holding its
   characters, in reading order; header/row-header/section flags follow the
   TableFormer cells the words came from (full-width body cells are section
   rows). The multiset of words must be equal before and after, else the
   table is left unchanged.
6. Row-ruled tables (horizontal rules, (almost) no vertical ones; optional,
   on by default): rows come from the rules, columns from TableFormer. Each
   TableFormer column boundary becomes a virtual vertical rule in the
   whitespace between the words of the columns on either side (at a break
   that recurs in the horizontal rules there, as Word draws borders cell by
   cell, else mid-gap), left open in the row bands holding words of a cell
   spanning it. Then steps 4-5 run on that lattice, rebuilding when rules
   separate at least `min_row_rule_coverage` of the vertically adjacent
   TableFormer cell pairs (booktabs and row-group rules never qualify). Not
   done when a boundary has no clean gap, a spanning cell shares a row band
   with cells ending or starting at its boundary, or text above the top /
   below the bottom rule holds several TableFormer rows (logged reasons).

Header cells holding two text blocks separated by a blank line (a column name
with a note under it) get a newline between the blocks, so the chunk
serializer can keep the note out of the column name; Markdown and HTML
exports render the newline as a space.

Every decision is geometric; nothing is protocol- or string-specific.
"""

from __future__ import annotations

import ctypes
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from statistics import median

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pr
from docling_core.types.doc import BoundingBox, CoordOrigin, DoclingDocument, TableCell, TableData, TableItem

# ---------------------------------------------------------------------------- text normalisation

_MAP = {"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "‚": "'", "–": "-", "—": "-", "‐": "-",
        "‑": "-", "−": "-", "￾": "-", "\xad": "-", "\x02": "-", "^": ""}
try:  # the pipeline's Symbol/Wingdings map (U+F0B7 -> "•", ...), so mapped cell text still matches the PDF
    from protocol_postprocess import _SYMBOL_FONT

    _MAP.update(_SYMBOL_FONT)
except Exception:  # noqa: BLE001 - optional; private-use glyphs then simply do not match
    pass
_TRANS = str.maketrans(_MAP)


def _norm(s: str) -> str:
    """Comparison form of text: NFKC, typographic quotes/dashes and Symbol glyphs mapped,
    the superscript marker "^" dropped, lowercase, no whitespace."""
    return "".join(unicodedata.normalize("NFKC", s).translate(_TRANS).lower().split())


# ---------------------------------------------------------------------------- page frame

class PageFrame:
    """Maps pdfium user-space points into Docling's page frame (bottom-left origin).

    pdfium reports characters and path objects in unrotated user space; Docling
    (docling-parse) uses the displayed page: crop-box origin at (0, 0) and the
    page's /Rotate applied clockwise.
    """

    def __init__(self, page: pdfium.PdfPage):
        l, b, r, t = page.get_bbox()
        self.l, self.b, self.w, self.h = l, b, r - l, t - b
        self.rot = page.get_rotation() % 360

    def pt(self, x: float, y: float) -> tuple[float, float]:
        x, y = x - self.l, y - self.b
        if self.rot == 90:
            return y, self.w - x
        if self.rot == 180:
            return self.w - x, self.h - y
        if self.rot == 270:
            return self.h - y, x
        return x, y

    def box(self, l: float, b: float, r: float, t: float) -> tuple[float, float, float, float]:
        x0, y0 = self.pt(l, b)
        x1, y1 = self.pt(r, t)
        return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


# ---------------------------------------------------------------------------- text layer

@dataclass(slots=True)
class Char:
    ch: str
    l: float
    b: float
    r: float
    t: float
    cx: float = 0.0
    cy: float = 0.0

    def __post_init__(self):
        self.cx, self.cy = (self.l + self.r) / 2, (self.b + self.t) / 2


def page_chars(page: pdfium.PdfPage, frame: PageFrame) -> list[Char]:
    """Printed characters of a page in content-stream order (whitespace and pdfium's
    generated spaces/line breaks left out), in the Docling page frame."""
    tp = page.get_textpage()  # keep the object alive: its finalizer closes the raw handle
    raw = tp.raw
    l, r, b, t = ctypes.c_double(), ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
    out = []
    for i in range(pr.FPDFText_CountChars(raw)):
        u = pr.FPDFText_GetUnicode(raw, i)
        if not u or pr.FPDFText_IsGenerated(raw, i) == 1:
            continue
        try:
            ch = chr(u)
        except ValueError:
            continue
        if ch.isspace():
            continue
        pr.FPDFText_GetCharBox(raw, i, l, r, b, t)
        out.append(Char(ch, *frame.box(l.value, b.value, r.value, t.value)))
    return out


def page_words(page: pdfium.PdfPage, frame: PageFrame) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Words of a page's text layer with their boxes (Docling frame): runs of printed
    characters split at whitespace, pdfium's generated spaces/line breaks, a line change
    or a gap wider than 0.6 of the glyph height."""
    tp = page.get_textpage()
    raw = tp.raw
    l, r, b, t = ctypes.c_double(), ctypes.c_double(), ctypes.c_double(), ctypes.c_double()
    words, cur = [], []

    def flush():
        if cur:
            words.append(("".join(c.ch for c in cur), (min(c.l for c in cur), min(c.b for c in cur),
                                                         max(c.r for c in cur), max(c.t for c in cur))))
            cur.clear()

    for i in range(pr.FPDFText_CountChars(raw)):
        u = pr.FPDFText_GetUnicode(raw, i)
        try:
            ch = chr(u) if u else " "
        except ValueError:
            ch = " "
        if ch.isspace() or pr.FPDFText_IsGenerated(raw, i) == 1:
            flush()
            continue
        pr.FPDFText_GetCharBox(raw, i, l, r, b, t)
        c = Char(ch, *frame.box(l.value, b.value, r.value, t.value))
        if cur:
            p = cur[-1]
            h = max(p.t - p.b, c.t - c.b, 1.0)
            if min(p.t, c.t) - max(p.b, c.b) < -0.2 * h or c.l - p.r > 0.6 * h or c.l - p.r < -3 * h:
                flush()
        cur.append(c)
    flush()
    return words


# ---------------------------------------------------------------------------- ruling lines

@dataclass
class Rules:
    h: list = field(default_factory=list)  # (y, x0, x1)
    v: list = field(default_factory=list)  # (x, y0, y1)
    fills: list = field(default_factory=list)  # shaded rectangles (l, b, r, t)


def _apply(m, x: float, y: float) -> tuple[float, float]:
    return m.a * x + m.c * y + m.e, m.b * x + m.d * y + m.f


def _visible(obj, fill: bool) -> bool:
    """False for white or fully transparent paint (invisible "rules")."""
    rgba = [ctypes.c_uint() for _ in range(4)]
    getter = pr.FPDFPageObj_GetFillColor if fill else pr.FPDFPageObj_GetStrokeColor
    if not getter(obj.raw, *rgba):
        return True
    r, g, b, a = (c.value for c in rgba)
    return a > 0 and min(r, g, b) < 245


def page_rules(page: pdfium.PdfPage, frame: PageFrame, thin: float = 2.5, min_len: float = 3.0,
               min_box: float = 10.0) -> Rules:
    """Horizontal/vertical ruling segments and shaded fills of a page, in the Docling frame.

    Every path object (inside Form XObjects too) is split into subpaths: a
    filled subpath thinner than `thin` is a rule; a stroked subpath gives its
    axis-aligned straight segments, unless it is a small closed box (both
    sides under `min_box`, e.g. a check box). Larger filled subpaths are fills.
    """
    rules = Rules()
    x, y = ctypes.c_float(), ctypes.c_float()
    for obj in page.get_objects(filter=[pr.FPDF_PAGEOBJ_PATH], max_depth=8):
        fill_mode, stroke = ctypes.c_int(0), ctypes.c_int(0)
        if not pr.FPDFPath_GetDrawMode(obj.raw, fill_mode, stroke):
            continue
        filled = fill_mode.value != 0 and _visible(obj, True)
        stroked = bool(stroke.value) and _visible(obj, False)
        if not (filled or stroked):
            continue
        mats = [obj.get_matrix()]
        cont = obj.container
        while cont is not None:
            mats.append(cont.get_matrix())
            cont = cont.container
        subpaths: list[list] = []  # [points, closed]
        for i in range(pr.FPDFPath_CountSegments(obj.raw)):
            seg = pr.FPDFPath_GetPathSegment(obj.raw, i)
            if not pr.FPDFPathSegment_GetPoint(seg, x, y):
                continue
            px, py = x.value, y.value
            for m in mats:
                px, py = _apply(m, px, py)
            px, py = frame.pt(px, py)
            typ = pr.FPDFPathSegment_GetType(seg)
            if typ == pr.FPDF_SEGMENT_MOVETO or not subpaths:
                subpaths.append([[(px, py, typ)], False])
            else:
                subpaths[-1][0].append((px, py, typ))
            if pr.FPDFPathSegment_GetClose(seg):
                subpaths[-1][1] = True
        for pts, closed in subpaths:
            if len(pts) < 2:
                continue
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            l, r, b, t = min(xs), max(xs), min(ys), max(ys)
            w, h = r - l, t - b
            if filled:
                if h <= thin and w >= min_len:
                    rules.h.append(((b + t) / 2, l, r))
                    continue
                if w <= thin and h >= min_len:
                    rules.v.append(((l + r) / 2, b, t))
                    continue
                if w > thin and h > thin and not stroked:
                    rules.fills.append((l, b, r, t))
                    continue
            if not stroked or (closed and w < min_box and h < min_box):
                continue  # a check box or a glyph-like mark is not a table border
            segs = list(zip(pts, pts[1:])) + ([(pts[-1], pts[0])] if closed else [])
            for (x0, y0, _), (x1, y1, typ) in segs:
                if typ == pr.FPDF_SEGMENT_BEZIERTO:
                    continue
                if abs(y1 - y0) < 0.8 and abs(x1 - x0) >= min_len:
                    rules.h.append(((y0 + y1) / 2, min(x0, x1), max(x0, x1)))
                elif abs(x1 - x0) < 0.8 and abs(y1 - y0) >= min_len:
                    rules.v.append(((x0 + x1) / 2, min(y0, y1), max(y0, y1)))
    return rules


def _merge_collinear(segs: list, gap: float = 4.0, tol: float = 0.6) -> list:
    """Join collinear segments (same position within `tol`) whose ends are at most `gap`
    apart: dashed or dotted rules, and rules drawn cell by cell, become one segment."""
    out = []
    group: list = []
    for seg in sorted(segs) + [None]:
        if seg is not None and group and abs(seg[0] - group[0][0]) <= tol:
            group.append(seg)
            continue
        if group:
            pos = sum(s[0] for s in group) / len(group)
            cur = None
            for _, s0, s1 in sorted(group, key=lambda s: s[1]):
                if cur and s0 <= cur[1] + gap:
                    cur[1] = max(cur[1], s1)
                else:
                    if cur:
                        out.append((pos, cur[0], cur[1]))
                    cur = [s0, s1]
            out.append((pos, cur[0], cur[1]))
        group = [seg] if seg is not None else []
    return out


def _drop_decorations(h: list, v: list, box, chars: list[Char], tol: float = 2.5,
                      edge: float = 6.0) -> tuple[list, list]:
    """Drop strokes that are not table rules.

    A horizontal segment that hugs text (characters just above it or crossed by
    it cover >= 60 % of its length: an underline or strike-through) is dropped
    unless both its ends meet a vertical rule or the table's edge. A short
    vertical segment (under 10 pt) meeting no horizontal rule or edge at either
    end is dropped too (a glyph-like mark). Rules broken by gaps (around a
    redaction box, say) are kept: only text-hugging strokes are suspects.
    The edge is the table box's side or the outermost extent of the rules.
    """
    l, b, r, t = box
    if h:
        l, r = max(l, min(s[1] for s in h)), min(r, max(s[2] for s in h))
    if v:
        b, t = max(b, min(s[1] for s in v)), min(t, max(s[2] for s in v))

    def meets_v(px: float, py: float) -> bool:
        return px <= l + edge or px >= r - edge or any(
            abs(x - px) <= tol and y0 - tol <= py <= y1 + tol for x, y0, y1 in v)

    def meets_h(px: float, py: float) -> bool:
        return py <= b + edge or py >= t - edge or any(
            abs(y - py) <= tol and x0 - tol <= px <= x1 + tol for y, x0, x1 in h)

    def hugs_text(y: float, x0: float, x1: float) -> bool:
        near = [(c.l, c.r) for c in chars
                if x0 - 1 <= c.cx <= x1 + 1 and (y - 1.5 <= c.b <= y + 4 or c.b <= y <= c.t)]
        return bool(near) and _cover(near, x0, x1) >= 0.6

    keep_h = [s for s in h if not (hugs_text(*s) and not (meets_v(s[1], s[0]) and meets_v(s[2], s[0])))]
    keep_v = [s for s in v if s[2] - s[1] >= 10 or meets_h(s[0], s[1]) or meets_h(s[0], s[2])]
    return keep_h, keep_v


# ---------------------------------------------------------------------------- lattice

def _cover(intervals, lo: float, hi: float) -> float:
    """Share of [lo, hi] covered by the union of intervals."""
    if hi <= lo:
        return 0.0
    total, end = 0.0, lo
    for s0, s1 in sorted(intervals):
        s0, s1 = max(s0, end), min(s1, hi)
        if s1 > s0:
            total += s1 - s0
            end = s1
    return total / (hi - lo)


@dataclass
class Lattice:
    xs: list  # column boundaries, ascending (outer ones included)
    ys: list  # row boundaries, descending (outer ones included)
    x_span: list  # (min, max) rule positions merged into each x boundary
    y_span: list
    implicit_x: set  # boundaries added (no rule) because text lies outside the ruled grid
    implicit_y: set
    h: list  # rule segments used (y, x0, x1)
    v: list  # (x, y0, y1)
    slot: dict = field(default_factory=dict)  # (row, col) -> cell index
    cells: list = field(default_factory=list)  # (r0, c0, r1, c1) per merged cell

    @property
    def n_rows(self) -> int:
        return len(self.ys) - 1

    @property
    def n_cols(self) -> int:
        return len(self.xs) - 1

    def rect(self, k: int) -> tuple[float, float, float, float]:
        r0, c0, r1, c1 = self.cells[k]
        return self.xs[c0], self.ys[r1], self.xs[c1], self.ys[r0]

    def locate(self, x: float, y: float, tol: float = 3.0) -> int | None:
        """Merged cell holding point (x, y); points just outside the grid are clamped."""
        if not (self.xs[0] - tol <= x <= self.xs[-1] + tol and self.ys[-1] - tol <= y <= self.ys[0] + tol):
            return None
        j = sum(1 for xb in self.xs[1:-1] if x >= xb)
        i = sum(1 for yb in self.ys[1:-1] if y <= yb)
        return self.slot[(i, j)]

    def h_cover(self, i: int, lo: float, hi: float) -> float:
        if i in self.implicit_y:
            return 0.0
        a, z = self.y_span[i]
        return _cover([(x0, x1) for y, x0, x1 in self.h if a - 2 <= y <= z + 2], lo, hi)

    def v_cover(self, j: int, lo: float, hi: float) -> float:
        if j in self.implicit_x:
            return 0.0
        a, z = self.x_span[j]
        return _cover([(y0, y1) for x, y0, y1 in self.v if a - 2 <= x <= z + 2], lo, hi)


def _clusters(vals: list[float], tol: float = 2.0, sliver: float = 3.0) -> list[tuple[float, float, float]]:
    """(mean, min, max) per cluster of positions; clusters closer than `sliver` merge (double rules)."""
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and (v - out[-1][-1] <= tol or v - sum(out[-1]) / len(out[-1]) < sliver):
            out[-1].append(v)
        else:
            out.append([v])
    return [(sum(c) / len(c), min(c), max(c)) for c in out]


def _table_rules(rules: Rules, box, chars: list[Char], pad: float = 4.0) -> tuple[list, list]:
    """The table's rule segments (h, v): clipped to the box (+ pad), collinear pieces joined,
    decorations dropped."""
    l, b, r, t = box
    v = [(x, max(y0, b - pad), min(y1, t + pad)) for x, y0, y1 in rules.v
         if l - pad <= x <= r + pad and y1 > b - pad and y0 < t + pad]
    h = [(y, max(x0, l - pad), min(x1, r + pad)) for y, x0, x1 in rules.h
         if b - pad <= y <= t + pad and x1 > l - pad and x0 < r + pad]
    return _drop_decorations(_merge_collinear(h), _merge_collinear(v), box, chars)


def build_lattice(rules: Rules, box, chars: list[Char], pad: float = 4.0) -> Lattice | None:
    """The ruled grid of a table box (Docling frame, bottom-left), or None without rules or text."""
    return _grid(*_table_rules(rules, box, chars, pad), box, chars)


def _grid(h: list, v: list, box, chars: list[Char]) -> Lattice | None:
    """The lattice of a table's rule segments (as from `_table_rules`, or virtual ones)."""
    l, b, r, t = box
    inside = [c for c in chars if l - 0.5 <= c.cx <= r + 0.5 and b - 0.5 <= c.cy <= t + 0.5]
    if not (h or v) or not inside:
        return None
    xc = _clusters([x for x, _, _ in v])
    yc = sorted(_clusters([y for y, _, _ in h]), key=lambda c: -c[0])
    xs, x_span = [c[0] for c in xc], [(c[1], c[2]) for c in xc]
    ys, y_span = [c[0] for c in yc], [(c[1], c[2]) for c in yc]
    # Text outside the ruled grid (an open side, no outer frame): an implicit boundary.
    lo_x, hi_x = min(c.cx for c in inside), max(c.cx for c in inside)
    lo_y, hi_y = min(c.cy for c in inside), max(c.cy for c in inside)
    add_l, add_r = not xs or lo_x < xs[0] - 1, not xs or hi_x > xs[-1] + 1
    add_t, add_b = not ys or hi_y > ys[0] + 1, not ys or lo_y < ys[-1] - 1
    if add_l:
        xs, x_span = [min(l, min(c.l for c in inside)) - 1] + xs, [(0.0, 0.0)] + x_span
    if add_r:
        xs, x_span = xs + [max(r, max(c.r for c in inside)) + 1], x_span + [(0.0, 0.0)]
    if add_t:
        ys, y_span = [max(t, max(c.t for c in inside)) + 1] + ys, [(0.0, 0.0)] + y_span
    if add_b:
        ys, y_span = ys + [min(b, min(c.b for c in inside)) - 1], y_span + [(0.0, 0.0)]
    nc, nr = len(xs) - 1, len(ys) - 1
    if nc < 1 or nr < 1:
        return None
    lat = Lattice(xs, ys, x_span, y_span, ({0} if add_l else set()) | ({nc} if add_r else set()),
                  ({0} if add_t else set()) | ({nr} if add_b else set()), h, v)

    def vbound(j: int, i: int) -> bool:  # boundary xs[j] separates row i's slots
        return j in (0, nc) or lat.v_cover(j, ys[i + 1], ys[i]) >= 0.5

    def hbound(i: int, j: int) -> bool:  # boundary ys[i] separates column j's slots
        return i in (0, nr) or lat.h_cover(i, xs[j], xs[j + 1]) >= 0.5

    parent = {(i, j): (i, j) for i in range(nr) for j in range(nc)}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i in range(nr):
        for j in range(nc):
            if j + 1 < nc and not vbound(j + 1, i):
                parent[find((i, j + 1))] = find((i, j))
            if i + 1 < nr and not hbound(i + 1, j):
                parent[find((i + 1, j))] = find((i, j))
    groups: dict = defaultdict(list)
    for k in parent:
        groups[find(k)].append(k)
    for g in sorted(groups.values(), key=min):
        r0, r1 = min(i for i, _ in g), max(i for i, _ in g) + 1
        c0, c1 = min(j for _, j in g), max(j for _, j in g) + 1
        k = len(lat.cells)
        lat.cells.append((r0, c0, r1, c1))
        for i in range(r0, r1):
            for j in range(c0, c1):
                if (i, j) in lat.slot:
                    raise IrregularGrid  # a non-rectangular merged region: the rules do not form a grid
                lat.slot[(i, j)] = k
    return lat


class IrregularGrid(ValueError):
    """The ruling lines enclose a non-rectangular region: no lattice."""


# ---------------------------------------------------------------------------- locating TableFormer's words

@dataclass
class Token:
    text: str
    cell: int  # index into the table's non-empty cells
    k: int  # position within the cell's text
    norm: str
    chars: list = field(default_factory=list)  # indices into the page's chars
    box: tuple | None = None
    inherited: bool = False


def _bl(bbox: BoundingBox, page_h: float) -> tuple[float, float, float, float]:
    if bbox.coord_origin == CoordOrigin.TOPLEFT:
        return bbox.l, page_h - bbox.b, bbox.r, page_h - bbox.t
    return bbox.l, bbox.b, bbox.r, bbox.t


def _in(c: Char, box, pad: float) -> bool:
    l, b, r, t = box
    return l - pad <= c.cx <= r + pad and b - pad <= c.cy <= t + pad


def locate_tokens(cells: list[TableCell], page_h: float, chars: list[Char], table_box,
                  pad: float = 1.5) -> tuple[list[Token], float]:
    """TableFormer's words located in the text layer, and the share of their
    (normalised) characters found.

    Pass 1 aligns each cell's text with the unused text-layer characters inside
    its box (stream order); pass 2 looks for still unplaced words among the
    unused characters of the whole table, nearest to their cell; pass 3 gives a
    word found nowhere its neighbour's place.
    """
    tokens: list[Token] = []
    by_cell: dict[int, list[Token]] = defaultdict(list)
    for ci, cell in enumerate(cells):
        for k, w in enumerate(cell.text.split()):
            tk = Token(w, ci, k, _norm(w))
            tokens.append(tk)
            by_cell[ci].append(tk)
    in_table = [i for i, c in enumerate(chars) if _in(c, table_box, 2.0)]
    cnorm = {i: _norm(chars[i].ch) for i in in_table}
    idx = np.array(in_table, dtype=np.int64)
    cxs = np.array([chars[i].cx for i in in_table])
    cys = np.array([chars[i].cy for i in in_table])
    used: set[int] = set()
    for ci in sorted(by_cell, key=lambda ci: (cells[ci].start_row_offset_idx, cells[ci].start_col_offset_idx)):
        toks = [tk for tk in by_cell[ci] if tk.norm]
        if not toks or cells[ci].bbox is None:
            continue
        l, b, r, t = _bl(cells[ci].bbox, page_h)
        sel = idx[(cxs >= l - pad) & (cxs <= r + pad) & (cys >= b - pad) & (cys <= t + pad)]
        cand = [int(i) for i in sel if int(i) not in used]
        b_map = [i for i in cand for _ in cnorm[i]]
        b_str = "".join(cnorm[i] for i in cand)
        if not b_str:
            continue
        a_map = [tk for tk in toks for _ in tk.norm]
        sm = SequenceMatcher(None, "".join(tk.norm for tk in toks), b_str, autojunk=False)
        for ai, bi, n in sm.get_matching_blocks():
            for d in range(n):
                tk, i = a_map[ai + d], b_map[bi + d]
                if not tk.chars or tk.chars[-1] != i:
                    tk.chars.append(i)
                used.add(i)
    # Pass 2: words not found inside their own cell's box.
    stream = [(i, cnorm[i]) for i in in_table if cnorm[i]]
    starts: dict[str, list[int]] = defaultdict(list)
    for s, (_, ni) in enumerate(stream):
        starts[ni[0]].append(s)
    for tk in tokens:
        if tk.chars or not tk.norm:
            continue
        cell = cells[tk.cell]
        ref = _bl(cell.bbox, page_h) if cell.bbox is not None else table_box
        rx, ry = (ref[0] + ref[2]) / 2, (ref[1] + ref[3]) / 2
        best, best_d = None, None
        for s in starts.get(tk.norm[0], ()):
            got, idx, e = "", [], s
            while len(got) < len(tk.norm) and e < len(stream):
                j, nj = stream[e]
                if j in used:
                    break
                got += nj
                idx.append(j)
                e += 1
            if got != tk.norm:
                continue
            d = sum(abs(chars[j].cx - rx) + abs(chars[j].cy - ry) for j in idx) / len(idx)
            if best_d is None or d < best_d:
                best, best_d = idx, d
        if best:
            tk.chars = best
            used.update(best)
    total = sum(len(tk.norm) for tk in tokens)
    found = sum(min(len(tk.norm), sum(len(cnorm[i]) for i in tk.chars)) for tk in tokens)
    for tk in tokens:
        if tk.chars:
            cs = [chars[i] for i in tk.chars]
            tk.box = (min(c.l for c in cs), min(c.b for c in cs), max(c.r for c in cs), max(c.t for c in cs))
    # Pass 3: unplaced words follow their neighbour in the same cell.
    for ci, toks in by_cell.items():
        own = [tk for tk in toks if tk.box is not None]
        for n, tk in enumerate(toks):
            if tk.box is not None:
                continue
            src = next((x for x in reversed(toks[:n]) if x.box is not None and not x.inherited), None) \
                or next((x for x in toks[n + 1:] if x.box is not None and not x.inherited), None)
            if src is not None:
                tk.box, tk.chars = src.box, list(src.chars)
            elif not own and cells[ci].bbox is not None:
                tk.box = _bl(cells[ci].bbox, page_h)
            tk.inherited = True
    return tokens, (found / total if total else 1.0)


# ---------------------------------------------------------------------------- comparison and rebuild

def _lines(toks: list[Token]) -> list[list]:
    """Boxed words grouped into text lines [bottom, top, words], top to bottom, words
    left to right. A word joins a line it overlaps vertically by at least half of the
    smaller height, so raised footnote markers stay on their line."""
    lines: list[list] = []  # [bottom, top, words]
    for tk in sorted((tk for tk in toks if tk.box is not None), key=lambda tk: -(tk.box[1] + tk.box[3])):
        b, t = tk.box[1], tk.box[3]
        for line in lines:
            if min(t, line[1]) - max(b, line[0]) >= 0.5 * max(min(t - b, line[1] - line[0]), 0.1):
                line[2].append(tk)
                line[0], line[1] = min(line[0], b), max(line[1], t)
                break
        else:
            lines.append([b, t, [tk]])
    lines.sort(key=lambda ln: -(ln[0] + ln[1]) / 2)
    return [[*ln[:2], sorted(ln[2], key=lambda tk: (tk.box[0], tk.cell, tk.k))] for ln in lines]


def _reading_order(toks: list[Token]) -> list[Token]:
    """Words in reading order: lines top to bottom, left to right; words without a
    place go last in their original order."""
    out = [tk for ln in _lines(toks) for tk in ln[2]]
    return out + [tk for tk in toks if tk.box is None]


def _block_text(toks: list[Token], gap_factor: float, boundaries: list[float]) -> str | None:
    """The words as vertically separated text blocks joined by newlines, or None.

    A block ends where the empty space between two consecutive lines is at
    least `gap_factor` times the median line height and contains one of
    `boundaries` (row rules of the other columns that this cell spans): a
    note sitting in a lower header row of a cell that spans several, not a
    title wrapped over widely spaced lines.
    """
    if len(toks) < 2 or not boundaries or any(tk.box is None or tk.inherited for tk in toks):
        return None
    lines = _lines(toks)
    if len(lines) < 2:
        return None
    height = median(ln[1] - ln[0] for ln in lines)
    blocks, cur = [], [lines[0]]
    for prev, ln in zip(lines, lines[1:]):
        if prev[0] - ln[1] >= gap_factor * height and any(ln[1] < y < prev[0] for y in boundaries):
            blocks.append(cur)
            cur = []
        cur.append(ln)
    blocks.append(cur)
    if len(blocks) < 2:
        return None
    return "\n".join(" ".join(tk.text for ln in blk for tk in ln[2]) for blk in blocks)


def _vote(lat: Lattice, tk: Token, chars: list[Char]) -> int | None:
    """Lattice cell holding most of a word's characters."""
    if tk.chars:
        return Counter(lat.locate(chars[i].cx, chars[i].cy) for i in tk.chars).most_common(1)[0][0]
    if tk.box is not None:
        return lat.locate((tk.box[0] + tk.box[2]) / 2, (tk.box[1] + tk.box[3]) / 2)
    return None


def _compaction(spans) -> tuple[list[int], list[int]]:
    """Rows and columns where some cell starts (others hold no text of their own)."""
    return sorted({s[0] for s in spans}), sorted({s[1] for s in spans})


def _compact(span, rows: list[int], cols: list[int]) -> tuple[int, int, int, int]:
    r0, c0, r1, c1 = span
    return rows.index(r0), cols.index(c0), sum(1 for r in rows if r < r1), sum(1 for c in cols if c < c1)


def rule_coverage(cells: list[TableCell], page_h: float, lat: Lattice) -> tuple[float, int]:
    """Share of pairs of adjacent TableFormer cells that a ruling line separates, and
    the number of pairs. A rule separates A (left of / above B) from B when it lies
    between their centres and covers at least half of their common extent."""
    col_ruled, col_total, row_ruled, row_total = _pair_coverage(cells, page_h, lat)
    total = col_total + row_total
    return ((col_ruled + row_ruled) / total if total else 0.0), total


def _pair_coverage(cells: list[TableCell], page_h: float, lat: Lattice) -> tuple[int, int, int, int]:
    """Ruled and total pairs of adjacent TableFormer cells (as in `rule_coverage`): side by
    side (a vertical rule between them), then stacked (a horizontal rule between them).
    The stacked share is the row-ruled mode's row_rule_coverage."""
    slot: dict = {}
    for ci, c in enumerate(cells):
        for i in range(c.start_row_offset_idx, c.end_row_offset_idx):
            for j in range(c.start_col_offset_idx, c.end_col_offset_idx):
                slot.setdefault((i, j), ci)
    pairs = set()
    for (i, j), a in slot.items():
        for ni, nj, kind in ((i, j + 1, "h"), (i + 1, j, "v")):
            b_ = slot.get((ni, nj))
            if b_ is not None and b_ != a:
                pairs.add((a, b_, kind))
    boxes = {ci: _bl(c.bbox, page_h) for ci, c in enumerate(cells) if c.bbox is not None}
    n = {"h": [0, 0], "v": [0, 0]}  # kind -> [ruled, total]
    for a, b_, kind in pairs:
        if a not in boxes or b_ not in boxes:
            continue
        A, B = boxes[a], boxes[b_]
        n[kind][1] += 1
        if kind == "h":  # A left of B: a vertical rule between their centres
            c_lo, c_hi = sorted(((A[0] + A[2]) / 2, (B[0] + B[2]) / 2))
            lo, hi = max(A[1], B[1]), min(A[3], B[3])
            if hi - lo < 1:
                lo, hi = min(A[3], B[3]), max(A[1], B[1])
            n[kind][0] += any(c_lo < x < c_hi and lat.v_cover(j, lo, hi) >= 0.5 for j, x in enumerate(lat.xs))
        else:  # A above B: a horizontal rule between their centres
            c_lo, c_hi = sorted(((A[1] + A[3]) / 2, (B[1] + B[3]) / 2))
            lo, hi = max(A[0], B[0]), min(A[2], B[2])
            if hi - lo < 1:
                lo, hi = min(A[2], B[2]), max(A[0], B[0])
            n[kind][0] += any(c_lo < y < c_hi and lat.h_cover(i, lo, hi) >= 0.5 for i, y in enumerate(lat.ys))
    return n["h"][0], n["h"][1], n["v"][0], n["v"][1]


def _rebuild(table: TableItem, cells: list[TableCell], lat: Lattice, by_l: dict[int, list[Token]],
             page_h: float) -> tuple[TableData, list[tuple[TableCell, list[Token], int]]]:
    """TableData from the lattice cells holding words; also each new cell with its words
    and lattice cell."""
    rows, cols = _compaction([lat.cells[k] for k in by_l])
    n_cols = len(cols)
    keys = sorted(by_l, key=lambda k: lat.cells[k][:2])
    spans = {k: _compact(lat.cells[k], rows, cols) for k in keys}
    sources = {k: Counter(tk.cell for tk in by_l[k]) for k in keys}

    def share(k: int, flag: str) -> bool:
        return 2 * sum(n for ci, n in sources[k].items() if getattr(cells[ci], flag)) > sum(sources[k].values())

    # Header band: leading rows whose cells (row-label column aside) are mostly headers, up to
    # the first full-width (section) row. A cell starting there that holds any header word is a
    # header (e.g. a header cell TableFormer split in two, leaving the lower half unflagged);
    # cells below the band are not (TableFormer sometimes flags whole SoA bodies).
    band: set[int] = set()
    regular = False  # a header row that is not a full-width title has been seen
    for r in range(len(rows)):
        full = [k for k in keys if spans[k][0] == r and spans[k][1] == 0 and spans[k][3] == n_cols > 1]
        if full:  # a title row on top of the header, or a section row below it
            if regular or not share(full[0], "column_header"):
                break
            band.add(r)
            continue
        starting = [k for k in keys if spans[k][0] == r and spans[k][1] > 0]
        if not starting or 2 * sum(share(k, "column_header") for k in starting) <= len(starting):
            break
        band.add(r)
        regular = True
    out, pairs = [], []
    for k in keys:
        toks = by_l[k]
        src = sources[k]
        whole = len(src) == 1 and next(iter(src.values())) == len(cells[next(iter(src))].text.split())
        text = cells[next(iter(src))].text if whole else " ".join(tk.text for tk in _reading_order(toks))
        r0, c0, r1, c1 = spans[k]
        header = r0 in band and (share(k, "column_header") or any(cells[ci].column_header for ci in src))
        l, b, r, t = lat.rect(k)
        cell = TableCell(
            text=text, start_row_offset_idx=r0, end_row_offset_idx=r1, start_col_offset_idx=c0,
            end_col_offset_idx=c1, row_span=r1 - r0, col_span=c1 - c0, column_header=header,
            row_header=share(k, "row_header"),
            row_section=share(k, "row_section") or (not header and c0 == 0 and c1 == n_cols and n_cols > 1),
            bbox=BoundingBox(l=l, t=page_h - t, r=r, b=page_h - b, coord_origin=CoordOrigin.TOPLEFT))
        out.append(cell)
        pairs.append((cell, toks, k))
    data = TableData(num_rows=len(rows), num_cols=n_cols, table_cells=out)
    data.orientation = table.data.orientation
    return data, pairs


def _split_header_notes(pairs: list[tuple[TableCell, list[Token], int | None]], lat: Lattice,
                        gap: float | None) -> int:
    """Newline between the text blocks of header cells that span several lattice rows,
    where the blocks lie on either side of a row rule of the other columns (same words,
    same order). pairs: (cell, its words, its lattice cell)."""
    if not gap:
        return 0
    split = 0
    for cell, toks, k in pairs:
        if not cell.column_header or k is None:
            continue
        r0, _, r1, _ = lat.cells[k]
        text = _block_text(toks, gap, [lat.ys[i] for i in range(r0 + 1, r1)])
        if text and text.split() == cell.text.split():
            cell.text = text
            split += 1
    return split


# ---------------------------------------------------------------------------- row-ruled tables

def _rule_breaks(h_raw: list, box, pad: float = 4.0, max_gap: float = 6.0, tol: float = 0.6,
                 x_tol: float = 1.0) -> list[tuple[float, int]]:
    """Where horizontal rules are broken (Word draws borders cell by cell): (x, number of
    rules broken there), for break positions recurring on at least two rules of the box.
    h_raw: the page's horizontal segments before collinear pieces are joined."""
    l, b, r, t = box
    segs = sorted((y, x0, x1) for y, x0, x1 in h_raw
                  if b - pad <= y <= t + pad and x1 > l - pad and x0 < r + pad)
    groups: list[list] = []
    for s in segs:
        if groups and abs(s[0] - groups[-1][0][0]) <= tol:
            groups[-1].append(s)
        else:
            groups.append([s])
    found: list[tuple[float, int]] = []  # (x, rule index)
    for gi, g in enumerate(groups):
        g = sorted(g, key=lambda s: s[1])
        end = g[0][2]
        for s in g[1:]:
            if -1.0 <= s[1] - end <= max_gap:
                found.append(((end + s[1]) / 2, gi))
            end = max(end, s[2])
    out: list[list] = []  # [x sum, n, rule set]
    for x, gi in sorted(found):
        if out and x - out[-1][0] / out[-1][1] <= x_tol:
            out[-1][0] += x
            out[-1][1] += 1
            out[-1][2].add(gi)
        else:
            out.append([x, 1, {gi}])
    return [(sx / n, len(rs)) for sx, n, rs in out if len(rs) >= 2]


def _band(ys: list, y: float) -> int:
    """Lattice row band (index into the rows of `ys`, descending boundaries) holding height y."""
    return sum(1 for yb in ys[1:-1] if y <= yb)


def _virtual_columns(cells: list[TableCell], tokens: list[Token], ys: list, breaks: list,
                     min_gap: float = 1.0) -> tuple[list, dict] | str:
    """Virtual vertical rules for a table ruled with horizontal lines only: TableFormer's
    column boundaries, each placed in the whitespace between the words of the columns on
    either side, as (x, y0, y1) segments over the row bands `ys` (lattice row boundaries,
    descending). A boundary is kept open in the bands holding words of a TableFormer cell
    that spans it. Returns (segments, counts), or the reason the columns cannot be drawn:
    "columns_not_separable" (the words left of a boundary are not all left of the words
    right of it) or "span_conflict" (a band holds a spanning cell and cells ending or
    starting at the boundary it spans: header levels without a rule between them)."""
    spans = [(c.start_row_offset_idx, c.start_col_offset_idx, c.end_row_offset_idx, c.end_col_offset_idx)
             for c in cells]
    rows, cols = _compaction(spans)
    comp = [_compact(s, rows, cols) for s in spans]
    placed = [tk for tk in tokens if tk.box is not None and not tk.inherited]
    segs: list = []
    n_breaks = n_open = 0
    for j in range(1, len(cols)):
        left = [tk.box[2] for tk in placed if comp[tk.cell][3] <= j]
        right = [tk.box[0] for tk in placed if comp[tk.cell][1] >= j]
        if not left or not right:
            return "columns_not_separable"
        lo, hi = max(left), min(right)
        if hi - lo < min_gap:
            return "columns_not_separable"
        inside = [(n, x) for x, n in breaks if lo < x < hi]
        if inside:
            x = max(inside, key=lambda nx: (nx[0], -abs(nx[1] - (lo + hi) / 2)))[1]
            n_breaks += 1
        else:
            x = (lo + hi) / 2
        bands = lambda which: {_band(ys, (tk.box[1] + tk.box[3]) / 2)  # noqa: E731
                               for tk in tokens if tk.box is not None and which(comp[tk.cell])}
        open_ = bands(lambda c: c[1] < j < c[3])
        if open_ & bands(lambda c: c[3] == j or c[1] == j):
            return "span_conflict"
        n_open += len(open_)
        for i in range(len(ys) - 1):
            if i not in open_:
                segs.append((x, ys[i + 1], ys[i]))
    return _merge_collinear(segs), {"virtual_boundaries": len(cols) - 1, "boundaries_at_rule_breaks": n_breaks,
                                    "span_bands_open": n_open}


# ---------------------------------------------------------------------------- driver

class PageCache:
    """Per-page characters, rules and frame, computed once."""

    def __init__(self, pdf: pdfium.PdfDocument):
        self.pdf = pdf
        self._data: dict[int, tuple] = {}

    def __call__(self, page_no: int) -> tuple[list[Char], Rules, PageFrame]:
        if page_no not in self._data:
            page = self.pdf[page_no - 1]
            frame = PageFrame(page)
            self._data[page_no] = (page_chars(page, frame), page_rules(page, frame), frame)
        return self._data[page_no]


def _compare(lat: Lattice, tokens: list[Token], cells: list[TableCell], chars: list[Char]) -> tuple[dict, dict, dict]:
    """TableFormer's grid against a lattice: counts (crossing, fused, outside, span
    differences, ...), and the words' lattice cells: TableFormer cell -> Counter of lattice
    cells, lattice cell -> its words."""
    assign = {id(tk): _vote(lat, tk, chars) for tk in tokens}
    tf_map: dict[int, Counter] = defaultdict(Counter)
    by_l: dict[int, list[Token]] = defaultdict(list)
    for tk in tokens:
        k = assign[id(tk)]
        if k is not None:
            tf_map[tk.cell][k] += 1
            by_l[k].append(tk)
    outside = sum(1 for tk in tokens if assign[id(tk)] is None)
    crossing = sum(1 for m in tf_map.values() if len(m) > 1)
    fused = sum(1 for toks in by_l.values() if len({tk.cell for tk in toks}) > 1)
    tf_spans = [(c.start_row_offset_idx, c.start_col_offset_idx, c.end_row_offset_idx, c.end_col_offset_idx)
                for c in cells]
    t_rows, t_cols = _compaction(tf_spans)
    l_rows, l_cols = _compaction([lat.cells[k] for k in by_l]) if by_l else ([], [])
    span_diff = 0
    if not crossing and not fused and not outside:
        for ci, m in tf_map.items():
            span_diff += _compact(tf_spans[ci], t_rows, t_cols) != _compact(lat.cells[next(iter(m))], l_rows, l_cols)
    exact = sum(1 for toks in by_l.values()
                if len({tk.cell for tk in toks}) == 1 and len(tf_map[toks[0].cell]) == 1)
    stats = dict(tf_rows=len(t_rows), tf_cols=len(t_cols), lat_rows=len(l_rows), lat_cols=len(l_cols),
                 tf_cells=len(cells), lattice_cells=len(by_l), exact_cells=exact,
                 tf_cells_crossing_rule=crossing, lattice_cells_fused=fused, words_outside=outside,
                 span_differences=span_diff)
    return stats, tf_map, by_l


def _agrees(stats: dict) -> bool:
    return not (stats["tf_cells_crossing_rule"] or stats["lattice_cells_fused"] or stats["words_outside"]
                or stats["span_differences"])


def _verified(entry: dict, cells: list[TableCell], tokens: list[Token], tf_map: dict, lat: Lattice,
              note_gap: float | None) -> None:
    entry["action"] = "verified_ok"
    own = [(c, [tk for tk in tokens if tk.cell == ci], next(iter(tf_map[ci]), None))
           for ci, c in enumerate(cells) if c.column_header]
    entry["header_notes_split"] = _split_header_notes(own, lat, note_gap)


def _correct(entry: dict, table: TableItem, cells: list[TableCell], tokens: list[Token], lat: Lattice,
             by_l: dict, page_h: float, note_gap: float | None) -> None:
    new, new_pairs = _rebuild(table, cells, lat, by_l, page_h)
    before = Counter(tk.text for tk in tokens)
    after = Counter(w for c in new.table_cells for w in c.text.split())
    if before != after:
        entry.update(action="skipped", reason="word_multiset_mismatch")
    else:
        table.data = new
        entry.update(action="corrected", rows_after=new.num_rows, cols_after=new.num_cols,
                     header_notes_split=_split_header_notes(new_pairs, lat, note_gap))


def check_table(table: TableItem, doc: DoclingDocument, pages: PageCache, mode: str = "correct",
                min_rule_coverage: float = 0.9, min_text_match: float = 0.9,
                header_note_gap: float | None = 1.0, *, row_ruled: bool = True,
                min_row_rule_coverage: float = 0.9, max_col_rule_coverage: float = 0.1) -> dict:
    """Verify (and in mode "correct" possibly rebuild) one single-page table; returns its
    log entry (counts, action, reason; no text).

    row_ruled: a table that is not fully ruled but has horizontal rules and (almost) no
    vertical ones (at most `max_col_rule_coverage` of side-by-side cell pairs separated by a
    vertical rule) is checked against a row-ruled lattice: rows from the horizontal rules,
    columns from TableFormer placed in the whitespace between the columns' words
    (`_virtual_columns`), and rebuilt when at least `min_row_rule_coverage` of the stacked
    cell pairs are separated by a rule. Such entries carry lattice_mode "rows"."""
    entry: dict = {"table": table, "page": table.prov[0].page_no if table.prov else None,
                   "rows_before": table.data.num_rows, "cols_before": table.data.num_cols}
    if len(table.prov) != 1:
        return {**entry, "action": "skipped", "reason": "not_single_page"}
    cells = [c for c in table.data.table_cells if c.text.strip()]
    if not cells:
        return {**entry, "action": "skipped", "reason": "no_text"}
    if any(c.bbox is None for c in cells):
        return {**entry, "action": "skipped", "reason": "cells_without_box"}
    pno = table.prov[0].page_no
    page_h = doc.pages[pno].size.height
    chars, rules, _ = pages(pno)
    box = _bl(table.prov[0].bbox, page_h)
    tokens, matched = locate_tokens(cells, page_h, chars, box)
    entry["text_match"] = round(matched, 4)
    note_gap = header_note_gap if mode == "correct" else None
    try:
        lat = build_lattice(rules, box, chars)
    except IrregularGrid:
        entry.update(action="skipped", reason="irregular_grid")
        return entry
    if lat is None:
        entry.update(action="skipped", reason="no_rules")
        return entry
    col_ruled, col_pairs, row_ruled_pairs, row_pairs = _pair_coverage(cells, page_h, lat)
    n_pairs = col_pairs + row_pairs
    coverage = (col_ruled + row_ruled_pairs) / n_pairs if n_pairs else 0.0
    entry.update(lattice_rows=lat.n_rows, lattice_cols=lat.n_cols, rule_coverage=round(coverage, 3),
                 adjacent_pairs=n_pairs)
    if matched < min_text_match:
        entry.update(action="skipped", reason="text_layer_mismatch")
        return entry
    stats, tf_map, by_l = _compare(lat, tokens, cells, chars)
    entry.update(stats)
    if _agrees(stats):
        _verified(entry, cells, tokens, tf_map, lat, note_gap)
    elif stats["words_outside"]:
        entry.update(action="skipped", reason="words_outside_lattice")
    elif coverage < min_rule_coverage:
        col_cov = col_ruled / col_pairs if col_pairs else 0.0
        entry.update(action="disagree_not_ruled", col_rule_coverage=round(col_cov, 3))
        if row_ruled and row_pairs and col_cov <= max_col_rule_coverage:
            _check_rows(entry, table, cells, tokens, chars, rules, box, lat, page_h, mode, note_gap,
                        row_ruled_pairs / row_pairs, row_pairs, min_row_rule_coverage)
    elif mode == "verify":
        entry["action"] = "disagree"
    else:
        _correct(entry, table, cells, tokens, lat, by_l, page_h, note_gap)
    return entry


def _check_rows(entry: dict, table: TableItem, cells: list[TableCell], tokens: list[Token], chars: list[Char],
                rules: Rules, box, lat: Lattice, page_h: float, mode: str, note_gap: float | None,
                row_cov: float, row_pairs: int, min_row_rule_coverage: float) -> None:
    """Row-ruled mode of `check_table` (see there): updates the entry, rebuilds the table
    in mode "correct". `lat` is the lattice of the real rules (rows only, in effect)."""
    entry.update(lattice_mode="rows", row_rule_coverage=round(row_cov, 3), row_pairs=row_pairs)
    if row_cov < min_row_rule_coverage:
        return  # booktabs, rules between row groups only, or TableFormer rows not ruled: stays disagree_not_ruled
    # Text above the top rule or below the bottom rule (no rule there): the rules say nothing
    # about its rows, so TableFormer rows there must not be merged.
    edge_bands = ([0] if 0 in lat.implicit_y else []) + ([lat.n_rows - 1] if lat.n_rows in lat.implicit_y else [])
    for i in edge_bands:
        tf_rows = {cells[tk.cell].start_row_offset_idx for tk in tokens
                   if tk.box is not None and _band(lat.ys, (tk.box[1] + tk.box[3]) / 2) == i}
        if len(tf_rows) > 1:
            entry.update(action="skipped", reason="rows_outside_rules")
            return
    cols = _virtual_columns(cells, tokens, lat.ys, _rule_breaks(rules.h, box))
    if isinstance(cols, str):
        entry.update(action="skipped", reason=cols)
        return
    segs, info = cols
    entry.update(info)
    try:
        rlat = _grid(lat.h, segs, box, chars)
    except IrregularGrid:
        entry.update(action="skipped", reason="irregular_grid")
        return
    if rlat is None:
        entry.update(action="skipped", reason="no_rules")
        return
    stats, tf_map, by_l = _compare(rlat, tokens, cells, chars)
    entry.update(stats, lattice_rows=rlat.n_rows, lattice_cols=rlat.n_cols)
    if _agrees(stats):
        _verified(entry, cells, tokens, tf_map, rlat, note_gap)
    elif stats["words_outside"]:
        entry.update(action="skipped", reason="words_outside_lattice")
    elif mode == "verify":
        entry["action"] = "disagree"
    else:
        _correct(entry, table, cells, tokens, rlat, by_l, page_h, note_gap)


def correct_tables_with_lattice(doc: DoclingDocument, pdf: pdfium.PdfDocument, log: dict | None = None,
                                mode: str = "correct", *, min_rule_coverage: float = 0.9,
                                min_text_match: float = 0.9, header_note_gap: float | None = 1.0,
                                row_ruled: bool = True, min_row_rule_coverage: float = 0.9,
                                max_col_rule_coverage: float = 0.1) -> dict:
    """Verify every single-page table fragment against the PDF's ruling lines and, in
    mode "correct", rebuild disagreeing fully ruled ones from the lattice.

    Run on the raw per-page fragments, before tables are merged across pages.
    mode "verify" logs only and changes nothing; "correct" also rebuilds and
    splits header notes (header_note_gap=None turns that off). row_ruled: also
    check (and rebuild) tables ruled with horizontal lines only, rows from the
    rules and columns from TableFormer (see `check_table`). `log` (optional)
    receives one entry per table under "table_lattice": the table object (the
    caller turns it into a ref), page, action (verified_ok / corrected /
    disagree_not_ruled / disagree (verify mode) / skipped), reason, lattice_mode
    ("rows" for the row-ruled check) and counts, never text. Returns counts per
    action and skip reason; "corrected" includes row-ruled corrections, which are
    also counted under "corrected_rows" (likewise "verified_ok_rows" etc.).
    """
    if mode not in ("verify", "correct"):
        raise ValueError(f"unknown lattice mode: {mode}")
    pages = PageCache(pdf)
    entries = log.setdefault("table_lattice", []) if log is not None else []
    counts: Counter = Counter()
    for table in doc.tables:
        if not table.prov:
            continue
        try:
            entry = check_table(table, doc, pages, mode, min_rule_coverage, min_text_match, header_note_gap,
                                row_ruled=row_ruled, min_row_rule_coverage=min_row_rule_coverage,
                                max_col_rule_coverage=max_col_rule_coverage)
        except Exception as exc:  # noqa: BLE001 - one odd table must not stop the conversion
            entry = {"table": table, "page": table.prov[0].page_no, "action": "skipped",
                     "reason": f"error:{type(exc).__name__}"}
        entries.append(entry)
        counts["tables"] += 1
        counts[entry["action"]] += 1
        if entry["action"] == "skipped":
            counts["skipped:" + entry["reason"]] += 1
        if entry.get("lattice_mode") == "rows":
            counts[entry["action"] + "_rows"] += 1
        counts["header_notes_split"] += entry.get("header_notes_split", 0)
    return dict(counts)
