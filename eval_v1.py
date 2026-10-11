"""Evaluation harness v1: stage-wise metrics for the protocol RAG pipeline.

Gold questions are anchored to verbatim evidence quotes from the PDF text layer,
not to chunk ids, so they survive re-chunking (format: reports/eval/README.md).

  validate --gold F        schema check, duplicate ids, counts by category/kind/split/USDM class
  run --gold F             per-question stage metrics -> reports/eval/runs/<RUN>/{results.jsonl,summary.json}
  rescore RUN [--gold F]   recompute a run's metrics from the caches (new gold / scorer, no API) -> RUN_rescored
  compare RUN_A RUN_B      paired comparison (bootstrap CI, McNemar, flip list) and the regression gates
  convert-legacy           reports/retrieval_eval.json (+ answer_eval.json) -> gold format (dev split)

Stages (a failing question is attributed to the first stage that fails):
  S0 extraction   evidence quote in the converted document (Docling text items, table cells) and
                  inside a single chunk
  S1 candidates   a full evidence group inside the rerank candidate pool
  S2 ranking      evidence recall@1/3/5/10 at chunk level and at context-unit level (small-to-big:
                  table part -> whole table, figure part -> whole figure), full-group recall, MRR@10
  S3 context      a full evidence group in the rendered build_context() text; context tokens
  S4 answer       (--answers) fact recall, critical facts, forbidden facts, numeric exactness, list
                  precision, abstention / false abstention, unverified numbers

Quotes are matched after normalisation (NFKC, dash/quote/comparator variants, whitespace, case),
then with spaces and hyphens removed ("nospace": flattened superscripts "10 9 /L" = "109/L",
dropped line-end hyphens, lost spaces): the "strict" level. A quote that spans table cells is
located in the Docling tables (S0) and a text contains it at the "cells" level when each touched
row's label and the quote's cells of that row share a line, whatever the serialization (chunk rows,
Markdown). The primary metrics use the cells level (= strict for text quotes); *_strict and
*_fuzzy (local token-set or partial ratio >= 0.9, numbers must match) are reported beside them.

Answer facts match through their gold "key_terms" (AND of OR-groups, numbers on token boundaries,
one spelling for powers of ten) when present, else through text/alts/value. An optional LLM judge
(--judge) gives per-fact verdicts, cached; its agreement with the deterministic check is Cohen's kappa.

API use: none by default. Query embeddings, reranks and answers are read from the pipeline's caches
(output/chunks/*_cache.json); misses are listed, and --plan counts the API calls a run would need.
Only --allow-api fills misses through OpenRouter (zero-data-retention routing, as retrieve.py and
eval_answers.py do); OPENROUTER_OFFLINE=1 always wins. `rescore` never calls retrieval or answer APIs.

--numbers-only prints (and stores) only aggregates and question ids: no question, quote, answer or
chunk text, no chunk ids or paths; errors are the exception type and our file:line only. Use it for
the P-protocols.

Pluggability: a run evaluates a DocEnv built by an environment factory (--env-factory module:function,
default eval_v1:default_env). A DocEnv holds a retrieval pipeline (.prepare/.retrieve), a context-unit
expander (.unit) and a context assembler (.build), so a different partition (e.g. parent-child) can be
evaluated by supplying those objects; the scoring code only sees chunk dicts and texts.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import math
import os
import re

import keyterm_match
import sys
import time
import traceback
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

from project_paths import main_pdf

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "reports" / "eval" / "runs"
MAIN_CHUNKS = ROOT / "output" / "chunks" / "chunks.jsonl"
MAIN_PDF = main_pdf()

KS = (1, 3, 5, 10)
FUZZY_THRESHOLD = 0.9
FUZZY_MIN_CHARS = 12  # shorter quotes are never fuzzy-matched (too many near misses)
SEED = 20261006
N_BOOT = 10_000
GATE_MIN_DELTA = -0.02  # lower bound of the paired 95% CI of B - A, in proportion units (-2 points)
GATE_METRICS = {  # audit §7.8 gate 1
    "s2_chunk_recall@5": "evidence recall@5",
    "s3_sufficient": "context sufficiency",
    "s4_fact_recall": "fact recall",
}
TOKEN_GROWTH_FLAG = 0.25  # audit §7.8 gate 5 (flag only)
ABSTAIN_MIN = 0.90
FALSE_ABSTAIN_MAX = 0.05

KINDS = {"lookup", "enumeration", "multi_evidence", "cross_reference", "unanswerable"}
SPLITS = {"dev", "test"}
MODALITIES = {"text", "list", "table", "figure"}
COMPARATORS = {"≥", "≤", ">", "<", "=", "±"}


# =========================================================================== normalisation

_TRANS = str.maketrans({
    **dict.fromkeys("‐‑‒–—―−⁃﹘﹣－", "-"),
    **dict.fromkeys("‘’‚‛′´`", "'"),
    **dict.fromkeys("“”„‟″«»", '"'),
    **dict.fromkeys("≧⩾", "≥"),
    **dict.fromkeys("≦⩽", "≤"),
    **dict.fromkeys("×✕✖⨯", "x"),
    **dict.fromkeys("^­​‌‍⁠﻿", ""),
})
_COMPACT_DROP = re.compile(r"[\s\-]+")
_TOK = re.compile(r"[^\W_]+|[≥≤<>%±]")


@lru_cache(maxsize=200_000)
def normalize(text: str) -> str:
    """NFKC, unified dashes/quotes/comparators/multiplication sign, no carets, casefold, one space."""
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANS)
    t = t.replace(">=", "≥").replace("<=", "≤").casefold()
    return " ".join(t.split())


def compact(text: str) -> str:
    """normalize() without whitespace and hyphens (second matching pass)."""
    return _COMPACT_DROP.sub("", normalize(text))


@dataclass
class Prepared:
    norm: str
    comp: str
    raw: str = ""
    _tokens: list | None = None
    _lines: list | None = None
    _kt: tuple[str, str] | None = None

    def tokens(self) -> list[tuple[str, int, int]]:
        if self._tokens is None:
            self._tokens = [(m.group(), m.start(), m.end()) for m in _TOK.finditer(self.norm)]
        return self._tokens

    def lines(self) -> list[Prepared]:
        """The non-blank lines of the text (a table row is one line in every serialization)."""
        if self._lines is None:
            parts = [ln for ln in self.raw.splitlines() if ln.strip()]
            self._lines = [self] if len(parts) <= 1 else [prep(ln) for ln in parts]
        return self._lines

    def kt(self) -> tuple[str, str]:
        """(key-term canonical form, the same without spaces and hyphens); see kt_canon()."""
        if self._kt is None:
            c = kt_canon(self.raw)
            self._kt = (c, _COMPACT_DROP.sub("", c))
        return self._kt


_PREP: dict[str, Prepared] = {}


def prep(text: str) -> Prepared:
    p = _PREP.get(text)
    if p is None:
        n = normalize(text)
        p = Prepared(n, _COMPACT_DROP.sub("", n), text or "")
        if len(_PREP) < 100_000:
            _PREP[text] = p
    return p


# Key-term canonical form (fact scoring): normalize() plus one spelling for powers of ten and
# tighter spacing around comparators and slashes. "10^9", "10⁹", "×10⁹", "× 10 9 /L" (PDF text layer)
# and "109/L" (flattened superscript) all become "10e9".
_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_EXP_SUP = re.compile(r"10\s*([⁻⁺]?[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,2})(?![⁰¹²³⁴⁵⁶⁷⁸⁹])")
_EXP_CARET = re.compile(r"10\s*(?:\^|\*\*)\s*\(?([-+−]?\d{1,2})\)?")
_EXP_FLAT_UNIT = re.compile(r"(?<![\d.,])10 ?([1-9]|1[0-9])(?= ?/)")  # "109/l", "10 9 /l"
_EXP_FLAT_TIMES = re.compile(r"(?<![a-z])x ?10 ?([1-9]|1[0-9])(?![\d.,])")  # "x 10 9 cells", "x109"
_EXP_TIMES = re.compile(r"\s*(?<![a-z])x\s*(?=10e)")


@lru_cache(maxsize=50_000)
def kt_canon(text: str) -> str:
    t = _EXP_SUP.sub(lambda m: "10e" + m.group(1).translate(_SUPERSCRIPT), text or "")
    t = _EXP_CARET.sub(lambda m: "10e" + m.group(1).replace("−", "-"), t)
    t = normalize(t)
    t = _EXP_FLAT_UNIT.sub(r"10e\1", t)
    t = _EXP_FLAT_TIMES.sub(r"x10e\1", t)
    t = _EXP_TIMES.sub(" ", t)
    t = re.sub(r"\s*/\s*", "/", t)
    t = re.sub(r"([≥≤<>=±~])\s+", r"\1", t)
    return " ".join(t.split())


def partial_ratio(short: str, long: str) -> float:
    """Best SequenceMatcher ratio of `short` against a same-length window of `long` (fuzzywuzzy-style)."""
    if not short or not long:
        return 0.0
    if len(short) > len(long):
        short, long = long, short
    m, best = len(short), 0.0
    for a, b, size in SequenceMatcher(None, short, long, autojunk=False).get_matching_blocks():
        if not size:
            continue
        start = max(0, min(b - a, len(long) - m))
        best = max(best, SequenceMatcher(None, short, long[start:start + m], autojunk=False).ratio())
        if best >= 0.999:
            break
    return best


def _digit_runs(text: str) -> list[str]:
    return re.findall(r"\d+", text)


def fuzzy_score(q: Prepared, t: Prepared) -> float:
    """Local fuzzy similarity of quote q inside text t, in [0, 1].

    The window of t with the most quote tokens (multiset) is located; the score is the larger of
    the token coverage in that window ("token-set" style) and the partial character ratio against
    it. Every digit run of the quote must occur in the window, so 25 never fuzzy-matches 75.
    """
    qt = [x[0] for x in q.tokens()]
    tt = t.tokens()
    if len(qt) < 2 or not tt:
        return 0.0
    need = Counter(qt)
    width = min(len(tt), max(len(qt), round(len(qt) * 1.25)))
    have: Counter = Counter()
    overlap = best = 0
    best_start = 0
    for i, (tok, _, _) in enumerate(tt):
        have[tok] += 1
        if have[tok] <= need.get(tok, 0):
            overlap += 1
        if i >= width:
            old = tt[i - width][0]
            if have[old] <= need.get(old, 0):
                overlap -= 1
            have[old] -= 1
        if overlap > best:
            best, best_start = overlap, max(0, i - width + 1)
    token_score = best / len(qt)
    if token_score < 0.5:
        return token_score
    s = tt[best_start][1]
    e = tt[min(len(tt) - 1, best_start + width - 1)][2]
    span = t.norm[max(0, s - 15):e + 15]
    span_digits = "".join(_digit_runs(span))
    if any(run not in span_digits for run in _digit_runs(q.comp)):
        return 0.0
    return max(token_score, partial_ratio(q.norm, span))


def match_level(q: Prepared, t: Prepared, fuzzy: bool = True) -> str | None:
    """'exact' | 'nospace' | 'fuzzy' | None for quote q in text t."""
    if not q.norm:
        return None
    if q.norm in t.norm:
        return "exact"
    if q.comp and q.comp in t.comp:
        return "nospace"
    if fuzzy and len(q.comp) >= FUZZY_MIN_CHARS and fuzzy_score(q, t) >= FUZZY_THRESHOLD:
        return "fuzzy"
    return None


STRICT = ("exact", "nospace")
LEVELS = ("strict", "cells", "fuzzy")  # nested: strict ⊆ cells ⊆ fuzzy; "cells" is the primary level


# =========================================================================== table evidence (cells)
#
# A PDF quote of table cells ("Neurologic exam X X See Section 8.2.2") is rarely contiguous in a
# serialization of the table: chunks write "Label: Column = value; ...", the context writes Markdown
# rows "| a | b |". In S0 each quote is therefore located in the Docling tables: a run of consecutive
# cells in row-major or column-major reading order whose texts, concatenated, spell the quote (spaces
# and hyphens ignored; the first and last cell may be cut). Spanning cells are optional in the run
# (the PDF prints a merged cell once) and a horizontally merged cell counts once. A text then
# "contains" the quote at the cells level when, for every table row the quote touches, some line of
# the text holds that row's label (first-column cell) and the quote's cells of that row (any order,
# any format: one table row is one line in the chunk, Markdown and Docling-row serializations);
# column-header cells may be anywhere in the text. Single-cell quotes need no cell matching.

PART_MIN = 3  # compact characters of a cut first/last cell (shorter cuts are noise)
MAX_LOCATIONS = 30  # table locations kept per quote
LABEL_KEY = 50  # long row labels are compared by their first characters (chunk continuations shorten them)


@dataclass
class TCell:
    text: str  # cell text, whitespace collapsed
    row: int  # start row / column of the (possibly spanning) cell
    col: int
    row_span: int = 1
    col_span: int = 1
    header: bool = False  # column header


@dataclass
class TTable:
    ref: str
    grid: list[list[TCell]]  # grid[r][c]; a spanning cell is repeated in every position it covers
    _seq: dict = field(default_factory=dict)

    def header_row(self, r: int) -> bool:
        cells = [c for c in self.grid[r] if c.text]
        return bool(cells) and all(c.header for c in cells)

    def label(self, r: int) -> str | None:
        """The row label as the chunk serializer writes it: the first-column cell."""
        if len(self.grid[r]) < 2 or self.header_row(r):
            return None
        return self.grid[r][0].text or None

    def seq(self, order: str) -> list[tuple[int, int, str, bool]]:
        """(row, col, compact text, optional) in reading order; spanning cells are optional."""
        if order not in self._seq:
            grid, out = self.grid, []
            n_rows, n_cols = len(grid), max(len(r) for r in grid)
            outer, inner = (n_rows, n_cols) if order == "row" else (n_cols, n_rows)
            for a in range(outer):
                prev = None
                for b in range(inner):
                    r, c = (a, b) if order == "row" else (b, a)
                    if c >= len(grid[r]):
                        continue
                    cell = grid[r][c]
                    key = (cell.row, cell.col)
                    if key == prev:  # the same merged cell again along the reading direction
                        continue
                    prev = key
                    comp = compact(cell.text)
                    if comp:
                        out.append((r, c, comp, cell.row_span > 1 or cell.col_span > 1))
            self._seq[order] = out
        return self._seq[order]


@dataclass(frozen=True)
class Frag:
    """A piece of a quote that must appear in a text: a whole cell (norm) or a cut cell (compact)."""
    text: str
    whole: bool


@dataclass(frozen=True)
class TableLoc:
    table: str
    order: str  # "row" | "col"
    cells: tuple  # ((row, col, cell text, fragment text), ...)
    records: tuple  # ((row label or None, (Frag, ...)), ...): one per data row touched
    free: tuple  # Frags of column-header cells (anywhere in the text)

    @property
    def frags(self) -> list[Frag]:
        return [f for _, fs in self.records for f in fs] + list(self.free)


def docling_tables(doc: Any) -> list[TTable]:
    """The Docling document's tables as plain grids (TableItem.data.grid)."""
    out = []
    for item in doc.tables:
        grid = item.data.grid
        if not grid or not grid[0]:
            continue
        out.append(TTable(item.self_ref, [[TCell(" ".join((c.text or "").split()), c.start_row_offset_idx,
                                                  c.start_col_offset_idx, c.row_span, c.col_span,
                                                  bool(c.column_header)) for c in row] for row in grid]))
    return out


