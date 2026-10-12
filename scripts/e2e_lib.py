"""Helpers of scripts/e2e_test.py (pure Python, no network, no new packages): made-up PDFs (plain or encrypted with
the PDF Standard Security Handler, RC4 revision 2 / 40-bit or revision 3 / 128-bit), the quotes an extraction
agent takes from tool results, USDM-key scoring, latency / memory statistics, the OpenRouter request counts
derived from job details, and the scrubbing of numbers-only output.

Everything here is made up or computed from tool results; nothing reads a document by itself. Tested by
tests/test_e2e_lib.py.
"""

from __future__ import annotations

import hashlib
import math
import re
import statistics
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any, Iterable

# ============================================================================ made-up PDFs

# Padding string of the Standard Security Handler (ISO 32000-1, 7.6.3.3, Algorithm 2 step a)
PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
PERMISSIONS = -4                 # every permission granted (bits 1-2 must be 0)


def rc4(key: bytes, data: bytes) -> bytes:
    """RC4 (ARCFOUR) of data with key: the stream cipher of the PDF Standard Security Handler revisions 2-4."""
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for n, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[n] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def _padded(password: str) -> bytes:
    return (password.encode("latin-1") + PAD)[:32]


def _md5(*parts: bytes) -> bytes:
    h = hashlib.md5()  # noqa: S324 - the PDF format's own key derivation, not a security choice of ours
    for p in parts:
        h.update(p)
    return h.digest()


def owner_entry(owner: str, user: str, revision: int, key_len: int) -> bytes:
    """/O (Algorithm 3)."""
    digest = _md5(_padded(owner or user))
    if revision >= 3:
        for _ in range(50):
            digest = _md5(digest)
    key = digest[:key_len]
    out = rc4(key, _padded(user))
    if revision >= 3:
        for i in range(1, 20):
            out = rc4(bytes(b ^ i for b in key), out)
    return out


def file_key(user: str, o_entry: bytes, permissions: int, file_id: bytes, revision: int, key_len: int) -> bytes:
    """The file encryption key (Algorithm 2)."""
    digest = _md5(_padded(user), o_entry, struct.pack("<i", permissions), file_id)
    if revision >= 3:
        for _ in range(50):
            digest = _md5(digest[:key_len])
    return digest[:key_len]


def user_entry(key: bytes, file_id: bytes, revision: int) -> bytes:
    """/U (Algorithm 4 for revision 2, Algorithm 5 for revision 3)."""
    if revision == 2:
        return rc4(key, PAD)
    out = rc4(key, _md5(PAD, file_id))
    for i in range(1, 20):
        out = rc4(bytes(b ^ i for b in key), out)
    return out + bytes(16)


def object_key(key: bytes, num: int, gen: int = 0) -> bytes:
    """The RC4 key of one indirect object's strings and streams (Algorithm 1)."""
    return _md5(key, struct.pack("<i", num)[:3], struct.pack("<i", gen)[:2])[:min(len(key) + 5, 16)]


@dataclass
class Image:
    """An RGB raster: width x height pixels, 3 bytes each, row by row from the top."""

    width: int
    height: int
    rgb: bytes


@dataclass
class Page:
    """One page: text lines (x, y, size, text), drawn in Helvetica, and an optional image at rect (l, b, r, t)."""

    lines: list[tuple[float, float, float, str]] = field(default_factory=list)
    image: Image | None = None
    rect: tuple[float, float, float, float] = (72, 300, 540, 612)


