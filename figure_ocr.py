"""OCR each bitmap figure once, on the whole crop, and keep its text in reading order.

Docling OCRs every layout cluster inside a figure as a separate crop, so the
small clusters of a flow chart are read without context ("PI" -> "Id*") and come
back as fragments in no useful order. `reocr_figures` re-reads each figure that
has no PDF text layer inside it (a bitmap, or vector art with outlined text):

  1. render the figure region from the PDF with pdfium (scale 3 = 216 dpi, the
     scale Docling's own OCR uses), slightly padded so edge text is not clipped;
  2. OCR the whole crop once (RapidOCR, the models Docling loads);
  3. join same-row fragments into lines, group lines into blocks (a flow-chart
     box, a paragraph) by proximity, with a pixel check so lines on either side
     of a drawn border or a colour change are never grouped;
  4. order the blocks by recursive XY-cut (split at whitespace gaps; the
     direction whose gaps hold a drawn divider, else the wider gap, first), and
     the lines of a block top to bottom;
  5. replace the old OCR text items under the picture by one group per block
     (GroupItem named BLOCK_GROUP_NAME) holding one TextItem per line, each with
     its own provenance box. Items that come from the PDF text layer, captions,
     footnotes and non-text children are left alone.

An old OCR item is kept (as one more line, ordered with the others) only if no
new line overlaps it and it has a word the new OCR lacks, so a figure never
loses text the old pass found in a region the new pass did not read. Figures
with text in the PDF text layer are vector figures and are not touched. The
decisions log gets per-figure counts and reasons, never document text.

Integration: run after drop_table_shadow_pictures and before
flatten_picture_text; flatten can keep this order via `block_text(doc, pic)`.
"""

from __future__ import annotations

import logging
import re
import time
import warnings
from collections import Counter
from dataclasses import dataclass

import numpy as np

BLOCK_GROUP_NAME = "figure_ocr_block"
DEFAULT_SCALE = 3.0  # x 72 dpi; Docling's RapidOCR stage uses 3.0 too
MAX_SIDE_PX = 2000  # RapidOCR downsizes larger images anyway (Global.max_side_len)
PAD_PT = 4.0  # crop padding; lines are kept only if their centre is inside the figure
MAX_TEXT_LAYER_CHARS = 20  # more PDF text-layer characters inside => vector figure, left alone
MIN_BITMAP_SHARE = 0.5  # figures without old OCR text are read only when mostly a bitmap
SKIP_CLASSES = {"logo", "icon", "signature", "stamp", "qr_code", "bar_code"}


# --------------------------------------------------------------------------- geometry

