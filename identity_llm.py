"""The identity stage: chunks/identity.json for a converted protocol, the rules record (identity.build_identity) with
the values a model chose from the title pages, each one verified by code in the lines the model cited.

    from identity_llm import run_identity
    run_identity(out_dir, model="openrouter:anthropic/claude-opus-5.5", cache_dir=out_dir / "identity_cache")
    python identity_llm.py --chunks <document folder>/chunks [--model M] [--cache DIR] [--dest DIR]

The model only selects; the code verifies. The model sees numbered lines (select_lines: the body text and table rows
of pages 1-3 in reading order, then the document's distinct running headers/footers) and the rule values as
candidates, and answers per field (MODEL_FIELDS) the value as printed and the ids of the lines it is printed on. A
value is used only when the cited lines exist (1 to MAX_LINES_PER_FIELD, consecutive, on one page and in one layer; a
header/footer line only alone), the value occurs in their text joined with " " (after normalising Unicode forms,
quotes, dashes, soft hyphens, whitespace and case; as it is, or with a wrap hyphen "investi- gational" joined or
"placebo- controlled" kept; wrap hyphens are "-", U+2010, U+2011 and the soft hyphen, never an en or em dash), it
does not start inside a word wrapped from the line before, and it has the field's shape:
  * titles: 3 words or more, not starting with a cover-page label ("Protocol Title: ...");
  * acronym, sponsor_protocol_id, registry ids: a whole token of the cited text (a registry id matching its pattern;
    a protocol id that is not a registry id: a bare 5-6 digit number counts as an IND number only where the cited
    lines print it after an "IND" label);
  * sponsor: a name, no cover-page label, no address part (", 51368", a number of 4 digits or more);
  * amendment: a designator after the whole word "Amendment" or "Amended" in the cited lines joined: one with a
    digit within 30 characters of the word ("3.1", "05" in "AMENDED CLINICAL TRIAL PROTOCOL 05"); one letter or a
    roman numeral right after the word, with only these between: "No.", "Nr.", "Number", ":", "#", "(" and spaces
    ("Amendment (a)", "Amendment No. B", "Amendment IV");
  * version_label: a version word (version, amendment, amended, revision, edition) and a digit, or a designator after
    the word with the same separators; version_date: a year and a month name or a numeric date, not a registry id.
A verified value equal to the rule value confirms it; a registry id the rules found is never replaced (the model may
only fill a missing one); any other verified value replaces the rule value, with method identity.MODEL_METHOD, its
source from the cited lines and the replaced rule value and method under "rules". A version label accepted without
its amendment gives the amendment its designator ("Amendment 3.1 (US)" -> "3.1"). Every other answer keeps the rule
value, with the reason (llm "kept_rules").

What is sent: only the selected lines (with page and layer) and the rule values, in one chat request per document to
https://openrouter.ai/api/v1/chat/completions with zero-data-retention routing (provider: data_collection "deny",
zdr, no fallbacks), retried on a rate limit, a server error or a transport error (ATTEMPTS in all); nothing while
OPENROUTER_OFFLINE is set. The answer is cached in cache_dir under a hash of (model, PROMPT_VERSION, the lines, the
rule values), with figure_vlm.document_key(doc) so the service's cache purge finds a document's entries (best effort:
an answer that cannot be cached is still used); a cache hit sends nothing (it works offline) and is verified again on
every run. Logs and the command line carry counts, field names and status only, never document text or values.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
import unicodedata
from pathlib import Path
from typing import Any, Callable

from docling_core.types.doc import ContentLayer, DoclingDocument, TableItem, TextItem

log = logging.getLogger("identity_llm")

PROMPT_VERSION = "4"  # bump with any change of INSTRUCTIONS, of the lines selected or of the message layout (it
#                       keys the cache). 2: headers/footers that differ only in their digits are one line;
#                       3: that line shows the most frequent of their texts; 4: the 40 groups are the first 40 to occur
URL = "https://openrouter.ai/api/v1/chat/completions"
ATTEMPTS, BACKOFF = 3, (5, 15)  # requests in all; seconds before the 2nd and the 3rd (rate limit, 5xx, transport)
MODEL_FIELDS = ("official_title", "brief_title", "acronym", "sponsor", "sponsor_protocol_id", "nct", "eudract",
                "eu_ct", "ind", "who_utn", "version_label", "amendment", "version_date")  # detected_title: rules only
REGISTRY_FIELDS = ("nct", "eudract", "eu_ct", "ind", "who_utn")
MAX_LINES_PER_FIELD = 4
MAX_LINE_CHARS, MAX_LINES, MAX_CHARS, MAX_FURNITURE = 600, 400, 40_000, 40
_MODEL_PREFIX = "openrouter:"


class IdentityModelError(RuntimeError):
    """The model step failed: "unsupported model", "status <code>", "transport error", "unparseable answer". The
    message never holds response or document text."""


# =========================================================================== the lines the model sees

def _first_page(item: Any) -> int | None:
    pages = [p.page_no for p in getattr(item, "prov", None) or []]
    return min(pages) if pages else None


def _line_text(text: str) -> str:
    return " ".join(text.split())[:MAX_LINE_CHARS]


def select_lines(doc: DoclingDocument, pages: int = 3) -> list[dict]:
    """The numbered lines the model sees: [{"id": "L1", "page", "layer": "body" | "table" | "furniture", "text",
    "ref", "row"}], ids in order.

    First the body text items whose first page is <= pages, in reading order (pictures not traversed), a table on
    those pages as one line per non-empty grid row in its place (ref = the table, row = the grid index); then the
    distinct running headers/footers of the whole document (distinct by text with its digits ignored: "Page 3 of 120"
    and "Page 4 of 120" are one group): the first MAX_FURNITURE groups by their first occurrence, each shown as its
    most frequent text (the earliest on a tie) with that text's first page and ref ("... Amendment 4" on pp. 3-12
    rather than "... Amendment 3" on pp. 1-2), ordered by that text's first occurrence. Texts are
    whitespace-collapsed and cut to MAX_LINE_CHARS. At most MAX_LINES lines and MAX_CHARS characters in all: the
    headers/footers are set aside first, the body and table lines are added in order until the next one would not
    fit. Deterministic."""
    from chunk_protocol import _row_text

    texts = [t for t in doc.texts if t.content_layer == ContentLayer.FURNITURE and t.text.strip()]
    texts.sort(key=lambda t: (_first_page(t) is None, _first_page(t) or 0))  # stable: document order on a page
    groups: dict[str, dict[str, list]] = {}  # digits ignored -> exact text -> [count, rank of its first, its item]
    for rank, t in enumerate(texts):  # groups in the order of their first occurrence (dicts keep insertion order)
        text = " ".join(t.text.split())
        variants = groups.setdefault(re.sub(r"\d+", "#", text), {})
        if text in variants:
            variants[text][0] += 1
        else:
            variants[text] = [1, rank, t]
    chosen = list(groups.values())[:MAX_FURNITURE]
    shown = sorted((rank, text, t) for variants in chosen
                   for text, (_, rank, t) in [max(variants.items(), key=lambda v: (v[1][0], -v[1][1]))])
    furniture = [{"page": _first_page(t), "layer": "furniture", "text": _line_text(text), "ref": t.self_ref,
                  "row": None} for _, text, t in shown]
    room_lines = MAX_LINES - len(furniture)
    room_chars = MAX_CHARS - sum(len(x["text"]) for x in furniture)
    body: list[dict] = []
    full = False
    for item, _ in doc.iterate_items(with_groups=False, traverse_pictures=False,
                                     included_content_layers={ContentLayer.BODY}):
        page = _first_page(item)
        if page is None or page > pages:
            continue
        if isinstance(item, TableItem):
            todo = [("table", _row_text(row), r) for r, row in enumerate(item.data.grid or [])]
        elif isinstance(item, TextItem):
            todo = [("body", item.text, None)]
        else:
            continue
        for layer, text, row in todo:
            text = _line_text(text or "")
            if not text:
                continue
            if len(body) + 1 > room_lines or len(text) > room_chars:
                full = True
                break
            body.append({"page": page, "layer": layer, "text": text, "ref": item.self_ref, "row": row})
            room_chars -= len(text)
        if full:
            break
    return [{"id": f"L{i}", **x} for i, x in enumerate(body + furniture, start=1)]


# =========================================================================== the request

INSTRUCTIONS = (
    "The numbered lines are the first pages of one clinical-trial protocol, followed by its distinct running headers "
    "and footers. Each line reads: id | page | layer | text (layer: body = page text, table = one table row, "
    "furniture = a running header or footer).\n"
    "For each field below, give its value exactly as printed (the same words, spelling, case and punctuation) and "
    "the ids of the line(s) it is printed on. Cite several lines only when one value wraps over consecutive lines. "
    "Give null, with no lines, when the field is not printed. Never infer, translate, expand, shorten or combine "
    "values from different places.\n"
    "Fields:\n"
    "- official_title: the full scientific title of the study.\n"
    "- brief_title: a short or public title of the study, only if one is printed.\n"
    "- acronym: the study's acronym or short name, only if one is printed; not a compound, product or protocol "
    "code.\n"
    "- sponsor: the name of the sponsor organisation (the name only, no address).\n"
    "- sponsor_protocol_id: the sponsor's protocol number or code.\n"
    "- nct: this study's ClinicalTrials.gov number (NCT and 8 digits).\n"
    "- eudract: this study's EudraCT number.\n"
    "- eu_ct: this study's EU CT number (CTIS).\n"
    "- ind: this study's IND number (the number only).\n"
    "- who_utn: this study's WHO Universal Trial Number (UTN).\n"
    "- version_label: the version of this document as printed, e.g. \"Amendment 5\", \"Amendment (a)\" or "
    "\"Version 2.0\".\n"
    "- amendment: the amendment number (or letter) of this document only, as printed, e.g. \"5\", \"3.1\" or \"a\".\n"
    "- version_date: the date of this version of the document, as printed.\n"
    "The candidates were found by rules and may be wrong or incomplete: check each one against the lines.\n"
    "Answer with one JSON object and nothing else, every field listed: "
    "{\"fields\": {\"<field>\": {\"value\": \"<as printed>\" or null, \"lines\": [\"L1\"]}}}"
)


def _rule_values(record: dict) -> dict[str, str | None]:
    return {f: (record["fields"].get(f) or {}).get("value") for f in MODEL_FIELDS}


def build_messages(lines: list[dict], record: dict) -> list[dict]:
    """The chat messages: INSTRUCTIONS, then the rule values as candidates and the numbered lines."""
    shown = "\n".join(f"{x['id']} | p. {x['page'] if x['page'] is not None else '-'} | {x['layer']} | {x['text']}"
                      for x in lines)
    user = ("Candidates (found by rules, may be wrong or incomplete):\n"
            + json.dumps(_rule_values(record), ensure_ascii=False, indent=1)
            + "\n\nLines:\n" + shown)
    return [{"role": "system", "content": INSTRUCTIONS}, {"role": "user", "content": user}]


def _model_name(model: str) -> str:
    """The OpenRouter model id of an "openrouter:<id>" model; anything else: IdentityModelError."""
    name = model[len(_MODEL_PREFIX):].strip() if isinstance(model, str) and model.startswith(_MODEL_PREFIX) else ""
    if not name:
        raise IdentityModelError("unsupported model")
    return name


def _parse_answer(content: Any) -> dict:
    """The first JSON object of the answer text (inside a ```json fence when there is one), with a "fields" dict."""
    if not isinstance(content, str):
        raise IdentityModelError("unparseable answer")
    fenced = re.search(r"```(?:json)?[ \t]*\n?(.*?)```", content, re.S | re.I)
    text = fenced.group(1) if fenced else content
    start = text.find("{")
    try:
        answer = json.JSONDecoder().raw_decode(text, start)[0] if start >= 0 else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("fields"), dict):
        raise IdentityModelError("unparseable answer")
    return answer


def ask_model(messages: list[dict], model: str, *, post: Callable | None = None, timeout: float = 120,
              sleep: Callable[[float], None] | None = None) -> dict:
    """One chat request to OpenRouter with zero-data-retention routing: {"answer": the parsed JSON object, "usage":
    the usage block or {}}. model: "openrouter:<model id>". post: the request function (default
    openrouter_http.post_json; tests pass a fake). A status 429 or 5xx and an httpx.TransportError are retried, the
    same body sent again (ATTEMPTS in all, BACKOFF seconds between them; sleep: default time.sleep). Raises
    retrieve.OfflineRefused when OPENROUTER_OFFLINE is set, IdentityModelError for another model kind ("unsupported
    model"), another status or the last retried one ("status <code>"), the last transport error ("transport error")
    or an answer that is not the JSON object ("unparseable answer")."""
    import httpx

    from figure_vlm import _api_key
    from openrouter_http import post_json
    from retrieve import require_online

    name = _model_name(model)
    require_online()  # OPENROUTER_OFFLINE=1: refused before the key is read or anything is sent
    key = _api_key("OPENROUTER_API_KEY")
    body = {
        "model": name,
        "temperature": 0,
        "max_tokens": 1500,
        "messages": messages,
        "provider": {"data_collection": "deny", "zdr": True, "allow_fallbacks": False},
    }
    sleep = sleep or time.sleep
    for attempt in range(1, ATTEMPTS + 1):
        try:
            if post is None:
                resp = post_json(URL, body, key, timeout)
            else:
                resp = post(URL, body, key, timeout)
        except httpx.TransportError as exc:
            if attempt == ATTEMPTS:
                raise IdentityModelError("transport error") from None
            log.info("identity model: %s, attempt %d of %d", type(exc).__name__, attempt, ATTEMPTS)
        else:
            retried = resp.status_code == 429 or 500 <= resp.status_code < 600
            if resp.status_code == 200 or not retried or attempt == ATTEMPTS:
                break
            log.info("identity model: status %s, attempt %d of %d", resp.status_code, attempt, ATTEMPTS)
        sleep(BACKOFF[min(attempt, len(BACKOFF)) - 1])
    if resp.status_code != 200:
        raise IdentityModelError(f"status {resp.status_code}")
    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except Exception:  # noqa: BLE001 - any other shape is an unparseable answer
        raise IdentityModelError("unparseable answer") from None
    answer = _parse_answer(content)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    log.info("identity model: %s prompt tokens, %s completion tokens", usage.get("prompt_tokens"),
             usage.get("completion_tokens"))
    return {"answer": answer, "usage": usage}


# =========================================================================== verification

_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2212"
_QUOTES = {"'": "\u2018\u2019\u201a\u201b", '"': "\u201c\u201d\u201e\u201f"}  # curly quotes -> straight
_TRANSLATE = {**{ord(c): "-" for c in _DASHES}, ord("\u00ad"): None,  # soft hyphen: removed
              **{ord(c): q for q, curly in _QUOTES.items() for c in curly}}
_TO_HYPHEN = {ord(c): "-" for c in _DASHES}
# Wrap hyphens: what ends a line inside a wrapped word ("-", U+2010, U+2011, the soft hyphen; never an en or em dash
# or the minus sign: "ZORBEX\u2014 A Phase 3 Study" holds the token "ZORBEX"). They are found in the printed text,
# before the dashes are normalised, and marked with a private-use character no text holds.
_WRAP_HYPHENS = "-\u2010\u2011\u00ad"
_MARK = "\ue000"
_LINE_END_WRAP = re.compile(rf"(?<=[^\W_])[{_WRAP_HYPHENS}]$")             # "Protocol ZX-" (a cited line's end)
_IN_TEXT_WRAP = re.compile(rf"(?<=[^\W_])[{_WRAP_HYPHENS}](?=\s+[^\W_])")  # "investi- gational" (within a line)
_JOINED = re.compile(rf"{_MARK} (?=[^\W_])")  # a wrap the next word continues
# A whole token: not preceded or followed by a word character (a letter, digit or "_"), nor by a "-", "/" or "."
# joined to one ("WD" is not a token of "ALXN1840-WD-204", "DRUGX" not one of "DRUGX-3", "123" not one of
# "ABC/123/456", "ABC" not one of "ABC_DEF", "ZORBEX" not one of "ZORBEX.Study"), except the "." of the abbreviations
# No. / Nr. / Ref. ("IND No.140,453", "Protocol No.ABC-123"); nor followed by a wrap hyphen ("ZX" is not a token of
# "Protocol ZX-"). So "ABC-123/Amendment 2" or "NCT04573309/2019-003431-33" hold no token "ABC-123" or "NCT04573309".
_TOKEN_BEFORE = r"(?<!\w)(?:(?<!\w[-/.])|(?<=\bno\.)|(?<=\bnr\.)|(?<=\bref\.))"
_TOKEN_AFTER = rf"(?!\w)(?![-/.]\w)(?!{_MARK})"
# A whole number or designator: no word character or decimal part around it ("3" is not one of "3.1"; "a" is one of
# "(a)").
_NUMBER_BEFORE, _NUMBER_AFTER = r"(?<!\w)(?<!\d[.,])", r"(?!\w)(?![.,]\d)"
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")
_MONTH = re.compile(r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
                    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b")  # in normalised text
_NUMERIC_DATE = re.compile(r"(?<!\d)(?:\d{1,2}[./-]\d{1,2}[./-]\d{4}|\d{4}-\d{1,2}-\d{1,2})(?!\d)")
# The word before an amendment designator (not "Amendments"), and what may stand between it and a designator
# without a digit: "No." / "Nr." / "Number" / ":" / "#" / "(" and spaces.
_AMENDMENT_WORD = r"\b(?:amendment|amended)\b"
_DESIGNATOR_LEAD = r"(?:\s*(?:(?:no|nr)\b\.?|number\b|[:#(]))*\s*"
# An amendment designator without a digit: one letter or a roman numeral from i to xx ("a", "IV", "xii").
_ROMAN = r"x?(?:ix|iv|v?i{1,3}|v)|xx?"
_LETTER_OR_ROMAN = re.compile(rf"(?i)[^\W\d_]|{_ROMAN}")
# A version label names a version word; without a digit it needs such a designator after the word, with the same
# separators ("Amendment (a)", "Amendment No. B", "Revision B", "Amendment IV"; not "Amendment Number", not "Protocol
# Amendments", not "ZX-4417"). On normalised text.
_VERSION_WORD = re.compile(r"\b(?:version|amendment|amended|revision|edition)\b")
_DESIGNATED = re.compile(r"\b(?:version|amendment|amended|revision|edition)\b" + _DESIGNATOR_LEAD
                         + rf"(?:[^\W\d_]|{_ROMAN})(?![^\W_])")


def _norm(text: str | None) -> str:
    """NFKC, curly quotes straight, dashes "-", soft hyphens removed, whitespace collapsed, casefolded."""
    return " ".join(unicodedata.normalize("NFKC", text or "").translate(_TRANSLATE).split()).casefold()


def _marked(texts: list[str]) -> str:
    """The cited lines joined with " " and normalised, each wrap hyphen marked (_MARK): at the end of a line, and
    before a space and a word within one."""
    return _norm(_IN_TEXT_WRAP.sub(_MARK, " ".join(_LINE_END_WRAP.sub(_MARK, x) for x in texts)))


def _forms(texts: list[str]) -> list[str]:
    """The normalised cited text as it is (wrap hyphens shown as "-"); when a wrap hyphen is followed by the next
    word, also with each such joined ("investi- gational" -> "investigational") and kept ("placebo- controlled" ->
    "placebo-controlled")."""
    t = _marked(texts)
    if not _JOINED.search(t):
        return [t.replace(_MARK, "-")]
    return [t.replace(_MARK, "-"), _JOINED.sub("", t).replace(_MARK, "-"), _JOINED.sub("-", t).replace(_MARK, "-")]


def _occurs(value: str, texts: list[str]) -> bool:
    """The normalised value is in one of the _forms of the cited lines."""
    v = _norm(value)
    return bool(v) and any(v in t for t in _forms(texts))


def _is_token(value: str, texts: list[str]) -> bool:
    """The normalised value is a whole token of the cited lines: of their forms with the wrap hyphens joined or kept
    when one is followed by the next word, of the text as it is when none is (a wrap hyphen is never a token
    boundary: "ZX" and "4417" are not tokens of "Protocol ZX-" / "4417", "ZX-4417" and "ZX4417" are; nor is "ZX" of
    "Protocol ZX-" alone). A value that starts or ends with "-" is none."""
    v = _norm(value)
    if not v or v.startswith("-") or v.endswith("-"):
        return False
    t = _marked(texts)
    forms = [_JOINED.sub("", t), _JOINED.sub("-", t)] if _JOINED.search(t) else [t]
    rx = re.compile(_TOKEN_BEFORE + re.escape(v) + _TOKEN_AFTER)
    return any(rx.search(f) for f in forms)


def _starts_inside_wrap(value: str, cited: list[dict], lines: list[dict], index: dict[str, int]) -> bool:
    """The value starts at the very beginning of the first cited line, and the line before it (same page and layer)
    ends with a wrap hyphen glued to a word: the value starts inside a wrapped word ("4417" of "4417 Amendment 4"
    after "Protocol ZX-")."""
    n = index[cited[0]["id"]]
    prev = lines[n - 1] if n > 0 else None
    if (prev is None or (prev["page"], prev["layer"]) != (cited[0]["page"], cited[0]["layer"])
            or not _LINE_END_WRAP.search(prev["text"])):
        return False
    v = _norm(value)
    forms = _forms([x["text"] for x in cited])
    return any(f.startswith(v) for f in forms) and not any(v in f[1:] for f in forms)


def _first_group(pattern: str) -> str:
    """The text of the first capturing group of a regex (r"\\b(NCT\\d{8})(?!\\d)" -> r"NCT\\d{8}")."""
    i, start, depth = 0, None, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if c == "[":  # a character class holds no group
            i += 1
            i += pattern[i:i + 1] == "^"
            i += pattern[i:i + 1] == "]"
            while i < len(pattern) and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
        elif c == "(":
            if start is None and (not pattern.startswith("(?", i) or pattern.startswith("(?P<", i)):
                start = pattern.index(">", i) + 1 if pattern.startswith("(?P<", i) else i + 1
            if start is not None:
                depth += 1
        elif c == ")" and start is not None:
            depth -= 1
            if depth == 0:
                return pattern[start:i]
        i += 1
    raise ValueError("no capturing group")


_REGISTRY_RX: dict[str, re.Pattern] = {}


def _registry_rx(kind: str) -> re.Pattern:
    """The group of chunk_protocol.IDENTIFIER_PATTERNS[kind] as a pattern of its own (the value without its label)."""
    if kind not in _REGISTRY_RX:
        from chunk_protocol import IDENTIFIER_PATTERNS

        rx = IDENTIFIER_PATTERNS[kind]
        _REGISTRY_RX[kind] = re.compile(_first_group(rx.pattern), rx.flags)
    return _REGISTRY_RX[kind]


def _registry_kind(value: str) -> str | None:
    """The registry whose pattern the whole value matches ("NCT01234567", "2019-003431-33", "119006"), or None."""
    v = value.translate(_TO_HYPHEN)
    return next((k for k in REGISTRY_FIELDS if _registry_rx(k).fullmatch(v)), None)


def _printed_as_ind(value: str, texts: list[str]) -> bool:
    """The cited lines print the value as an IND number ("IND Number: 119006", "US IND No. 140,453")."""
    from chunk_protocol import IDENTIFIER_PATTERNS

    joined = " ".join(" ".join(t.split()) for t in texts)
    return any(m.group(1) == value for m in IDENTIFIER_PATTERNS["ind"].finditer(joined))


def _starts_with_label(value: str) -> bool:
    """The value starts with a cover-page label ("Sponsor Name: ...", "Protocol Title: ...")."""
    from identity import _COVER_RX

    return any(rx.match(value) for _, rx in _COVER_RX)


def _shape_problem(field: str, value: str, texts: list[str]) -> str | None:
    """Why `value` cannot be this field (its shape, or not a whole token of the cited lines), or None."""
    if field in ("official_title", "brief_title"):
        if _starts_with_label(value):
            return "starts with a label"
        return None if len(value.split()) >= 3 and len(value) <= 600 else "not a title"
    if field == "acronym":
        if re.search(r"\s", value) or not 2 <= len(value) <= 20 or not re.search(r"[^\W\d_]", value):
            return "not an acronym"
        return None if _is_token(value, texts) else "not a token"
    if field == "sponsor":
        if _starts_with_label(value):
            return "starts with a label"
        if len(value) > 120 or not re.search(r"[^\W\d_]", value) or re.search(r",\s*\d|\d{4,}", value):
            return "not a sponsor name"  # an address part: "Bayer AG, 51368 Leverkusen"
        return None
    if field == "sponsor_protocol_id":
        if len(value) > 40 or not re.search(r"\d", value):
            return "not a protocol id"
        if _registry_kind(value) not in (None, "ind") or _printed_as_ind(value, texts):
            return "a registry id"  # a bare 5-6 digit number is an IND number only after an "IND" label
        return None if _is_token(value, texts) else "not a token"
    if field in REGISTRY_FIELDS:
        if not _registry_rx(field).fullmatch(value):
            return f"not a valid {field}"
        return None if _is_token(value, texts) else "not a token"
    if field == "amendment":  # the label is searched in the cited lines joined ("Amendment" / "9" on two lines)
        return None if _amendment_in(value, texts) else (
            "not an amendment number" if not _amendment_shape(value) else "no amendment label")
    if field == "version_label":
        v = _norm(value)
        if len(value) > 60 or not _VERSION_WORD.search(v) or not (re.search(r"\d", v) or _DESIGNATED.search(v)):
            return "not a version label"
        return None
    if field == "version_date":
        v = _norm(value)
        if len(value) > 40 or not _YEAR.search(v) or not (_MONTH.search(v) or _NUMERIC_DATE.search(v)) \
                or _registry_kind(value):
            return "not a date"
        return None
    return None


def _amendment_shape(value: str) -> bool:
    """1-8 letters, digits and "."; a digit in it, or one letter, or a roman numeral (i to xx)."""
    return bool(re.fullmatch(r"(?:[^\W_]|\.){1,8}", value)
                and (re.search(r"\d", value) or _LETTER_OR_ROMAN.fullmatch(value)))


def _amendment_in(value: str, texts: list[str]) -> bool:
    """value has the amendment shape and is printed as the designator of "Amendment" / "Amended" (the whole word) in
    the cited lines: a value with a digit within 30 characters after the word ("AMENDED CLINICAL TRIAL PROTOCOL
    05"); a letter or roman numeral right after it, after "No." / "Nr." / "Number" / ":" / "#" / "(" and spaces at
    most ("Amendment (a)", "Amendment No. IV"; not the "a" of "Amendment 4 to a protocol")."""
    if not _amendment_shape(value):
        return False
    between = r".{0,30}?" if re.search(r"\d", value) else _DESIGNATOR_LEAD
    rx = re.compile(_AMENDMENT_WORD + between + _NUMBER_BEFORE + re.escape(_norm(value)) + _NUMBER_AFTER)
    return any(rx.search(t) for t in _forms(texts))


def _designator_of(label: str) -> str | None:
    """The amendment designator a version label holds ("Amendment 3.1 (US)" -> "3.1", "Amendment (a)" -> "a",
    "AMENDED CLINICAL TRIAL PROTOCOL 05" -> "05"): its first word-like part the amendment rule accepts in it."""
    for m in re.finditer(r"[^\W_](?:[^\W_]|\.(?=[^\W_]))*", label):
        if _amendment_in(m.group(), [label]):
            return m.group()
    return None


def _cited(ids: list[str], lines: list[dict], index: dict[str, int]) -> tuple[list[dict] | None, str | None]:
    """(the cited lines in id order, None) or (None, why they cannot be used)."""
    if any(i not in index for i in ids):
        return None, "no such line"
    pos = sorted({index[i] for i in ids})
    if not pos:
        return None, "no lines cited"
    if len(pos) > MAX_LINES_PER_FIELD:
        return None, "too many lines"
    if pos[-1] - pos[0] != len(pos) - 1:
        return None, "lines not consecutive"
    cited = [lines[p] for p in pos]
    if len({x["layer"] for x in cited}) != 1:
        return None, "lines in several layers"
    if len(cited) > 1 and cited[0]["layer"] == "furniture":  # distinct headers/footers: never one wrapped value
        return None, "furniture lines cited together"
    if len({x["page"] for x in cited}) != 1:
        return None, "lines not on one page"
    return cited, None


def verify(answer: dict, lines: list[dict], record: dict, section_of: Callable[[str], str | None]) -> dict:
    """Check the model's answer in the lines it cites: {"fields": {field: Field} (the accepted ones, as they go into
    the record), "accepted": [field], "confirmed": [field], "kept_rules": [{"field", "reason"}]} (see the module
    docstring). Only MODEL_FIELDS are read; a field answered null (or empty) is not listed and keeps its rule
    value."""
    from identity import MODEL_METHOD

    index = {x["id"]: n for n, x in enumerate(lines)}
    given = answer.get("fields") if isinstance(answer, dict) else None
    given = given if isinstance(given, dict) else {}
    out: dict[str, Any] = {"fields": {}, "accepted": [], "confirmed": [], "kept_rules": []}

    def keep(field: str, reason: str) -> None:
        out["kept_rules"].append({"field": field, "reason": reason})

    for field in MODEL_FIELDS:
        entry = given.get(field)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            keep(field, "malformed answer")
            continue
        value = entry.get("value")
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        ids = entry.get("lines")
        ids = [ids] if isinstance(ids, str) else ids
        if not isinstance(value, str) or not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            keep(field, "malformed answer")
            continue
        value = " ".join(value.split())
        if field in REGISTRY_FIELDS:  # checked and stored with en dashes etc. as "-": "2019-003431-33"
            value = value.translate(_TO_HYPHEN)
        cited, why = _cited([i.strip() for i in ids], lines, index)
        if cited is None:
            keep(field, why)
            continue
        texts = [x["text"] for x in cited]
        if not _occurs(value, texts):
            keep(field, "not in the cited lines")
            continue
        if (why := _shape_problem(field, value, texts)) is not None:
            keep(field, why)
            continue
        if _starts_inside_wrap(value, cited, lines, index):
            keep(field, "not a token" if field in _TOKEN_FIELDS else "starts inside a wrapped word")
            continue
        rule = record["fields"].get(field) or {}
        if _norm(value) in {_norm(rule.get(k)) for k in ("value", "verbatim") if rule.get(k)}:
            out["confirmed"].append(field)
            continue
        if field in REGISTRY_FIELDS and rule.get("value"):
            keep(field, "registry id: the rule value is kept")
            continue
        first = cited[0]
        source = {"page": first["page"], "layer": first["layer"], "text": " / ".join(texts),
                  "ref": first["ref"], "refs": list(dict.fromkeys(x["ref"] for x in cited)), "row": first["row"],
                  "section_id": section_of(first["ref"]) if first["layer"] != "furniture" else None}
        chosen: dict[str, Any] = {"value": value, "method": MODEL_METHOD, "source": source}
        if field == "sponsor":
            chosen["verbatim"] = value
        chosen["rules"] = {"value": rule.get("value"), "method": rule.get("method")}
        out["fields"][field] = chosen
        out["accepted"].append(field)
    _derive_amendment(out, record)
    return out


_TOKEN_FIELDS = ("acronym", "sponsor_protocol_id", *REGISTRY_FIELDS)


def _derive_amendment(out: dict, record: dict) -> None:
    """A version label accepted without its amendment: the label's designator ("Amendment 3.1 (US)" -> "3.1") becomes
    the amendment, with the label's source and the replaced rule field under "rules", unless it is the rules'
    amendment already (or the label has none). The amendment's lists then say the outcome: "accepted" only."""
    from identity import MODEL_METHOD

    label = out["fields"].get("version_label")
    if label is None or "amendment" in out["accepted"]:
        return
    designator = _designator_of(label["value"])
    rule = record["fields"].get("amendment") or {}

    def number(v: str | None) -> str:  # "05" and "5" are the same amendment
        return _norm(v).lstrip("0") or _norm(v)

    if designator is None or number(designator) == number(rule.get("value")):
        return
    out["fields"]["amendment"] = {"value": designator, "method": MODEL_METHOD, "source": copy.deepcopy(label["source"]),
                                  "rules": {"value": rule.get("value"), "method": rule.get("method")}}
    out["confirmed"] = [f for f in out["confirmed"] if f != "amendment"]
    out["kept_rules"] = [k for k in out["kept_rules"] if k["field"] != "amendment"]
    out["accepted"] = [f for f in MODEL_FIELDS if f in out["accepted"] or f == "amendment"]
    out["fields"] = {f: out["fields"][f] for f in out["accepted"]}


# =========================================================================== one document

def _cache_key(model: str, lines: list[dict], record: dict) -> str:
    data = [model, PROMPT_VERSION, [[x["id"], x["page"], x["layer"], x["text"]] for x in lines], _rule_values(record)]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def _read_entry(path: Path) -> dict | None:
    """A cache entry, or None when it is absent or not one (a broken entry is asked again)."""
    if not path.is_file():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(entry, dict) and isinstance(entry.get("answer"), dict) \
        and isinstance(entry["answer"].get("fields"), dict)
    return entry if ok else None


def _write_entry(path: Path, entry: dict) -> None:
    """Store a cache entry: a temporary file in the cache folder ("<doc_key>.<random>.tmp", so a crash leftover is
    found by the document key), then moved into place (the temporary file removed on failure). Best effort: a
    failure is logged (its type only) and the answer is used all the same."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{entry['doc_key']}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(entry, indent=1, ensure_ascii=False))
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except Exception as exc:  # noqa: BLE001 - the cache is an optimisation
        log.warning("identity cache entry not written (%s at %s)", type(exc).__name__, _where(exc))


def refine(doc: DoclingDocument, record: dict, *, model: str, cache_dir: str | Path,
           section_of: Callable[[str], str | None], post: Callable | None = None) -> tuple[dict, dict]:
    """(a copy of record with the accepted fields replaced, llm): llm = {"accepted", "confirmed", "kept_rules",
    "cache": "hit" | "miss", "lines": the number of lines, "usage": the request's usage block}.

    The answer comes from cache_dir/<sha256 of (model, PROMPT_VERSION, the lines, the rule values)>.json when it is
    there (nothing sent: works offline), else from ask_model (one request, retried on a rate limit, a server or a
    transport error), then stored there when it can be ({"model", "prompt_version", "doc_key", "answer", "usage"};
    written to a temporary file, then moved into place; a failure is only logged). Cached answers are verified again
    on every call. Raises IdentityModelError, retrieve.OfflineRefused (a miss while OPENROUTER_OFFLINE is set) and
    the request's other errors."""
    from figure_vlm import document_key

    _model_name(model)  # an unsupported model never reaches the cache
    lines = select_lines(doc)
    path = Path(cache_dir) / f"{_cache_key(model, lines, record)}.json"
    entry = _read_entry(path)
    cache = "hit" if entry is not None else "miss"
    if entry is None:
        got = ask_model(build_messages(lines, record), model, post=post)
        entry = {"model": model, "prompt_version": PROMPT_VERSION, "doc_key": document_key(doc),
                 "answer": got["answer"], "usage": got["usage"]}
        _write_entry(path, entry)
    checked = verify(entry["answer"], lines, record, section_of)
    out = copy.deepcopy(record)
    out["fields"].update(checked["fields"])
    usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
    llm = {"accepted": checked["accepted"], "confirmed": checked["confirmed"], "kept_rules": checked["kept_rules"],
           "cache": cache, "lines": len(lines), "usage": usage}
    return out, llm


def _where(exc: BaseException) -> str:
    """file:line of the innermost frame of this project's code in the exception's traceback (never its message)."""
    root, where = Path(__file__).resolve().parent, "?"
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            rel = Path(frame.filename).resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if rel.parts and rel.parts[0] != ".venv":
            where = f"{rel.as_posix()}:{frame.lineno}"
    return where


def run_identity(out_dir: str | Path, *, model: str, cache_dir: str | Path, post: Callable | None = None,
                 dest_dir: str | Path | None = None) -> dict:
    """Write identity.json for the converted protocol in out_dir (ProtocolDocument.open: the folder or its chunks
    folder) into dest_dir (default: its chunks folder) and return the stage result {"status", "model", "accepted",
    "confirmed", "kept_rules", "cache"} (the three counts):
      * model empty: the rules record; status "done", model None, counts 0, cache None;
      * else the record refine() gives, stored with model, PROMPT_VERSION and llm; status "done", the model, the
        counts, cache "hit" | "miss";
      * offline with no cache entry (retrieve.OfflineRefused): the rules record; status "skipped_offline";
      * any other error of the model step: the rules record; status "partial", "error": the exception type (and
        "reason": "unsupported model" for a model that is not "openrouter:<id>");
      * stats.json without the Docling JSON's source_sha256 (an older output): nothing written (a record without it
        could never be loaded) and nothing asked; status "partial", "reason": "no source hash".
    In the last three the model is the one asked for and the counts are 0. Records are written with the stats.json
    "document" block they were built from (identity.write_record's meta). An error while opening the document or
    writing the record propagates. Logs: status, counts and field names only."""
    from identity import write_record
    from protocol_api import ProtocolDocument
    from retrieve import OfflineRefused

    pd = ProtocolDocument.open(out_dir)
    model = (model or "").strip()
    result: dict[str, Any] = {"status": "done", "model": model or None, "accepted": 0, "confirmed": 0,
                              "kept_rules": 0, "cache": None}
    if not pd.source_sha256:
        result.update(status="partial", reason="no source hash")
        log.warning("identity: stats.json has no source_sha256; no identity record is written")
        return result
    record = pd.rules_identity_record()
    dest = Path(dest_dir) if dest_dir is not None else pd.chunks_dir
    dest.mkdir(parents=True, exist_ok=True)
    refined = llm = None
    if model:
        try:
            refined, llm = refine(pd.doc, record, model=model, cache_dir=cache_dir, section_of=pd._section_of,
                                  post=post)
        except OfflineRefused:
            result["status"] = "skipped_offline"
        except Exception as exc:  # noqa: BLE001 - the stage falls back to the rules record
            result.update(status="partial", error=type(exc).__name__)
            if isinstance(exc, IdentityModelError) and str(exc) == "unsupported model":
                result["reason"] = "unsupported model"
            log.warning("identity model step failed (%s at %s); the rules record is written", type(exc).__name__,
                        _where(exc))
    if refined is not None:
        write_record(dest, refined, source_sha256=pd.source_sha256, meta=pd.meta, model=model,
                     prompt_version=PROMPT_VERSION, llm=llm)
        result.update(accepted=len(llm["accepted"]), confirmed=len(llm["confirmed"]),
                      kept_rules=len(llm["kept_rules"]), cache=llm["cache"])
        log.info("identity %s: model %s, accepted %s, confirmed %s, kept rules %s, cache %s", result["status"], model,
                 llm["accepted"], llm["confirmed"], [k["field"] for k in llm["kept_rules"]], llm["cache"])
    else:
        write_record(dest, record, source_sha256=pd.source_sha256, meta=pd.meta)
        log.info("identity %s: rules record (model %s)", result["status"], model or None)
    return result


# =========================================================================== command line

def main(argv: list[str] | None = None) -> int:
    from identity import RECORD_FILE
    from pipeline_version import identity_model

    ap = argparse.ArgumentParser(description="Write chunks/identity.json: the identity record of a converted "
                                             "protocol, with the values a model chose verified in the cited lines.")
    ap.add_argument("--chunks", type=Path, required=True, help="the document's chunks folder")
    ap.add_argument("--model", default=None,
                    help='"openrouter:<model id>"; default: IDENTITY_MODEL or the pipeline default; "" = rules only')
    ap.add_argument("--cache", type=Path, default=None, help="answer cache folder (default: <document>/identity_cache)")
    ap.add_argument("--dest", type=Path, default=None, help="where identity.json is written (default: --chunks)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    chunks = args.chunks if (args.chunks / "stats.json").is_file() else args.chunks / "chunks"
    model = identity_model() if args.model is None else args.model.strip()
    try:
        res = run_identity(chunks, model=model, cache_dir=args.cache or chunks.parent / "identity_cache",
                           dest_dir=args.dest)
    except Exception as exc:  # noqa: BLE001 - the type and where only (a message can quote the document)
        print(f"identity: failed ({type(exc).__name__} at {_where(exc)})")
        return 1
    names = {"accepted": [], "confirmed": [], "kept_rules": []}
    if res["status"] == "done" and res["model"]:
        llm = json.loads(((args.dest or chunks) / RECORD_FILE).read_text(encoding="utf-8")).get("llm") or {}
        names = {"accepted": llm.get("accepted") or [], "confirmed": llm.get("confirmed") or [],
                 "kept_rules": [k["field"] for k in llm.get("kept_rules") or []]}
    counts = ", ".join(f"{k} {res[k]} [{' '.join(names[k])}]" for k in ("accepted", "confirmed", "kept_rules"))
    extra = "".join(f", {k} {res[k]}" for k in ("error", "reason") if res.get(k))
    print(f"identity: {res['status']}, model {res['model'] or 'none'}, {counts}, cache {res['cache'] or 'none'}{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
