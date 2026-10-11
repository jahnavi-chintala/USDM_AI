"""Self-test for figure_ocr.py (whole-crop figure OCR in reading order).

Run: `python tests/test_figure_ocr.py`. The unit tests use synthetic line boxes;
the end-to-end tests build a small PDF by hand: page 1 holds a flow chart
drawn with PIL and embedded as a JPEG (no text layer) plus a vector-text
figure; page 2 holds a bitmap with a text-layer label printed over it. The
end-to-end tests run RapidOCR on CPU (model load ~10 s, then a few seconds).
"""

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from docling_core.types.doc import BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ProvenanceItem, Size
from PIL import Image, ImageDraw, ImageFont

import figure_ocr as fo


def L(x0, y0, x1, y1, text):
    return fo.Line(x0, y0, x1, y1, text)


def texts(blocks):
    return [[l.text for l in b] for b in blocks]


# ---------------------------------------------------------------- ordering (no OCR)

def test_columns_schema() -> None:
    # Left-to-right schema: the left and right boxes are vertically centred on
    # the two stacked middle boxes, so there is no full-width row gap.
    frags = [
        L(400, 60, 600, 90, "Arm A (n=40)"), L(410, 95, 590, 125, "Drug X 10 mg"),
        L(60, 150, 220, 180, "Screening"), L(50, 185, 230, 215, "Day -28 to -1"),
        L(400, 260, 600, 290, "Arm B (n=40)"), L(440, 295, 560, 325, "Placebo"),
        L(780, 150, 920, 180, "Follow-up"), L(790, 185, 910, 215, "30 days"),
    ]
    got = texts(fo.reading_order(frags))
    assert got == [["Screening", "Day -28 to -1"], ["Arm A (n=40)", "Drug X 10 mg"],
                   ["Arm B (n=40)", "Placebo"], ["Follow-up", "30 days"]], got


def test_grid_reads_by_rows() -> None:
    # Quadrant labels: the row gap is wider than the column gap, so rows first.
    frags = [
        L(380, 120, 560, 160, "Left Upper"), L(380, 160, 650, 200, "Quadrant (LUQ)"),
        L(80, 120, 280, 160, "Right Upper"), L(10, 160, 280, 200, "Quadrant (RUQ)"),
        L(80, 430, 280, 470, "Right Lower"), L(10, 470, 280, 510, "Quadrant (RLQ)"),
        L(380, 430, 560, 470, "Left Lower"), L(380, 470, 650, 510, "Quadrant (LLQ)"),
    ]
    got = [l.text for b in fo.reading_order(frags) for l in b]
    assert got == ["Right Upper", "Quadrant (RUQ)", "Left Upper", "Quadrant (LUQ)",
                   "Right Lower", "Quadrant (RLQ)", "Left Lower", "Quadrant (LLQ)"], got
    # An explicit column preference reads the same grid column by column.
    got = [l.text for b in fo.reading_order(frags, mode="columns") for l in b]
    assert got[:4] == ["Right Upper", "Quadrant (RUQ)", "Right Lower", "Quadrant (RLQ)"], got


def test_same_row_fragments_join() -> None:
    lines = fo.assemble_lines([L(100, 10, 200, 40, "Cohort A"), L(210, 12, 300, 40, "(n=120)"),
                               L(600, 10, 700, 40, "Primary")])
    assert sorted(l.text for l in lines) == ["Cohort A (n=120)", "Primary"], [l.text for l in lines]


def test_border_splits_blocks() -> None:
    # Two boxes, one above the other, 1.2 line heights apart: the drawn border
    # between them keeps them apart; inside a box a blank line does not split.
    img = np.full((400, 400, 3), 255, np.uint8)
    img[100:103, 20:380] = 0  # bottom border of the upper box
    img[118:121, 20:380] = 0  # top border of the lower box
    lines = [L(50, 10, 300, 40, "Box one line 1"), L(50, 70, 300, 100 - 4, "Box one line 3"),
             L(50, 130, 300, 160, "Box two line 1")]
    got = texts(fo.order_blocks(fo.group_blocks(lines, img)))
    assert got == [["Box one line 1", "Box one line 3"], ["Box two line 1"]], got
    # Without the border the same geometry is one block.
    img[:] = 255
    assert len(fo.group_blocks(lines, img)) == 1


