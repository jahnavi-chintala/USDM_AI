"""Synthetic gold questions for the USDM-4 builder eval (split "dev" only).

For each section unit of a converted protocol (Docling JSON), an LLM writes the
questions an agent building a CDISC USDM v4 study definition would ask, tagged
with USDM class/attributes, kind and category, with atomic answer facts and
verbatim evidence quotes. Everything the model returns is checked locally and
deterministically before it is kept:

  quotes       each quote must be in the PDF text layer (pypdfium2) on its
               stated page after whitespace/hyphenation/glyph normalisation;
               neighbouring pages, then the unit's pages, are tried and the page
               corrected; otherwise the question is dropped
  leakage      the question may not share a 5-word sequence with its quotes
  unanswerable twins must name absent_terms, none of which may occur anywhere
               in the document (PDF text layer or Docling text)
  dedupe       normalised token Jaccard >= 0.8 with an earlier question
  roundtrip    (--roundtrip, off by default) a second call answers from the
               quotes only; every critical fact must be found in that answer
  class cap    at most --class-cap of the kept questions per USDM class

API use: OpenRouter chat completions with no-data-collection / zero-data-
retention routing and no fallbacks, temperature 0, JSON-schema response_format
(if a provider rejects a parameter with HTTP 400 the call is retried once
without response_format/temperature, and counted). Responses are cached per
(model, prompt version, unit text hash, request shape): MAIN in
reports/eval/synthetic_cache/, P-protocols in output_tests/<P>/eval/synthetic_cache/.
--dry-run / --plan build the units and prompts and report the calls and tokens
a run would need, without any call.

Confidential protocols (P1, P2, ...): only aggregate counts and question ids are
printed; errors as exception type + our code location (test_protocols.safe_error).

  python gen_questions.py MAIN [--max-units N] [--per-unit N] [--model M] [--dry-run]
  python gen_questions.py P1 P2 P3
"""

import argparse
import hashlib
import json
import math
import re
import statistics
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from project_paths import main_json, main_pdf
from retrieve import post_with_retry  # module attribute: tests replace it with a fake client
from test_protocols import safe_error

ROOT = Path(__file__).parent
MAIN_JSON = main_json()
MAIN_PDF = main_pdf()
MAIN_OUT = ROOT / "reports" / "eval" / "gold" / "MAIN_synthetic.json"
MAIN_CACHE = ROOT / "reports" / "eval" / "synthetic_cache"

URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
PROVIDER = {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
PROMPT_VERSION = "syn-v1"
ROUNDTRIP_VERSION = "rt-v1"
MAX_TOKENS = 16000

UNIT_TOKENS = 6000  # cap per unit (estimated tokens of the unit text)
MIN_UNIT_CONTENT = 40  # units with fewer non-heading tokens are skipped
CHARS_PER_TOKEN = 3.5  # estimate for English protocol text (numbers, abbreviations)
OUT_TOKENS_PER_QUESTION = 450  # estimate of one question's JSON
OUT_TOKENS_PER_CALL = 1500  # allowance for reasoning / preamble per call
PRICES = {DEFAULT_MODEL: (2.0, 10.0)}  # USD per million input / output tokens

KINDS = ["lookup", "enumeration", "multi_evidence", "cross_reference", "unanswerable"]
KIND_PRIORITY = {"multi_evidence": 0, "cross_reference": 1, "enumeration": 2, "unanswerable": 3, "lookup": 4}
CATEGORIES = ["study_identification", "design", "population", "eligibility", "objectives_endpoints",
              "estimands", "intervention", "schedule", "assessments", "safety", "statistics", "amendments"]
COMPARATORS = ["=", "<", "<=", ">", ">=", "range", "approx"]

# USDM v4 classes and their typical attributes (a reference for the model; the
# class must come from this list, attributes are free text).
USDM_CLASSES = {
    "Study": ["name", "label", "description"],
    "StudyVersion": ["versionIdentifier", "rationale", "dateValues", "titles", "studyIdentifiers", "amendments"],
    "StudyTitle": ["text", "type"],
    "StudyIdentifier": ["text", "scopeId"],
    "Organization": ["name", "type", "identifier", "identifierScheme", "legalAddress"],
    "StudyRole": ["name", "code", "organizationIds", "assignedPersons", "masking"],
    "Masking": ["text", "isMasked"],
    "GovernanceDate": ["name", "type", "dateValue", "geographicScopes"],
    "StudyAmendment": ["number", "summary", "primaryReason", "secondaryReasons", "changes", "enrollments",
                       "dateValues", "geographicScopes"],
    "StudyAmendmentReason": ["code", "otherReason"],
    "StudyChange": ["name", "summary", "rationale", "changedSections"],
    "SubjectEnrollment": ["name", "quantity", "forGeographicScopeId", "forStudySiteId", "forStudyCohortId"],
    "GeographicScope": ["type", "code"],
    "InterventionalStudyDesign": ["name", "studyType", "studyPhase", "model", "intentTypes", "subTypes",
                                  "therapeuticAreas", "characteristics", "blindingSchema", "rationale"],
    "ObservationalStudyDesign": ["name", "studyType", "model", "timePerspective", "samplingMethod",
                                 "characteristics", "rationale"],
    "StudyArm": ["name", "description", "type", "dataOriginType", "dataOriginDescription", "populationIds"],
    "StudyEpoch": ["name", "description", "type"],
    "StudyCell": ["armId", "epochId", "elementIds"],
    "StudyElement": ["name", "description", "transitionStartRule", "transitionEndRule", "studyInterventionIds"],
    "StudyCohort": ["name", "description", "plannedEnrollmentNumber", "plannedAge", "plannedSex",
                    "criterionIds", "characteristics", "indicationIds"],
    "StudyDesignPopulation": ["name", "description", "plannedEnrollmentNumber", "plannedCompletionNumber",
                              "plannedAge", "plannedSex", "includesHealthySubjects", "criterionIds"],
    "EligibilityCriterion": ["category", "identifier", "criterionItemId"],
    "EligibilityCriterionItem": ["name", "text", "dictionaryId"],
    "Characteristic": ["name", "text"],
    "Indication": ["name", "description", "codes", "isRareDisease"],
    "Objective": ["text", "level", "endpoints"],
    "Endpoint": ["text", "purpose", "level"],
    "Estimand": ["populationSummary", "analysisPopulationId", "variableOfInterestId", "interventionIds",
                 "intercurrentEvents"],
    "IntercurrentEvent": ["name", "text", "strategy"],
    "AnalysisPopulation": ["name", "text", "subsetOfIds"],
    "StudyIntervention": ["name", "role", "type", "description", "productDesignation", "pharmacologicClass",
                          "minimumResponseDuration", "administrations"],
    "AdministrableProduct": ["name", "administrableDoseForm", "productDesignation", "pharmacologicClass",
                             "ingredients", "sourcing"],
    "Administration": ["name", "dose", "route", "frequency", "duration", "administrableProductId"],
    "Ingredient": ["role", "substanceId"],
    "Strength": ["numerator", "denominator"],
    "MedicalDevice": ["name", "hardwareVersion", "softwareVersion", "embeddedProductId"],
    "Encounter": ["name", "description", "type", "environmentalSettings", "contactModes",
                  "transitionStartRule", "transitionEndRule", "scheduledAtId"],
    "Activity": ["name", "description", "procedures", "biomedicalConceptIds", "timelineId"],
    "Procedure": ["name", "description", "procedureType", "code", "studyInterventionId"],
    "ScheduleTimeline": ["name", "description", "entryCondition", "mainTimeline", "plannedDuration", "exits"],
    "ScheduledActivityInstance": ["name", "activityIds", "encounterId", "epochId", "defaultConditionId"],
    "ScheduledDecisionInstance": ["name", "conditionAssignments", "defaultConditionId"],
    "Timing": ["name", "type", "value", "valueLabel", "relativeToFrom", "relativeFromScheduledInstanceId",
               "windowLower", "windowUpper", "windowLabel"],
    "Condition": ["name", "text", "contextIds", "appliesToIds"],
    "BiomedicalConcept": ["name", "label", "synonyms", "reference", "properties"],
    "StudySite": ["name", "country"],
    "NarrativeContent": ["name", "sectionNumber", "sectionTitle"],
    "Abbreviation": ["abbreviatedText", "expandedText"],
}

SYSTEM_PROMPT = """You write evaluation questions for an AI agent that reads a clinical trial protocol and \
builds a CDISC USDM v4 (Unified Study Definitions Model) study definition from it. Each question asks for \
information that agent must extract to fill USDM v4 classes and attributes. You see one section of the \
protocol at a time; [page N] lines mark where each page starts.

Rules for every question:
1. Ask what the USDM-building agent needs: identifiers, titles, phase, design (arms, epochs, cohorts, parts, \
blinding, randomisation), population (planned enrolment, age, sex), eligibility criteria, objectives, endpoints, \
estimands, interventions (product, dose, route, frequency, duration, dose modifications), schedule (visits, \
activities, timing windows), procedures and assessments, analysis populations, amendments. Skip boilerplate \
that maps to no USDM attribute.
2. Answerable from the given text alone. Self-contained: name the cohort, arm, part or period it concerns; never \
write "this section", "the table above" or "as described".
3. Paraphrase. Never reuse five or more consecutive words of an evidence quote in the question; use the \
vocabulary of a data modeller instead of the protocol's sentences.
4. evidence: verbatim quotes copied character for character from the text (same spelling, symbols and numbers; \
no ellipses, no added words). Each quote lies on a single page (give that page number from the [page N] \
markers), is short (one sentence, list item or table cell; at most 40 words) and is not a figure placeholder. \
For tables quote the text of a single cell (or a row label), never text spanning cells. Give every quote needed \
to support all critical facts. section: the heading the quote is under.
5. facts: atomic answer facts. text = the shortest answer string (a value with its unit, a name, a short phrase; \
not a sentence); alts = other acceptable forms (abbreviation, expansion, equivalent units or notation); \
critical = true if a correct answer must contain it; value = the number when the fact is a quantity, else null; \
unit = e.g. "mg/kg", "days", "participants", or null; comparator = one of "=", "<", "<=", ">", ">=", "range", \
"approx", or null.
6. kind: lookup (one fact or one place) | enumeration (a list; complete_list = true only if facts list every \
member) | multi_evidence (needs two or more separate quotes, e.g. a table cell and its footnote, or two pages) | \
cross_reference (the text refers to another section, table or appendix and the answer needs both; only if both \
are in this text) | unanswerable.
7. forbidden: plausible wrong answers a careless reader could give from nearby text (another cohort's dose, a \
superseded value, a screening window instead of a treatment window), each with why; [] if none.
8. usdm_class: exactly one class from the list below; usdm_attributes: the attribute names the answer fills.
9. category: one of: __CATEGORIES__.
10. Prefer variety: numbers with units, complete lists, conditions/footnotes, timing windows, cross-references.

Unanswerable twin (only when asked for): a question that looks like it belongs to this section and mirrors one \
of your answerable questions, but asks about something the protocol never mentions (a cohort, arm, visit, dose \
level, endpoint, time point or criterion that does not exist). Set answerable false, kind "unanswerable", \
evidence [], facts [], complete_list false; absent_terms = the exact distinguishing strings (names, numbers with \
units, e.g. "Cohort D", "Day 43") whose absence makes it unanswerable; forbidden = the value a model might \
invent. For answerable questions absent_terms is [].

USDM v4 classes (class: typical attributes):
__CLASSES__

Return only a JSON object: {"questions": [{"question", "usdm_class", "usdm_attributes", "category", "kind", \
"answerable", "evidence": [{"quote", "page", "section"}], "facts": [{"text", "alts", "critical", "value", "unit", \
"comparator"}], "complete_list", "forbidden": [{"text", "why"}], "absent_terms", "notes"}]}. notes: one short \
sentence on what makes the question hard, or "".
"""

ROUNDTRIP_PROMPT = (
    "You answer a question about a clinical trial protocol using only the numbered excerpts given. Reply with "
    'a JSON object {"answer": "..."}: a complete, concise answer that states every value, name or list item '
    'asked for, with units. If the excerpts do not contain the answer, reply {"answer": "NOT FOUND"}.'
)


def system_prompt() -> str:
    classes = "\n".join(f"- {c}: {', '.join(a)}" for c, a in USDM_CLASSES.items())
    return SYSTEM_PROMPT.replace("__CATEGORIES__", ", ".join(CATEGORIES)).replace("__CLASSES__", classes)


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


STR = {"type": "string"}
STRS = {"type": "array", "items": STR}
QUESTION_SCHEMA = _obj({"questions": {"type": "array", "items": _obj({
    "question": STR,
    "usdm_class": {"type": "string", "enum": list(USDM_CLASSES)},
    "usdm_attributes": STRS,
    "category": {"type": "string", "enum": CATEGORIES},
    "kind": {"type": "string", "enum": KINDS},
    "answerable": {"type": "boolean"},
    "evidence": {"type": "array", "items": _obj({"quote": STR, "page": {"type": "integer"}, "section": STR})},
    "facts": {"type": "array", "items": _obj({
        "text": STR, "alts": STRS, "critical": {"type": "boolean"}, "value": _nullable({"type": "number"}),
        "unit": _nullable(STR), "comparator": _nullable({"type": "string", "enum": COMPARATORS})})},
    "complete_list": {"type": "boolean"},
    "forbidden": {"type": "array", "items": _obj({"text": STR, "why": STR})},
    "absent_terms": STRS,
    "notes": STR,
})}})
ANSWER_SCHEMA = _obj({"answer": STR})


def est_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- text normalisation

def _symbol_table() -> dict[int, str]:
    from protocol_postprocess import _SYMBOL_FONT  # Symbol/Wingdings private-use glyphs as the converter maps them

    # pdfium reports line-end hyphens as U+0002 / U+FFFE (soft hyphen U+00AD likewise): make them "-"
    # so that hyphenated line breaks can be joined.
    return {ord(k): v for k, v in _SYMBOL_FONT.items()} | {0xAD: "-", 0x02: "-", 0xFFFE: "-"}


_SYMBOLS = _symbol_table()
_TOKEN = re.compile(r"[^\W_]+|[%<>=≥≤±+/×°]")
_WORD = re.compile(r"[^\W_]+")
_HYPHEN_BREAK = re.compile(r"(?<=[^\W\d_])-[ \t]*\r?\n\s*(?=[^\W\d_])")
_LINE_HYPHEN = re.compile(r"[\x02￾­]\s*")  # pdfium's mark for a hyphen at a line end (newline dropped)
_STOP = set("""a an and are as at be by for from has have how in is it its of on or that the this to was were
what when where which who will with does do should must can""".split())
# Characters (letters, digits, symbols; spacing removed) a quote needs before it may match without word
# boundaries, as for superscripts ("10 9 /L" vs "109/L") or dropped spaces; shorter quotes ("Day 1") must
# match on token boundaries so they cannot hit "Day 15".
MIN_SQUASH_QUOTE = 12


def _prep(text: str) -> str:
    return unicodedata.normalize("NFKC", text.translate(_SYMBOLS)).casefold()


def tokens(text: str) -> list[str]:
    """Lowercase words, numbers and meaningful symbols; punctuation and spacing ignored."""
    return _TOKEN.findall(_prep(text))


def token_str(text: str) -> str:
    return " " + " ".join(tokens(text)) + " "


def squash(text: str) -> str:
    return "".join(tokens(text))


def words(text: str) -> list[str]:
    return _WORD.findall(_prep(text))


class PdfText:
    """The PDF text layer, normalised per page for quote checks.

    Each page is kept twice on token boundaries (line-break hyphens joined, as
    in "partici-/pants", and split, as in "dose-/escalation") and once with all
    spacing removed (fallback for long quotes, when pdfium drops or adds spaces).
    """

    def __init__(self, page_texts: list[str]):
        self.joined, self.split, self.squashed = [], [], []
        for raw in page_texts:
            self.joined.append(token_str(_HYPHEN_BREAK.sub("", _LINE_HYPHEN.sub("", raw))))
            split = _LINE_HYPHEN.sub("-", raw)
            self.split.append(token_str(split))
            self.squashed.append(squash(split))

    @classmethod
    def from_pdf(cls, path: Path) -> "PdfText":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        texts = []
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                textpage = page.get_textpage()
                texts.append(textpage.get_text_range())
                textpage.close()
                page.close()
        finally:
            pdf.close()
        return cls(texts)

    def __len__(self) -> int:
        return len(self.joined)

    def on_page(self, quote: str, page: int) -> bool:
        if not 1 <= page <= len(self):
            return False
        q = token_str(quote)
        if q.strip() and (q in self.joined[page - 1] or q in self.split[page - 1]):
            return True
        s = squash(quote)
        return len(s) >= MIN_SQUASH_QUOTE and s in self.squashed[page - 1]

    def find(self, quote: str, page: int | None, also: list[int] = ()) -> int | None:
        """The page the quote is on: the stated page, its neighbours, then `also` (nearest first)."""
        order = [page, page - 1, page + 1, page - 2, page + 2] if page else []
        order += sorted(also, key=lambda p: abs(p - page) if page else p)
        for p in dict.fromkeys(order):
            if p is not None and self.on_page(quote, p):
                return p
        return None


# --------------------------------------------------------------------------- units

@dataclass
class Item:
    kind: str  # heading | text | list | table | footnote | caption | figure
    text: str
    pages: list[int]
    path: tuple[str, ...]  # heading texts in scope (a heading includes itself)
    number: str | None  # accepted section number of the deepest numbered heading in scope
    parts: tuple[list[str], list[str], list[str]] | None = None  # table: (prefix, rows, notes)
    tokens: int = 0

    def __post_init__(self) -> None:
        self.tokens = est_tokens(self.text)

    @property
    def section(self) -> str:
        return self.path[-1] if self.path else "Front matter"


@dataclass
class Node:
    level: int
    path: tuple[str, ...] = ()
    number: str | None = None
    heading: Item | None = None
    items: list[Item] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)

    def own(self) -> list[Item]:
        return ([self.heading] if self.heading else []) + self.items

    def flat(self) -> list[Item]:
        return self.own() + [it for c in self.children for it in c.flat()]

    def tokens(self) -> int:
        return sum(it.tokens for it in self.own()) + sum(c.tokens() for c in self.children)


