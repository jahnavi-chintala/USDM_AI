"""Protocol cleanup of a DoclingDocument before export.

Every rule is content-based (auto-detected per document) with overrides in
protocol_settings.ProtocolSettings. `postprocess()` returns step counts and
keeps a decisions log (refs, pages, reasons; never document text) in
`postprocess.last_decisions`.
"""

import re
from collections import Counter
from difflib import SequenceMatcher
from statistics import median

import pypdfium2 as pdfium
from docling_core.types.doc import (
    BoundingBox,
    ContentLayer,
    DocItemLabel,
    DoclingDocument,
    GroupItem,
    ListItem,
    NodeItem,
    PictureItem,
    ProvenanceItem,
    RefItem,
    SectionHeaderItem,
    TableCell,
    TableData,
    TableItem,
    TextItem,
)

from protocol_settings import ProtocolSettings
from figure_ocr import block_text, reocr_figures
from table_lattice import correct_tables_with_lattice
from table_merge import mark_inner_header_rows, merge_split_tables
from text_reconcile import pair_label_values, reconcile_text

_SECTION_NUM = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
_DOT_LEADER = re.compile(r"\s*\.{3,}\s*(\d+)\s*$")


def _section_tuple(text: str) -> tuple[int, ...] | None:
    """Section number of a heading, trailing ".0" dropped ("1.0" -> (1,))."""
    m = _SECTION_NUM.match(text.strip())
    if not m:
        return None
    t = tuple(int(x) for x in m.group(1).split("."))
    while len(t) > 1 and t[-1] == 0:
        t = t[:-1]
    return t


def _is_successor(prev: tuple[int, ...], cand: tuple[int, ...], max_gap: int) -> bool:
    """Whether `cand` can follow `prev` in a section outline.

    Accepted: a new child ("4.2" -> "4.2.1"), or the same prefix with one
    component advanced by 1..max_gap and any deeper parts starting at 1
    ("4.2.3" -> "4.3", "4.2.3" -> "5.1" when "5." was missed). A running
    maximum is not used, so one stray "21 CFR ..." or "100 mg ..." heading
    cannot demote every later section, and "2. HBV Monitoring" inside 10.12.1
    is rejected.
    """
    prev = prev or (0,)
    n = len(prev)
    if len(cand) > n and cand[:n] == prev and all(c == 1 for c in cand[n:]):
        return True
    for i in range(min(n, len(cand))):
        if cand[:i] != prev[:i]:
            break
        if 1 <= cand[i] - prev[i] <= max_gap and all(c == 1 for c in cand[i + 1:]):
            return True
    return False


def section_numbers(doc: DoclingDocument, settings: ProtocolSettings | None = None) -> dict[str, str]:
    """Heading self_ref -> accepted section number ("10.4.3"; "1.0" -> "1").

    Re-derived from the saved headings rather than stored: custom item meta
    would be printed by Docling's Markdown export. A number is accepted when
    the heading's level equals its depth (as fix_heading_levels sets it for
    accepted numbers) and it is a successor of the previous accepted number,
    so a demoted list heading ("3. Postmenopausal female:" under 10.4.3) has
    none. Works on a reloaded JSON as on the in-memory document.
    """
    max_gap = (settings or ProtocolSettings()).successor_max_gap
    out, prev = {}, ()
    for h, _ in doc.iterate_items():
        if isinstance(h, SectionHeaderItem) and (t := _section_tuple(h.text)) \
                and h.level == len(t) and _is_successor(prev, t, max_gap):
            out[h.self_ref], prev = ".".join(map(str, t)), t
    return out