def chart_image(width: int = 480, height: int = 320) -> Image:
    """A made-up bar chart: white background, black axes, four coloured bars of different heights."""
    white, black = (255, 255, 255), (20, 20, 20)
    colours = [(31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40)]
    heights = [0.45, 0.8, 0.6, 0.3]
    rows = []
    x0, y_axis = 40, height - 30
    bar_w = (width - x0 - 40) // (2 * len(heights))
    for y in range(height):
        row = bytearray()
        for x in range(width):
            px = white
            if (x0 - 2 <= x <= x0 and 10 <= y <= y_axis) or (y_axis <= y <= y_axis + 2 and x0 <= x <= width - 10):
                px = black
            else:
                for k, (h, c) in enumerate(zip(heights, colours)):
                    left = x0 + bar_w // 2 + 2 * k * bar_w
                    if left <= x < left + bar_w and y_axis - int(h * (y_axis - 20)) <= y < y_axis:
                        px = c
                        break
            row += bytes(px)
        rows.append(bytes(row))
    return Image(width, height, b"".join(rows))


def _pdf_string(text: str) -> bytes:
    raw = text.encode("latin-1")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def make_pdf(pages: list[Page], *, user_password: str | None = None, owner_password: str | None = None,
             revision: int = 3, file_id: bytes | None = None) -> bytes:
    """A small PDF of `pages` (Helvetica text, optional Flate-compressed RGB image per page). With user_password
    it is encrypted with the Standard Security Handler: revision 2 (RC4, 40-bit) or 3 (RC4, 128-bit); the
    owner password defaults to the user password. file_id (16 bytes) defaults to a hash of the content, so
    the same arguments give the same bytes."""
    if revision not in (2, 3):
        raise ValueError("revision must be 2 or 3")
    objs: list[bytes | None] = []

    def reserve() -> int:
        objs.append(None)
        return len(objs)

    def put(num: int, body: bytes) -> None:
        objs[num - 1] = body

    catalog, pages_id, font = reserve(), reserve(), reserve()
    put(font, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    streams: list[tuple[int, bytes, bytes]] = []  # (object number, dictionary entries, data)
    page_ids = []
    for p in pages:
        content = b""
        resources = b"/Font << /F1 %d 0 R >>" % font
        if p.image is not None:
            img = reserve()
            data = zlib.compress(p.image.rgb, 9)
            streams.append((img, b"/Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                                 b"/BitsPerComponent 8 /Filter /FlateDecode" % (p.image.width, p.image.height), data))
            resources += b" /XObject << /Im1 %d 0 R >>" % img
            l, b, r, t = p.rect
            content += b"q %.2f 0 0 %.2f %.2f %.2f cm /Im1 Do Q\n" % (r - l, t - b, l, b)
        for x, y, size, text in p.lines:
            content += b"BT /F1 %.1f Tf %.2f %.2f Td %s Tj ET\n" % (size, x, y, _pdf_string(text))
        cs = reserve()
        streams.append((cs, b"", content))
        pid = reserve()
        put(pid, b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Resources << %s >> /Contents %d 0 R >>"
            % (pages_id, resources, cs))
        page_ids.append(pid)
    put(pages_id, b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % i for i in page_ids),
                                                                 len(page_ids)))
    put(catalog, b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    if file_id is None:
        file_id = _md5(b"e2e", *[d for _, _, d in streams], (user_password or "").encode("latin-1"),
                       bytes([revision]))
    key = None
    encrypt_id = None
    if user_password is not None:
        key_len = 5 if revision == 2 else 16
        o_entry = owner_entry(owner_password or user_password, user_password, revision, key_len)
        key = file_key(user_password, o_entry, PERMISSIONS, file_id, revision, key_len)
        u_entry = user_entry(key, file_id, revision)
        encrypt_id = reserve()
        v = 1 if revision == 2 else 2
        put(encrypt_id, b"<< /Filter /Standard /V %d /R %d /Length %d /O <%s> /U <%s> /P %d >>"
            % (v, revision, key_len * 8, o_entry.hex().encode(), u_entry.hex().encode(), PERMISSIONS))
    for num, entries, data in streams:
        if key is not None:
            data = rc4(object_key(key, num), data)
        put(num, b"<< %s /Length %d >>\nstream\n" % (entries, len(data)) + data + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + (body or b"null") + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = b"/Size %d /Root %d 0 R /ID [<%s> <%s>]" % (len(objs) + 1, catalog, file_id.hex().encode(),
                                                           file_id.hex().encode())
    if encrypt_id is not None:
        trailer += b" /Encrypt %d 0 R" % encrypt_id
    out += b"trailer\n<< " + trailer + b" >>\nstartxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


SYNTHETIC_TAG = "E2E-SYNTH"


def synthetic_protocol(code: str, *, with_figure: bool = True) -> list[Page]:
    """Pages of a made-up protocol (no real study): a title, two short sections, a three-item criteria list and,
    with_figure, a bar chart with a caption on page 2. `code` makes the text (and the bytes) distinct."""
    p1 = Page(lines=[
        (72, 720, 16, f"Synthetic Protocol {SYNTHETIC_TAG} {code}"),
        (72, 696, 11, "This synthetic document exists only to test a document processing service."),
        (72, 680, 11, "It describes no real study, product, sponsor or participant."),
        (72, 650, 13, "1 Introduction"),
        (72, 630, 11, "The made-up study compares two invented schedules of reading exercises."),
        (72, 614, 11, "Every number and name in this text was chosen for the test and means nothing."),
        (72, 584, 13, "2 Inclusion Criteria"),
        (72, 564, 11, "1. Volunteers who enjoy reading synthetic documents on a quiet afternoon."),
        (72, 548, 11, "2. Volunteers who can count the four coloured bars of the invented chart."),
        (72, 532, 11, "3. Volunteers who agree to forget this document after the test has finished."),
    ])
    pages = [p1]
    if with_figure:
        pages.append(Page(lines=[(72, 720, 13, "3 Results of the Invented Schedules"),
                                 (72, 280, 11, f"Figure 1: Invented counts per schedule ({code})")],
                          image=chart_image(), rect=(72, 300, 540, 612)))
    return pages


def truncated(data: bytes, fraction: float = 0.4) -> bytes:
    """The first `fraction` of a PDF's bytes: a corrupt PDF (a %PDF- header, no cross-reference table)."""
    return data[:max(1100, int(len(data) * fraction))]


NOT_A_PDF = ("This upload is plain text pretending to be a protocol. It has no PDF header at all.\n" * 40).encode()


# ============================================================================ quotes an agent takes from tool results

_MD_NOISE = re.compile(r"<!--.*?-->|\{#S[^}\s]*\}")
_UNESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!|<>~])")
_BULLET = re.compile(r"^\s*(?:[-*+·•▪◦‣∙●○■□➢►]|o(?=\s))\s+")
_ENUM = re.compile(r"^\s*(?:\(?(?:\d{1,3}(?:\.\d{1,3})*[a-z]?|[ivxlc]{1,6}|[a-zA-Z])[.)]|\[\s*\d{1,3}[a-z]?\s*\]"
                   r"|[A-Z]{1,3}\s?\d{1,3}[a-z]?\.)\s+")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def clean_line(line: str) -> str:
    """A Markdown line as plain text: comments and section anchors removed, heading hashes, bold / italic
    markers and backslash escapes dropped, whitespace collapsed."""
    line = _MD_NOISE.sub(" ", line)
    line = re.sub(r"^\s*#{1,6}\s+", "", line)
    line = line.replace("**", "").replace("__", "")
    line = _UNESCAPE.sub(r"\1", line)
    return " ".join(line.split())


def strip_marker(line: str) -> str:
    """The text of a list line without its marker ('1.', 'a)', '[2b]', 'I 05.', '·', '-'), up to two levels."""
    for _ in range(2):
        new = _BULLET.sub("", line, count=1)
        new = _ENUM.sub("", new, count=1)
        if new == line:
            break
        line = new
    return line.strip()


def words(text: str) -> int:
    return len(_WORD.findall(text or ""))


def block_text(result: dict, block: dict) -> str:
    a, z = (block.get("chars") or [0, 0])[:2]
    return (result.get("markdown") or "")[a:z]


def table_cells(md: str) -> list[str]:
    """Cell texts of a Markdown table (every row, separator rows skipped), each cleaned; empty cells dropped."""
    out = []
    for raw in md.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip("|"))]
        if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        for c in cells:
            c = clean_line(c.replace("<br>", " ").replace("<br/>", " "))
            if c:
                out.append(c)
    return out


