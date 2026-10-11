"""Merge tables that Docling emits as separate items because they span pages.

Protocols are full of long tables (amendment history, Schedule of Activities,
lab panels, TOC) that the layout model sees once per page. Two tables are
merged when the second immediately follows the first in reading order, starts
on the next page, has the same label and column count, and either repeats the
same column header or has none (then the first must end its page and the second
start its page). Every candidate decision is logged (merged / rejected + reason).

Column counts may differ only when header alignment shows one page split a
column in two (collapsed, logged "split_cols:<reason>"). A column missing on
one page is not merged ("cols_differ"); continuation_chains() reports such
fragments so they can be retrieved together.

When appending a continuation page, repeated header rows are dropped, then a
full-width section row the PDF repeats under the header ("Study Intervention:
X" again at the top of the page) is dropped too, so the cut-row join sees the
real first row. mark_inner_header_rows() flags stacked header blocks inside a
table (a body row repeating the header's row-label cell starts a new block,
e.g. a second band of column labels), so serializers switch column names there.
"""

from collections import Counter
from difflib import SequenceMatcher

from docling_core.types.doc import (
    BoundingBox,
    ContentLayer,
    DoclingDocument,
    PictureItem,
    ProvenanceItem,
    RefItem,
    TableCell,
    TableItem,
)

from protocol_settings import ProtocolSettings

# A continuation cell that continues one of these is the tail of a multi-line
# text cell, not a new short-valued sub-row ("X", "g/dL", "Cycle 1").
_LONG_CELL_CHARS = 25
# A continuation page's header row is a repeat when this share of its words is in the
# previous page's header.
_REPEAT_SHARE = 0.9


def _header_rows(table: TableItem) -> int:
    """Number of leading rows made only of column-header cells."""
    rows: dict[int, list[TableCell]] = {}
    for c in table.data.table_cells:
        rows.setdefault(c.start_row_offset_idx, []).append(c)
    n = 0
    while n in rows and all(c.column_header for c in rows[n]):
        n = max(c.end_row_offset_idx for c in rows[n])
    return n


def _header_text(table: TableItem, n_rows: int) -> str:
    """Header text in reading order with all whitespace removed.

    TableFormer may split the same header into cells slightly differently on
    each page (e.g. a note glued onto a header cell), so cell boundaries are
    ignored and only the text sequence is compared.
    """
    cells = sorted(
        (c.start_row_offset_idx, c.start_col_offset_idx, c.text)
        for c in table.data.table_cells
        if c.start_row_offset_idx < n_rows
    )
    return "".join("".join(t.split()) for _, _, t in cells)


def _same_header(a: str, b: str) -> bool:
    # Pages may tag a different number of rows as header (e.g. the SoA's
    # "Visit Window" row), so compare on the common prefix.
    n = min(len(a), len(b))
    if n < 10:
        return a == b
    return SequenceMatcher(None, a[:n], b[:n]).ratio() >= 0.9


def _header_words(table: TableItem, n_rows: int) -> Counter:
    return Counter(
        w for c in table.data.table_cells if c.start_row_offset_idx < n_rows
        for w in c.text.split()
    )


def _same_header_words(a: Counter, b: Counter) -> bool:
    """Order-insensitive match: TableFormer may order header cells differently."""
    small = min(sum(a.values()), sum(b.values()))
    return small >= 3 and sum((a & b).values()) / small >= 0.9


def _row_key(table: TableItem, row: int) -> str:
    cells = sorted((c.start_col_offset_idx, c.text) for c in _row_cells(table, row))
    return "".join("".join(t.split()) for _, t in cells)


def _row_cells(table: TableItem, row: int) -> list[TableCell]:
    return [c for c in table.data.table_cells if c.start_row_offset_idx == row]


def _full_width_text(table: TableItem, row: int) -> str | None:
    """Normalised text of a section row: one cell starting on `row` that spans every column."""
    cells = [c for c in _row_cells(table, row) if c.text.strip()]
    if (table.data.num_cols > 1 and len(cells) == 1 and cells[0].start_col_offset_idx == 0
            and cells[0].end_col_offset_idx >= table.data.num_cols):
        return "".join(cells[0].text.split()).lower()
    return None