def _norm_title(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


_FIGURE_TITLE = re.compile(r"(?i)^\s*figure\s+\S")


def _outline_levels(heads: list[SectionHeaderItem], pdf: pdfium.PdfDocument | None,
                    settings: ProtocolSettings, log: dict) -> dict[int, int] | None:
    """Heading id -> level from the PDF bookmarks, or None if they do not fit.

    Bookmarks are matched to headings by normalised title (or one being a
    prefix of the other, for headings glued to the next line) within one page.
    The level counts only ancestors that are headings themselves (matched or
    numbered), so a "/" root or an unmatched cover-page node does not shift it.
    Used when >= outline_min_match of the bookmarks (table/figure titles
    excluded) match a heading.
    """
    if pdf is None:
        return None
    try:
        marks = []
        for b in pdf.get_toc():
            dest = b.get_dest()
            idx = dest.get_index() if dest is not None else None
            marks.append((b.level, b.get_title() or "", None if idx is None else idx + 1))
    except Exception:  # malformed outline: fall back to numbering
        return None
    if not marks:
        return None
    title_re = re.compile(settings.table_title_pattern)
    norm = [_norm_title(t) for _, t, _ in marks]
    matched: dict[int, SectionHeaderItem] = {}
    for h in heads:
        page, n = (h.prov[0].page_no if h.prov else None), _norm_title(h.text)
        if page is None or not n:
            continue
        for i, (_, _, bpage) in enumerate(marks):
            bn = norm[i]
            if i in matched or not bn or bpage is None or abs(bpage - page) > 1:
                continue
            if bn == n or (min(len(bn), len(n)) >= 15 and (n.startswith(bn) or bn.startswith(n))):
                matched[i] = h
                break
    candidates = [i for i, (_, t, _) in enumerate(marks)
                  if norm[i] and not title_re.match(t) and not _FIGURE_TITLE.match(t)]
    share = sum(i in matched for i in candidates) / len(candidates) if candidates else 0.0
    log["outline"] = {"bookmarks": len(marks), "candidates": len(candidates),
                      "matched": len(matched), "share": round(share, 3)}
    if not matched or (settings.heading_source == "auto" and share < settings.outline_min_match):
        return None
    levels, stack = {}, []  # stack: (bookmark depth, counts as a heading level)
    for i, (depth, title, _) in enumerate(marks):
        while stack and stack[-1][0] >= depth:
            stack.pop()
        if i in matched:
            levels[id(matched[i])] = 1 + sum(counts for _, counts in stack)
        stack.append((depth, i in matched or _section_tuple(title) is not None))
    return levels


def fix_heading_levels(doc: DoclingDocument, pdf: pdfium.PdfDocument | None = None,
                       settings: ProtocolSettings | None = None, log: dict | None = None) -> int:
    """Set section-header levels; Docling gives every section header the same level.

    In order: (1) the PDF outline when it matches the headings; (2) section
    numbering, accepting a number only as a successor of the previous accepted
    one (see _is_successor); (3) unnumbered "Appendix X" headings (level 1, or
    2 under an "...APPENDICES" section); (4) other unnumbered headings nest one
    level under the current section. Headings before the first numbered one
    (title page, TOC) stay top-level.
    """
    settings = settings or ProtocolSettings()
    log = log if log is not None else {}
    demoted = log.setdefault("demoted_headings", [])
    heads = [h for h, _ in doc.iterate_items() if isinstance(h, SectionHeaderItem)]
    outline = None if settings.heading_source == "numbering" else _outline_levels(heads, pdf, settings, log)
    log["heading_source"] = "outline" if outline is not None else "numbering"
    has_numbers = any(_section_tuple(h.text) for h in heads)
    appendix = re.compile(settings.appendix_pattern)
    changed = 0
    depth = 0  # level that unnumbered headings nest under
    prev: tuple[int, ...] = ()  # most recent accepted section number
    top_text = ""  # most recent top-level section
    in_body = not has_numbers  # past the front matter
    for item in heads:
        text = item.text.strip()
        t = _section_tuple(text)
        if outline is not None and id(item) in outline:
            level = outline[id(item)]
            if t:
                prev, in_body = t, True
            if in_body:
                depth = level
        elif t and _is_successor(prev, t, settings.successor_max_gap):
            level = depth = len(t)
            prev, in_body = t, True
        elif appendix.match(text):
            level = depth = 2 if "APPENDI" in top_text.upper() else 1
        else:
            # Unnumbered sub-headings ("Inclusion Criteria" -> "Age and Sex:")
            # nest under the current section.
            level = min(depth + 1, 6) if depth else 1
            if t:
                demoted.append({"heading": item, "page": item.prov[0].page_no if item.prov else None,
                                "level": level, "reason": "number_not_successor"})
        if level == 1:
            top_text = text
        if item.level != level:
            item.level = level
            changed += 1
    return changed


def _cell(text: str, row: int, col: int, header: bool = False) -> TableCell:
    return TableCell(text=text, start_row_offset_idx=row, end_row_offset_idx=row + 1,
                     start_col_offset_idx=col, end_col_offset_idx=col + 1,
                     column_header=header)


_TOC_CELL = re.compile(r"\s*\d+(?:\.\d+)*\.?\s+\S")
_TOC_ENTRY = re.compile(r"^(\d+(\.\d+)*\.?|Appendix\s+\S+)?\s*.+?(\.{3,}|\s{2,}|\t)\s*\d+\s*$")


def rebuild_toc(doc: DoclingDocument, settings: ProtocolSettings | None = None,
                log: dict | None = None) -> int:
    """Replace the extracted table of contents with one built from the headings.

    The PDF's TOC comes out of TableFormer with dot leaders and several entries
    merged per cell. The accepted numbered headings (with their real page
    numbers) give a faithful, clean "Section | Page" table instead. Other
    document indexes (List of Tables) just get their dot leaders cleaned.
    """
    settings = settings or ProtocolSettings()
    log = log if log is not None else {}
    indexes = [t for t in doc.tables if t.label == DocItemLabel.DOCUMENT_INDEX]
    toc = [t for t in indexes if sum(
        bool(_TOC_CELL.match(c.text)) for c in t.data.table_cells
    ) > len(t.data.table_cells) / 3]

    for t in indexes:
        if t not in toc:
            for cell in t.data.table_cells:
                cell.text = _DOT_LEADER.sub(r" (p. \1)", cell.text)
                cell.text = re.sub(r"(?:\s*\.){4,}\s*", " ", cell.text).strip()
    if not toc:
        return 0

    numbers = section_numbers(doc, settings)
    after_toc = [h for h, _ in doc.iterate_items() if isinstance(h, SectionHeaderItem) and h.prov
                 and h.prov[0].page_no > toc[-1].prov[-1].page_no]
    entries = [(h.text.strip(), h.prov[0].page_no) for h in after_toc if h.self_ref in numbers]
    cells = [_cell("Section", 0, 0, True), _cell("Page", 0, 1, True)]
    for row, (text, page) in enumerate(entries, start=1):
        cells += [_cell(text, row, 0), _cell(str(page), row, 1)]
    toc[0].data = TableData(num_rows=len(entries) + 1, num_cols=2, table_cells=cells)

    # TOC lines the layout model left outside the TOC table ("8.7.1. ... 76",
    # or whole TOC pages read as loose lines), on front-matter pages with dot
    # leaders and on the TOC table's pages. Only TOC-entry-like items go: a
    # line ending in a page number after leaders/spacing, or a piece of a
    # heading's text (entries wrapped over 2-3 lines); body text stays.
    first_body_page = entries[0][1] if entries else 0
    leader_pages = {
        t.prov[0].page_no for t in doc.texts
        if t.prov and t.prov[0].page_no < first_body_page and re.search(r"\.{5,}", t.text)
    } | {p.page_no for t in toc for p in t.prov}
    heading_texts = [" ".join(h.text.split()) for h in after_toc]

    def toc_reason(t: TextItem) -> str | None:
        if settings.toc_delete_scope == "pages":
            return "toc_page"
        if _TOC_ENTRY.match(t.text.strip()):
            return "toc_entry"
        s = re.sub(r"\s+\d+$", "", " ".join(t.text.split()))
        if len(s) >= 10 and any(s in h for h in heading_texts):
            return "heading_fragment"
        return None

    strays = []
    for t in doc.texts:
        if (t.prov and t.prov[0].page_no in leader_pages and t.content_layer == ContentLayer.BODY
                and t.label in (DocItemLabel.TEXT, DocItemLabel.LIST_ITEM)
                and not isinstance(t.parent.resolve(doc), (TableItem, PictureItem))
                and (reason := toc_reason(t))):
            strays.append(t)
            log.setdefault("deleted_toc_items", []).append(
                {"page": t.prov[0].page_no, "label": t.label.value, "reason": reason})
    log["toc_items_kept"] = sum(
        1 for t in doc.texts if t.prov and t.prov[0].page_no in leader_pages
        and t.content_layer == ContentLayer.BODY and t.label in (DocItemLabel.TEXT, DocItemLabel.LIST_ITEM)
        and not isinstance(t.parent.resolve(doc), (TableItem, PictureItem)) and t not in strays)
    if toc[1:] or strays:
        doc.delete_items(node_items=toc[1:] + strays)
    log["toc_items_deleted"] = len(strays)
    return len(entries)


def _ocr_order(doc: DoclingDocument, pic: PictureItem, frags: list[TextItem], mode: str) -> list[TextItem]:
    """Reading order of OCR fragments inside a figure.

    Fragments are grouped into columns by clustering their x-centres (a gap
    wider than the median fragment width starts a new column) and read column
    by column, top to bottom, as left-to-right schema diagrams read. A figure
    taller than wide, or with a single column, reads top to bottom.
    """
    box = pic.prov[0].bbox
    tl = {id(it): it.prov[0].bbox.to_top_left_origin(doc.pages[it.prov[0].page_no].size.height)
          for it in frags}
    rows = sorted(frags, key=lambda it: (tl[id(it)].t, tl[id(it)].l))
    if mode == "rows" or (mode == "auto" and abs(box.t - box.b) > box.r - box.l):
        return rows
    col, cols, right = -1, {}, None
    for it in sorted(frags, key=lambda it: tl[id(it)].l):
        b = tl[id(it)]
        if right is None or b.l >= right:  # no horizontal overlap with the current column
            col, right = col + 1, b.r
        cols[id(it)], right = col, max(right, b.r)
    if mode == "auto" and col == 0:
        return rows
    return sorted(frags, key=lambda it: (cols[id(it)], tl[id(it)].t, tl[id(it)].l))


def flatten_picture_text(doc: DoclingDocument, settings: ProtocolSettings | None = None) -> int:
    """Collapse OCR'd text inside figures into one plain paragraph.

    Layout analysis labels fragments of a flow chart as headings and list
    items, which would pollute the document outline. Fragments are ordered
    (see _ocr_order) and attached to the picture as a single text item. The
    picture's captions and footnotes (Docling makes them children of the
    picture) and non-text children are kept as they are.
    """
    settings = settings or ProtocolSettings()
    flattened = 0
    for pic in doc.pictures:
        if not pic.children or not pic.prov:
            continue
        keep = {r.cref for r in [*pic.captions, *pic.footnotes]}
        frags, remove = [], []
        for ref in pic.children:
            child = ref.resolve(doc)
            if ref.cref in keep:
                continue
            if isinstance(child, GroupItem):
                leaves = [x for x, _ in doc.iterate_items(root=child, with_groups=False, traverse_pictures=True)]
                if not all(isinstance(x, TextItem) and x.self_ref not in keep for x in leaves):
                    continue
            elif isinstance(child, TextItem):
                leaves = [child]
            else:
                continue
            frags += [x for x in leaves if x.text.strip() and x.prov]
            remove.append(child)
        if not frags:
            continue
        # A figure re-OCR'd as a whole (figure_ocr.py) is already in reading order, block by block.
        text = block_text(doc, pic)
        if text is None:
            ordered = _ocr_order(doc, pic, frags, settings.figure_ocr_order)
            text = "; ".join(" ".join(it.text.split()) for it in ordered)
        doc.delete_items(node_items=remove)
        doc.add_text(label=DocItemLabel.TEXT, text=f"Text in figure: {text}", parent=pic)
        flattened += 1
    return flattened


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def drop_table_shadow_pictures(doc: DoclingDocument, log: dict | None = None) -> int:
    """Remove pictures that duplicate a table region.

    The layout model sometimes labels a dense table page (e.g. part of the SoA)
    as both a table and a picture; the picture would repeat its text. A picture
    is only dropped when none of its text is missing from the overlapping
    table, so a real figure next to or inside a table region survives.
    """
    log = log if log is not None else {}
    tables_by_page: dict[int, list] = {}
    for t in doc.tables:
        for p in t.prov:
            tables_by_page.setdefault(p.page_no, []).append((p.bbox, t))
    shadows = []
    for pic in doc.pictures:
        if not pic.prov:
            continue
        pb, page = pic.prov[0].bbox, pic.prov[0].page_no
        for tb, table in tables_by_page.get(page, []):
            if pb.intersection_over_self(tb) >= 0.8 or tb.intersection_over_self(pb) >= 0.8:
                pic_words = set().union(*[_words(x.text) for x, _ in doc.iterate_items(
                    root=pic, with_groups=False, traverse_pictures=True) if isinstance(x, TextItem)])
                missing = pic_words - set().union(*[_words(c.text) for c in table.data.table_cells])
                size = doc.pages[page].size
                entry = {"picture": pic, "table": table, "page": page,
                         "area_fraction": round(pb.area() / (size.width * size.height), 3),
                         "missing_words": len(missing)}
                if missing:
                    log.setdefault("kept_overlapping_pictures", []).append(entry)
                    continue
                log.setdefault("shadow_pictures", []).append(entry)
                shadows.append(pic)
                break
    if shadows:
        doc.delete_items(node_items=shadows)
    return len(shadows)


def demote_stray_furniture(doc: DoclingDocument, settings: ProtocolSettings | None = None,
                           log: dict | None = None) -> int:
    """Move body text that repeats a page header/footer into the furniture layer.

    Only text lying in the page's top/bottom band, or (not a section header)
    repeating a header/footer seen on several pages: templates that repeat the
    current section title in the running header keep their body heading.
    """
    settings = settings or ProtocolSettings()
    log = log if log is not None else {}
    pages: dict[str, set[int]] = {}
    for t in doc.texts:
        if t.label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER):
            pages.setdefault(" ".join(t.text.split()), set()).update(p.page_no for p in t.prov)
    moved = 0
    for t in doc.texts:
        key = " ".join(t.text.split())
        if t.content_layer != ContentLayer.BODY or key not in pages:
            continue
        in_band = False
        if t.prov:
            h = doc.pages[t.prov[0].page_no].size.height
            b = t.prov[0].bbox.to_top_left_origin(h)
            in_band = b.b <= settings.furniture_band * h or b.t >= (1 - settings.furniture_band) * h
        frequent = len(pages[key]) >= settings.furniture_min_pages and t.label != DocItemLabel.SECTION_HEADER
        if in_band or frequent:
            t.content_layer = ContentLayer.FURNITURE
            moved += 1
            log.setdefault("furniture", []).append(
                {"page": t.prov[0].page_no if t.prov else None, "label": t.label.value,
                 "reason": "page_band" if in_band else "repeated_header"})
    return moved