@dataclass
class Quote:
    text: str
    category: str          # inclusion, exclusion, objectives_endpoints, visit, identifier
    section_id: str | None
    verbatim: bool = True  # taken verbatim from a tool result (False: a label the tool composed)


def section_quotes(result: dict, category: str, *, min_words: int = 4, tables: bool = False) -> list[Quote]:
    """The values an extraction agent takes from one read_section result: every line of the text and list blocks
    (marker stripped) with at least min_words words and, with tables, every table cell with at least min_words
    words; each with the block's section_id (the agent passes it to quote_check). Headings, labels, figures and
    page markers are skipped. Duplicates (same text and section) are kept once."""
    out, seen = [], set()
    for block in result.get("blocks") or []:
        kind = block.get("kind")
        text = block_text(result, block)
        if kind in ("text", "list"):
            pieces = [strip_marker(clean_line(line)) for line in text.splitlines()]
        elif kind == "table" and tables:
            pieces = table_cells(text)
        else:
            continue
        for piece in pieces:
            piece = piece.strip()
            key = (piece.casefold(), block.get("section_id"))
            if words(piece) >= min_words and key not in seen:
                seen.add(key)
                out.append(Quote(piece, category, block.get("section_id")))
    return out


def visit_quotes(soa_result: dict) -> list[Quote]:
    """Every SoA visit label get_soa reports (once per text). Never counted as verbatim: soa.py composes labels
    and header paths from several header cells and the label column ("Hour 0" from "Time point (hours)" + "0")."""
    out, seen = [], set()
    for s in soa_result.get("soas") or []:
        for v in s.get("visits") or []:
            label = (v.get("label") or "").strip()
            if not label or label.casefold() in seen:
                continue
            seen.add(label.casefold())
            out.append(Quote(label, "visit", None, verbatim=False))
    return out