def _bl(doc: DoclingDocument, prov: ProvenanceItem) -> BoundingBox:
    return prov.bbox.to_bottom_left_origin(doc.pages[prov.page_no].size.height)


class PageBoxes:
    """Body-layer boxes per page, to tell whether an item ends or starts its page.

    Built once per pass; children of pictures (OCR fragments) and furniture are
    ignored. Boxes equal to the tested box (the item itself, or a fragment
    already merged into it) are skipped. The body area of a page size is the
    lowest bottom / highest top of any body item on pages of that size; a box
    "ends" its page when nothing lies below it and it reaches within
    EDGE_TOL of the page height of that bottom (a table ending mid-page with
    blank space below does not), and likewise for "starts".
    """

    EDGE_TOL = 0.2  # rows that may not break across pages leave a gap (here up to ~15 %)

    def __init__(self, doc: DoclingDocument):
        self.doc = doc
        self.by_page: dict[int, list[BoundingBox]] = {}
        self.area: dict[float, tuple[float, float]] = {}  # page height -> (body bottom, body top)
        for it in [*doc.texts, *doc.tables, *doc.pictures]:
            if getattr(it, "content_layer", None) != ContentLayer.BODY or not it.prov:
                continue
            if it.parent and isinstance(it.parent.resolve(doc), PictureItem):
                continue
            for p in it.prov:
                b = _bl(doc, p)
                self.by_page.setdefault(p.page_no, []).append(b)
                h = doc.pages[p.page_no].size.height
                lo, hi = self.area.get(h, (b.b, b.t))
                self.area[h] = (min(lo, b.b), max(hi, b.t))

    def _others(self, prov: ProvenanceItem):
        box = _bl(self.doc, prov)
        h = self.doc.pages[prov.page_no].size.height
        return box, h, [b for b in self.by_page.get(prov.page_no, []) if b != box]

    def ends_page(self, prov: ProvenanceItem) -> bool:
        """Nothing in the body lies below this box, and it reaches the body bottom."""
        box, h, others = self._others(prov)
        return (not any((b.t + b.b) / 2 < box.b for b in others)
                and box.b - self.area[h][0] <= self.EDGE_TOL * h)

    def starts_page(self, prov: ProvenanceItem) -> bool:
        """Nothing in the body lies above this box, and it reaches the body top."""
        box, h, others = self._others(prov)
        return (not any((b.t + b.b) / 2 > box.t for b in others)
                and self.area[h][1] - box.t <= self.EDGE_TOL * h)


def _continuation_reason(prev: TableItem, nxt: TableItem) -> str | None:
    """Why nxt continues prev (header_text / header_words / no_header), else None."""
    h_prev, h_next = _header_rows(prev), _header_rows(nxt)
    if h_next == 0:
        return "no_header"
    if _same_header(_header_text(prev, h_prev), _header_text(nxt, h_next)):
        return "header_text"
    if _same_header_words(_header_words(prev, h_prev), _header_words(nxt, h_next)):
        return "header_words"
    return None


_MAX_GROUP = 4  # widest run of columns one header may be split into
_INDEL = 1000  # alignment cost of a column missing on one side


def _norm(s: str) -> str:
    return "".join(s.split()).lower()


def _fuzzy_eq(a: str, b: str) -> bool:
    """Header strings equal up to a char or two (footnote markers)."""
    if a == b:
        return True
    if not a or not b:
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.9


def _group_text(table: TableItem, n_rows: int, lo: int, hi: int) -> str | None:
    """Normalised header text over columns [lo, hi): each header cell touching the
    range once, in (row, col) order. None if some column has no header text
    (a column with an empty header cannot prove it is half of a split one)."""
    cells = [c for c in table.data.table_cells
             if c.start_row_offset_idx < n_rows and c.start_col_offset_idx < hi and c.end_col_offset_idx > lo]
    if hi - lo > 1:
        for col in range(lo, hi):
            if not any(c.start_col_offset_idx <= col < c.end_col_offset_idx and _norm(c.text) for c in cells):
                return None
    cells.sort(key=lambda c: (c.start_row_offset_idx, c.start_col_offset_idx))
    return "".join(_norm(c.text) for c in cells)