_PAGE_NUMBER_ONLY = re.compile(r"^(?:page\s*)?#+(?:\s*(?:of|/)\s*#+)?$", re.I)
# A page-number expression anywhere in a (digit-masked) header line: "Version #.# Page: # of #".
_PAGE_NUMBER_IN = re.compile(r"\bpage\s*:?\s*#+(?:\s*(?:of|/)\s*#+)?(?!\w)", re.I)
_WORD = re.compile(r"[^\W_]+")


def restore_unique_headers(doc: DoclingDocument, log: dict | None = None) -> int:
    """Return page headers/footers that occur on a single page only to the body.

    A running header repeats on many pages; a header line printed on one page only
    (typically the title page: "NCT #: NCT04573309", a registry or IND number) is
    content, and as furniture it never reaches the chunks. Repetition is compared with
    digits masked, so "Page 3 of 90" style lines still count as repeated. Never restored:
    lines holding a page-number expression ("Version 1.0 Page: 3 of 86"), and lines without
    digits whose words all occur in a header/footer repeated on other pages (a fragment of
    the running header that the layout split differently on one page; digits may be new
    information, such as an identifier, so a line with digits is not judged this way).
    """
    log = log if log is not None else {}
    furniture = [t for t in doc.texts if t.label in (DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER)]
    pages: dict[str, set[int]] = {}
    for t in furniture:
        pages.setdefault(re.sub(r"\d+", "#", " ".join(t.text.split())).lower(), set()).update(
            p.page_no for p in t.prov)
    running = {w for key, ps in pages.items() if len(ps) > 1 for w in _WORD.findall(key)}
    restored = 0
    for t in furniture:
        key = re.sub(r"\d+", "#", " ".join(t.text.split())).lower()
        if len(pages[key]) != 1 or not t.text.strip() or _PAGE_NUMBER_ONLY.match(key) or len(doc.pages) < 3:
            continue
        if _PAGE_NUMBER_IN.search(key) or ("#" not in key and set(_WORD.findall(key)) <= running):
            continue
        t.label = DocItemLabel.TEXT
        t.content_layer = ContentLayer.BODY
        restored += 1
        log.setdefault("restored_headers", []).append(
            {"page": t.prov[0].page_no if t.prov else None, "reason": "single_page_header"})
    return restored