IDENTIFIER_KEYS = ("nct", "sponsor_protocol_id")


def identifier_quotes(info: dict) -> list[Quote]:
    """The NCT and sponsor protocol id values of document_info and the verbatim lines they were found in."""
    out = []
    for key in IDENTIFIER_KEYS:
        entry = ((info or {}).get("identifiers") or {}).get(key) or {}
        if entry.get("value"):
            out.append(Quote(str(entry["value"]), "identifier", None))
        src = (entry.get("source") or {}).get("text")
        if src and words(src) >= 2:
            out.append(Quote(str(src), "identifier", None))
    return out


# ============================================================================ scoring against the public USDM keys

CRITERION_MATCH = 0.8      # share of a key criterion's content tokens found in one window of the section text

# the smoke test's criterion match: the largest share of a needle's tokens found in one window of the haystack
from smoke_test import window_cover  # noqa: E402,F401


def criteria_found(study, category: str, section_text: str) -> list[tuple[str, float]]:
    """(criterion identifier, best window cover) of every key EligibilityCriterion of `category` (inclusion /
    exclusion) in the section text."""
    import usdm_gold

    hay = usdm_gold.content_tokens(section_text)
    out = []
    for c in study.of("EligibilityCriterion"):
        if category not in (c.attrs.get("category") or "").lower() or not c.text:
            continue
        ident = (c.attrs.get("identifier") or c.name or c.id).strip()
        out.append((ident, round(window_cover(usdm_gold.content_tokens(c.text), hay), 3)))
    return out


def texts_found(texts: Iterable[str], section_text: str, threshold: float = CRITERION_MATCH) -> tuple[int, int]:
    """(found, total) of key texts (objectives, endpoints) in the section text."""
    import usdm_gold

    hay = usdm_gold.content_tokens(section_text)
    texts = [t for t in texts if t and usdm_gold.content_tokens(t)]
    return sum(window_cover(usdm_gold.content_tokens(t), hay) >= threshold for t in texts), len(texts)


def norm_id(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).upper()