def _runs_from(seq: list, i: int, q: str, pos: int, acc: list, out: list) -> None:
    """Extend a cell run over seq[i:] that spells q[pos:] (depth-first; optional cells may be skipped)."""
    if len(out) >= 4 * MAX_LOCATIONS:
        return
    if pos == len(q):
        out.append(list(acc))
        return
    if i >= len(seq):
        return
    t, rest = seq[i][2], q[pos:]
    if rest.startswith(t):
        acc.append((i, t, True))
        _runs_from(seq, i + 1, q, pos + len(t), acc, out)
        acc.pop()
    elif len(rest) >= PART_MIN and rest in t:  # the last cell, cut (a wrapped cell read line by line)
        out.append(acc + [(i, rest, False)])
    if seq[i][3]:
        _runs_from(seq, i + 1, q, pos, acc, out)


def locate_in_table(quote: str, table: TTable) -> tuple[list[TableLoc], bool]:
    """(multi-cell locations of the quote in the table, the quote lies inside a single cell).

    Middle cells of a run are whole; the first and last may be cut (the quote starts or ends inside
    them, or reads one line of a wrapped cell)."""
    q = compact(quote)
    if len(q) < PART_MIN:
        return [], False
    single, locs, seen = False, [], set()
    for order in ("row", "col"):
        seq = table.seq(order)
        for i0, (_, _, t, _) in enumerate(seq):
            if q in t:
                single = True
                continue
            runs: list = []
            start = t.find(q[0])
            while start != -1:  # the quote starts inside this cell
                k = len(os.path.commonprefix([t[start:], q]))
                whole = start == 0 and k == len(t)
                if k < len(q) and (whole or k >= PART_MIN):
                    _runs_from(seq, i0 + 1, q, k, [(i0, t[start:start + k], whole)], runs)
                start = t.find(q[0], start + 1)
            for run in runs:
                cells = tuple((seq[i][0], seq[i][1]) for i, _, _ in run)
                if len(run) < 2 or cells in seen:
                    continue
                seen.add(cells)
                locs.append(_table_loc(table, order, run, seq))
    return locs, single


def _table_loc(table: TTable, order: str, run: list, seq: list) -> TableLoc:
    by_row: dict[int, list[Frag]] = defaultdict(list)
    free: list[Frag] = []
    cells = []
    for i, frag, whole in run:
        r, c = seq[i][0], seq[i][1]
        text = table.grid[r][c].text
        f = Frag(text if whole else frag, whole)
        cells.append((r, c, text, text if whole else frag))
        (free if table.header_row(r) else by_row[r]).append(f)
    records = tuple((table.label(r), tuple(fs)) for r, fs in sorted(by_row.items()))
    return TableLoc(table.ref, order, tuple(cells), records, tuple(free))


class TableIndex:
    """Quote -> table locations for one document (cached per quote)."""

    def __init__(self, tables: list[TTable] | None):
        self.tables = list(tables or [])
        self._cache: dict[str, tuple[list[TableLoc], bool]] = {}

    def locate(self, quote: str) -> tuple[list[TableLoc], bool]:
        """(multi-cell locations, at most MAX_LOCATIONS; the quote lies in a table at all)."""
        if quote not in self._cache:
            locs: list[TableLoc] = []
            in_table = False
            for t in self.tables:
                tl, single = locate_in_table(quote, t)
                locs += tl
                in_table = in_table or single or bool(tl)
            self._cache[quote] = (locs[:MAX_LOCATIONS], in_table)
        return self._cache[quote]


@lru_cache(maxsize=50_000)
def _bounded(norm: str) -> re.Pattern:
    left = r"(?<![0-9a-z])" if norm[:1].isalnum() else ""
    right = r"(?![0-9a-z])" if norm[-1:].isalnum() else ""
    return re.compile(left + re.escape(norm) + right)


def frag_in(f: Frag, t: Prepared) -> bool:
    """A whole cell: on word boundaries (so cell "X" is not the x of "exam"); a cut cell: compact
    substring. Long cells also match with spaces and hyphens removed."""
    if not f.whole:
        return compact(f.text) in t.comp
    n = normalize(f.text)
    if not n:
        return True
    if _bounded(n).search(t.norm):
        return True
    c = compact(f.text)
    return len(c) >= 8 and c in t.comp


def label_in(label: str, t: Prepared) -> bool:
    c = compact(label)
    if len(c) > LABEL_KEY:
        return c[:LABEL_KEY] in t.comp
    return frag_in(Frag(label, True), t)


def loc_in(loc: TableLoc, t: Prepared) -> bool:
    """Does text t contain this table location (each touched row's label and cells on one line)?"""
    if any(compact(f.text) not in t.comp for f in loc.frags):
        return False
    if any(lab and compact(lab)[:LABEL_KEY] not in t.comp for lab, _ in loc.records):
        return False
    lines = t.lines()
    for lab, frags in loc.records:
        if lab is None:
            if not any(all(frag_in(f, ln) for f in frags) for ln in lines):
                return False
        else:
            cand = [ln for ln in lines if label_in(lab, ln)]
            if not cand or not all(any(frag_in(f, ln) for ln in cand) for f in frags):
                return False
    return all(frag_in(f, t) for f in loc.free)


@dataclass
class QuoteSpec:
    """One evidence quote with its table locations (empty for text quotes)."""
    text: str
    p: Prepared
    locs: list[TableLoc] = field(default_factory=list)
    in_table: bool = False

    def cells_in(self, t: Prepared) -> bool:
        return any(loc_in(loc, t) for loc in self.locs)


def quote_specs(quotes: list[str], index: TableIndex | None = None) -> list[QuoteSpec]:
    out = []
    for x in quotes:
        locs, in_table = index.locate(x) if index is not None else ([], False)
        out.append(QuoteSpec(x, prep(x), locs, in_table))
    return out


# =========================================================================== gold schema

def load_gold(path: str | Path) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "questions" in data:
        data = data["questions"]
    return data


def evidence_groups(q: dict) -> list[list[str]]:
    """Alternatives (any one suffices), each the quotes that are all needed."""
    return [[e.get("quote") or "" for e in g.get("all_of") or []] for g in q.get("evidence") or []]


def facts_of(q: dict) -> list[dict]:
    return list(((q.get("answer") or {}).get("facts")) or [])


def key_terms_error(kt: Any) -> str | None:
    """None if `kt` is a valid key_terms value: a list of non-empty lists of non-empty strings."""
    if not isinstance(kt, list):
        return "must be a list of lists of strings"
    for g in kt:
        if not isinstance(g, list) or not g or not all(isinstance(t, str) and t.strip() for t in g):
            return "every group must be a non-empty list of non-empty strings"
    return None