_SPACED_HYPHEN = re.compile(r"\b(\w+) - (\w+)\b")
_TIGHT_HYPHEN = re.compile(r"(?=\b(\w+)-(\w+)\b)")


def fix_hyphen_spacing(doc: DoclingDocument, pdf_text: str) -> int:
    """Undo 'AB - 01234567' -> 'AB-01234567' spacing added by the pdfium backend.

    A pair is only joined when the PDF text layer contains it tightly
    hyphenated and never with spaces, so genuine " - " dashes stay intact.
    """
    tight = {m.groups() for m in _TIGHT_HYPHEN.finditer(pdf_text)}
    spaced = {m.groups() for m in _SPACED_HYPHEN.finditer(pdf_text)}
    fixed = 0

    def repl(m: re.Match) -> str:
        nonlocal fixed
        if m.groups() in tight and m.groups() not in spaced:
            fixed += 1
            return f"{m.group(1)}-{m.group(2)}"
        return m.group(0)

    def fix(s: str) -> str:
        # Loop so chains like "a - b - c" are fully collapsed.
        prev = None
        while prev != s:
            prev, s = s, _SPACED_HYPHEN.sub(repl, s)
        return s

    for t in doc.texts:
        t.text = fix(t.text)
    for table in doc.tables:
        for cell in table.data.table_cells:
            cell.text = fix(cell.text)
    return fixed


# ---------------------------------------------------------------- table titles and footnotes

# Footnote marker families; with footnote_markers="auto" only the families that
# occur as the first item directly below a table in the document are used.
_FOOTNOTE_FAMILIES = {
    "letter_dot": (r"[a-z]\.\s", r"a\."),
    "letter_paren": (r"[a-z]\)\s", r"a\)"),
    "paren_letter": (r"\([a-z]\)\s", r"\(a\)"),
    "number": (r"\d{1,2}\.?\s", r"1\b"),
    "symbol": (r"[*†‡§¶]+\s?\S", r"[*†‡§¶]"),
    "note": (r"(?i)notes?:", r"(?i)notes?:"),
    "abbreviations": (r"(?i)abbreviations?:", r"(?i)abbreviations?:"),
    "source": (r"(?i)source:", r"(?i)source:"),
}
_TERMINAL = (".", ";", "!", "?", ":")


class _Footnotes:
    """Recognises table-footnote items with the document's marker families."""

    def __init__(self, families: dict[str, tuple[str, str]]):
        self.families = {k: (re.compile(m), re.compile(f)) for k, (m, f) in families.items()}

    @staticmethod
    def candidate(it) -> str | None:
        """Footnote text of an item ("a. ..." for a lettered list item), if it is body text."""
        if isinstance(it, ListItem):
            return f"{it.marker} {it.text}".strip() if it.marker else it.text.strip()
        if isinstance(it, TextItem) and not isinstance(it, SectionHeaderItem):
            return it.text.strip()
        return None

    def match(self, it) -> tuple[str, str, bool] | None:
        """(text, family, is the family's first marker) for a footnote-like item."""
        text = self.candidate(it)
        if not text:
            return None
        for name, (marker, first) in self.families.items():
            if marker.match(text):
                return text, name, bool(first.match(text))
        return None


def _line_height(doc: DoclingDocument) -> float:
    """Typical body line height: median height of short (one-line) text items."""
    hs = [abs(t.prov[0].bbox.t - t.prov[0].bbox.b) for t in doc.texts
          if t.prov and t.content_layer == ContentLayer.BODY and 0 < len(t.text) < 60]
    return median(hs) if hs else 12.0


def _gap(doc: DoclingDocument, upper: ProvenanceItem, lower: ProvenanceItem) -> float | None:
    """Vertical gap from the bottom of `upper` (a prov) to the top of `lower`, same page only."""
    if upper.page_no != lower.page_no:
        return None
    h = doc.pages[upper.page_no].size.height
    return lower.bbox.to_top_left_origin(h).t - upper.bbox.to_top_left_origin(h).b


def _below(doc: DoclingDocument, table: TableItem) -> ProvenanceItem:
    """Prov of the lowest part of a table fragment on its last page (table or its footnotes)."""
    last = table.prov[-1]
    provs = [last] + [f.resolve(doc).prov[0] for f in table.footnotes
                      if f.resolve(doc).prov and f.resolve(doc).prov[0].page_no == last.page_no]
    h = doc.pages[last.page_no].size.height
    return max(provs, key=lambda p: p.bbox.to_top_left_origin(h).b)


def _adjacent(doc: DoclingDocument, anchor: ProvenanceItem | None, it, max_gap: float,
              page_break_ok: bool) -> bool:
    """`it` lies directly below `anchor` (a prov), or, when allowed, on the next page.

    Block items are consecutive in reading order, so an item on the next page
    is the first body item of that page.
    """
    if anchor is None or not getattr(it, "prov", None):
        return False
    gap = _gap(doc, anchor, it.prov[0])
    if gap is not None:
        return -2 <= gap <= max_gap
    return page_break_ok and it.prov[0].page_no == anchor.page_no + 1


def _detect_markers(doc: DoclingDocument, body: list, settings: ProtocolSettings, max_gap: float) -> _Footnotes:
    """Marker families of the footnote-like blocks found directly below tables."""
    if settings.footnote_markers != "auto":
        return _Footnotes({f"custom{i}": (m, m) for i, m in enumerate(settings.footnote_markers)})
    probe, found = _Footnotes(_FOOTNOTE_FAMILIES), set()
    for i, table in enumerate(body):
        if not isinstance(table, TableItem) or not table.prov:
            continue
        anchor = _below(doc, table)
        for j, it in enumerate(body[i + 1:]):
            kids = [c.resolve(doc) for c in it.children] if isinstance(it, GroupItem) else [it]
            matches = [probe.match(k) for k in kids]
            if not kids or None in matches:
                break
            for k in kids:
                if not _adjacent(doc, anchor, k, max_gap, page_break_ok=j > 0):
                    break
                anchor = k.prov[0]
            else:
                found.update(m[1] for m in matches)
                continue
            break
    return _Footnotes({k: v for k, v in _FOOTNOTE_FAMILIES.items() if k in found})


def _caption_position(doc: DoclingDocument, settings: ProtocolSettings) -> str:
    """Where table titles sit (above/below), by majority over captioned tables."""
    if settings.caption_position != "auto":
        return settings.caption_position
    votes = Counter()
    for t in doc.tables:
        for ref in t.captions:
            cap = ref.resolve(doc)
            if cap.prov and t.prov and cap.prov[0].page_no == t.prov[0].page_no:
                h = doc.pages[t.prov[0].page_no].size.height
                cb, tb = cap.prov[0].bbox.to_top_left_origin(h), t.prov[0].bbox.to_top_left_origin(h)
                votes["above" if (cb.t + cb.b) / 2 < tb.t + 1 else "below"] += 1
    return "below" if votes["below"] > votes["above"] else "above"