@dataclass
class Unit:
    index: int
    items: list[Item]

    @property
    def uid(self) -> str:
        return f"U{self.index:03d}"

    @property
    def path(self) -> tuple[str, ...]:
        return self.items[0].path

    @property
    def number(self) -> str | None:
        return self.items[0].number

    @property
    def pages(self) -> list[int]:
        return sorted({p for it in self.items for p in it.pages})

    @property
    def page_label(self) -> str:
        pages = self.pages
        return "?" if not pages else str(pages[0]) if len(pages) == 1 else f"{pages[0]}-{pages[-1]}"

    @property
    def tokens(self) -> int:
        return sum(it.tokens for it in self.items)

    @property
    def text(self) -> str:
        lines, current = [], None
        for it in self.items:
            key = it.pages[0] if len(it.pages) == 1 else tuple(it.pages)
            if it.pages and key != current:
                lines.append(f"[page {it.pages[0]}]" if len(it.pages) == 1
                             else f"[pages {it.pages[0]}-{it.pages[-1]}]")
                current = key
            lines.append(it.text)
        return "\n".join(lines)


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _pages(item) -> list[int]:
    return sorted({p.page_no for p in getattr(item, "prov", []) or []})


def table_lines(table) -> tuple[list[str], list[str]]:
    """Compact Markdown (header lines incl. separator, body rows); a merged cell's text once per row."""
    grid = table.data.grid
    if not grid:
        return [], []
    rows = []
    for row in grid:
        cells, last = [], None
        for cell in row:
            key = (cell.start_row_offset_idx, cell.start_col_offset_idx)
            cells.append("" if key == last else _clean(cell.text).replace("|", "\\|"))
            last = key
        rows.append("| " + " | ".join(cells) + " |")
    n_head = 0
    for row in grid:
        if not any(c.column_header for c in row):
            break
        n_head += 1
    n_head = max(n_head, 1)
    return rows[:n_head] + ["|" + "---|" * len(grid[0])], rows[n_head:]