def validate_gold(items: Any) -> tuple[list[str], list[str]]:
    """(errors, warnings); messages name question ids and fields only (safe for numbers-only)."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(items, list):
        return ["gold must be a JSON list of question objects"], []
    seen: Counter = Counter()
    for n, q in enumerate(items):
        if not isinstance(q, dict):
            errors.append(f"item {n}: not an object")
            continue
        qid = q.get("id")
        where = f"{qid}" if isinstance(qid, str) and qid else f"item {n}"
        if not isinstance(qid, str) or not qid:
            errors.append(f"{where}: missing id")
        else:
            seen[qid] += 1
        for key, typ in (("doc", str), ("question", str), ("category", str)):
            if not isinstance(q.get(key), typ) or not q.get(key):
                errors.append(f"{where}: '{key}' must be a non-empty string")
        if q.get("split") not in SPLITS:
            errors.append(f"{where}: split must be dev|test")
        if q.get("kind") not in KINDS:
            errors.append(f"{where}: kind must be one of {sorted(KINDS)}")
        usdm = q.get("usdm")
        if not isinstance(usdm, dict) or not isinstance(usdm.get("class"), str) or not usdm.get("class"):
            errors.append(f"{where}: usdm.class missing")
        elif not isinstance(usdm.get("attributes", []), list):
            errors.append(f"{where}: usdm.attributes must be a list")
        mod = q.get("modality")
        if not isinstance(mod, list) or not mod:
            errors.append(f"{where}: modality must be a non-empty list")
        elif set(mod) - MODALITIES:
            warnings.append(f"{where}: unusual modality {sorted(set(mod) - MODALITIES)}")
        answerable = q.get("answerable")
        if not isinstance(answerable, bool):
            errors.append(f"{where}: answerable must be true/false")
        ev = q.get("evidence")
        if not isinstance(ev, list):
            errors.append(f"{where}: evidence must be a list")
            ev = []
        if answerable is False and ev:
            errors.append(f"{where}: unanswerable question with evidence")
        if answerable is True and not ev and not q.get("needs_review"):
            warnings.append(f"{where}: answerable without evidence (excluded from S0-S3)")
        if (q.get("kind") == "unanswerable") != (answerable is False) and isinstance(answerable, bool):
            warnings.append(f"{where}: kind 'unanswerable' and answerable=false disagree")
        for gi, g in enumerate(ev):
            quotes = g.get("all_of") if isinstance(g, dict) else None
            if not isinstance(quotes, list) or not quotes:
                errors.append(f"{where}: evidence[{gi}].all_of must be a non-empty list")
                continue
            for qi, e in enumerate(quotes):
                if not isinstance(e, dict) or not isinstance(e.get("quote"), str) or not e["quote"].strip():
                    errors.append(f"{where}: evidence[{gi}].all_of[{qi}].quote missing")
                    continue
                page = e.get("page")
                if page is not None and (not isinstance(page, int) or page < 1):
                    errors.append(f"{where}: evidence[{gi}].all_of[{qi}].page must be a positive int or null")
                if len(compact(e["quote"])) < FUZZY_MIN_CHARS:
                    warnings.append(f"{where}: evidence[{gi}].all_of[{qi}] is a short quote (may over-match)")
        ans = q.get("answer")
        if not isinstance(ans, dict):
            errors.append(f"{where}: answer must be an object")
            continue
        facts = ans.get("facts", [])
        if not isinstance(facts, list):
            errors.append(f"{where}: answer.facts must be a list")
            facts = []
        fids: Counter = Counter()
        for fi, f in enumerate(facts):
            if not isinstance(f, dict):
                errors.append(f"{where}: answer.facts[{fi}] not an object")
                continue
            fids[f.get("id")] += 1
            if not (isinstance(f.get("text"), str) and f["text"].strip()) and f.get("value") in (None, ""):
                errors.append(f"{where}: answer.facts[{fi}] needs text or value")
            if not isinstance(f.get("alts", []), list):
                errors.append(f"{where}: answer.facts[{fi}].alts must be a list")
            if not isinstance(f.get("critical", False), bool):
                errors.append(f"{where}: answer.facts[{fi}].critical must be true/false")
            if f.get("comparator") not in (None, "") and normalize(str(f["comparator"])) not in COMPARATORS:
                errors.append(f"{where}: answer.facts[{fi}].comparator must be one of {sorted(COMPARATORS)}")
            if f.get("key_terms") is not None and (err := key_terms_error(f["key_terms"])):
                errors.append(f"{where}: answer.facts[{fi}].key_terms {err}")
        dup_f = [k for k, v in fids.items() if v > 1]
        if dup_f:
            errors.append(f"{where}: duplicate fact ids")
        if not isinstance(ans.get("complete_list", False), bool):
            errors.append(f"{where}: answer.complete_list must be true/false")
        forb = ans.get("forbidden", [])
        if not isinstance(forb, list) or any(
                not isinstance(x, dict) or not (str(x.get("text") or "").strip() or x.get("key_terms"))
                for x in forb):
            errors.append(f"{where}: answer.forbidden must be a list of {{text, why[, key_terms]}}")
        else:
            for xi, x in enumerate(forb):
                if x.get("key_terms") is not None and (err := key_terms_error(x["key_terms"])):
                    errors.append(f"{where}: answer.forbidden[{xi}].key_terms {err}")
        if answerable is True and not facts and q.get("kind") != "unanswerable":
            warnings.append(f"{where}: no answer facts (no S4 fact metrics)")
    for qid, n in seen.items():
        if n > 1:
            errors.append(f"{qid}: duplicate id ({n}x)")
    return errors, warnings


def gold_counts(items: list[dict]) -> dict:
    out: dict[str, Any] = {"questions": len(items)}
    for key, get in (("split", lambda q: q.get("split")), ("doc", lambda q: q.get("doc")),
                     ("category", lambda q: q.get("category")), ("kind", lambda q: q.get("kind")),
                     ("usdm_class", lambda q: (q.get("usdm") or {}).get("class")),
                     ("answerable", lambda q: str(q.get("answerable")).lower())):
        out[key] = dict(sorted(Counter(str(get(q)) for q in items).items()))
    out["modality"] = dict(sorted(Counter(m for q in items for m in q.get("modality") or []).items()))
    groups = [g for q in items for g in evidence_groups(q)]
    out["evidence"] = {
        "quotes": sum(len(g) for g in groups),
        "groups": len(groups),
        "all_of_groups": sum(len(g) > 1 for g in groups),
        "with_alternatives": sum(len(evidence_groups(q)) > 1 for q in items),
        "without_evidence": sum(not evidence_groups(q) for q in items),
    }
    facts = [f for q in items for f in facts_of(q)]
    forbidden = [x for q in items for x in (q.get("answer") or {}).get("forbidden") or []]
    out["facts"] = {"facts": len(facts), "critical": sum(bool(f.get("critical")) for f in facts),
                    "numeric": sum(f.get("value") not in (None, "") for f in facts),
                    "with_key_terms": sum(bool(f.get("key_terms")) for f in facts),
                    "forbidden": len(forbidden),
                    "forbidden_with_key_terms": sum(isinstance(x, dict) and bool(x.get("key_terms"))
                                                    for x in forbidden),
                    "complete_list_questions": sum(bool((q.get("answer") or {}).get("complete_list"))
                                                   for q in items)}
    out["needs_review"] = sum(bool(q.get("needs_review")) for q in items)
    return out


# =========================================================================== scoring (pure functions)

def flatten(q: dict) -> tuple[list[list[int]], list[str]]:
    """Evidence as groups of indices into one flat quote list."""
    groups, quotes = [], []
    for g in evidence_groups(q):
        groups.append(list(range(len(quotes), len(quotes) + len(g))))
        quotes.extend(g)
    return groups, quotes


def _specs(quotes: list) -> list[QuoteSpec]:
    return [x if isinstance(x, QuoteSpec) else QuoteSpec(x, prep(x)) for x in quotes]


def quote_ranks(quotes: list, texts: list[str], fuzzy: bool = True, limit: int | None = None,
                ranked: bool = True) -> list[tuple[int | None, int | None, int | None]]:
    """Per quote (a string or a QuoteSpec): 1-based index of the first text containing it at the
    (strict, cells, fuzzy) level; the levels are nested, so cells <= strict and fuzzy <= cells.
    `limit` caps the search for the cells and fuzzy levels (the strict search is cheap and complete).
    ranked=False only answers "is it in some text": a looser level is searched only on a miss."""
    preps = [prep(t) for t in texts]
    cap = len(preps) if limit is None else min(limit, len(preps))
    out = []
    for s in _specs(quotes):
        strict = next((i for i, tp in enumerate(preps, 1) if match_level(s.p, tp, fuzzy=False)), None)
        cells = strict
        if s.locs and (ranked or strict is None):
            upto = min(cap, strict - 1) if strict else cap
            cells = next((i for i, tp in enumerate(preps[:upto], 1) if s.cells_in(tp)), strict)
        loose = cells
        if fuzzy and len(s.p.comp) >= FUZZY_MIN_CHARS and (ranked or cells is None):
            upto = min(cap, cells - 1) if cells else cap
            loose = next((i for i, tp in enumerate(preps[:upto], 1)
                          if fuzzy_score(s.p, tp) >= FUZZY_THRESHOLD), cells)
        out.append((strict, cells, loose))
    return out


def group_scores(groups: list[list[int]], ranks: list[int | None], k: int | None
                 ) -> tuple[bool, float, bool]:
    """(any quote within k, best alternative's share of quotes within k, a full group within k)."""
    def ok(i: int) -> bool:
        r = ranks[i]
        return r is not None and (k is None or r <= k)

    if not groups:
        return False, 0.0, False
    shares = [sum(ok(i) for i in g) / len(g) for g in groups]
    return any(s > 0 for s in shares), max(shares), any(s == 1 for s in shares)


def retrieval_metrics(prefix: str, groups: list[list[int]], quotes: list, ranked_texts: list[str]
                      ) -> tuple[dict, int | None]:
    """Recall/hit/full-group@k and MRR@10 at the primary (cells) level; recall@5 and group@5 also at
    the strict and fuzzy levels. Returns the metrics and the first (cells-level) rank."""
    ranks = quote_ranks(quotes, ranked_texts, limit=max(KS))
    strict, cells, loose = ([r[i] for r in ranks] for i in range(3))
    m: dict[str, Any] = {}
    for k in KS:
        hit, rec, full = group_scores(groups, cells, k)
        m[f"{prefix}_hit@{k}"], m[f"{prefix}_recall@{k}"], m[f"{prefix}_group@{k}"] = hit, rec, full
    first = min((r for r in cells if r), default=None)
    m[f"{prefix}_mrr"] = 1 / first if first and first <= 10 else 0.0
    for level, rk in (("strict", strict), ("fuzzy", loose)):
        _, m[f"{prefix}_recall@5_{level}"], m[f"{prefix}_group@5_{level}"] = group_scores(groups, rk, 5)
    return m, first


def group_levels(groups: list[list[int]], quotes: list, texts: list[str]) -> dict[str, bool]:
    """Is a full group inside the texts (each quote inside some single text) at each level?"""
    ranks = quote_ranks(quotes, texts, ranked=False)
    return {lv: group_scores(groups, [r[i] for r in ranks], None)[2] for i, lv in enumerate(LEVELS)}


def group_in_texts(groups: list[list[int]], quotes: list, texts: list[str], fuzzy: bool = False) -> bool:
    """A full group whose every quote lies inside some single text (cells level, or fuzzy)."""
    return group_levels(groups, quotes, texts)["fuzzy" if fuzzy else "cells"]


def put_levels(m: dict, key: str, levels: dict[str, bool]) -> None:
    """key = the primary (cells) level; key_strict and key_fuzzy."""
    m[key], m[f"{key}_strict"], m[f"{key}_fuzzy"] = levels["cells"], levels["strict"], levels["fuzzy"]


def ranked_units(ranked: list[dict], expander: Any) -> list[str]:
    """Context units in rank order; a unit whose items are all already covered is skipped."""
    seen: set = set()
    out = []
    for chunk in ranked:
        unit = expander.unit(chunk)
        if unit.keys and unit.keys <= seen:
            continue
        seen |= unit.keys
        out.append(unit.text)
    return out


@lru_cache(maxsize=20_000)
def _phrase_regex(norm: str, short: bool) -> re.Pattern:
    left = r"(?<![0-9a-z])" if norm[:1].isalnum() else ""
    right = r"(?:s|es)?(?![0-9a-z])" if short and norm[-1:].isalnum() else ""
    return re.compile(left + re.escape(norm) + right)


def phrase_in(phrase: str, text: Prepared) -> bool:
    """Fact phrase in an answer: word-start boundary; short phrases (< 6 chars) whole-word (plural ok);
    long phrases (>= 8 chars) also match with spaces/hyphens removed."""
    p = prep(phrase)
    if not p.norm:
        return False
    if _phrase_regex(p.norm, len(p.comp) < 6).search(text.norm):
        return True
    return len(p.comp) >= 8 and p.comp in text.comp


_CMP_ALIASES = {
    "≥": ["≥", "at least", "greater than or equal to", "more than or equal to", "no less than", "minimum of",
          "or more", "or greater", "or higher", "or above", "minimum", "lowest", "not below", "not less than",
          "not lower than", "no lower than", "floor"],
    "≤": ["≤", "at most", "less than or equal to", "no more than", "up to", "maximum of", "or less",
          "or lower", "or fewer", "or below", "within", "maximum", "highest", "not exceed", "not more than",
          "no later than", "not later than"],
    ">": [">", "greater than", "more than", "above", "exceeding", "exceeds", "over"],
    "<": ["<", "less than", "below", "under", "fewer than", "lower than"],
    "±": ["±", "+/-", "plus or minus"],
    "=": [],
}


def numeric_exact(fact: dict, text: Prepared) -> bool:
    """value (as a whole number), unit (just after it) and comparator (near it) all in the text."""
    value = normalize(str(fact.get("value")))
    if not value:
        return False
    unit = compact(str(fact.get("unit") or ""))
    cmp_ = normalize(str(fact.get("comparator") or ""))
    aliases = _CMP_ALIASES.get(cmp_, [cmp_]) if cmp_ else []
    for m in re.finditer(r"(?<![\d.,])" + re.escape(value) + r"(?!\d|[.,]\d)", text.norm):
        after = text.norm[m.end():m.end() + 40]
        window = text.norm[max(0, m.start() - 40):m.end() + 40]
        if unit and unit not in compact(after):
            continue
        if aliases and not any(a in window for a in aliases):
            continue
        return True
    return False


@lru_cache(maxsize=20_000)
def _term_regex(term: str) -> tuple[re.Pattern | None, re.Pattern | None]:
    """(pattern on the key-term canonical text, pattern on its compact form or None).

    Numbers sit on token boundaries ("4" never matches "40", "1.4", "4.5" or the 9 of "10e9");
    words start on a word boundary; short terms (< 6 characters) are whole words (plural allowed),
    longer ones may be inflected ("hospitali" matches "hospitalization")."""
    ct = kt_canon(term)
    if not ct:
        return None, None
    cc = _COMPACT_DROP.sub("", ct)
    short = len(cc) < 6
    if ct[0].isdigit():
        left = r"(?<!\d)(?<!\d\.)(?<!10e)"
    elif ct[0].isalnum():
        left = r"(?<![0-9a-z])"
    else:
        left = ""
    if ct[-1].isdigit():
        right = r"(?!\d)(?!\.\d)(?!e[-+]?\d)"
    elif ct[-1].isalnum() and short:
        right = r"(?:s|es)?(?![0-9a-z])"
    else:
        right = ""
    pat = re.compile(left + re.escape(ct) + right)
    comp = None
    if len(cc) >= 8:  # spaces and hyphens removed; only the number boundaries are kept
        cl = r"(?<!\d)(?<!\d\.)" if cc[0].isdigit() else ""
        cr = r"(?!\d)(?!\.\d)" if cc[-1].isdigit() else ""
        comp = re.compile(cl + re.escape(cc) + cr)
    return pat, comp


def term_in(term: str, text: Prepared) -> bool:
    pat, comp = _term_regex(term)
    if pat is None:
        return False
    canon, canon_comp = text.kt()
    return bool(pat.search(canon) or (comp is not None and comp.search(canon_comp)))


def key_terms_found(key_terms: list, text: Prepared) -> bool:
    """AND over the groups, OR within a group, with the gold's own matcher (keyterm_match.py), so
    the scorer and the gold validator can never disagree on what a key term means."""
    groups = [[g] if isinstance(g, str) else [t for t in (g or []) if isinstance(t, str) and t.strip()]
              for g in key_terms]
    groups = [g for g in groups if g]
    return bool(groups) and keyterm_match.key_terms_match(groups, text.raw)


def fact_found(fact: dict, text: Prepared) -> bool:
    """key_terms when the fact has them; else text/alts as phrases, or value+unit+comparator."""
    if fact.get("key_terms"):
        return key_terms_found(fact["key_terms"], text)
    for s in [fact.get("text"), *(fact.get("alts") or [])]:
        if isinstance(s, str) and s.strip() and phrase_in(s, text):
            return True
    return fact.get("value") not in (None, "") and numeric_exact(fact, text)


def forbidden_found(entry: dict, text: Prepared) -> bool:
    if entry.get("key_terms"):
        return key_terms_found(entry["key_terms"], text)
    return bool(entry.get("text")) and phrase_in(str(entry["text"]), text)


_ABSTAIN = re.compile(
    r"\b(?:(?:does|do|did) not|doesn't|don't|didn't) (?:contain|include|provide|specify|mention|state|describe|"
    r"define|list|say|address|give|identify|indicate|reference)\b|\bnot (?:found|provided|specified|mentioned|stated|available|included|"
    r"described|defined|given|addressed|present)\b|\bno (?:information|mention|details?|data|reference)\b|"
    r"\b(?:cannot|can't|unable to) (?:be )?(?:determine|determined|answer|answered|find|found|identify)\b|"
    r"\binsufficient (?:information|context)\b|"
    # "The protocol has/lists/specifies no X", "there is no X", "no such X"
    r"\b(?:has|have|lists?|specif(?:y|ies)|contains?|includes?|defines?|mentions?|describes?|provides?|states?) no\b|"
    r"\bthere (?:is|are) no\b|\bno such\b")


def abstained(answer: str) -> bool:
    """Structured status not_found, or an abstention phrase within the first 300 characters."""
    try:
        obj = json.loads(answer)
    except (ValueError, TypeError):
        obj = None
    if isinstance(obj, dict) and "status" in obj:
        return str(obj["status"]).lower() in ("not_found", "not found", "abstain")
    return bool(_ABSTAIN.search(normalize(answer)[:300]))


_LIST_ITEM = re.compile(r"^\s*(?:[-*•▪◦·]|\d{1,2}[.)]|[a-z][.)])\s+(\S.*)$")
_CITATION = re.compile(r"\[\d+(?:\s*[,\-–]\s*\d+)*\]")
_NUMBER = re.compile(r"(?<![\w.,])\d+(?:[.,]\d+)*")


def list_items(answer: str) -> list[str]:
    return [m.group(1) for line in answer.splitlines() if (m := _LIST_ITEM.match(line))]


def unverified_numbers(answer: str, context: str) -> int:
    """Numbers in the answer (citations and list enumerators removed) that the context does not contain."""
    a = _CITATION.sub(" ", answer)
    a = re.sub(r"(?m)^\s*\d{1,2}[.)]\s+", " ", a)
    cn, cc = normalize(context), compact(context)
    missing = 0
    for num in set(_NUMBER.findall(normalize(a))):
        pat = r"(?<![\d.,])" + re.escape(num) + r"(?!\d|[.,]\d)"
        if not re.search(pat, cn) and not re.search(r"(?<!\d)" + re.escape(num) + r"(?!\d)", cc):
            missing += 1
    return missing


def facts_found(facts: list[dict], text: str) -> list[bool]:
    tp = prep(text)
    return [fact_found(f, tp) for f in facts]


def fact_metrics(prefix: str, facts: list[dict], text: str) -> dict:
    if not facts:
        return {}
    found = facts_found(facts, text)
    crit = [ok for f, ok in zip(facts, found) if f.get("critical")]
    m = {f"{prefix}_fact_recall": sum(found) / len(found)}
    if crit:
        m[f"{prefix}_critical_recall"] = sum(crit) / len(crit)
        m[f"{prefix}_critical_ok"] = all(crit)
    return m


def answer_metrics(q: dict, answer: str, context: str) -> dict:
    """S4 metrics of one answer (deterministic checks only)."""
    facts = facts_of(q)
    spec = q.get("answer") or {}
    ap = prep(answer)
    m: dict[str, Any] = fact_metrics("s4", facts, answer)
    forbidden = [f for f in spec.get("forbidden") or [] if isinstance(f, dict)]
    if forbidden:
        m["s4_forbidden_hit"] = any(forbidden_found(f, ap) for f in forbidden)
    numeric = [f for f in facts if f.get("value") not in (None, "")]
    if numeric:
        m["s4_numeric_exact"] = sum(numeric_exact(f, ap) for f in numeric) / len(numeric)
    items = list_items(answer)
    if spec.get("complete_list") and facts and len(items) >= 2:
        matched = sum(any(fact_found(f, prep(it)) for f in facts) for it in items)
        m["s4_list_precision"] = matched / len(items)
        m["s4_extra_items"] = len(items) - matched
    abst = abstained(answer)
    m["s4_abstained"] = abst
    if q.get("answerable") is False:
        m["s4_abstain_correct"] = abst
    else:
        m["s4_false_abstention"] = abst and (m.get("s4_fact_recall", 0.0) == 0.0)
    m["s4_unverified_numbers"] = unverified_numbers(answer, context)
    return m


def attribute(m: dict, rec: dict, k: int) -> str | None:
    """The first failing stage for this question (None = passes every evaluated stage).

    A cache miss only counts where it stops the evaluation: a question that already fails S0 or S1
    (the pool needs no API call) is attributed to that stage."""
    if rec.get("status") == "error":
        return "error"
    if m.get("s0_doc") is False:
        return "S0_extraction"
    if m.get("s0_chunks") is False:
        return "S0_chunking"
    if m.get("s1_pool") is False:
        return "S1_candidates"
    if str(rec.get("status", "")).startswith("miss"):
        return "cache_miss"
    if m.get("s3_sufficient") is False:
        return "S2_ranking" if m.get("s2_prebudget_group") is False else "S3_context"
    if m.get("s3_sufficient") is None and m.get(f"s2_chunk_group@{k}") is False:
        return "S2_ranking"
    if str(rec.get("answer_status", "")).startswith("miss"):
        return "cache_miss"
    if q_fail_s4(m):
        return "S4_answer"
    return None


def q_fail_s4(m: dict) -> bool:
    return bool((m.get("s4_fact_recall") is not None and m["s4_fact_recall"] < 1)
                or m.get("s4_forbidden_hit") or m.get("s4_false_abstention")
                or m.get("s4_abstain_correct") is False)


# =========================================================================== pipeline adapters

@dataclass
class Retrieval:
    status: str  # "ok" | "miss:embedding" | "miss:rerank"
    pool: list[dict] = field(default_factory=list)  # rerank candidates (chunk dicts)
    ranked: list[dict] = field(default_factory=list)  # ranked chunks, best first
    payload: Any = None  # pipeline-specific object for the context assembler (retrieve.Result)


@dataclass
class Unit:
    keys: frozenset  # document items the unit covers (dedup: a unit inside covered items is skipped)
    text: str


@dataclass
class Ctx:
    text: str
    tokens: int
    info: dict = field(default_factory=dict)


@dataclass
class DocEnv:
    """Everything the scorer needs for one document; every component is replaceable."""
    doc: str
    chunks: list[dict]
    doc_text: str | None = None  # whole converted document text (S0)
    pipeline: Any = None  # .name, .prepare(questions), .retrieve(q) -> Retrieval, .embed_misses
    units: Any = None  # .name, .unit(chunk) -> Unit
    assembler: Any = None  # .name, .build(q, Retrieval, k) -> Ctx
    answerer: Any = None  # .name, .get(question, context) -> str | None (None = cache miss)
    notes: list[str] = field(default_factory=list)
    source_sha: str | None = None
    tables: list[TTable] | None = None  # the converted document's tables (cell-level evidence matching)
    cache_dir: Path | None = None  # where the judge cache lives (next to the answer cache)


def rerank_cache_key(model: str, query: str, chunks: list[dict]) -> str:
    """Mirror of the cache key in retrieve.Retriever.rerank."""
    return hashlib.sha256(json.dumps([model, query, [c["content_hash"] for c in chunks]]).encode()).hexdigest()


def query_cache_key(model: str, text: str) -> str:
    """Mirror of the key in eval_retrieval.cached_query_vectors."""
    return hashlib.sha256(f"{model}|{text}".encode()).hexdigest()


class RerankPipeline:
    """retrieve.Retriever in rerank mode: dense + BM25 candidates (the S1 pool) ordered by the reranker."""

    name = "rerank"

    def __init__(self, retriever: Any, allow_api: bool = False, rerank_model: str | None = None):
        from retrieve import RERANK_MODEL

        self.r = retriever
        self.chunks = retriever.chunks
        self.allow_api = allow_api
        self.model = rerank_model or RERANK_MODEL
        self.vecs: dict[str, Any] = {}
        self.embed_misses = 0
        self._rerank_cache: dict | None = None

    def query_text(self, question: str) -> str:
        return self.r.query_text(self.r.expand(question)[0])

    def prepare(self, questions: list[dict]) -> None:
        from eval_retrieval import cached_query_vectors

        cache_file = self.r.cache_dir / "query_embedding_cache.json"
        model = self.r.meta["model"]
        texts = {q["id"]: self.query_text(q["question"]) for q in questions}
        uniq = list(dict.fromkeys(texts.values()))
        if self.allow_api:
            have = uniq
        else:
            cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
            have = [t for t in uniq if query_cache_key(model, t) in cache]
            del cache
        self.embed_misses = len(uniq) - len(have)
        vecs = cached_query_vectors(have, model, cache_file) if have else []
        by_text = dict(zip(have, vecs))
        self.vecs = {qid: by_text.get(t) for qid, t in texts.items()}
        rc = self.r.cache_dir / "rerank_cache.json"
        self._rerank_cache = json.loads(rc.read_text(encoding="utf-8")) if rc.exists() else {}

    def retrieve(self, q: dict) -> Retrieval:
        from retrieve import CANDIDATES_PER_RANKER

        vec = self.vecs.get(q["id"])
        if vec is None:
            return Retrieval("miss:embedding")
        n = len(self.chunks)
        hybrid = self.r.search(q["question"], vec, k=n, mode="hybrid")
        pool = [h.chunk for h in hybrid.hits
                if h.dense_rank <= CANDIDATES_PER_RANKER or h.bm25_rank <= CANDIDATES_PER_RANKER]
        if not self.allow_api and rerank_cache_key(self.model, hybrid.expanded_query, pool) \
                not in (self._rerank_cache or {}):
            return Retrieval("miss:rerank", pool=pool)
        res = self.r.search(q["question"], vec, k=n, mode="rerank", rerank_model=self.model)
        return Retrieval("ok", pool, [h.chunk for h in res.hits], res)


class ChunkUnits:
    """Identity expansion: the unit of a chunk is the chunk itself."""

    name = "chunk"

    def unit(self, chunk: dict) -> Unit:
        return Unit(frozenset([chunk.get("chunk_id") or id(chunk)]), chunk.get("text") or "")


class SmallToBigUnits:
    """retrieve.ContextBuilder's expansion without a budget: table part -> whole table (with its
    page-split chain), figure part -> whole figure, text of mixed chunks rebuilt from the Docling JSON."""

    name = "small_to_big"

    def __init__(self, builder: Any):
        self.builder = copy.copy(builder)
        self.builder.budget = 10 ** 9
        self._cache: dict[str, Unit] = {}

    def unit(self, chunk: dict) -> Unit:
        cid = chunk["chunk_id"]
        if cid not in self._cache:
            from retrieve import Hit, Result

            ctx = self.builder.build(Result("", "", {}, [Hit(chunk, 0.0, 0, 0)]))
            keys = set(chunk.get("doc_item_refs") or [cid])
            for b in ctx.blocks:
                keys.update(b.table_ids or ([b.table_id] if b.table_id else []))
            self._cache[cid] = Unit(frozenset(keys), "\n\n".join(b.text for b in ctx.blocks))
        return self._cache[cid]


class BuildContextAssembler:
    """retrieve.build_context() on the top-k hits (the answering model's context, with the budget)."""

    name = "build_context"

    def __init__(self, builder: Any):
        self.builder = builder

    def build(self, q: dict, retrieval: Retrieval, k: int) -> Ctx:
        from retrieve import Result, build_context

        res = retrieval.payload
        ctx = build_context(Result(res.query, res.expanded_query, res.definitions, res.hits[:k]), self.builder)
        return Ctx(ctx.render(), ctx.tokens, {"blocks": len(ctx.blocks), "fallbacks": len(ctx.fallbacks),
                                              "dropped": len(ctx.dropped)})


class CachedAnswerer:
    """eval_answers.answer(): same model, system prompt, cache file and cache key; ZDR routing.

    get() returns None on a cache miss unless the API is allowed."""

    def __init__(self, cache_file: Path, model: str | None = None, allow_api: bool = False):
        import eval_answers

        self.ea = eval_answers
        self.model = model or eval_answers.ANSWER_MODEL
        self.name = f"answer:{self.model}"
        self.system = eval_answers.SYSTEM
        self.cache_file = Path(cache_file)
        self.allow_api = allow_api
        self.cache = json.loads(self.cache_file.read_text(encoding="utf-8")) if self.cache_file.exists() else {}

    def key(self, question: str, context: str) -> str:
        return hashlib.sha256(json.dumps([self.model, self.system, context, question]).encode()).hexdigest()

    def get(self, question: str, context: str) -> str | None:
        key = self.key(question, context)
        if key in self.cache:
            return self.cache[key]
        if not self.allow_api:
            return None
        text = self.ea.answer(self.model, context, question, self.cache_file)
        self.cache[key] = text
        return text


# =========================================================================== LLM judge (optional)
#
# Per answer: a verdict per gold fact (supported / contradicted / missing) and the answer's claims
# that no fact covers. Strict JSON (json_schema response format, exact fact ids), temperature 0 where
# the model accepts it, OpenRouter zero-data-retention routing. Verdicts are cached on disk keyed by
# (model, prompt version, question id, answer hash, facts hash); the API is called only with
# --allow-api and never under OPENROUTER_OFFLINE=1 (then a missing verdict is a "miss").

JUDGE_MODEL = "openai/gpt-6.1-sol"  # non-Anthropic on purpose: the answers come from a Claude model; on OpenRouter's ZDR list (2026-10)
JUDGE_PROMPT_VERSION = "judge-v1"
OPENROUTER_CHAT = "https://openrouter.ai/api/v1/chat/completions"
ZDR_PROVIDER = {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
VERDICTS = ("supported", "contradicted", "missing")
JUDGE_ATTEMPTS = 3
JUDGE_SYSTEM = (
    "You grade an answer to a question about a clinical trial protocol against reference facts written by "
    "an expert. Give every reference fact exactly one verdict:\n"
    '- "supported": the answer states the fact. Paraphrase and other notation are fine, but every number, '
    "unit, comparator, timing and qualifier must agree. A fact may list acceptable alternative wordings.\n"
    '- "contradicted": the answer states something incompatible with the fact (a different number, '
    "comparator, unit, timing, population or item).\n"
    '- "missing": the answer does not state the fact, or only vaguely or partly.\n'
    "Judge from the answer text alone; use no outside knowledge and do not assume the answer is right.\n"
    'Also list "extra_claims": short paraphrases of substantive claims in the answer (numbers, criteria, '
    "schedule items, definitions) that no reference fact covers. Ignore citations, hedges and restatements "
    "of the question. Use an empty list when there are none.\n"
    'Reply with JSON only: {"facts": [{"id": "<fact id>", "verdict": "supported|contradicted|missing"}], '
    '"extra_claims": ["..."]}, one entry per reference fact, in the given order.'
)
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": list(VERDICTS)}},
            "required": ["id", "verdict"], "additionalProperties": False}},
        "extra_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["facts", "extra_claims"],
    "additionalProperties": False,
}