@dataclass
class Line:
    """One OCR line in crop pixels (top-left origin)."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    score: float = 1.0
    old: bool = False  # kept from the previous (per-cluster) OCR

    @property
    def h(self) -> float:
        return max(1.0, self.y1 - self.y0)

    @property
    def w(self) -> float:
        return max(1.0, self.x1 - self.x0)

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def _union(lines: list[Line]) -> tuple[float, float, float, float]:
    return (min(l.x0 for l in lines), min(l.y0 for l in lines),
            max(l.x1 for l in lines), max(l.y1 for l in lines))


def _has_edge(img: np.ndarray | None, x0: float, x1: float, y0: float, y1: float, axis: int) -> bool:
    """A drawn border or a colour change between two lines.

    The region between them is averaged across the gap's long side (axis=0:
    a horizontal gap between rows, profile along y; axis=1: a vertical gap
    between fragments of a row, profile along x). A sharp step or a large
    range in that profile means a line or a background change, i.e. two boxes.
    """
    if img is None:
        return False
    hgt, wid = img.shape[:2]
    xa, xb = max(0, int(round(x0))), min(wid, int(round(x1)))
    ya, yb = max(0, int(round(y0))), min(hgt, int(round(y1)))
    if xb - xa < 2 or yb - ya < 2:
        return False
    region = img[ya:yb, xa:xb].astype(np.float32)
    profile = region.mean(axis=1 if axis == 0 else 0)  # (n, channels)
    if profile.shape[0] < 2:
        return False
    step = np.abs(np.diff(profile, axis=0)).max()
    spread = (profile.max(axis=0) - profile.min(axis=0)).max()
    return bool(step > 35 or spread > 45)


def assemble_lines(frags: list[Line], img: np.ndarray | None = None) -> list[Line]:
    """Join fragments on the same text row that are only a word gap apart."""
    out: list[Line] = []
    for f in sorted(frags, key=lambda l: (l.x0, l.y0)):
        best = None
        for l in out:
            if l.old or f.old:
                continue
            hh = min(l.h, f.h)
            v_overlap = min(l.y1, f.y1) - max(l.y0, f.y0)
            gap = f.x0 - l.x1
            if v_overlap >= 0.6 * hh and -0.3 * hh <= gap <= 0.8 * max(l.h, f.h) and max(l.h, f.h) <= 1.5 * hh:
                ya, yb = max(l.y0, f.y0), min(l.y1, f.y1)
                if not _has_edge(img, l.x1 + 1, f.x0 - 1, ya + 0.2 * (yb - ya), yb - 0.2 * (yb - ya), axis=1):
                    best = l
                    break
        if best is None:
            out.append(Line(f.x0, f.y0, f.x1, f.y1, f.text, f.score, f.old))
        else:
            best.text = f"{best.text} {f.text}"
            best.x1, best.y0, best.y1 = max(best.x1, f.x1), min(best.y0, f.y0), max(best.y1, f.y1)
            best.score = min(best.score, f.score)
    return out


def group_blocks(lines: list[Line], img: np.ndarray | None = None) -> list[list[Line]]:
    """Group lines into blocks: each line joins the nearest line below it when
    they overlap horizontally, have similar heights, are at most 1.5 line
    heights apart (a blank line inside a box; 1.0 without pixels), and nothing
    is drawn between them (no border, no background change)."""
    n = len(lines)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(lines):
        below = None
        for j, b in enumerate(lines):
            if j == i or b.cy <= a.cy:
                continue
            hh = min(a.h, b.h)
            if b.y0 < a.y1 - 0.3 * hh:  # same row or overlapping: not "below"
                continue
            overlap = min(a.x1, b.x1) - max(a.x0, b.x0)
            if overlap < 0.3 * min(a.w, b.w):
                continue
            if below is None or b.y0 < lines[below].y0:
                below = j
        if below is None:
            continue
        b = lines[below]
        hh, gap = min(a.h, b.h), b.y0 - a.y1
        # Without pixels there is no evidence of a shared background: stay tighter.
        if max(a.h, b.h) > 1.6 * hh or gap > (1.5 if img is not None else 1.0) * hh:
            continue
        xa, xb = max(a.x0, b.x0), min(a.x1, b.x1)
        inset = 0.1 * (xb - xa)
        if not _has_edge(img, xa + inset, xb - inset, a.y1 + 1, b.y0 - 1, axis=0):
            parent[find(i)] = find(below)
    groups: dict[int, list[Line]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(lines[i])
    return [_rows(g) for g in groups.values()]


def _rows(lines: list[Line]) -> list[Line]:
    """Lines of a block: top to bottom, left to right within a text row."""
    rows: list[list[Line]] = []
    for l in sorted(lines, key=lambda l: l.cy):
        if rows and min(rows[-1][-1].y1, l.y1) - max(rows[-1][-1].y0, l.y0) >= 0.5 * min(rows[-1][-1].h, l.h):
            rows[-1].append(l)
        else:
            rows.append([l])
    return [l for r in rows for l in sorted(r, key=lambda l: l.x0)]


def _gaps(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Whitespace gaps between the merged intervals of a projection."""
    gaps, end = [], None
    for a, b in sorted(spans):
        if end is not None and a > end:
            gaps.append((end, a))
        end = b if end is None else max(end, b)
    return gaps


