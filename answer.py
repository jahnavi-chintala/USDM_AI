"""Grounded answering / extraction over one protocol, with deterministic verification.

The consumer is an agent that builds USDM 4 data, so every statement must be traceable to verbatim
protocol text, and anything that cannot be traced must say so. Design: reports/audit/retrieval_generation.md
§6 and reports/pipeline_audit_plan.md §3.12.

  answer(question, out_dir="output", model=..., k=PRODUCTION_K, client=None) -> dict
      retrieval exactly as production (retrieve.production_builder: parent-aware context, top-8 reranked
      children, 12K budget), one structured LLM call, deterministic verification, at most one corrective
      retry, a final status.
  GroundedAnswerer(...).run(question, context)
      the same on an already built retrieve.Context (protocol_api / eval use).
  env_factory(doc, chunks_path, args)
      eval_v1 integration: `eval_v1.py run ... --k 8 --env-factory answer:env_factory --answers`.
  python answer.py OUT_DIR "question" [--model M] [-k K] [--verify]
      prints the result JSON; refuses (exit 3) under OPENROUTER_OFFLINE=1 when anything is uncached.

Answer, then verify (answer_then_verify, AnswerVerifier, env_factory_verify[_strict], verify-report)
  The claim-first mode above makes answers terse (judge fact recall -2.8 pts on MAIN, -5.2 on public;
  reports/pipeline_audit_plan.md §7). This mode keeps the production answer and verifies it afterwards:
  1. the production prose answer, produced exactly as eval_answers.answer() (same model, system prompt,
     user message, cache <chunks>/answer_cache.json and key): already cached for the production eval runs;
     never rewritten;
  2. one verification call (ZDR routing, temperature 0, strict JSON schema): the answer's factual
     statements in its own wording, each "quoted" (verbatim quotes), "derived" (inputs + arithmetic) or
     "unsupported", plus conflicts between blocks about the asked facts; cached in
     <chunks>/answer_verify_cache.json keyed by (model, verify prompt version + hash, context hash,
     question, prose hash);
  3. deterministic checks with verify() above: a statement must be in the prose (>= 0.9 of its tokens in
     order in one place, with no number, comparator or negation added, dropped or changed; else dropped as
     invented by the verifier); its quotes must be in its blocks; its numbers, comparators and units must be
     in its quotes (derived: inputs quoted, arithmetic recomputed). Per statement: verified | derived_ok |
     unverified (with issues) | unsupported (the verifier's verdict).
  Result {"status", "answer" (the prose, unchanged), "statements", "conflicts", "dropped_statements",
  "verification" {statements_total, verified, derived, unverified, unsupported, dropped, quotes_ok,
  quotes_failed, numbers_unverified, numbers_outside_statements, ...}, "annotated" (the prose with
  "⚠[unverified]" / "⚠[unsupported]" after flagged statements), "answer_strict" (flagged statements cut)}.
  Status: not_found when the prose abstains (eval_v1.abstained) and no statement verifies; conflict when a
  verifier conflict has all its quotes verified (two distinct protocol-text versions); partial when a
  statement is unverified/unsupported, a prose number outside every statement is unverified, or the prose
  abstains in part; else answered.
  eval_v1: --env-factory answer:env_factory_verify scores the unchanged prose (fact recall = production)
  and records each question's verification in <chunks>/answer_verify_status.json;
  answer:env_factory_verify_strict scores answer_strict (same caches, no extra call).
  `python answer.py verify-report RUN [--judge-run RUN] [--show-text]`: per-document statistics and, given a
  judge-scored run of the same prose (default j_fin_par_<doc>), the co-occurrence of the flags with the
  judge's contradicted facts and extra claims.

Result
  {"status": "answered" | "partial" | "not_found" | "conflict",
   "answer": prose,
   "claims": [{"text", "quotes": [{"block", "quote", "match", "source", ...}], "derived", "derivation",
               "verified", "issues"}],
   "conflicts": [{"claims", "quotes", "note", "verified"}],
   "values": [{"name", "value", "unit", "comparator", "quote_ref": [claim, quote], "verified", "issues"}],
   "context_blocks": [{"n", "section", "pages", "kind"}],
   "verification": {"quotes_ok", "quotes_failed", "numbers_unverified", "retried", ...},
   "meta": {...}}
  Issues starting with "note:" are informational; every other issue makes its claim/value unverified.

Verification (deterministic, after every model call)
  * Quotes: a quote must occur in its cited block: "exact" (character for character, whitespace
    collapsed), "normalised" (keyterm_match.normalize: NFKC, case, dash/quote variants, "^", "×",
    thousands separators, powers of ten; then also without spaces and without hyphens between letters;
    digits never merge), "elided" ("…" between parts of one passage, in order, gaps <= 300 characters) or
    "fuzzy" (character similarity >= 0.95 to a block window, and the differences touch no digit,
    comparator, sign or meaningful word: only punctuation, a filler word, or a one-letter typo inside a
    word of 5+ letters; recorded as "fuzzy", with the block's text). A quote not in its block but in
    another one is re-cited to that block ("note: re-cited"). Shorter than 6 characters: rejected.
  * Figures: text under "Diagram (Mermaid, AI-generated)", "Description (AI-generated by ...)", after
    "*Figure description generated by ...*" and inside ```mermaid fences is AI-generated; a claim whose
    verified quotes all lie in such text is not verified.
  * Numbers: every number in a claim must occur in that claim's quotes, with the same comparator
    (≥ ≤ > < ±, symbols or words such as "at least", "no more than", "or older"; a quote's symbol
    comparator may not be dropped) and, when the claim gives a unit after it, that unit in the quotes
    (day/days/d, mg vs mg/kg, ×10^9/L ...). Derived claims ("derived": true) must give a derivation whose
    inputs are in the quotes; a pure arithmetic derivation ("123 + 64 = 187") is recomputed. Values are
    checked the same way against the quote they reference. Numbers in the prose answer must occur in a
    verified quote (same symbol comparator), be a derived result, or come from the question / a cited
    block's section label; the others are listed in verification.numbers_unverified.
  * Any other failure is critical: the model is called once more with the failures listed and must fix or
    drop the claim. The better attempt (fewer critical failures; the retry on a tie) is kept. "answered" is
    downgraded to "partial" when unverified claims/values or prose numbers remain; "conflict" needs a
    conflict whose claims are all verified.

API and data: one chat completion per question (+1 retry when verification fails) through OpenRouter with
provider {"data_collection": "deny", "zdr": true, "allow_fallbacks": false}; temperature 0 and a strict
json_schema response format, each dropped once if the provider rejects it with HTTP 400. Responses are
cached in <chunks>/answer_v2_cache.json, keyed by model, prompt version (+ prompt/schema hash), context
hash, question and, for the retry, the first response and the feedback. OPENROUTER_OFFLINE=1 (or
allow_api=False) turns a cache miss into OfflineMiss: nothing is sent.
"""

from __future__ import annotations

import argparse
import ast
import atexit
import hashlib
import json
import operator
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from keyterm_match import normalize as _kt_normalize
from retrieve import PRODUCTION_BUDGET, PRODUCTION_K

DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
PROMPT_VERSION = "answer-v2.0"
CACHE_NAME = "answer_v2_cache.json"
STATUS_NAME = "answer_v2_status.json"
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
PROVIDER = {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
STATUSES = ("answered", "partial", "not_found", "conflict")
MAX_TOKENS = 16000
EMPTY_ATTEMPTS = 3  # an empty reply is re-requested (never cached); not the verification retry
MIN_QUOTE_CHARS = 6  # compact characters
FUZZY_MIN_CHARS = 20
FUZZY_THRESHOLD = 0.95
ELIDED_MAX_GAP = 300

LOG_PREFIX = "answer_v2"


class OfflineMiss(RuntimeError):
    """A call is needed but the API is not allowed (OPENROUTER_OFFLINE=1 or allow_api=False)."""


# =========================================================================== prompt

SYSTEM = """You answer questions about one clinical trial protocol for an agent that builds USDM 4 study data. Use ONLY the numbered context blocks; never outside knowledge. Reply with one JSON object that follows the schema.

Grounding rules
1. Claims. Split the answer into claims: atomic statements (one criterion, one value, one schedule item, one definition). Every claim needs at least one quote {"block": n, "quote": "..."} copied character for character from block [n]: a contiguous span such as a sentence, a list item, a table row or a cell with its row label. Do not paraphrase, correct, reorder or add words inside a quote. Use "…" only to skip words within one sentence or table row, never to join different places. The header line of a block ("[n] type | section | pages") is not quotable. Keep quotes short but sufficient on their own (usually 5-60 words).
2. Numbers. Copy numbers, units and comparators exactly as the quote shows them, in claims, values and the answer: "≥" and ">" differ, "≤" and "<" differ; keep notations such as ×10^9/L, mg/m2, ±3 days and visit windows; never convert units, round or reformat. Every number in a claim must appear in that claim's quotes with the same comparator and unit. For a table cell, quote the row (label and cells, as written in the block) and, as a second quote, the header text that names the column (e.g. the visit or week). Do not put section or page numbers in claim text.
3. Derived values. If you compute anything (a sum, count, difference, duration), make it its own claim with "derived": true and "derivation" showing the inputs and the arithmetic, e.g. "123 + 64 = 187" or "count of exclusion criteria listed = 12", and quote every input. Otherwise "derived": false and "derivation": "".
4. AI-generated figure text. Text under "Diagram (Mermaid, AI-generated)" or "Description (AI-generated by …)", after "*Figure description generated by …*", and Mermaid code was written by an AI model from a figure image: it is not protocol text. "Text in figure (OCR)" is text read from the figure itself. A claim must not rest on AI-generated text alone; support it with protocol or OCR text, or leave it out (if you keep it, write "(AI-generated figure description only)" in the claim text).
5. Versions and history. Report current requirements. Amendment histories, summary-of-changes tables and document history describe past changes; use them only when the question asks about amendments or changes. Text marked "Was:" or "Removed:" is superseded.
6. Conflicts. If blocks disagree about the asked fact (e.g. synopsis vs body, "<400" vs "≤400", ">" vs "≥", different doses, timings or windows), set status "conflict", give one claim per version, each with its own quote, and add a conflicts entry with those claim indexes (0-based) and a note on what differs. Never pick one version silently.
7. Status. "answered": the blocks fully answer the question. "partial": they answer only part; the answer says what is missing (also when a needed list or table is shown only in part, e.g. a block labelled "retrieved rows" or "Section part"). "not_found": no block supports an answer; claims and values are empty and the answer says the context does not contain it. A near-miss (a similar but different visit, dose, cohort, test or protocol) is not an answer: report not_found and do not guess. "conflict": see rule 6.
8. Answer. Plain prose, at most a few short paragraphs or a list, consistent with the claims; cite blocks like [3]. It must contain no fact, number or comparator that is not in a claim.
9. Values. For values an agent would store in USDM fields (doses, frequencies, routes, thresholds, durations, visit timings and windows, counts, identifiers, dates, names), add {"name", "value", "unit", "comparator", "quote_ref": [claim index, quote index]} (0-based) with value, unit and comparator copied exactly from that quote; null when absent. Empty list when there are none.
Lists: when the question asks for all items (criteria, visits, activities, arms, objectives), give one claim per item and include every item the blocks contain.
Schedule of activities: an "X" (or similar mark) in an activity's row under a visit column means the activity is done at that visit; read footnote markers and notes; report windows exactly as written."""

SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "quotes": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"block": {"type": "integer"}, "quote": {"type": "string"}},
                    "required": ["block", "quote"], "additionalProperties": False}},
                "derived": {"type": "boolean"},
                "derivation": {"type": "string"}},
            "required": ["text", "quotes", "derived", "derivation"], "additionalProperties": False}},
        "conflicts": {"type": "array", "items": {
            "type": "object",
            "properties": {"claims": {"type": "array", "items": {"type": "integer"}}, "note": {"type": "string"}},
            "required": ["claims", "note"], "additionalProperties": False}},
        "values": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "value": {"type": "string"},
                           "unit": {"type": ["string", "null"]}, "comparator": {"type": ["string", "null"]},
                           "quote_ref": {"type": "array", "items": {"type": "integer"}}},
            "required": ["name", "value", "unit", "comparator", "quote_ref"], "additionalProperties": False}},
        "answer": {"type": "string"},
        "status": {"type": "string", "enum": list(STATUSES)},
    },
    "required": ["claims", "conflicts", "values", "answer", "status"],
    "additionalProperties": False,
}

RETRY_INSTRUCTIONS = (
    "Return the complete corrected JSON object (same schema). For each problem: copy the quote exactly from "
    "the block that contains it (or cite that block), copy numbers, units and comparators exactly as the quote "
    "has them, add a quote that contains a missing number (e.g. the table header naming the column), label "
    "computed values as derived with their inputs, or remove the claim or value and its content from the "
    "answer. Do not add anything you cannot quote. Adjust the status if needed.")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


PROMPT_HASH = sha(SYSTEM + json.dumps(SCHEMA, sort_keys=True) + RETRY_INSTRUCTIONS)[:12]


def user_message(context_text: str, question: str) -> str:
    return (f"Context (numbered blocks; each header gives [n], the block type, its section and pages):\n\n"
            f"{context_text}\n\nQuestion: {question}\n\nReply with the JSON object only.")


# =========================================================================== normalisation and quote matching

def norm(text: str) -> str:
    return _kt_normalize(text or "")


_WS_KEEP_DIGITS = re.compile(r"(?<!\d)\s+|\s+(?!\d)")  # whitespace, except between two digits
_LETTER_HYPHEN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")


def compact(normed: str) -> str:
    """normed without spaces (digits never merge) and without hyphens between letters."""
    return _LETTER_HYPHEN.sub("", _WS_KEEP_DIGITS.sub("", normed))


@dataclass
class _Text:
    raw_ws: str
    norm: str
    comp: str

    @classmethod
    def of(cls, text: str) -> "_Text":
        n = norm(text)
        return cls(" ".join((text or "").split()), n, compact(n))


