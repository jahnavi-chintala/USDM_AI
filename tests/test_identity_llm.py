"""Self-test for identity_llm.py (the identity model step: the model selects, the code verifies):
`python tests/test_identity_llm.py`.

Public protocols only (output_public/<name>/; written to only as temporary copies of protocol.json and chunks/ files;
one whose output is absent is skipped with a note), plus small made-up documents. No network, no pytest: the run is
offline (OPENROUTER_OFFLINE=1, a made-up API key, so no .env is read); the tests with a fake request function unset
OPENROUTER_OFFLINE inside a block and restore it.

Checks:
  select_lines: ids L1.. in order; the body text of pages 1-3 in reading order, a table as one line per non-empty
    grid row (the table's ref, the row index) in its place, no text inside pictures, nothing of page 4; then the
    distinct headers/footers of the whole document (distinct with their digits ignored: "Page 3 of 120" and "Page 4
    of 120" are one, showing the most frequent text, the earliest on a tie, with its first page and ref; page order,
    at most 40); every line's text is its item's
    or table row's text (whitespace-collapsed, at most 600 characters); the 400-line and 40,000-character limits keep
    the headers/footers; deterministic; the same on the public protocols.
  build_messages: the instructions name every field; the user message holds the rule values and every line (no item
    refs, nothing of a later page).
  verify: accepted values (one line; a title wrapped over two lines; a title hyphen-wrapped across two lines, the
    hyphen joined or kept; an acronym hyphen-wrapped; a table row; a registry id printed with en dashes, stored with
    "-"), with method, source (page, layer, text, refs, row, section) and the replaced rule value; confirmed values
    (normalised equality, the sponsor's verbatim form); every kept_rules reason, the first failing check winning;
    registry ids fill-only both ways; tokens: inside a longer word, a hyphenated code, "ABC_DEF", "ABC/123/456" or
    "ZORBEX.Study" refused, after "No." / "Nr." / "Ref." accepted ("IND No.140,453"), no token glued from neighbouring
    words ("DrugXin", "ZX4417NCT0123"), none cut at a line-wrap hyphen ("ZX" / "4417" of "Protocol ZX-" / "4417");
    cited lines in two layers or two headers/footers together refused; amendment designators as printed ("3.1", "a"
    in "Amendment (a)", "05" after "Amended ...", roman "IV", "B" after "No.") and version labels without a digit
    ("Amendment (a)", "Revision B") accepted; an amendment without "Amendment"/"Amended" before it, only part of
    "3.1", a word ("Number", "to"), a stray letter further on ("a" in "Amendment 4 to a protocol", "X", "I"), "1" after
    "Amendments", and the version labels "Protocol Amendments" / "Versions" refused; null, empty and unknown fields
    not listed; the record
    is not modified; the normalisation (NFKC, quotes, dashes, soft hyphen, case).
  the final review's checks: version labels need a version word (and a digit or a designator with the amendment
    rule's separators: "Amendment No. B"; "ZX-4417" refused), dates a year and a month or a numeric date (a EudraCT
    number refused), a protocol id that is a registry id, a title or sponsor starting with a cover-page label and a
    sponsor with an address are refused; a version label accepted alone gives the amendment its designator (not the
    rules' one again, not from a label without one); only "-", U+2010, U+2011 and the soft hyphen are wrap hyphens
    (an em dash is no boundary problem); a value starting a line that continues a word wrapped from the line before
    is refused; the 40 header/footer groups are the first 40 to occur; the module docstring lists the amendment
    rule's separators.
  ask_model: the exact body (keys, ZDR provider block, model without the "openrouter:" prefix, temperature 0,
    max_tokens 1500, the messages unmodified) sent to the chat URL with the key; the default request function is
    openrouter_http.post_json; retries (429 then 200: two requests, one answer; three 500s or three transport errors:
    IdentityModelError after three requests; a 400: one request) with the backoff sleeps; an unparseable answer
    raises IdentityModelError; no message holds response text; a ```json fence and leading prose are handled; an
    unsupported model and the offline refusal send nothing.
  refine: one request, then a cache hit with no request (offline); the cache entry's name and keys (doc_key =
    figure_vlm.document_key); a cached answer is verified again; offline without an entry: OfflineRefused, no
    request; the record given is not modified; a cache that cannot be written (os.replace failing, the folder a file)
    leaves no temporary file and the answer is used all the same; the temporary file is named "<doc_key>.*.tmp".
  run_identity on a copy of public Alexion: no model -> the rules record (it loads); a fake model -> the accepted
    fields carry MODEL_METHOD, a source that resolves and the rule value they replaced, the confirmed and refused ones
    keep the rules; offline without a cache entry -> skipped_offline and the rules record; an unsupported model, a
    status 429 (retried) or a failing request -> partial with the exception type only; an unwritable cache -> done;
    dest_dir; the same input twice gives the same bytes; the record carries the stamp of the stats.json it was built
    from; the identity_llm log and the command line's output hold no answer value and no line text; a stats.json
    without source_sha256: partial ("no source hash"), nothing written, nothing asked.
  the command line on a copy: the script with --model "" writes the rules record; the IDENTITY_MODEL default offline
    is skipped_offline; a fake model prints the field names (no values) and caches in the document folder; a missing
    folder exits 1 with the exception type only. Output: one line.
"""

import copy
import hashlib
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"  # the default of the whole run; tests unset it only around a fake request
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
FAKE_KEY = "sk-or-v1-identitytest-" + secrets.token_hex(8)
os.environ["OPENROUTER_API_KEY"] = FAKE_KEY  # the real key / .env is never read
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from docling_core.types.doc import ContentLayer, DocItemLabel, RefItem, TableItem  # noqa: E402

import identity_llm as il  # noqa: E402
from identity import FIELDS, MODEL_METHOD, RECORD_FILE, load_record, meta_stamp  # noqa: E402
from retrieve import OfflineRefused  # noqa: E402
from test_identity import SOURCE_KEYS, add_table, check_source, make_doc, prov  # noqa: E402