def resolve_repeated_table_titles(doc: DoclingDocument, settings: ProtocolSettings | None = None,
                                  log: dict | None = None) -> dict[str, int]:
    """Handle table titles that the PDF repeats on continuation pages, and footnotes.

    A table that continues on the next page repeats its title, which layout
    analysis reports as a section heading or a second caption. Left alone, the
    repeat blocks the page-split table merge and becomes a fake heading that
    captures the content below it (e.g. Table 2's footnotes). A title is a
    repeat when it has the same number, a similar normalised text and sits on
    the page of, or after, the table's last title (so restarted numbering keeps
    its captions). Repeats are removed and any footnote block after them is
    attached to the table; a title seen for the first time becomes the caption
    of the table it belongs to (above or below, per document).
    """
    settings = settings or ProtocolSettings()
    log = log if log is not None else {}
    stats = {"repeated_titles_removed": 0, "captions_restored": 0, "footnotes_attached": 0}
    title_re = re.compile(settings.table_title_pattern)
    body = [r.resolve(doc) for r in doc.body.children]
    body = [it for it in body if getattr(it, "content_layer", None) != ContentLayer.FURNITURE]
    max_gap = settings.footnote_max_gap_lines * _line_height(doc)
    notes = _detect_markers(doc, body, settings, max_gap)
    log["footnote_markers"] = sorted(notes.families)
    position = _caption_position(doc, settings)
    log["caption_position"] = position
    seen: list[dict] = []  # {"num", "norm", "table", "page"} per distinct title
    to_delete: list = []
    removed_titles: list = []  # repeated title items (deleted, unlike moved footnote items)

    def title_of(it, is_caption: bool = False) -> tuple[str, str] | None:
        if not isinstance(it, TextItem):
            return None
        m = title_re.match(it.text)
        if not m:
            return None
        # Without a separator ("Table 5 shows ...") only a heading/caption counts.
        if m.lastindex and m.lastindex >= 2 and not m.group(2) and not is_caption \
                and it.label not in (DocItemLabel.SECTION_HEADER, DocItemLabel.CAPTION):
            return None
        norm = re.sub(r"\b(continued|cont d|cont)\b", " ", _norm_title(it.text))
        return m.group(1).upper(), " ".join(norm.split())

    def repeat_of(title: tuple[str, str], page: int) -> tuple[dict, float] | None:
        for s in reversed(seen):
            if s["num"] != title[0] or not 0 <= page - s["page"] <= 1:
                continue
            n = min(len(s["norm"]), len(title[1]))
            ratio = SequenceMatcher(None, s["norm"][:n], title[1][:n]).ratio()
            if ratio >= settings.title_similarity:
                return s, ratio
        return None

    def page_of(it) -> int | None:
        if isinstance(it, GroupItem):
            return page_of(it.children[0].resolve(doc)) if it.children else None
        return it.prov[0].page_no if getattr(it, "prov", None) else None

    def first_on_page(j: int) -> bool:
        """body[j] is the first item of its page in reading order (ignoring removed titles)."""
        page = page_of(body[j])
        for k in range(j - 1, -1, -1):
            if body[k] not in removed_titles:
                p = page_of(body[k])
                return p is not None and page is not None and p < page
        return True

    def attach_block(table: TableItem, start: int, anchor, first_needed: bool) -> tuple[int, int]:
        """Attach the footnote block starting at body[start]; returns (next index, attached).

        Each item must lie directly below the previous one (the table, its
        repeated title or the previous footnote); only items after the first
        may continue at the top of the next page. A paragraph starting
        lowercase continues the previous footnote only if that one lacks
        terminal punctuation and the paragraph is the first item on the next
        page. The block's first item must be a family's first marker ("a.",
        "Note:") unless the table already has footnotes.
        """
        footnotes = [r.resolve(doc) for r in table.footnotes]
        added, j = 0, start
        while j < len(body):
            it = body[j]
            kids = [c.resolve(doc) for c in it.children] if isinstance(it, GroupItem) else [it]
            matches = [notes.match(k) for k in kids]
            a, ok = anchor, bool(kids) and None not in matches and (
                not first_needed or bool(footnotes) or matches[0][2])
            for k in kids if ok else []:
                if not _adjacent(doc, a, k, max_gap, page_break_ok=not first_needed):
                    ok = False
                    break
                a = k.prov[0]
            if ok:
                for k, m in zip(kids, matches):
                    footnotes.append(_add_footnote(doc, table, m[0], k))
                    log.setdefault("footnotes", []).append(
                        {"table": table, "page": page_of(k), "reason": f"marker:{m[1]}"})
                    added += 1
                anchor = a
            elif (isinstance(it, TextItem) and not isinstance(it, SectionHeaderItem) and footnotes
                  and it.text.strip()[:1].islower() and it.prov and footnotes[-1].prov
                  and it.prov[0].page_no == footnotes[-1].prov[0].page_no + 1 and first_on_page(j)
                  and not footnotes[-1].text.rstrip().endswith(_TERMINAL)):
                footnotes[-1].text = f"{footnotes[-1].text} {it.text.strip()}"
                footnotes[-1].orig = footnotes[-1].text
                log.setdefault("footnotes", []).append(
                    {"table": table, "page": page_of(it), "reason": "continuation_next_page"})
                anchor = it.prov[0]
            else:
                break
            first_needed = False
            to_delete.append(it)
            j += 1
        return j, added

    def remember(title, table: TableItem, page: int) -> dict:
        seen.append({"num": title[0], "norm": title[1], "table": table, "page": page, "last": table})
        return seen[-1]

    i, run = 0, None  # run: title entry of the table run ending at body[i - 1]
    while i < len(body):
        it = body[i]
        if isinstance(it, TableItem):
            titled = False
            for ref in list(it.captions):
                cap = ref.resolve(doc)
                title = title_of(cap, is_caption=True)
                page = page_of(cap) or page_of(it)
                rep = repeat_of(title, page) if title and page else None
                if rep and rep[0]["table"] is not it:
                    it.captions.remove(ref)
                    to_delete.append(cap)
                    rep[0]["page"], run, titled = page, rep[0], True
                    stats["repeated_titles_removed"] += 1
                    log.setdefault("removed_titles", []).append(
                        {"table": rep[0]["table"], "page": page, "reason": "repeated_caption",
                         "similarity": round(rep[1], 3)})
                elif rep:  # its own title, restored from a heading just before it
                    run, titled = rep[0], True
                elif title and page:
                    run, titled = remember(title, it, page), True
            if not titled and run is not None and i > 0 and body[i - 1] is run["last"] and it.prov:
                run["page"] = it.prov[-1].page_no  # untitled continuation fragment of the run
            if run is not None:
                run["last"] = it
            # A footnote block directly below the table belongs to it.
            i, added = attach_block(it, i + 1, _below(doc, it) if it.prov else None, True)
            stats["footnotes_attached"] += added
            continue
        title = title_of(it)
        page = page_of(it)
        if not title or page is None:
            i += 1
            continue
        nxt = body[i + 1] if i + 1 < len(body) else None
        prv = body[i - 1] if i > 0 else None
        rep = repeat_of(title, page)
        if rep:
            to_delete.append(it)
            removed_titles.append(it)
            rep[0]["page"] = page
            stats["repeated_titles_removed"] += 1
            log.setdefault("removed_titles", []).append(
                {"table": rep[0]["table"], "page": page, "reason": "repeated_heading",
                 "similarity": round(rep[1], 3)})
            if not isinstance(nxt, TableItem):
                i, added = attach_block(rep[0]["table"], i + 1, it.prov[0], False)
                stats["footnotes_attached"] += added
                continue
        else:
            target = nxt if position == "above" else prv
            if isinstance(target, TableItem) and not target.captions:
                cap = doc.add_text(label=DocItemLabel.CAPTION, text=it.text,
                                   prov=it.prov[0] if it.prov else None, parent=target)
                target.captions.append(cap.get_ref())
                to_delete.append(it)
                remember(title, target, page)
                stats["captions_restored"] += 1
                log.setdefault("captions_restored", []).append({"table": target, "page": page})
        i += 1
    if to_delete:
        doc.delete_items(node_items=to_delete)
    return stats