_AI_LABEL = re.compile(r"^\*?\s*(?:Diagram \(Mermaid, AI-generated\)(?: \(part \d+\))?:|"
                       r"Description \(AI-generated\b.*?\)(?: \(part \d+\))?:|Figure description generated by\b.*)$")
_SEG_END = re.compile(r"^(?:Text in figure\b.*|Description(?: \(part \d+\))?:|Figure[:( ].*|#{1,6} .*|\|.*)$")


def split_ai(text: str) -> tuple[str, bool]:
    """(the text without its AI-generated figure parts, whether it had any). AI parts: a labelled
    Mermaid diagram or AI description up to the next figure part / heading / table, the
    "*Figure description generated by ...*" form, and any ```mermaid fence."""
    out: list[str] = []
    ai = fence = False
    any_ai = False
    for line in (text or "").split("\n"):
        s = line.strip()
        if fence:
            if s.startswith("```"):
                fence = False
                ai = False  # a Mermaid part ends with its fence
            continue
        if s.startswith("```mermaid"):
            fence = any_ai = True
            continue
        if _AI_LABEL.match(s):
            ai = any_ai = True
            continue
        if ai and _SEG_END.match(s):
            ai = False
        if not ai:
            out.append(line)
    return "\n".join(out), any_ai


@dataclass
class VBlock:
    """A numbered context block prepared for verification."""
    n: int
    text: str
    section: str = ""
    pages: list = field(default_factory=list)
    kind: str = "text"

    def __post_init__(self) -> None:
        self.full = _Text.of(self.text)
        human, self.has_ai = split_ai(self.text)
        self.human = _Text.of(human) if self.has_ai else self.full


_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:\.\.\.|…)\s*\]|\.\.\.|…)\s*")
_WRAP = "\"'“”‘’«»„"


def clean_quote(quote: str) -> str:
    q = " ".join(str(quote or "").split())
    for _ in range(2):
        q = q.strip().strip(_WRAP).strip()
        q = re.sub(r"^(?:\.\.\.|…)\s*|\s*(?:\.\.\.|…)$", "", q)
    return q


_FTOK = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+|[^\w\s]")
_CRIT_SYMBOLS = set("≥≤<>±=%~+")
_FILLER = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "by"}


def _lev_le1(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a
    i = 0
    while i < len(a) and a[i] == b[i]:
        i += 1
    return a[i:] == b[i + 1:]


def fuzzy_safe(q: str, w: str) -> bool:
    """The differences between quote q and block window w are harmless: punctuation, a filler word, or
    a one-letter typo inside a word of 5+ letters; never a number, comparator/sign or other word."""
    qt, wt = _FTOK.findall(q), _FTOK.findall(w)

    def digit(tokens: list[str], i: int) -> bool:
        return 0 <= i < len(tokens) and any(ch.isdigit() for ch in tokens[i])

    for tag, i1, i2, j1, j2 in SequenceMatcher(None, qt, wt, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        a, b = qt[i1:i2], wt[j1:j2]
        for tok in a + b:
            if any(ch.isdigit() for ch in tok) or tok in _CRIT_SYMBOLS:
                return False
        # punctuation next to a number is part of it ("-1", "1.5", "1/2")
        if any(not t.isalnum() for t in a + b) and (digit(qt, i1 - 1) or digit(qt, i2) or digit(wt, j1 - 1)
                                                     or digit(wt, j2)):
            return False
        # one table-cell boundary may be skipped, never an empty cell or a row boundary ("| |"):
        # that would shift the cells of a schedule row against its columns
        if a.count("|") > 1 or b.count("|") > 1:
            return False
        if tag == "replace":
            if len(a) == len(b) == 1 and a[0].isalpha() and b[0].isalpha():
                x, y = a[0], b[0]
                if not (min(len(x), len(y)) >= 5 and x[0] == y[0] and _lev_le1(x, y)):
                    return False
            elif any(t.isalnum() for t in a + b):
                return False
        elif any(t.isalnum() and t not in _FILLER for t in a + b):
            return False
    return True


def _fuzzy(qn: str, tn: str) -> list[tuple[float, str]]:
    """Candidate (similarity, block window) pairs of normalised quote qn inside normalised text tn, best
    first: windows of about the quote's token length around the places holding most of its tokens."""
    qtok = _FTOK.findall(qn)
    toks = [(m.group(), m.start(), m.end()) for m in _FTOK.finditer(tn)]
    if len(qtok) < 4 or not toks:
        return []
    need = Counter(qtok)
    width = min(len(toks), len(qtok))
    have: Counter = Counter()
    overlap = 0
    scored: list[tuple[int, int]] = []
    for i, (tok, _, _) in enumerate(toks):
        have[tok] += 1
        if have[tok] <= need[tok]:
            overlap += 1
        if i >= width:
            old = toks[i - width][0]
            if have[old] <= need[old]:
                overlap -= 1
            have[old] -= 1
        if i >= width - 1:
            scored.append((overlap, i - width + 1))
    scored.sort(key=lambda x: -x[0])
    found: dict[str, float] = {}
    for ov, start in scored[:5]:
        if ov < 0.7 * len(qtok):
            break
        for s in range(max(0, start - 2), min(len(toks) - 1, start + 2) + 1):
            for wlen in range(max(1, width - 2), width + 3):
                e = min(len(toks), s + wlen) - 1
                window = tn[toks[s][1]:toks[e][2]]
                if window not in found:
                    found[window] = SequenceMatcher(None, qn, window, autojunk=False).ratio()
    return sorted(((r, w) for w, r in found.items()), key=lambda x: -x[0])


def _find_elided(parts: list[str], t: _Text) -> bool:
    segs = [norm(p) for p in parts]
    if any(len(compact(s)) < 3 for s in segs) or len(compact("".join(segs))) < MIN_QUOTE_CHARS:
        return False
    start = t.norm.find(segs[0])
    tries = 0
    while start >= 0 and tries < 50:
        pos, ok = start + len(segs[0]), True
        for s in segs[1:]:
            nxt = t.norm.find(s, pos)
            if nxt < 0 or nxt - pos > ELIDED_MAX_GAP:
                ok = False
                break
            pos = nxt + len(s)
        if ok:
            return True
        start = t.norm.find(segs[0], start + 1)
        tries += 1
    return False


def match_text(quote: str, t: _Text) -> tuple[str | None, float, str]:
    """(level, similarity, text to check numbers against) of a cleaned quote in a prepared text."""
    parts = [p for p in _ELLIPSIS.split(quote) if p.strip()]
    if len(parts) > 1:
        return ("elided", 1.0, norm(" ".join(parts))) if _find_elided(parts, t) else (None, 0.0, "")
    qn = norm(quote)
    if len(compact(qn)) < MIN_QUOTE_CHARS:
        return None, 0.0, ""
    if " ".join(quote.split()) in t.raw_ws:
        return "exact", 1.0, qn
    if qn in t.norm or compact(qn) in t.comp:
        return "normalised", 1.0, qn
    if len(qn) >= FUZZY_MIN_CHARS:
        for r, window in _fuzzy(qn, t.norm):
            if r < FUZZY_THRESHOLD:
                break
            if fuzzy_safe(qn, window):
                return "fuzzy", round(r, 3), window
    return None, 0.0, ""


def locate(quote: str, block: VBlock) -> tuple[str | None, str, float, str]:
    """(level, source "document" | "ai_generated", similarity, matched text) of a quote in a block."""
    lvl, r, txt = match_text(quote, block.human)
    if lvl:
        return lvl, "document", r, txt
    if block.has_ai:
        lvl, r, txt = match_text(quote, block.full)
        if lvl:
            return lvl, "ai_generated", r, txt
    return None, "", 0.0, ""


# =========================================================================== numbers, comparators, units

_POW = re.compile(r"10e(?:6|9|12)(?!\d)")
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)*(?!\d)")
_SYM_BEFORE = re.compile(r"([≥≤<>±])\s?(?:[^\W\d_][\w\-]*\s)?$")
_PRE_WORDS = sorted([
    ("at least", "≥"), ("no less than", "≥"), ("not less than", "≥"), ("no fewer than", "≥"),
    ("not fewer than", "≥"), ("greater than or equal to", "≥"), ("more than or equal to", "≥"),
    ("equal to or greater than", "≥"), ("equal to or more than", "≥"), ("a minimum of", "≥"),
    ("minimum of", "≥"), ("minimum", "≥"), ("no earlier than", "≥"), ("not earlier than", "≥"),
    ("at most", "≤"), ("no more than", "≤"), ("not more than", "≤"), ("less than or equal to", "≤"),
    ("equal to or less than", "≤"), ("up to", "≤"), ("a maximum of", "≤"), ("maximum of", "≤"),
    ("maximum", "≤"), ("not exceeding", "≤"), ("not to exceed", "≤"), ("not exceed", "≤"),
    ("no later than", "≤"), ("not later than", "≤"), ("within", "≤"),
    ("more than", ">"), ("greater than", ">"), ("above", ">"), ("exceeding", ">"), ("exceeds", ">"),
    ("in excess of", ">"), ("later than", ">"),
    ("less than", "<"), ("fewer than", "<"), ("below", "<"), ("under", "<"), ("lower than", "<"),
    ("earlier than", "<"), ("plus or minus", "±"), ("+/-", "±"),
], key=lambda x: -len(x[0]))
_POST = re.compile(r"[^,;:()\n|]{0,25}?\b(?:or|and) (more|greater|higher|above|older|later|less|fewer|lower|below|"
                   r"younger|under|earlier)\b")
_POST_GE = {"more", "greater", "higher", "above", "older", "later"}
_UNIT_AT = re.compile(r"(\s?)(x ?10e(?:6|9|12)/[a-zμ]+|/?[a-zμ%]+(?:/[a-zμ0-9]+)*)")
_UNIT_ANY = re.compile(r"x ?10e(?:6|9|12)/[a-zμ]+|/?[a-zμ%]+(?:/[a-zμ0-9]+)*")
_UNIT_CANON = {
    **dict.fromkeys(("day", "days", "d"), "day"), **dict.fromkeys(("week", "weeks", "wk", "wks", "w"), "week"),
    **dict.fromkeys(("hour", "hours", "h", "hr", "hrs"), "hour"),
    **dict.fromkeys(("minute", "minutes", "min", "mins"), "minute"),
    **dict.fromkeys(("month", "months", "mo"), "month"), **dict.fromkeys(("year", "years", "yr", "yrs", "y"), "year"),
    **dict.fromkeys(("second", "seconds", "sec", "secs", "s"), "second"),
    **dict.fromkeys(("mcg", "ug", "μg"), "μg"), **dict.fromkeys(("ul", "μl"), "μl"),
    **dict.fromkeys(("umol", "μmol"), "μmol"), **dict.fromkeys(("unit", "units", "u"), "unit"),
    **dict.fromkeys(("cycle", "cycles"), "cycle"), **dict.fromkeys(("dose", "doses"), "dose"),
    **dict.fromkeys(("msec", "ms"), "ms"),
    **{u: u for u in ("mg", "g", "kg", "ng", "pg", "ml", "l", "dl", "mmol", "mol", "iu", "meq", "mm", "cm", "m",
                      "cm2", "m2", "mmhg", "bpm", "%", "cells", "copies", "kcal", "mosm", "mbq", "gy", "cgy")},
}
_SINGLE_LETTER_UNITS = {"d", "h", "w", "y", "s", "m", "g", "l", "u"}


def _canon_unit(tok: str, attached: bool) -> str | None:
    tok = tok.strip()
    if tok.startswith("x") and "10e" in tok:
        return tok.replace(" ", "")
    lead = "/" if tok.startswith("/") else ""
    parts = tok.lstrip("/").split("/")
    canon = []
    for i, p in enumerate(parts):
        c = _UNIT_CANON.get(p)
        if c is None:
            return None
        if i == 0 and not lead and len(parts) == 1 and len(p) == 1 and not attached and p in _SINGLE_LETTER_UNITS:
            return None  # "3 d" is too ambiguous; "3d" is fine
        canon.append(c)
    return lead + "/".join(canon)


@dataclass
class Mention:
    value: str
    cmp: str | None
    cmp_src: str | None  # "sym" | "pre" | "post"
    unit: str | None

    def show(self) -> str:
        return f"{self.cmp or ''}{self.value}{' ' + self.unit if self.unit else ''}"


def _cmp_before(t: str, s: int) -> tuple[str | None, str | None]:
    head = t[max(0, s - 40):s]
    m = _SYM_BEFORE.search(head[-18:])
    if m:
        return m.group(1), "sym"
    h = head.rstrip(" (")
    for phrase, cls in _PRE_WORDS:
        if h.endswith(phrase) and (len(h) == len(phrase) or not h[-len(phrase) - 1].isalnum()):
            return cls, "pre"
    return None, None


_AREA_EXP = re.compile(r"\b(k?m|cm|mm)\s([23])(?![\d.,])")  # flattened superscript: "kg/m 2" -> "kg/m2"
_AREA_UNIT = re.compile(r"\b(k?m|cm|mm)[23](?![\d.,])")


def mentions(text: str, normalized: bool = False) -> list[Mention]:
    """Numbers in a text with the comparator and unit attached to each (powers of ten are units)."""
    t = _AREA_EXP.sub(r"\1\2", text if normalized else norm(text))
    masked = _POW.sub(lambda m: "#" * len(m.group()), t)
    masked = _AREA_UNIT.sub(lambda m: m.group(1) + "#", masked)  # the 2 of m2 is not a number
    out = []
    for m in _NUMBER.finditer(masked):
        s, e = m.span()
        cmp_, src = _cmp_before(t, s)
        if cmp_ is None:
            pm = _POST.match(t, e)
            if pm:
                cmp_, src = ("≥" if pm.group(1) in _POST_GE else "≤"), "post"
        um = _UNIT_AT.match(t, e)
        unit = _canon_unit(um.group(2), um.group(1) == "") if um else None
        out.append(Mention(m.group(), cmp_, src, unit))
    return out


def units_in(text_norm: str) -> set[str]:
    out = set()
    for m in _UNIT_ANY.finditer(_AREA_EXP.sub(r"\1\2", text_norm)):
        c = _canon_unit(m.group(), attached=True)
        if c:
            out.add(c)
    return out


_CITE = re.compile(r"\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\]")
_BLOCK_REF = re.compile(r"(?i)\bblocks?\s+\d+(?:\s*(?:,|and|-)\s*\d+)*")
_ENUM = re.compile(r"(?m)^\s*(?:\d{1,2}[.)]|[-*•·▪])\s+")
# Cross-references ("Table 7", "Section 8.3.8", "Appendix 2"): allowed when a cited block contains them.
_XREF = re.compile(r"\b(?:table|figure|section|appendix|attachment|annex|amendment|schedule)\s+"
                   r"[a-z]{0,6}[.-]?\d+(?:\.\d+)*[a-z]?\b")


def strip_refs(text: str) -> str:
    return _ENUM.sub(" ", _BLOCK_REF.sub(" ", _CITE.sub(" ", text or "")))


def text_mentions(text: str, haystacks: list[str]) -> list[Mention]:
    """mentions() of a claim/answer text without citations, enumerators and cross-references that one of
    the (normalised) haystacks contains."""
    t = norm(strip_refs(text))
    t = _XREF.sub(lambda m: " " if any(m.group() in h for h in haystacks) else m.group(), t)
    return mentions(t, normalized=True)


def check_mentions(found: list[Mention], quote_norms: list[str], allowed: set[str] = frozenset()) -> list[str]:
    """Issues for numbers of a claim (or value) against its quotes."""
    occ_all = [m for q in quote_norms for m in mentions(q, normalized=True)]
    qunits = set().union(*(units_in(q) for q in quote_norms)) if quote_norms else set()
    issues = []
    for cm in found:
        occ = [o for o in occ_all if o.value == cm.value]
        if not occ:
            if cm.value not in allowed:
                issues.append(f"number {cm.show()} is not in the claim's quotes")
            continue
        if cm.cmp:
            same = [o for o in occ if o.cmp == cm.cmp]
            if not same:
                other = sorted({o.cmp for o in occ if o.cmp})
                if other:
                    issues.append(f"comparator mismatch: the claim has {cm.cmp}{cm.value}, the quote has "
                                  + " / ".join(f"{c}{cm.value}" for c in other))
                else:
                    issues.append(f"comparator {cm.cmp} before {cm.value} is not in the quote")
        elif occ and all(o.cmp and o.cmp_src != "post" for o in occ):
            issues.append(f"comparator dropped: the quote has {occ[0].cmp}{cm.value}, the claim has {cm.value}")
        if cm.unit and cm.unit not in qunits:
            issues.append(f"unit {cm.unit!r} of {cm.value} is not in the quote (quote units: "
                          f"{', '.join(sorted(qunits)) or 'none'})")
    return list(dict.fromkeys(issues))


_ARITH_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def _eval_arith(expr: str) -> float | None:
    expr = re.sub(r"(?<=[\d)\s])x(?=[\s\d(])", "*", expr).replace("÷", "/")
    if not re.fullmatch(r"[\d\s.+\-*/()]+", expr or "") or not re.search(r"\d", expr):
        return None
    try:
        node = ast.parse(expr.strip(), mode="eval").body
    except SyntaxError:
        return None

    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in _ARITH_OPS:
            return _ARITH_OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.USub):
            return -ev(n.operand)
        raise ValueError

    try:
        return ev(node)
    except (ValueError, ZeroDivisionError):
        return None


