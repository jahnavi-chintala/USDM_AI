"""Deterministic key-term matcher: the one implementation of the gold scoring contract.

Used by eval_v1.py (answer and context facts, forbidden facts) and by the gold scripts in
reports/eval/scripts/ (gold_validate.py checks every fact against its own evidence with it).

Contract
  fact["key_terms"] and forbidden["key_terms"] are lists of AND-groups; each group is a list of
  OR-alternatives. A key-term list is satisfied by a text when every group has at least one
  alternative that matches the text. Alternatives are written in plain lower/mixed case; the
  same normalize() is applied to the text and to every alternative.

normalize()
  1. NFKC; dashes (‐‑‒–—―−⁃﹘﹣－) -> '-'; quotes -> ' and "; ≧⩾ -> ≥; ≦⩽ -> ≤; ×✕✖⨯ -> x;
     caret, soft hyphen, U+FFFE and zero-width characters removed; markdown '*' and '`' removed,
     '_' -> space; '>=' -> '≥', '<=' -> '≤'; casefold; whitespace collapsed to single spaces.
  2. Spaces around '/' removed ("ANC <1000/ µL" -> "anc <1000/μl").
  3. Space between a sign (≥ ≤ < > ± ~ + -) and a following number removed ("≥ 18" -> "≥18").
  4. Thousands separators removed ("1,000" -> "1000"); a multiplication sign between digits is
     spaced ("25×10⁹" -> "25 x 10⁹"), so "10^9/L" matches inside "25×10⁹/L".
  5. Powers of ten before a unit unified: "10^9/L", "10⁹/L", "10 9 /L" and the PDF's flattened
     "109 /L" all become "10e9/l" (also 10^6 and 10^12; also before "cells" / "per").

Alternative matching (alt = normalize(alternative); compact = alt without spaces and hyphens)
  * left boundary: an alternative starting with a letter/digit must not be preceded by a
    letter/digit; one starting with a digit must also not be preceded by "<digit>." or "<digit>,".
  * right boundary: an alternative ending with a digit must not be followed by a digit or by
    ".<digit>" / ",<digit>" (numbers never match partially: "amendment 1" does not match
    "amendment 10", "0.8" does not match "0.80"). An alternative ending with a letter whose
    compact form is shorter than 6 characters is a whole word with an optional plural "s"/"es"
    ("day" matches "days", "ae" does not match "aes..." prefixes of other words). A longer one
    ending with a letter is a prefix/stem ("randomi" matches "randomized", "discontinu" matches
    "discontinuation").
  * spacing: an alternative whose compact form has >= 8 characters also matches with any of its
    spaces/hyphens removed or added (up to 3 space/hyphen characters between any two characters):
    "m-protein" matches "M protein", "≥1.0 x 10^9/l" matches "≥1.0×10⁹/L". Shorter alternatives
    must match exactly after normalisation.

Usage
  from keyterm_match import key_terms_match, missing_groups, normalize
  key_terms_match(fact["key_terms"], answer_text) -> bool
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_TRANS = str.maketrans({
    **dict.fromkeys("‐‑‒–—―−⁃﹘﹣－", "-"),
    **dict.fromkeys("‘’‚‛′´", "'"),
    **dict.fromkeys("“”„‟″«»", '"'),
    **dict.fromkeys("≧⩾", "≥"),
    **dict.fromkeys("≦⩽", "≤"),
    **dict.fromkeys("×✕✖⨯", "x"),
    **dict.fromkeys("^­￾​‌‍⁠﻿*`", ""),
    "_": " ",
})
_SLASH = re.compile(r"\s*/\s*")
_SIGN_SPACE = re.compile(r"([≥≤<>±~+\-])\s+(?=[\d.])")
_THOUSANDS = re.compile(r"(?<=\d),(?=\d{3}(?!\d))")
_TIMES = re.compile(r"(?<=\d)\s*x\s*(?=\d)")  # "25×10⁹" -> "25 x 109": the power of ten stays its own token
_POW10 = re.compile(r"(?<![\d.])10\s?(6|9|12)(?=/|\s?cells\b|\s?per\b)")
_WORD = r"[^\W_]"


@lru_cache(maxsize=50_000)
def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").translate(_TRANS)
    t = t.replace(">=", "≥").replace("<=", "≤").casefold()
    t = " ".join(t.split())
    t = _SLASH.sub("/", t)
    t = _SIGN_SPACE.sub(r"\1", t)
    t = _THOUSANDS.sub("", t)
    t = _TIMES.sub(" x ", t)
    return _POW10.sub(r"10e\1", t)


def compact(norm: str) -> str:
    return re.sub(r"[\s\-]+", "", norm)


@lru_cache(maxsize=100_000)
def alt_regex(alt: str) -> re.Pattern | None:
    a = normalize(alt)
    if not a:
        return None
    c = compact(a)
    left = ""
    if a[0].isalnum():
        left = rf"(?<!{_WORD})"
        if a[0].isdigit():
            left += r"(?<![0-9][.,])"
    if a[-1].isdigit():
        right = r"(?![0-9])(?![.,][0-9])"
    elif a[-1].isalnum() and len(c) < 6:
        right = rf"(?:s|es)?(?!{_WORD})"
    else:
        right = ""
    if len(c) >= 8:
        body = r"[\s\-]{0,3}".join(re.escape(ch) for ch in c)
    else:
        body = re.escape(a)
    return re.compile(left + body + right)


def alt_in(alt: str, norm_text: str) -> bool:
    """norm_text must already be normalize()d."""
    rx = alt_regex(alt)
    return bool(rx and rx.search(norm_text))


def missing_groups(key_terms: list[list[str]], text: str, *, normalized: bool = False) -> list[int]:
    """Indexes of the AND-groups that no alternative satisfies."""
    t = text if normalized else normalize(text)
    return [i for i, group in enumerate(key_terms) if not any(alt_in(a, t) for a in group)]


def key_terms_match(key_terms: list[list[str]], text: str, *, normalized: bool = False) -> bool:
    if not key_terms:
        return False
    t = text if normalized else normalize(text)
    return all(any(alt_in(a, t) for a in group) for group in key_terms)


if __name__ == "__main__":  # tiny self-test
    assert key_terms_match([["90"], ["day"], ["last dose", "last administration"]],
                           "a minimum of 90 calendar days after the last administration")
    assert not key_terms_match([["amendment 1"]], "Amendment 10 5 January 2021")
    assert key_terms_match([["≥1.0 x 10^9/l"]], "ANC ≥1.0 × 109 /L")
    assert key_terms_match([["≥1.0 x 10^9/l"]], "ANC >= 1.0×10⁹/L")
    assert key_terms_match([["m-protein"]], "serum M protein")
    assert not key_terms_match([["0.8"]], "≥0.80")
    assert key_terms_match([["25"], ["10^9/L"]], "Platelets must be ≥25×10⁹/L")
    assert key_terms_match([["≥25 x 10^9/l"]], "platelets ≥ 25x10 9 /L")
    assert key_terms_match([["<1000"]], "ANC <1,000/µL")
    assert key_terms_match([["day"]], "28 Days")
    assert not key_terms_match([["male"]], "female participants")
    assert key_terms_match([["randomized via"]], "participants are **randomized via** IRT")
    assert not key_terms_match([["is randomized"]], "the study is non-randomized")
    print("ok")