def _add_footnote(doc: DoclingDocument, table: TableItem, text: str, src) -> TextItem:
    fn = doc.add_text(label=DocItemLabel.FOOTNOTE, text=text,
                      prov=src.prov[0] if getattr(src, "prov", None) else None, parent=table)
    table.footnotes.append(fn.get_ref())
    return fn


# ---------------------------------------------------------------- list items, glyphs, cells

def absorb_split_list_lines(doc: DoclingDocument, pdf: pdfium.PdfDocument) -> int:
    """Repair list items whose layout box was drawn narrower than their text.

    A line wider than the list item's box (e.g. inclusion criterion 9's
    "Cohort B: ..." line) is split off as a separate text block and placed out
    of order, while the lines around it are glued together. Body text lying
    inside a list item's vertical span and mostly (>= 50 % of its width) within
    the item's column is merged back, and the item's text is re-read from the
    PDF over the combined area, in line order. A side note next to the list
    does not overlap the column and stays.
    """
    repaired = 0
    texts_by_page: dict[int, list[TextItem]] = {}
    for t in doc.texts:
        if (t.prov and t.label == DocItemLabel.TEXT and t.content_layer == ContentLayer.BODY
                and not isinstance(t.parent.resolve(doc), (PictureItem, TableItem))):
            texts_by_page.setdefault(t.prov[0].page_no, []).append(t)
    for li in [t for t in doc.texts if isinstance(t, ListItem) and t.prov]:
        page, lb = li.prov[0].page_no, li.prov[0].bbox

        def in_column(b: BoundingBox) -> bool:
            overlap = min(b.r, lb.r) - max(b.l, lb.l)
            return b.r > b.l and overlap / (b.r - b.l) >= 0.5

        inside = [
            t for t in texts_by_page.get(page, [])
            if lb.b - 1 <= (t.prov[0].bbox.t + t.prov[0].bbox.b) / 2 <= lb.t + 1
            and t.prov[0].bbox.l >= lb.l - 2 and t.prov[0].bbox.l < lb.r and in_column(t.prov[0].bbox)
        ]
        if not inside:
            continue
        box = lb
        for t in inside:
            box = BoundingBox.enclosing_bbox([box, t.prov[0].bbox])
        with_marker = pdf[page - 1].get_textpage().get_text_bounded(box.l, box.b, box.r, box.t)
        text = " ".join(with_marker.split())
        if li.marker:
            text = re.sub(rf"^{re.escape(li.marker)}\s*", "", text)
        li.text = li.orig = text
        li.prov[0].bbox = box
        li.prov[0].charspan = (0, len(text))
        doc.delete_items(node_items=inside)
        for t in inside:
            texts_by_page[page].remove(t)
        repaired += 1
    return repaired


_HYPHEN_END = re.compile(r"[^\W_][-‐‑]$")


def join_hyphen_split_items(doc: DoclingDocument, log: dict | None = None) -> int:
    """Join a paragraph the layout model split into two items at a line that ends with a hyphen.

    Item A (body text or list item) ends with a word and an attached hyphen; the next item B in the
    same parent (body text or list item) starts with a word character, lies on the same page directly
    below A (gap at most half a line) and in A's column (>= 50 % of B's width within A's). A paragraph
    does not end on a dangling hyphen, so B is A's next line. B's text is appended to A's after a space
    and B is deleted; text_reconcile then decides from the PDF whether "word- word" becomes
    "word-word", "wordword" or stays suspended, as for a line break inside one item. Run before
    reconcile_text. Returns the number of joins; log["hyphen_joins"]: item, page (no text).
    """
    entries = log.setdefault("hyphen_joins", []) if log is not None else []
    max_gap = 0.5 * _line_height(doc)
    kinds = (DocItemLabel.TEXT, DocItemLabel.LIST_ITEM)
    joined = 0
    for parent in [doc.body, *doc.groups]:
        i = 0
        while i + 1 < len(parent.children):
            a, b = parent.children[i].resolve(doc), parent.children[i + 1].resolve(doc)
            ok = (isinstance(a, TextItem) and isinstance(b, TextItem) and a.label in kinds and b.label in kinds
                  and a.content_layer == ContentLayer.BODY and b.content_layer == ContentLayer.BODY
                  and a.prov and b.prov and _HYPHEN_END.search(a.text.rstrip()) is not None
                  and b.text[:1].isalnum() and not (getattr(b, "marker", "") or "").strip())  # never past a marker
            if ok:
                pa, pb = a.prov[-1], b.prov[0]
                gap = _gap(doc, pa, pb)
                h = doc.pages[pa.page_no].size.height
                ba, bb = pa.bbox.to_top_left_origin(h), pb.bbox.to_top_left_origin(h)
                width = bb.r - bb.l
                in_column = width > 0 and (min(ba.r, bb.r) - max(ba.l, bb.l)) / width >= 0.5
                ok = gap is not None and -1.0 <= gap <= max_gap and in_column and len(b.prov) == 1
            if not ok:
                i += 1
                continue
            a.text = f"{a.text.rstrip()} {b.text.lstrip()}"
            a.orig = f"{a.orig.rstrip()} {b.orig.lstrip()}" if a.orig and b.orig else a.text
            a.prov[-1] = ProvenanceItem(page_no=pa.page_no, bbox=BoundingBox.enclosing_bbox([pa.bbox, pb.bbox]),
                                        charspan=(0, len(a.text)))
            entries.append({"item": a, "page": pa.page_no})
            doc.delete_items(node_items=[b])
            joined += 1
    return joined