_SPLIT_LEN_TOL = 3  # characters the joined header texts of a split run may differ by


def _own_headers(table: TableItem, n_rows: int, lo: int, hi: int) -> bool:
    """Every column of [lo, hi) has header text of its own (more than one column).

    A header cell spanning several columns of the run is a group header over
    distinct columns (seen: two visit columns under one header, filled in the
    same rows), not one column TableFormer split in two.
    """
    if hi - lo == 1:
        return True
    cells = [c for c in table.data.table_cells
             if c.start_row_offset_idx < n_rows and c.start_col_offset_idx < hi and c.end_col_offset_idx > lo]
    if any(c.start_col_offset_idx < hi and c.end_col_offset_idx > lo and c.col_span > 1
           and max(c.start_col_offset_idx, lo) + 1 < min(c.end_col_offset_idx, hi) for c in cells):
        return False
    return all(any(c.start_col_offset_idx == col and c.col_span == 1 and _norm(c.text) for c in cells)
               for col in range(lo, hi))


def _column_alignment(a: TableItem, b: TableItem) -> list[tuple[int, int, int, int]] | None:
    """Align a's and b's columns by per-column header text.

    Returns the column groups [(a_lo, a_k, b_lo, b_l)] of a minimal-cost
    alignment made only of fuzzy-equal single columns and runs whose joined
    header texts are fuzzy-equal (a column TableFormer split in two on one
    page, or a shifted boundary). None when a column exists on one side only
    (or there are no header rows to compare).
    """
    n_rows = min(_header_rows(a), _header_rows(b))
    if n_rows == 0:
        return None
    m, n = a.data.num_cols, b.data.num_cols
    ta: dict[tuple[int, int], str | None] = {}
    tb: dict[tuple[int, int], str | None] = {}

    def txt(cache, table, lo, k):
        if (lo, k) not in cache:
            cache[(lo, k)] = _group_text(table, n_rows, lo, lo + k)
        return cache[(lo, k)]

    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(m + 1)]
    back: list[list[tuple | None]] = [[None] * (n + 1) for _ in range(m + 1)]
    cost[0][0] = 0
    for i in range(m + 1):
        for j in range(n + 1):
            c0 = cost[i][j]
            if c0 == inf:
                continue
            steps = [(1, 0, _INDEL), (0, 1, _INDEL)]
            steps += [(k, l, k + l - 2) for k in range(1, _MAX_GROUP + 1) for l in range(1, _MAX_GROUP + 1)]
            for k, l, w in steps:
                if i + k > m or j + l > n:
                    continue
                if k and l:
                    x, y = txt(ta, a, i, k), txt(tb, b, j, l)
                    if x is None or y is None or not _fuzzy_eq(x, y):
                        continue
                    # A split run must account for every character: the ratio alone lets a
                    # short missing column hide next to a long header (seen: 18 of ~560 chars).
                    if k != l and (abs(len(x) - len(y)) > _SPLIT_LEN_TOL
                                   or not _own_headers(a, n_rows, i, i + k)
                                   or not _own_headers(b, n_rows, j, j + l)):
                        continue
                if c0 + w < cost[i + k][j + l]:
                    cost[i + k][j + l] = c0 + w
                    back[i + k][j + l] = (i, j, k, l)
    if cost[m][n] >= _INDEL:
        return None
    groups, i, j = [], m, n
    while (i, j) != (0, 0):
        pi, pj, k, l = back[i][j]
        groups.append((pi, k, pj, l))
        i, j = pi, pj
    return groups[::-1]