def judge_temperature_supported(model: str) -> bool:
    """OpenAI reasoning models (o-series, gpt-5*) reject a temperature other than the default."""
    return not re.match(r"openai/(?:o\d|gpt-5)", model)


def fact_payload(f: dict) -> dict:
    text = f.get("text") if isinstance(f.get("text"), str) and f["text"].strip() else None
    if text is None:
        text = " ".join(str(f.get(k) or "") for k in ("comparator", "value", "unit")).strip()
    return {"id": str(f.get("id")), "fact": text,
            "also_acceptable": [a for a in f.get("alts") or [] if isinstance(a, str) and a.strip()]}


def sha(obj: Any) -> str:
    data = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def parse_judge(content: Any, ids: list[str]) -> dict | None:
    """The judge's JSON reply, validated strictly; None when it is not exactly the schema."""
    if not isinstance(content, str):
        return None
    text = content.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    if not isinstance(obj, dict) or set(obj) != {"facts", "extra_claims"}:
        return None
    facts, extra = obj["facts"], obj["extra_claims"]
    if not isinstance(facts, list) or not isinstance(extra, list) or not all(isinstance(x, str) for x in extra):
        return None
    verdicts: dict[str, str] = {}
    for f in facts:
        if (not isinstance(f, dict) or set(f) != {"id", "verdict"} or f["verdict"] not in VERDICTS
                or f["id"] in verdicts):
            return None
        verdicts[f["id"]] = f["verdict"]
    if set(verdicts) != set(ids):
        return None
    return {"verdicts": [verdicts[i] for i in ids], "extra_claims": [x.strip() for x in extra if x.strip()]}


class OpenRouterClient:
    """POST a chat completion to OpenRouter (retrieve.post_with_retry refuses under OPENROUTER_OFFLINE=1)."""

    def complete(self, body: dict) -> dict:
        from figure_vlm import _api_key
        from retrieve import post_with_retry

        return post_with_retry(OPENROUTER_CHAT, body, _api_key("OPENROUTER_API_KEY")).json()