def _render(item, doc, emitted: set[str]) -> tuple[str, str, tuple | None] | None:
    """(kind, text, table parts) of one body item, or None to skip it."""
    from docling_core.types.doc import DocItemLabel, ListItem, PictureItem, TableItem, TextItem

    if isinstance(item, TableItem):  # captions/footnotes already emitted as body items are not repeated
        caption = " ".join(_clean(r.resolve(doc).text) for r in item.captions if r.cref not in emitted).strip()
        notes = [_clean(r.resolve(doc).text) for r in item.footnotes if r.cref not in emitted]
        emitted.update(r.cref for r in [*item.captions, *item.footnotes])
        head, body = table_lines(item)
        prefix = ([f"Table: {caption}"] if caption else []) + head
        notes = [n for n in notes if n]
        text = "\n".join(prefix + body + notes)
        return ("table", text, (prefix, body, notes)) if body or caption else None
    if isinstance(item, PictureItem):
        caption = " ".join(_clean(r.resolve(doc).text) for r in item.captions if r.cref not in emitted).strip()
        emitted.update(r.cref for r in item.captions)
        if not caption:
            return None
        return "figure", f"[Figure: {caption}] (image; its content is not in this text and must not be quoted)", None
    if isinstance(item, ListItem):
        text = _clean(f"{item.marker or ''} {item.text}")
        return ("list", text, None) if _clean(item.text) else None
    if isinstance(item, TextItem):
        text = _clean(item.text)
        if not text:
            return None
        kind = {DocItemLabel.FOOTNOTE: "footnote", DocItemLabel.CAPTION: "caption"}.get(item.label, "text")
        return kind, text, None
    return None


def section_tree(doc) -> Node:
    """Body items under their headings (levels as fix_heading_levels set them); TOC tables excluded."""
    from docling_core.types.doc import DocItemLabel, SectionHeaderItem, TableItem, TitleItem

    from protocol_postprocess import section_numbers

    numbers = section_numbers(doc)
    root = Node(level=-1)
    stack = [root]
    emitted: set[str] = set()
    for item, _ in doc.iterate_items(with_groups=False, traverse_pictures=False):
        if item.self_ref in emitted:
            continue
        if isinstance(item, TableItem) and item.label == DocItemLabel.DOCUMENT_INDEX:
            emitted.update(r.cref for r in [*item.captions, *item.footnotes])
            continue
        if isinstance(item, SectionHeaderItem | TitleItem):
            text = _clean(item.text)
            if not text:
                continue
            level = item.level if isinstance(item, SectionHeaderItem) else 0
            while stack[-1].level >= level:
                stack.pop()
            parent = stack[-1]
            path = parent.path + (text,)
            number = numbers.get(item.self_ref) or parent.number
            node = Node(level, path, number, Item("heading", f"{'#' * min(max(level, 1), 6)} {text}",
                                                  _pages(item), path, number))
            parent.children.append(node)
            stack.append(node)
            continue
        rendered = _render(item, doc, emitted)
        if rendered:
            kind, text, parts = rendered
            node = stack[-1]
            node.items.append(Item(kind, text, _pages(item), node.path, node.number, parts))
    return root


def _split_text(it: Item, cap: int) -> list[Item]:
    limit = int(cap * CHARS_PER_TOKEN)
    pieces, cur = [], ""
    for sent in re.split(r"(?<=[.;:])\s+", it.text):
        while len(sent) > limit:  # a single overlong "sentence": hard split
            if cur:
                pieces.append(cur)
                cur = ""
            pieces.append(sent[:limit])
            sent = sent[limit:]
        if cur and len(cur) + 1 + len(sent) > limit:
            pieces.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    if cur:
        pieces.append(cur)
    return [Item(it.kind, p, it.pages, it.path, it.number) for p in pieces]


def split_item(it: Item, cap: int) -> list[Item]:
    """An item over the cap in pieces: tables between rows (header repeated), text between sentences."""
    if it.tokens <= cap:
        return [it]
    if not it.parts:
        return _split_text(it, cap)
    prefix, rows, notes = it.parts
    pieces, cur = [], []
    label = f"(table continued, part {len(rows)} of {len(rows)})"  # the longest continuation label
    budget = cap - est_tokens("\n".join([label, *prefix, *notes])) - 2
    for row in rows:
        if cur and est_tokens("\n".join(cur + [row])) > budget:
            pieces.append(cur)
            cur = []
        cur.append(row)
    pieces.append(cur)
    out = []
    for i, chunk in enumerate(pieces):
        head = prefix if i == 0 else [f"(table continued, part {i + 1} of {len(pieces)})"] + prefix
        tail = notes if i == len(pieces) - 1 else []
        out.append(Item("table", "\n".join(head + chunk + tail), it.pages, it.path, it.number,
                        (head, chunk, tail)))
    return out


def _pack(groups: list[list[Item]], cap: int) -> list[list[Item]]:
    out: list[list[Item]] = []
    for g in groups:
        if out and sum(i.tokens for i in out[-1]) + sum(i.tokens for i in g) <= cap:
            out[-1] = out[-1] + g
        else:
            out.append(list(g))
    return out