def check_derivation(derivation: str, quote_norms: list[str]) -> tuple[set[str], set[str], list[str]]:
    """(input numbers, result numbers, issues) of a derived claim's derivation."""
    t = norm(strip_refs(derivation))
    if not t:
        return set(), set(), ["derived claim without a derivation (inputs and arithmetic)"]
    lhs, sep, rhs = t.rpartition("=")
    if not sep:
        lhs, rhs = t, ""
    inputs = {m.value for m in mentions(lhs, normalized=True)}
    results = {m.value for m in mentions(rhs, normalized=True)}
    have = {m.value for q in quote_norms for m in mentions(q, normalized=True)}
    issues = [f"derivation input {v} is not in the claim's quotes" for v in sorted(inputs - have)]
    if sep and len(results) == 1:
        got = _eval_arith(_POW.sub("", lhs))
        if got is not None:
            want = float(next(iter(results)))
            decimals = len(next(iter(results)).partition(".")[2])
            if round(got, decimals) != want and abs(got - want) > 1e-9 * max(1.0, abs(want)):
                issues.append(f"derivation arithmetic is wrong: {lhs.strip()} = {got:g}, not {want:g}")
    return inputs, results, issues


# =========================================================================== parsing the model output

def parse_json(content: str) -> Any:
    """The first JSON object in a reply (code fences and preambles tolerated)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", (content or "").strip())
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            return dec.raw_decode(text[m.start():])[0]
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object in the model output")


def _int(x: Any) -> int | None:
    if isinstance(x, bool):
        return None
    if isinstance(x, int):
        return x
    m = re.fullmatch(r"\s*\[?\s*(\d+)\s*\]?\s*", str(x)) if isinstance(x, (str, float)) else None
    return int(m.group(1)) if m else None


def _opt(x: Any, drop: tuple = ()) -> str | None:
    s = "" if x is None else str(x).strip()
    return None if s.lower() in ("", "null", "none", "n/a", *drop) else s


def coerce(obj: Any) -> tuple[dict | None, list[str]]:
    """The model object in canonical shape, or None; plus schema problems (all critical)."""
    if not isinstance(obj, dict):
        return None, ["the reply is not a JSON object"]
    problems = []
    status = str(obj.get("status") or "").strip().lower().replace(" ", "_").replace("-", "_")
    if status not in STATUSES:
        problems.append(f"status {obj.get('status')!r} is not one of {', '.join(STATUSES)}")
        status = "partial"
    claims = []
    for i, c in enumerate(obj.get("claims") or []):
        if not isinstance(c, dict):
            problems.append(f"claim {i} is not an object")
            continue
        quotes = []
        for q in c.get("quotes") or []:
            if isinstance(q, dict):
                quotes.append({"block": _int(q.get("block")), "quote": str(q.get("quote") or "")})
        claims.append({"text": str(c.get("text") or "").strip(), "quotes": quotes,
                       "derived": bool(c.get("derived")), "derivation": str(c.get("derivation") or "").strip()})
    conflicts = []
    for cf in obj.get("conflicts") or []:
        if isinstance(cf, dict):
            idx = [i for i in (_int(x) for x in cf.get("claims") or []) if i is not None]
            conflicts.append({"claims": idx, "note": str(cf.get("note") or "").strip()})
    values = []
    for v in obj.get("values") or []:
        if isinstance(v, dict):
            ref = [i for i in (_int(x) for x in (v.get("quote_ref") or [])) if i is not None]
            values.append({"name": str(v.get("name") or "").strip(), "value": str(v.get("value") or "").strip(),
                           "unit": _opt(v.get("unit")), "comparator": _opt(v.get("comparator"), drop=("=",)),
                           "quote_ref": ref})
    answer = obj.get("answer")
    if not isinstance(answer, str):
        problems.append("answer is missing or not a string")
        answer = "" if answer is None else str(answer)
    return {"status": status, "answer": answer.strip(), "claims": claims, "conflicts": conflicts,
            "values": values}, problems


# =========================================================================== verification

_HISTORY = re.compile(r"(?i)amendment|history|summary of changes|revision|protocol changes|change log")
_HISTORY_Q = re.compile(r"(?i)amend|change|history|revis|version|previous|original protocol")


@dataclass
class Checked:
    """One model attempt after verification."""
    parsed: bool
    status_model: str = ""
    answer: str = ""
    claims: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    values: list = field(default_factory=list)
    numbers_unverified: list = field(default_factory=list)
    critical: list = field(default_factory=list)  # (where, issue) pairs that warrant the retry
    numbers_unverified_values: list = field(default_factory=list)  # the bare values of numbers_unverified


def _value_text(v: dict) -> str:
    return " ".join(x for x in (v.get("comparator") or "", v.get("value") or "", v.get("unit") or "") if x)


def locate_quote(qi: int, q: dict, blocks: list[VBlock], by_n: dict | None = None) -> tuple[dict, list[str], str | None]:
    """(entry, issues, matched text or None) of quote qi {"block", "quote"} in the context blocks: in its
    cited block, else re-cited to another block that holds it (as protocol text, when the cited block
    had it only as AI-generated figure text)."""
    by_n = by_n if by_n is not None else {b.n: b for b in blocks}
    issues: list[str] = []
    qtext = clean_quote(q["quote"])
    entry: dict[str, Any] = {"block": q["block"], "quote": q["quote"], "match": None, "source": None}
    block = by_n.get(q["block"]) if q["block"] is not None else None
    lvl, src, sim, mtext = locate(qtext, block) if block is not None and qtext else (None, "", 0.0, "")
    if qtext and (lvl is None or src == "ai_generated"):
        for b in blocks:
            if b is block:
                continue
            other = locate(qtext, b)
            if other[0] and (lvl is None or other[1] == "document"):
                lvl, src, sim, mtext = other
                issues.append(f"note: quote {qi} is in block [{b.n}], not [{q['block']}]; re-cited")
                entry["cited_block"] = q["block"]
                entry["block"] = b.n
                break
    if lvl is None:
        short = len(compact(norm(qtext))) < MIN_QUOTE_CHARS
        where = f"block [{q['block']}]" if block is not None else f"block [{q['block']}] (no such block)"
        issues.append(f"quote {qi} is too short to verify" if short and qtext else
                      f"quote {qi} not found in {where} or any other block: «{qtext[:90]}»")
        return entry, issues, None
    entry.update(match=lvl, source=src)
    if lvl == "fuzzy":
        entry.update(similarity=sim, matched=mtext)
        issues.append(f"note: quote {qi} matched fuzzily ({sim:.2f})")
    return entry, issues, mtext


def verify(obj: dict | None, problems: list[str], blocks: list[VBlock], question: str,
           answer_refs: list[VBlock] | None = None) -> Checked:
    """answer_refs: blocks whose section labels, pages and cross-references ("Table 7") the prose answer
    may mention without a quote (default: the blocks cited by verified claims)."""
    if obj is None:
        return Checked(False, critical=[("response", p) for p in problems])
    out = Checked(True, obj["status"], obj["answer"], critical=[("response", p) for p in problems])
    by_n = {b.n: b for b in blocks}
    history_q = bool(_HISTORY_Q.search(question or ""))
    claim_quote_norms: list[list[str]] = []
    derived_ok: list[set[str]] = []
    for ci, c in enumerate(obj["claims"]):
        issues: list[str] = []
        quotes_out, qnorms = [], []
        if not c["text"]:
            issues.append("empty claim text")
        if not c["quotes"]:
            issues.append("no quote")
        for qi, q in enumerate(c["quotes"]):
            entry, qissues, mtext = locate_quote(qi, q, blocks, by_n)
            issues += qissues
            if mtext is not None:
                qnorms.append(mtext)
            quotes_out.append(entry)
        ok_quotes = [e for e in quotes_out if e["match"]]
        if ok_quotes and all(e["source"] == "ai_generated" for e in ok_quotes):
            issues.append("supported only by AI-generated figure text")
        if ok_quotes and not history_q and all(_HISTORY.search(by_n[e["block"]].section or "")
                                               for e in ok_quotes if e["block"] in by_n):
            issues.append("note: supported only by amendment/history text")
        cited_blocks = [by_n[e["block"]] for e in ok_quotes if e["block"] in by_n]
        allowed = {m.value for b in cited_blocks for m in mentions(b.section or "")}
        found = text_mentions(c["text"], [b.full.norm for b in cited_blocks] + [norm(b.section) for b in cited_blocks])
        results: set[str] = set()
        if c["derived"]:
            _, results, dissues = check_derivation(c["derivation"], qnorms)
            issues += dissues
            found = [m for m in found if m.value not in results]
        issues += check_mentions(found, qnorms, allowed)
        issues = list(dict.fromkeys(issues))
        verified = bool(ok_quotes) and not any(not i.startswith("note:") for i in issues)
        claim = {"text": c["text"], "quotes": quotes_out, "derived": c["derived"], "derivation": c["derivation"],
                 "verified": verified, "issues": issues}
        out.claims.append(claim)
        out.critical += [(f"claim {ci}", i) for i in issues if not i.startswith("note:")]
        claim_quote_norms.append(_matched_texts(quotes_out, qnorms))
        derived_ok.append(results if (verified and c["derived"]) else set())

    for vi, v in enumerate(obj["values"]):
        issues = []
        ref = v["quote_ref"]
        if len(ref) != 2 or not (0 <= ref[0] < len(out.claims)) or not (0 <= ref[1] < len(out.claims[ref[0]]["quotes"])):
            issues.append(f"quote_ref {ref} does not point to a claim's quote")
        else:
            claim = out.claims[ref[0]]
            qn = claim_quote_norms[ref[0]][ref[1]]
            if not claim["verified"] or not qn:
                issues.append(f"its quote (claim {ref[0]}, quote {ref[1]}) is not verified")
            elif not v["value"]:
                issues.append("empty value")
            else:
                vm = mentions(_value_text(v))
                if vm:
                    vm = [m for m in vm if m.value not in derived_ok[ref[0]]]
                    issues += [i.replace("the claim's quotes", "the referenced quote").replace("the claim has", "the value has")
                               for i in check_mentions(vm, [qn])]
                else:
                    vn = norm(v["value"])
                    if vn not in qn and compact(vn) not in compact(qn):
                        issues.append(f"value {v['value']!r} is not in the referenced quote")
                if v.get("unit") and not mentions(v["value"]):
                    un = norm(v["unit"])
                    if un not in qn and compact(un) not in compact(qn):
                        issues.append(f"unit {v['unit']!r} is not in the referenced quote")
        issues = list(dict.fromkeys(issues))
        out.values.append({**v, "verified": not issues, "issues": issues})
        out.critical += [(f"value {vi}", i) for i in issues]

    for cf in obj["conflicts"]:
        idx = [i for i in dict.fromkeys(cf["claims"]) if 0 <= i < len(out.claims)]
        quotes = [{"claim": i, "block": q["block"], "quote": q["quote"]}
                  for i in idx for q in out.claims[i]["quotes"] if q["match"]]
        ok = len(idx) >= 2 and all(out.claims[i]["verified"] for i in idx)
        out.conflicts.append({"claims": idx, "quotes": quotes, "note": cf["note"], "verified": ok})
        if len(idx) < 2:
            out.critical.append(("conflict", f"a conflict entry must name at least two claims (got {cf['claims']})"))

    if obj["status"] in ("answered", "partial", "conflict") and not obj["claims"]:
        out.critical.append(("response", f"status {obj['status']} needs at least one claim with a quote"))
    if obj["status"] == "conflict" and not obj["conflicts"]:
        out.critical.append(("response", "status conflict needs a conflicts entry naming the claims that disagree"))

    # Numbers in the prose answer: in a verified quote (same symbol comparator), a derived result, the
    # question, or a cited block's section label / pages.
    verified_quotes = [qn for c, qns in zip(out.claims, claim_quote_norms) if c["verified"] for qn in qns if qn]
    occ_all = [m for q in verified_quotes for m in mentions(q, normalized=True)]
    allowed = {m.value for m in mentions(question or "")}
    cited: dict[int, VBlock] = {}
    for c, res in zip(out.claims, derived_ok):
        allowed |= res
        if c["verified"] and c["derived"]:
            allowed |= {m.value for m in mentions(strip_refs(c["derivation"]))}
        if c["verified"]:
            for e in c["quotes"]:
                b = by_n.get(e["block"])
                if b is not None and e["match"]:
                    cited[b.n] = b
                    allowed |= {m.value for m in mentions(b.section or "")} | {str(p) for p in b.pages or []}
    refs = list(cited.values()) if answer_refs is None else list(answer_refs)
    for b in refs:
        allowed |= {m.value for m in mentions(b.section or "")} | {str(p) for p in b.pages or []}
    bad, bad_values = [], []
    hay = [b.full.norm for b in refs] + [norm(b.section) for b in refs]
    for pm in text_mentions(out.answer, hay):
        occ = [o for o in occ_all if o.value == pm.value]
        if not occ:
            if pm.value not in allowed:
                bad.append(pm.show())
                bad_values.append(pm.value)
        elif pm.cmp and pm.cmp_src != "post" and not any(o.cmp == pm.cmp for o in occ):
            bad.append(f"{pm.show()} (comparator differs from the quotes: "
                       f"{', '.join(sorted({(o.cmp or '') + o.value for o in occ}))})")
            bad_values.append(pm.value)
    first: dict[str, str] = {}
    for shown, value in zip(bad, bad_values):
        first.setdefault(shown, value)
    out.numbers_unverified = list(first)
    out.numbers_unverified_values = list(first.values())  # parallel to numbers_unverified
    out.critical += [("answer text", f"number {b} is not supported by a verified quote") for b in out.numbers_unverified]
    return out


def _matched_texts(quotes_out: list[dict], qnorms: list[str]) -> list[str]:
    """qnorms holds the matched text of the matched quotes only, in order: spread it over all quotes."""
    it = iter(qnorms)
    return [next(it) if e["match"] else "" for e in quotes_out]


def feedback(checked: Checked) -> str:
    lines = ["Automatic verification of your answer found these problems:"]
    lines += [f"- {where}: {issue}" for where, issue in checked.critical]
    lines.append(RETRY_INSTRUCTIONS)
    return "\n".join(lines)


def finalize(checked: Checked) -> str:
    """The final status from the model's status and the verification."""
    if not checked.parsed:
        return "partial"
    s = checked.status_model
    if s == "not_found":
        return "not_found"
    if any(cf["verified"] for cf in checked.conflicts):
        return "conflict"
    unverified = (any(not c["verified"] for c in checked.claims) or any(not v["verified"] for v in checked.values)
                  or bool(checked.numbers_unverified) or not any(c["verified"] for c in checked.claims))
    if s in ("answered", "conflict"):
        return "partial" if unverified else "answered"
    return "partial"