def test_bordered_cards_read_card_by_card() -> None:
    # Three bordered cards side by side; each has a caption block at the top and a
    # label at the bottom. The row gap (picture area) is wider than the column gaps,
    # so without pixels the cards read row by row; their borders say: by card.
    img = np.full((400, 640, 3), 255, np.uint8)
    for x0 in (10, 220, 430):
        img[5:395, x0:x0 + 3] = (200, 0, 60)
        img[5:395, x0 + 197:x0 + 200] = (200, 0, 60)
    frags = []
    for i, x0 in enumerate((10, 220, 430)):
        frags += [L(x0 + 30, 30, x0 + 170, 60, f"25% type {i}"), L(x0 + 30, 62, x0 + 170, 92, f"subtype {i}"),
                  L(x0 + 30, 330, x0 + 170, 360, f"Treatment {i}")]
    rows = [l.text for b in fo.reading_order(frags) for l in b]
    assert rows[:3] == ["25% type 0", "subtype 0", "25% type 1"], rows
    cards = [l.text for b in fo.reading_order(frags, img) for l in b]
    assert cards[:4] == ["25% type 0", "subtype 0", "Treatment 0", "25% type 1"], cards


def test_script_noise() -> None:
    lines = [L(0, 0, 50, 20, "300"), L(0, 30, 80, 50, "H-score"), L(100, 0, 120, 20, "工"),
             L(0, 60, 200, 80, "Low grade (n=77)")]
    assert [l.text for l in fo._script_noise(lines)] == ["工"]
    assert fo._script_noise([L(0, 0, 50, 20, "临床试验"), L(0, 30, 50, 50, "工")]) == []


def test_clean_text() -> None:
    assert fo.clean_text("  ①  step② ") == "1 step2"


# ---------------------------------------------------------------- synthetic PDF

PAGE_W, PAGE_H = 612, 792
CHART_PT = (100, 480, 500, 700)  # l, b, r, t of the flow chart image on page 1
VECTOR_PT = (90, 270, 320, 330)  # vector-text figure on page 1
MIXED_PT = (100, 450, 400, 650)  # page 2: bitmap with a text-layer label over it


def _font(px: int):
    try:
        return ImageFont.truetype("arial.ttf", px)
    except OSError:
        return ImageFont.load_default(size=px)


def flow_chart() -> Image.Image:
    """A 3-column schema at 180 dpi (400 x 220 pt)."""
    img = Image.new("RGB", (1000, 550), "white")
    d = ImageDraw.Draw(img)
    f = _font(30)

    def box(x0, y0, x1, y1, lines, gap_after=()):
        d.rounded_rectangle((x0, y0, x1, y1), radius=18, fill=(205, 222, 255), outline=(0, 0, 128), width=3)
        y = y0 + 25
        for i, s in enumerate(lines):
            w = d.textlength(s, font=f)
            d.text(((x0 + x1 - w) / 2, y), s, fill="black", font=f)
            y += 42 + (40 if i in gap_after else 0)

    box(30, 170, 280, 330, ["Screening", "Day -28 to -1"])
    box(360, 30, 640, 250, ["Arm A (n=40)", "Drug X 10 mg", "Oral daily"], gap_after=(1,))
    box(360, 300, 640, 470, ["Arm B (n=40)", "Placebo"])
    box(720, 170, 970, 330, ["Follow-up", "30 days"])
    return img


def photo_with_space() -> Image.Image:
    img = Image.new("RGB", (750, 500), (230, 200, 180))
    d = ImageDraw.Draw(img)
    d.text((60, 60), "Injection site", fill="black", font=_font(40))
    return img