def ink_mask(img: np.ndarray) -> np.ndarray:
    """Pixels that differ clearly from the dominant (background) colour."""
    q = (img[..., :3] // 32).reshape(-1, 3).astype(np.int32)
    codes = q[:, 0] * 64 + q[:, 1] * 8 + q[:, 2]
    mode = np.bincount(codes).argmax()
    bg = np.array([mode // 64, (mode // 8) % 8, mode % 8]) * 32 + 16
    return np.abs(img[..., :3].astype(np.int16) - bg).max(axis=-1) > 60


def _longest_run(a: np.ndarray) -> int:
    """Longest run of True along axis 0, over all columns of a 2-D boolean array."""
    run = np.zeros(a.shape[1], np.int32)
    best = 0
    for row in a:
        run = (run + 1) * row
        best = max(best, int(run.max(initial=0)))
    return best


def _ruled(ink: np.ndarray | None, gaps: list[tuple[float, float]], lo: float, hi: float, rows: bool) -> bool:
    """A drawn line inside one of the gaps, running along it for >= 70 % of the
    blocks' extent (a panel or card border, a divider): evidence for cutting there."""
    if ink is None or hi - lo < 4:
        return False
    for a, b in gaps:
        if rows:  # horizontal gap: rows a..b, a line runs along x over lo..hi
            sub = ink[int(a):int(np.ceil(b)), int(lo):int(np.ceil(hi))].T
        else:
            sub = ink[int(lo):int(np.ceil(hi)), int(a):int(np.ceil(b))]
        if sub.size and _longest_run(sub) >= 0.7 * (hi - lo):
            return True
    return False


def order_blocks(blocks: list[list[Line]], mode: str = "auto", stats: Counter | None = None,
                 ink: np.ndarray | None = None) -> list[list[Line]]:
    """Reading order of blocks by recursive XY-cut.

    At each level the blocks are split at every whitespace gap of one direction:
    rows (horizontal gaps) or columns (vertical gaps). `auto` cuts where a drawn
    line runs along the gaps of only one direction (panels, cards, dividers;
    needs `ink`, see ink_mask); otherwise it takes the direction with the widest
    gap (rows on a tie). So a grid reads row by row, a left-to-right schema whose
    boxes leave no full-width horizontal gap reads column by column, and side-by-
    side bordered cards read card by card. `rows` / `columns` prefer that
    direction whenever it has a gap. With no gap at all, blocks read top to bottom.
    """
    if len(blocks) <= 1:
        return blocks
    boxes = [_union(b) for b in blocks]
    hg = _gaps([(y0, y1) for _, y0, _, y1 in boxes])
    vg = _gaps([(x0, x1) for x0, _, x1, _ in boxes])
    if not hg and not vg:
        if stats is not None:
            stats["xy_fallbacks"] += 1
        return [blocks[i] for i in sorted(range(len(blocks)), key=lambda i: (boxes[i][1], boxes[i][0]))]
    widest = lambda gs: max((b - a for a, b in gs), default=0.0)  # noqa: E731
    if mode == "rows":
        use_rows = bool(hg)
    elif mode == "columns":
        use_rows = not vg
    elif not hg or not vg:
        use_rows = bool(hg)
    else:
        x0, y0 = min(b[0] for b in boxes), min(b[1] for b in boxes)
        x1, y1 = max(b[2] for b in boxes), max(b[3] for b in boxes)
        ruled_rows, ruled_cols = _ruled(ink, hg, x0, x1, True), _ruled(ink, vg, y0, y1, False)
        use_rows = ruled_rows if ruled_rows != ruled_cols else widest(hg) >= widest(vg)
        if stats is not None and ruled_rows != ruled_cols:
            stats["xy_ruled_cuts"] += 1
    cuts = [(a + b) / 2 for a, b in (hg if use_rows else vg)]
    parts: list[list[list[Line]]] = [[] for _ in range(len(cuts) + 1)]
    for blk, (x0, y0, x1, y1) in zip(blocks, boxes):
        centre = (y0 + y1) / 2 if use_rows else (x0 + x1) / 2
        parts[sum(centre > c for c in cuts)].append(blk)
    return [b for p in parts if p for b in order_blocks(p, mode, stats, ink)]


def reading_order(frags: list[Line], img: np.ndarray | None = None, mode: str = "auto",
                  stats: Counter | None = None) -> list[list[Line]]:
    """OCR fragments -> ordered blocks of ordered lines."""
    lines = assemble_lines(frags, img)
    ink = ink_mask(img) if img is not None else None
    return order_blocks(group_blocks(lines, img), mode, stats, ink)


# --------------------------------------------------------------------------- OCR engine

_READERS: dict[str, object] = {}


def _rapidocr():
    if "rapidocr" not in _READERS:
        from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
        from docling.datamodel.pipeline_options import RapidOcrOptions
        from docling.models.stages.ocr.rapid_ocr_model import RapidOcrModel

        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        model = RapidOcrModel(enabled=True, artifacts_path=None, options=RapidOcrOptions(backend="torch"),
                              accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4))
        logging.getLogger("RapidOCR").setLevel(logging.WARNING)
        _READERS["rapidocr"] = model.reader
    return _READERS["rapidocr"]


_LOADERS = {"rapidocr": _rapidocr}


_ENCLOSED = re.compile("[\u2460-\u24ff]")  # circled/parenthesised digits and letters


def clean_text(text: str) -> str:
    """Collapse whitespace; circled digits/letters ("①", read off step markers) as plain ones."""
    import unicodedata

    text = _ENCLOSED.sub(lambda m: unicodedata.normalize("NFKC", m.group()), str(text))
    return " ".join(text.split())


def ocr_image(img, engine: str = "rapidocr") -> list[Line]:
    """OCR a whole image once; one Line per detected text box."""
    if engine not in _LOADERS:
        raise ValueError(f"unknown OCR engine {engine!r} (supported: {sorted(_LOADERS)})")
    reader = _LOADERS[engine]()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        res = reader(np.asarray(img.convert("RGB")))
    if res is None or res.boxes is None:
        return []
    out = []
    for box, text, score in zip(res.boxes.tolist(), res.txts, res.scores):
        text = clean_text(text)
        if not text:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append(Line(min(xs), min(ys), max(xs), max(ys), text, float(score)))
    return out


# --------------------------------------------------------------------------- PDF helpers

class _PageInfo:
    """Text-layer character centres and image-object boxes of one PDF page (PDF points, bottom-left)."""

    def __init__(self, page):
        import pypdfium2.raw as pdfium_c

        tp = page.get_textpage()
        self.chars = []
        for i in range(tp.count_chars()):
            ch = tp.get_text_range(i, 1)
            if ch.strip():
                l, b, r, t = tp.get_charbox(i)
                self.chars.append(((l + r) / 2, (b + t) / 2))
        self.images = [obj.get_bounds() for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=4)]

    def chars_in(self, l: float, b: float, r: float, t: float) -> int:
        return sum(l <= x <= r and b <= y <= t for x, y in self.chars)

    def bitmap_share(self, l: float, b: float, r: float, t: float) -> float:
        area = max(1e-6, (r - l) * (t - b))
        cover = sum(max(0.0, min(r, ir) - max(l, il)) * max(0.0, min(t, it) - max(b, ib))
                    for il, ib, ir, it in self.images)
        return min(1.0, cover / area)


def _words(text: str) -> Counter:
    return Counter(re.findall(r"\w+", text.lower()))


def _overlap(px: tuple[float, float, float, float], ln: Line) -> float:
    """Intersection of a box and a line, relative to the smaller of the two."""
    inter = (max(0.0, min(px[2], ln.x1) - max(px[0], ln.x0)) *
             max(0.0, min(px[3], ln.y1) - max(px[1], ln.y0)))
    return inter / max(1e-6, min((px[2] - px[0]) * (px[3] - px[1]), ln.w * ln.h))


_CJK = re.compile("[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]")


def _script_noise(lines: list[Line]) -> list[Line]:
    """Short CJK-only lines in a figure whose text is otherwise Latin: the
    multilingual recogniser reading a glyph-like shape, e.g. an error bar as a
    CJK character)."""
    letters = "".join(ch for ln in lines for ch in ln.text if ch.isalpha())
    if not letters or len(_CJK.findall(letters)) > 0.2 * len(letters):
        return []
    return [ln for ln in lines if _CJK.search(ln.text) and len(ln.text.replace(" ", "")) <= 3
            and not re.search(r"[A-Za-z0-9]", ln.text)]


def _figure_class(pic) -> tuple[str | None, float]:
    meta = getattr(pic, "meta", None)
    if not (meta and meta.classification and meta.classification.predictions):
        return None, 0.0
    pred = meta.classification.get_main_prediction()
    return pred.class_name, pred.confidence or 0.0


def _old_text_children(doc, pic):
    """Removable text children of a picture: TextItems, or groups of TextItems
    (captions and footnotes excluded); with their leaves."""
    from docling_core.types.doc import GroupItem, TextItem

    keep = {r.cref for r in [*pic.captions, *pic.footnotes]}
    out = []
    for ref in pic.children:
        if ref.cref in keep:
            continue
        child = ref.resolve(doc)
        if isinstance(child, GroupItem):
            leaves = [x for x, _ in doc.iterate_items(root=child, with_groups=False, traverse_pictures=True)]
            if leaves and all(isinstance(x, TextItem) and x.self_ref not in keep for x in leaves):
                out.append((child, leaves))
        elif isinstance(child, TextItem) and child.self_ref not in keep:
            out.append((child, [child]))
    return out


# --------------------------------------------------------------------------- main entry

def reocr_figures(doc, pdf, log: dict | None = None, engine: str = "rapidocr", scale: float = DEFAULT_SCALE,
                  order: str = "auto", keep_uncovered_old: bool = True) -> dict:
    """Re-OCR every figure without a PDF text layer, once per figure, in reading order.

    `pdf` is the pypdfium2 document the DoclingDocument was converted from;
    `order` is auto | rows | columns (see order_blocks). Returns counts;
    `log["figure_ocr"]` gets one entry per figure: picture, page, decision
    (reocr | skipped:<reason> | no_text_found), line/block/word counts and
    seconds; never text.
    """
    from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, GroupLabel, ProvenanceItem

    if engine not in _LOADERS:
        raise ValueError(f"unknown OCR engine {engine!r} (supported: {sorted(_LOADERS)})")
    log = log if log is not None else {}
    entries = log.setdefault("figure_ocr", [])
    stats = Counter()
    pages: dict[int, _PageInfo] = {}
    to_delete = []
    for pic in doc.pictures:
        if not pic.prov:
            continue
        prov = pic.prov[0]
        entry = {"picture": pic, "page": prov.page_no}
        entries.append(entry)
        stats["figures"] += 1
        cls, conf = _figure_class(pic)
        size = doc.pages[prov.page_no].size if prov.page_no in doc.pages else None
        if len(pic.prov) > 1 or size is None:
            entry["decision"] = "skipped:geometry"
            stats["skipped:geometry"] += 1
            continue
        box = prov.bbox.to_bottom_left_origin(size.height)
        page = pdf[prov.page_no - 1]
        pw, ph = page.get_size()
        if page.get_rotation() or abs(pw - size.width) > 1 or abs(ph - size.height) > 1:
            entry["decision"] = "skipped:page_geometry"
            stats["skipped:page_geometry"] += 1
            continue
        if cls in SKIP_CLASSES and conf >= 0.8 and box.area() / (size.width * size.height) < 0.05:
            entry["decision"] = "skipped:class"
            stats["skipped:class"] += 1
            continue
        info = pages.setdefault(prov.page_no, _PageInfo(page))
        n_chars = info.chars_in(box.l, box.b, box.r, box.t)
        share = info.bitmap_share(box.l, box.b, box.r, box.t)
        entry.update(text_layer_chars=n_chars, bitmap_share=round(share, 2))
        if n_chars > MAX_TEXT_LAYER_CHARS:
            entry["decision"] = "skipped:vector_text"
            stats["skipped:vector_text"] += 1
            continue

        # Old OCR children: text items inside the figure with no text-layer characters under them.
        old = []
        for node, leaves in _old_text_children(doc, pic):
            boxes = []
            for it in leaves:
                if not it.prov or it.prov[0].page_no != prov.page_no:
                    break
                ob = it.prov[0].bbox.to_bottom_left_origin(size.height)
                if ob.intersection_over_self(box) < 0.5 or info.chars_in(ob.l, ob.b, ob.r, ob.t):
                    break
                boxes.append(ob)
            else:
                old.append((node, leaves, boxes))
        if not old and share < MIN_BITMAP_SHARE:
            entry["decision"] = "skipped:no_text_no_bitmap"
            stats["skipped:no_text_no_bitmap"] += 1
            continue

        if engine not in _READERS:  # load once, outside the per-figure timing
            load = time.time()
            _LOADERS[engine]()
            stats["model_load_seconds"] += round(time.time() - load, 1)
        start = time.time()
        l, b, r, t = (max(0.0, box.l - PAD_PT), max(0.0, box.b - PAD_PT),
                      min(size.width, box.r + PAD_PT), min(size.height, box.t + PAD_PT))
        s = min(scale, MAX_SIDE_PX / max(r - l, t - b))
        img = page.render(scale=s, crop=(l, b, size.width - r, size.height - t)).to_pil().convert("RGB")
        arr = np.asarray(img)

        def to_pdf(x0, y0, x1, y1):  # crop pixels -> PDF points, bottom-left origin
            return l + x0 / s, t - y1 / s, l + x1 / s, t - y0 / s

        lines = []
        for ln in ocr_image(img, engine):
            x0, y0, x1, y1 = to_pdf(ln.x0, ln.y0, ln.x1, ln.y1)
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if not (box.l <= cx <= box.r and box.b <= cy <= box.t):
                stats["lines_outside"] += 1
                continue
            if info.chars_in(x0, y0, x1, y1):
                stats["lines_on_text_layer"] += 1
                continue
            lines.append(ln)
        noise = {id(ln) for ln in _script_noise(lines)}
        if noise:
            stats["lines_script_noise"] += len(noise)
            lines = [ln for ln in lines if id(ln) not in noise]

        # Old items are dropped where the new OCR read the same region; one is kept
        # only if no new line overlaps it AND it has words the new OCR lacks.
        kept_old = 0
        old_words, new_words = Counter(), Counter(w for ln in lines for w in _words(ln.text).elements())
        for node, leaves, boxes in old:
            for it, ob in zip(leaves, boxes):
                old_words += _words(it.text)
                px = ((ob.l - l) * s, (t - ob.t) * s, (ob.r - l) * s, (t - ob.b) * s)
                if keep_uncovered_old and not any(_overlap(px, ln) >= 0.2 for ln in lines if not ln.old) and any(
                        len(w) > 1 and w not in new_words for w in _words(it.text)):
                    lines.append(Line(*px, clean_text(it.text), 1.0, old=True))
                    kept_old += 1
        if not lines:
            entry.update(decision="no_text_found", seconds=round(time.time() - start, 1))
            stats["no_text_found"] += 1
            continue
        blocks = reading_order(lines, arr, order, stats)

        for blk in blocks:
            grp = doc.add_group(label=GroupLabel.UNSPECIFIED, name=BLOCK_GROUP_NAME, parent=pic)
            for ln in blk:
                x0, y0, x1, y1 = to_pdf(ln.x0, ln.y0, ln.x1, ln.y1)
                bbox = BoundingBox(l=x0, t=y1, r=x1, b=y0, coord_origin=CoordOrigin.BOTTOMLEFT)
                doc.add_text(label=DocItemLabel.TEXT, text=ln.text, parent=grp,
                             prov=ProvenanceItem(page_no=prov.page_no, bbox=bbox, charspan=(0, len(ln.text))))
        to_delete += [node for node, _, _ in old]
        all_new = Counter(w for blk in blocks for ln in blk for w in _words(ln.text).elements())
        entry.update(decision="reocr", kind="bitmap" if share >= MIN_BITMAP_SHARE else "no_text_layer",
                     scale=round(s, 2), old_items=sum(len(lv) for _, lv, _ in old), old_items_kept=kept_old,
                     lines=sum(len(bk) for bk in blocks), blocks=len(blocks),
                     words_old=sum(old_words.values()), words_new=sum(all_new.values()),
                     words_added=sum((new_words - old_words).values()),
                     words_removed=sum((old_words - all_new).values()),
                     seconds=round(time.time() - start, 1))
        stats["reocr"] += 1
        for k in ("lines", "blocks", "old_items", "old_items_kept", "words_old", "words_new",
                  "words_added", "words_removed"):
            stats[k] += entry[k]
        stats["seconds"] += entry["seconds"]
    if to_delete:
        doc.delete_items(node_items=to_delete)
    return dict(stats)


# --------------------------------------------------------------------------- consumers

def ocr_blocks(doc, pic) -> list[list] | None:
    """The figure's OCR text as blocks of TextItems, if reocr_figures produced it.

    None when the picture's (non-caption) text children are not all figure-OCR
    blocks, e.g. a vector figure; callers then fall back to their own ordering.
    """
    from docling_core.types.doc import GroupItem, TextItem

    keep = {r.cref for r in [*pic.captions, *pic.footnotes]}
    blocks = []
    for ref in pic.children:
        if ref.cref in keep:
            continue
        child = ref.resolve(doc)
        if isinstance(child, GroupItem) and child.name == BLOCK_GROUP_NAME:
            blocks.append([x for x in (r.resolve(doc) for r in child.children) if isinstance(x, TextItem)])
        elif isinstance(child, (TextItem, GroupItem)):
            return None
    return blocks or None


def block_text(doc, pic, line_sep: str = "; ", block_sep: str = " | ") -> str | None:
    """Flattened figure text that keeps reading order and boxes: lines of one
    block joined by `line_sep` (a space after a line ending in ":", as in
    "Primary Endpoint: ORR by BICR"), blocks by `block_sep`. None if not re-OCR'd."""
    blocks = ocr_blocks(doc, pic)
    if blocks is None:
        return None
    out = []
    for b in blocks:
        text = ""
        for it in b:
            line = " ".join(it.text.split())
            text = line if not text else f"{text}{' ' if text.endswith(':') else line_sep}{line}"
        if text:
            out.append(text)
    return block_sep.join(out)


def main() -> None:
    """Counts only (safe for private protocols): python figure_ocr.py MAIN P1 Bayer_NCT04304508 ..."""
    import argparse
    import json
    import sys

    import pypdfium2 as pdfium

    from test_protocols import raw_doc, source_pdf

    parser = argparse.ArgumentParser()
    parser.add_argument("labels", nargs="+")
    parser.add_argument("--scale", type=float, default=DEFAULT_SCALE)
    args = parser.parse_args()
    for label in args.labels:
        try:
            doc = raw_doc(label)
            log: dict = {}
            counts = reocr_figures(doc, pdfium.PdfDocument(str(source_pdf(label))), log, scale=args.scale)
            per_fig = [{k: v for k, v in e.items() if k != "picture"} for e in log["figure_ocr"]]
            print(label, json.dumps(counts), flush=True)
            if not (label.startswith("P") and label[1:].isdigit()):
                for e in per_fig:
                    print("   ", json.dumps(e))
        except BaseException as exc:  # noqa: BLE001 - never print messages (may quote documents)
            print(label, "failed:", type(exc).__name__, file=sys.stderr)


if __name__ == "__main__":
    main()