def key_identifiers(study) -> dict[str, str]:
    """The key's identifiers that document_info reports too: nct, sponsor_protocol_id, eudract, ind."""
    out: dict[str, str] = {}
    for i in study.of("StudyIdentifier"):
        text = (i.text or "").strip()
        scope = f"{i.attrs.get('scope_name')} {i.attrs.get('scope')} {i.attrs.get('scope_type')}".casefold()
        if not text:
            continue
        if re.fullmatch(r"NCT\d{8}", text):
            out.setdefault("nct", text)
        elif re.fullmatch(r"\d{4}-\d{6}-\d{2}", text):
            out.setdefault("eudract", text)
        elif "sponsor" in scope or "pharma" in scope or "drug company" in scope:
            out.setdefault("sponsor_protocol_id", text)
        elif "fda" in scope.split() or "food" in scope:
            out.setdefault("ind", text)
    return out


def compare_identifiers(expected: dict[str, str], info: dict) -> dict[str, tuple[str | None, str | None, bool]]:
    """key -> (expected, got, equal) for every expected identifier (whitespace and case ignored)."""
    ids = (info or {}).get("identifiers") or {}
    out = {}
    for key, value in expected.items():
        got = (ids.get(key) or {}).get("value")
        out[key] = (value, got, norm_id(got) == norm_id(value))
    return out


# ============================================================================ measurements

def summarize(values: Iterable[float]) -> dict[str, float | int]:
    """n, median, max (seconds, 3 decimals) of a list of latencies."""
    vals = sorted(float(v) for v in values)
    if not vals:
        return {"n": 0, "median": 0.0, "max": 0.0}
    return {"n": len(vals), "median": round(statistics.median(vals), 3), "max": round(vals[-1], 3)}


_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000 ** 2, "mib": 1024 ** 2, "gb": 1000 ** 3, "gib": 1024 ** 3,
          "tb": 1000 ** 4, "tib": 1024 ** 4}


def parse_size(text: str) -> int | None:
    """'1.234GiB' / '512MiB' / '10kB' -> bytes; None when unparsable."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgGtT]?i?[bB])\s*", text or "")
    if not m:
        return None
    unit = m.group(2).lower()
    return int(float(m.group(1)) * _UNITS.get(unit, 1)) if unit in _UNITS else None


def parse_mem_usage(text: str) -> int | None:
    """`docker stats` MemUsage '1.23GiB / 8GiB' -> used bytes."""
    return parse_size((text or "").split("/")[0])


def gib(n: int | None) -> str:
    return "-" if n is None else f"{n / 1024 ** 3:.2f} GiB"


EMBED_BATCH = 32           # embed_chunks.embed_texts: chunks per embedding request


def derived_requests(job: dict | None) -> dict[str, int]:
    """OpenRouter requests a finished job made, derived from its stage details (retries not included): figures
    = figures described_now + failed (a diagram figure adds one Mermaid request, not counted), embedding
    batches = ceil(embedded / 32)."""
    stages = {s.get("name"): s for s in (job or {}).get("stages") or []}
    fig = (stages.get("figures") or {}).get("detail") or {}
    emb = (stages.get("embed") or {}).get("detail") or {}
    figures = int(fig.get("described_now") or 0) + int(fig.get("failed") or 0)
    embedded = int(emb.get("embedded") or 0)
    return {"figures": figures, "embed_batches": math.ceil(embedded / EMBED_BATCH), "embedded_chunks": embedded}


def stage_seconds(job: dict | None) -> dict[str, float]:
    return {str(s.get("name")): float(s.get("seconds") or 0) for s in (job or {}).get("stages") or []}


# ============================================================================ numbers-only output

_HEX = re.compile(r"[0-9a-fA-F]{16,}")


def scrub(text: str) -> str:
    """Hex ids (doc ids, job ids, hash prefixes) replaced by <id>; used on every numbers-only line that is not
    built from numbers alone (error types, HTTP statuses)."""
    return _HEX.sub("<id>", str(text))