class Judge:
    """Cached LLM judge of answers against the gold facts."""

    def __init__(self, model: str, cache_file: Path, allow_api: bool = False, client: Any = None):
        self.model = model
        self.name = f"judge:{model}:{JUDGE_PROMPT_VERSION}"
        self.cache_file = Path(cache_file)
        self.allow_api = allow_api
        self.client = client
        self.cache = json.loads(self.cache_file.read_text(encoding="utf-8")) if self.cache_file.exists() else {}
        self.calls = 0

    def key(self, q: dict, answer: str) -> str:
        facts = [fact_payload(f) for f in facts_of(q)]
        return sha([self.model, JUDGE_PROMPT_VERSION, q["id"], sha(answer), sha([q.get("question"), facts])])

    def body(self, q: dict, answer: str) -> dict:
        user = json.dumps({"question": q.get("question"), "reference_facts": [fact_payload(f) for f in facts_of(q)],
                           "answer": answer}, ensure_ascii=False, indent=1)
        body: dict[str, Any] = {
            "model": self.model, "max_tokens": 8000,
            "messages": [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "fact_verdicts", "strict": True, "schema": JUDGE_SCHEMA}},
            "provider": dict(ZDR_PROVIDER)}
        if judge_temperature_supported(self.model):
            body["temperature"] = 0
        return body

    def judge(self, q: dict, answer: str) -> tuple[str, dict | None]:
        """("ok", {"verdicts": [...per gold fact...], "extra_claims": [...]}) | ("miss", None) | ("error:...", None)."""
        key = self.key(q, answer)
        if key in self.cache:
            return "ok", self.cache[key]
        if not self.allow_api or os.environ.get("OPENROUTER_OFFLINE") == "1":
            return "miss", None
        if self.client is None:
            self.client = OpenRouterClient()
        ids = [str(f.get("id")) for f in facts_of(q)]
        body = self.body(q, answer)
        for _ in range(JUDGE_ATTEMPTS):
            self.calls += 1
            resp = self.client.complete(body)
            try:
                content = resp["choices"][0]["message"].get("content")
            except (KeyError, IndexError, TypeError):
                content = None
            if isinstance(content, list):
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            parsed = parse_judge(content, ids)
            if parsed is not None:
                parsed.update({"model": self.model, "prompt_version": JUDGE_PROMPT_VERSION,
                               "provider": resp.get("provider") if isinstance(resp, dict) else None})
                self.cache[key] = parsed
                self._save()
                return "ok", parsed
        return "error:invalid_json", None

    def _save(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.cache_file)


def judge_metrics(facts: list[dict], verdicts: list[str], extra: list, det: list[bool]) -> dict:
    """s4j_* metrics of one judged answer, and the per-fact agreement with the deterministic check."""
    sup = [v == "supported" for v in verdicts]
    m: dict[str, Any] = {"s4j_fact_recall": sum(sup) / len(sup),
                         "s4j_contradicted": any(v == "contradicted" for v in verdicts),
                         "s4j_extra_claims": len(extra),
                         "s4j_agreement": sum(a == b for a, b in zip(sup, det)) / len(sup)}
    crit = [ok for f, ok in zip(facts, sup) if f.get("critical")]
    if crit:
        m["s4j_critical_recall"] = sum(crit) / len(crit)
        m["s4j_critical_ok"] = all(crit)
    return m


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    """Cohen's kappa of two binary raters; None when undefined (no pairs, or both raters constant)."""
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe >= 1 - 1e-12 else (po - pe) / (1 - pe)


def docling_text(doc: Any) -> str:
    """All text of a Docling document in reading order: text items (list markers kept), table rows
    (cells joined left to right, as a PDF text layer reads them), figure text, furniture."""
    from docling_core.types.doc import ContentLayer, TableItem, TextItem

    parts: list[str] = []
    seen: set[str] = set()
    for item, _ in doc.iterate_items(traverse_pictures=True,
                                     included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE}):
        seen.add(item.self_ref)
        if isinstance(item, TableItem):
            rows: dict[int, list[str]] = defaultdict(list)
            for c in sorted(item.data.table_cells, key=lambda c: (c.start_row_offset_idx, c.start_col_offset_idx)):
                rows[c.start_row_offset_idx].append(c.text)
            parts += [" ".join(r) for _, r in sorted(rows.items())]
        elif isinstance(item, TextItem):
            parts.append(f"{getattr(item, 'marker', '') or ''} {item.text}".strip())
    parts += [t.text for t in doc.texts if t.self_ref not in seen]  # anything outside the tree
    return "\n".join(parts)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def default_env(doc: str, chunks_path: Path, opts: argparse.Namespace) -> DocEnv:
    """The current pipeline: retrieve.Retriever (rerank) + ContextBuilder small-to-big + build_context."""
    env = DocEnv(doc=doc, chunks=read_jsonl(chunks_path), cache_dir=Path(chunks_path).parent)
    env.source_sha = env.chunks[0].get("source_sha256") if env.chunks else None
    builder = None
    if not getattr(opts, "s0_only", False):
        try:
            from retrieve import Retriever

            retriever = Retriever(chunks_path)
            env.pipeline = RerankPipeline(retriever, allow_api=opts.allow_api)
            env.chunks = retriever.chunks
        except Exception as exc:  # noqa: BLE001 - e.g. no embeddings for this document: S0 only
            env.notes.append(f"retrieval unavailable ({safe_error(exc)}); S0 only")
    if env.pipeline is not None:
        try:
            from retrieve import ContextBuilder

            builder = ContextBuilder(env.pipeline.r)
            env.units = SmallToBigUnits(builder)
            env.assembler = BuildContextAssembler(builder)
        except Exception as exc:  # noqa: BLE001
            env.notes.append(f"context builder unavailable ({safe_error(exc)}); no unit-level S2, no S3")
    if env.units is None:
        env.units = ChunkUnits()
    try:
        if builder is not None:
            document = builder.doc
        else:
            from docling_core.types.doc import DoclingDocument

            from retrieve import source_json

            document = DoclingDocument.load_from_json(source_json(Path(chunks_path).parent))
        env.doc_text = docling_text(document)
        env.tables = docling_tables(document)
    except Exception as exc:  # noqa: BLE001
        env.notes.append(f"Docling JSON unavailable ({safe_error(exc)}); S0 on chunks only")
    if getattr(opts, "answers", False) and env.assembler is not None:
        env.answerer = CachedAnswerer(Path(chunks_path).parent / "answer_cache.json",
                                      model=opts.answer_model, allow_api=opts.allow_api)
    return env


def load_factory(spec: str):
    module, _, func = spec.partition(":")
    if module in ("eval_v1", "__main__") and func in ("", "default_env"):
        return default_env
    return getattr(importlib.import_module(module), func or "make_env")


# =========================================================================== evaluation

def safe_error(exc: BaseException) -> str:
    """Exception type and our own code locations only (no message: it can quote document text)."""
    frames = [f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)
              if Path(f.filename).resolve().parent == ROOT]
    return f"{type(exc).__name__} at {' > '.join(frames[-3:]) or 'library code'}"


def base_record(q: dict) -> dict:
    return {"id": q["id"], "doc": q.get("doc"), "split": q.get("split"), "category": q.get("category"),
            "kind": q.get("kind"), "usdm_class": (q.get("usdm") or {}).get("class"),
            "modality": list(q.get("modality") or []), "answerable": q.get("answerable"),
            "needs_review": bool(q.get("needs_review")),
            "n_groups": len(evidence_groups(q)), "n_quotes": sum(map(len, evidence_groups(q))),
            "status": "no_retriever", "answer_status": "skipped", "judge_status": "skipped", "m": {}, "info": {}}


def s0_doc_levels(specs: list[QuoteSpec], doc_prep: Prepared) -> list[str | None]:
    """Per quote: 'exact' | 'nospace' (in the document text) | 'cells' (spans table cells) | 'fuzzy' | None."""
    out = []
    for s in specs:
        lv = match_level(s.p, doc_prep, fuzzy=False)
        if lv is None and s.in_table:
            lv = "cells"
        if lv is None and match_level(s.p, doc_prep):
            lv = "fuzzy"
        out.append(lv)
    return out


def table_info(specs: list[QuoteSpec], numbers_only: bool) -> list[dict]:
    """Where the multi-cell quotes lie: the first location of each (cell texts unless numbers-only)."""
    out = []
    for i, s in enumerate(specs):
        if not s.locs:
            continue
        loc = s.locs[0]
        row = {"quote": i, "locations": len(s.locs), "table": loc.table, "order": loc.order,
               "cells": [[r, c] for r, c, _, _ in loc.cells]}
        if not numbers_only:
            row["labels"] = [lab for lab, _ in loc.records]
            row["cell_texts"] = [t for _, _, t, _ in loc.cells]
        out.append(row)
    return out


def evaluate_question(q: dict, env: DocEnv, doc_prep: Prepared | None, k: int, answers: bool,
                      numbers_only: bool, index: TableIndex | None = None, judge: Judge | None = None) -> dict:
    rec = base_record(q)
    m, info = rec["m"], rec["info"]
    groups, quotes = flatten(q)
    specs = quote_specs(quotes, index)
    has_ev = bool(quotes)
    facts = facts_of(q)

    if has_ev:  # S0
        if doc_prep is not None:
            levels = s0_doc_levels(specs, doc_prep)
            for key, ok in (("s0_doc_strict", STRICT), ("s0_doc", (*STRICT, "cells")),
                            ("s0_doc_fuzzy", (*STRICT, "cells", "fuzzy"))):
                m[key] = group_scores(groups, [1 if lv in ok else None for lv in levels], None)[2]
            info["quote_levels"] = dict(Counter(lv or "missing" for lv in levels))
        info["table_quotes"] = sum(bool(s.locs) for s in specs)
        if info["table_quotes"]:
            info["quote_cells"] = table_info(specs, numbers_only)
        put_levels(m, "s0_chunks", group_levels(groups, specs, [c.get("text") or "" for c in env.chunks]))

    if env.pipeline is None:
        return rec
    ret = env.pipeline.retrieve(q)
    rec["status"] = ret.status
    if ret.pool:
        info["pool_size"] = len(ret.pool)
        if has_ev:  # S1
            put_levels(m, "s1_pool", group_levels(groups, specs, [c.get("text") or "" for c in ret.pool]))
    if ret.status != "ok":
        return rec

    if not numbers_only:
        info["top_chunks"] = [c.get("chunk_id") for c in ret.ranked[:10]]
    if has_ev:  # S2
        sm, first = retrieval_metrics("s2_chunk", groups, specs, [c.get("text") or "" for c in ret.ranked])
        m.update(sm)
        info["first_rank_chunk"] = first
        if env.units is not None:
            units = ranked_units(ret.ranked, env.units)
            um, ufirst = retrieval_metrics("s2_unit", groups, specs, units)
            m.update(um)
            info["first_rank_unit"] = ufirst
            info["units_in_pool"] = len(units)
            prebudget = ranked_units(ret.ranked[:k], env.units)  # the context before the token budget
            put_levels(m, "s2_prebudget_group", group_levels(groups, specs, ["\n\n".join(prebudget)]))

    if env.assembler is None:
        return rec
    ctx = env.assembler.build(q, ret, k)  # S3
    m["s3_tokens"] = ctx.tokens
    info.update({f"ctx_{key}": v for key, v in ctx.info.items()})
    if has_ev:
        put_levels(m, "s3_sufficient", group_levels(groups, specs, [ctx.text]))
    m.update(fact_metrics("s3", facts, ctx.text))

    if answers and env.answerer is not None:  # S4
        text = env.answerer.get(q["question"], ctx.text)
        if text is None:
            rec["answer_status"] = "miss:answer"
        else:
            rec["answer_status"] = "ok"
            m.update(answer_metrics(q, text, ctx.text))
            if facts:
                det = facts_found(facts, text)
                info["fact_found"] = det
                if judge is not None:
                    try:
                        status, res = judge.judge(q, text)
                    except Exception as exc:  # noqa: BLE001 - a judge failure must not lose the record
                        status, res = f"error:{type(exc).__name__}", None
                        info["judge_error"] = safe_error(exc)
                    rec["judge_status"] = status
                    if res is not None:
                        info["judge_verdicts"] = list(res["verdicts"])
                        m.update(judge_metrics(facts, res["verdicts"], res.get("extra_claims") or [], det))
    return rec


def evaluate_doc(questions: list[dict], env: DocEnv, k: int = 5, answers: bool = False,
                 numbers_only: bool = False, judge: Judge | None = None) -> list[dict]:
    doc_prep = prep(env.doc_text) if env.doc_text else None
    index = TableIndex(env.tables)
    if env.pipeline is not None:
        env.pipeline.prepare(questions)
    out = []
    for q in questions:
        try:
            rec = evaluate_question(q, env, doc_prep, k, answers, numbers_only, index, judge)
        except Exception as exc:  # noqa: BLE001 - one bad question must not stop the run
            rec = base_record(q)
            rec["status"] = "error"
            rec["error"] = safe_error(exc) if numbers_only else f"{safe_error(exc)}: {str(exc)[:300]}"
        rec["stage_fail"] = attribute(rec["m"], rec, k)
        out.append(rec)
    return out


# =========================================================================== summaries

def aggregate(records: list[dict]) -> dict[str, dict]:
    vals: dict[str, list[float]] = defaultdict(list)
    for r in records:
        for key, v in r["m"].items():
            if v is not None:
                vals[key].append(float(v))
    return {key: {"n": len(v), "mean": sum(v) / len(v)} for key, v in sorted(vals.items())}


DIMENSIONS = ("category", "kind", "usdm_class", "modality", "doc", "split")


def summarize(records: list[dict], meta: dict) -> dict:
    by: dict[str, dict] = {}
    for dim in DIMENSIONS:
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in records:
            for val in (r.get(dim) or ["-"]) if dim == "modality" else [r.get(dim)]:
                groups[str(val)].append(r)
        by[dim] = {val: {"n": len(rs), "metrics": aggregate(rs)} for val, rs in sorted(groups.items())}
    misses = {
        "embedding": [r["id"] for r in records if r["status"] == "miss:embedding"],
        "rerank": [r["id"] for r in records if r["status"] == "miss:rerank"],
        "answer": [r["id"] for r in records if r["answer_status"] == "miss:answer"],
        "judge": [r["id"] for r in records if r.get("judge_status") == "miss"],
        "error": [r["id"] for r in records if r["status"] == "error"
                  or str(r.get("judge_status", "")).startswith("error")],
    }
    attribution: dict[str, list[str]] = defaultdict(list)
    for r in records:
        attribution[r.get("stage_fail") or "pass"].append(r["id"])
    out = {**meta,
           "questions": len(records),
           "answerable": sum(r["answerable"] is True for r in records),
           "unanswerable": sum(r["answerable"] is False for r in records),
           "with_evidence": sum(r["n_quotes"] > 0 for r in records),
           "needs_review": sum(r["needs_review"] for r in records),
           "status": dict(Counter(r["status"] for r in records)),
           "answer_status": dict(Counter(r["answer_status"] for r in records)),
           "metrics": aggregate(records), "by": by, "misses": misses,
           "attribution": {k: sorted(v) for k, v in sorted(attribution.items())},
           "quote_levels": dict(sum((Counter(r["info"].get("quote_levels", {})) for r in records), Counter())),
           "table_quotes": sum(r["info"].get("table_quotes", 0) for r in records)}
    judged = judge_summary(records)
    if judged:
        out["judge_agreement"] = judged
    return out