def _node_groups(node: Node, cap: int) -> list[list[Item]]:
    if node.tokens() <= cap:
        return [node.flat()]
    own = [[piece] for it in node.own() for piece in split_item(it, cap)]
    groups = _pack(own, cap)
    for child in node.children:
        groups += _node_groups(child, cap)
    return _pack(groups, cap)


def build_units(doc, cap: int = UNIT_TOKENS) -> list[Unit]:
    """Section units in reading order: whole sections up to `cap` tokens, bigger ones split
    into their subsections and at item boundaries; adjacent small groups are packed."""
    groups = _node_groups(section_tree(doc), cap)
    content = [sum(i.tokens for i in g if i.kind != "heading") for g in groups]
    keep = [g for g, n in zip(groups, content) if n and n >= MIN_UNIT_CONTENT]  # never headings only
    return [Unit(i, g) for i, g in enumerate(keep)]


def select_units(units: list[Unit], max_units: int | None) -> list[Unit]:
    """All units, or `max_units` spread evenly over the document."""
    if not max_units or max_units >= len(units):
        return list(units)
    return [units[i] for i in sorted({int(k * len(units) / max_units) for k in range(max_units)})]


# --------------------------------------------------------------------------- prompts, API, cache

def user_prompt(unit: Unit, per_unit: int, twin: bool) -> str:
    low = min(3, per_unit)
    ask = (f"Write {low} to {per_unit} answerable questions about the protocol section below (fewer only if it "
           "has little USDM-relevant content; none if it has none).")
    if twin:
        ask += " Also write exactly 1 unanswerable twin (see the rules)."
    return (f"{ask}\n\nSection path: {' > '.join(unit.path) or 'Front matter'}\nPages: {unit.page_label}\n\n"
            f"<section>\n{unit.text}\n</section>")


def request_body(model: str, system: str, user: str, schema: dict | None, name: str = "questions") -> dict:
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "provider": dict(PROVIDER),
    }
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": name, "strict": True, "schema": schema}}
    return body


def gen_key(model: str, unit_text: str, per_unit: int, twin: bool) -> str:
    """Cache key: model, prompt version (+ hash of the prompt and schema), unit text hash, request shape."""
    prompt = sha(system_prompt() + json.dumps(QUESTION_SCHEMA, sort_keys=True))[:12]
    return sha(json.dumps([model, PROMPT_VERSION, prompt, sha(unit_text), per_unit, twin]))[:32]


def roundtrip_key(model: str, question: str, quotes: list[str]) -> str:
    return "rt-" + sha(json.dumps([model, ROUNDTRIP_VERSION, sha(ROUNDTRIP_PROMPT), question, quotes]))[:29]


class Cache:
    """One JSON file per response, so interrupted or parallel runs never corrupt it."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def path(self, key: str) -> Path:
        return self.folder / f"{key}.json"

    def get(self, key: str) -> dict | None:
        p = self.path(key)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def put(self, key: str, entry: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.path(key).with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path(key))


def chat(body: dict) -> tuple[str, dict, list[str]]:
    """(content, usage, parameters dropped) of one OpenRouter call.

    OPENROUTER_OFFLINE=1 makes post_with_retry refuse. On HTTP 400 the call is
    retried without an optional parameter: the one the error names, else
    temperature (some models accept only the default sampling), then
    response_format (not every provider supports structured outputs). For
    P-protocols the error text is only inspected here, never printed.
    """
    from figure_vlm import _api_key

    key = _api_key("OPENROUTER_API_KEY")
    dropped: list[str] = []
    while True:
        try:
            resp = post_with_retry(URL, {k: v for k, v in body.items() if k not in dropped}, key)
            break
        except RuntimeError as exc:
            msg = str(exc)
            left = [k for k in ("temperature", "response_format") if k in body and k not in dropped]
            if not msg.startswith("OpenRouter 400") or not left:
                raise
            named = [k for k in left if k in msg or (k == "response_format"
                                                       and re.search(r"(?i)json.?schema|structured", msg))]
            dropped.append((named or left)[0])
    data = resp.json()
    choice = data["choices"][0]
    content = choice["message"].get("content")
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content or not content.strip():
        raise RuntimeError(f"empty model output (finish_reason={choice.get('finish_reason')!r})")
    return content, data.get("usage") or {}, dropped


def parse_json(content: str):
    """The first JSON value in a model reply (code fences and preambles tolerated)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", content.strip())
    dec = json.JSONDecoder()
    for m in re.finditer(r"[\[{]", text):
        try:
            return dec.raw_decode(text[m.start():])[0]
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON value in model output")


def parse_questions(content: str) -> list[dict]:
    data = parse_json(content)
    qs = data.get("questions") if isinstance(data, dict) else data
    if not isinstance(qs, list):
        raise ValueError("model output has no question list")
    return [q for q in qs if isinstance(q, dict)]


# --------------------------------------------------------------------------- validation

def _str(x) -> str:
    return _clean(x) if isinstance(x, str) else ""


def _num(x):
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, int | float):
        return x
    try:
        v = float(str(x).replace(",", ""))
        return int(v) if v.is_integer() else v
    except ValueError:
        return None


def _page(x) -> int | None:
    v = _num(x)
    return int(v) if v is not None and float(v).is_integer() and v > 0 else None