# Adobe Symbol encoding (bytes 0x20-0xFE as Word emits them at U+F020-U+F0FE).
# ASCII-identical positions map to themselves.
_SYMBOL_ENCODING = (
    " !∀#∃%&∋()∗+,−./0123456789:;<=>?"
    "≅ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_"
    "‾αβχδεφγηιϕκλμνοπθρστυϖωξψζ{|}∼"
)
_SYMBOL_HIGH = (  # 0xA0-0xFE; 0xF0 is undefined in Symbol
    "€ϒ′≤⁄∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…⏐⎯↵"
    "ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇®©™∏√⋅¬∧∨⇔⇐⇑⇒⇓"
    "◊〈®©™∑⎛⎜⎝⎡⎢⎣⎧⎨⎩⎪\x00〉∫⌠⎮⌡⎞⎟⎠⎤⎥⎦⎫⎬⎭"
)
_SYMBOL_FONT = {chr(0xF020 + i): c for i, c in enumerate(_SYMBOL_ENCODING)}
_SYMBOL_FONT |= {chr(0xF0A0 + i): c for i, c in enumerate(_SYMBOL_HIGH) if c != "\x00"}
# Wingdings marks used in protocols, where the Symbol slot is a bracket piece or
# unused (the code points are shared; the font is not known from the text).
_SYMBOL_FONT |= {chr(0xF080 + i): c for i, c in enumerate("⓪①②③④⑤⑥⑦⑧⑨⑩")}
_SYMBOL_FONT |= {"": "➔", "": "⇨", "": "✗", "": "✓", "": "☒", "": "☑"}
# Word's own bullets and glyphs as mapped before (U+F0A7 is the Wingdings
# square bullet; U+F0D7 is used for a multiplication sign).
_SYMBOL_FONT |= {"": "▪", "": "×"}
_PRIVATE_USE = re.compile(r"[-]")


def normalize_symbol_glyphs(doc: DoclingDocument, settings: ProtocolSettings | None = None,
                            log: dict | None = None) -> int:
    """Replace Symbol/Wingdings private-use characters with real Unicode.

    Leading bullets on list items are dropped (the item is already a list item);
    elsewhere (e.g. bullets inside table cells, Greek letters) they are mapped.
    Unknown private-use characters are never deleted: they are kept (or, with
    unknown_pua="replace", become U+FFFD) and counted by code point.
    """
    settings = settings or ProtocolSettings()
    log = log if log is not None else {}
    unknown = Counter()
    fixed = 0

    def sub(m: re.Match) -> str:
        ch = m.group(0)
        if ch in _SYMBOL_FONT:
            return _SYMBOL_FONT[ch]
        unknown[f"U+{ord(ch):04X}"] += 1
        return "�" if settings.unknown_pua == "replace" else ch

    def fix(text: str) -> str:
        nonlocal fixed
        if not _PRIVATE_USE.search(text):
            return text
        new = _PRIVATE_USE.sub(sub, text)
        fixed += new != text
        return new

    for t in doc.texts:
        if isinstance(t, ListItem):
            t.text = re.sub(r"^\s*[]\s*", "", t.text)
        t.text = fix(t.text)
    for table in doc.tables:
        for cell in table.data.table_cells:
            cell.text = fix(cell.text)
    log["unknown_pua"] = dict(unknown)
    return fixed


def fix_split_duplicate_cells(doc: DoclingDocument) -> int:
    """Repair a phrase whose words were dealt across two narrow adjacent cells.

    In narrow columns (e.g. SoA "Day 15" / "Day 22", both "Cycle 1"), a
    slightly-off predicted column boundary can move a word into the next cell:
    "Cycle" | "1 Cycle 1". When two neighbouring cells together read as one
    phrase repeated twice, each cell gets that phrase. Nothing else matches.
    """
    fixed = 0
    for table in doc.tables:
        grid = {(c.start_row_offset_idx, c.start_col_offset_idx): c
                for c in table.data.table_cells if c.row_span == 1 and c.col_span == 1}
        for (row, col), a in grid.items():
            b = grid.get((row, col + 1))
            if not b or not a.text.strip() or not b.text.strip():
                continue
            words = f"{a.text} {b.text}".split()
            half = len(words) // 2
            if len(words) % 2 == 0 and words[:half] == words[half:] and a.text.split() != words[:half]:
                a.text = b.text = " ".join(words[:half])
                fixed += 1
    return fixed


def _span(box: BoundingBox) -> tuple[float, float]:
    """Vertical extent of a box, whatever its coordinate origin."""
    return min(box.t, box.b), max(box.t, box.b)


def fix_overlapping_cells(doc: DoclingDocument, log: dict | None = None) -> int:
    """Resolve TableFormer cells that claim the same grid slot, using their page boxes.

    Two shapes are repaired; anything else is left as is and logged:
      dropped_duplicate  a small cell lying inside a taller one whose text
                         contains its text (a fragment of the same cell text);
      shrunk_span        a row-spanning cell whose box ends above (or starts
                         below) the other cell: its span is cut back to the rows
                         its box covers, so both texts stay, each in its own slot.
    Runs on page fragments before merging, so the boxes of one table share a page.
    """
    entries = log.setdefault("overlapping_cells", []) if log is not None else []
    fixed = 0
    def overlaps(table: TableItem):
        cells = table.data.table_cells
        return [(a, b) for i, a in enumerate(cells) for b in cells[i + 1:]
                if a.start_row_offset_idx < b.end_row_offset_idx and b.start_row_offset_idx < a.end_row_offset_idx
                and a.start_col_offset_idx < b.end_col_offset_idx and b.start_col_offset_idx < a.end_col_offset_idx]

    for table in doc.tables:
        page = table.prov[0].page_no if table.prov else None
        kept = set()
        while pending := [p for p in overlaps(table) if (id(p[0]), id(p[1])) not in kept]:
            a, b = pending[0]  # one repair at a time: spans and the cell list change
            action = _resolve_overlap(table, a, b)
            entries.append({"table": table, "page": page, "action": action})
            if action == "kept":
                kept.add((id(a), id(b)))
            else:
                fixed += 1
    return fixed


def _resolve_overlap(table: TableItem, a, b) -> str:
    tall, small = (a, b) if a.row_span >= b.row_span else (b, a)
    if tall.bbox is None or small.bbox is None or tall.row_span == small.row_span:
        return "kept"
    (tt, tb), (st, sb) = _span(tall.bbox), _span(small.bbox)
    tol = 2.0
    inside = (tall.bbox.l - tol <= small.bbox.l and small.bbox.r <= tall.bbox.r + tol
              and tt - tol <= st and sb <= tb + tol)
    norm = lambda s: " ".join(s.split())  # noqa: E731
    if inside and norm(small.text) and norm(small.text) in norm(tall.text):
        table.data.table_cells.remove(small)
        return "dropped_duplicate"
    if min(tb, sb) - max(tt, st) <= tol:  # no real vertical overlap
        if small.start_row_offset_idx >= tall.start_row_offset_idx + 1 and \
                small.end_row_offset_idx >= tall.end_row_offset_idx and sb > tb:  # small lies below
            tall.end_row_offset_idx = small.start_row_offset_idx
        elif small.end_row_offset_idx <= tall.end_row_offset_idx - 1 and \
                small.start_row_offset_idx <= tall.start_row_offset_idx and st < tt:  # small lies above
            tall.start_row_offset_idx = small.end_row_offset_idx
        else:
            return "kept"
        tall.row_span = tall.end_row_offset_idx - tall.start_row_offset_idx
        # A cell freed from a header span, in a row of header cells, is a header cell too
        # (else that header row stops counting as one and repeats survive page merging).
        if tall.column_header and not small.column_header:
            peers = [c for c in table.data.table_cells if c is not small
                     and c.start_row_offset_idx <= small.start_row_offset_idx < c.end_row_offset_idx]
            if peers and all(c.column_header for c in peers):
                small.column_header = True
        return "shrunk_span"
    return "kept"