def _jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def make_pdf(path: Path, pages: list[dict]) -> None:
    """Minimal PDF: per page an optional JPEG image (placed at `rect`) and Helvetica text lines."""
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    pages_id = add(b"")  # placeholder, filled below
    for p in pages:
        res, content = [], b""
        if p.get("image") is not None:
            img = p["image"]
            data = _jpeg(img)
            im = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                     b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (img.width, img.height, len(data))
                     + data + b"\nendstream")
            res.append(b"/XObject << /Im1 %d 0 R >>" % im)
            l, b, r, t = p["rect"]
            content += b"q %.2f 0 0 %.2f %.2f %.2f cm /Im1 Do Q\n" % (r - l, t - b, l, b)
        for x, y, s in p.get("text", []):
            content += b"BT /F1 12 Tf %.2f %.2f Td (%s) Tj ET\n" % (x, y, s.encode("latin-1"))
        cs = add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources << /Font << /F1 %d 0 R >> %s >> "
                            b"/Contents %d 0 R >>" % (pages_id, PAGE_W, PAGE_H, font, b" ".join(res), cs)))
    objs[pages_id - 1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % i for i in page_ids), len(page_ids))
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref)
    path.write_bytes(bytes(out))


def pv(page: int, l: float, b: float, r: float, t: float) -> ProvenanceItem:
    return ProvenanceItem(page_no=page, bbox=BoundingBox(l=l, t=t, r=r, b=b, coord_origin=CoordOrigin.BOTTOMLEFT),
                          charspan=(0, 1))


def build(tmp: Path):
    import pypdfium2 as pdfium

    pdf_path = tmp / "synthetic.pdf"
    make_pdf(pdf_path, [
        {"image": flow_chart(), "rect": CHART_PT,
         "text": [(100, 300, "Vector label Alpha"), (100, 280, "Vector label Beta")]},
        {"image": photo_with_space(), "rect": MIXED_PT, "text": [(300, 470, "Key")]},
    ])
    d = DoclingDocument(name="synthetic")
    for p in (1, 2):
        d.add_page(page_no=p, size=Size(width=PAGE_W, height=PAGE_H))
    # Page 1, bitmap flow chart, with per-cluster OCR as Docling leaves it (fragments, a misread).
    chart = d.add_picture(prov=pv(1, *CHART_PT))
    cap = d.add_text(DocItemLabel.CAPTION, "Figure 1. Study schema", prov=pv(1, 100, 702, 300, 712), parent=chart)
    chart.captions.append(cap.get_ref())
    s = 1000 / 400  # image px per pt
    for text, (x0, y0, x1, y1) in [("Arm A(n=40)", (400, 50, 600, 90)), ("Id•", (60, 200, 200, 240)),
                                   ("Placebo Follow-up", (400, 350, 950, 390)),
                                   ("Legacy note", (700, 470, 960, 530))]:  # a region the new OCR finds blank
        d.add_text(DocItemLabel.SECTION_HEADER if text == "Arm A(n=40)" else DocItemLabel.TEXT, text, parent=chart,
                   prov=pv(1, CHART_PT[0] + x0 / s, CHART_PT[3] - y1 / s, CHART_PT[0] + x1 / s, CHART_PT[3] - y0 / s))
    # Page 1, vector figure: its text comes from the PDF text layer.
    vector = d.add_picture(prov=pv(1, *VECTOR_PT))
    d.add_text(DocItemLabel.TEXT, "Vector label Alpha", prov=pv(1, 100, 297, 205, 309), parent=vector)
    d.add_text(DocItemLabel.TEXT, "Vector label Beta", prov=pv(1, 100, 277, 200, 289), parent=vector)
    # Page 2, bitmap with a text-layer label printed over it.
    mixed = d.add_picture(prov=pv(2, *MIXED_PT))
    d.add_text(DocItemLabel.TEXT, "Key", prov=pv(2, 300, 467, 325, 479), parent=mixed)
    return d, pdfium.PdfDocument(str(pdf_path)), chart, vector, mixed