def _split_ranges(groups: list[tuple[int, int, int, int]]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Column ranges [lo, hi) to collapse in a and in b: runs of unequal width."""
    ra = [(i, i + k) for i, k, j, l in groups if k != l and k > 1]
    rb = [(j, j + l) for i, k, j, l in groups if k != l and l > 1]
    return ra, rb


def _collapse_columns(table: TableItem, ranges: list[tuple[int, int]]) -> None:
    """Collapse each column range [lo, hi) into one column, in place.

    Cells lying inside a range are joined when their rows overlap (texts with
    a space, in row then column order), so a 2-row header cell next to a 1-row
    one becomes one 2-row cell; later columns shift left; spans and num_cols follow.
    """
    if not ranges:
        return
    ncols = table.data.num_cols
    rng = {c: (lo, hi) for lo, hi in ranges for c in range(lo, hi)}
    mapping, shift = [], 0
    for c in range(ncols):
        if c in rng and c > rng[c][0]:
            shift += 1
        mapping.append(c - shift)
    # Per range: clusters of cells whose row intervals overlap (cells sorted by start row).
    clusters: dict[int, list[list[TableCell]]] = {}
    groups: dict[tuple[int, int], list[TableCell]] = {}
    out: list[TableCell] = []
    for cell in sorted(table.data.table_cells, key=lambda c: (c.start_row_offset_idx, c.start_col_offset_idx)):
        r = rng.get(cell.start_col_offset_idx)
        if r and cell.end_col_offset_idx <= r[1]:
            open_ = clusters.setdefault(r[0], [])
            if open_ and cell.start_row_offset_idx < max(c.end_row_offset_idx for c in open_[-1]):
                open_[-1].append(cell)
            else:
                open_.append([cell])
                out.append(cell)  # placeholder, keeps reading order
                groups[(cell.start_row_offset_idx, r[0])] = open_[-1]
            continue
        cell.start_col_offset_idx = mapping[cell.start_col_offset_idx]
        cell.end_col_offset_idx = mapping[cell.end_col_offset_idx - 1] + 1
        cell.col_span = cell.end_col_offset_idx - cell.start_col_offset_idx
        out.append(cell)
    for (row, lo), cs in groups.items():
        base = cs[0]
        if len(cs) > 1:
            cs.sort(key=lambda c: (c.start_row_offset_idx, c.start_col_offset_idx))
            base.text = " ".join(c.text.strip() for c in cs if c.text.strip())
            base.end_row_offset_idx = max(c.end_row_offset_idx for c in cs)
            base.column_header = all(c.column_header for c in cs)
            base.row_header = any(c.row_header for c in cs)
            if all(c.bbox is not None for c in cs):
                base.bbox = BoundingBox.enclosing_bbox([c.bbox for c in cs])
        base.start_col_offset_idx = mapping[lo]
        base.end_col_offset_idx = mapping[lo] + 1
        base.col_span = 1
        base.row_span = base.end_row_offset_idx - base.start_row_offset_idx
    table.data.table_cells = out
    table.data.num_cols = ncols - sum(hi - lo - 1 for lo, hi in ranges)


def _at_page_break(prev: TableItem, nxt: TableItem, boxes: PageBoxes | None) -> bool:
    return boxes is None or (boxes.ends_page(prev.prov[-1]) and boxes.starts_page(nxt.prov[0]))


def _cols_differ_continuation(prev: TableItem, nxt: TableItem, boxes: PageBoxes | None) -> str | None:
    """Header reason when nxt would continue prev but for the column count, else None."""
    if prev.label != nxt.label or not prev.prov or not nxt.prov:
        return None
    if nxt.prov[0].page_no != prev.prov[-1].page_no + 1 or prev.data.num_cols == nxt.data.num_cols:
        return None
    if nxt.captions:
        return None
    reason = _continuation_reason(prev, nxt)
    if reason is None or not _at_page_break(prev, nxt, boxes):
        return None
    return reason


def _is_continuation(prev: TableItem, nxt: TableItem, boxes: PageBoxes | None = None,
                     collapse_split: bool = False) -> tuple[bool, str]:
    """(merge?, reason). Column counts must match, except when collapse_split is
    on and the header alignment shows one page split a column in two
    ("split_cols:<reason>"; the caller collapses it). Otherwise a column-count
    change is rejected as "cols_differ" (see continuation_chains)."""
    if prev.label != nxt.label:
        return False, "label_differs"
    if not prev.prov or not nxt.prov:
        return False, "no_prov"
    if nxt.prov[0].page_no != prev.prov[-1].page_no + 1:
        return False, "not_next_page"
    if prev.data.num_cols != nxt.data.num_cols:
        reason = _cols_differ_continuation(prev, nxt, boxes)
        if collapse_split and reason is not None and _column_alignment(prev, nxt) is not None:
            return True, f"split_cols:{reason}"
        return False, "cols_differ"
    if nxt.captions:  # a new caption means a new table
        return False, "has_caption"
    reason = _continuation_reason(prev, nxt)
    if reason is None:
        return False, "header_mismatch"
    # A header-less table is only a continuation if the page break cuts it:
    # otherwise two different same-shaped tables would be merged.
    if reason == "no_header" and boxes is not None and not (
            boxes.ends_page(prev.prov[-1]) and boxes.starts_page(nxt.prov[0])):
        return False, "no_header_not_at_page_break"
    return True, reason


def _move_footnotes(doc: DoclingDocument, prev: TableItem, nxt: TableItem) -> None:
    """Re-parent nxt's footnotes onto prev, so deleting nxt does not delete them."""
    moved = {ref.cref for ref in nxt.footnotes}
    nxt.children = [c for c in nxt.children if c.cref not in moved]
    for ref in nxt.footnotes:
        RefItem(cref=ref.cref).resolve(doc).parent = prev.get_ref()
        prev.children.append(ref)
        prev.footnotes.append(ref)
    nxt.footnotes = []


def _cut_row_evidence(first: list[TableCell], by_col: dict[int, TableCell]) -> str | None:
    """Textual evidence that the first continuation row is the tail of a cut row.

    Either a continuation cell starts lowercase (mid-sentence), or every
    continuation cell continues a long text cell, i.e. a multi-line cell the
    page break could cut. A label-less sub-row of short values ("X", units) is
    not joined. Trailing punctuation is not required: cut amendment rows here
    end a list item and continue with the next one ("14. ..." / "15. ...").
    """
    filled = [c for c in first if c.text.strip()]
    if any(c.text.strip()[:1].islower() for c in filled):
        return "lowercase_start"
    if all(len(by_col[c.start_col_offset_idx].text.strip()) >= _LONG_CELL_CHARS for c in filled):
        return "continues_long_cell"
    return None


def _append(doc: DoclingDocument, prev: TableItem, nxt: TableItem, join_cut_rows: str = "on",
            boxes: PageBoxes | None = None, log: dict | None = None) -> dict:
    """Append nxt's rows to prev; returns the join decision for the log. `log` (optional)
    receives "kept_header_rows" and "repeated_section_rows" entries."""
    _move_footnotes(doc, prev, nxt)
    # Drop the page's header rows that repeat the previous header (by words, as cell
    # boundaries may differ per page). A row flagged as header whose words are not in
    # the previous header is a body row the model mis-flagged: it is kept, unflagged.
    n_head, prev_words = _header_rows(nxt), _header_words(prev, _header_rows(prev))
    skip = 0
    while skip < n_head:
        cells = _row_cells(nxt, skip)
        words = Counter(w for c in cells for w in c.text.split())
        if words and sum((words & prev_words).values()) < _REPEAT_SHARE * sum(words.values()):
            break
        skip = max([c.end_row_offset_idx for c in cells] or [skip + 1])
    page = nxt.prov[0].page_no if nxt.prov else None
    if skip < n_head:
        for c in nxt.data.table_cells:
            if skip <= c.start_row_offset_idx < n_head:
                c.column_header = False
        if log is not None:
            log.setdefault("kept_header_rows", []).append({"table": prev, "page": page, "rows": n_head - skip})
    # Also drop repeated header rows that this page tagged as body rows.
    prev_headers = {_row_key(prev, r) for r in range(_header_rows(prev))}
    while skip < nxt.data.num_rows and _row_key(nxt, skip) in prev_headers:
        skip = max(c.end_row_offset_idx for c in _row_cells(nxt, skip))
    # A section row repeated at the top of the page (the PDF repeats the section the
    # page continues): the same full-width row as the last one above the break.
    last_section = next((s for r in range(prev.data.num_rows - 1, -1, -1)
                         if (s := _full_width_text(prev, r)) is not None), None)
    dropped = 0
    while last_section and skip < nxt.data.num_rows and _full_width_text(nxt, skip) == last_section:
        skip = max(c.end_row_offset_idx for c in _row_cells(nxt, skip))
        dropped += 1
    if dropped and log is not None:
        log.setdefault("repeated_section_rows", []).append({"table": prev, "page": page, "rows": dropped})
    offset = prev.data.num_rows - skip

    # A first body row whose first column is empty may be the tail of a row
    # that was cut by the page break: join its text onto the last row of `prev`.
    first = _row_cells(nxt, skip)
    last = _row_cells(prev, prev.data.num_rows - 1)
    single_span = all(c.row_span == 1 and c.col_span == 1 for c in first + last)
    joined_row, join = None, None
    if first and single_span and not any(
        c.start_col_offset_idx == 0 and c.text.strip() for c in first
    ):
        by_col = {c.start_col_offset_idx: c for c in last}
        if all(c.start_col_offset_idx in by_col for c in first if c.text.strip()):
            evidence = "forced" if join_cut_rows == "on" else None
            if join_cut_rows == "auto":
                geometric = boxes is None or (boxes.ends_page(prev.prov[-1]) and boxes.starts_page(nxt.prov[0]))
                evidence = _cut_row_evidence(first, by_col) if geometric else None
                if not geometric:
                    join = {"page": nxt.prov[0].page_no, "joined": False, "reason": "not_at_page_break"}
            if evidence:
                for c in first:
                    if c.text.strip():
                        tgt = by_col[c.start_col_offset_idx]
                        tgt.text = f"{tgt.text} {c.text}".strip()
                joined_row = skip
                offset -= 1
                join = {"page": nxt.prov[0].page_no, "joined": True, "reason": evidence}
            elif join is None:
                join = {"page": nxt.prov[0].page_no, "joined": False,
                        "reason": "disabled" if join_cut_rows == "off" else "no_text_evidence"}

    for c in nxt.data.table_cells:
        if c.start_row_offset_idx < skip or c.start_row_offset_idx == joined_row:
            continue
        cell = c.model_copy(deep=True)
        cell.start_row_offset_idx += offset
        cell.end_row_offset_idx += offset
        prev.data.table_cells.append(cell)

    prev.data.num_rows += nxt.data.num_rows - skip - (1 if joined_row is not None else 0)
    prev.prov.extend(nxt.prov)
    return join


def merge_split_tables(doc: DoclingDocument, settings: ProtocolSettings | None = None,
                       log: dict | None = None) -> int:
    """Merge page-split tables in place; returns the number of merges.

    `log` (optional) receives "merges" (every candidate pair: merged or the
    rejection reason) and "joins" (cut-row decisions), with table objects
    under "table" for the caller to turn into refs.
    """
    settings = settings or ProtocolSettings()
    boxes = PageBoxes(doc)
    merges_log = log.setdefault("merges", []) if log is not None else []
    joins_log = log.setdefault("joins", []) if log is not None else []
    merges = 0
    for parent in [doc.body, *doc.groups]:
        children = [ref.resolve(doc) for ref in parent.children]
        to_delete = []
        anchor = None
        for item in children:
            # Page headers/footers sit between the fragments; look through them.
            if item.content_layer == ContentLayer.FURNITURE:
                continue
            if isinstance(item, TableItem):
                if anchor is not None:
                    ok, reason = _is_continuation(anchor, item, boxes, settings.collapse_split_columns)
                    entry = {"table": anchor, "prev_page": anchor.prov[-1].page_no if anchor.prov else None,
                             "next_page": item.prov[0].page_no if item.prov else None,
                             "merged": ok, "reason": reason}
                    merges_log.append(entry)
                    if ok:
                        if reason.startswith("split_cols:"):
                            ra, rb = _split_ranges(_column_alignment(anchor, item))
                            _collapse_columns(anchor, ra)
                            _collapse_columns(item, rb)
                        join = _append(doc, anchor, item, settings.join_cut_rows, boxes, log)
                        if join:
                            joins_log.append({"table": anchor, **join})
                        if log is not None:  # lets log entries naming a fragment point at the merged table
                            log.setdefault("_merged_into", {})[id(item)] = (item, anchor)  # keeps item alive
                        to_delete.append(item)
                        merges += 1
                        continue
                anchor = item
            else:
                anchor = None
        if to_delete:
            doc.delete_items(node_items=to_delete)
    return merges


def leading_header_rows(table: TableItem) -> int:
    """Leading rows that form the column header, the row-label column exempt (a header row's
    first cell is often left unflagged): a row counts when a column-header cell starts on it
    and every other cell with text starting on it is a column header."""
    n = 0
    for r in range(table.data.num_rows):
        starting = [c for c in _row_cells(table, r) if c.text.strip()]
        others = [c for c in starting if c.start_col_offset_idx > 0]
        if not any(c.column_header for c in starting) or not all(c.column_header for c in others):
            break
        n = r + 1
    return n


def mark_inner_header_rows(doc: DoclingDocument, log: dict | None = None) -> int:
    """Flag stacked header blocks inside tables; returns the number of rows flagged.

    A body row whose row-label cell repeats the text of the header's row-label
    cell ("Number of participants" | 20-39 | 40-64 ... then later
    "Number of participants" | 20-27 | 28-35 ...) starts a new block of column
    labels for the rows below it: its cells become column-header cells. Leading
    header detection is unchanged (it counts leading rows only), so exports
    still show these rows in place.
    """
    entries = log.setdefault("inner_header_rows", []) if log is not None else []
    flagged = 0
    for table in doc.tables:
        n_head = leading_header_rows(table)
        if not n_head or table.data.num_cols < 2:
            continue
        stub = [c for c in table.data.table_cells
                if c.start_col_offset_idx == 0 and c.start_row_offset_idx < n_head and c.text.strip()]
        key = "".join("".join(c.text.split()).lower() for c in sorted(stub, key=lambda c: c.start_row_offset_idx))
        if len(key) < 3:
            continue
        r = n_head
        while r < table.data.num_rows:
            first = [c for c in _row_cells(table, r) if c.start_col_offset_idx == 0]
            if first and "".join(first[0].text.split()).lower() == key and not first[0].column_header:
                end = first[0].end_row_offset_idx
                rows = [c for c in table.data.table_cells if r <= c.start_row_offset_idx < end]
                if any(c.start_col_offset_idx > 0 and c.text.strip() for c in rows):
                    for c in rows:
                        c.column_header = True
                    entries.append({"table": table, "page": table.prov[0].page_no if table.prov else None,
                                    "row": r, "rows": end - r})
                    flagged += end - r
                r = end
                continue
            r += 1
    return flagged


def continuation_chains(doc: DoclingDocument, collapse_split: bool = False) -> list[list[str]]:
    """Chains of tables (self_refs, in order, length >= 2) that continue each other
    across pages but were not merged because a column is missing on one page.

    Same adjacency as merge_split_tables (same parent, furniture skipped, the
    next table immediately follows); a pair qualifies when it has the same
    label, the next table starts on the next page without a caption, the
    header continues, the page break cuts the table, and the column counts
    differ (without being a pure split column, when collapse_split is on, since
    those were merged). Pure: the document is not changed.
    """
    boxes = PageBoxes(doc)
    chains: list[list[str]] = []
    for parent in [doc.body, *doc.groups]:
        prev, chain = None, []
        for ref in parent.children:
            item = ref.resolve(doc)
            if item.content_layer == ContentLayer.FURNITURE:
                continue
            linked = (isinstance(item, TableItem) and prev is not None
                      and _cols_differ_continuation(prev, item, boxes) is not None
                      and (not collapse_split or _column_alignment(prev, item) is None))
            if linked:
                chain = chain or [prev.self_ref]
                chain.append(item.self_ref)
            else:
                if len(chain) >= 2:
                    chains.append(chain)
                chain = []
            prev = item if isinstance(item, TableItem) else None
        if len(chain) >= 2:
            chains.append(chain)
    return chains