# =========================================================================== model calls and cache

class OpenRouterChat:
    """POST a chat completion (retrieve.post_with_retry refuses under OPENROUTER_OFFLINE=1)."""

    def complete(self, body: dict) -> dict:
        from figure_vlm import _api_key
        from retrieve import post_with_retry

        return post_with_retry(OPENROUTER_CHAT, body, _api_key("OPENROUTER_API_KEY")).json()


class AnswerCache:
    """{key: {"content", "dropped", "provider", "usage"}} in one JSON file, written atomically."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self.data: dict = {}
        if self.path and self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, key: str) -> dict | None:
        return self.data.get(key)

    def put(self, key: str, entry: dict) -> None:
        self.data[key] = entry
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)


def request_body(model: str, messages: list[dict]) -> dict:
    return {"model": model, "temperature": 0, "max_tokens": MAX_TOKENS, "messages": messages,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "grounded_answer", "strict": True, "schema": SCHEMA}},
            "provider": dict(PROVIDER)}


def chat(client: Any, body: dict) -> tuple[str, dict]:
    """(content, meta) of one completion. On HTTP 400 the call is repeated without the optional parameter
    the error names (else temperature, then response_format); an empty reply is re-requested."""
    dropped: list[str] = []
    for _ in range(EMPTY_ATTEMPTS):
        while True:
            try:
                data = client.complete({k: v for k, v in body.items() if k not in dropped})
                break
            except RuntimeError as exc:
                msg = str(exc)
                left = [k for k in ("temperature", "response_format") if k in body and k not in dropped]
                if not msg.startswith("OpenRouter 400") or not left:
                    raise
                named = [k for k in left if k in msg or (k == "response_format"
                                                           and re.search(r"(?i)json.?schema|structured", msg))]
                dropped.append((named or left)[0])
        try:
            choice = data["choices"][0]
            content = choice["message"].get("content")
        except (KeyError, IndexError, TypeError):
            raise RuntimeError(f"unexpected OpenRouter reply (keys: {sorted(data) if isinstance(data, dict) else type(data)})")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if content and content.strip():
            return content, {"dropped": dropped, "provider": data.get("provider"), "usage": data.get("usage"),
                             "finish_reason": choice.get("finish_reason")}
    raise RuntimeError(f"model returned no text after {EMPTY_ATTEMPTS} attempts "
                       f"(finish_reason={choice.get('finish_reason')!r})")


def offline() -> bool:
    return os.environ.get("OPENROUTER_OFFLINE") == "1"


def context_blocks(context: Any) -> list[VBlock]:
    """VBlocks of a retrieve.Context (numbered from 1, as Context.render numbers them) or of a list of
    blocks / dicts with text, section, pages, kind."""
    items = getattr(context, "blocks", context)
    out = []
    for i, b in enumerate(items, start=1):
        get = (lambda k, d=None, b=b: b.get(k, d)) if isinstance(b, dict) else (lambda k, d=None, b=b: getattr(b, k, d))
        out.append(VBlock(i, get("text", "") or "", get("section", "") or "", list(get("pages", []) or []),
                          get("kind", "text") or "text"))
    # The abbreviation definitions rendered after the numbered blocks are protocol text too (the glossary):
    # an extra, unnumbered-in-the-prompt block, found by the any-block fallback of quote location.
    defs = getattr(context, "definitions", None) or {}
    if defs:
        out.append(VBlock(len(out) + 1, "\n".join(f"- {k}: {v}" for k, v in sorted(defs.items())),
                          "Abbreviations (glossary)", [], "definitions"))
    return out


class CachedCaller:
    """One cached chat completion per key (AnswerCache); OfflineMiss instead of a call when the API is not
    allowed (allow_api=False or OPENROUTER_OFFLINE=1)."""

    what = "answer"
    prompt_version = PROMPT_VERSION

    def __init__(self, model: str = DEFAULT_MODEL, cache_file: str | Path | None = None, client: Any = None,
                 allow_api: bool = True):
        self.model = model
        self.cache = AnswerCache(Path(cache_file) if cache_file else None)
        self.client = client
        self.allow_api = allow_api
        self.calls = 0

    def _call(self, key: str, body: dict) -> tuple[str, dict, bool]:
        """(content, meta, cached) for one cache key."""
        hit = self.cache.get(key)
        if hit is not None:
            return hit["content"], hit, True
        if not self.allow_api or offline():
            raise OfflineMiss(f"{self.what} not cached and the API is not allowed"
                              + (" (OPENROUTER_OFFLINE=1)" if offline() else ""))
        if self.client is None:
            self.client = OpenRouterChat()
        self.calls += 1
        content, meta = chat(self.client, body)
        entry = {"content": content, **meta, "model": self.model, "prompt_version": self.prompt_version}
        self.cache.put(key, entry)
        return content, entry, False


class GroundedAnswerer(CachedCaller):
    """The structured call, verification, retry and status on a built context."""

    def key(self, context_text: str, question: str, retry: list | None = None) -> str:
        return sha(json.dumps([self.model, PROMPT_VERSION, PROMPT_HASH, sha(context_text), question, retry],
                              ensure_ascii=False))

    def _complete(self, key: str, messages: list[dict]) -> tuple[str, dict, bool]:
        """(content, meta, cached) for one cache key."""
        return self._call(key, request_body(self.model, messages))

    def run(self, question: str, context: Any, context_text: str | None = None) -> dict:
        """Answer from a built context (retrieve.Context, or blocks + context_text)."""
        blocks = context_blocks(context)
        text = context_text if context_text is not None else context.render()
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_message(text, question)}]
        calls_before = self.calls
        content1, meta1, cached1 = self._complete(self.key(text, question), messages)
        first = self._check(content1, blocks, question)
        final, retried, meta, cached = first, False, meta1, cached1
        if first.critical:
            fb = feedback(first)
            msgs2 = messages + [{"role": "assistant", "content": content1}, {"role": "user", "content": fb}]
            content2, meta2, cached2 = self._complete(self.key(text, question, [sha(content1), fb]), msgs2)
            second = self._check(content2, blocks, question)
            retried, cached = True, cached1 and cached2
            if second.parsed and (not first.parsed or len(second.critical) <= len(first.critical)):
                final, meta = second, meta2
        return self._result(final, first if retried else None, retried, blocks, context, meta,
                            self.calls - calls_before, cached)

    @staticmethod
    def _check(content: str, blocks: list[VBlock], question: str) -> Checked:
        try:
            obj, problems = coerce(parse_json(content))
        except ValueError:
            obj, problems = None, ["the reply is not valid JSON (it may have been cut off: keep quotes and "
                                   "the answer shorter)"]
        return verify(obj, problems, blocks, question)

    def _result(self, c: Checked, first: Checked | None, retried: bool, blocks: list[VBlock], context: Any,
                meta: dict, calls: int, cached: bool) -> dict:
        quotes = [q for cl in c.claims for q in cl["quotes"]]
        status = finalize(c)
        verification = {
            "quotes_ok": sum(1 for q in quotes if q["match"]),
            "quotes_failed": sum(1 for q in quotes if not q["match"]),
            "numbers_unverified": list(c.numbers_unverified),
            "retried": retried,
            "claims_verified": sum(1 for cl in c.claims if cl["verified"]),
            "claims_total": len(c.claims),
            "values_failed": sum(1 for v in c.values if not v["verified"]),
            "problems": [f"{w}: {i}" for w, i in c.critical],
            "model_status": c.status_model or None,
        }
        if first is not None:
            verification["first_attempt_problems"] = [f"{w}: {i}" for w, i in first.critical]
        return {
            "status": status,
            "answer": c.answer if c.parsed else "",
            "claims": c.claims,
            "conflicts": c.conflicts,
            "values": c.values,
            "context_blocks": [{"n": b.n, "section": b.section or "Front matter", "pages": b.pages, "kind": b.kind}
                               for b in blocks],
            "verification": verification,
            "meta": {"model": self.model, "prompt_version": PROMPT_VERSION, "api_calls": calls, "cached": cached,
                     "dropped_params": meta.get("dropped") or [], "provider": meta.get("provider"),
                     "finish_reason": meta.get("finish_reason"),
                     "context_tokens": getattr(context, "tokens", None)},
        }


# =========================================================================== retrieval + answer()

_ENVS: dict[str, tuple[Any, Any]] = {}


def production_env(out_dir: str | Path) -> tuple[Any, Any]:
    """(retrieve.Retriever, retrieve.production_builder) for an output folder, loaded once."""
    key = str(Path(out_dir).resolve())
    if key not in _ENVS:
        from retrieve import Retriever, production_builder

        retriever = Retriever(Path(out_dir) / "chunks" / "chunks.jsonl")
        _ENVS[key] = (retriever, production_builder(retriever))
    return _ENVS[key]


def retrieve_context(question: str, out_dir: str | Path, k: int = PRODUCTION_K, allow_api: bool = True):
    """The production context for a question: embed (cached), rerank (cached), parent-aware build."""
    from eval_retrieval import cached_query_vectors

    retriever, builder = production_env(out_dir)
    qtext = retriever.query_text(retriever.expand(question)[0])
    cache_file = retriever.cache_dir / "query_embedding_cache.json"
    model = retriever.meta["model"]
    if not allow_api or offline():
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
        if hashlib.sha256(f"{model}|{qtext}".encode()).hexdigest() not in cache:
            raise OfflineMiss("query embedding not cached and the API is not allowed")
    vec = cached_query_vectors([qtext], model, cache_file)[0]
    if not allow_api and not offline():
        os.environ["OPENROUTER_OFFLINE"] = "1"  # the reranker has no allow flag: force cache-only for this call
        try:
            return _search_build(retriever, builder, question, vec, k)
        finally:
            del os.environ["OPENROUTER_OFFLINE"]
    return _search_build(retriever, builder, question, vec, k)


def _search_build(retriever: Any, builder: Any, question: str, vec: Any, k: int):
    try:
        res = retriever.search(question, vec, k=k, mode="rerank")
    except RuntimeError as exc:
        if "OPENROUTER_OFFLINE" in str(exc):
            raise OfflineMiss("rerank not cached and the API is not allowed") from exc
        raise
    return builder.build(res)


def answer(question: str, out_dir: str | Path = "output", model: str = DEFAULT_MODEL, k: int = PRODUCTION_K,
           client: Any = None, *, context: Any = None, allow_api: bool = True,
           cache_file: str | Path | None = None) -> dict:
    """Grounded, verified answer to a question about the protocol in out_dir (see the module docstring).

    context: an already built retrieve.Context (skips retrieval). The answer cache defaults to
    <out_dir>/chunks/answer_v2_cache.json. Raises OfflineMiss when a call is needed but not allowed."""
    if context is None:
        context = retrieve_context(question, out_dir, k, allow_api=allow_api)
    cache = cache_file if cache_file is not None else Path(out_dir) / "chunks" / CACHE_NAME
    return GroundedAnswerer(model, cache, client, allow_api).run(question, context)


# =========================================================================== eval_v1 integration

def scorer_text(result: dict) -> str:
    """What eval_v1 scores: JSON with the status (eval_v1.abstained reads it: abstention == not_found), the
    prose answer one line per item (so key terms keep their word boundaries) and the claim texts
    (unverified ones prefixed). Quotes are left out: they would score the context, not the answer."""
    lines = [" ".join(ln.replace("\t", " ").split()) for ln in (result.get("answer") or "").splitlines()]
    return json.dumps({
        "status": result["status"],
        "answer": [ln for ln in lines if ln],
        "claims": [("" if c.get("verified") else "UNVERIFIED: ") + c["text"] for c in result.get("claims") or []],
        "conflicts": [cf["note"] for cf in result.get("conflicts") or [] if cf.get("note")],
    }, ensure_ascii=False, indent=1)


class RecordingAssembler:
    """parents.ParentAssembler that also keeps each built retrieve.Context, keyed by its rendered text,
    so the answerer can verify quotes against the blocks of exactly the context the scorer sees."""

    def __init__(self, builder: Any, name: str):
        self.builder = builder
        self.name = name
        self.contexts: dict[str, tuple[str | None, Any]] = {}

    def build(self, q: dict, retrieval: Any, k: int):
        from eval_v1 import Ctx
        from retrieve import Result

        res = retrieval.payload
        ctx = self.builder.build(Result(res.query, res.expanded_query, res.definitions, res.hits[:k]))
        text = ctx.render()
        self.contexts[text] = (q.get("id"), ctx)
        return Ctx(text, ctx.tokens, {
            "blocks": len(ctx.blocks), "fallbacks": len(ctx.fallbacks), "dropped": len(ctx.dropped),
            "duplicates": len(ctx.duplicates), "expanded": len(ctx.expanded), "parents": len(ctx.parents)})


class EvalAnswerer:
    """eval_v1 answerer: .get(question, context_text) -> scorer_text(result) | None (cache miss)."""

    def __init__(self, recorder: RecordingAssembler, cache_dir: Path, model: str, allow_api: bool,
                 doc: str = "", numbers_only: bool = False):
        self.recorder = recorder
        self.model = model
        self.name = f"answer_v2:{model}:{PROMPT_VERSION}"
        self.answerer = GroundedAnswerer(model, Path(cache_dir) / CACHE_NAME, allow_api=allow_api)
        self.status_file = Path(cache_dir) / STATUS_NAME
        self.doc = doc
        self.numbers_only = numbers_only
        self.records: dict[str, dict] = {}
        self.misses = 0
        atexit.register(self.report)

    def get(self, question: str, context: str) -> str | None:
        entry = self.recorder.contexts.get(context)
        if entry is None:
            raise RuntimeError("context was not built by answer:env_factory's assembler")
        qid, ctx = entry
        try:
            res = self.answerer.run(question, ctx, context_text=context)
        except OfflineMiss:
            self.misses += 1
            return None
        v = res["verification"]
        self.records[qid or sha(question)[:12]] = {
            "status": res["status"], "model_status": v["model_status"], "claims": v["claims_total"],
            "claims_verified": v["claims_verified"], "quotes_ok": v["quotes_ok"], "quotes_failed": v["quotes_failed"],
            "values_failed": v["values_failed"], "numbers_unverified": len(v["numbers_unverified"]),
            "retried": v["retried"], "api_calls": res["meta"]["api_calls"], "model": self.model,
            "prompt_version": PROMPT_VERSION}
        self._save()
        return scorer_text(res)

    def _save(self) -> None:
        old = json.loads(self.status_file.read_text(encoding="utf-8")) if self.status_file.exists() else {}
        old.update(self.records)
        tmp = self.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(old, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.status_file)

    def report(self) -> None:
        recs = list(self.records.values())
        if not recs and not self.misses:
            return
        st = Counter(r["status"] for r in recs)
        claims = sum(r["claims"] for r in recs)
        print(f"{LOG_PREFIX} {self.doc}: {len(recs)} answers ({', '.join(f'{s} {st[s]}' for s in STATUSES if st[s])}); "
              f"claims verified {sum(r['claims_verified'] for r in recs)}/{claims}; quotes failed "
              f"{sum(r['quotes_failed'] for r in recs)}; answers with unverified prose numbers "
              f"{sum(1 for r in recs if r['numbers_unverified'])}; retried {sum(r['retried'] for r in recs)}; "
              f"API calls {self.answerer.calls}; cache misses {self.misses}"
              + ("" if self.numbers_only or not recs else f"; per question: {self.status_file}"))


def env_factory(doc: str, chunks_path: Any, opts: Any):
    """`eval_v1.py run ... --k 8 --env-factory answer:env_factory --answers`: the production context
    (parents.env_factory_12k = retrieve.production_builder) and this module's verified answers."""
    import parents

    env = parents.make_env(doc, chunks_path, opts, budget=PRODUCTION_BUDGET)
    if env.assembler is None:
        return env
    rec = RecordingAssembler(env.assembler.builder, env.assembler.name)
    env.assembler = rec
    if getattr(opts, "k", PRODUCTION_K) != PRODUCTION_K:
        env.notes.append(f"{LOG_PREFIX}: note: production uses k={PRODUCTION_K} (this run: --k {opts.k})")
    if getattr(opts, "answers", False):
        model = getattr(opts, "answer_model", None) or DEFAULT_MODEL
        env.answerer = EvalAnswerer(rec, Path(chunks_path).parent, model, bool(getattr(opts, "allow_api", False)),
                                    doc=doc, numbers_only=bool(getattr(opts, "numbers_only", False)))
        env.notes.append(f"{LOG_PREFIX}: {model}, prompt {PROMPT_VERSION}; an uncached answer is 1 call plus 1 retry "
                         "call when verification fails (the plan's answer count x1-x2); abstention = status not_found")
    return env