def _agreement(records: list[dict]) -> dict:
    pairs = [(bool(d), v == "supported") for r in records
             for d, v in zip(r["info"]["fact_found"], r["info"]["judge_verdicts"])]
    conf = Counter(f"det{'+' if d else '-'}/judge{'+' if j else '-'}" for d, j in pairs)
    return {"facts": len(pairs), "kappa": cohen_kappa(pairs),
            "agreement": sum(d == j for d, j in pairs) / len(pairs) if pairs else None,
            "confusion": dict(sorted(conf.items())),
            "verdicts": dict(Counter(v for r in records for v in r["info"]["judge_verdicts"]))}


def judge_summary(records: list[dict]) -> dict | None:
    """Judge verdicts vs the deterministic fact check: Cohen's kappa per fact, overall and by modality."""
    status = Counter(r.get("judge_status", "skipped") for r in records)
    if set(status) <= {"skipped"}:
        return None
    judged = [r for r in records if r["info"].get("judge_verdicts") is not None
              and r["info"].get("fact_found") is not None]
    by_mod: dict[str, list[dict]] = defaultdict(list)
    for r in judged:
        for mod in r.get("modality") or ["-"]:
            by_mod[mod].append(r)
    return {"status": dict(status), **_agreement(judged),
            "by_modality": {mod: _agreement(rs) for mod, rs in sorted(by_mod.items())}}


STAGE_ROWS = [  # (stage, label, primary key = cells level, strict key, fuzzy key)
    ("S0", "extraction recall (quote in converted document)", "s0_doc", "s0_doc_strict", "s0_doc_fuzzy"),
    ("S0", "quote inside a single chunk", "s0_chunks", "s0_chunks_strict", "s0_chunks_fuzzy"),
    ("S1", "candidate recall (full group in rerank pool)", "s1_pool", "s1_pool_strict", "s1_pool_fuzzy"),
    *[("S2", f"chunk evidence recall@{k}", f"s2_chunk_recall@{k}",
       "s2_chunk_recall@5_strict" if k == 5 else None, "s2_chunk_recall@5_fuzzy" if k == 5 else None) for k in KS],
    ("S2", "chunk full-group recall@5", "s2_chunk_group@5", "s2_chunk_group@5_strict", "s2_chunk_group@5_fuzzy"),
    ("S2", "chunk MRR@10", "s2_chunk_mrr", None, None),
    *[("S2", f"unit evidence recall@{k}", f"s2_unit_recall@{k}",
       "s2_unit_recall@5_strict" if k == 5 else None, "s2_unit_recall@5_fuzzy" if k == 5 else None) for k in KS],
    ("S2", "unit full-group recall@5", "s2_unit_group@5", "s2_unit_group@5_strict", "s2_unit_group@5_fuzzy"),
    ("S2", "unit MRR@10", "s2_unit_mrr", None, None),
    ("S2", "full group in top-k expanded (before budget)", "s2_prebudget_group", "s2_prebudget_group_strict",
     "s2_prebudget_group_fuzzy"),
    ("S3", "context sufficiency (full group in context)", "s3_sufficient", "s3_sufficient_strict",
     "s3_sufficient_fuzzy"),
    ("S3", "context tokens (mean)", "s3_tokens", None, None),
    ("S3", "answer facts present in context", "s3_fact_recall", None, None),
    ("S3", "all critical facts in context", "s3_critical_ok", None, None),
    ("S4", "fact recall", "s4_fact_recall", None, None),
    ("S4", "critical fact recall", "s4_critical_recall", None, None),
    ("S4", "all critical facts correct", "s4_critical_ok", None, None),
    ("S4", "forbidden fact present (rate, lower is better)", "s4_forbidden_hit", None, None),
    ("S4", "numeric exactness (value+unit+comparator)", "s4_numeric_exact", None, None),
    ("S4", "list precision (complete_list)", "s4_list_precision", None, None),
    ("S4", "extra list items (mean)", "s4_extra_items", None, None),
    ("S4", "abstention on unanswerable", "s4_abstain_correct", None, None),
    ("S4", "false abstention on answerable", "s4_false_abstention", None, None),
    ("S4", "unverified numbers per answer (mean)", "s4_unverified_numbers", None, None),
    ("S4j", "judge: fact recall (supported)", "s4j_fact_recall", None, None),
    ("S4j", "judge: critical fact recall", "s4j_critical_recall", None, None),
    ("S4j", "judge: all critical facts supported", "s4j_critical_ok", None, None),
    ("S4j", "judge: a fact contradicted (rate)", "s4j_contradicted", None, None),
    ("S4j", "judge: extra claims per answer (mean)", "s4j_extra_claims", None, None),
    ("S4j", "judge: per-fact agreement with deterministic", "s4j_agreement", None, None),
]
COUNT_METRICS = {"s3_tokens", "s4_extra_items", "s4_unverified_numbers", "pool_size", "s4j_extra_claims"}
TABLE_COLS = [("S0", "s0_doc"), ("S0ch", "s0_chunks"), ("S1", "s1_pool"), ("R@5c", "s2_chunk_recall@5"),
              ("R@5u", "s2_unit_recall@5"), ("Suf.s", "s3_sufficient_strict"), ("Suff", "s3_sufficient"),
              ("Suf.f", "s3_sufficient_fuzzy"), ("Tok", "s3_tokens"), ("FactR", "s4_fact_recall"),
              ("FactJ", "s4j_fact_recall")]


def fmt(metric: str, v: float | None) -> str:
    if v is None:
        return "-"
    if metric in COUNT_METRICS:
        return f"{v:.1f}" if metric != "s3_tokens" else f"{v:.0f}"
    return f"{v:.3f}" if metric.endswith("_mrr") else f"{100 * v:.1f}%"


def print_summary(s: dict, numbers_only: bool) -> None:
    print(f"\n=== run {s['run']}: {s['questions']} questions (answerable {s['answerable']}, unanswerable "
          f"{s['unanswerable']}, with evidence {s['with_evidence']}, needs review {s['needs_review']}) | "
          f"split {s['split']} | k {s['k']}")
    print(f"pipeline {s.get('components')} | retrieval status {s['status']} | answers {s['answer_status']}")
    print(f"quote match levels in the document: {s['quote_levels']} | quotes spanning table cells: "
          f"{s.get('table_quotes', 0)} | scorer {s.get('scorer_version', '1')}")
    print(f"\n{'stage':5s} {'metric':50s} {'n':>4s} {'strict':>8s} {'cells*':>8s} {'+fuzzy':>8s}   "
          "(* primary)")
    for stage, label, key, skey, fkey in STAGE_ROWS:
        agg = s["metrics"].get(key)
        if not agg:
            continue
        st = s["metrics"].get(skey) if skey else None
        fz = s["metrics"].get(fkey) if fkey else None
        print(f"{stage:5s} {label:50s} {agg['n']:4d} {fmt(skey, st['mean']) if st else '':>8s} "
              f"{fmt(key, agg['mean']):>8s} {fmt(fkey, fz['mean']) if fz else '':>8s}")
    ja = s.get("judge_agreement")
    if ja:
        kappa = "n/a" if ja["kappa"] is None else f"{ja['kappa']:.3f}"
        agree = "n/a" if ja["agreement"] is None else f"{100 * ja['agreement']:.1f}%"
        print(f"\njudge {s.get('judge')}: status {ja['status']}; per-fact agreement with the deterministic check "
              f"{agree}, Cohen's kappa {kappa} over {ja['facts']} facts; verdicts {ja['verdicts']}; "
              f"{ja['confusion']}")
        for mod, row in ja["by_modality"].items():
            k_ = "n/a" if row["kappa"] is None else f"{row['kappa']:.3f}"
            print(f"  {mod:8s} facts {row['facts']:4d}  kappa {k_}")
    cols = [(h, k) for h, k in TABLE_COLS if k in s["metrics"]]
    for dim in ("category", "kind", "usdm_class", "modality"):
        rows = s["by"].get(dim, {})
        if not rows:
            continue
        print(f"\nby {dim}")
        print(f"  {'':28s} {'n':>4s} " + " ".join(f"{h:>6s}" for h, _ in cols))
        for val, row in rows.items():
            cells = [fmt(k, row["metrics"].get(k, {}).get("mean")) for _, k in cols]
            print(f"  {val[:28]:28s} {row['n']:4d} " + " ".join(f"{c:>6s}" for c in cells))
    print("\nfirst failing stage per question:")
    for stage, ids in s["attribution"].items():
        if stage == "pass":
            print(f"  pass: {len(ids)}")
        else:
            print(f"  {stage}: {len(ids)} {ids}")
    for kind, ids in s["misses"].items():
        if ids:
            print(f"cache misses ({kind}): {len(ids)} {ids}")


def print_plan(records: list[dict], embed_misses: int, answers: bool, judge: bool = False) -> None:
    need_rerank = [r["id"] for r in records if r["status"] in ("miss:embedding", "miss:rerank")]
    need_answer = ([r["id"] for r in records if r["status"] in ("miss:embedding", "miss:rerank")
                    or r["answer_status"] == "miss:answer"] if answers else [])
    print("\nAPI calls this run would need (nothing was called):")
    print(f"  query embeddings: {embed_misses} (the question text only)")
    print(f"  reranks:          {len(need_rerank)} (question + ~30 candidate chunk texts each) {need_rerank}")
    if answers:
        print(f"  answers:          {len(need_answer)} (question + the rendered context each) {need_answer}")
    if judge:
        need_judge = [r["id"] for r in records if r.get("judge_status") == "miss"]
        print(f"  judge:            {len(need_judge)} (question + gold facts + the answer each; at least, "
              f"answers not yet generated add theirs) {need_judge}")
    print("  routing: OpenRouter, provider data_collection=deny, zdr=true, allow_fallbacks=false")


# =========================================================================== commands

def resolve_chunks(doc: str, override: str | None) -> Path:
    if override:
        return Path(override)
    if re.fullmatch(r"P\d+", doc):
        return ROOT / "output_tests" / doc / "chunks" / "chunks.jsonl"
    if doc == "MAIN":
        return MAIN_CHUNKS
    public = ROOT / "output_public" / doc / "chunks" / "chunks.jsonl"
    if (ROOT / "public_protocols" / doc).is_dir():
        return public
    # Never fall back to another document's chunks: that would score the wrong protocol.
    raise SystemExit(f"unknown document {doc!r}: expected MAIN, P<n> or a folder name under public_protocols/")


def cmd_validate(args: argparse.Namespace) -> int:
    gold = load_gold(args.gold)
    errors, warnings = validate_gold(gold)
    for e in errors:
        print(f"ERROR {e}")
    for w in warnings[:args.max_warnings]:
        print(f"warn  {w}")
    if len(warnings) > args.max_warnings:
        print(f"warn  ... {len(warnings) - args.max_warnings} more warnings")
    if isinstance(gold, list) and all(isinstance(q, dict) for q in gold):
        print(json.dumps(gold_counts(gold), indent=1, ensure_ascii=False))
    print(f"{'INVALID' if errors else 'valid'}: {len(errors)} errors, {len(warnings)} warnings")
    return 1 if errors else 0


SCORER_VERSION = "2"  # 1: strict quote matching only; 2: + table cells (primary), key_terms, judge
RUN_ARGS = ("split", "k", "answers", "answer_model", "doc", "chunks", "s0_only", "env_factory", "judge")


def make_judge(args: argparse.Namespace, env: DocEnv, judge_api: bool) -> Judge | None:
    model = getattr(args, "judge", None)
    if not model or not args.answers or env.answerer is None:
        return None
    cache = Path(args.judge_cache) if getattr(args, "judge_cache", None) else (
        (env.cache_dir or RUNS.parent) / "judge_cache.json")
    return Judge(model, cache, allow_api=judge_api)


def execute_run(args: argparse.Namespace, judge_api: bool, extra_meta: dict | None = None) -> int:
    """Evaluate the selected questions (args: see the run sub-command) and write the run."""
    gold_path = Path(args.gold)
    gold = load_gold(gold_path)
    errors, _ = validate_gold(gold)
    if errors:
        print(f"gold is invalid ({len(errors)} errors); run `validate` first")
        return 2
    qs = [q for q in gold if args.split == "all" or q["split"] == args.split]
    if args.doc:
        qs = [q for q in qs if q["doc"] in args.doc]
    if args.ids:
        wanted = set(args.ids)
        qs = [q for q in qs if q["id"] in wanted]
    if not qs:
        print("no questions selected")
        return 2
    if getattr(args, "judge", None) and not args.answers:
        print("note: --judge needs --answers; no judge this run")
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for q in qs:
        by_doc[q["doc"]].append(q)
    factory = load_factory(args.env_factory)
    records: list[dict] = []
    components: dict[str, Any] = {}
    embed_misses = 0
    judge_calls = 0
    t0 = time.time()
    for doc, dq in by_doc.items():
        try:
            env = factory(doc, resolve_chunks(doc, args.chunks), args)
        except Exception as exc:  # noqa: BLE001
            print(f"{doc}: environment failed: {safe_error(exc)}")
            for q in dq:
                rec = base_record(q)
                rec["status"] = "error"
                rec["stage_fail"] = "error"
                records.append(rec)
            continue
        for note in env.notes:
            print(f"{doc}: {note}")
        keys = {c.get("doc_key") for c in env.chunks[:1]} | {c.get("protocol_id") for c in env.chunks[:1]}
        if (doc != "MAIN" and not re.fullmatch(r"P\d+", doc) and not (ROOT / "public_protocols" / doc).is_dir()
                and doc not in keys):
            print(f"{doc}: warning: the gold doc does not match these chunks' doc_key/protocol_id")
        judge = make_judge(args, env, judge_api)
        components[doc] = {"pipeline": getattr(env.pipeline, "name", None),
                           "units": getattr(env.units, "name", None),
                           "assembler": getattr(env.assembler, "name", None),
                           "answerer": getattr(env.answerer, "name", None),
                           "judge": getattr(judge, "name", None),
                           "source_sha256": env.source_sha, "chunks": len(env.chunks),
                           "tables": len(env.tables or [])}
        records += evaluate_doc(dq, env, k=args.k, answers=args.answers, numbers_only=args.numbers_only,
                                judge=judge)
        embed_misses += getattr(env.pipeline, "embed_misses", 0) if env.pipeline is not None else 0
        judge_calls += getattr(judge, "calls", 0)
    if args.plan:
        print_plan(records, embed_misses, args.answers, judge=bool(getattr(args, "judge", None)))
        return 0
    name = re.sub(r"[^\w.\-]+", "_", args.name or f"{gold_path.stem}_{args.split}_k{args.k}_{time.strftime('%Y%m%d-%H%M%S')}")
    meta = {"run": name, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "split": args.split, "k": args.k,
            "answers": args.answers, "numbers_only": args.numbers_only, "scorer_version": SCORER_VERSION,
            "gold_sha256": hashlib.sha256(gold_path.read_bytes()).hexdigest()[:16],
            "components": components, "judge": getattr(args, "judge", None), "judge_api_calls": judge_calls,
            "seconds": round(time.time() - t0, 1)}
    meta["args"] = {key: getattr(args, key, None) for key in RUN_ARGS}
    if args.numbers_only:
        meta["args"]["chunks"] = None if not args.chunks else "(set)"
    else:
        meta["gold"] = str(gold_path)
    meta.update(extra_meta or {})
    summary = summarize(records, meta)
    out = RUNS / name
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "results.jsonl", "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    print_summary(summary, args.numbers_only)
    print(f"\nwrote run '{name}' ({len(records)} questions)" + ("" if args.numbers_only else f" to {out}"))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    offline = os.environ.get("OPENROUTER_OFFLINE") == "1"
    args.allow_api = bool(args.allow_api and not offline and not args.plan)
    if args.allow_api:
        print("API allowed: cache misses will be filled through OpenRouter (zero-data-retention routing)")
    return execute_run(args, judge_api=args.allow_api)