MODEL = "openrouter:anthropic/claude-opus-5.5"
ZDR = {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
SECRET = "Made-up confidential response text QX-77"  # must never reach an exception message or a result
REASONS = {"malformed answer", "no such line", "no lines cited", "too many lines", "lines not consecutive",
           "lines in several layers", "furniture lines cited together", "lines not on one page",
           "not in the cited lines", "not a title", "not an acronym", "not a token",
           "not a sponsor name", "not a protocol id", "not an amendment number", "no amendment label",
           "not a version label", "not a date", "registry id: the rule value is kept", "a registry id",
           "starts with a label", "starts inside a wrapped word",
           *(f"not a valid {k}" for k in il.REGISTRY_FIELDS)}
ALEXION = "Alexion_NCT04573309"
PUBLIC = ("Alexion_NCT04573309", "Bayer_NCT04304508", "Lilly_NCT03421379", "LZZT_CDISC_Pilot",
          "PrECOG_NCT01797120", "Sanofi_NCT03637764")


# --------------------------------------------------------------------------- helpers

def norm(s: str | None) -> str:
    return " ".join((s or "").split())


@contextmanager
def online():
    """The online code path: OPENROUTER_OFFLINE unset inside the block, restored after it."""
    old = os.environ.pop("OPENROUTER_OFFLINE", None)
    try:
        yield
    finally:
        if old is not None:
            os.environ["OPENROUTER_OFFLINE"] = old


@contextmanager
def temp_dir():
    tmp = Path(tempfile.mkdtemp(prefix="identity_llm_test_"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class FakeResponse:
    def __init__(self, status: int, data=None, text: str = "") -> None:
        self.status_code, self._data, self.text = status, data, text

    def json(self):
        if self._data is None:
            raise ValueError("no JSON body")
        return self._data


class FakePost:
    """The request function: records (url, body, key, timeout); answers with `content` (a dict is sent as JSON, a
    callable gets the body), or with `status` and SECRET as the response text, or raises `error`. `script`: per
    request in order, a status (int) or an exception to raise; after it, the answer above."""

    def __init__(self, content=None, status: int = 200, error: Exception | None = None, script=()) -> None:
        self.content, self.status, self.error, self.calls = content, status, error, []
        self.script = list(script)

    def __call__(self, url, body, key, timeout):
        self.calls.append((url, copy.deepcopy(body), key, timeout))
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        if self.error is not None:
            raise self.error
        status = step if isinstance(step, int) else self.status
        if status != 200:
            return FakeResponse(status, {"error": SECRET}, text=SECRET)
        content = self.content(body) if callable(self.content) else self.content
        if isinstance(content, dict):
            content = json.dumps(content)
        return FakeResponse(200, {"choices": [{"message": {"role": "assistant", "content": content}}],
                                  "usage": {"prompt_tokens": 120, "completion_tokens": 40}})


def sent_lines(body: dict) -> dict[str, str]:
    """id -> text of the lines in a request's user message."""
    out = {}
    for line in body["messages"][1]["content"].splitlines():
        m = re.match(r"^(L\d+) \| p\. \S+ \| \w+ \| (.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def expect(fn, cls):
    try:
        fn()
    except cls as exc:
        return exc
    raise AssertionError(f"expected {cls.__name__}")


class _Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@contextmanager
def capture_log():
    """The messages of the identity_llm logger (every level) inside the block."""
    logger, handler = logging.getLogger("identity_llm"), _Lines()
    old = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler.lines
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)


def assert_no_document_text(outputs: list[str], body: dict, answer: dict) -> None:
    """No line text sent (6 characters or more) and no answer value (4 or more) in the outputs."""
    texts = [t for t in sent_lines(body).values() if len(t) >= 6]
    values = [str(f["value"]) for f in answer["fields"].values() if f.get("value") and len(str(f["value"])) >= 4]
    assert texts and values
    for out in outputs:
        assert not [t for t in texts if t in out] and not [v for v in values if v in out], out


@contextmanager
def no_sleep():
    """time.sleep recorded instead of slept (the retry backoff of ask_model)."""
    import time

    real, slept = time.sleep, []
    time.sleep = slept.append
    try:
        yield slept
    finally:
        time.sleep = real


def public_copy(name: str, tmp: Path) -> Path | None:
    """protocol.json (+ protocol.decisions.json) and the chunks/ files ProtocolDocument.open reads, copied into tmp;
    None (with a note) when the public output is absent."""
    src = ROOT / "output_public" / name
    if not (src / "protocol.json").is_file() or not (src / "chunks" / "stats.json").is_file():
        print(f"  note: {name}: output_public output absent, skipped")
        return None
    dst = tmp / name
    (dst / "chunks").mkdir(parents=True)
    for f in ("protocol.json", "protocol.decisions.json"):
        if (src / f).is_file():
            shutil.copy(src / f, dst / f)
    for f in ("stats.json", "sections.json", "tables.jsonl", "glossary.json", "abbreviations.json"):
        if (src / "chunks" / f).is_file():
            shutil.copy(src / "chunks" / f, dst / "chunks" / f)
    return dst


def rule(value: str, method: str, **extra) -> dict:
    return {"value": value, "method": method, "source": None, **extra}


SEC = lambda ref: "S:" + ref  # noqa: E731


def verify_doc():
    """A made-up title page: (doc, line id by text, the table)."""
    doc, _ = make_doc([
        ("t", "CLINICAL STUDY PROTOCOL", 1),                                                          # L1
        ("t", "A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With Condition Y", 1),  # L2
        ("t", "Short Title: Drug X in Adults", 1),                                                    # L3
        ("t", "With Condition Y (ZORBEX-3)", 1),                                                      # L4
        ("t", "A Long Study of the Effects of Hyper-", 1),                                            # L5
        ("t", "tension Treatment", 1),                                                                # L6
        ("t", "Protocol Number: ZX-4417 NCT01234567", 1),                                             # L7
        ("t", "Sponsor Name: Acme Pharma, Inc.", 1),                                                  # L8
        ("t", "Protocol Amendment 4, 5 January 2021", 1),                                             # L9
        ("t", "Version 4.0", 1),                                                                      # L10
        ("t", "ZORBEXSTUDY cohort", 1),                                                               # L11
        ("t", "EudraCT number: 2019-003431-33", 2),                                                   # L12
        ("t", "Compare with NCT07654321 and code NCT076543219", 2),                                   # L13
        ("header", "Acme Pharma ZX-4417 Amendment 4", 1),                                             # L16
        ("footer", "Confidential", 2),                                                                # L17
    ])
    table = add_table(doc, [["Registry", "Number"], ["UTN", "U1111-1202-0839"]], page=2)              # L14, L15
    lines = il.select_lines(doc)
    return doc, lines, table


def verify_record() -> dict:
    fields = {f: None for f in FIELDS}
    fields.update(official_title=rule("A Phase 3, Randomized Study of Drug X", "title page"),
                  sponsor=rule("Acme Pharma, Inc", "cover-page label", verbatim="Acme Pharma, Inc."),
                  sponsor_protocol_id=rule("ZX-4417", "labelled pattern"),
                  nct=rule("NCT01234567", "registry pattern"),
                  version_label=rule("Amendment 4", "running header"),
                  amendment=rule("4", "running header"))
    return {"schema": 1, "fields": fields, "cover_page": {}, "others": {}}


def one(field: str, value, ids, record: dict | None = None) -> tuple[str, str | None]:
    """verify() of one answered field: ("accepted" | "confirmed" | "kept" | "none", the reason)."""
    _, lines, _ = verify_doc()
    got = il.verify({"fields": {field: {"value": value, "lines": ids}}}, lines, record or verify_record(), SEC)
    if got["kept_rules"]:
        assert len(got["kept_rules"]) == 1 and got["kept_rules"][0]["field"] == field, got["kept_rules"]
        return "kept", got["kept_rules"][0]["reason"]
    if got["accepted"]:
        return "accepted", None
    return ("confirmed", None) if got["confirmed"] else ("none", None)


# --------------------------------------------------------------------------- select_lines

def test_select_lines_synthetic() -> None:
    doc, refs = make_doc([
        ("header", "Protocol ZX-4417 Amendment 4", 1),
        ("footer", "Page 1 of 4", 1),
        ("t", "CLINICAL STUDY PROTOCOL", 1),
        ("t", "A Phase 3   Study of\nDrug X", 1),
        ("header", "Protocol ZX-4417 Amendment 4", 2),   # the same header again: not a new line
        ("footer", "Page 2 of 4", 2),
        ("header", "Protocol  ZX-4417\nAmendment 4", 3),  # the same after whitespace collapsing
        ("t", "Page four text", 4),                     # after page 3: not a line
    ], pages=4)
    table = add_table(doc, [["Version", "Date"], ["", ""], ["Amendment 4", "5 Jan 2021"]], page=2)
    pic = doc.add_picture(prov=prov(2))
    doc.add_text(DocItemLabel.TEXT, "Words inside a figure", prov=prov(2), parent=pic)
    last = doc.add_text(DocItemLabel.TEXT, "Page three text", prov=prov(3))
    lines = il.select_lines(doc)
    got = [(x["id"], x["page"], x["layer"], x["text"], x["ref"], x["row"]) for x in lines]
    assert got == [
        ("L1", 1, "body", "CLINICAL STUDY PROTOCOL", refs[2], None),
        ("L2", 1, "body", "A Phase 3 Study of Drug X", refs[3], None),
        ("L3", 2, "table", "Version Date", table.self_ref, 0),
        ("L4", 2, "table", "Amendment 4 5 Jan 2021", table.self_ref, 2),   # the empty row 1 is skipped
        ("L5", 3, "body", "Page three text", last.self_ref, None),
        ("L6", 1, "furniture", "Protocol ZX-4417 Amendment 4", refs[0], None),
        ("L7", 1, "furniture", "Page 1 of 4", refs[1], None),   # "Page 2 of 4" differs only in a digit: the same
    ], got
    assert all(set(x) == {"id", "page", "layer", "text", "ref", "row"} for x in lines)
    assert il.select_lines(doc) == lines  # deterministic
    assert [x["layer"] for x in il.select_lines(doc, pages=1)] == ["body", "body", "furniture", "furniture"]


def test_select_lines_headers_differing_in_digits() -> None:
    """Running headers/footers that differ only in their digits are one line (the first occurrence's text, page and
    ref): page numbers do not fill the 40-line cap, a later header still gets in."""
    items = [("t", "Title page", 1)]
    for p in range(1, 121):
        items += [("footer", f"Page {p} of 120", p), ("header", f"Protocol ZX-{4400 + p % 3} Amendment 4", p)]
    items += [("header", "Final Protocol Amendment 4, 5 January 2021", 90)]
    doc, refs = make_doc(items, pages=120)
    furniture = [(x["page"], x["text"], x["ref"]) for x in il.select_lines(doc) if x["layer"] == "furniture"]
    # equally frequent variants (40 pages each, "Page n of 120" once each): the earliest is shown
    assert furniture == [(1, "Page 1 of 120", refs[1]), (1, "Protocol ZX-4401 Amendment 4", refs[2]),
                         (90, "Final Protocol Amendment 4, 5 January 2021", refs[-1])], furniture
    # variants that carry identity digits: the most frequent text is shown, with its first page and ref, in page order
    items = [("t", "Title page", 1)]
    items += [("header", "Protocol ZX-4417 Amendment 3", p) for p in (1, 2)]
    items += [("header", "Protocol ZX-4417 Amendment 4", p) for p in range(3, 13)]
    items += [("footer", "Confidential", p) for p in range(1, 13)]
    doc, refs = make_doc(items, pages=12)
    furniture = [(x["page"], x["text"], x["ref"]) for x in il.select_lines(doc) if x["layer"] == "furniture"]
    assert furniture == [(1, "Confidential", refs[13]), (3, "Protocol ZX-4417 Amendment 4", refs[3])], furniture


def test_select_lines_limits() -> None:
    # 450 short body lines and 3 footers: 400 lines in all, the footers kept, the body a prefix in order
    doc, _ = make_doc([("t", f"Body line number {i}", 1) for i in range(450)]
                      + [("footer", f"Footer {w}", p) for p, w in ((1, "one"), (2, "two"), (3, "three"))], pages=3)
    lines = il.select_lines(doc)
    assert len(lines) == il.MAX_LINES == 400, len(lines)
    assert [x["text"] for x in lines[-3:]] == ["Footer one", "Footer two", "Footer three"]
    assert [x["text"] for x in lines[:-3]] == [f"Body line number {i}" for i in range(397)]
    assert [x["id"] for x in lines] == [f"L{i}" for i in range(1, 401)]
    # long lines: cut to 600 characters, 40,000 characters in all, the footers kept
    doc, _ = make_doc([("t", f"{i:03d} " + "word " * 140, 1) for i in range(100)] + [("footer", "Footer one", 1)])
    lines = il.select_lines(doc)
    assert all(len(x["text"]) <= il.MAX_LINE_CHARS for x in lines)
    assert sum(len(x["text"]) for x in lines) <= il.MAX_CHARS
    body = [x for x in lines if x["layer"] == "body"]
    assert len(body) == (il.MAX_CHARS - len("Footer one")) // il.MAX_LINE_CHARS == 66, len(body)
    assert [x["text"][:3] for x in body] == [f"{i:03d}" for i in range(66)] and lines[-1]["text"] == "Footer one"
    # at most 40 distinct headers/footers, in page order (texts that differ in letters, not only in digits)
    spell = lambda p: "".join("abcdefghij"[int(d)] for d in str(p))  # noqa: E731
    doc, refs = make_doc([("t", "Title page", 1)] + [("footer", f"Running footer {spell(p)}", p)
                                                     for p in range(50, 0, -1)], pages=50)
    furniture = [x for x in il.select_lines(doc) if x["layer"] == "furniture"]
    assert len(furniture) == il.MAX_FURNITURE == 40
    assert [x["page"] for x in furniture] == list(range(1, 41))
    assert [x["text"] for x in furniture] == [f"Running footer {spell(p)}" for p in range(1, 41)]


def test_select_lines_public() -> None:
    """On the public protocols: every line's text is its item's (or table row's), pages and layers match, the
    headers/footers are distinct, the limits hold, two calls give the same lines."""
    from docling_core.types.doc import DoclingDocument

    from chunk_protocol import _row_text

    n = 0
    for name in PUBLIC:
        path = ROOT / "output_public" / name / "protocol.json"
        if not path.is_file():
            print(f"  note: {name}: output_public output absent, skipped")
            continue
        doc = DoclingDocument.load_from_json(path)
        lines = il.select_lines(doc)
        assert [x["id"] for x in lines] == [f"L{i}" for i in range(1, len(lines) + 1)], name
        assert len(lines) <= il.MAX_LINES and sum(len(x["text"]) for x in lines) <= il.MAX_CHARS, name
        layers = [x["layer"] for x in lines]
        assert layers == sorted(layers, key=lambda k: k == "furniture"), name  # the headers/footers last
        for x in lines:
            item = RefItem(cref=x["ref"]).resolve(doc)
            first = min(p.page_no for p in item.prov)
            if isinstance(item, TableItem):
                assert x["layer"] == "table" and x["row"] is not None, (name, x["id"])
                assert x["text"] == norm(_row_text(item.data.grid[x["row"]]))[:600], (name, x["id"])
            else:
                assert x["row"] is None and x["text"] == norm(item.text)[:600], (name, x["id"])
                want = "furniture" if item.content_layer == ContentLayer.FURNITURE else "body"
                assert x["layer"] == want, (name, x["id"])
            assert x["page"] == first, (name, x["id"])
            if x["layer"] != "furniture":
                assert x["page"] <= 3, (name, x["id"])
        furniture = [x for x in lines if x["layer"] == "furniture"]
        assert len({re.sub(r"\d+", "#", x["text"]) for x in furniture}) == len(furniture) <= il.MAX_FURNITURE, name
        assert [x["page"] for x in furniture] == sorted(x["page"] for x in furniture), name
        assert il.select_lines(doc) == lines, name
        print(f"  {name}: {len(lines)} lines ({len(furniture)} headers/footers)")
        n += 1
    print(f"  {n} public documents")


def test_build_messages() -> None:
    _, lines, _ = verify_doc()
    messages = il.build_messages(lines, verify_record())
    assert [m["role"] for m in messages] == ["system", "user"] and messages[0]["content"] == il.INSTRUCTIONS
    assert all(f"- {f}:" in il.INSTRUCTIONS for f in il.MODEL_FIELDS) and "detected_title" not in il.INSTRUCTIONS
    user = messages[1]["content"]
    for x in lines:
        assert f"{x['id']} | p. {x['page']} | {x['layer']} | {x['text']}\n" in user + "\n", x["id"]
    assert "#/texts/" not in user and "#/tables/" not in user  # item refs are not sent
    cands = json.loads(user.split("\n\nLines:\n")[0].split("\n", 1)[1])
    assert list(cands) == list(il.MODEL_FIELDS) and cands["nct"] == "NCT01234567" and cands["acronym"] is None
    # nothing but the lines: the text of a later page is not sent
    doc, _ = make_doc([("t", "Title page text", 1), ("t", "Text of page four never sent", 4)], pages=4)
    user = il.build_messages(il.select_lines(doc), verify_record())[1]["content"]
    assert "Title page text" in user and "page four" not in user


# --------------------------------------------------------------------------- verify

def test_verify_accepted_and_confirmed() -> None:
    doc, lines, table = verify_doc()
    ids = {x["text"]: x["id"] for x in lines}
    by_id = {x["id"]: x for x in lines}
    assert [x["id"] for x in lines][-4:] == ["L14", "L15", "L16", "L17"] and by_id["L15"]["row"] == 1
    record = verify_record()
    before = copy.deepcopy(record)
    answer = {"fields": {
        "official_title": {"value": "A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With  Condition Y",
                           "lines": [ids["A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With "
                                         "Condition Y"]]},
        "brief_title": {"value": "Drug X in Adults With Condition Y (ZORBEX-3)", "lines": ["L4", "L3"]},
        "acronym": {"value": "ZORBEX-3", "lines": ["L4"]},
        "sponsor": {"value": "Acme Pharma, Inc.", "lines": ["L8"]},                 # the rules' verbatim form
        "sponsor_protocol_id": {"value": "zx-4417", "lines": ["L7"]},               # case differs: normalised
        "nct": {"value": "NCT01234567", "lines": ["L7"]},
        "eudract": {"value": "2019-003431-33", "lines": ["L12"]},                   # no rule value: filled
        "who_utn": {"value": "U1111-1202-0839", "lines": ["L15"]},                  # a table row
        "version_label": {"value": "Amendment 4", "lines": "L9"},                   # one id as a string
        "amendment": {"value": 4, "lines": ["L9"]},                                 # a number
        "version_date": {"value": "5 January 2021", "lines": ["L16"]},              # not there: the header
        "eu_ct": {"value": None, "lines": []},
        "ind": {"value": "  ", "lines": []},
        "phase": {"value": "3", "lines": ["L2"]},                                   # not a model field
        "detected_title": {"value": "CLINICAL STUDY PROTOCOL", "lines": ["L1"]},    # rules only
    }}
    got = il.verify(answer, lines, record, SEC)
    assert record == before  # not modified
    assert got["accepted"] == ["official_title", "brief_title", "acronym", "eudract", "who_utn"], got
    assert got["confirmed"] == ["sponsor", "sponsor_protocol_id", "nct", "version_label", "amendment"], got
    assert got["kept_rules"] == [{"field": "version_date", "reason": "not in the cited lines"}], got["kept_rules"]
    assert list(got["fields"]) == got["accepted"]
    off = got["fields"]["official_title"]
    l2 = by_id["L2"]
    assert off == {"value": "A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With Condition Y",
                   "method": MODEL_METHOD,
                   "source": {"page": 1, "layer": "body", "text": l2["text"], "ref": l2["ref"], "refs": [l2["ref"]],
                              "row": None, "section_id": SEC(l2["ref"])},
                   "rules": {"value": "A Phase 3, Randomized Study of Drug X", "method": "title page"}}, off
    brief = got["fields"]["brief_title"]  # two lines, cited out of order: id order
    assert brief["source"]["text"] == "Short Title: Drug X in Adults / With Condition Y (ZORBEX-3)"
    assert brief["source"]["refs"] == [by_id["L3"]["ref"], by_id["L4"]["ref"]]
    assert brief["source"]["ref"] == by_id["L3"]["ref"] and brief["rules"] == {"value": None, "method": None}
    utn = got["fields"]["who_utn"]["source"]
    assert (utn["layer"], utn["ref"], utn["row"], utn["page"]) == ("table", table.self_ref, 1, 2)
    assert utn["text"] == "UTN U1111-1202-0839" and utn["section_id"] == SEC(table.self_ref)
    for f in got["fields"].values():
        assert set(f) == {"value", "method", "source", "rules"} and set(f["source"]) == SOURCE_KEYS
        check_source(doc, f["source"], SEC, f["value"])
    # a word hyphen-wrapped across two lines: the hyphen joined
    got = il.verify({"fields": {"official_title": {"value": "A Long Study of the Effects of Hypertension Treatment",
                                                   "lines": ["L5", "L6"]}}}, lines, record, SEC)
    assert got["accepted"] == ["official_title"], got
    assert got["fields"]["official_title"]["source"]["text"] == "A Long Study of the Effects of Hyper- / tension " \
                                                                "Treatment"
    # ... or kept ("Placebo-" / "controlled"); an acronym hyphen-wrapped; a soft hyphen at the end of a line
    wrapped, _ = make_doc([("t", "A Randomized, Placebo-", 1), ("t", "controlled Study of Drug X", 1),
                           ("t", "in the ZORBEX-", 1), ("t", "3 trial of an Investi\u00ad", 1),
                           ("t", "gational Drug", 1)], pages=1)
    wl = il.select_lines(wrapped)
    rec = verify_record()
    got = il.verify({"fields": {
        "official_title": {"value": "A Randomized, Placebo-controlled Study of Drug X", "lines": ["L1", "L2"]},
        "acronym": {"value": "ZORBEX-3", "lines": ["L3", "L4"]},
        "brief_title": {"value": "trial of an Investigational Drug", "lines": ["L4", "L5"]}}}, wl, rec, SEC)
    assert got["accepted"] == ["official_title", "brief_title", "acronym"] and got["kept_rules"] == [], got
    got = il.verify({"fields": {"official_title": {"value": "A Randomized, Placebocontrolled Study of Drug X",
                                                   "lines": ["L1", "L2"]}}}, wl, rec, SEC)
    assert got["accepted"] == ["official_title"], got  # the joined form holds it too
    got = il.verify({"fields": {"official_title": {"value": "A Randomized, Placebo controlled Study of Drug X",
                                                   "lines": ["L1", "L2"]}}}, wl, rec, SEC)
    assert got["kept_rules"] == [{"field": "official_title", "reason": "not in the cited lines"}], got
    # a registry id printed with en dashes: checked and stored with "-"
    doc2, _ = make_doc([("t", "EudraCT 2019\u2013003431\u201333", 1)], pages=1)
    got = il.verify({"fields": {"eudract": {"value": "2019\u2013003431\u201333", "lines": ["L1"]}}},
                    il.select_lines(doc2), rec, SEC)
    assert got["accepted"] == ["eudract"] and got["fields"]["eudract"]["value"] == "2019-003431-33", got
    # the sponsor: a new name gets "verbatim"; a header/footer line has no section
    rec = verify_record()
    rec["fields"]["sponsor"] = None
    got = il.verify({"fields": {"sponsor": {"value": "Acme Pharma", "lines": ["L16"]}}}, lines, rec, SEC)
    sp = got["fields"]["sponsor"]
    assert sp["value"] == sp["verbatim"] == "Acme Pharma" and sp["source"]["layer"] == "furniture", sp
    assert sp["source"]["section_id"] is None and sp["source"]["page"] == 1
    assert list(sp) == ["value", "method", "source", "verbatim", "rules"]
    # answers that are not {"fields": {...}}: nothing
    for bad in ({}, {"fields": []}, [], None, {"fields": {"acronym": None}}):
        assert il.verify(bad, lines, record, SEC) == {"fields": {}, "accepted": [], "confirmed": [], "kept_rules": []}


def test_verify_kept_rules_reasons() -> None:
    title = "A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With Condition Y"
    cases = [  # (field, value, cited ids, reason)
        ("official_title", title, ["L99"], "no such line"),
        ("official_title", title, ["L2", "L99"], "no such line"),
        ("official_title", title, [], "no lines cited"),
        ("official_title", title, ["L1", "L2", "L3", "L4", "L5"], "too many lines"),
        ("brief_title", "Short Title: Drug X in Adults A Long Study", ["L3", "L5"], "lines not consecutive"),
        ("brief_title", "ZORBEXSTUDY cohort EudraCT number", ["L11", "L12"], "lines not on one page"),
        ("brief_title", "and code NCT076543219 Registry Number", ["L13", "L14"], "lines in several layers"),
        ("brief_title", "UTN U1111-1202-0839 Acme Pharma", ["L15", "L16"], "lines in several layers"),
        ("sponsor", "Acme Pharma ZX-4417 Amendment 4 Confidential", ["L16", "L17"], "furniture lines cited together"),
        ("acronym", "DrugXin", ["L3"], "not in the cited lines"),             # no token glued from three words
        ("sponsor_protocol_id", "ZX4417NCT01234567", ["L7"], "not in the cited lines"),
        ("official_title", "A Phase 3 Study of Something Else Entirely", ["L2"], "not in the cited lines"),
        ("official_title", "A Phase 3", ["L99"], "no such line"),  # the first failing check wins
        ("brief_title", "Drug X", ["L3"], "not a title"),
        ("official_title", "Phase 3", ["L2"], "not a title"),
        ("acronym", "Drug X", ["L3"], "not an acronym"),
        ("acronym", "3", ["L4"], "not an acronym"),
        ("acronym", "ZORBEX", ["L11"], "not a token"),        # inside the longer word ZORBEXSTUDY
        ("acronym", "ZORBEX", ["L4"], "not a token"),         # inside the hyphenated ZORBEX-3
        ("acronym", "ZX", ["L7"], "not a token"),             # inside the protocol number ZX-4417
        ("sponsor", "4.0", ["L10"], "not a sponsor name"),
        ("sponsor_protocol_id", "Protocol", ["L7"], "not a protocol id"),
        ("sponsor_protocol_id", "4417", ["L7"], "not a token"),
        ("nct", "NCT0123456", ["L7"], "not a valid nct"),
        ("nct", "NCT07654321", ["L13"], "registry id: the rule value is kept"),  # the rules have NCT01234567
        ("eudract", "2019-003431", ["L12"], "not a valid eudract"),
        ("who_utn", "UTN U1111-1202-0839", ["L15"], "not a valid who_utn"),
        ("amendment", "Amendment 4", ["L9"], "not an amendment number"),
        ("amendment", "4", ["L10"], "no amendment label"),     # "Version 4.0": no "Amendment" before it
        ("amendment", "4.0", ["L10"], "no amendment label"),
        ("amendment", "x", ["L3"], "no amendment label"),      # "Drug X": a token, but no "Amendment"
        ("version_label", "Version", ["L10"], "not a version label"),
        ("version_label", "Short Title: Drug X in Adults", ["L3"], "not a version label"),
        ("version_date", "January", ["L9"], "not a date"),
        ("official_title", 3.5, ["L2"], "malformed answer"),
        ("official_title", title, 2, "malformed answer"),
        ("official_title", title, [2], "malformed answer"),
    ]
    seen = set()
    for field, value, ids, reason in cases:
        got = one(field, value, ids)
        assert got == ("kept", reason), (field, value, ids, got)
        seen.add(reason)
    assert seen <= REASONS
    _, lines, _ = verify_doc()
    got = il.verify({"fields": {"acronym": "ZORBEX-3"}}, lines, verify_record(), SEC)  # not {"value", "lines"}
    assert got["kept_rules"] == [{"field": "acronym", "reason": "malformed answer"}]
    # a registry id: valid in shape but inside a longer number
    rec = verify_record()
    rec["fields"]["nct"] = None
    assert one("nct", "NCT07654321", ["L13"], rec) == ("accepted", None)  # fills the missing id
    assert one("nct", "NCT07654321", ["L13"]) == ("kept", "registry id: the rule value is kept")
    rec["fields"]["nct"] = None
    _, lines, _ = verify_doc()
    got = il.verify({"fields": {"nct": {"value": "NCT07654321", "lines": ["L13"]}}}, lines, rec, SEC)
    assert got["accepted"] == ["nct"] and got["fields"]["nct"]["rules"] == {"value": None, "method": None}
    # "NCT07654321" also stands inside "NCT076543219" on that line; alone it would not be a token
    doc, _ = make_doc([("t", "Code NCT076543219 only", 1)], pages=1)
    got = il.verify({"fields": {"nct": {"value": "NCT07654321", "lines": ["L1"]}}}, il.select_lines(doc), rec, SEC)
    assert got["kept_rules"] == [{"field": "nct", "reason": "not a token"}], got
    # an amendment number with its label is accepted; the confirmed rule value stays
    assert one("amendment", "4", ["L9"]) == ("confirmed", None)
    rec = verify_record()
    rec["fields"]["amendment"] = None
    assert one("amendment", "4", ["L9"], rec) == ("accepted", None)
    assert one("amendment", "4", ["L16"], rec) == ("accepted", None)  # "... Amendment 4" in a header
    # registry ids the rules lack: every kind is filled
    rec = verify_record()
    doc, _ = make_doc([("t", "EU CT number 2022-500014-26-00, IND 140,453 and WHO UTN U1111-1202-0839", 1)], pages=1)
    got = il.verify({"fields": {"eu_ct": {"value": "2022-500014-26-00", "lines": ["L1"]},
                                "ind": {"value": "140,453", "lines": ["L1"]},
                                "who_utn": {"value": "U1111-1202-0839", "lines": ["L1"]},
                                "eudract": {"value": "2022-500014-26", "lines": ["L1"]}}},
                    il.select_lines(doc), rec, SEC)
    assert got["accepted"] == ["eu_ct", "ind", "who_utn"], got
    assert got["kept_rules"] == [{"field": "eudract", "reason": "not a token"}], got  # part of the EU CT number


def test_verify_tokens_and_layers() -> None:
    """Whole tokens: "_" is part of a word, "/" and "." between letters or digits join like "-" (but "No." / "Nr." /
    "Ref." end a label); a line-wrap hyphen is no boundary; no value glued from neighbouring words; a header and a
    footer are never one value."""
    doc, _ = make_doc([("t", "Code ABC_DEF here", 1),                        # L1
                       ("t", "Ref ABC/123/456 and v1.2.3", 1),                # L2
                       ("t", "Protocol ZX-4417 NCT 0123 4567", 1),            # L3
                       ("t", "Study ABC (ref 123/) and ZX.9", 1),             # L4
                       ("t", "US IND No.140,453 and EudraCT No.2019-003431-33", 1),  # L5
                       ("t", "Protocol No.ABC-123, Nr.QX-5 and Ref.ZQ-77", 1),       # L6
                       ("t", "The ZORBEX.Study and ABC-123/Amendment 2", 1),         # L7
                       ("t", "Protocol ZX-", 1),                                     # L8
                       ("t", "4417 Amendment 4", 1),                                 # L9
                       ("header", "Acme Pharma", 1), ("footer", "Confidential", 1)], pages=1)  # L10, L11
    lines = il.select_lines(doc)
    rec = verify_record()
    rec["fields"]["sponsor_protocol_id"] = rec["fields"]["sponsor"] = None
    cases = [  # (field, value, cited ids, kept reason or None = accepted)
        # a label abbreviation's "." is a boundary
        ("ind", "140,453", ["L5"], None),
        ("eudract", "2019-003431-33", ["L5"], None),
        ("sponsor_protocol_id", "ABC-123", ["L6"], None),
        ("sponsor_protocol_id", "QX-5", ["L6"], None),
        ("sponsor_protocol_id", "ZQ-77", ["L6"], None),
        ("acronym", "ZORBEX", ["L7"], "not a token"),                 # "ZORBEX.Study": no label abbreviation
        ("sponsor_protocol_id", "ABC-123", ["L7"], "not a token"),    # "ABC-123/Amendment": stays refused
        # a line-wrap hyphen is no token boundary
        ("acronym", "ZX", ["L8", "L9"], "not a token"),
        ("sponsor_protocol_id", "4417", ["L8", "L9"], "not a token"),
        ("sponsor_protocol_id", "ZX-4417", ["L8", "L9"], None),
        ("acronym", "ZX", ["L8"], "not a token"),                     # cut at the hyphen ending the line
        ("acronym", "ZX-", ["L8"], "not a token"),
        ("acronym", "ABC", ["L1"], "not a token"),                    # inside ABC_DEF
        ("acronym", "DEF", ["L1"], "not a token"),
        ("sponsor_protocol_id", "123", ["L2"], "not a token"),        # inside ABC/123/456
        ("sponsor_protocol_id", "v1.2", ["L2"], "not a token"),       # inside v1.2.3
        ("sponsor_protocol_id", "ZX4417NCT0123", ["L3"], "not in the cited lines"),
        ("sponsor_protocol_id", "ZX-4417", ["L3"], None),
        ("acronym", "ABC", ["L4"], None),                             # "(", ")" and spaces are boundaries
        ("sponsor_protocol_id", "123", ["L4"], None),                 # "/" before a space: a boundary
        ("sponsor_protocol_id", "ZX.9", ["L4"], None),
        ("sponsor", "Acme Pharma Confidential", ["L10", "L11"], "furniture lines cited together"),
        ("sponsor", "Acme Pharma", ["L10"], None),
    ]
    for field, value, ids, reason in cases:
        got = il.verify({"fields": {field: {"value": value, "lines": ids}}}, lines, rec, SEC)
        if reason is None:
            assert got["accepted"] == [field], (field, value, got)
        else:
            assert got["kept_rules"] == [{"field": field, "reason": reason}], (field, value, got)


def test_verify_amendment_designators() -> None:
    """Amendment designators as printed on real title pages ("3.1", "a" in "Amendment (a)", "05" after "Amended
    ...") and version labels without a digit ("Amendment (a)")."""
    doc, _ = make_doc([("t", "Amendment Number: 3.1 (US)", 1),                       # L1
                       ("t", "Amendment (a) Electronically Signed and Approved", 1),  # L2
                       ("t", "AMENDED CLINICAL TRIAL PROTOCOL 05", 1),               # L3
                       ("t", "Cohort a enrolment, version b of the plan", 1),        # L4
                       ("t", "Amendment Number", 1),                                 # L5
                       ("t", "Amendment 4 to a protocol", 1),                        # L6
                       ("t", "Amendment IV", 1),                                     # L7
                       ("t", "Summary of Protocol Amendments 1-3", 1),               # L8
                       ("t", "Versions", 1),                                         # L9
                       ("t", "Document Revisions", 1),                               # L10
                       ("t", "Revision B", 1),                                       # L11
                       ("t", "Amended Protocol: A Phase 3 Study", 1),                # L12
                       ("t", "Amendment 3 to the Study of Drug X", 1),               # L13
                       ("t", "Amendment 2 as I signed", 1),                          # L14
                       ("t", "Amendment: 05", 1),                                    # L15
                       ("t", "Amendment No. B and Amendment #c", 1)], pages=1)       # L16
    lines = il.select_lines(doc)
    rec = verify_record()
    rec["fields"]["amendment"] = rec["fields"]["version_label"] = None
    cases = [  # (field, value, cited ids, kept reason or None = accepted)
        ("amendment", "3.1", ["L1"], None),
        ("amendment", "3", ["L1"], "no amendment label"),   # only part of "3.1"
        ("amendment", "a", ["L2"], None),                   # in parentheses
        ("amendment", "05", ["L3"], None),                  # after "Amended ... Protocol" (within 30 characters)
        ("amendment", "IV", ["L7"], None),                  # a roman numeral
        ("amendment", "05", ["L15"], None),
        ("amendment", "B", ["L16"], None),                  # after "No."
        ("amendment", "c", ["L16"], None),                  # after "#"
        ("amendment", "a", ["L4"], "no amendment label"),
        # a stray letter or roman numeral further on is not the designator; "Amendments" is not the label
        ("amendment", "a", ["L6"], "no amendment label"),
        ("amendment", "A", ["L12"], "no amendment label"),
        ("amendment", "X", ["L13"], "no amendment label"),
        ("amendment", "I", ["L14"], "no amendment label"),
        ("amendment", "1", ["L8"], "no amendment label"),
        ("amendment", "3.1.2.4.5", ["L1"], "not in the cited lines"),
        ("amendment", "(a)", ["L2"], "not an amendment number"),
        ("amendment", "Number", ["L1"], "not an amendment number"),   # a word after "Amendment"
        ("amendment", "to", ["L6"], "not an amendment number"),
        ("amendment", "xxi", ["L7"], "not in the cited lines"),
        ("version_label", "Amendment (a)", ["L2"], None),
        ("version_label", "Amended Clinical Trial Protocol 05", ["L3"], None),
        ("version_label", "version b", ["L4"], None),
        ("version_label", "Revision B", ["L11"], None),
        ("version_label", "Amendment IV", ["L7"], None),                    # a roman numeral designator
        ("version_label", "Amendment Number", ["L5"], "not a version label"),
        ("version_label", "Cohort a enrolment", ["L4"], "not a version label"),
        ("version_label", "Protocol Amendments", ["L8"], "not a version label"),  # not the word's own last letter
        ("version_label", "Versions", ["L9"], "not a version label"),
        ("version_label", "Document Revisions", ["L10"], "not a version label"),
    ]
    assert il._LETTER_OR_ROMAN.fullmatch("xix") and il._LETTER_OR_ROMAN.fullmatch("XX")
    assert not il._LETTER_OR_ROMAN.fullmatch("xxi") and not il._LETTER_OR_ROMAN.fullmatch("to")
    derived = {"Amendment (a)": "a", "Amended Clinical Trial Protocol 05": "05", "Amendment IV": "IV"}  # F3
    for field, value, ids, reason in cases:
        got = il.verify({"fields": {field: {"value": value, "lines": ids}}}, lines, rec, SEC)
        if reason is None:
            amendment = derived.get(value) if field == "version_label" else None
            assert got["accepted"] == [field] + (["amendment"] if amendment else []), (field, value, got)
            assert got["fields"][field]["value"] == value, (field, value, got)
            if amendment:  # a version label accepted alone gives the amendment its designator
                assert got["fields"]["amendment"]["value"] == amendment, (value, got)
        else:
            assert got["kept_rules"] == [{"field": field, "reason": reason}], (field, value, got)
    # a rule amendment "3" against the printed "3.1": the model's designator replaces it
    rec["fields"]["amendment"] = rule("3", "running header")
    got = il.verify({"fields": {"amendment": {"value": "3.1", "lines": ["L1"]}}}, lines, rec, SEC)
    assert got["accepted"] == ["amendment"] and got["fields"]["amendment"]["rules"] == {"value": "3",
                                                                                      "method": "running header"}
    long_label = "Version " + "1" * 60
    doc, _ = make_doc([("t", long_label, 1)], pages=1)
    got = il.verify({"fields": {"version_label": {"value": long_label, "lines": ["L1"]}}}, il.select_lines(doc), rec,
                    SEC)
    assert got["kept_rules"] == [{"field": "version_label", "reason": "not a version label"}]  # over 60 characters


def test_verify_normalisation() -> None:
    assert il._norm("A\u2013B \u201cx\u201d it\u2019s soft\u00adhyphen  \uff38  \u2212 1\n2") == \
        'a-b "x" it\'s softhyphen x - 1 2'
    doc, _ = make_doc([("t", "A Study of Drug\u00a0X \u2013 the \u201cNew\u201d Regimen in Older Adults", 1)],
                      pages=1)
    lines = il.select_lines(doc)
    rec = verify_record()
    got = il.verify({"fields": {"brief_title": {"value": 'A study of drug X - the "new" regimen in older adults',
                                                "lines": ["L1"]}}}, lines, rec, SEC)
    assert got["accepted"] == ["brief_title"], got
    # the stored value is the model's, whitespace-collapsed (the source keeps the printed text)
    assert got["fields"]["brief_title"]["value"] == 'A study of drug X - the "new" regimen in older adults'
    assert il._first_group(r"\b(NCT\d{8})(?!\d)") == r"NCT\d{8}"
    assert il._first_group(r"(?<![\d-])(\d{4}-\d{6}-\d{2})(?!\d|-\d)") == r"\d{4}-\d{6}-\d{2}"
    assert il._first_group(r"[(](?P<v>a(?:b|c))") == "a(?:b|c)"
    assert il._registry_rx("ind").fullmatch("119006") and not il._registry_rx("ind").fullmatch("IND 119006")


def check_cases(doc, rec: dict, cases) -> None:
    """verify() of each (field, value, cited ids, kept reason or None = accepted; a version label perhaps with the
    amendment it gives) on doc's lines."""
    lines = il.select_lines(doc)
    for field, value, ids, reason in cases:
        got = il.verify({"fields": {field: {"value": value, "lines": ids}}}, lines, rec, SEC)
        if reason is None:
            derived = ["amendment"] if field == "version_label" and il._designator_of(value) else []
            assert got["accepted"] == [field] + derived and got["kept_rules"] == [], (field, value, got)
        else:
            assert got["kept_rules"] == [{"field": field, "reason": reason}], (field, value, got)
            assert reason in REASONS, reason


def empty_record() -> dict:
    return {"schema": 1, "fields": {f: None for f in FIELDS}, "cover_page": {}, "others": {}}


def test_verify_tightened_shapes() -> None:
    """F2: a version label names a version word (and a digit or a designator, with the amendment rule's
    separators); a date has a year and a month or a numeric date and is no registry id; a protocol id that is a
    registry id, a title or sponsor that starts with a cover-page label and a sponsor with an address are refused."""
    doc, _ = make_doc([
        ("t", "Amendment No. B of Edition 3", 1),                                                     # L1
        ("t", "Protocol ZX-4417 dated 18 Mar 2022", 1),                                               # L2
        ("t", "24 OCT 2019, 05-Dec-2017 and January 22, 2014", 1),                                    # L3
        ("t", "23 November 2020 or 2014-01-22 or 22.01.2014 or 01/22/2014", 1),                       # L4
        ("t", "EudraCT 2019-003431-33 NCT01234567 IND 119006 U1111-1202-0839 EU CT 2022-500014-26-00", 1),  # L5
        ("t", "Sponsor Name: Bayer AG, 51368 Leverkusen, Germany", 1),                               # L6
        ("t", "Protocol Title: A Phase 3 Study of Drug X in Adults", 1),                             # L7
        ("t", "Acme Pharma 12345 Main Street", 1),                                                    # L8
        ("t", "Protocol Number: 19766", 1),                                                           # L9
        ("t", "US IND No. 140,453", 1),                                                               # L10
    ], pages=1)
    check_cases(doc, empty_record(), [
        ("version_label", "Amendment No. B", ["L1"], None),
        ("version_label", "Edition 3", ["L1"], None),
        ("version_label", "ZX-4417", ["L2"], "not a version label"),       # a bare id
        ("version_label", "No. B of Edition", ["L1"], "not a version label"),
        ("version_date", "18 Mar 2022", ["L2"], None),
        ("version_date", "24 OCT 2019", ["L3"], None),
        ("version_date", "05-Dec-2017", ["L3"], None),
        ("version_date", "January 22, 2014", ["L3"], None),
        ("version_date", "23 November 2020", ["L4"], None),
        ("version_date", "2014-01-22", ["L4"], None),
        ("version_date", "22.01.2014", ["L4"], None),
        ("version_date", "01/22/2014", ["L4"], None),
        ("version_date", "2019-003431-33", ["L5"], "not a date"),          # a EudraCT number
        ("version_date", "2014", ["L4"], "not a date"),                    # a year alone
        ("sponsor_protocol_id", "2019-003431-33", ["L5"], "a registry id"),
        ("sponsor_protocol_id", "NCT01234567", ["L5"], "a registry id"),
        ("sponsor_protocol_id", "119006", ["L5"], "a registry id"),        # an IND number alone
        ("sponsor_protocol_id", "U1111-1202-0839", ["L5"], "a registry id"),
        ("sponsor_protocol_id", "2022-500014-26-00", ["L5"], "a registry id"),
        ("sponsor_protocol_id", "ZX-4417", ["L2"], None),
        ("sponsor_protocol_id", "19766", ["L9"], None),               # a bare number is an IND only after "IND"
        ("sponsor_protocol_id", "140,453", ["L10"], "a registry id"),  # printed as an IND number
        ("sponsor", "Sponsor Name: Bayer AG", ["L6"], "starts with a label"),
        ("sponsor", "Bayer AG, 51368 Leverkusen", ["L6"], "not a sponsor name"),
        ("sponsor", "Acme Pharma 12345", ["L8"], "not a sponsor name"),
        ("sponsor", "Bayer AG", ["L6"], None),
        ("official_title", "Protocol Title: A Phase 3 Study of Drug X in Adults", ["L7"], "starts with a label"),
        ("brief_title", "A Phase 3 Study of Drug X", ["L7"], None),
    ])
    # F7: the module docstring names every separator the amendment rule accepts
    for sep in ('"No."', '"Nr."', '"Number"', '":"', '"#"', '"("', "spaces"):
        assert sep in il.__doc__, sep


def test_verify_amendment_number_with_leading_zero() -> None:
    """F3: "Amendment 5" with the rules' amendment "05" is the same amendment: nothing is derived."""
    doc, _ = make_doc([("t", "Amendment 5", 1)], pages=1)
    rec = empty_record()
    rec["fields"].update(amendment=rule("05", "labelled pattern"), version_label=rule("Version 1", "labelled pattern"))
    got = il.verify({"fields": {"version_label": {"value": "Amendment 5", "lines": ["L1"]}}}, il.select_lines(doc),
                    rec, SEC)
    assert got["accepted"] == ["version_label"] and "amendment" not in got["fields"], got


def test_verify_amendment_from_the_version_label() -> None:
    """F3: a version label accepted without its amendment gives the amendment its designator (MODEL_METHOD, the
    label's source, the replaced rule field), unless the rules have that amendment already or the label has none."""
    from identity import MODEL_METHOD

    doc, _ = make_doc([("t", "Protocol Amendment 3.1 (US)", 1), ("t", "Version 2.0", 1), ("t", "Amendment 4", 1)],
                      pages=1)
    lines = il.select_lines(doc)
    rec = empty_record()
    rec["fields"].update(amendment=rule("3", "running header"), version_label=rule("Amendment 3", "running header"))

    def run(answer: dict, record: dict = rec) -> dict:
        return il.verify({"fields": answer}, lines, record, SEC)

    got = run({"version_label": {"value": "Amendment 3.1 (US)", "lines": ["L1"]}})
    assert got["accepted"] == ["version_label", "amendment"] and got["kept_rules"] == [], got
    label, amendment = got["fields"]["version_label"], got["fields"]["amendment"]
    assert list(got["fields"]) == ["version_label", "amendment"]
    assert amendment == {"value": "3.1", "method": MODEL_METHOD, "source": label["source"],
                         "rules": {"value": "3", "method": "running header"}}, amendment
    assert amendment["source"] is not label["source"]  # a copy
    # the model's own amendment refused ("3" is only part of "3.1"): the label's designator all the same, one listing
    got = run({"version_label": {"value": "Amendment 3.1 (US)", "lines": ["L1"]},
               "amendment": {"value": "3", "lines": ["L1"]}})
    assert got["accepted"] == ["version_label", "amendment"] and got["kept_rules"] == [], got
    assert got["fields"]["amendment"]["value"] == "3.1"
    # the model's amendment accepted: it stays the model's
    got = run({"version_label": {"value": "Amendment 3.1 (US)", "lines": ["L1"]},
               "amendment": {"value": "3.1", "lines": ["L1"]}, "version_date": None})
    assert got["accepted"] == ["version_label", "amendment"] and got["fields"]["amendment"]["source"]["ref"]
    # the designator is the rules' amendment already: unchanged; a label without a designator: unchanged
    other = copy.deepcopy(rec)
    other["fields"]["amendment"] = rule("4", "running header")
    got = run({"version_label": {"value": "Amendment 4", "lines": ["L3"]}}, other)
    assert got["accepted"] == ["version_label"] and "amendment" not in got["fields"], got
    got = run({"version_label": {"value": "Version 2.0", "lines": ["L2"]}})
    assert got["accepted"] == ["version_label"] and "amendment" not in got["fields"], got
    # no version label accepted: nothing derived
    got = run({"version_label": {"value": "Version 9.0", "lines": ["L2"]}})
    assert got["accepted"] == [] and got["kept_rules"] == [{"field": "version_label",
                                                             "reason": "not in the cited lines"}]
    assert il._designator_of("AMENDED CLINICAL TRIAL PROTOCOL 05") == "05"
    assert il._designator_of("Amendment (a)") == "a" and il._designator_of("Amendment No. IV") == "IV"
    assert il._designator_of("Version 2.0") is None and il._designator_of("Protocol Amendments 1-3") is None


def test_verify_wrap_hyphens() -> None:
    """F4: only "-", U+2010, U+2011 and the soft hyphen wrap a word at a line end (an en or em dash does not); F5: a
    value that starts at the very beginning of a line continuing a word wrapped from the line before is refused."""
    doc, _ = make_doc([
        ("t", "The ZORBEX\u2014 A Phase 3 Study", 1),        # L1: an em dash
        ("t", "Code ZX\u2013 4417 here", 1),                # L2: an en dash
        ("t", "Protocol ZX\u2010", 1),                      # L3: a hyphen (U+2010) ending the line
        ("t", "4417 Amendment 4", 1),                       # L4
        ("t", "A Long Study of Investi\u00ad", 1),          # L5: a soft hyphen ending the line
        ("t", "gational Drug X in Adults", 1),              # L6
        ("t", "A Study of Hyper-", 1),                      # L7
        ("t", "tension Treatment of Adults", 1),            # L8
        ("t", "Protocol QX-", 1),                           # L9: the next line is on another page
        ("t", "5521 Amendment 2", 2),                       # L10
    ], pages=2)
    rec = empty_record()
    check_cases(doc, rec, [
        ("acronym", "ZORBEX", ["L1"], None),                                 # an em dash is no wrap hyphen
        ("acronym", "ZX", ["L2"], None),
        ("acronym", "ZX", ["L3", "L4"], "not a token"),
        ("acronym", "ZX", ["L3"], "not a token"),
        ("sponsor_protocol_id", "ZX-4417", ["L3", "L4"], None),
        ("brief_title", "A Long Study of Investigational Drug X in Adults", ["L5", "L6"], None),
        # F5: inside a wrapped word
        ("sponsor_protocol_id", "4417", ["L4"], "not a token"),
        ("official_title", "gational Drug X in Adults", ["L6"], "starts inside a wrapped word"),
        ("brief_title", "tension Treatment of Adults", ["L8"], "starts inside a wrapped word"),
        ("brief_title", "Treatment of Adults", ["L8"], None),               # not at the line's start
        ("amendment", "4", ["L4"], None),                                    # "4" stands later on that line too
        ("sponsor_protocol_id", "5521", ["L10"], None),                      # the line before is on another page
    ])


def test_select_lines_first_forty_groups() -> None:
    """F6: the 40 header/footer groups are the first 40 to occur; each shows its most frequent text, ordered by
    that text's first occurrence."""
    spell = lambda p: "".join("abcdefghij"[int(d)] for d in str(p))  # noqa: E731
    items = [("t", "Title page", 1), ("header", "Protocol ZX-4417 Amendment 3", 1)]
    items += [("footer", f"Footer {spell(p)}", p) for p in range(1, 46)]
    items += [("header", "Protocol ZX-4417 Amendment 4", p) for p in range(46, 51)]
    doc, refs = make_doc(items, pages=50)
    furniture = [(x["page"], x["text"], x["ref"]) for x in il.select_lines(doc) if x["layer"] == "furniture"]
    assert len(furniture) == 40, len(furniture)
    assert furniture[:-1] == [(p, f"Footer {spell(p)}", refs[1 + p]) for p in range(1, 40)], furniture[:3]
    assert furniture[-1] == (46, "Protocol ZX-4417 Amendment 4", refs[47]), furniture[-1]
    assert il.PROMPT_VERSION == "4"


# --------------------------------------------------------------------------- ask_model

def test_ask_model_request() -> None:
    messages = [{"role": "system", "content": "made-up instructions"}, {"role": "user", "content": "L1 | p. 1 | x"}]
    answer = {"fields": {"acronym": {"value": "ZORBEX-3", "lines": ["L4"]}}}
    fake = FakePost("```json\n" + json.dumps(answer) + "\n```")
    with online():
        got = il.ask_model(copy.deepcopy(messages), MODEL, post=fake)
    assert got == {"answer": answer, "usage": {"prompt_tokens": 120, "completion_tokens": 40}}, got
    assert len(fake.calls) == 1
    url, body, key, timeout = fake.calls[0]
    assert url == il.URL == "https://openrouter.ai/api/v1/chat/completions"
    assert list(body) == ["model", "temperature", "max_tokens", "messages", "provider"], list(body)
    assert body["provider"] == ZDR and json.dumps(body["provider"]) == json.dumps(ZDR)
    assert body["model"] == "anthropic/claude-opus-5.5" and body["temperature"] == 0 and body["max_tokens"] == 1500
    assert body["messages"] == messages and key == FAKE_KEY and timeout == 120
    # leading prose, no fence; no usage block
    for content in ("Here it is: " + json.dumps(answer) + " Done.", json.dumps(answer)):
        with online():
            assert il.ask_model(messages, MODEL, post=FakePost(content))["answer"] == answer
    # the default request function is openrouter_http.post_json
    import openrouter_http

    real, fake = openrouter_http.post_json, FakePost(answer)
    openrouter_http.post_json = fake
    try:
        with online():
            assert il.ask_model(messages, MODEL, timeout=7)["answer"] == answer
    finally:
        openrouter_http.post_json = real
    assert len(fake.calls) == 1 and fake.calls[0][3] == 7 and fake.calls[0][1]["provider"] == ZDR


def test_ask_model_retries() -> None:
    """429 / 5xx / transport errors: up to ATTEMPTS requests with the BACKOFF sleeps, the same body each time; other
    statuses fail at once; no message holds response text."""
    import httpx

    messages = [{"role": "user", "content": "x"}]
    answer = {"fields": {"acronym": {"value": "ZORBEX-3", "lines": ["L4"]}}}
    assert (il.ATTEMPTS, il.BACKOFF) == (3, (5, 15))
    cases = [  # (script, the answer or the error message, requests, sleeps)
        ([429], answer, 2, [5]),
        ([503, 502], answer, 3, [5, 15]),
        ([500, 500, 500], "status 500", 3, [5, 15]),
        ([429, 429, 429], "status 429", 3, [5, 15]),
        ([400], "status 400", 1, []),
        ([401], "status 401", 1, []),
        ([429, 404], "status 404", 2, [5]),
        ([httpx.ConnectError("made-up reset")], answer, 2, [5]),
        ([httpx.ReadTimeout("made-up timeout")] * 3, "transport error", 3, [5, 15]),
    ]
    for script, want, n, sleeps in cases:
        fake, slept = FakePost(answer, script=script), []
        with online():
            if isinstance(want, dict):
                assert il.ask_model(messages, MODEL, post=fake, sleep=slept.append)["answer"] == want, script
            else:
                exc = expect(lambda: il.ask_model(messages, MODEL, post=fake, sleep=slept.append),
                             il.IdentityModelError)
                assert str(exc) == want and SECRET not in repr(exc) and "made-up" not in repr(exc), (script, exc)
                assert exc.__cause__ is None
        assert len(fake.calls) == n and slept == sleeps, (script, len(fake.calls), slept)
        assert all(c[1] == fake.calls[0][1] for c in fake.calls), script  # the same body on every attempt
    # another exception is not retried
    fake = FakePost(error=RuntimeError(SECRET))
    with online():
        expect(lambda: il.ask_model(messages, MODEL, post=fake, sleep=lambda s: None), RuntimeError)
    assert len(fake.calls) == 1
    # the default sleep is time.sleep
    fake = FakePost(answer, script=[503])
    with online(), no_sleep() as slept:
        il.ask_model(messages, MODEL, post=fake)
    assert slept == [5] and len(fake.calls) == 2


def test_ask_model_errors() -> None:
    messages = [{"role": "user", "content": "x"}]
    for content in ("I cannot help with that.", '{"answer": 1}', '{"fields": ["L1"]}', "{not json", "", None,
                    "```json\n[1, 2]\n```"):
        with online():
            exc = expect(lambda: il.ask_model(messages, MODEL, post=FakePost(content)), il.IdentityModelError)
        assert str(exc) == "unparseable answer" and exc.__cause__ is None, (content, str(exc))

    def no_choices(url, body, key, timeout):
        return FakeResponse(200, {"error": {"message": SECRET}})

    def no_json(url, body, key, timeout):
        return FakeResponse(200, None, text=SECRET)

    for post in (no_choices, no_json):
        with online():
            exc = expect(lambda: il.ask_model(messages, MODEL, post=post), il.IdentityModelError)
        assert str(exc) == "unparseable answer" and SECRET not in repr(exc) and exc.__suppress_context__
    # an unsupported model and the offline refusal: nothing sent
    for model in ("anthropic/claude-opus-5.5", "openrouter:", "openrouter:  ", "local:qwen", "", None):
        fake = FakePost({"fields": {}})
        with online():
            exc = expect(lambda: il.ask_model(messages, model, post=fake), il.IdentityModelError)
        assert str(exc) == "unsupported model" and fake.calls == [], model
    fake = FakePost({"fields": {}})
    expect(lambda: il.ask_model(messages, MODEL, post=fake), OfflineRefused)  # OPENROUTER_OFFLINE=1
    assert fake.calls == []


# --------------------------------------------------------------------------- refine

def good_answer(body: dict) -> dict:
    lines = {t: i for i, t in sent_lines(body).items()}
    return {"fields": {
        "official_title": {"value": "A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With Condition Y",
                           "lines": [lines["A Phase 3, Randomized Study of Drug X Versus Placebo in Adults With "
                                           "Condition Y"]]},
        "acronym": {"value": "ZORBEX-3", "lines": [lines["With Condition Y (ZORBEX-3)"]]},
        "nct": {"value": "NCT07654321", "lines": [lines["Compare with NCT07654321 and code NCT076543219"]]},
        "sponsor": {"value": "Acme Pharma, Inc.", "lines": [lines["Sponsor Name: Acme Pharma, Inc."]]},
    }}


def test_refine_cache() -> None:
    from figure_vlm import document_key

    doc, lines, _ = verify_doc()
    record = verify_record()
    before = copy.deepcopy(record)
    fake = FakePost(good_answer)
    with temp_dir() as tmp:
        cache = tmp / "identity_cache"
        with online():
            out, llm = il.refine(doc, record, model=MODEL, cache_dir=cache, section_of=SEC, post=fake)
        assert len(fake.calls) == 1 and record == before
        assert llm == {"accepted": ["official_title", "acronym"], "confirmed": ["sponsor"],
                       "kept_rules": [{"field": "nct", "reason": "registry id: the rule value is kept"}],
                       "cache": "miss", "lines": len(lines),
                       "usage": {"prompt_tokens": 120, "completion_tokens": 40}}, llm
        assert out["fields"]["official_title"]["method"] == MODEL_METHOD
        assert out["fields"]["acronym"]["value"] == "ZORBEX-3"
        assert {k: v for k, v in out["fields"].items() if k not in ("official_title", "acronym")} == \
            {k: v for k, v in record["fields"].items() if k not in ("official_title", "acronym")}
        assert out["cover_page"] == record["cover_page"] and out is not record
        out["fields"]["sponsor"]["value"] = "changed"  # a deep copy: the record given stays as it was
        assert record == before
        # the cache entry: its name, its keys, no temporary file left
        files = sorted(p.name for p in cache.iterdir())
        key = hashlib.sha256(json.dumps(
            [MODEL, il.PROMPT_VERSION, [[x["id"], x["page"], x["layer"], x["text"]] for x in lines],
             {f: (record["fields"][f] or {}).get("value") for f in il.MODEL_FIELDS}], sort_keys=True)
            .encode("utf-8")).hexdigest()
        assert files == [f"{key}.json"], files
        entry = json.loads((cache / files[0]).read_text(encoding="utf-8"))
        assert set(entry) == {"model", "prompt_version", "doc_key", "answer", "usage"}, sorted(entry)
        assert entry["model"] == MODEL and entry["prompt_version"] == il.PROMPT_VERSION
        assert entry["doc_key"] == document_key(doc) and entry["answer"] == good_answer(fake.calls[0][1])
        assert b"\r" not in (cache / files[0]).read_bytes() and FAKE_KEY not in (cache / files[0]).read_text("utf-8")
        # a hit: offline, nothing sent, the same result
        out2, llm2 = il.refine(doc, record, model=MODEL, cache_dir=cache, section_of=SEC, post=fake)
        assert len(fake.calls) == 1 and llm2 == {**llm, "cache": "hit"}
        out["fields"]["sponsor"]["value"] = record["fields"]["sponsor"]["value"]
        assert out2 == out
        # the cached answer is verified again on every call
        entry["answer"]["fields"]["official_title"]["lines"] = ["L99"]
        (cache / files[0]).write_text(json.dumps(entry), encoding="utf-8")
        _, llm3 = il.refine(doc, record, model=MODEL, cache_dir=cache, section_of=SEC, post=fake)
        assert llm3["accepted"] == ["acronym"] and len(fake.calls) == 1
        assert {"field": "official_title", "reason": "no such line"} in llm3["kept_rules"]
        # offline without an entry (another rule value, another model): refused, nothing sent, nothing written
        other = verify_record()
        other["fields"]["acronym"] = rule("ZX", "title page")
        expect(lambda: il.refine(doc, other, model=MODEL, cache_dir=cache, section_of=SEC, post=fake), OfflineRefused)
        expect(lambda: il.refine(doc, record, model="openrouter:made-up/model-x", cache_dir=cache, section_of=SEC,
                                 post=fake), OfflineRefused)
        assert len(fake.calls) == 1 and len(list(cache.iterdir())) == 1
        # an unsupported model never reads the cache; a broken entry is asked again
        expect(lambda: il.refine(doc, record, model="local:qwen", cache_dir=cache, section_of=SEC, post=fake),
               il.IdentityModelError)
        (cache / files[0]).write_text("{broken", encoding="utf-8")
        with online():
            _, llm4 = il.refine(doc, record, model=MODEL, cache_dir=cache, section_of=SEC, post=fake)
        assert llm4["cache"] == "miss" and len(fake.calls) == 2 and _read(cache / files[0])["answer"]
        # no cache folder yet: made on the first miss
        with online():
            il.refine(doc, record, model=MODEL, cache_dir=tmp / "new" / "cache", section_of=SEC, post=fake)
        assert len(list((tmp / "new" / "cache").iterdir())) == 1
        # a cache that cannot be written: no temporary file left, the answer used all the same, the type logged
        real_replace, replaced = os.replace, []

        def failing_replace(src, dst):
            replaced.append(Path(src).name)
            raise PermissionError(f"made-up refusal {SECRET}")

        (tmp / "ro").mkdir()
        os.replace = failing_replace
        try:
            with online(), capture_log() as logs:
                out5, llm5 = il.refine(doc, record, model=MODEL, cache_dir=tmp / "ro", section_of=SEC, post=fake)
        finally:
            os.replace = real_replace
        assert llm5 == {**llm, "cache": "miss"} and out5["fields"] == out2["fields"] and len(fake.calls) == 4
        assert list((tmp / "ro").iterdir()) == []
        assert any("PermissionError" in m for m in logs) and not any(SECRET in m for m in logs), logs
        # F9: the temporary file is named after the document key (the service's purge finds a crash leftover)
        assert len(replaced) == 1 and replaced[0].startswith(f"{document_key(doc)}.") and replaced[0].endswith(".tmp")
        (tmp / "a_file").write_text("not a folder", encoding="utf-8")
        with online():
            _, llm6 = il.refine(doc, record, model=MODEL, cache_dir=tmp / "a_file", section_of=SEC, post=fake)
        assert llm6["cache"] == "miss" and llm6["accepted"] == llm["accepted"] and len(fake.calls) == 5


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- run_identity on a public protocol

def alexion_answer(body: dict) -> dict:
    """A made-up model answer for public Alexion, by line text: the brief title the rules cut at a line break, the
    version of the running header and the amendment "3.1" (the rules: "3") are fixed; the sponsor and NCT confirmed;
    an acronym inside the protocol number and a date the cited line does not hold refused."""
    lines = sent_lines(body)

    def find(pred) -> str:
        return next(i for i, t in lines.items() if pred(t))

    short = find(lambda t: t.startswith("Short Title:"))
    return {"fields": {
        "brief_title": {"value": "Copper and Molybdenum Balance in Participants with Wilson Disease Treated with "
                                 "ALXN1840", "lines": [short, f"L{int(short[1:]) + 1}"]},
        "sponsor": {"value": "Alexion Pharmaceuticals, Inc.", "lines": [find(lambda t: t.startswith("Sponsor Name:"))]},
        "nct": {"value": "NCT04573309", "lines": [find(lambda t: "NCT04573309" in t)]},
        "acronym": {"value": "WD", "lines": [find(lambda t: "ALXN1840-WD-204" in t)]},
        "amendment": {"value": "3.1", "lines": [find(lambda t: t.startswith("Amendment Number:"))]},
        "version_label": {"value": "Amendment 3.1 (US)", "lines": [find(lambda t: t == "Protocol Amendment 3.1 (US)")]},
        "version_date": {"value": "18 Mar 2022", "lines": [find(lambda t: t == "Protocol Amendment 3.1 (US)")]},
        "phase": {"value": "2", "lines": ["L1"]},
    }}


def test_run_identity_public() -> None:
    from protocol_api import ProtocolDocument

    with temp_dir() as tmp:
        folder = public_copy(ALEXION, tmp)
        if folder is None:
            return
        chunks, cache = folder / "chunks", folder / "identity_cache"
        target = chunks / RECORD_FILE
        pd = ProtocolDocument.open(folder)
        rules = json.loads(json.dumps(pd.rules_identity_record()))
        # no model: the rules record
        res = il.run_identity(folder, model="", cache_dir=cache)
        assert res == {"status": "done", "model": None, "accepted": 0, "confirmed": 0, "kept_rules": 0,
                       "cache": None}, res
        rules_bytes = target.read_bytes()
        data, why = load_record(chunks, pd.source_sha256, pd.meta)  # F1: written with the stats values it came from
        assert why is None and data["model"] is None and data["prompt_version"] is None and data["llm"] is None
        assert data["meta_sha256"] == meta_stamp(pd.meta)
        assert {k: data[k] for k in ("fields", "cover_page", "others")} == \
            {k: rules[k] for k in ("fields", "cover_page", "others")}
        assert il.run_identity(chunks, model="  ", cache_dir=cache)["status"] == "done"  # a chunks folder works too
        assert target.read_bytes() == rules_bytes and not cache.exists()
        # offline without a cache entry: skipped, the rules record
        res = il.run_identity(folder, model=MODEL, cache_dir=cache)
        assert res == {"status": "skipped_offline", "model": MODEL, "accepted": 0, "confirmed": 0, "kept_rules": 0,
                       "cache": None}, res
        assert target.read_bytes() == rules_bytes and not cache.exists()
        # an unsupported model, an upstream error, a failing request: partial, the exception type only
        for model, post, error, reason, calls in (
                ("local:qwen", None, "IdentityModelError", "unsupported model", 0),
                (MODEL, FakePost(status=429), "IdentityModelError", None, 3),          # retried, then given up
                (MODEL, FakePost(error=RuntimeError(SECRET)), "RuntimeError", None, 1)):
            with online(), no_sleep() as slept:
                res = il.run_identity(folder, model=model, cache_dir=cache, post=post)
            want = {"status": "partial", "model": model, "accepted": 0, "confirmed": 0, "kept_rules": 0,
                    "cache": None, "error": error, **({"reason": reason} if reason else {})}
            assert res == want, res
            assert SECRET not in json.dumps(res) and target.read_bytes() == rules_bytes
            assert (len(post.calls) if post else 0) == calls and slept == ([5, 15] if calls == 3 else []), slept
        # a cache that cannot be written (the cache folder is a file): done all the same
        (tmp / "a_file").write_text("not a folder", encoding="utf-8")
        with online():
            res = il.run_identity(folder, model=MODEL, cache_dir=tmp / "a_file", post=FakePost(alexion_answer))
        assert res["status"] == "done" and res["cache"] == "miss" and res["accepted"] == 3, res
        # a fake model: the accepted fields carry the model method, their source and the rule value replaced
        fake = FakePost(alexion_answer)
        with online(), capture_log() as logs:
            res = il.run_identity(folder, model=MODEL, cache_dir=cache, post=fake)
        assert res == {"status": "done", "model": MODEL, "accepted": 3, "confirmed": 2, "kept_rules": 2,
                       "cache": "miss"}, res
        assert len(fake.calls) == 1 and fake.calls[0][1]["provider"] == ZDR
        # the log: counts, field names and status; no line text, no answer value
        assert logs and any("brief_title" in m for m in logs), logs
        assert_no_document_text(logs, fake.calls[0][1], alexion_answer(fake.calls[0][1]))
        data, why = load_record(chunks, pd.source_sha256)
        assert why is None and data["model"] == MODEL and data["prompt_version"] == il.PROMPT_VERSION
        llm = data["llm"]
        accepted = ["brief_title", "version_label", "amendment"]
        assert llm["accepted"] == accepted and llm["confirmed"] == ["sponsor", "nct"], llm
        assert llm["kept_rules"] == [{"field": "acronym", "reason": "not a token"},
                                     {"field": "version_date", "reason": "not in the cited lines"}], llm
        assert llm["cache"] == "miss" and llm["lines"] == len(sent_lines(fake.calls[0][1])) and llm["usage"]
        brief, version = data["fields"]["brief_title"], data["fields"]["version_label"]
        assert brief["value"] == "Copper and Molybdenum Balance in Participants with Wilson Disease Treated with " \
                                 "ALXN1840" and brief["method"] == MODEL_METHOD
        assert brief["rules"] == {"value": rules["fields"]["brief_title"]["value"], "method": "cover-page label"}
        assert brief["source"]["layer"] == "body" and len(brief["source"]["refs"]) == 2
        assert brief["source"]["section_id"] == pd._section_of(brief["source"]["ref"])
        assert version["value"] == "Amendment 3.1 (US)" and version["source"]["layer"] == "furniture"
        assert version["source"]["section_id"] is None
        assert version["rules"] == {k: rules["fields"]["version_label"][k] for k in ("value", "method")}
        amendment = data["fields"]["amendment"]
        assert amendment["value"] == "3.1" and amendment["source"]["text"] == "Amendment Number: 3.1 (US)"
        assert amendment["rules"] == {k: rules["fields"]["amendment"][k] for k in ("value", "method")}
        for f in (brief, version, amendment):
            assert set(f["source"]) == SOURCE_KEYS and f["method"] == MODEL_METHOD
            src = dict(f["source"])
            check_source(pd.doc, src, lambda ref, layer=src["layer"]: None if layer == "furniture"
                         else pd._section_of(ref), f["value"])
        for key in FIELDS:  # every other field is the rules'
            if key not in accepted:
                assert data["fields"][key] == rules["fields"][key], key
        model_bytes = target.read_bytes()
        assert SECRET not in model_bytes.decode("utf-8") and FAKE_KEY not in model_bytes.decode("utf-8")
        # the same input twice: the same bytes (offline cache hits, nothing sent)
        assert il.run_identity(folder, model=MODEL, cache_dir=cache, post=fake)["cache"] == "hit"
        hit_bytes = target.read_bytes()
        assert il.run_identity(folder, model=MODEL, cache_dir=cache, post=fake)["cache"] == "hit"
        assert target.read_bytes() == hit_bytes and len(fake.calls) == 1
        assert {**json.loads(model_bytes)["llm"], "cache": "hit"} == json.loads(hit_bytes)["llm"]
        il.run_identity(folder, model="", cache_dir=cache)
        assert target.read_bytes() == rules_bytes
        # dest_dir: written there, the chunks folder's record untouched
        res = il.run_identity(folder, model=MODEL, cache_dir=cache, dest_dir=tmp / "scored" / "alexion")
        assert res["status"] == "done" and res["cache"] == "hit"
        assert (tmp / "scored" / "alexion" / RECORD_FILE).read_bytes() == hit_bytes
        assert target.read_bytes() == rules_bytes


def test_run_identity_without_source_hash() -> None:
    """F8: a stats.json without the Docling JSON's source_sha256 (an older output): no record is written (it could
    never be loaded) and nothing is asked; status "partial", reason "no source hash"; an identity.json already there
    is left as it is."""
    with temp_dir() as tmp:
        folder = public_copy(ALEXION, tmp)
        if folder is None:
            return
        stats_path, target = folder / "chunks" / "stats.json", folder / "chunks" / RECORD_FILE
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        del stats["document"]["source_sha256"]
        stats_path.write_text(json.dumps(stats), encoding="utf-8")
        for model in ("", MODEL):
            fake = FakePost(alexion_answer)
            with online():
                res = il.run_identity(folder, model=model, cache_dir=folder / "identity_cache", post=fake)
            assert res == {"status": "partial", "model": model or None, "accepted": 0, "confirmed": 0, "kept_rules": 0,
                           "cache": None, "reason": "no source hash"}, res
            assert not target.exists() and fake.calls == [] and not (folder / "identity_cache").exists()
        target.write_text("left as it is", encoding="utf-8")
        assert il.run_identity(folder, model="", cache_dir=folder / "identity_cache")["reason"] == "no source hash"
        assert target.read_text(encoding="utf-8") == "left as it is"
        code, out = main_output(["--chunks", str(folder / "chunks"), "--model", ""])
        assert code == 0 and out == ["identity: partial, model none, accepted 0 [], confirmed 0 [], kept_rules 0 [], "
                                     "cache none, reason no source hash"], out


# --------------------------------------------------------------------------- the command line

def run_cli(args: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "OPENROUTER_OFFLINE": "1"}
    return subprocess.run([sys.executable, str(ROOT / "identity_llm.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, cwd=str(ROOT), timeout=600)


def main_output(argv: list[str]) -> tuple[int, list[str]]:
    """identity_llm.main(argv) in this process: (exit code, the lines it printed)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = il.main(argv)
    return code, buf.getvalue().splitlines()


def test_command_line() -> None:
    with temp_dir() as tmp:
        folder = public_copy(ALEXION, tmp)
        if folder is None:
            return
        chunks = folder / "chunks"
        # the script itself, rules only
        proc = run_cli(["--chunks", str(chunks), "--model", ""])
        assert proc.returncode == 0, (proc.returncode, proc.stderr[-2000:])
        out = proc.stdout.strip().splitlines()
        assert out == ["identity: done, model none, accepted 0 [], confirmed 0 [], kept_rules 0 [], cache none"], out
        written = (chunks / RECORD_FILE).read_bytes()
        il.run_identity(folder, model="", cache_dir=folder / "identity_cache")
        assert (chunks / RECORD_FILE).read_bytes() == written  # the rules record
        # the default model (IDENTITY_MODEL) offline without a cache entry: skipped, exit 0, nothing cached
        old = os.environ.get("IDENTITY_MODEL")
        os.environ["IDENTITY_MODEL"] = MODEL
        try:
            code, out = main_output(["--chunks", str(chunks), "--dest", str(tmp / "dest")])
        finally:
            if old is None:
                os.environ.pop("IDENTITY_MODEL")
            else:
                os.environ["IDENTITY_MODEL"] = old
        assert code == 0 and out == [f"identity: skipped_offline, model {MODEL}, accepted 0 [], confirmed 0 [], "
                                     "kept_rules 0 [], cache none"], out
        assert (tmp / "dest" / RECORD_FILE).read_bytes() == written and not (folder / "identity_cache").exists()
        # a fake model (the default request function replaced): the field names, never values; the document folder
        # given instead of its chunks folder; the cache in the document folder
        import openrouter_http

        real, fake = openrouter_http.post_json, FakePost(alexion_answer)
        openrouter_http.post_json = fake
        try:
            with online():
                code, out = main_output(["--chunks", str(folder), "--model", MODEL])
        finally:
            openrouter_http.post_json = real
        assert code == 0 and len(fake.calls) == 1 and out == [
            f"identity: done, model {MODEL}, accepted 3 [brief_title version_label amendment], confirmed 2 [sponsor "
            "nct], kept_rules 2 [acronym version_date], cache miss"], out
        assert_no_document_text(out, fake.calls[0][1], alexion_answer(fake.calls[0][1]))
        assert len(list((folder / "identity_cache").iterdir())) == 1
        assert load_record(chunks, json.loads((chunks / "stats.json").read_text(encoding="utf-8"))["document"]
                           ["source_sha256"])[0]["model"] == MODEL
        # no such folder: exit 1, the exception type only
        code, out = main_output(["--chunks", str(tmp / "missing" / "chunks"), "--model", ""])
        assert code == 1 and len(out) == 1 and out[0].startswith("identity: failed (FileNotFoundError at "), out
        assert str(tmp) not in out[0]


if __name__ == "__main__":
    tests = [f for name, f in dict(globals()).items() if name.startswith("test_")]
    for t in tests:
        t()
        print("ok", t.__name__)
    print(f"{len(tests)} tests passed")