def normalise(q: dict) -> dict | None:
    """A model question in canonical form, or None when malformed."""
    question = _str(q.get("question"))
    cls, cat, kind = q.get("usdm_class"), q.get("category"), q.get("kind")
    if len(question.split()) < 4 or cls not in USDM_CLASSES or cat not in CATEGORIES or kind not in KINDS:
        return None
    answerable = bool(q.get("answerable", True)) and kind != "unanswerable"
    kind = kind if answerable else "unanswerable"
    evidence = [{"quote": _str(e.get("quote")), "page": _page(e.get("page")), "section": _str(e.get("section"))}
                for e in q.get("evidence") or [] if isinstance(e, dict) and _str(e.get("quote"))]
    facts = []
    for f in q.get("facts") or []:
        if not isinstance(f, dict) or not _str(f.get("text")):
            continue
        comp = f.get("comparator")
        facts.append({"text": _str(f.get("text")),
                      "alts": [_str(a) for a in f.get("alts") or [] if _str(a)],
                      "critical": bool(f.get("critical", True)), "value": _num(f.get("value")),
                      "unit": _str(f.get("unit")) or None, "comparator": comp if comp in COMPARATORS else None})
    if answerable and (not evidence or not facts):
        return None
    if facts and not any(f["critical"] for f in facts):
        for f in facts:
            f["critical"] = True
    if kind == "multi_evidence" and len(evidence) < 2:
        kind = "lookup"
    if kind == "enumeration" and len(facts) < 2:
        kind = "lookup"
    return {
        "question": question, "usdm_class": cls,
        "usdm_attributes": [a for a in (_str(a) for a in q.get("usdm_attributes") or []) if a and len(a) <= 60],
        "category": cat, "kind": kind, "answerable": answerable,
        "evidence": evidence if answerable else [], "facts": facts if answerable else [],
        "complete_list": bool(q.get("complete_list")) and answerable and kind == "enumeration",
        "forbidden": [{"text": _str(f.get("text")), "why": _str(f.get("why"))}
                      for f in q.get("forbidden") or [] if isinstance(f, dict) and _str(f.get("text"))],
        "absent_terms": [] if answerable else [_str(t) for t in q.get("absent_terms") or [] if _str(t)],
        "notes": _str(q.get("notes")),
    }


def _item_of(quote: str, unit: Unit) -> Item | None:
    q, s = token_str(quote), squash(quote)
    for it in unit.items:
        if q.strip() and q in token_str(it.text):
            return it
    for it in unit.items:
        if len(s) >= MIN_SQUASH_QUOTE and s in squash(it.text):
            return it
    return None


def verify_quotes(c: dict, unit: Unit, pdf: PdfText) -> tuple[bool, int]:
    """Locate every quote in the PDF text layer; fix pages and sections, set modality.

    Returns (all quotes found, number of pages corrected)."""
    fixed, modality = 0, set()
    for ev in c["evidence"]:
        item = _item_of(ev["quote"], unit)
        also = item.pages if item and item.pages else unit.pages
        page = pdf.find(ev["quote"], ev["page"], also + [p for p in unit.pages if p not in also])
        if page is None:
            return False, fixed
        fixed += page != ev["page"]
        ev["page"] = page
        if item is not None:
            ev["section"] = item.section
            modality.add({"heading": "text", "caption": "text"}.get(item.kind, item.kind))
        elif not ev["section"]:
            ev["section"] = unit.items[0].section
    c["modality"] = sorted(modality) or ["text"]
    return True, fixed


def ngrams(ws: list[str], n: int = 5) -> set[tuple[str, ...]]:
    return {tuple(ws[i:i + n]) for i in range(len(ws) - n + 1)}


def leaks(question: str, quotes: list[str], n: int = 5) -> bool:
    """Does the question share an n-word sequence with any of its quotes?"""
    qn = ngrams(words(question), n)
    return any(qn & ngrams(words(quote), n) for quote in quotes)


def _bag(question: str) -> set[str]:
    return {w for w in words(question) if w not in _STOP}


def jaccard(a: str, b: str) -> float:
    x, y = _bag(a), _bag(b)
    return len(x & y) / len(x | y) if x | y else 1.0


def dedupe(cands: list[dict], threshold: float = 0.8) -> list[dict]:
    kept: list[dict] = []
    for c in cands:
        if all(jaccard(c["question"], k["question"]) < threshold for k in kept):
            kept.append(c)
    return kept


class DocText:
    """Every string of the document (PDF text layer and Docling text) for absence checks."""

    def __init__(self, pdf: PdfText, doc=None):
        parts = [*pdf.joined, *pdf.split]
        if doc is not None:
            from docling_core.types.doc import ContentLayer, TableItem, TextItem

            for it, _ in doc.iterate_items(traverse_pictures=True,
                                           included_content_layers={ContentLayer.BODY, ContentLayer.FURNITURE}):
                if isinstance(it, TableItem):
                    parts += [token_str(c.text) for c in it.data.table_cells]
                elif isinstance(it, TextItem):
                    parts.append(token_str(it.text))
        self.text = "\n".join(parts)

    def contains(self, term: str) -> bool:
        t = token_str(term)
        return bool(t.strip()) and t in self.text


def absent(c: dict, doc_text: DocText) -> bool:
    """An unanswerable twin is kept only if it names absent terms and none occurs in the document."""
    return bool(c["absent_terms"]) and not any(doc_text.contains(t) for t in c["absent_terms"])


def fact_in(fact: dict, answer: str) -> bool:
    ans = token_str(answer)
    if any(token_str(a).strip() and token_str(a) in ans for a in [fact["text"], *fact["alts"]]):
        return True
    if fact["value"] is not None:
        nums = {_num(t) for t in re.findall(r"\d+(?:[.,]\d+)*", answer)}
        return float(fact["value"]) in {float(n) for n in nums if n is not None}
    return False


def class_cap(cands: list[dict], share: float, minimum: int = 3) -> list[dict]:
    """At most max(minimum, share x n) questions per USDM class; rarer kinds kept first."""
    if share >= 1:
        return cands
    cap = max(minimum, math.ceil(share * len(cands)))
    keep = set()
    for cls in {c["usdm_class"] for c in cands}:
        mine = sorted((i for i, c in enumerate(cands) if c["usdm_class"] == cls),
                      key=lambda i: (KIND_PRIORITY[cands[i]["kind"]], i))
        keep.update(mine[:cap])
    return [c for i, c in enumerate(cands) if i in keep]


def to_record(c: dict, doc_id: str, n: int, model: str) -> dict:
    notes = [f"synthetic {PROMPT_VERSION} by {model}, unit {c['_unit']}"]
    if c["notes"]:
        notes.append(c["notes"])
    if not c["answerable"]:
        notes.append("absent from the document (verified): " + "; ".join(c["absent_terms"]))
    return {
        "id": f"{doc_id}-SYN-{n:04d}", "doc": doc_id, "split": "dev", "source": "synthetic",
        "usdm": {"class": c["usdm_class"], "attributes": c["usdm_attributes"]},
        "category": c["category"], "kind": c["kind"], "modality": c.get("modality", []) if c["answerable"] else [],
        "question": c["question"], "answerable": c["answerable"],
        "evidence": [{"all_of": [{"quote": e["quote"], "page": e["page"], "section": e["section"]}
                                 for e in c["evidence"]]}] if c["answerable"] else [],
        "answer": {"facts": [{"id": f"f{i}", **f} for i, f in enumerate(c["facts"], start=1)],
                   "complete_list": c["complete_list"], "forbidden": c["forbidden"]},
        "notes": " | ".join(notes),
    }