def cmd_rescore(args: argparse.Namespace) -> int:
    """Recompute every metric of an existing run from the caches, with the current gold and scorer.

    The run's questions and settings (k, answers, answer model, chunks, environment) are reused;
    retrieval and answers come only from the caches (no API call: a miss is reported). With --judge
    and --allow-api, missing judge verdicts may be fetched (never under OPENROUTER_OFFLINE=1)."""
    src = run_dir(args.run)
    if not (src / "results.jsonl").exists():
        print(f"run {args.run} not found")
        return 2
    old = json.loads((src / "summary.json").read_text(encoding="utf-8")) if (src / "summary.json").exists() else {}
    ids = [r["id"] for r in read_jsonl(src / "results.jsonl")]
    gold = args.gold or old.get("gold")
    if not gold:
        print("the run does not record its gold file (numbers-only run): pass --gold")
        return 2
    gold_path = Path(gold) if Path(gold).is_absolute() or Path(gold).exists() else ROOT / gold
    old_args = old.get("args") or {}
    answer_model = args.answer_model or old_args.get("answer_model")
    if not answer_model:  # runs before scorer 2 record only the answerer's name: "answer:<model>"
        names = [c.get("answerer") for c in (old.get("components") or {}).values()
                 if str(c.get("answerer") or "").startswith("answer:")]
        answer_model = names[0].split(":", 1)[1] if names else None
    chunks = args.chunks or old_args.get("chunks")
    if chunks == "(set)":
        print("the run used --chunks (not recorded in numbers-only mode): pass --chunks")
        return 2
    gold_ids = {q.get("id") for q in load_gold(gold_path) if isinstance(q, dict)}
    gone = [i for i in ids if i not in gold_ids]
    if gone:
        print(f"{len(gone)} question(s) of the run are not in the gold any more and are dropped: {gone}")
    ns = argparse.Namespace(
        gold=str(gold_path), split="all", k=old.get("k", 5), name=args.name or f"{src.name}_rescored",
        answers=bool(old.get("answers")), answer_model=answer_model, plan=False, allow_api=False,
        numbers_only=bool(args.numbers_only or old.get("numbers_only")), doc=old_args.get("doc"),
        ids=[i for i in ids if i in gold_ids], chunks=chunks, s0_only=bool(old_args.get("s0_only")),
        env_factory=args.env_factory or old_args.get("env_factory") or "eval_v1:default_env",
        judge=args.judge or old_args.get("judge"), judge_cache=args.judge_cache)
    judge_api = bool(args.allow_api and os.environ.get("OPENROUTER_OFFLINE") != "1")
    print(f"rescoring {src.name} ({len(ns.ids)} questions, k={ns.k}, answers={ns.answers}) with gold "
          f"{'(hash only)' if ns.numbers_only else gold_path} and scorer {SCORER_VERSION}; no retrieval/answer "
          f"API calls" + ("; judge API allowed" if judge_api and ns.judge else ""))
    rc = execute_run(ns, judge_api=judge_api, extra_meta={"rescored_from": src.name,
                                                          "rescored_from_scorer": old.get("scorer_version", "1")})
    if rc == 0:
        new = json.loads((RUNS / re.sub(r"[^\w.\-]+", "_", ns.name) / "summary.json").read_text(encoding="utf-8"))
        for doc, comp in (old.get("components") or {}).items():
            now = (new.get("components") or {}).get(doc) or {}
            if comp.get("source_sha256") != now.get("source_sha256"):
                print(f"warning: {doc}: chunks source_sha256 differs from the original run "
                      f"({comp.get('source_sha256')} -> {now.get('source_sha256')}): retrieval is not the same")
        print(f"(rescore of {src.name}: compare with `compare {src.name} {new['run']}`)")
    return rc


# --------------------------------------------------------------------------- compare

def bootstrap_ci(diffs: list[float], clusters: list[str] | None = None, n_boot: int = N_BOOT,
                 seed: int = SEED) -> tuple[float, float]:
    """Paired bootstrap 95% CI of mean(diffs). With >1 cluster (protocol): two-stage cluster bootstrap
    (resample protocols, then questions within each)."""
    import numpy as np

    d = np.asarray(diffs, dtype=float)
    if len(d) == 0:
        return float("nan"), float("nan")
    rng = np.random.default_rng(seed)
    labels = list(dict.fromkeys(clusters)) if clusters else []
    if len(labels) > 1:
        members = [np.flatnonzero(np.asarray(clusters) == c) for c in labels]
        stats = np.empty(n_boot)
        for b in range(n_boot):
            picked = rng.integers(0, len(members), size=len(members))
            idx = np.concatenate([members[c][rng.integers(0, len(members[c]), size=len(members[c]))]
                                  for c in picked])
            stats[b] = d[idx].mean()
    else:
        stats = d[rng.integers(0, len(d), size=(n_boot, len(d)))].mean(axis=1)
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)


def mcnemar_p(b: int, c: int) -> float:
    """Exact two-sided McNemar (binomial) p-value; b, c = discordant pair counts."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def passed(metric: str, v: Any) -> bool | None:
    """Pass/fail view of a metric for the flip list (fractions pass at 1.0)."""
    if v is None or metric in COUNT_METRICS:
        return None
    if isinstance(v, bool):
        return (not v) if metric in ("s4_forbidden_hit", "s4_false_abstention", "s4j_contradicted") else v
    return v >= 1.0 - 1e-9


def compare_records(a: list[dict], b: list[dict], n_boot: int = N_BOOT, seed: int = SEED) -> dict:
    """Paired comparison of two runs' records (same question ids, same order)."""
    clusters = [r.get("doc") or "-" for r in a]
    names = sorted(set().union(*(r["m"] for r in a)) & set().union(*(r["m"] for r in b))) if a else []
    metrics: dict[str, dict] = {}
    for name in names:
        pairs = [(ra["m"].get(name), rb["m"].get(name), ra["id"], cl)
                 for ra, rb, cl in zip(a, b, clusters)]
        pairs = [p for p in pairs if p[0] is not None and p[1] is not None]
        if not pairs:
            continue
        av = [float(p[0]) for p in pairs]
        bv = [float(p[1]) for p in pairs]
        diffs = [y - x for x, y in zip(av, bv)]
        lo, hi = bootstrap_ci(diffs, [p[3] for p in pairs], n_boot, seed)
        row = {"n": len(pairs), "a": sum(av) / len(av), "b": sum(bv) / len(bv),
               "delta": sum(diffs) / len(diffs), "lo": lo, "hi": hi,
               "binary": all(isinstance(p[0], bool) and isinstance(p[1], bool) for p in pairs)}
        if row["binary"]:
            nb = sum(1 for p in pairs if p[0] and not p[1])
            nc = sum(1 for p in pairs if not p[0] and p[1])
            row["mcnemar_p"] = mcnemar_p(nb, nc)
        flips_down = [p[2] for p in pairs if passed(name, p[0]) and passed(name, p[1]) is False]
        flips_up = [p[2] for p in pairs if passed(name, p[0]) is False and passed(name, p[1])]
        row["newly_failing"], row["newly_passing"] = flips_down, flips_up
        metrics[name] = row
    return {"shared": len(a), "metrics": metrics, "gates": gates(metrics, a, b)}


def gates(metrics: dict, a: list[dict], b: list[dict]) -> list[dict]:
    out = []
    for key, label in GATE_METRICS.items():  # 1: no significant drop
        row = metrics.get(key)
        if not row:
            out.append({"gate": f"{label} ({key}): CI lower bound of delta >= {100 * GATE_MIN_DELTA:.0f} pts",
                        "result": "n/a"})
            continue
        ok = row["lo"] >= GATE_MIN_DELTA - 1e-12
        out.append({"gate": f"{label} ({key}): CI lower bound of delta >= {100 * GATE_MIN_DELTA:.0f} pts",
                    "result": "pass" if ok else "FAIL",
                    "detail": f"delta {100 * row['delta']:+.1f} pts, 95% CI [{100 * row['lo']:+.1f}, "
                              f"{100 * row['hi']:+.1f}], n={row['n']}"})
    crit = "s4_critical_ok" if "s4_critical_ok" in metrics else "s3_critical_ok"  # 2: no new critical failures
    if crit in metrics:
        new = metrics[crit]["newly_failing"]
        out.append({"gate": f"no new critical-fact failures ({crit})", "result": "FAIL" if new else "pass",
                    "detail": f"newly failing {new}" if new else f"n={metrics[crit]['n']}"})
    else:
        out.append({"gate": "no new critical-fact failures", "result": "n/a"})
    if "s4_unverified_numbers" in metrics:  # 3: unverified numbers must not increase
        row = metrics["s4_unverified_numbers"]
        sa, sb = row["a"] * row["n"], row["b"] * row["n"]
        out.append({"gate": "unverified numbers in answers do not increase", "result": "pass" if sb <= sa else "FAIL",
                    "detail": f"{sa:.0f} -> {sb:.0f}"})
    for key, cond, label in (("s4_abstain_correct", lambda v: v >= ABSTAIN_MIN, f">= {ABSTAIN_MIN:.0%}"),
                             ("s4_false_abstention", lambda v: v <= FALSE_ABSTAIN_MAX, f"<= {FALSE_ABSTAIN_MAX:.0%}")):
        vals = [r["m"][key] for r in b if r["m"].get(key) is not None]  # 4: absolute, on run B
        if vals:
            rate = sum(vals) / len(vals)
            out.append({"gate": f"{key} {label} (run B)", "result": "pass" if cond(rate) else "FAIL",
                        "detail": f"{rate:.1%} of {len(vals)}"})
    if "s3_tokens" in metrics and metrics["s3_tokens"]["a"] > 0:  # 5: flag only
        growth = metrics["s3_tokens"]["b"] / metrics["s3_tokens"]["a"] - 1
        out.append({"gate": f"context tokens growth <= {TOKEN_GROWTH_FLAG:.0%} (flag only)",
                    "result": "flag" if growth > TOKEN_GROWTH_FLAG else "pass", "detail": f"{growth:+.1%}"})
    return out


COMPARE_ROWS = ["s0_doc", "s0_chunks", "s1_pool", "s2_chunk_recall@1", "s2_chunk_recall@5", "s2_chunk_recall@10",
                "s2_chunk_group@5", "s2_chunk_mrr", "s2_unit_recall@1", "s2_unit_recall@5", "s2_unit_group@5",
                "s2_unit_mrr", "s2_prebudget_group", "s3_sufficient_strict", "s3_sufficient", "s3_sufficient_fuzzy",
                "s3_tokens", "s3_fact_recall", "s3_critical_ok", "s4_fact_recall", "s4_critical_recall",
                "s4_critical_ok", "s4_forbidden_hit", "s4_numeric_exact", "s4_list_precision", "s4_abstain_correct",
                "s4_false_abstention", "s4_unverified_numbers", "s4j_fact_recall", "s4j_critical_ok",
                "s4j_contradicted", "s4j_extra_claims", "s4j_agreement"]
FLIP_ROWS = ["s0_doc", "s0_chunks", "s1_pool", "s2_chunk_group@5", "s2_unit_group@5", "s3_sufficient",
             "s3_critical_ok", "s4_fact_recall", "s4_critical_ok", "s4_abstain_correct", "s4_false_abstention",
             "s4_forbidden_hit", "s4j_fact_recall", "s4j_critical_ok"]


def run_dir(name: str) -> Path:
    p = Path(name)
    return p if (p / "results.jsonl").exists() else RUNS / name