# =========================================================================== answer, then verify
#
# Step 1 is the production prose answer, produced exactly as eval_answers.answer() does (same model,
# system prompt, user message, cache file <chunks>/answer_cache.json and cache key), so every answer of
# the production eval runs is already cached. The prose is never rewritten. Step 2 is one verification
# call that splits the prose into statements and attaches quotes; this module's deterministic checks
# (verify()) then decide what holds.

VERIFY_MODEL = DEFAULT_MODEL
VERIFY_PROMPT_VERSION = "verify-v1.0"
VERIFY_CACHE_NAME = "answer_verify_cache.json"
VERIFY_STATUS_NAME = "answer_verify_status.json"
PROSE_CACHE_NAME = "answer_cache.json"  # eval_answers.answer()'s cache
VERIFY_LOG_PREFIX = "answer_verify"
SUPPORTS = ("quoted", "derived", "unsupported")
RESULTS = ("verified", "derived_ok", "unverified", "unsupported", "meta")
# A statement about the answer or the context itself ("the context does not state X", "I found no ..."),
# not a fact about the protocol: reported as "meta", never flagged.
_META = re.compile(
    r"(?i)\b(?:the|this) (?:context|provided (?:context|blocks)|blocks?|excerpts?)\b[^.]{0,80}?"
    r"\b(?:does not|doesn't|do not|don't|did not|is silent|gives? no|provides? no|contains? no|lists? no)\b"
    r"|\bI (?:found|could not|did not|cannot|can't)\b|\bnot (?:stated|specified|given|provided|mentioned)"
    r" (?:in|by) the (?:context|blocks?)\b|\bno (?:other|further|explicit)\b[^.]{0,60}\b(?:in|within) the (?:context|blocks?)\b")
_REF_NUMBER = r"(?i)(?:sections?|§|appendix|appendices|tables?|figures?|criteri(?:on|a)|amendments?)\s*(?:#\s*)?{n}\b"


def _is_reference_number(num: str, text: str) -> bool:
    """A section / table / appendix / criterion / amendment number cited in the text ("Section 4.2.1")."""
    return bool(re.search(_REF_NUMBER.format(n=re.escape(num)), text))


FLAGGED = ("unverified", "unsupported")
IN_PROSE_MIN = 0.9  # share of a statement's tokens found, in order, in one place of the prose

VERIFY_SYSTEM = """You check an answer about one clinical trial protocol against the numbered context blocks it was written from, for an agent that builds USDM 4 study data. You do not answer the question yourself and you do not improve, correct or complete the answer. Reply with one JSON object that follows the schema.

1. Statements. List the factual statements the answer makes, in the answer's order: one per sentence or list item, split further only where one sentence states several independent facts (e.g. two criteria, two visits, two doses). Copy each statement's words from the answer in their order; you may leave out citations such as [3], list markers and connecting words, but never reword, correct, complete or add anything, and never list a statement the answer does not make. Leave out headings, restatements of the question, and sentences that only say the context does not state something.
2. Support. Give each statement exactly one "support":
 - "quoted": "quotes" [{"block": n, "quote": "..."}] copied character for character from block [n] that together state everything the statement says, including every number, unit and comparator. A quote is a contiguous span: a sentence, a list item, a table row, or a cell with its row label; use "…" only to skip words within one sentence or table row, never to join different places. The header line of a block ("[n] type | section | pages") is not quotable. For a table cell, quote the row and, as a second quote, the header text that names the column (e.g. the visit or week).
 - "derived": the statement gives a value computed from the blocks (a sum, count, difference, duration). Quote every input and give "derivation" with the inputs and the arithmetic, e.g. "123 + 64 = 187" or "count of exclusion criteria listed = 12".
 - "unsupported": no block states it, a block contradicts it, or the blocks support only part of it (a different number, comparator, unit, visit, timing, population or item is not support). "quotes" may show the closest text; "note" says what is not supported.
 "derivation" is "" unless the support is "derived"; "note" is "" or a short remark.
3. Exactness. "≥" and ">" differ, "≤" and "<" differ; a dose, unit, window, visit or timing must be the same as in the quote. Judge each statement as the answer wrote it; do not repair it to match a quote.
4. AI-generated figure text. Text under "Diagram (Mermaid, AI-generated)" or "Description (AI-generated by …)", after "*Figure description generated by …*", and Mermaid code was written by an AI model from a figure image, not by the protocol authors; "Text in figure (OCR)" is text read from the figure itself. Prefer protocol or OCR text; if a statement rests on AI-generated text alone, quote that text anyway.
5. History. Amendment histories, summary-of-changes tables and text marked "Was:" or "Removed:" describe superseded versions; quote current text where it exists.
6. Conflicts. In "conflicts", list each place where blocks disagree with each other about a fact the question asks for (e.g. synopsis vs body, "<400" vs "≤400", ">" vs "≥", different doses, timings or windows): one entry per disagreement, with one quote per version and a short "note" on what differs. A superseded amendment version is not a conflict, and neither is a disagreement between the answer and the blocks (that makes the statement unsupported). Empty list when there are none."""

_VQUOTES = {"type": "array", "items": {
    "type": "object",
    "properties": {"block": {"type": "integer"}, "quote": {"type": "string"}},
    "required": ["block", "quote"], "additionalProperties": False}}
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "statements": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "support": {"type": "string", "enum": list(SUPPORTS)},
                           "quotes": _VQUOTES, "derivation": {"type": "string"}, "note": {"type": "string"}},
            "required": ["text", "support", "quotes", "derivation", "note"], "additionalProperties": False}},
        "conflicts": {"type": "array", "items": {
            "type": "object",
            "properties": {"note": {"type": "string"}, "quotes": _VQUOTES},
            "required": ["note", "quotes"], "additionalProperties": False}},
    },
    "required": ["statements", "conflicts"],
    "additionalProperties": False,
}
VERIFY_PROMPT_HASH = sha(VERIFY_SYSTEM + json.dumps(VERIFY_SCHEMA, sort_keys=True))[:12]


def verify_user_message(context_text: str, question: str, prose: str) -> str:
    return (f"Context (numbered blocks; each header gives [n], the block type, its section and pages):\n\n"
            f"{context_text}\n\nQuestion: {question}\n\nAnswer to check (between the markers):\n<<<ANSWER\n"
            f"{prose}\nANSWER>>>\n\nReply with the JSON object only.")


def verify_request_body(model: str, messages: list[dict]) -> dict:
    return {"model": model, "temperature": 0, "max_tokens": MAX_TOKENS, "messages": messages,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer_verification", "strict": True,
                                                "schema": VERIFY_SCHEMA}},
            "provider": dict(PROVIDER)}


# --------------------------------------------------------------------------- step 1: the production prose