# --------------------------------------------------------------------------- run

@dataclass
class Target:
    doc: str  # MAIN | P1 | P2 ...
    json_path: Path | None
    pdf_path: Path
    out_file: Path
    cache_dir: Path
    private: bool  # confidential third-party protocol: aggregate numbers only


@dataclass
class Options:
    model: str = DEFAULT_MODEL
    per_unit: int = 5  # the model is asked for min(3, N) to N answerable questions per unit
    max_units: int | None = None
    unit_tokens: int = UNIT_TOKENS
    twin_every: int = 2
    class_cap: float = 0.25
    roundtrip: bool = False
    dry_run: bool = False
    workers: int = 4
    schema: bool = True


def resolve_target(doc_id: str) -> Target:
    if doc_id == "MAIN":
        return Target("MAIN", MAIN_JSON, MAIN_PDF, MAIN_OUT, MAIN_CACHE, private=False)
    if not re.fullmatch(r"P\d+", doc_id):
        raise ValueError(f"unknown document id {doc_id!r} (MAIN or P1, P2, ...)")
    from test_protocols import OUT, doc_json, protocols

    pdfs = dict(protocols())
    if doc_id not in pdfs:
        raise KeyError(doc_id)
    eval_dir = OUT / doc_id / "eval"
    return Target(doc_id, doc_json(doc_id), pdfs[doc_id], eval_dir / "synthetic.json",
                  eval_dir / "synthetic_cache", private=True)


def _counts(cands: list[dict], key: str) -> dict:
    return dict(sorted(Counter(c[key] for c in cands).items()))


def _fmt(n: float) -> str:
    return f"{n:,.0f}"


def plan(units: list[Unit], opts: Options, cache: Cache) -> dict:
    system = system_prompt()
    calls = cached = tok_in = tok_out = all_in = 0
    for i, unit in enumerate(units):
        twin = opts.twin_every > 0 and i % opts.twin_every == 0
        t_in = est_tokens(system) + est_tokens(user_prompt(unit, opts.per_unit, twin)) + 20
        all_in += t_in
        if cache.get(gen_key(opts.model, unit.text, opts.per_unit, twin)) is not None:
            cached += 1
            continue
        calls += 1
        tok_in += t_in
        tok_out += OUT_TOKENS_PER_CALL + OUT_TOKENS_PER_QUESTION * (opts.per_unit + twin)
    return {"calls": calls, "cached": cached, "tokens_in": tok_in, "tokens_out": tok_out, "tokens_in_all": all_in}


def _cost(model: str, tok_in: float, tok_out: float) -> str:
    if model not in PRICES:
        return "price unknown for this model"
    p_in, p_out = PRICES[model]
    return f"~${(tok_in * p_in + tok_out * p_out) / 1e6:.2f} at ${p_in:g}/${p_out:g} per M tokens"