def test_end_to_end() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        d, pdf, chart, vector, mixed = build(Path(tmp))
        try:
            _check_end_to_end(d, pdf, chart, vector, mixed)
        finally:
            pdf.close()  # Windows: the temporary PDF cannot be deleted while open


def _check_end_to_end(d, pdf, chart, vector, mixed) -> None:
    import protocol_postprocess as pp
    from chunk_protocol import figure_parts

    vector_before = [c.resolve(d).text for c in vector.children]
    log: dict = {}
    counts = fo.reocr_figures(d, pdf, log)
    status = {e["page"] * 10 + i: e["decision"] for i, e in enumerate(log["figure_ocr"])}
    assert counts["reocr"] == 2 and counts["skipped:vector_text"] == 1, (counts, status)

    # The flow chart: one group per box, boxes in reading order, the misread gone.
    blocks = fo.ocr_blocks(d, chart)
    assert blocks is not None
    flat = [it.text for b in blocks for it in b]
    joined = " ".join(flat)
    keys = ["Screening", "Day -28", "Arm A (n=40)", "Drug X 10 mg", "Oral daily", "Arm B (n=40)", "Placebo",
            "Follow-up", "30 days"]
    pos = [joined.find(k) for k in keys]
    assert all(p >= 0 for p in pos) and pos == sorted(pos), (joined, pos)
    assert "Id•" not in joined and "Arm A(n=40)" not in joined, joined
    box_of = {k: i for i, b in enumerate(blocks) for k in keys if any(k in it.text for it in b)}
    assert box_of["Arm A (n=40)"] == box_of["Oral daily"] != box_of["Arm B (n=40)"], (box_of, flat)
    assert box_of["Screening"] == box_of["Day -28"] != box_of["Follow-up"], (box_of, flat)
    assert "Legacy note" in flat, flat  # old text where the new OCR read nothing is kept
    entry = next(e for e in log["figure_ocr"] if e["picture"] is chart)
    assert entry["old_items"] == 4 and entry["old_items_kept"] == 1, entry
    for b in blocks:  # every line has its own box, inside the figure
        for it in b:
            bb = it.prov[0].bbox
            assert it.prov[0].page_no == 1 and CHART_PT[0] - 1 <= bb.l < bb.r <= CHART_PT[2] + 1, bb
            assert CHART_PT[1] - 1 <= bb.b < bb.t <= CHART_PT[3] + 1, bb
    assert chart.caption_text(d) == "Figure 1. Study schema"

    # The vector figure is untouched.
    assert [c.resolve(d).text for c in vector.children] == vector_before
    assert next(e for e in log["figure_ocr"] if e["picture"] is vector)["decision"] == "skipped:vector_text"

    # Mixed: the text-layer label stays as it was; its OCR duplicate is dropped.
    kids = [c.resolve(d) for c in mixed.children]
    assert kids[0].text == "Key" and kids[0].label == DocItemLabel.TEXT, [k.text for k in kids]
    mixed_text = " ".join(x.text for x, _ in d.iterate_items(root=mixed, traverse_pictures=True)
                          if hasattr(x, "text"))
    assert mixed_text.count("Key") == 1 and "Injection site" in mixed_text, mixed_text
    assert fo.ocr_blocks(d, mixed) is None  # not all figure-OCR blocks: callers use their own order

    # Downstream consumers still work.
    bt = fo.block_text(d, chart)
    assert bt.startswith("Screening") and " | " in bt, bt
    assert pp.flatten_picture_text(d) == 3
    text = next(c.resolve(d).text for c in chart.children if c.cref != chart.captions[0].cref)
    assert text.startswith("Text in figure: ") and "Arm B (n=40)" in text and "Id•" not in text, text
    f = figure_parts(chart, d)
    assert f["caption"] == "Figure 1. Study schema" and len(f["texts"]) == 1, f


def test_unknown_engine() -> None:
    try:
        fo.reocr_figures(DoclingDocument(name="x"), None, engine="nope")
    except ValueError:
        return
    raise AssertionError("unknown engine accepted")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