class ProseAnswerer:
    """The production prose answer: eval_answers.answer() (= eval_v1.CachedAnswerer): same model, system
    prompt, user message, request body, cache file and cache key. get() reads the cache; on a miss it
    calls eval_answers.answer() itself (an injected client gets the identical body, for tests), or raises
    OfflineMiss when the API is not allowed."""

    def __init__(self, cache_file: str | Path | None, model: str | None = None, client: Any = None,
                 allow_api: bool = True):
        import eval_answers

        self.ea = eval_answers
        self.model = model or eval_answers.ANSWER_MODEL
        self.system = eval_answers.SYSTEM
        self.cache_file = Path(cache_file) if cache_file else None
        self.client = client
        self.allow_api = allow_api
        self.calls = 0
        self.cache: dict = self._load()

    def _load(self) -> dict:
        if self.cache_file and self.cache_file.exists():
            return json.loads(self.cache_file.read_text(encoding="utf-8"))
        return {}

    def key(self, context: str, question: str) -> str:
        return hashlib.sha256(json.dumps([self.model, self.system, context, question]).encode()).hexdigest()

    def body(self, context: str, question: str) -> dict:
        return {"model": self.model, "temperature": 0, "max_tokens": 4000,
                "messages": [{"role": "system", "content": self.system},
                             {"role": "user", "content": f"Context:\n\n{context}\n\nQuestion: {question}"}],
                "provider": {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}}

    def get(self, question: str, context: str) -> tuple[str, bool]:
        """(prose, cached)."""
        key = self.key(context, question)
        if key in self.cache:
            return self.cache[key], True
        self.cache = self._load()  # another process may have answered it since
        if key in self.cache:
            return self.cache[key], True
        if not self.allow_api or offline():
            raise OfflineMiss("prose answer not cached and the API is not allowed"
                              + (" (OPENROUTER_OFFLINE=1)" if offline() else ""))
        self.calls += 1
        if self.client is None:
            if self.cache_file is None:
                raise ValueError("the production prose answer needs its cache file")
            text = self.ea.answer(self.model, context, question, self.cache_file)
            self.cache = self._load()
        else:
            text = self._client_answer(self.body(context, question))
            self.cache = self._load()
            self.cache[key] = text
            if self.cache_file:
                self.cache_file.parent.mkdir(parents=True, exist_ok=True)
                self.cache_file.write_text(json.dumps(self.cache, indent=1), encoding="utf-8")
        self.cache[key] = text
        return text, False

    def _client_answer(self, body: dict) -> str:
        """eval_answers.answer()'s request loop on an injected client (an empty reply is re-requested)."""
        for _ in range(3):
            data = self.client.complete(body)
            content = data["choices"][0]["message"].get("content")
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content and content.strip():
                return content
        raise RuntimeError("model returned no answer text after 3 attempts")


# --------------------------------------------------------------------------- statements in the prose

_PTOK = re.compile(r"(?:(?<![\w.])[-‐‑‒–—−](?=\d))?\d+(?:[.,]\d+)*|[^\W\d_]+|[≥≤<>±=%~+]")
_NEGATIONS = frozenset({"not", "no", "never", "without", "non", "neither", "nor", "none", "except", "excluding",
                        "unless", "cannot"})
_STOP = frozenset("the a an of in on at to for by and or is are be was were with as from this that".split())


def prose_tokens(text: str) -> list[tuple[str, int, int]]:
    """(normalised token, start, end) of the words, numbers (with a leading sign) and comparator symbols of
    a text; citations ("[3]", "block 3") are left out. Tokens are normalised one by one, so "10^9/L",
    "10⁹/L" and "10 9 /L" give the same tokens."""
    masked = _CITE.sub(lambda m: " " * len(m.group()), text or "")
    masked = _BLOCK_REF.sub(lambda m: " " * len(m.group()), masked)
    out = []
    for m in _PTOK.finditer(masked):
        t = norm(m.group()).replace(" ", "")
        if t:
            out.append((t, m.start(), m.end()))
    return out


def _critical(tok: str) -> bool:
    return any(ch.isdigit() for ch in tok) or tok in _CRIT_SYMBOLS or tok in _NEGATIONS


def locate_in_prose(text: str, ptoks: list[tuple[str, int, int]], taken: set | None = None
                    ) -> tuple[tuple[int, int] | None, float, str]:
    """(character span in the prose, token coverage, reason when not found) of a verifier statement.

    Found: >= IN_PROSE_MIN of the statement's tokens occur in order in one place of the prose (a window a
    little longer than the statement), and no number, comparator or negation differs between the two
    (the statement may not drop, add or change one). A span in `taken` (an earlier statement's) is a
    duplicate: another occurrence is used, or the statement is not found."""
    taken = taken or set()
    st = [t for t, _, _ in prose_tokens(text)]
    n = len(st)
    if not n:
        return None, 0.0, "the statement has no words"
    toks = [t for t, _, _ in ptoks]
    exact = [(ptoks[i][1], ptoks[i + n - 1][2]) for i in range(len(toks) - n + 1)
             if toks[i] == st[0] and toks[i:i + n] == st]
    if exact:
        free = [s for s in exact if s not in taken]
        return (free[0], 1.0, "") if free else (None, 1.0, "duplicate of an earlier statement")
    lead = set(st[:n // 10 + 1])
    width = n + max(2, -(-n // 5))
    cands = []
    for i, tok in enumerate(toks):
        if tok not in lead:
            continue
        mb = [b for b in SequenceMatcher(None, st, toks[i:i + width], autojunk=False).get_matching_blocks() if b.size]
        matched = sum(b.size for b in mb)
        if matched >= IN_PROSE_MIN * n:
            cands.append((matched, i, mb))
    if not cands:
        best = 0
        for i, tok in enumerate(toks):
            if tok in lead:
                best = max(best, sum(b.size for b in SequenceMatcher(None, st, toks[i:i + width],
                                                                     autojunk=False).get_matching_blocks()))
        return None, round(best / n, 3), f"not in the answer (token overlap {best / n:.2f})"
    reason = ""
    for matched, i, mb in sorted(cands, key=lambda c: (-c[0], c[1])):
        sa = {b.a + k for b in mb for k in range(b.size)}
        pb = {b.b + k for b in mb for k in range(b.size)}
        lo, hi = min(pb), max(pb)
        missing = [st[j] for j in range(n) if j not in sa and _critical(st[j])]
        extra = [toks[i + j] for j in range(lo, hi + 1) if j not in pb and _critical(toks[i + j])]
        if missing or extra:
            reason = ("numbers, comparators or negations differ from the answer: statement "
                      f"{missing or '-'}, answer {extra or '-'}")
            continue
        span = (ptoks[i + lo][1], ptoks[i + hi][2])
        if span not in taken:
            return span, round(matched / n, 3), ""
        reason = reason or "duplicate of an earlier statement"
    return None, round(cands[0][0] / n, 3), reason or "duplicate of an earlier statement"


# --------------------------------------------------------------------------- verifier output

def coerce_verification(obj: Any) -> tuple[dict | None, list[str]]:
    """The verifier object in canonical shape, or None; plus schema problems."""
    if not isinstance(obj, dict):
        return None, ["the verifier reply is not a JSON object"]
    problems = []
    if not isinstance(obj.get("statements"), list):
        problems.append("statements is missing or not a list")
    statements = []
    for i, s in enumerate(obj.get("statements") or [] if isinstance(obj.get("statements"), list) else []):
        if not isinstance(s, dict):
            problems.append(f"statement {i} is not an object")
            continue
        quotes = [{"block": _int(q.get("block")), "quote": str(q.get("quote") or "")}
                  for q in s.get("quotes") or [] if isinstance(q, dict)]
        derivation = str(s.get("derivation") or "").strip()
        support = str(s.get("support") or "").strip().lower().replace(" ", "_")
        if support not in SUPPORTS:
            problems.append(f"statement {i}: support {s.get('support')!r} is not one of {', '.join(SUPPORTS)}")
            support = "derived" if derivation else ("quoted" if quotes else "unsupported")
        statements.append({"text": str(s.get("text") or "").strip(), "support": support, "quotes": quotes,
                           "derivation": derivation, "note": str(s.get("note") or "").strip()})
    conflicts = []
    for cf in obj.get("conflicts") or [] if isinstance(obj.get("conflicts"), list) else []:
        if isinstance(cf, dict):
            conflicts.append({"note": str(cf.get("note") or "").strip(),
                              "quotes": [{"block": _int(q.get("block")), "quote": str(q.get("quote") or "")}
                                         for q in cf.get("quotes") or [] if isinstance(q, dict)]})
    return {"statements": statements, "conflicts": conflicts}, problems


def prose_abstains(prose: str) -> bool:
    """eval_v1's abstention detection (an abstention phrase near the start of the answer)."""
    from eval_v1 import abstained

    return abstained(prose)


_TRAIL_CITES = re.compile(r"(?:\s*\[\s*\d+(?:\s*[,;–-]\s*\d+)*\s*\])+")


def _past_citations(prose: str, end: int) -> int:
    m = _TRAIL_CITES.match(prose, end)
    return m.end() if m else end


def annotate(prose: str, statements: list[dict], numbers: list[str], conflicts: list[dict]) -> str:
    """The prose with "⚠[unverified]" / "⚠[unsupported]" after each flagged statement (after its citations)
    and footer lines for unverified numbers outside every statement and for verified conflicts."""
    marks: dict[int, list[str]] = {}
    for st in statements:
        if st["result"] in FLAGGED:
            at = _past_citations(prose, st["span"][1])
            if f"⚠[{st['result']}]" not in marks.get(at, []):
                marks.setdefault(at, []).append(f"⚠[{st['result']}]")
    out, pos = [], 0
    for at in sorted(marks):
        out += [prose[pos:at], " " + " ".join(marks[at])]
        pos = at
    out.append(prose[pos:])
    footer = []
    if numbers:
        footer.append(f"⚠[unverified numbers: {', '.join(numbers)}]")
    footer += [f"⚠[conflict: {cf['note'] or 'the blocks disagree'}]" for cf in conflicts if cf["verified"]]
    return "".join(out) + ("\n\n" + "\n".join(footer) if footer else "")


_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=\S)")
_LIST_MARK = re.compile(r"^\s*(?:[-*•·▪◦]|\d{1,2}[.)]|[a-z][.)])\s+")


def _segments(prose: str) -> list[tuple[int, int]]:
    """(start, end) of the sentences / list items of the prose, line by line."""
    segs, pos = [], 0
    for line in prose.split("\n"):
        start = pos
        for part in _SENT_SPLIT.split(line):
            s = line.find(part, start - pos) if part else -1
            if part.strip() and s >= 0:
                segs.append((pos + s, pos + s + len(part)))
                start = pos + s + len(part)
        pos += len(line) + 1
    return segs


def _subtract(span: tuple[int, int], others: list[tuple[int, int]]) -> list[tuple[int, int]]:
    parts = [span]
    for a, b in others:
        nxt = []
        for s, e in parts:
            if b <= s or a >= e:
                nxt.append((s, e))
                continue
            if s < a:
                nxt.append((s, a))
            if b < e:
                nxt.append((b, e))
        parts = nxt
    return [p for p in parts if p[1] > p[0]]


def strict_answer(prose: str, statements: list[dict]) -> str:
    """The prose without its unverified / unsupported statements: a sentence or list item made mostly of
    flagged statements (and of no verified one) goes whole, otherwise only the flagged spans (with their
    citations) are cut. Abstention sentences ("the context does not state ...") always stay."""
    from eval_v1 import _ABSTAIN

    bad = [(st["span"][0], _past_citations(prose, st["span"][1])) for st in statements if st["result"] in FLAGGED]
    if not bad:
        return prose
    good = [tuple(st["span"]) for st in statements if st["result"] not in FLAGGED]
    ptoks = prose_tokens(prose)
    cuts: list[tuple[int, int]] = []
    for s, e in _segments(prose):
        seg = prose[s:e]
        toks = [t for t in ptoks if s <= t[1] < e]
        if not toks or _ABSTAIN.search(norm(seg)):
            continue
        in_bad = [t for t in toks if any(a <= t[1] < b for a, b in bad)]
        in_good = [t for t in toks if any(a <= t[1] < b for a, b in good)]
        if not in_bad:
            continue
        if not in_good and len(in_bad) >= 0.5 * len(toks):
            cuts.append((s, e))
        else:
            for a, b in bad:
                if a < e and b > s:
                    cuts += _subtract((max(a, s), min(b, e)), good)
    keep = [True] * len(prose)
    for a, b in cuts:
        for i in range(a, b):
            keep[i] = False
    text = "".join(ch for ch, k in zip(prose, keep) if k)
    lines_in, lines_out = text.split("\n"), []
    orig = prose.split("\n")
    for i, line in enumerate(lines_in):
        cleaned = re.sub(r"[ \t]{2,}", " ", line)
        if line != (orig[i] if i < len(orig) else line):  # a cut touched this line
            cleaned = re.sub(r"(?:\s*,|\s+(?:and|or|but|while|whereas|as well as))+\s*(?=[.;:]|\s*$)", "", cleaned)
        cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)
        cleaned = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", cleaned)
        cleaned = re.sub(r"^(\s*(?:[-*•·▪◦]|\d{1,2}[.)])\s+)[,;:.]\s*", r"\1", cleaned).rstrip()
        body = _LIST_MARK.sub("", cleaned)
        was = orig[i] if i < len(orig) else line
        if was.strip() and not re.search(r"\w", body):
            continue  # the line held only flagged statements
        lines_out.append(cleaned)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines_out)).strip()