def run_doc(target: Target, opts: Options) -> dict:
    """Generate, validate and write one document's synthetic questions; prints numbers only."""
    from docling_core.types.doc import DoclingDocument

    say = print
    errors: Counter = Counter()

    def fail(exc: BaseException) -> None:
        sig = safe_error(exc)
        errors[sig] += 1
        if not target.private:  # MAIN is local; P-protocol messages may quote document text
            say(f"  error: {sig}: {str(exc)[:300]}")

    say(f"\n=== {target.doc}" + (" (plan only; no API calls)" if opts.dry_run else ""))
    if target.json_path is None or not Path(target.json_path).exists():
        say("  not converted")
        return {"status": "not converted"}
    doc = DoclingDocument.load_from_json(target.json_path)
    all_units = build_units(doc, opts.unit_tokens)
    units = select_units(all_units, opts.max_units)
    sizes = [u.tokens for u in units] or [0]
    cache = Cache(target.cache_dir)
    p = plan(units, opts, cache)
    twins = sum(1 for i in range(len(units)) if opts.twin_every > 0 and i % opts.twin_every == 0)
    say(f"  units {len(all_units)} (selected {len(units)}; est. tokens min {min(sizes)} / median "
        f"{statistics.median(sizes):.0f} / max {max(sizes)}) | twin requests {twins}")
    say(f"  generation calls needed {p['calls']} (cached {p['cached']}) | est. tokens in {_fmt(p['tokens_in'])}"
        f" / out {_fmt(p['tokens_out'])} | {_cost(opts.model, p['tokens_in'], p['tokens_out'])} ({opts.model})")
    if opts.roundtrip:
        n_rt = opts.per_unit * len(units)
        say(f"  roundtrip: up to ~{n_rt} more calls (~{_fmt(n_rt * 700)} tokens in, ~{_fmt(n_rt * 300)} out)")
    stats = {"units": len(all_units), "selected": len(units), **p}
    if opts.dry_run:
        return stats

    # ---- generation (cached per unit)
    system = system_prompt()
    usage_new, dropped_params, new_calls = Counter(), Counter(), 0

    def generate(i: int, unit: Unit):
        twin = opts.twin_every > 0 and i % opts.twin_every == 0
        key = gen_key(opts.model, unit.text, opts.per_unit, twin)
        entry = cache.get(key)
        if entry is not None:
            return parse_questions(entry["content"]), None
        body = request_body(opts.model, system, user_prompt(unit, opts.per_unit, twin),
                            QUESTION_SCHEMA if opts.schema else None)
        content, usage, dropped = chat(body)
        qs = parse_questions(content)  # unparsable output raises and is not cached
        cache.put(key, {"model": opts.model, "prompt_version": PROMPT_VERSION, "unit_sha": sha(unit.text),
                        "per_unit": opts.per_unit, "twin": twin, "content": content, "usage": usage,
                        "dropped_params": dropped})
        return qs, (usage, dropped)

    def safe_generate(arg):
        try:
            return generate(*arg)
        except Exception as exc:  # noqa: BLE001 - counted per error location, the run continues
            return exc

    with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
        results = list(pool.map(safe_generate, enumerate(units)))

    raw: list[tuple[Unit, dict]] = []
    for unit, res in zip(units, results):
        if isinstance(res, BaseException):
            fail(res)
            continue
        qs, fresh = res
        if fresh is not None:
            new_calls += 1
            usage, dropped = fresh
            usage_new.update({k: v for k, v in usage.items() if isinstance(v, int | float)})
            dropped_params.update(dropped)
        raw += [(unit, q) for q in qs]

    # ---- deterministic filters
    funnel = {"generated": len(raw)}
    cands = []
    for unit, q in raw:
        c = normalise(q)
        if c is not None:
            c["_unit"] = unit.uid
            c["_unit_obj"] = unit
            cands.append(c)
    funnel["well_formed"] = len(cands)

    pdf = PdfText.from_pdf(target.pdf_path)
    kept, pages_fixed = [], 0
    for c in cands:
        if not c["answerable"]:
            kept.append(c)
            continue
        ok, fixed = verify_quotes(c, c["_unit_obj"], pdf)
        if ok:
            kept.append(c)
            pages_fixed += fixed
    funnel["quotes_verified"] = len(kept)

    cands = [c for c in kept if not leaks(c["question"], [e["quote"] for e in c["evidence"]])]
    funnel["no_leakage"] = len(cands)

    doc_text = DocText(pdf, doc)
    cands = [c for c in cands if c["answerable"] or absent(c, doc_text)]
    funnel["unanswerable_verified"] = len(cands)

    cands = dedupe(cands)
    funnel["deduped"] = len(cands)

    rt_calls = 0
    if opts.roundtrip:
        def roundtrip(c):
            if not c["answerable"]:
                return True, False
            quotes = [e["quote"] for e in c["evidence"]]
            key = roundtrip_key(opts.model, c["question"], quotes)
            entry = cache.get(key)
            fresh = entry is None
            if fresh:
                excerpts = "\n".join(f"[{i}] (page {e['page']}) {e['quote']}"
                                     for i, e in enumerate(c["evidence"], start=1))
                body = request_body(opts.model, ROUNDTRIP_PROMPT,
                                    f"Excerpts:\n{excerpts}\n\nQuestion: {c['question']}",
                                    ANSWER_SCHEMA if opts.schema else None, name="answer")
                content, usage, _ = chat(body)
                entry = {"model": opts.model, "version": ROUNDTRIP_VERSION, "content": content, "usage": usage}
                cache.put(key, entry)
            data = parse_json(entry["content"])
            answer = data.get("answer", "") if isinstance(data, dict) else ""
            return all(fact_in(f, str(answer)) for f in c["facts"] if f["critical"]), fresh

        def safe_rt(c):
            try:
                return roundtrip(c)
            except Exception as exc:  # noqa: BLE001
                return exc

        with ThreadPoolExecutor(max_workers=max(1, opts.workers)) as pool:
            checks = list(pool.map(safe_rt, cands))
        passed = []
        for c, res in zip(cands, checks):
            if isinstance(res, BaseException):
                fail(res)
                continue
            ok, fresh = res
            rt_calls += fresh
            if ok:
                passed.append(c)
        cands = passed
        funnel["roundtrip_passed"] = len(cands)

    cands = class_cap(cands, opts.class_cap)
    funnel["class_capped"] = len(cands)

    records = [to_record(c, target.doc, n, opts.model) for n, c in enumerate(cands, start=1)]
    target.out_file.parent.mkdir(parents=True, exist_ok=True)
    target.out_file.write_text(json.dumps(records, indent=1, ensure_ascii=False), encoding="utf-8")

    say(f"  API calls made {new_calls + rt_calls} (generation {new_calls}, roundtrip {rt_calls}); "
        f"usage of new calls: " + (", ".join(f"{k} {v:g}" for k, v in sorted(usage_new.items())) or "none"))
    if dropped_params:
        say(f"  calls retried without rejected parameters: {dict(dropped_params)}")
    if errors:
        say(f"  failures: {dict(errors)}")
    say("  funnel: " + " -> ".join(f"{k} {v}" for k, v in funnel.items()) + f" (quote pages corrected {pages_fixed})")
    say(f"  kept by kind {_counts(cands, 'kind')}")
    say(f"  kept by category {_counts(cands, 'category')}")
    say(f"  kept by USDM class {_counts(cands, 'usdm_class')}")
    ids = [r["id"] for r in records]
    say(f"  question ids: {ids[0]} .. {ids[-1]} ({len(ids)})" if ids else "  question ids: none")
    out = target.out_file
    say(f"  written: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out.name} ({len(records)} questions)")
    return {**stats, "funnel": funnel, "errors": dict(errors), "new_calls": new_calls, "roundtrip_calls": rt_calls,
            "kept": len(records)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("docs", nargs="+", help="MAIN and/or P1 P2 ...")
    parser.add_argument("--max-units", type=int, help="only N units, spread evenly over the document")
    parser.add_argument("--per-unit", type=int, default=Options.per_unit, help="questions per unit (asks for 3..N)")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--unit-tokens", type=int, default=UNIT_TOKENS, help="cap per unit (estimated tokens)")
    parser.add_argument("--twin-every", type=int, default=Options.twin_every,
                        help="ask for one unanswerable twin every K units (0 = never)")
    parser.add_argument("--class-cap", type=float, default=Options.class_cap,
                        help="max share of kept questions per USDM class (1 = no cap)")
    parser.add_argument("--roundtrip", action="store_true", help="second call answers from the quotes only")
    parser.add_argument("--workers", type=int, default=Options.workers, help="parallel API calls")
    parser.add_argument("--no-schema", action="store_true", help="omit the JSON-schema response_format")
    parser.add_argument("--dry-run", action="store_true", help="build units and prompts; report calls; no API")
    parser.add_argument("--plan", action="store_true", help="same as --dry-run")
    args = parser.parse_args(argv)
    opts = Options(model=args.model, per_unit=max(1, args.per_unit), max_units=args.max_units,
                   unit_tokens=args.unit_tokens, twin_every=args.twin_every, class_cap=args.class_cap,
                   roundtrip=args.roundtrip, dry_run=args.dry_run or args.plan, workers=args.workers,
                   schema=not args.no_schema)
    for doc_id in args.docs:
        try:
            target = resolve_target(doc_id)
        except (KeyError, ValueError) as exc:
            print(f"\n=== {doc_id}: {'no such protocol' if isinstance(exc, KeyError) else exc}")
            continue
        try:
            run_doc(target, opts)
        except Exception as exc:  # noqa: BLE001 - report and continue with the next document
            print(f"  failed: {safe_error(exc)}" + ("" if target.private else f": {str(exc)[:300]}"))


if __name__ == "__main__":
    sys.exit(main())