def _finalise(doc: DoclingDocument, log: dict) -> dict:
    """Replace item objects in the log by their final self_ref (None if deleted).

    A table fragment merged into an earlier one is reported as that table.
    """
    merged_into = log.pop("_merged_into", {})

    def ref(obj: NodeItem) -> str | None:
        while id(obj) in merged_into:
            obj = merged_into[id(obj)][1]
        try:
            return obj.self_ref if RefItem(cref=obj.self_ref).resolve(doc) is obj else None
        except Exception:
            return None

    for entries in log.values():
        for e in entries if isinstance(entries, list) else []:
            for k, v in list(e.items()) if isinstance(e, dict) else []:
                if isinstance(v, NodeItem):
                    e[k] = ref(v)
    return log


def postprocess(doc: DoclingDocument, pdf: pdfium.PdfDocument,
                settings: ProtocolSettings | None = None) -> dict[str, int]:
    """Run all cleanup steps; returns step counts (small, JSON-serialisable).

    The full decisions log (merges, joins, footnotes, demoted headings, deleted
    TOC items, removed titles, ...: refs, pages and reasons, no text) is left in
    `postprocess.last_decisions`.
    """
    settings = settings or ProtocolSettings()
    log: dict = {}
    pdf_text = "\n".join(pdf[i].get_textpage().get_text_range() for i in range(len(pdf)))
    hyphens = fix_hyphen_spacing(doc, pdf_text)
    shadows = drop_table_shadow_pictures(doc, log)
    furniture = demote_stray_furniture(doc, settings, log)
    restored_headers = restore_unique_headers(doc, log)  # title-page header lines are content (e.g. NCT number)
    # OCR each bitmap figure once as a whole (per-region crops misread and jumble it); before flattening.
    reocr = reocr_figures(doc, pdf, log, order=settings.figure_ocr_order) if settings.figure_reocr else {}
    pictures = flatten_picture_text(doc, settings)
    list_lines = absorb_split_list_lines(doc, pdf)
    # A paragraph split into two items at a hyphenated line end: one item again, before reconcile_text
    # decides the hyphen from the PDF.
    hyphen_joins = join_hyphen_split_items(doc, log) if settings.join_hyphen_splits else 0
    # After the list repair, whose text is re-read from the PDF text layer.
    glyphs = normalize_symbol_glyphs(doc, settings, log)
    titles = resolve_repeated_table_titles(doc, settings, log)
    # Text repairs from pdfium character geometry: superscripts written as "^" ("10^9/L",
    # "Response^a"), real line-end hyphens restored, merged words split. After the title step
    # (its marker patterns expect "a." / "*") and any step that re-reads text from the PDF; before
    # merging tables, while every cell box is still on its own page.
    reconciled = (reconcile_text(doc, pdf, log, min_alignment=settings.reconcile_min_alignment)
                  if settings.reconcile_text else {})
    pairs = (pair_label_values(doc, log, max_page=settings.label_pairs_max_page)
             if settings.pair_label_values else 0)
    overlaps = fix_overlapping_cells(doc, log)  # before merging: boxes still share one page
    # Check every single-page table fragment against the PDF's ruling lines; rebuild fully ruled
    # ones whose TableFormer grid disagrees (fused or missing columns, text in the wrong row).
    # Keeps the cells' words (already repaired above), so it runs after reconcile_text.
    lattice = (correct_tables_with_lattice(doc, pdf, log, settings.table_lattice,
                                           min_rule_coverage=settings.lattice_min_rule_coverage,
                                           min_text_match=settings.lattice_min_text_match,
                                           header_note_gap=settings.header_note_gap or None,
                                           row_ruled=settings.lattice_row_ruled,
                                           min_row_rule_coverage=settings.lattice_min_row_rule_coverage,
                                           max_col_rule_coverage=settings.lattice_max_col_rule_coverage,
                                           text_rows=settings.lattice_text_rows,
                                           text_rows_unruled=settings.lattice_text_rows_unruled)
               if settings.table_lattice != "off" else {})
    merged = merge_split_tables(doc, settings, log)
    inner_headers = mark_inner_header_rows(doc, log)
    split_cells = fix_split_duplicate_cells(doc)
    if settings.heading_levels == "multi_signal":
        # Outline when it matches (identical to fix_heading_levels); otherwise numbering scheme
        # detection + TOC + level-1 consensus (structure.py). Imported here: structure imports this module.
        from structure import assign_heading_levels

        headings = assign_heading_levels(doc, pdf, settings, log)
    else:
        headings = fix_heading_levels(doc, pdf, settings, log)
    toc = rebuild_toc(doc, settings, log)
    reordered = nested = 0
    if settings.reading_order_repair:
        # Last: text read before a numbered heading although it lies below it on the page goes under
        # that heading in the body, so chunks, parents and protocol_api agree (needs final headings).
        from structure import repair_reading_order

        reordered = repair_reading_order(doc, log)
    if settings.heading_levels == "multi_signal":
        # After the final order: a "Label:" heading never outranks the numbered heading it follows
        # (the section index nests it; this gives the chunker's heading paths the same level).
        from structure import nest_colon_labels

        nested = nest_colon_labels(doc, log)
    postprocess.last_decisions = _finalise(doc, log)
    return {"hyphens_fixed": hyphens, "symbol_glyphs_fixed": glyphs, "shadow_pictures_dropped": shadows,
            "stray_furniture_moved": furniture, "single_page_headers_restored": restored_headers, "figures_reocr": reocr.get("reocr", 0),
            "figure_text_flattened": pictures,
            "list_items_repaired": list_lines, "hyphen_split_items_joined": hyphen_joins, **titles,
            "text_reconciled": sum(v for v in reconciled.values() if isinstance(v, int)),
            "label_value_pairs": pairs, "tables_lattice_corrected": lattice.get("corrected", 0),
            "tables_lattice_rows_corrected": lattice.get("corrected_rows", 0),
            "table_text_rows_joined": lattice.get("text_rows_joined", 0),
            "tables_merged": merged, "inner_header_rows": inner_headers, "split_cells_fixed": split_cells, "overlapping_cells_fixed": overlaps,
            "headings_releveled": headings, "toc_entries": toc, "reading_order_repairs": reordered,
            "colon_labels_nested": nested,
            "toc_items_deleted": log.get("toc_items_deleted", 0),
            "unknown_pua_kept": sum(log.get("unknown_pua", {}).values())}


postprocess.last_decisions = {}