def verify_prose(prose: str, obj: dict | None, problems: list[str], blocks: list[VBlock], question: str) -> dict:
    """Deterministic verification of the verifier's statements against the prose and the blocks:
    status, statements (verified | derived_ok | unverified | unsupported), conflicts, counts, the
    annotated prose and the strict prose (flagged statements removed)."""
    ptoks = prose_tokens(prose)
    kept, dropped, taken = [], [], set()
    for i, s in enumerate((obj or {}).get("statements") or []):
        span, cov, why = locate_in_prose(s["text"], ptoks, taken) if s["text"] else (None, 0.0, "empty statement")
        if span is None:
            dropped.append({"index": i, "text": s["text"], "support": s["support"], "coverage": cov, "reason": why})
            continue
        taken.add(span)
        kept.append((i, s, span, cov))
    claims = [{"text": prose[span[0]:span[1]], "quotes": [] if s["support"] == "unsupported" else s["quotes"],
               "derived": s["support"] == "derived", "derivation": s["derivation"] if s["support"] == "derived" else ""}
              for _, s, span, _ in kept]
    checked = verify({"status": "answered", "answer": prose, "claims": claims, "conflicts": [], "values": []},
                     [], blocks, question, answer_refs=blocks)
    statements = []
    for (i, s, span, cov), c in zip(kept, checked.claims):
        if s["support"] == "unsupported":
            result = "unsupported"
            issues = [f"verifier: {s['note']}" if s["note"] else "verifier: no block supports it"]
            quotes = [{**q, "match": None, "source": None, "checked": False} for q in s["quotes"]]
        else:
            result = ("derived_ok" if s["support"] == "derived" else "verified") if c["verified"] else "unverified"
            issues, quotes = c["issues"], c["quotes"]
            # Only reference numbers ("Section 4.2.1") failed the number check: the statement is verified.
            nums = [m.group(1) for i in issues if (m := re.match(r"number (\S+) is not in the claim's quotes", i))]
            if result == "unverified" and nums and len(nums) == len(issues) and all(
                    _is_reference_number(n, prose[span[0]:span[1]]) for n in nums):
                result, issues = ("derived_ok" if s["support"] == "derived" else "verified"), []
        if result in FLAGGED and _META.search(prose[span[0]:span[1]]):
            result = "meta"
            issues = [*issues, "statement about the answer or the context, not a protocol fact"]
        statements.append({"index": i, "text": s["text"], "prose_text": prose[span[0]:span[1]], "span": list(span),
                           "support": s["support"], "result": result, "quotes": quotes,
                           "derivation": s["derivation"], "note": s["note"], "issues": issues, "coverage": cov})
    by_n = {b.n: b for b in blocks}
    conflicts = []
    for cf in (obj or {}).get("conflicts") or []:
        quotes, issues, texts = [], [], set()
        for qi, q in enumerate(cf["quotes"]):
            entry, qissues, mtext = locate_quote(qi, q, blocks, by_n)
            quotes.append(entry)
            issues += qissues
            if mtext is not None and entry["source"] == "document":
                texts.add(mtext)
        if len(cf["quotes"]) < 2:
            issues.append("a conflict needs at least two quotes (one per version)")
        if any(e["match"] and e["source"] == "ai_generated" for e in quotes):
            issues.append("a side rests on AI-generated figure text")
        ok = len(cf["quotes"]) >= 2 and all(e["match"] for e in quotes) and len(texts) >= 2 \
            and not any(e["source"] == "ai_generated" for e in quotes)
        conflicts.append({"note": cf["note"], "quotes": quotes, "verified": ok, "issues": issues})

    counts = Counter(st["result"] for st in statements)
    flagged_values = {m.value for st in statements if st["result"] in FLAGGED
                      for m in text_mentions(st["prose_text"], [])}
    uncovered = [n for n, v in zip(checked.numbers_unverified, checked.numbers_unverified_values)
                 if v not in flagged_values and not _is_reference_number(str(v), prose)]
    checked_quotes = [q for st in statements if st["result"] != "unsupported" for q in st["quotes"]]
    covered = sum(1 for t in ptoks if any(a <= t[1] < b for a, b in taken))
    abstains = prose_abstains(prose)
    n_ok = counts["verified"] + counts["derived_ok"]
    if abstains and n_ok == 0:
        status = "not_found"
    elif obj is None:
        status = "partial"
    elif any(cf["verified"] for cf in conflicts):
        status = "conflict"
    elif abstains or counts["unverified"] or counts["unsupported"] or uncovered or not statements:
        status = "partial"
    else:
        status = "answered"
    verification = {
        "parsed": obj is not None,
        "statements_total": len(statements), "verified": counts["verified"], "derived": counts["derived_ok"],
        "unverified": counts["unverified"], "unsupported": counts["unsupported"], "meta": counts["meta"],
        "dropped": len(dropped),
        "quotes_ok": sum(1 for q in checked_quotes if q["match"]),
        "quotes_failed": sum(1 for q in checked_quotes if not q["match"]),
        "conflicts_verified": sum(1 for cf in conflicts if cf["verified"]),
        "numbers_unverified": list(checked.numbers_unverified),
        "numbers_outside_statements": uncovered,
        "prose_coverage": round(covered / len(ptoks), 3) if ptoks else None,
        "abstains": abstains,
        "problems": list(problems),
    }
    annotated = annotate(prose, statements, uncovered, conflicts)
    if obj is None:
        annotated += "\n\n⚠[verification failed: " + "; ".join(problems) + "]"
    return {"status": status, "answer": prose, "statements": statements, "conflicts": conflicts,
            "dropped_statements": dropped, "verification": verification, "annotated": annotated,
            "answer_strict": prose if status == "not_found" else strict_answer(prose, statements)}


# --------------------------------------------------------------------------- step 2: the verification call

class AnswerVerifier(CachedCaller):
    """One verification call per (model, prompt, context, question, prose), cached in
    <chunks>/answer_verify_cache.json, then verify_prose()."""

    what = "verification"
    prompt_version = VERIFY_PROMPT_VERSION

    def key(self, context_text: str, question: str, prose: str) -> str:
        return sha(json.dumps([self.model, VERIFY_PROMPT_VERSION, VERIFY_PROMPT_HASH, sha(context_text), question,
                               sha(prose)], ensure_ascii=False))

    def run(self, question: str, context: Any, prose: str, context_text: str | None = None) -> dict:
        blocks = context_blocks(context)
        text = context_text if context_text is not None else context.render()
        messages = [{"role": "system", "content": VERIFY_SYSTEM},
                    {"role": "user", "content": verify_user_message(text, question, prose)}]
        calls_before = self.calls
        content, meta, cached = self._call(self.key(text, question, prose), verify_request_body(self.model, messages))
        try:
            obj, problems = coerce_verification(parse_json(content))
        except ValueError:
            obj, problems = None, ["the verifier reply is not valid JSON"]
        res = verify_prose(prose, obj, problems, blocks, question)
        res["context_blocks"] = [{"n": b.n, "section": b.section or "Front matter", "pages": b.pages, "kind": b.kind}
                                 for b in blocks]
        res["meta"] = {"verify_model": self.model, "verify_prompt_version": VERIFY_PROMPT_VERSION,
                       "verify_api_calls": self.calls - calls_before, "verify_cached": cached,
                       "dropped_params": meta.get("dropped") or [], "provider": meta.get("provider"),
                       "finish_reason": meta.get("finish_reason"), "context_tokens": getattr(context, "tokens", None)}
        return res


def answer_then_verify(question: str, out_dir: str | Path = "output", model: str | None = None,
                       verify_model: str = VERIFY_MODEL, k: int = PRODUCTION_K, client: Any = None, *,
                       context: Any = None, allow_api: bool = True, prose_cache: str | Path | None = None,
                       verify_cache: str | Path | None = None) -> dict:
    """The production prose answer (unchanged), then one verification call and deterministic checks.

    model: the prose model (default eval_answers.ANSWER_MODEL). Caches default to
    <out_dir>/chunks/answer_cache.json (prose, shared with eval_answers / eval_v1) and
    <out_dir>/chunks/answer_verify_cache.json. Raises OfflineMiss when a call is needed but not allowed."""
    if context is None:
        context = retrieve_context(question, out_dir, k, allow_api=allow_api)
    chunks = Path(out_dir) / "chunks"
    text = context.render()
    pa = ProseAnswerer(prose_cache if prose_cache is not None else chunks / PROSE_CACHE_NAME, model, client, allow_api)
    prose, prose_cached = pa.get(question, text)
    va = AnswerVerifier(verify_model, verify_cache if verify_cache is not None else chunks / VERIFY_CACHE_NAME,
                        client, allow_api)
    res = va.run(question, context, prose, context_text=text)
    res["meta"].update(prose_model=pa.model, prose_cached=prose_cached, prose_key=pa.key(text, question),
                       api_calls=pa.calls + va.calls)
    return res


# --------------------------------------------------------------------------- eval_v1 integration

def verify_record(res: dict, prose_key: str, prose_model: str, verify_model: str) -> dict:
    """The per-question line of answer_verify_status.json."""
    v = res["verification"]
    return {"status": res["status"], "statements": v["statements_total"], "verified": v["verified"],
            "derived": v["derived"], "unverified": v["unverified"], "unsupported": v["unsupported"],
            "dropped": v["dropped"], "quotes_ok": v["quotes_ok"], "quotes_failed": v["quotes_failed"],
            "conflicts_verified": v["conflicts_verified"], "numbers_unverified": len(v["numbers_unverified"]),
            "numbers_outside_statements": len(v["numbers_outside_statements"]), "parsed": v["parsed"],
            "prose_coverage": v["prose_coverage"], "strict_changed": res["answer_strict"] != res["answer"],
            "items": [{"result": st["result"], "text": st["prose_text"], "issues": st["issues"]}
                      for st in res["statements"]],
            "conflicts": [{"note": cf["note"], "verified": cf["verified"]} for cf in res["conflicts"]],
            "prose_key": prose_key, "prose_sha256": sha(res["answer"]), "prose_model": prose_model,
            "verify_model": verify_model, "prompt_version": VERIFY_PROMPT_VERSION}


class EvalVerifyAnswerer:
    """eval_v1 answerer: .get(question, context_text) -> the prose (strict: without its flagged statements)
    | None (cache miss). Records per-question verification in <chunks>/answer_verify_status.json."""

    def __init__(self, recorder: RecordingAssembler, cache_dir: Path, prose_model: str | None, verify_model: str,
                 allow_api: bool, doc: str = "", strict: bool = False, numbers_only: bool = False):
        self.recorder = recorder
        self.prose = ProseAnswerer(Path(cache_dir) / PROSE_CACHE_NAME, prose_model, allow_api=allow_api)
        self.verifier = AnswerVerifier(verify_model, Path(cache_dir) / VERIFY_CACHE_NAME, allow_api=allow_api)
        self.strict = strict
        self.name = (f"{VERIFY_LOG_PREFIX}{'_strict' if strict else ''}:{self.prose.model}:{verify_model}:"
                     f"{VERIFY_PROMPT_VERSION}")
        self.status_file = Path(cache_dir) / VERIFY_STATUS_NAME
        self.doc = doc
        self.numbers_only = numbers_only
        self.records: dict[str, dict] = {}
        self.prose_misses: list[str] = []
        self.verify_misses: list[str] = []
        atexit.register(self.report)

    def get(self, question: str, context: str) -> str | None:
        entry = self.recorder.contexts.get(context)
        if entry is None:
            raise RuntimeError("context was not built by answer:env_factory_verify's assembler")
        qid, ctx = entry
        qkey = qid or sha(question)[:12]
        try:
            prose, _ = self.prose.get(question, context)
        except OfflineMiss:
            self.prose_misses.append(qkey)
            return None
        try:
            res = self.verifier.run(question, ctx, prose, context_text=context)
        except OfflineMiss:
            self.verify_misses.append(qkey)
            return None
        self.records[qkey] = verify_record(res, self.prose.key(context, question), self.prose.model,
                                           self.verifier.model)
        self._save()
        return res["answer_strict"] if self.strict else res["answer"]

    def _save(self) -> None:
        old = json.loads(self.status_file.read_text(encoding="utf-8")) if self.status_file.exists() else {}
        old.update(self.records)
        tmp = self.status_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(old, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.status_file)

    def report(self) -> None:
        recs = list(self.records.values())
        pm, vm = len(self.prose_misses), len(self.verify_misses)
        if not recs and not pm and not vm:
            return
        st = Counter(r["status"] for r in recs)
        tot: Counter = Counter()
        for r in recs:
            tot.update({k: int(r[k]) for k in ("statements", "verified", "derived", "unverified", "unsupported",
                                               "dropped", "quotes_failed", "conflicts_verified", "strict_changed")})
        print(f"{VERIFY_LOG_PREFIX}{' (strict)' if self.strict else ''} {self.doc}: {len(recs)} answers "
              f"({', '.join(f'{s} {st[s]}' for s in STATUSES if st[s]) or '-'}); statements {tot['statements']}: "
              f"verified {tot['verified']}, derived {tot['derived']}, unverified {tot['unverified']}, unsupported "
              f"{tot['unsupported']}, dropped as not in the answer {tot['dropped']}; quotes failed "
              f"{tot['quotes_failed']}; verified conflicts {tot['conflicts_verified']}"
              + (f"; strict text changed in {tot['strict_changed']}" if self.strict else "")
              + f"; API calls made: prose {self.prose.calls}, verification {self.verifier.calls}; cache misses: "
              f"prose {pm}, verification {vm} -> calls still needed: {pm} prose + {pm + vm} verification"
              + ("" if self.numbers_only or not recs else f"; per question: {self.status_file}"))