def cmd_compare(args: argparse.Namespace) -> int:
    da, db = run_dir(args.run_a), run_dir(args.run_b)
    ra = {r["id"]: r for r in read_jsonl(da / "results.jsonl")}
    rb = {r["id"]: r for r in read_jsonl(db / "results.jsonl")}
    shared = sorted(set(ra) & set(rb))
    if not shared:
        print("no shared question ids")
        return 2
    res = compare_records([ra[i] for i in shared], [rb[i] for i in shared], args.n_boot, args.seed)
    sa = json.loads((da / "summary.json").read_text(encoding="utf-8")) if (da / "summary.json").exists() else {}
    sb = json.loads((db / "summary.json").read_text(encoding="utf-8")) if (db / "summary.json").exists() else {}
    print(f"A = {args.run_a} ({len(ra)} questions), B = {args.run_b} ({len(rb)}); shared {len(shared)}; "
          f"only A {len(set(ra) - set(rb))}, only B {len(set(rb) - set(ra))}")
    for key in ("k", "split", "components", "gold_sha256", "scorer_version"):
        if sa.get(key) != sb.get(key):
            print(f"  note: {key} differs: A {sa.get(key)} | B {sb.get(key)}")
    if sa.get("scorer_version", "1") != sb.get("scorer_version", "1"):
        print("  note: scorer 1 has strict quote matching only; from scorer 2 the primary evidence metrics count "
              "table quotes at the cells level (strict = *_strict). Rescore A to compare like with like.")
    print(f"\n{'metric':24s} {'n':>4s} {'A':>7s} {'B':>7s} {'delta':>7s} {'95% CI':>17s} {'McNemar p':>9s} "
          f"{'-/+':>6s}")
    rows = sorted(res["metrics"]) if args.all else [k for k in COMPARE_ROWS if k in res["metrics"]]
    for key in rows:
        r = res["metrics"][key]
        scale = 1 if key in COUNT_METRICS else 100
        p = f"{r['mcnemar_p']:.3f}" if "mcnemar_p" in r else ""
        print(f"{key:24s} {r['n']:4d} {fmt(key, r['a']):>7s} {fmt(key, r['b']):>7s} "
              f"{scale * r['delta']:+7.1f} [{scale * r['lo']:+6.1f}, {scale * r['hi']:+6.1f}] {p:>9s} "
              f"{len(r['newly_failing']):>2d}/{len(r['newly_passing']):<2d}")
    print("\n(delta and CI in percentage points; counts in units; -/+ = questions newly failing / passing)")
    print("\nflip list (pass -> fail | fail -> pass):")
    for key in FLIP_ROWS:
        r = res["metrics"].get(key)
        if r and (r["newly_failing"] or r["newly_passing"]):
            print(f"  {key}: - {r['newly_failing']} | + {r['newly_passing']}")
    print("\ngates:")
    for g in res["gates"]:
        print(f"  [{g['result']:>4s}] {g['gate']}" + (f": {g['detail']}" if g.get("detail") else ""))
    verdict = "FAIL" if any(g["result"] == "FAIL" for g in res["gates"]) else "PASS"
    print(f"\nverdict: {verdict}")
    tag = re.sub(r"[^\w.\-]+", "_", Path(args.run_a).name)
    out = db / f"compare_vs_{tag}.json"
    out.write_text(json.dumps({"a": args.run_a, "b": args.run_b, "verdict": verdict, **res}, indent=1),
                   encoding="utf-8")
    return 1 if verdict == "FAIL" else 0


# --------------------------------------------------------------------------- legacy conversion

# Per question of reports/retrieval_eval.json (types from reports/audit/retrieval_generation.md §3):
# (category, kind, modality, USDM class, USDM attributes)
LEGACY_RETRIEVAL_META = [
    ("eligibility", "lookup", ["list"], "EligibilityCriterion", ["text"]),
    ("eligibility.numeric", "lookup", ["list"], "EligibilityCriterion", ["text"]),
    ("eligibility.numeric", "lookup", ["list"], "EligibilityCriterion", ["text"]),
    ("soa", "enumeration", ["table"], "ScheduledActivityInstance", ["activityIds", "encounterId"]),
    ("objectives_endpoints", "lookup", ["table"], "Endpoint", ["text", "level"]),
    ("dosing", "enumeration", ["table"], "StudyIntervention", ["administrations"]),
    ("dosing", "lookup", ["text", "table"], "Activity", ["description"]),
    ("dosing", "lookup", ["text"], "StudyIntervention", ["administrations"]),
    ("safety", "lookup", ["table"], "NarrativeContent", ["text"]),
    ("definitions", "lookup", ["table"], "Abbreviation", ["abbreviatedText", "expandedText"]),
    ("dosing", "lookup", ["figure"], "Administration", ["route"]),
    ("statistics", "lookup", ["text"], "StudyDesignPopulation", ["plannedEnrollmentNumber"]),
    ("dosing.modification", "lookup", ["table"], "StudyIntervention", ["administrations"]),
    ("statistics", "lookup", ["text"], "NarrativeContent", ["text"]),
    ("eligibility", "enumeration", ["text", "list"], "EligibilityCriterion", ["text"]),
    ("amendments", "enumeration", ["table"], "StudyAmendment", ["changes"]),
    ("eligibility.numeric", "lookup", ["list"], "EligibilityCriterion", ["text"]),
    ("numeric_table", "lookup", ["table"], "NarrativeContent", ["text"]),
    ("statistics", "lookup", ["text"], "NarrativeContent", ["text"]),
    ("safety", "enumeration", ["text"], "NarrativeContent", ["text"]),
    ("design", "multi_evidence", ["text", "figure"], "StudyCohort", ["name", "plannedEnrollmentNumber"]),
    ("soa", "lookup", ["table"], "Timing", ["windowLower", "windowUpper"]),
    ("definitions", "lookup", ["list"], "EligibilityCriterion", ["text"]),
    ("safety", "lookup", ["table"], "NarrativeContent", ["text"]),
]
LEGACY_ANSWER_META = {  # reports/answer_eval.json ids
    "soa_eot": ("soa", "enumeration", ["table"], "ScheduledActivityInstance", ["activityIds", "encounterId"]),
    "soa_c1d4": ("soa", "enumeration", ["table"], "ScheduledActivityInstance", ["activityIds", "encounterId"]),
    "soa_ltfu": ("soa", "enumeration", ["table"], "ScheduledActivityInstance", ["activityIds", "encounterId"]),
    "soa_fu": ("soa", "enumeration", ["table"], "ScheduledActivityInstance", ["activityIds", "encounterId"]),
    "soa_pk_row": ("soa", "enumeration", ["table"], "ScheduledActivityInstance", ["activityIds", "encounterId"]),
    "amend_country": ("amendments", "enumeration", ["table"], "StudyAmendment", ["geographicScopes"]),
    "amend_2_row": ("amendments", "lookup", ["table"], "StudyAmendment", ["number", "dateValues"]),
    "t2_heme_g4": ("dosing.modification", "lookup", ["table"], "StudyIntervention", ["administrations"]),
    "t2_footnote": ("dosing.modification", "lookup", ["table"], "StudyIntervention", ["administrations"]),
    "text_primary": ("objectives_endpoints", "lookup", ["table"], "Endpoint", ["text", "level"]),
}
COMPLETE_LIST_TYPES = ("table column", "whole table")


def pdf_pages(pdf: Path) -> list[Prepared]:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(str(pdf))
    return [prep(doc[i].get_textpage().get_text_range()) for i in range(len(doc))]


def cmd_convert_legacy(args: argparse.Namespace) -> int:
    retrieval = json.loads(Path(args.retrieval).read_text(encoding="utf-8"))
    answer_cases = json.loads(Path(args.answers).read_text(encoding="utf-8")) if Path(args.answers).exists() else []
    pages = pdf_pages(Path(args.pdf))
    chunks = read_jsonl(Path(args.chunks))
    by_question = {c["q"]: c for c in answer_cases}

    def section_of(phrase: str) -> str | None:
        p = prep(phrase)
        return next((c.get("section_number") for c in chunks if match_level(p, prep(c["text"]), fuzzy=False)), None)

    def facts_from(case: dict) -> list[dict]:
        return [{"id": f"f{i}", "text": alts[0], "alts": alts[1:], "critical": False,
                 "value": None, "unit": None, "comparator": None} for i, alts in enumerate(case["facts"], 1)]

    out = []
    meta_ok = len(retrieval) == len(LEGACY_RETRIEVAL_META)
    for n, case in enumerate(retrieval, 1):
        cat, kind, modality, cls, attrs = (LEGACY_RETRIEVAL_META[n - 1] if meta_ok
                                           else ("uncategorized", "lookup", ["text"], "unknown", []))
        evidence, review, notes = [], [], []
        for phrase in case.get("contains", []):
            p = prep(phrase)
            hits = [i for i, page in enumerate(pages, 1) if match_level(p, page, fuzzy=False)]
            if not hits:
                review.append("contains-phrase not on any PDF page (serialization artifact?)")
                continue
            section = (case.get("sections") or [None])[0] or section_of(phrase)
            evidence.append({"all_of": [{"quote": phrase, "page": hits[0], "section": section}]})
            if len(hits) > 1:
                notes.append(f"phrase on pages {hits}")
            if len(p.comp) < 20:
                review.append("short/generic quote; may over-match")
        if case.get("sections") and not evidence:
            review.append(f"section-only gold ({', '.join(case['sections'])}); needs a quote")
        if case.get("page") is not None:
            notes.append(f"legacy page restriction {case['page']}")
        ans_case = by_question.pop(case["q"], None)
        facts = facts_from(ans_case) if ans_case else []
        complete = bool(ans_case and ans_case["type"].startswith(COMPLETE_LIST_TYPES))
        if ans_case:
            notes.append(f"facts from answer_eval {ans_case['id']}")
        out.append({"id": f"LEGACY-R{n:02d}", "doc": "MAIN", "split": "dev", "usdm": {"class": cls, "attributes": attrs},
                    "category": cat, "kind": kind, "modality": modality, "question": case["q"], "answerable": True,
                    "evidence": evidence, "answer": {"facts": facts, "complete_list": complete, "forbidden": []},
                    "notes": "; ".join(notes), "source": f"legacy:retrieval_eval.json#{n}",
                    "needs_review": bool(review), "review_reasons": review})
    for case in by_question.values():
        cat, kind, modality, cls, attrs = LEGACY_ANSWER_META.get(
            case["id"], ("uncategorized", "lookup", ["text"], "unknown", []))
        out.append({"id": f"LEGACY-A-{case['id']}", "doc": "MAIN", "split": "dev",
                    "usdm": {"class": cls, "attributes": attrs}, "category": cat, "kind": kind,
                    "modality": modality, "question": case["q"], "answerable": True, "evidence": [],
                    "answer": {"facts": facts_from(case),
                               "complete_list": case["type"].startswith(COMPLETE_LIST_TYPES), "forbidden": []},
                    "notes": f"answer_eval type: {case['type']}", "source": f"legacy:answer_eval.json#{case['id']}",
                    "needs_review": True, "review_reasons": ["no evidence quotes (answer-level legacy question)"]})
    errors, warnings = validate_gold(out)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8")
    with_ev = sum(bool(q["evidence"]) for q in out)
    print(f"wrote {len(out)} questions to {dest}: {with_ev} with evidence quotes, "
          f"{sum(q['needs_review'] for q in out)} need review, {sum(bool(q['answer']['facts']) for q in out)} with facts; "
          f"validation: {len(errors)} errors, {len(warnings)} warnings")
    for q in out:
        if q["needs_review"]:
            print(f"  review {q['id']}: {'; '.join(q['review_reasons'])}")
    return 1 if errors else 0


# --------------------------------------------------------------------------- main

def add_judge_args(p: argparse.ArgumentParser, default_doc: str = "") -> None:
    p.add_argument("--judge", nargs="?", const=JUDGE_MODEL, default=None, metavar="MODEL",
                   help=f"LLM judge of the answers (needs --answers; model when the flag has no value: "
                        f"{JUDGE_MODEL}); verdicts are cached, the API is called only with --allow-api"
                        + (f"; without the flag: {default_doc}" if default_doc else ""))
    p.add_argument("--judge-cache", default=None,
                   help="judge cache file (default: judge_cache.json next to the chunks' answer cache)")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("validate", help="schema check and counts")
    p.add_argument("--gold", required=True)
    p.add_argument("--max-warnings", type=int, default=50)
    p.add_argument("--numbers-only", action="store_true", help="(validate never prints text)")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("run", help="stage metrics per question")
    p.add_argument("--gold", required=True)
    p.add_argument("--split", choices=["dev", "test", "all"], default="dev")
    p.add_argument("--k", type=int, default=5, help="hits in the answering context (S3/S4)")
    p.add_argument("--name", help="run name (reports/eval/runs/<name>/)")
    p.add_argument("--answers", action="store_true", help="S4 answer metrics (answer cache)")
    p.add_argument("--answer-model", default=None, help="default: eval_answers.ANSWER_MODEL")
    p.add_argument("--plan", action="store_true", help="count the API calls the run needs; call nothing")
    p.add_argument("--allow-api", action="store_true",
                   help="fill cache misses through OpenRouter (ZDR); ignored under OPENROUTER_OFFLINE=1")
    p.add_argument("--numbers-only", action="store_true", help="print/store only aggregates and question ids")
    p.add_argument("--doc", nargs="*", help="only these gold docs (e.g. MAIN P1)")
    p.add_argument("--ids", nargs="*", help="only these question ids")
    p.add_argument("--chunks", help="chunks.jsonl to use for every doc (default: by doc)")
    p.add_argument("--s0-only", action="store_true", help="extraction checks only (no retrieval)")
    p.add_argument("--env-factory", default="eval_v1:default_env",
                   help="module:function(doc, chunks_path, args) -> DocEnv (pluggable pipeline)")
    add_judge_args(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("rescore", help="recompute an existing run's metrics from the caches (no API calls)")
    p.add_argument("run", help="run name or folder")
    p.add_argument("--gold", help="gold file (default: the run's own)")
    p.add_argument("--name", help="new run name (default: <RUN>_rescored)")
    p.add_argument("--answer-model", default=None, help="default: the run's answer model")
    p.add_argument("--chunks", help="default: the run's")
    p.add_argument("--env-factory", default=None, help="default: the run's (eval_v1:default_env)")
    p.add_argument("--numbers-only", action="store_true", help="(kept on if the run was numbers-only)")
    p.add_argument("--allow-api", action="store_true",
                   help="only for --judge: fetch missing verdicts (ZDR); ignored under OPENROUTER_OFFLINE=1")
    add_judge_args(p, default_doc="the run's judge, if any")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("compare", help="paired comparison of two runs + regression gates")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p.add_argument("--n-boot", type=int, default=N_BOOT)
    p.add_argument("--seed", type=int, default=SEED)
    p.add_argument("--all", action="store_true", help="print every metric")
    p.add_argument("--numbers-only", action="store_true", help="(compare never prints text)")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("convert-legacy", help="reports/retrieval_eval.json (+answer_eval.json) -> gold format")
    p.add_argument("--retrieval", default=str(ROOT / "reports" / "retrieval_eval.json"))
    p.add_argument("--answers", default=str(ROOT / "reports" / "answer_eval.json"))
    p.add_argument("--pdf", default=str(MAIN_PDF))
    p.add_argument("--chunks", default=str(MAIN_CHUNKS))
    p.add_argument("--out", default=str(ROOT / "reports" / "eval" / "legacy" / "MAIN_legacy.json"))
    p.set_defaults(func=cmd_convert_legacy)

    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