def _verify_env(doc: str, chunks_path: Any, opts: Any, strict: bool):
    env = env_factory(doc, chunks_path, argparse.Namespace(**{**vars(opts), "answers": False}))
    if env.assembler is None or not getattr(opts, "answers", False):
        return env
    import eval_answers

    prose_model = getattr(opts, "answer_model", None) or eval_answers.ANSWER_MODEL
    verify_model = getattr(opts, "verify_model", None) or VERIFY_MODEL
    env.answerer = EvalVerifyAnswerer(env.assembler, Path(chunks_path).parent, prose_model, verify_model,
                                      bool(getattr(opts, "allow_api", False)), doc=doc, strict=strict,
                                      numbers_only=bool(getattr(opts, "numbers_only", False)))
    env.notes.append(
        f"{VERIFY_LOG_PREFIX}: prose = eval_answers ({prose_model}, cache {PROSE_CACHE_NAME}), verification = "
        f"{verify_model}, prompt {VERIFY_PROMPT_VERSION}; scorer text = "
        + ("the prose WITHOUT its unverified/unsupported statements" if strict else "the unchanged prose")
        + "; the plan's answer count = questions needing a call (1 verification call each when the prose is "
          "cached, 2 calls otherwise; exact split printed at exit)")
    return env


def env_factory_verify(doc: str, chunks_path: Any, opts: Any):
    """`eval_v1.py run ... --k 8 --env-factory answer:env_factory_verify --answers`: production context and
    production prose (scored unchanged, so fact recall = production), plus verification statistics."""
    return _verify_env(doc, chunks_path, opts, strict=False)


def env_factory_verify_strict(doc: str, chunks_path: Any, opts: Any):
    """As env_factory_verify, but the scorer sees the prose without its unverified / unsupported statements
    (same caches: no extra API call)."""
    return _verify_env(doc, chunks_path, opts, strict=True)


# --------------------------------------------------------------------------- verify-report

def _tokset(text: str) -> set[str]:
    return {t for t, _, _ in prose_tokens(text) if t not in _STOP}


def _closest(text: str, items: list[dict], need_numbers: bool) -> tuple[dict | None, float]:
    """The status item whose text shares most of `text`'s tokens (share of text's tokens; >= 0.5 to count).
    need_numbers: the item must contain one of text's numbers (for paraphrases of the answer's claims)."""
    ta = _tokset(text)
    if not ta:
        return None, 0.0
    nums = {t for t in ta if any(ch.isdigit() for ch in t)}
    best, score = None, 0.0
    for it in items:
        tb = _tokset(it.get("text") or "")
        if need_numbers and nums and not nums & tb:
            continue
        s = len(ta & tb) / len(ta)
        if s > score:
            best, score = it, s
    return (best, round(score, 2)) if score >= 0.5 else (None, round(score, 2))


def _flagged(row: dict) -> bool:
    return bool(row.get("unverified") or row.get("unsupported") or row.get("numbers_outside_statements"))


def _print_vstats(label: str, rows: list[dict]) -> None:
    n = len(rows)
    tot: Counter = Counter()
    for r in rows:
        tot.update({k: r.get(k) or 0 for k in ("statements", "verified", "derived", "unverified", "unsupported",
                                                "dropped", "quotes_failed", "conflicts_verified")})
    s = tot["statements"] or 1
    st = Counter(r["status"] for r in rows)
    print(f"{label:28s} {n:4d} {tot['statements'] / max(n, 1):6.1f} {100 * tot['verified'] / s:6.1f} "
          f"{100 * tot['derived'] / s:6.1f} {100 * tot['unverified'] / s:6.1f} {100 * tot['unsupported'] / s:6.1f} "
          f"{tot['dropped']:5d} {tot['quotes_failed']:5d} {tot['conflicts_verified']:4d} "
          f"{sum(_flagged(r) for r in rows):5d}  "
          + ", ".join(f"{k} {st[k]}" for k in STATUSES if st[k]))


def verify_report(run: str, judge_run: str | None = None, show_text: bool = False) -> int:
    """Per document: statements per answer, verified / derived / unverified / unsupported shares, conflicts;
    and, when a judge-scored run of the same answers exists (--judge-run, default j_fin_par_<doc>), whether
    the verification flags co-occur with the judge's contradicted facts and extra claims. Offline: reads
    the run, the status files, the answer cache and the judge cache only."""
    import eval_v1 as ev

    src = ev.run_dir(run)
    if not (src / "results.jsonl").exists():
        print(f"run {run} not found")
        return 2
    summary = json.loads((src / "summary.json").read_text(encoding="utf-8")) if (src / "summary.json").exists() else {}
    chunks_arg = (summary.get("args") or {}).get("chunks")
    if chunks_arg == "(set)":
        print("the run used --chunks (not recorded in numbers-only mode); cannot locate its status file")
        return 2
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for r in ev.read_jsonl(src / "results.jsonl"):
        by_doc[r["doc"]].append(r)
    header = (f"{'doc':28s} {'ans':>4s} {'st/ans':>6s} {'ver%':>6s} {'der%':>6s} {'unv%':>6s} {'uns%':>6s} "
              f"{'drop':>5s} {'qfail':>5s} {'conf':>4s} {'flagd':>5s}  status")
    per_doc: dict[str, list[dict]] = {}
    judge_rows: list[tuple[str, dict]] = []
    for doc, recs in by_doc.items():
        answerer = ((summary.get("components") or {}).get(doc) or {}).get("answerer") or ""
        if answerer and not answerer.startswith(VERIFY_LOG_PREFIX):
            print(f"{doc}: warning: the run's answerer is {answerer!r}, not an answer_verify factory")
        chunks_dir = ev.resolve_chunks(doc, chunks_arg).parent
        sfile = chunks_dir / VERIFY_STATUS_NAME
        status = json.loads(sfile.read_text(encoding="utf-8")) if sfile.exists() else {}
        rows = {r["id"]: status[r["id"]] for r in recs if r["id"] in status}
        missing = [r["id"] for r in recs if r["id"] not in status]
        if missing:
            print(f"{doc}: {len(missing)} question(s) without a verification record: {missing}")
        per_doc[doc] = list(rows.values())
        jdir = ev.run_dir(judge_run or f"j_fin_par_{doc}")
        if (jdir / "results.jsonl").exists() and rows:
            judge_rows.append((doc, _judge_check(ev, jdir, chunks_dir, rows)))
    print(f"\nanswer-then-verify report for run {src.name} (verify prompt "
          f"{', '.join(sorted({r.get('prompt_version', '?') for rs in per_doc.values() for r in rs})) or '-'})")
    print("columns: answers, statements per answer, % of statements verified / derived_ok / unverified / "
          "unsupported, statements dropped as not in the answer, failed quotes, verified conflicts, answers "
          "flagged (an unverified/unsupported statement or a number outside every statement)\n")
    print(header)
    for doc, rows in per_doc.items():
        _print_vstats(doc, rows)
    if len(per_doc) > 1:
        _print_vstats("ALL", [r for rows in per_doc.values() for r in rows])
    for doc, jr in judge_rows:
        _print_judge(doc, jr, show_text)
    if len(judge_rows) > 1:
        pooled = {"run": "pooled", "confirmed": 0, "judged": 0, "unconfirmed": [], "contra_q": 0, "contra_q_flagged": 0,
                  "flagged_q": 0, "flagged_q_contra": 0, "flagged_q_extra": 0, "extra_q": 0, "contra": Counter(),
                  "extra": Counter(), "contra_items": [], "extra_total": 0}
        for _, jr in judge_rows:
            for k, v in jr.items():
                if isinstance(v, Counter):
                    pooled[k].update(v)
                elif isinstance(v, int) and k in pooled:
                    pooled[k] += v
                elif k == "unconfirmed":
                    pooled[k] += v
        _print_judge("ALL", pooled, False)
    return 0


def _judge_check(ev: Any, jdir: Path, chunks_dir: Path, rows: dict) -> dict:
    """Co-occurrence of the verification flags with one judge-scored run's verdicts, on the questions whose
    judged answer is provably the verified prose (its judge cache entry exists for that prose)."""
    jsum = json.loads((jdir / "summary.json").read_text(encoding="utf-8"))
    jrecs = {r["id"]: r for r in ev.read_jsonl(jdir / "results.jsonl")}
    gold_path = Path(jsum.get("gold") or "")
    gold_path = gold_path if gold_path.is_absolute() else ev.ROOT / gold_path
    gold = {q["id"]: q for q in ev.load_gold(gold_path) if isinstance(q, dict)} if gold_path.is_file() else {}
    judge = ev.Judge(jsum.get("judge") or ev.JUDGE_MODEL, chunks_dir / "judge_cache.json")
    pfile = chunks_dir / PROSE_CACHE_NAME
    prose_cache = json.loads(pfile.read_text(encoding="utf-8")) if pfile.exists() else {}
    out = {"run": jdir.name, "confirmed": 0, "judged": 0, "unconfirmed": [], "contra_q": 0, "contra_q_flagged": 0,
           "flagged_q": 0, "flagged_q_contra": 0, "flagged_q_extra": 0, "extra_q": 0, "contra": Counter(),
           "extra": Counter(), "contra_items": [], "extra_total": 0}
    for qid, row in rows.items():
        verdicts = ((jrecs.get(qid) or {}).get("info") or {}).get("judge_verdicts")
        if verdicts is None:
            continue
        out["judged"] += 1
        q, prose = gold.get(qid), prose_cache.get(row.get("prose_key"))
        hit = judge.cache.get(judge.key(q, prose)) if q is not None and prose is not None else None
        if hit is None:  # the judged answer is not provably the verified prose: left out
            out["unconfirmed"].append(qid)
            continue
        out["confirmed"] += 1
        items = row.get("items") or []
        flagged = _flagged(row)
        facts = ev.facts_of(q)
        contra = [i for i, v in enumerate(verdicts) if v == "contradicted"]
        extra = hit.get("extra_claims") or []
        out["flagged_q"] += flagged
        out["contra_q"] += bool(contra)
        out["contra_q_flagged"] += bool(contra) and flagged
        out["flagged_q_contra"] += flagged and bool(contra)
        out["flagged_q_extra"] += flagged and bool(extra)
        out["extra_q"] += bool(extra)
        out["extra_total"] += len(extra)
        for i in contra:
            fact = ev.fact_payload(facts[i]) if i < len(facts) else {"id": str(i), "fact": ""}
            it, score = max((_closest(t, items, need_numbers=False)
                             for t in [fact["fact"], *fact.get("also_acceptable", [])]), key=lambda x: x[1])
            res = it["result"] if it else "no match"
            out["contra"][res] += 1
            out["contra_items"].append({"qid": qid, "fact": fact["id"], "result": res, "score": score,
                                        "statement": (it or {}).get("text", ""), "issues": (it or {}).get("issues", [])})
        for e in extra:
            it, _ = _closest(e, items, need_numbers=True)
            out["extra"][it["result"] if it else "no match"] += 1
    return out


def _print_judge(doc: str, jr: dict, show_text: bool) -> None:
    def dist(c: Counter) -> str:
        return ", ".join(f"{k} {c[k]}" for k in (*RESULTS, "no match") if c[k]) or "-"

    def pct(a: int, b: int) -> str:
        return f"{a}/{b}" + (f" ({100 * a / b:.0f}%)" if b else "")

    print(f"\njudge cross-check {doc} ({jr['run']}): answers judged {jr['judged']}, the same prose confirmed via "
          f"the judge cache {jr['confirmed']}" + (f" (left out: {jr['unconfirmed']})" if jr["unconfirmed"] else ""))
    print(f"  answers with a judge-contradicted fact flagged by verification: {pct(jr['contra_q_flagged'], jr['contra_q'])}")
    print(f"  flagged answers with a judge-contradicted fact: {pct(jr['flagged_q_contra'], jr['flagged_q'])}; "
          f"with judge extra claims: {pct(jr['flagged_q_extra'], jr['flagged_q'])} (answers with extra claims "
          f"overall: {jr['extra_q']}/{jr['confirmed']})")
    print(f"  contradicted facts -> closest statement: {dist(jr['contra'])}")
    print(f"  judge extra claims ({jr['extra_total']}) -> closest statement: {dist(jr['extra'])}")
    for it in jr.get("contra_items") or []:
        line = f"    {it['qid']} fact {it['fact']}: {it['result']} (overlap {it['score']})"
        if show_text and it["statement"]:
            line += f" | {it['statement'][:160]}" + (f" | {'; '.join(it['issues'])[:200]}" if it["issues"] else "")
        print(line)


# =========================================================================== CLI

def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["verify-report"]:
        p = argparse.ArgumentParser(prog="answer.py verify-report",
                                    description="Offline report of an answer-then-verify eval run.")
        p.add_argument("run", help="eval run (reports/eval/runs/<RUN>) made with answer:env_factory_verify[_strict]")
        p.add_argument("--judge-run", default=None,
                       help="judge-scored run of the same prose answers (default: j_fin_par_<doc> when it exists)")
        p.add_argument("--show-text", action="store_true",
                       help="also print the statement text next to each judge-contradicted fact (local use only)")
        args = p.parse_args(argv[1:])
        return verify_report(args.run, args.judge_run, args.show_text)
    p = argparse.ArgumentParser(description="Grounded, verified answer to a question about one protocol "
                                            "(also: `answer.py verify-report RUN [--judge-run RUN]`).")
    p.add_argument("out_dir", help="the protocol's output folder (with chunks/), e.g. output or output_public/<name>")
    p.add_argument("question")
    p.add_argument("--model", default=None, help=f"answer model (default {DEFAULT_MODEL}; with --verify: the "
                                                 "production prose model, eval_answers.ANSWER_MODEL)")
    p.add_argument("--verify", action="store_true",
                   help="answer, then verify: the production prose answer plus one verification call")
    p.add_argument("--verify-model", default=VERIFY_MODEL)
    p.add_argument("-k", type=int, default=PRODUCTION_K, help="reranked children in the context")
    args = p.parse_args(argv)
    try:
        if args.verify:
            res = answer_then_verify(args.question, args.out_dir, model=args.model, verify_model=args.verify_model,
                                     k=args.k, allow_api=not offline())
        else:
            res = answer(args.question, args.out_dir, model=args.model or DEFAULT_MODEL, k=args.k,
                         allow_api=not offline())
    except OfflineMiss as exc:
        print(f"refusing: {exc}; unset OPENROUTER_OFFLINE to allow the call(s)", file=sys.stderr)
        return 3
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
