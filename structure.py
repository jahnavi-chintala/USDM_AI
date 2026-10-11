"""Document structure of one converted protocol: section tree, stable section ids, ICH M11 roles,
and multi-signal heading levels.

The consumer is an agent that builds USDM 4 data from one protocol at a time. It needs to find
"the inclusion criteria", "the Schedule of Activities" or "the amendment history" in any sponsor's
template without hard-coded section names, and to cite every value by section. This module gives it

  build_section_index(doc) -> SectionIndex
      Pure function of the FINAL post-processed DoclingDocument (headings as levelled by
      postprocess). Every body item belongs to exactly one section; items before the first
      heading belong to the front-matter section "S0".
  tag_roles(index, doc)
      Role tagging (ROLES, below); called by build_section_index.
  assign_heading_levels(doc, pdf, settings=None, log=None) -> int
      Drop-in replacement for protocol_postprocess.fix_heading_levels: the PDF outline when it
      matches (results identical to fix_heading_levels), otherwise numbering with per-document
      scheme detection + TOC entries + level-1 consensus, typography as tie-breaker.
  section_numbers(doc) -> {heading ref: "5.1"}
      Accepted section numbers: the longest successor chain (not greedy), any numbering scheme.
  reading_order_moves(doc) / repair_reading_order(doc, log=None) -> int
      Text the converter read before a numbered heading although it lies below that heading on the
      same page; repair_reading_order (a post-processing step) moves it there in the document body,
      so chunks, parents and protocol_api all see the repaired order.

Sections and ids
  "S" + accepted number ("S5.1"; Roman "IV." and "Section 4:" normalise to "S4"). Unnumbered
  sections: "S~" + 8 hex of sha1(parent id + "/" + normalised title) + "-" + ordinal among
  sections with the same hash ("S~1a2b3c4d-1"). "S0" is the front matter (always present).
  Ids depend only on the heading texts and the tree, so they are stable across runs and do not
  move when an unrelated section is added elsewhere. Numbers come from section_numbers(): the
  longest successor chain, with a constant level offset allowed (an outline whose root bookmark is
  the document title puts every section one level deeper; the tree is then built one level up).
  Section.level is the depth in the section tree (0 = front matter, 1 = top level).
  Section.item_refs are the top-level body units directly in the section, in reading order:
  paragraphs, list groups, tables, pictures and inline minor headings. Their descendants
  (list items, cells' captions/footnotes, figure text) map to the same section via section_of().

Minor headings
  Label-like headings stay inline in their section (in item_refs and minor_headings) instead of
  becoming tree nodes: text ending in ":", lettered "a." / "(a)" / "i." / "1)" headings, table or
  figure titles, bullets, very long headings (> 100 chars, sentences detected as headings),
  short headings repeated under the same parent ("Hypotension" under each CRS grade), headings
  repeated 3+ times in the document (running headers), and short headings that directly follow
  a minor heading at the same level ("Grade 1 CRS:" -> "Fever"). Never minor: headings with an
  accepted section number, an appendix label, a strong role match (score >= 0.8) or listed in a
  TOC table of the document. A criteria/objectives label with a colon ("Inclusion Criteria:")
  is a section unless its parent already has that role ("Other Exclusions:" inside 5.2); it never
  outranks the numbered heading it follows (it nests one level below it).

Roles (ICH M11 / TransCelerate CPT; see ROLE_INFO)
  Assigned per section by (a) soft similarity of the heading text (numbering stripped,
  normalised tokens, singular, trial->study, patient/subject->participant) to canonical titles
  and synonyms, (b) content cues of the section's own items (a mark-heavy table -> soa; a
  2-column acronym table -> abbreviations; a version/date table -> amendment_history; a TOC
  table -> toc), (c) inheritance from the parent when a section has no own signal. Some parents
  constrain their subtree: a synopsis, amendment history, eligibility list, SoA, AE appendix ...
  keeps its role for its children (so quoted section titles inside an amendment summary do not
  look like real sections), and a statistics subtree only contains statistical roles.
  role_score in [0, 1] (1 = exact canonical title; inherited = parent score x 0.9; content cue
  0.7); role None when nothing fits.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable

from docling_core.types.doc import (ContentLayer, DocItemLabel, DoclingDocument, GroupItem, ListItem, PictureItem,
                                    RefItem, SectionHeaderItem, TableItem, TextItem, TitleItem)

FRONT_MATTER = "Front matter"  # same name as chunk_protocol.FRONT_MATTER
INDEX_VERSION = 1

# =========================================================================== roles

ROLES = [
    "title_page", "toc", "synopsis", "schema", "soa", "introduction", "objectives_endpoints", "estimands",
    "design", "population", "eligibility", "inclusion", "exclusion", "lifestyle", "screen_failures",
    "interventions", "dose_modification", "concomitant_therapy", "discontinuation", "assessments",
    "assessments_efficacy", "assessments_safety", "ae_reporting", "pk_pd_biomarkers", "statistics",
    "sample_size", "interim_analysis", "regulatory_ethics", "abbreviations", "references",
    "amendment_history", "appendix_other",
]

ROLE_INFO = {  # role: (ICH M11 section, TransCelerate CPT section, what the USDM agent finds there)
    "title_page": ("Title page incl. sponsor signature", "Title page, signature pages",
                   "StudyTitle, StudyIdentifier, sponsor Organization, version/date; investigator agreement"),
    "toc": ("-", "Table of contents, lists of tables/figures", "navigation only; skip"),
    "synopsis": ("1.1 Protocol Synopsis", "1 Protocol Summary / 1.1 Synopsis",
                 "summary copies of design, objectives, population (body sections are authoritative)"),
    "schema": ("1.2 Trial Schema", "1.2 Schema", "design figure: Arms, Epochs, StudyCells"),
    "soa": ("1.3 Schedule of Activities", "1.3 SoA",
            "Encounters, Activities, ScheduledActivityInstances, Timings, footnotes"),
    "introduction": ("2 Introduction (purpose, benefit/risk)", "2 Introduction (rationale, background, benefit/risk)",
                     "rationale, background"),
    "objectives_endpoints": ("3 Trial Objectives and Associated Estimands", "3 Objectives and Endpoints",
                             "Objectives, Endpoints (levels)"),
    "estimands": ("3.x Estimands", "3 Objectives, Estimands and Endpoints; 9.x", "Estimand, IntercurrentEvent"),
    "design": ("4 Trial Design", "4 Study Design (overall design, rationale, dose justification, end of study)",
               "InterventionalStudyDesign attributes, Epochs, end-of-study definition"),
    "population": ("5 Trial Population (5.1-5.2)", "5 Study Population",
                   "StudyDesignPopulation (planned enrollment, age, sex)"),
    "eligibility": ("5.3-5.4 (combined list)", "5.1-5.2 (combined)", "EligibilityCriterion (both categories)"),
    "inclusion": ("5.3 Inclusion Criteria", "5.1 Inclusion Criteria", "EligibilityCriterion category Inclusion"),
    "exclusion": ("5.4 Exclusion Criteria", "5.2 Exclusion Criteria", "EligibilityCriterion category Exclusion"),
    "lifestyle": ("5.5 Lifestyle Considerations", "5.3 Lifestyle Considerations; contraception guidance",
                  "restrictions, contraception"),
    "screen_failures": ("5.6 Screen Failure and Rescreening", "5.4 Screen Failures", "rescreening rules"),
    "interventions": ("6 Trial Intervention (6.1-6.8, 6.10)", "6 Study Intervention (6.1-6.4, 6.7)",
                      "StudyIntervention, Administration (dose, route, frequency), randomisation, blinding"),
    "dose_modification": ("6.4/6.x Dose modification", "6.6 Dose Modification", "dose levels, modification rules"),
    "concomitant_therapy": ("6.9 Concomitant Therapy (+ rescue)", "6.5 Concomitant Therapy",
                            "permitted/prohibited medications"),
    "discontinuation": ("7 Discontinuation / Withdrawal", "7 Discontinuation", "discontinuation rules"),
    "assessments": ("8 Trial Assessments and Procedures", "8 Study Assessments and Procedures",
                    "procedures (Activities, Procedures)"),
    "assessments_efficacy": ("8.2 Efficacy Assessments", "8.1 Efficacy Assessments", "efficacy procedures"),
    "assessments_safety": ("8.3 Safety Assessments", "8.2 Safety Assessments; clinical laboratory tests appendix",
                           "safety procedures, lab panels (BiomedicalConcepts)"),
    "ae_reporting": ("9 AEs, SAEs, pregnancy", "8.3 AEs/SAEs; AE definitions appendix", "safety reporting"),
    "pk_pd_biomarkers": ("8.7-8.10 PK, PD, genetics, biomarkers, immunogenicity", "8.5-8.9",
                         "PK/PD/biomarker procedures"),
    "statistics": ("10 Statistical Considerations", "9 Statistical Considerations",
                   "analysis sets, hypotheses, analyses"),
    "sample_size": ("10.9 Sample Size Determination", "9.2 Sample Size Determination", "planned enrollment"),
    "interim_analysis": ("10.8 Interim Analyses", "9.5 Interim Analyses", "interim analyses"),
    "regulatory_ethics": ("11 Trial Oversight and Other General Considerations", "10.1 Regulatory, Ethical, Oversight",
                          "consent, committees, data protection, quality"),
    "abbreviations": ("13 Glossary of Terms and Abbreviations", "Abbreviations appendix", "Abbreviation"),
    "references": ("14 References", "11 References", "-"),
    "amendment_history": ("12.x Prior protocol amendments / summary of changes", "Protocol Amendment History",
                          "StudyAmendment (number, date, reasons, changes)"),
    "appendix_other": ("12 Appendix: supporting details", "10.x other appendices",
                       "other appendices (scales, country requirements, guidance)"),
}

_ROLE_PHRASES = {
    "title_page": ["title page", "cover page", "protocol title", "sponsor signature", "sponsor signatory",
                   "signature page", "protocol approval", "approval page", "investigator agreement",
                   "investigator signature", "investigator statement", "protocol signature", "protocol signatures",
                   "sponsor approval"],
    "toc": ["table of contents", "contents", "list of tables", "list of figures", "list of appendices",
            "list of attachments", "list of in text tables", "list of in text figures", "table of figures"],
    "synopsis": ["synopsis", "protocol synopsis", "protocol summary", "study summary", "clinical study synopsis",
                 "brief summary", "brief protocol synopsis"],
    "schema": ["schema", "study schema", "trial schema", "schematic", "study schematic", "study design schema",
               "study diagram", "design diagram", "study scheme"],
    "soa": ["schedule of activities", "schedule of assessments", "schedule of events", "schedule of evaluations",
            "schedule of procedures", "time and events", "time and events schedule", "study calendar",
            "study flow chart", "flow chart", "flowchart", "visit schedule", "study schedule", "soa",
            "study parameters", "study table"],
    "introduction": ["introduction", "background", "study rationale", "trial rationale", "rationale",
                     "background and rationale", "benefit risk assessment", "benefit risk", "risk assessment",
                     "benefit assessment", "overall benefit risk conclusion", "purpose of study", "disease background",
                     "nonclinical studies", "clinical experience"],
    "objectives_endpoints": ["objectives", "objective", "objectives and endpoints", "study objectives",
                             "primary objective", "secondary objective", "exploratory objective", "endpoints",
                             "endpoint", "study endpoints", "objectives estimands and endpoints",
                             "objectives and associated estimands", "outcome measures", "primary endpoint",
                             "secondary endpoint"],
    "estimands": ["estimand", "estimands", "intercurrent events", "intercurrent event"],
    "design": ["study design", "trial design", "overall design", "design", "investigational plan",
               "scientific rationale for study design", "rationale for study design", "justification for dose",
               "dose justification", "dose rationale", "end of study definition", "end of study", "end of trial",
               "study duration", "duration of study", "duration of study period", "study period",
               "discussion of design and control", "summary of study design", "number of participants",
               "participant input into design"],
    "population": ["study population", "trial population", "population", "selection of study population",
                   "participant selection", "selection of participants", "target population", "study participants",
                   "selection and withdrawal of participants"],
    "eligibility": ["eligibility criteria", "eligibility", "criteria for enrollment", "enrollment criteria",
                    "entry criteria", "selection criteria", "eligibility checklist"],
    "inclusion": ["inclusion criteria", "inclusion criterion", "inclusion", "eligibility criteria inclusion"],
    "exclusion": ["exclusion criteria", "exclusion criterion", "exclusion", "eligibility criteria exclusion"],
    "lifestyle": ["lifestyle considerations", "lifestyle restrictions", "lifestyle", "lifestyle and dietary requirements",
                  "meals and dietary restrictions", "dietary restrictions", "caffeine alcohol and tobacco",
                  "activity restrictions", "contraception", "contraceptive guidance", "contraceptive requirements",
                  "birth control", "contraception guidance"],
    "screen_failures": ["screen failures", "screen failure", "rescreening", "screen failure and rescreening",
                        "screening failures"],
    "interventions": ["study intervention", "study interventions", "study treatment", "trial intervention",
                      "investigational product", "investigational medicinal product", "study drug",
                      "study medication", "treatment plan", "treatments", "treatment", "treatment administered",
                      "dosage and administration", "dosing and administration", "administration schedule",
                      "study intervention administered", "preparation handling storage accountability",
                      "preparation handling storage and accountability", "randomization and blinding",
                      "randomization", "randomization procedures", "blinding", "unblinding", "participant assignment",
                      "treatment assignment", "method of treatment assignment", "measures to minimize bias",
                      "study intervention compliance", "treatment compliance", "treatment of overdose", "overdose",
                      "intervention after end of study", "treatment after end of study", "packaging and labeling",
                      "materials and supplies", "drug supplies", "investigational agent", "agent information",
                      "pharmaceutical information", "drug information", "study drug information", "dose administration",
                      "drug formulation", "drug formulation and procurement", "formulation", "agent formulation",
                      "handling dispensing and administration", "storage requirements", "drug accountability",
                      "investigational product handling"],
    "dose_modification": ["dose modification", "dose modifications", "dose adjustment", "dose reduction",
                          "dose delay", "dose interruption", "dose modifications and toxicity management",
                          "toxicity management", "management of toxicity", "redosing", "dose level modification",
                          "dose modification guidelines"],
    "concomitant_therapy": ["concomitant therapy", "concomitant medication", "concomitant therapies",
                            "prior and concomitant therapy", "permitted medication", "prohibited medication",
                            "prohibited therapy", "permitted therapy", "prohibited therapies and restrictions",
                            "allowed medicine and therapy", "disallowed medicine and therapy", "rescue medicine",
                            "rescue medication", "rescue therapy", "supportive care", "premedication",
                            "drug interactions", "concomitant"],
    "discontinuation": ["discontinuation", "discontinuation of study intervention", "participant discontinuation",
                        "discontinuation from study treatment", "discontinuation of study treatment",
                        "discontinuation from study", "discontinuation of study", "early discontinuation",
                        "discontinuation withdrawal", "withdrawal", "withdrawal from study", "withdrawal of consent",
                        "lost to follow up", "early termination", "discontinuation criteria",
                        "removal of participants", "withdrawal of participants", "off study criteria",
                        "criteria for removal", "participant disposition", "disposition criteria",
                        "replacement of participants", "stopping rules"],
    "assessments": ["study assessments and procedures", "study assessments", "assessments and procedures",
                    "study procedures", "procedures", "assessments", "evaluations", "study evaluations",
                    "efficacy and safety assessments", "efficacy and safety evaluations",
                    "efficacy pharmacokinetic and safety evaluations", "screening procedures",
                    "baseline assessments", "study visits", "health economics", "medical resource utilization",
                    "health economics or medical resource utilization", "required data", "data collection"],
    "assessments_efficacy": ["efficacy assessments", "efficacy", "efficacy measures", "efficacy evaluations",
                             "efficacy variables", "assessment of efficacy", "measurement of effect",
                             "response criteria", "response assessment", "tumor response", "antitumor effect",
                             "disease assessment", "clinical outcome assessments", "participant reported outcomes",
                             "quality of life", "efficacy criteria"],
    "assessments_safety": ["safety assessments", "safety", "safety measures", "safety evaluations",
                           "physical examination", "physical examinations", "vital signs", "electrocardiograms",
                           "clinical safety laboratory assessments", "clinical laboratory tests", "laboratory tests",
                           "laboratory assessments", "safety laboratory tests", "safety monitoring",
                           "pregnancy testing", "suicidal ideation", "other safety measures"],
    "ae_reporting": ["adverse events", "adverse event", "adverse events and serious adverse events",
                     "serious adverse events", "adverse event reporting", "reporting of adverse events",
                     "adverse event definitions", "definition of adverse event", "safety reporting", "pregnancy",
                     "exposure during pregnancy", "collection of pregnancy information",
                     "adverse events definitions and procedures for recording evaluating follow up and reporting",
                     "adverse event reporting requirements", "expedited reporting", "ae and sae", "aes and saes",
                     "clinical adverse events", "adverse events of special interest", "reporting of sae",
                     "medical device incidents", "product complaints", "complaint handling"],
    "pk_pd_biomarkers": ["pharmacokinetics", "pharmacokinetic", "pharmacodynamics", "pharmacodynamic", "biomarkers",
                         "biomarker", "genetics", "pharmacogenomics", "pharmacogenetics", "immunogenicity",
                         "immunogenicity assessments", "biospecimens", "correlative studies",
                         "translational research", "exploratory biomarker", "banked biospecimens", "bioanalysis",
                         "pk", "pd", "pk pd and biomarkers sampling", "blood sampling summary"],
    "statistics": ["statistical considerations", "statistical methods", "statistics", "statistical analysis",
                   "statistical analyses", "data analysis methods", "analysis methods", "statistical hypotheses",
                   "analysis sets", "analysis populations", "populations for analyses", "statistical analysis plan",
                   "analysis", "analyses", "statistical methodology", "multiplicity", "subgroup analyses",
                   "statistical considerations and data analysis", "primary analysis", "secondary analyses",
                   "statistical"],
    "sample_size": ["sample size", "sample size determination", "sample size justification",
                    "determination of sample size", "power calculation", "sample size and power"],
    "interim_analysis": ["interim analysis", "interim analyses", "interim", "interim efficacy analyses",
                         "interim safety analyses", "futility analysis"],
    "regulatory_ethics": ["regulatory ethical and study oversight considerations", "regulatory and ethical considerations",
                          "ethics", "ethical considerations", "ethical review", "regulatory considerations",
                          "regulatory requirements", "informed consent", "informed consent process",
                          "institutional review board", "independent ethics committee", "data protection",
                          "confidentiality", "data quality assurance", "quality assurance", "quality control",
                          "source documents", "record retention", "publication policy",
                          "dissemination of clinical study data", "study and site start and closure",
                          "study and site closure", "financial disclosure", "committees structure",
                          "data monitoring committee", "independent data monitoring committee", "steering committee",
                          "safety review committee", "study monitoring", "audit and inspection", "data management",
                          "good clinical practice", "protocol deviations", "study oversight", "trial oversight",
                          "study governance", "investigator responsibilities", "administrative considerations",
                          "regulatory ethical and study oversight", "study governance regulatory and ethical considerations",
                          "administrative", "administrative procedures", "protocol compliance", "study records",
                          "case report form", "electronic case report form", "data collection and management"],
    "abbreviations": ["abbreviations", "list of abbreviations", "abbreviations and definitions", "glossary",
                      "glossary of terms", "definitions of terms", "abbreviations and definitions of terms",
                      "acronyms", "list of abbreviations and definitions of terms"],
    "references": ["references", "bibliography", "literature references", "reference list", "list of references"],
    "amendment_history": ["protocol amendment history", "amendment history", "document history", "revision history",
                          "summary of changes", "protocol amendment summary of changes",
                          "protocol amendment summary of changes table", "summary of changes table",
                          "protocol amendments", "change history", "version history", "protocol history",
                          "rationale for amendment", "overall rationale for amendment", "amendment summary",
                          "changes to protocol", "protocol amendment"],
    "appendix_other": ["appendix", "appendices", "supporting documentation", "attachments", "annex", "annexes",
                       "supporting documentation and operational considerations", "country specific requirements",
                       "list of attachments"],
}

# =========================================================================== text normalisation

_STOP = {"of", "the", "and", "or", "for", "to", "in", "on", "a", "an", "with", "by", "from", "at", "as", "its",
         "their", "this", "any", "all", "per", "vs", "versus", "including", "other", "be", "is", "are", "was",
         "up", "into", "s", "via", "and/or", "if", "when", "after", "during", "within"}
_GENERIC = {"study", "participant", "assessment", "evaluation", "procedure", "consideration", "general",
            "information", "overview", "description", "plan", "data", "section", "protocol", "criterion",
            "management", "requirement", "definition", "detail", "guideline", "guidance", "trial", "measure",
            "test", "method", "summary"}
_IRREG = {"criteria": "criterion", "analyses": "analysis", "hypotheses": "hypothesis", "data": "data",
          "aes": "ae", "saes": "sae", "biospecimens": "biospecimen", "diagnoses": "diagnosis", "status": "status",
          "analysis": "analysis", "basis": "basis", "previous": "previous", "various": "various",
          "pharmacokinetics": "pharmacokinetic", "pharmacodynamics": "pharmacodynamic", "genetics": "genetic",
          "economics": "economic", "statistics": "statistic", "statistical": "statistic", "ethics": "ethic",
          "ethical": "ethic", "medicine": "medication", "medicines": "medication", "medications": "medication",
          "therapies": "therapy", "procedures": "procedure", "considerations": "consideration",
          "regulations": "regulation", "regulatory": "regulatory"}
_CANON = {"trial": "study", "subject": "participant", "patient": "participant", "volunteer": "participant",
          "randomisation": "randomization", "randomised": "randomized", "minimise": "minimize",
          "programme": "program", "haematology": "hematology", "drug": "drug", "intervention": "intervention",
          "investigational": "investigational", "enrolment": "enrollment", "behaviour": "behavior",
          "tumour": "tumor", "labelling": "labeling", "evaluating": "evaluation", "assessing": "assessment",
          "reporting": "report", "reported": "report", "recording": "record", "aes": "ae", "saes": "sae"}


def _stem(w: str) -> str:
    if w in _IRREG:
        return _IRREG[w]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is", "sis")):
        return w[:-1]
    return w


def _tokens(text: str) -> list[str]:
    t = (text or "").lower().replace("&", " and ").replace("isation", "ization").replace("omise", "omize")
    out = []
    for w in re.findall(r"[a-z0-9]+", t):
        w = _CANON.get(w, w)
        w = _stem(w)
        w = _CANON.get(w, w)
        if w not in _STOP:
            out.append(w)
    return out


def _w(t: str) -> float:
    return 0.3 if t in _GENERIC else 1.0


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _dice(a: Iterable[str], b: Iterable[str]) -> float:
    a, b = set(a), set(b)
    return 2 * len(a & b) / (len(a) + len(b)) if a and b else 0.0


# =========================================================================== section labels

@dataclass(frozen=True)
class Label:
    """A heading's numbering label. kind: arabic | roman | alpha | section | appendix | list.

    values: the label as integers ("4.2.1" -> (4, 2, 1); "IV" -> (4,); "B" -> (2,)); key: the
    label as written, normalised for matching ("4.2.1", "IV", "LZZT.1"); rest: the heading text
    after the label; alt: the other reading of an ambiguous letter ("I." = Roman 1 or letter 9).
    """
    kind: str
    values: tuple = ()
    key: str = ""
    rest: str = ""
    alt: tuple | None = None


NUMBERED_KINDS = {"arabic", "roman", "alpha", "section"}

_ARABIC = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(?=\S)")  # = protocol_postprocess._SECTION_NUM
_SECTION_WORD = re.compile(r"(?i)^(?:section|chapter|part)\s+(\d+(?:\.\d+)*|[IVX]+)(?![\w])\.?\s*[:.\-–—]?\s*(?=\S)")
_APPENDIX = re.compile(r"(?i)^(?:protocol\s+)?(?:appendix|attachment|annex|addendum|supplement)\s+(?:no\.?\s*)?"
                       r"((?-i:[A-Z]{1,6})(?:[.\-]?\d+)*|\d+(?:[.\-]\d+)*)(?=$|[\s.:,;)\-–—])[.:\-–—]?\s*")
_ROMAN = re.compile(r"^(?=[IVX])(X{0,3}(?:IX|IV|V?I{0,3}))(?:[.):]\s+|\s{2,}|\t|\s+(?=[A-Z]{2}))(?=\S)")
_ALPHA = re.compile(r"^\(?([A-Z])[.)]\s+(?=\S)")
_LIST = re.compile(r"^(?:\(?[a-z]\)|[a-z][.)]|\(?(?:i{1,3}|iv|vi{0,3}|ix|x)[.)]|\(\d+\)|\d+\))\s+(?=\S)")
_ROMAN_VAL = {"I": 1, "V": 5, "X": 10}


def _roman(s: str) -> int:
    total, prev = 0, 0
    for ch in reversed(s):
        v = _ROMAN_VAL[ch]
        total, prev = (total - v, prev) if v < prev else (total + v, v)
    return total


def _drop_zeros(t: tuple) -> tuple:
    while len(t) > 1 and t[-1] == 0:
        t = t[:-1]
    return t


def parse_label(text: str) -> Label | None:
    """The numbering label of a heading or TOC entry, or None."""
    t = _clean(text)
    if not t:
        return None
    if m := _ARABIC.match(t):
        vals = _drop_zeros(tuple(int(x) for x in m.group(1).split(".")))
        return Label("arabic", vals, ".".join(map(str, vals)), t[m.end():])
    if m := _SECTION_WORD.match(t):
        raw = m.group(1)
        vals = (_roman(raw.upper()),) if raw.upper().strip("IVX") == "" else \
            _drop_zeros(tuple(int(x) for x in raw.split(".")))
        return Label("section", vals, ".".join(map(str, vals)), t[m.end():])
    if m := _APPENDIX.match(t):
        return Label("appendix", (), m.group(1).upper().replace("-", "."), t[m.end():])
    if (m := _ROMAN.match(t)) and m.group(1):
        r = m.group(1)
        alt = ("alpha", (ord(r) - 64,)) if len(r) == 1 else None
        return Label("roman", (_roman(r),), r, t[m.end():], alt)
    if m := _ALPHA.match(t):
        return Label("alpha", (ord(m.group(1)) - 64,), m.group(1), t[m.end():])
    if m := _LIST.match(t):
        return Label("list", (), "", t[m.end():])
    return None


def _rest(text: str, label: Label | None = None) -> str:
    """Heading text without its label(s) ("10.2 Appendix 2: Clinical Laboratory Tests" -> "Clinical ...")."""
    label = label if label is not None else parse_label(text)
    rest = label.rest if label is not None else _clean(text)
    if label is not None and label.kind != "appendix" and (m := _APPENDIX.match(rest)):
        rest = rest[m.end():]
    return rest


def _is_successor(prev: tuple, cand: tuple, max_gap: int) -> bool:
    """Same rule as protocol_postprocess._is_successor (kept identical; see there)."""
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


class Scheme:
    """Per-document numbering scheme: how each label kind maps to a section-number tuple.

    absolute mode (Arabic dotted numbers present): Arabic labels are their own tuples, "Section 4"
    and a Roman "IV." are level-1 numbers (4,), single letters are not section numbers.
    contextual mode (no dotted numbers; "I." / "A." / "1." templates): each kind gets a level from
    how its values restart ("A." restarting at 1 after every Roman heading is nested under Roman);
    a label's tuple is the previous accepted number's prefix plus its own value.
    """

    def __init__(self, mode: str, levels: dict[str, int], roman_active: bool, alpha_active: bool):
        self.mode, self.levels, self.roman_active, self.alpha_active = mode, levels, roman_active, alpha_active

    @classmethod
    def detect(cls, labels: list[Label | None]) -> "Scheme":
        labs = [lb for lb in labels if lb is not None]
        dotted = sum(1 for lb in labs if lb.kind in ("arabic", "section") and len(lb.values) >= 2)
        roman_only = sum(1 for lb in labs if lb.kind == "roman" and lb.alt is None)
        alpha_only = sum(1 for lb in labs if lb.kind == "alpha")
        roman_active, alpha_active = roman_only >= 1, alpha_only >= 2  # one "II." makes "I." Roman
        if dotted >= 3 or not any(lb.kind in ("roman", "alpha") for lb in labs):
            return cls("absolute", {}, roman_active, alpha_active)
        resolved = cls("contextual", {}, roman_active, alpha_active).resolve_all(labels)
        seq = [(i, lb) for i, lb in enumerate(resolved) if lb is not None and lb.kind in NUMBERED_KINDS]
        votes: dict[str, Counter] = defaultdict(Counter)
        first: dict[str, int] = {}
        for pos, (i, lb) in enumerate(seq):
            first.setdefault(lb.kind, pos)
            if lb.values and lb.values[-1] == 1:
                prev = next((p for _, p in reversed(seq[:pos]) if p.kind != lb.kind), None)
                if prev is not None:
                    votes[lb.kind][prev.kind] += 1
        parent = {k: (v.most_common(1)[0][0] if v else None) for k, v in votes.items()}
        parent["section"] = None
        if seq:  # the kind of the first numbered heading is the top level
            parent[seq[0][1].kind] = None
        levels: dict[str, int] = {}

        def level(k: str, seen: tuple = ()) -> int:
            if k in levels:
                return levels[k]
            p = parent.get(k)
            if p is None or p in seen or p == k or p not in first:
                return 1
            return level(p, seen + (k,)) + 1

        for k in first:
            levels[k] = level(k)
        return cls("contextual", levels, roman_active, alpha_active)

    def resolve_all(self, labels: list[Label | None]) -> list[Label | None]:
        """Ambiguous letters ("I.", "V.", "X.") read as Roman or as alphabetic per document."""
        out, last_alpha = [], 0
        for lb in labels:
            if lb is not None and lb.kind == "roman" and lb.alt is not None:
                as_alpha = lb.alt[1][0]
                alpha = (self.alpha_active and not self.roman_active) or \
                        (self.alpha_active and last_alpha == as_alpha - 1)
                if alpha:
                    lb = Label("alpha", (as_alpha,), lb.key, lb.rest)
            if lb is not None and lb.kind == "alpha":
                last_alpha = lb.values[0]
            out.append(lb)
        return out

    def absolute(self, lb: Label | None) -> tuple | None:
        """The tuple of a label that does not depend on context, else None."""
        if lb is None or lb.kind not in NUMBERED_KINDS or not lb.values:
            return None
        if lb.kind in ("arabic", "section"):
            if self.mode == "absolute" or len(lb.values) >= 2 or self.levels.get(lb.kind, 1) == 1:
                return lb.values
            return None
        if lb.kind == "roman" and (self.mode == "absolute" or self.levels.get("roman", 1) == 1):
            return lb.values
        if lb.kind == "alpha" and self.mode == "contextual" and self.levels.get("alpha", 1) == 1:
            return lb.values
        return None

    def tuple_for(self, lb: Label | None, prev: tuple) -> tuple | None:
        if lb is None or lb.kind not in NUMBERED_KINDS or not lb.values:
            return None
        if (t := self.absolute(lb)) is not None:
            return t
        if self.mode == "absolute":
            return None
        lvl = self.levels.get(lb.kind, 1)
        if len(prev) < lvl - 1:
            return None
        return tuple(prev[:lvl - 1]) + (lb.values[-1],)


def _longest_chain(cands: list[tuple[int, tuple]], gap: int) -> list[tuple[int, tuple]]:
    """Longest subsequence in which each number is a successor of the previous one (first from ())."""
    n = len(cands)
    best = [0] * n
    pred = [-1] * n
    for j, (_, t) in enumerate(cands):
        if _is_successor((), t, gap):
            best[j] = 1
        for i in range(j):
            if best[i] and best[i] + 1 > best[j] and _is_successor(cands[i][1], t, gap):
                best[j], pred[j] = best[i] + 1, i
    if not n or not max(best):
        return []
    j = best.index(max(best))
    out = []
    while j >= 0:
        out.append(cands[j])
        j = pred[j]
    return out[::-1]


def section_numbers(doc: DoclingDocument, max_gap: int = 3, return_offset: bool = False):
    """Heading self_ref -> accepted section number ("10.4.3"; "IV" -> "4"; "Section 2:" -> "2").

    Like protocol_postprocess.section_numbers (a number is accepted when the heading's level
    equals its depth and it continues the outline), but (1) the longest successor chain is taken
    instead of the greedy one, so an early quoted "3.1" (in an amendment summary) cannot block the
    real sections 1-3; (2) Roman / "Section N" level-1 labels count; (3) a constant level offset
    is allowed (an outline whose single root bookmark is the document title puts every section
    one level deeper). On ordinary documents the result equals protocol_postprocess's.
    """
    heads = [h for h, _ in doc.iterate_items() if isinstance(h, SectionHeaderItem)]
    labels = [parse_label(h.text) for h in heads]
    scheme = Scheme.detect(labels)
    labels = scheme.resolve_all(labels)
    cands = [(i, t) for i, lb in enumerate(labels) if (t := scheme.absolute(lb)) is not None]
    best_k, best = 0, []
    for k in (0, 1, 2):
        chain = _longest_chain([(i, t) for i, t in cands if heads[i].level - k == len(t)], max_gap)
        if len(chain) > len(best):
            best_k, best = k, chain
    out = {heads[i].self_ref: ".".join(map(str, t)) for i, t in best}
    return (out, best_k) if return_offset else out


# =========================================================================== role scoring

_PHRASES: list[tuple[str, frozenset, float, int]] = []
for _role, _plist in _ROLE_PHRASES.items():
    for _p in _plist:
        _toks = frozenset(_tokens(_p))
        if _toks:
            _PHRASES.append((_role, _toks, sum(_w(t) for t in _toks), len(_toks)))
_ROLE_ORDER = {r: i for i, r in enumerate(ROLES)}
ROLE_MIN = 0.65    # a heading has an own role signal at or above this score
ROLE_STRONG = 0.75  # strong enough to escape a soft parent constraint / break a front-matter run


def heading_role(text: str) -> tuple[str | None, float]:
    """(role, score) from the heading text alone (labels stripped); (None, 0) when nothing fits.

    Exact canonical title or synonym = 1.0; a synonym contained in a longer title = 0.5 + 0.5 x
    its (generic-word-discounted) share of the title, at least 0.74 when the title starts with it
    ("Protocol Amendment ... Summary"); partial overlap = 0.7 x weighted Dice.
    """
    seq = _tokens(_rest(text))
    toks = set(seq)
    if not toks:
        return None, 0.0
    wt = sum(_w(t) for t in toks)
    best = (0.0, 0, -len(ROLES), None)
    for role, p, wp, n in _PHRASES:
        if p <= toks:
            if p == toks:
                s = 1.0
            elif wp < 1.0:
                continue
            else:
                s = 0.5 + 0.5 * wp / wt
                if n >= 2 and set(seq[:n]) == p:  # the title starts with the synonym (an own signal,
                    s = max(s, ROLE_STRONG - 0.01)  # but not strong enough to leave a parent's family)
        else:
            inter = p & toks
            wi = sum(_w(t) for t in inter)
            if wi < 1.0:
                continue
            s = 0.7 * 2 * wi / (wp + wt)
        key = (round(s, 6), n, -_ROLE_ORDER[role], role)
        if key > best:
            best = key
    return (best[3], best[0]) if best[3] is not None and best[0] > 0 else (None, 0.0)


# =========================================================================== section index

@dataclass
class Section:
    section_id: str
    number: str | None
    title: str
    level: int
    parent_id: str | None
    heading_ref: str | None
    item_refs: list[str] = field(default_factory=list)
    pages: list[int] = field(default_factory=list)
    role: str | None = None
    role_score: float = 0.0
    minor_headings: list[str] = field(default_factory=list)


@dataclass
class SectionIndex:
    sections: list[Section]
    by_id: dict[str, Section] = field(default_factory=dict)
    ref_section: dict[str, str] = field(default_factory=dict)  # any body item ref -> section_id

    def __post_init__(self) -> None:
        if not self.by_id:
            self.by_id = {s.section_id: s for s in self.sections}
        self._children: dict[str | None, list[Section]] | None = None

    def section_of(self, ref: str) -> str | None:
        """Section id of any body item ref (paragraph, list item, table, picture, their children,
        a heading); None for refs outside the body (page headers/footers) or unknown refs."""
        return self.ref_section.get(ref)

    def children(self, section_id: str | None) -> list[Section]:
        """Direct sub-sections in document order (section_id None: the top-level sections)."""
        if self._children is None:
            self._children = defaultdict(list)
            for s in self.sections:
                if s.section_id != "S0":
                    self._children[s.parent_id].append(s)
        return list(self._children.get(section_id, []))

    def path(self, section_id: str) -> list[Section]:
        """The section and its ancestors, outermost first."""
        out, s = [], self.by_id.get(section_id)
        while s is not None:
            out.append(s)
            s = self.by_id.get(s.parent_id) if s.parent_id else None
        return out[::-1]

    def subtree(self, section_id: str) -> list[Section]:
        """The section and all its descendants in document order."""
        out = [self.by_id[section_id]]
        for c in self.children(section_id):
            out += self.subtree(c.section_id)
        return out

    def with_role(self, role: str) -> list[Section]:
        return [s for s in self.sections if s.role == role]

    def to_json(self) -> dict:
        return {"version": INDEX_VERSION, "sections": [asdict(s) for s in self.sections],
                "ref_section": dict(self.ref_section)}

    @classmethod
    def from_json(cls, data: dict | str) -> "SectionIndex":
        if isinstance(data, str):
            data = json.loads(data)
        return cls([Section(**s) for s in data["sections"]], ref_section=dict(data.get("ref_section", {})))


_MINOR_LIST = re.compile(r"^(?:\(?[a-z]\)|[a-z][.)]|\(?(?:i{1,3}|iv|vi{0,3}|ix|x)[.)]|\(\d+\)|\d+\))\s")
_BULLET = re.compile(r"^[•●▪◦·\-–*>]\s*")
_CAPTION = re.compile(r"(?i)^(?:table|figure|listing|exhibit)\s+[A-Z]?\d+(?:[.\-]\d+)*\b")
_CONTAINER = re.compile(r"(?i)\b(?:appendi(?:x|ces)|supporting documentation|attachments|annex(?:es)?)\b")


def _body_sequence(doc: DoclingDocument) -> list[tuple[str, Any, list]]:
    """Reading-order events: ("heading", item, []) or ("unit", item, [descendant refs])."""
    out: list[tuple[str, Any, list]] = []

    def descendants(node) -> list[str]:
        refs, stack = [], list(getattr(node, "children", []) or [])
        while stack:
            ref = stack.pop()
            try:
                it = ref.resolve(doc)
            except Exception:
                continue
            refs.append(it.self_ref)
            stack.extend(getattr(it, "children", []) or [])
        return refs

    def visit(node) -> None:
        if getattr(node, "content_layer", ContentLayer.BODY) != ContentLayer.BODY:
            return
        if isinstance(node, (SectionHeaderItem, TitleItem)):
            out.append(("heading", node, descendants(node)))
            return
        if isinstance(node, GroupItem) and node.label.value not in ("list", "ordered_list", "inline"):
            out.append(("group", node, []))
            for ch in node.children:
                visit(ch.resolve(doc))
            return
        out.append(("unit", node, descendants(node)))

    for ref in doc.body.children:
        visit(ref.resolve(doc))
    return out


def _pages(doc: DoclingDocument, item) -> set[int]:
    pages = {p.page_no for p in getattr(item, "prov", []) or []}
    if isinstance(item, GroupItem):
        for it, _ in doc.iterate_items(root=item, with_groups=False):
            pages |= {p.page_no for p in getattr(it, "prov", []) or []}
    return pages


def _protected_titles(doc: DoclingDocument) -> set[str]:
    """Normalised texts in TOC (document index) tables: headings listed there are never minor."""
    out = set()
    for t in doc.tables:
        if t.label == DocItemLabel.DOCUMENT_INDEX:
            for c in t.data.table_cells:
                s = _norm(re.sub(r"(?:\s*\.){3,}\s*\d*\s*$", "", c.text))
                if len(s) >= 4:
                    out.add(s)
    return out


_LIST_ROLES = {"inclusion", "exclusion", "eligibility", "objectives_endpoints", "estimands"}


def _minor_reason(text: str, label: Label | None, numbered: bool, role: str | None, role_score: float,
                  protected: set[str]) -> str | None:
    """Why a heading is label-like (kept inline), or None. "colon_list" ("Inclusion Criteria:") is
    decided against the parent: a node unless the parent already has that role."""
    t = _clean(text)
    if numbered or (label is not None and label.kind == "appendix") or _norm(t) in protected:
        return None
    if _CAPTION.match(t):
        return "caption"
    if t.endswith(":"):
        return "colon_list" if role in _LIST_ROLES and role_score >= 0.8 else "colon"
    if role_score >= 0.8:
        return None
    if _MINOR_LIST.match(t) or (label is not None and label.kind == "list"):
        return "lettered"
    if _BULLET.match(t):
        return "bullet"
    if len(t) > 100:
        return "long"
    return None


def _nest_colon_labels(heads: list, numbers: dict[str, str], eff: dict[str, int], colon_list: set[str],
                       minor: set[str]) -> dict[str, int]:
    """A "Label:" section heading (colon_list) never outranks the numbered heading it follows: printed
    like a chapter title (Lilly's synopsis field "Objectives/Endpoints:", an "Other Exclusions:" at the
    level of 5.2), it still belongs to that section, which then decides whether it is a node or inline.
    Sets eff one level below that heading; returns {ref: new eff} of the changed ones."""
    changed: dict[str, int] = {}
    last: int | None = None
    for h in heads:
        r = h.self_ref
        if r in numbers:
            last = eff[r]
        elif r in colon_list:
            if last is not None and eff[r] <= last:
                eff[r] = changed[r] = last + 1
        elif r not in minor and last is not None and eff[r] <= last:
            last = None  # an unnumbered section at that level ends the numbered one
    return changed


def _heading_classes(doc: DoclingDocument, heads: list, numbers: dict[str, str]) -> tuple[set[str], set[str]]:
    """(minor, colon_list) heading refs from the heading texts alone (_minor_reason)."""
    protected = _protected_titles(doc)
    minor: set[str] = set()
    colon_list: set[str] = set()
    for h in heads:
        r = h.self_ref
        role, score = heading_role(h.text)
        reason = _minor_reason(h.text, parse_label(h.text), r in numbers, role, score, protected)
        if reason == "colon_list":
            colon_list.add(r)
        elif reason:
            minor.add(r)
    return minor, colon_list


def nest_colon_labels(doc: DoclingDocument, log: dict | None = None, max_gap: int = 3) -> int:
    """Post-processing: give "Label:" section headings that would outrank the numbered heading they
    follow the level build_section_index nests them at, so the chunker's heading paths (from item
    levels) agree with the section tree. Run after the final reading order. Returns the number changed;
    log["colon_labels_nested"] gets {item, page, level} per heading - never text."""
    heads = [x for k, x, _ in _body_sequence(doc) if k == "heading" and isinstance(x, SectionHeaderItem)]
    numbers, offset = section_numbers(doc, max_gap, return_offset=True)
    minor, colon_list = _heading_classes(doc, heads, numbers)
    eff = {h.self_ref: max(1, h.level - offset) for h in heads}
    changed = _nest_colon_labels(heads, numbers, eff, colon_list, minor)
    entries = log.setdefault("colon_labels_nested", []) if log is not None else []
    for h in heads:
        if h.self_ref in changed:
            h.level = min(changed[h.self_ref] + offset, 100)
            entries.append({"item": h, "page": h.prov[0].page_no if h.prov else None, "level": h.level})
    return len(changed)


def build_section_index(doc: DoclingDocument, *, max_gap: int = 3) -> SectionIndex:
    """Section tree of the final document; roles tagged (tag_roles). Pure: reads the document only."""
    seq = _body_sequence(doc)
    heads = [x for k, x, _ in seq if k == "heading" and isinstance(x, SectionHeaderItem)]
    numbers, offset = section_numbers(doc, max_gap, return_offset=True)
    labels = {h.self_ref: parse_label(h.text) for h in heads}
    roles = {h.self_ref: heading_role(h.text) for h in heads}
    eff = {h.self_ref: max(1, h.level - offset) for h in heads}

    minor, colon_list = _heading_classes(doc, heads, numbers)
    _nest_colon_labels(heads, numbers, eff, colon_list, minor)  # (a no-op once nest_colon_labels ran)
    # running headers detected as headings: the same unnumbered text 3+ times
    count = Counter(_norm(h.text) for h in heads)
    for h in heads:
        r = h.self_ref
        if r not in numbers and count[_norm(h.text)] >= 3 and roles[r][1] < 0.8 \
                and not (labels[r] is not None and labels[r].kind == "appendix"):
            minor.add(r)

    def is_short(h) -> bool:
        r = h.self_ref
        return (r not in numbers and len(_tokens(h.text)) <= 4 and len(h.text) <= 40 and roles[r][1] < 0.8
                and not (labels[r] is not None and labels[r].kind in ("appendix", "arabic", "roman", "section")))

    # two passes: duplicates under one parent and label runs need the tentative tree
    for _ in range(2):
        parent_of, stack, last_minor_level = {}, [], {}
        for h in heads:
            r, lv = h.self_ref, eff[h.self_ref]
            if r in minor:
                while len(stack) > 1 and eff[stack[-1].self_ref] >= lv:
                    stack.pop()
                last_minor_level[stack[-1].self_ref if stack else None] = lv
                continue
            while stack and eff[stack[-1].self_ref] >= lv:
                stack.pop()
            parent = stack[-1].self_ref if stack else None
            # label run: a short heading right after a minor heading of the same level; a criteria
            # label ("Other Exclusions:") inside a section that already has its role
            if (last_minor_level.get(parent) == lv and is_short(h)) or \
                    (r in colon_list and parent is not None and roles[parent][0] == roles[r][0]):
                minor.add(r)
                last_minor_level[parent] = lv
                continue
            last_minor_level.pop(parent, None)
            parent_of[r] = parent
            stack.append(h)
        sib = defaultdict(list)
        for h in heads:
            if h.self_ref in parent_of and is_short(h):
                sib[(parent_of[h.self_ref], _norm(h.text))].append(h.self_ref)
        for refs in sib.values():
            if len(refs) >= 2:
                minor.update(refs)

    # ---- tree
    front = Section("S0", None, FRONT_MATTER, 0, None, None)
    sections: list[Section] = [front]
    stack: list[tuple[int, Section]] = []
    cur = front
    ref_section_tmp: dict[str, Section] = {}
    pages: dict[int, set] = defaultdict(set)
    for kind, item, desc in seq:
        r = item.self_ref
        if kind == "heading" and isinstance(item, SectionHeaderItem) and r not in minor:
            lv = eff[r]
            while stack and stack[-1][0] >= lv:
                stack.pop()
            parent = stack[-1][1] if stack else None
            sec = Section("", numbers.get(r), _clean(item.text), (parent.level + 1) if parent else 1,
                          None, r)
            sec._parent_obj = parent  # type: ignore[attr-defined]
            sections.append(sec)
            stack.append((lv, sec))
            cur = sec
            ref_section_tmp[r] = sec
            pages[id(sec)] |= _pages(doc, item)
            for d in desc:
                ref_section_tmp[d] = sec
            continue
        if kind == "heading":  # minor heading (or a Title item): inline
            if isinstance(item, SectionHeaderItem):
                lv = eff[r]
                while len(stack) > 1 and stack[-1][0] >= lv:
                    stack.pop()
                cur = stack[-1][1] if stack else front
            cur.item_refs.append(r)
            cur.minor_headings.append(r)
        elif kind == "group":
            ref_section_tmp[r] = cur
            continue
        else:
            cur.item_refs.append(r)
        ref_section_tmp[r] = cur
        pages[id(cur)] |= _pages(doc, item)
        for d in desc:
            ref_section_tmp[d] = cur

    # ---- ids (parents come first in document order)
    used: Counter = Counter()
    for s in sections[1:]:
        parent = getattr(s, "_parent_obj", None)
        s.parent_id = parent.section_id if parent is not None else None
        if s.number and f"S{s.number}" not in used:
            sid = f"S{s.number}"
        else:
            h = hashlib.sha1(f"{s.parent_id or ''}/{_norm(s.title)}".encode("utf-8")).hexdigest()[:8]
            sid = f"S~{h}"
            used[sid] += 1
            sid = f"{sid}-{used[sid]}"
        used[sid] += 1
        s.section_id = sid
    for s in sections:
        s.pages = sorted(pages.get(id(s), set()))
        if hasattr(s, "_parent_obj"):
            del s._parent_obj  # type: ignore[attr-defined]
    index = SectionIndex(sections, ref_section={r: s.section_id for r, s in ref_section_tmp.items()})
    tag_roles(index, doc)
    return index


# =========================================================================== reading-order repair

def _resolve(doc: DoclingDocument, ref: str):
    return RefItem(cref=ref).resolve(doc)


def _anchor(doc: DoclingDocument, item) -> tuple[int, float, float, float] | None:
    """(page, top, left, page width) of a unit's first provenance box, top-left origin."""
    leaf = item
    if not getattr(item, "prov", None):
        leaf = next((x for x, _ in doc.iterate_items(root=item, with_groups=False) if getattr(x, "prov", None)),
                    None)
    if leaf is None or not leaf.prov:
        return None
    p = leaf.prov[0]
    page = doc.pages.get(p.page_no)
    if page is None or page.size is None:
        return None
    b = p.bbox.to_top_left_origin(page.size.height)
    return p.page_no, float(b.t), float(b.l), float(page.size.width)


def _owned_refs(doc: DoclingDocument) -> set[str]:
    """Captions/footnotes of tables and figures, and figure children: they go with their owner."""
    owned = {r.cref for it in [*doc.tables, *doc.pictures] for r in [*it.captions, *it.footnotes]}
    for pic in doc.pictures:
        owned.update(x.self_ref for x, _ in doc.iterate_items(root=pic, with_groups=True, traverse_pictures=True)
                     if x is not pic)
    return owned


def reading_order_moves(doc: DoclingDocument, index: SectionIndex | None = None,
                        owned: Iterable[str] | None = None, sections: bool = False) -> dict[str, tuple[str, str]]:
    """Units the converter's reading order put BEFORE a heading although they lie BELOW it on the same
    page (e.g. the end of a criteria list read before the page's first heading): unit ref -> (from, to
    section id).

    Conservative: text, list and minor-heading units only (never tables or figures, which float), the
    unit and the heading in the left half of the page, only backwards displacement (the unit sits in an
    earlier section than the heading geometrically above it), and only into a section with an accepted
    number. Unnumbered targets were label headings (synopsis fields, headings quoted in a revision
    history, two-column layouts), where a move was wrong on MAIN p20, PrECOG and Sanofi; numbered ones
    (Lilly 6.1) were right.

    sections=True also returns unnumbered sections whose heading lies below a numbered heading on the
    same page although the section comes before it (a centred "1. Protocol Synopsis" read after the
    synopsis fields): heading ref -> (section id, target). Such a section moves as a block (heading and
    units); one with sub-sections, or an ancestor of the target, is left alone."""
    index = index or build_section_index(doc)
    owned = _owned_refs(doc) if owned is None else owned
    order = {s.section_id: i for i, s in enumerate(index.sections)}
    heads: dict[int, list[tuple[float, str]]] = {}
    for s in index.sections:
        if s.heading_ref:
            try:
                a = _anchor(doc, _resolve(doc, s.heading_ref))
            except Exception:  # noqa: BLE001
                a = None
            if a and a[2] < a[3] * 0.5:
                heads.setdefault(a[0], []).append((a[1], s.section_id))
    def target_of(item, sid: str) -> str | None:
        """The numbered section whose heading is the nearest one above the item on its page, when the
        item (in the left half) sits in an earlier section `sid`."""
        a = _anchor(doc, item)
        if a is None or a[2] >= a[3] * 0.5 or a[0] not in heads:
            return None
        above = [(top, h) for top, h in heads[a[0]] if top < a[1] - 1.0]
        if not above:
            return None
        target = max(above)[1]
        if getattr(index.by_id[target], "number", None) is None:
            return None
        return target if target != sid and order[sid] < order[target] else None

    moves: dict[str, tuple[str, str]] = {}
    for s in index.sections:
        for ref in getattr(s, "item_refs", None) or []:  # (parents' fallback index has none)
            if ref in owned:
                continue
            try:
                item = _resolve(doc, ref)
            except Exception:  # noqa: BLE001
                continue
            textual = (isinstance(item, (TextItem, ListItem, SectionHeaderItem, TitleItem))
                       or (isinstance(item, GroupItem) and item.label.value in ("list", "ordered_list", "inline")))
            if not textual or getattr(item, "label", None) in (DocItemLabel.FOOTNOTE, DocItemLabel.CAPTION):
                continue
            target = target_of(item, s.section_id)
            if target is not None:
                moves[ref] = (s.section_id, target)
    if sections:
        for s in index.sections:
            if not s.heading_ref or s.number is not None or index.children(s.section_id):
                continue
            try:
                target = target_of(_resolve(doc, s.heading_ref), s.section_id)
            except Exception:  # noqa: BLE001
                continue
            if target is not None and s.section_id not in {p.section_id for p in index.path(target)}:
                moves[s.heading_ref] = (s.section_id, target)
    return moves


_HEADING_MOVE_KINDS = {"arabic", "roman", "section"}


def _box(doc: DoclingDocument, item) -> tuple[int, float, float, float] | None:
    """(page, top, left, right) of a unit's first provenance box, top-left origin."""
    leaf = item
    if not getattr(item, "prov", None):
        leaf = next((x for x, _ in doc.iterate_items(root=item, with_groups=False) if getattr(x, "prov", None)),
                    None)
    if leaf is None or not leaf.prov or doc.pages.get(leaf.prov[0].page_no) is None:
        return None
    p = leaf.prov[0]
    b = p.bbox.to_top_left_origin(doc.pages[p.page_no].size.height)
    return p.page_no, float(b.t), float(b.l), float(b.r)


def _side_by_side_pages(doc: DoclingDocument) -> set[int]:
    """Pages where two body boxes sit side by side (vertical overlap, horizontally apart): columns or
    label/value layouts, where vertical position alone does not give the reading order."""
    boxes: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
    for it, _ in doc.iterate_items(with_groups=False, traverse_pictures=False):
        if getattr(it, "content_layer", ContentLayer.BODY) != ContentLayer.BODY:
            continue
        for p in getattr(it, "prov", None) or []:
            page = doc.pages.get(p.page_no)
            if page is not None:
                b = p.bbox.to_top_left_origin(page.size.height)
                boxes[p.page_no].append((b.t, b.b, b.l, b.r))
    out = set()
    for page, bs in boxes.items():
        bs.sort()
        for k, (t1, b1, l1, r1) in enumerate(bs):
            for t2, b2, l2, r2 in bs[k + 1:]:
                if t2 >= b1:
                    break  # sorted by top: no later box overlaps this one vertically
                if min(b1, b2) - max(t1, t2) >= 0.5 * min(b1 - t1, b2 - t2) and (r1 <= l2 or r2 <= l1):
                    out.add(page)
                    break
            if page in out:
                break
    return out


def _late_headings(doc: DoclingDocument, entries: list) -> int:
    """Phase 1 of repair_reading_order: a heading with a section-number label read AFTER text that lies
    below it on its page (a centred chapter heading the converter reads last: Lilly's "3. Introduction"
    after 3.1 and 3.2) moves up, before that text. On a page with side-by-side boxes only text in the
    heading's own column (overlapping it horizontally) counts. Kept only if every number accepted
    before is still accepted and the heading's own number is accepted afterwards; else all are undone."""
    before = section_numbers(doc)
    columns = _side_by_side_pages(doc)
    done: list[tuple[Any, list, str | None, dict]] = []
    for h in [x for k, x, _ in _body_sequence(doc) if k == "heading" and isinstance(x, SectionHeaderItem)]:
        lb = parse_label(h.text)
        hb = _box(doc, h)
        if lb is None or lb.kind not in _HEADING_MOVE_KINDS or h.parent is None or hb is None:
            continue
        kids = h.parent.resolve(doc).children
        pos = next(i for i, r in enumerate(kids) if r.cref == h.self_ref)
        j = pos
        while j > 0:  # the run of preceding units that lie below the heading on its page
            pb = _box(doc, _resolve(doc, kids[j - 1].cref))
            if (pb is None or pb[0] != hb[0] or pb[1] <= hb[1] + 1.0
                    or (hb[0] in columns and min(pb[3], hb[3]) - max(pb[2], hb[2]) <= 0)):
                break
            j -= 1
        if j == pos:
            continue
        # ...and that run must lie above what follows the heading on its page: text read early that
        # belongs further down (Lilly p. 26, under 6.1) is left to phase 2, which places units.
        run_low = max(_box(doc, _resolve(doc, r.cref))[1] for r in kids[j:pos])
        nxt = []
        for r in kids[pos + 1:]:
            it = _resolve(doc, r.cref)
            if getattr(it, "content_layer", ContentLayer.BODY) != ContentLayer.BODY:
                continue  # a running footer after the heading
            b = _box(doc, it)
            if b is None or b[0] != hb[0]:
                break
            nxt.append(b[1])
        if nxt and run_low >= min(nxt):
            continue
        prev = kids[pos - 1].cref
        unit = kids.pop(pos)
        kids.insert(j, unit)
        entry = {"item": h, "page": hb[0], "kind": "heading", "units": pos - j, "action": "moved"}
        entries.append(entry)
        done.append((unit, kids, prev, entry))
    if not done:
        return 0
    after = section_numbers(doc)
    lost = bool(set(before) - set(after))
    n = len(done)
    for unit, kids, prev, entry in reversed(done):
        if not lost and unit.cref in after:
            continue
        kids.pop(next(i for i, r in enumerate(kids) if r.cref == unit.cref))
        kids.insert(next(i + 1 for i, r in enumerate(kids) if r.cref == prev), unit)
        entry.update(action="skipped", reason="numbers_lost" if lost else "number_not_accepted")
        n -= 1
    return n


def repair_reading_order(doc: DoclingDocument, log: dict | None = None) -> int:
    """Repair the converter's reading order where it contradicts the page geometry, in the document body
    (within each parent's children; items and refs are unchanged). Returns the number of moves kept.

    1. A heading with a section-number label read after text lying below it on its page moves up
       (_late_headings).
    2. Every unit (and unnumbered section, as a block) found by reading_order_moves(sections=True)
       moves to just after the heading it lies under (after that section's units above it on the page).
       Kept only if the rebuilt section index agrees: the unit is in its target section, or the moved
       section now comes after the target. A minor heading whose level is not below the target heading's
       climbs back out of the target (structure keeps minor headings at their own level), so moving it
       would change only the order; such moves are undone.
    log["reading_order"] gets one entry per move: item, page, kind (heading | unit | section), units
    moved, from/to section id (phase 2), action (moved | skipped), reason - never text."""
    entries = log.setdefault("reading_order", []) if log is not None else []
    n = _late_headings(doc, entries)
    index = build_section_index(doc)
    moves = reading_order_moves(doc, index, sections=True)
    blocks = {s.heading_ref: s for s in index.sections if s.heading_ref and s.heading_ref in moves}
    moved_into: dict[str, set[str]] = defaultdict(set)
    placed: list[tuple[list[str], list, str | None, dict]] = []  # (refs, siblings, original predecessor, entry)
    for ref, (frm, to) in sorted(moves.items(), key=lambda kv: _anchor(doc, _resolve(doc, kv[0]))[:2]):
        item = _resolve(doc, ref)
        a = _anchor(doc, item)
        refs = [ref, *blocks[ref].item_refs] if ref in blocks else [ref]
        entry = {"item": item, "page": a[0], "kind": "section" if ref in blocks else "unit",
                 "from_section": frm, "to_section": to, "units": len(refs)}
        head_ref = index.by_id[to].heading_ref
        head = _resolve(doc, head_ref)
        parents = {_resolve(doc, r).parent.cref if _resolve(doc, r).parent else None for r in refs}
        if head.parent is None or parents != {head.parent.cref}:
            entries.append({**entry, "action": "skipped", "reason": "different_parent"})
            continue
        kids = item.parent.resolve(doc).children
        pos = next(i for i, r in enumerate(kids) if r.cref == ref)
        if [r.cref for r in kids[pos:pos + len(refs)]] != refs:
            entries.append({**entry, "action": "skipped", "reason": "not_contiguous"})
            continue
        prev = kids[pos - 1].cref if pos > 0 else None
        units = kids[pos:pos + len(refs)]
        del kids[pos:pos + len(refs)]
        i = next(i for i, r in enumerate(kids) if r.cref == head_ref) + 1
        while i < len(kids):  # past the target section's own units that lie above this one
            c = kids[i].cref
            if index.ref_section.get(c) != to and c not in moved_into[to]:
                break
            ac = _anchor(doc, _resolve(doc, c))
            if ac is not None and (ac[0], ac[1]) > (a[0], a[1]):
                break
            i += 1
        kids[i:i] = units
        moved_into[to].update(refs)
        n += 1
        entry["action"] = "moved"
        entries.append(entry)
        placed.append((refs, kids, prev, entry))
    if placed:
        check = build_section_index(doc)
        rank = {s.heading_ref: k for k, s in enumerate(check.sections) if s.heading_ref}
        for refs, kids, prev, entry in reversed(placed):  # undo in reverse order of application
            if refs[0] in blocks:
                target_head = index.by_id[entry["to_section"]].heading_ref
                ok = rank.get(refs[0], -1) > rank.get(target_head, len(rank))
            else:
                ok = check.section_of(refs[0]) == entry["to_section"]
            if ok:
                continue
            j0 = next(i for i, r in enumerate(kids) if r.cref == refs[0])
            units = kids[j0:j0 + len(refs)]
            del kids[j0:j0 + len(refs)]
            j = 0 if prev is None else next((i + 1 for i, r in enumerate(kids) if r.cref == prev), len(kids))
            kids[j:j] = units
            entry.update(action="skipped", reason="not_in_target_after_move")
            n -= 1
    return n


# =========================================================================== role tagging

_STRICT_CHILDREN = {  # parent role -> child roles allowed; anything else inherits the parent's role
    "synopsis": {"synopsis", "schema", "soa"},
    "title_page": set(), "toc": set(), "abbreviations": set(), "references": set(), "inclusion": set(),
    "exclusion": set(), "soa": set(), "ae_reporting": set(), "regulatory_ethics": set(), "lifestyle": set(),
    "screen_failures": set(), "estimands": set(), "amendment_history": set(),
    "statistics": {"statistics", "sample_size", "interim_analysis", "estimands", "regulatory_ethics"},
    "sample_size": {"statistics", "sample_size", "interim_analysis", "estimands"},
    "interim_analysis": {"statistics", "sample_size", "interim_analysis", "estimands", "regulatory_ethics"},
    "objectives_endpoints": {"objectives_endpoints", "estimands"},
    "eligibility": {"eligibility", "inclusion", "exclusion"},
}
_STRICT_CHILDREN["introduction"] = {"introduction", "design"}
_SOFT_PARENTS = {"interventions", "dose_modification", "concomitant_therapy", "discontinuation",
                 "assessments_efficacy", "assessments_safety", "pk_pd_biomarkers"}
_SOFT_FORBIDDEN = {"objectives_endpoints", "synopsis", "toc", "title_page", "references", "introduction"}
_SOA_OVERRIDES = {None, "schema", "synopsis", "design", "assessments", "appendix_other", "title_page", "toc",
                  "introduction"}
_MARK = re.compile(r"^[xX✓✔√•●■☑](?:[a-z](?=$|[\s,;]))?(?:$|[\s,;(*†‡§¶0-9^])")
_DATE = re.compile(r"(?i)\b(?:\d{1,2}[\s\-/.](?:[a-z]{3,9}|\d{1,2})[\s\-/.,]+\d{2,4}|\d{4}-\d{2}-\d{2}|"
                   r"[a-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|[a-z]{3,9}\s+\d{4})\b")


def _grid(table: TableItem) -> list[list[str]]:
    try:
        return [[_clean(c.text) for c in row] for row in table.data.grid]
    except Exception:
        return []


def table_cues(table: TableItem) -> set[str]:
    """Content cues of one table: soa (mark-heavy grid), abbreviations (acronym list),
    amendment_history (version/amendment + date table), toc (document index)."""
    if table.label == DocItemLabel.DOCUMENT_INDEX:
        return {"toc"}
    grid = _grid(table)
    if not grid:
        return set()
    out = set()
    ncols = max(len(r) for r in grid)
    cells = [x for row in grid for x in row if x]
    marks = sum(1 for x in cells if len(x) <= 12 and _MARK.match(x))
    if ncols >= 4 and marks >= 8 and marks / max(len(cells), 1) >= 0.12:
        out.add("soa")
    if 2 <= ncols <= 4 and len(grid) >= 5:
        try:
            from glossary import _table_score
            share, hits, _ = _table_score([list(dict.fromkeys(r)) + [""] * (ncols - len(dict.fromkeys(r)))
                                           for r in grid])
            if share >= 0.5 and hits >= 4:
                out.add("abbreviations")
        except Exception:
            pass
    head = " ".join(" ".join(r) for r in grid[:2]).lower()
    if re.search(r"\b(version|amendment|document|revision)\b", head) and re.search(r"\bdate", head):
        dated = sum(1 for r in grid[1:] if any(_DATE.search(x) for x in r))
        if dated >= 1:
            out.add("amendment_history")
    return out


def tag_roles(index: SectionIndex, doc: DoclingDocument) -> SectionIndex:
    """Set role / role_score on every section (see module docstring). Returns the index."""
    tables = {t.self_ref: t for t in doc.tables}
    own: dict[str, tuple[str | None, float]] = {}
    container: set[str] = set()
    for s in index.sections:
        if s.section_id == "S0":
            continue
        own[s.section_id] = heading_role(s.title)
        lab = parse_label(s.title)
        if _CONTAINER.search(_rest(s.title, lab)) and not (lab is not None and lab.kind == "appendix") \
                and own[s.section_id][0] in (None, "appendix_other"):
            container.add(s.section_id)

    # body start: the first top-level numbered section (else the first introduction/objectives section)
    body_start = next((i for i, s in enumerate(index.sections) if s.level == 1 and s.number), None)
    if body_start is None:
        body_start = next((i for i, s in enumerate(index.sections)
                           if own.get(s.section_id, (None,))[0] in ("introduction", "objectives_endpoints",
                                                                    "synopsis", "design")), len(index.sections))
    toc_pages = [p for s in index.sections if own.get(s.section_id, (None,))[0] == "toc" for p in s.pages]
    first_toc = min(toc_pages) if toc_pages else 3

    in_amend: int | None = None  # level of the front-matter amendment-history run
    for pos, s in enumerate(index.sections):
        if s.section_id == "S0":
            s.role, s.role_score = "title_page", 0.5
            continue
        r_own, sc_own = own[s.section_id]
        parent = index.by_id.get(s.parent_id) if s.parent_id else None
        role, score = (r_own, sc_own) if sc_own >= ROLE_MIN else (None, 0.0)
        inherited = False
        lab = parse_label(s.title)
        appendix_label = lab is not None and (lab.kind == "appendix" or (
            lab.kind in NUMBERED_KINDS and _APPENDIX.match(lab.rest) is not None))  # "10.2 Appendix 2: ..."
        if appendix_label and role is None:
            role, score = "appendix_other", 0.6
        if s.section_id in container:
            role, score = "appendix_other", max(score, 0.6)
        if parent is not None and parent.role:
            p = parent.role
            inherit = (parent.role, round(parent.role_score * 0.9, 3))
            if p in _STRICT_CHILDREN:
                allowed = _STRICT_CHILDREN[p] | {p}
                if p == "amendment_history":  # quoted section titles stay in the history
                    escape = score >= ROLE_STRONG and not s.number and \
                        not (lab is not None and lab.kind in NUMBERED_KINDS)
                else:  # an appendix heading under a mis-levelled section starts a new part
                    escape = appendix_label
                if role not in allowed and not escape:
                    (role, score), inherited = inherit, True
            elif p == "appendix_other" and parent.section_id not in container and not appendix_label:
                (role, score), inherited = inherit, True  # inside one specific appendix
            elif p in _SOFT_PARENTS and (role is None or score < ROLE_STRONG or role in _SOFT_FORBIDDEN):
                (role, score), inherited = inherit, True
            elif role is None:
                (role, score), inherited = inherit, True
        # content cues from the section's own tables
        cues = set()
        for r in s.item_refs:
            if r in tables:
                cues |= table_cues(tables[r])
        if "soa" in cues and (inherited or role in _SOA_OVERRIDES or score < ROLE_STRONG):
            role, score = "soa", 0.7
        elif "toc" in cues and (role is None or score < ROLE_STRONG):
            role, score = "toc", 0.7
        elif "abbreviations" in cues and (inherited or role in (None, "appendix_other") or score < ROLE_STRONG):
            role, score = "abbreviations", 0.7
        elif "amendment_history" in cues and (role in (None, "title_page", "appendix_other") or score < ROLE_STRONG):
            role, score = "amendment_history", 0.7
        # front matter (before the first numbered top-level section)
        if pos < body_start:
            # an amendment summary quotes section titles ("Brief Protocol Synopsis- Patient
            # Population", "3.1 Eligibility ..."): the run ends only at an exact canonical title
            if in_amend is not None and s.level <= in_amend and role != "amendment_history" \
                    and (sc_own >= 0.95 or role == "toc"):
                in_amend = None
            if role == "amendment_history" and in_amend is None:
                in_amend = s.level
            elif in_amend is not None and sc_own < 0.95 and role != "toc" and \
                    (parent is None or parent.role == "amendment_history"):
                role, score = "amendment_history", 0.6
            if role is None and s.pages and min(s.pages) <= max(first_toc, 3):
                role, score = "title_page", 0.5
        s.role, s.role_score = role, float(round(score, 3)) if role else 0.0
    return index


# =========================================================================== heading levels

@dataclass
class TocEntry:
    """One TOC line: label + title, page (as printed), x (indent, points) and the derived level."""
    text: str
    page: int | None = None
    x: float | None = None
    label: Label | None = None
    tokens: tuple = ()
    tuple: tuple | None = None
    level: int | None = None


_TOC_LEADER = re.compile(r"^(?P<body>.*?\S)\s*(?:(?:\.\s?){3,}|…+|_{3,})\s*(?P<page>\d{1,4})\s*$")
_TOC_PLAIN = re.compile(r"^(?P<body>.*[A-Za-z)\]].*?)\s+(?P<page>\d{1,4})\s*$")
_TOC_SKIP = re.compile(r"(?i)^(?:table|figure|listing|graph|chart|exhibit)\s+[A-Z]?\d")
_TRAIL_LEADER = re.compile(r"(?:\s*\.){3,}\s*$|…+\s*$")


def _pdf_page_lines(page) -> list[tuple[str, float | None]]:
    """Text lines of a PDF page in stream order with the x of their first character."""
    import pypdfium2.raw as pdfium_c

    tp = page.get_textpage()
    n = tp.count_chars()
    lines, buf, x0 = [], [], None
    for i in range(n):
        u = pdfium_c.FPDFText_GetUnicode(tp.raw, i)
        if u in (10, 13):
            if u == 10 and buf:
                lines.append(("".join(buf), x0))
                buf, x0 = [], None
            continue
        ch = chr(u) if u else " "
        if x0 is None and not ch.isspace():
            x0 = tp.get_charbox(i)[0]
        buf.append(ch)
    if buf:
        lines.append(("".join(buf), x0))
    return lines


def _pdf_toc_lines(pdf) -> list[tuple[str, float | None, int]]:
    """Lines of the TOC pages: a run of pages near the front with >= 3 dot-leader entry lines."""
    try:
        n = len(pdf)
    except Exception:
        return []
    scan = min(n, max(12, int(n * 0.35)), 60)
    pages = []
    for i in range(scan):
        try:
            text = pdf[i].get_textpage().get_text_range()
        except Exception:
            continue
        leaders = sum(1 for ln in text.splitlines() if _TOC_LEADER.match(ln.strip()))
        if leaders >= 3 or (pages and pages[-1] == i - 1 and leaders >= 1):
            pages.append(i)
        elif pages and i > pages[-1] + 1:
            break
    out = []
    for i in pages:
        try:
            out += [(t, x, i + 1) for t, x in _pdf_page_lines(pdf[i])]
        except Exception:
            continue
    return out


def _doc_toc_lines(doc: DoclingDocument) -> list[tuple[str, float | None, int]]:
    """TOC lines from the layout model's document-index tables (rows) and dot-leader text items."""
    rows = []
    idx_tables = [t for t in doc.tables if t.label == DocItemLabel.DOCUMENT_INDEX]
    pages = {p.page_no for t in idx_tables for p in t.prov}
    for t in idx_tables:
        page = t.prov[0].page_no if t.prov else 0
        ph = doc.pages[page].size.height if page in doc.pages else 792
        by_row = defaultdict(list)
        for c in t.data.table_cells:
            by_row[c.start_row_offset_idx].append(c)
        for r in sorted(by_row):
            cells = sorted(by_row[r], key=lambda c: c.start_col_offset_idx)
            text = " ".join(c.text for c in cells if c.text.strip())
            boxes = [c.bbox for c in cells if c.bbox is not None]
            x = min(b.l for b in boxes) if boxes else None
            y = min(b.to_top_left_origin(ph).t for b in boxes) if boxes else r
            rows.append((page, y, text, x))
    leader_pages = {t.prov[0].page_no for t in doc.texts if t.prov and _TOC_LEADER.match(_clean(t.text))}
    for t in doc.texts:
        if not t.prov or t.prov[0].page_no not in (pages | leader_pages):
            continue
        if t.label not in (DocItemLabel.TEXT, DocItemLabel.LIST_ITEM, DocItemLabel.SECTION_HEADER):
            continue
        if isinstance(t.parent.resolve(doc) if t.parent else None, (TableItem, PictureItem)):
            continue
        p = t.prov[0]
        ph = doc.pages[p.page_no].size.height if p.page_no in doc.pages else 792
        rows.append((p.page_no, p.bbox.to_top_left_origin(ph).t, t.text, p.bbox.l))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(text, x, page) for page, _, text, x in rows]


def _parse_toc_lines(lines: list[tuple[str, float | None, int]]) -> list[TocEntry]:
    entries: list[TocEntry] = []
    pending: list | None = None  # [text, x]

    def add(text: str, page: int | None, x: float | None) -> None:
        text = _clean(_TRAIL_LEADER.sub("", text))
        if not text or _TOC_SKIP.match(text):
            return
        lab = parse_label(text)
        if lab is not None and lab.kind == "list":
            lab = None
        entries.append(TocEntry(text, page, x, lab, tuple(_tokens(_rest(text, lab)))))

    def labelled(t: str) -> bool:
        lab = parse_label(t)
        return lab is not None and lab.kind in NUMBERED_KINDS | {"appendix"}

    for text, x, _ in lines:
        t = _clean(text)
        if not t:
            continue
        if _TOC_SKIP.match(t):  # a list-of-tables/figures line ends any wrapped entry
            if pending is not None:
                add(pending[0], None, pending[1])
            pending = None
            continue
        m = _TOC_LEADER.match(t)
        if m is None and labelled(t) and (pm := _TOC_PLAIN.match(t)):
            lab = parse_label(pm.group("body"))
            m = pm if lab is not None and _clean(lab.rest) else None
        if m is not None:
            body, page = m.group("body"), int(m.group("page"))
            if pending is not None and not labelled(body):
                body, x = pending[0] + " " + body, pending[1]
            elif pending is not None:
                add(pending[0], None, pending[1])
            pending = None
            add(body, page, x)
        elif labelled(t):
            if pending is not None:
                add(pending[0], None, pending[1])
            pending = [t, x]
        elif pending is not None and len(pending[0]) < 300:
            pending[0] += " " + t
    if pending is not None:
        add(pending[0], None, pending[1])
    return entries


def toc_entries(doc: DoclingDocument, pdf=None) -> list[TocEntry]:
    """TOC entries from the PDF text of the TOC pages (preferred: wrapped lines and indents), else
    from the document's TOC tables / dot-leader lines (only before rebuild_toc replaces them)."""
    entries = _parse_toc_lines(_pdf_toc_lines(pdf)) if pdf is not None else []
    if len(entries) < 5:
        doc_entries = _parse_toc_lines(_doc_toc_lines(doc))
        if len(doc_entries) > len(entries):
            entries = doc_entries
    return entries


def _cluster(xs: list[float], tol: float = 4.0) -> list[float]:
    centres: list[float] = []
    for x in sorted(xs):
        if centres and x - centres[-1] <= tol:
            continue
        centres.append(x)
    return centres


def _toc_levels(entries: list[TocEntry], scheme: Scheme) -> None:
    """Level of each TOC entry: from its number (scheme), else from its indent (calibrated on the
    numbered entries when there are any), else appendix = 1."""
    prev: tuple = ()
    for e in entries:
        if e.label is not None and (t := scheme.tuple_for(e.label, prev)):
            e.tuple, e.level, prev = t, len(t), t
    xs = [e.x for e in entries if e.x is not None]
    centres = _cluster(xs)

    def cluster_of(x: float) -> int:
        return min(range(len(centres)), key=lambda k: abs(centres[k] - x))

    calib: dict[int, Counter] = defaultdict(Counter)
    for e in entries:
        if e.level and e.x is not None and centres:
            calib[cluster_of(e.x)][e.level] += 1
    indented = len(centres) >= 2  # a TOC without indents carries no level information
    for e in entries:
        if e.level:
            continue
        appendix = e.label is not None and e.label.kind == "appendix"
        if e.x is not None and centres and (calib or indented):
            k = cluster_of(e.x)
            if calib.get(k):
                e.level = calib[k].most_common(1)[0][0]
            elif not appendix:  # an appendix entry's level is left to the appendix rule
                e.level = k + 1 if indented and not calib else None


def _label_key(lb: Label | None) -> str | None:
    if lb is None or lb.kind == "list":
        return None
    return f"{'appendix' if lb.kind == 'appendix' else 'num'}:{lb.key.upper()}"


def _match_toc(texts: list[str], labels: list[Label | None], pages: list[int | None],
               entries: list[TocEntry]) -> tuple[dict[int, TocEntry], list[float]]:
    """Heading index -> TOC entry: the same label with an agreeing title or page, or (unlabelled
    heading) the same title on an agreeing page. Entries are consumed in order (repeated titles);
    a numbered heading may also take an entry a few places back (heading read out of order).
    Also returns the printed-to-physical page offset near each heading (unnumbered landscape or
    cover pages make it drift through the document)."""
    n = len(texts)
    if not entries:
        return {}, [0.0] * n
    toks = [tuple(_tokens(_rest(t, lb))) for t, lb in zip(texts, labels)]
    by_key = defaultdict(list)
    for e in entries:
        if (k := _label_key(e.label)) and e.page is not None:
            by_key[k].append(e)
    anchors = [(i, pages[i] - e.page) for i, lb in enumerate(labels) if pages[i] is not None
               for e in by_key.get(_label_key(lb) or "", []) if _dice(toks[i], e.tokens) >= 0.6]
    glob = statistics.median(d for _, d in anchors) if anchors else 0.0
    anchors = [(i, d) for i, d in anchors if abs(d - glob) <= 8]  # quoted copies are far off
    offsets = []
    for i in range(n):
        near = sorted(anchors, key=lambda a: abs(a[0] - i))[:5]
        offsets.append(statistics.median(d for _, d in near) if near else glob)
    head_keys = {k for lb in labels if (k := _label_key(lb))}
    out, ptr, used = {}, 0, set()
    for i in range(n):
        best = None
        hk = _label_key(labels[i])
        lo = max(0, ptr - 5) if hk else ptr
        for k in range(lo, min(len(entries), ptr + 60)):
            if k in used:
                continue
            e = entries[k]
            page_ok = None if pages[i] is None or e.page is None else abs(pages[i] - e.page - offsets[i]) <= 2
            if page_ok is False:
                continue
            sim = _dice(toks[i], e.tokens)
            ek = _label_key(e.label)
            if hk and ek:
                if hk != ek or not (sim >= 0.5 or page_ok):
                    continue
                score = 2 + sim
            elif hk is None:
                # an unlabelled heading takes a numbered entry only if no heading carries that
                # number (the layout lost it), and only on an agreeing page
                if sim < 0.85 or (ek is not None and (ek in head_keys or page_ok is not True)):
                    continue
                score = 1 + sim
            else:
                continue
            if best is None or score > best[0]:
                best = (score, k)
        if best is not None:
            out[i] = entries[best[1]]
            used.add(best[1])
            ptr = max(ptr, best[1] + 1)
    return out, offsets


# ---- typography (tie-breaker only)

def _heading_styles(doc: DoclingDocument, pdf, heads: list) -> list[tuple | None]:
    """(font family, bold, italic, size, caps, centred) of each heading from the PDF characters
    inside its box; None when unavailable."""
    out: list[tuple | None] = [None] * len(heads)
    if pdf is None:
        return out
    try:
        import pypdfium2.raw as pdfium_c
    except Exception:
        return out
    by_page = defaultdict(list)
    for i, h in enumerate(heads):
        if h.prov and h.prov[0].page_no in doc.pages:
            by_page[h.prov[0].page_no].append(i)
    lefts = []
    for i, h in enumerate(heads):
        if h.prov and h.prov[0].page_no in doc.pages:
            lefts.append(h.prov[0].bbox.l)
    margin = sorted(lefts)[len(lefts) // 10] if lefts else 0
    buf = ctypes.create_string_buffer(256)
    flags = ctypes.c_int()
    for page_no, idxs in by_page.items():
        try:
            tp = pdf[page_no - 1].get_textpage()
            n = tp.count_chars()
            size = doc.pages[page_no].size
        except Exception:
            continue
        boxes = [(i, heads[i].prov[0].bbox.to_bottom_left_origin(size.height)) for i in idxs]
        acc = defaultdict(list)
        for k in range(n):
            u = pdfium_c.FPDFText_GetUnicode(tp.raw, k)
            if u <= 32:
                continue
            l, b, r, t = tp.get_charbox(k)
            cx, cy = (l + r) / 2, (b + t) / 2
            for i, bb in boxes:
                if bb.l - 1 <= cx <= bb.r + 1 and bb.b - 1 <= cy <= bb.t + 1:
                    acc[i].append(k)
                    break
        for i, ks in acc.items():
            names, weights, heights, italic = Counter(), [], [], 0
            for k in ks[:120]:
                try:
                    pdfium_c.FPDFText_GetFontInfo(tp.raw, k, buf, 256, ctypes.byref(flags))
                    name = buf.value.decode("latin-1", "replace")
                    names[name] += 1
                    italic += bool(flags.value & 64)
                    weights.append(pdfium_c.FPDFText_GetFontWeight(tp.raw, k))
                    lb_, bb_, rb_, tb_ = tp.get_charbox(k, loose=True)
                    heights.append(tb_ - bb_)
                except Exception:
                    continue
            if not names:
                continue
            name = names.most_common(1)[0][0]
            family = re.split(r"[-,]", name.split("+")[-1])[0].lower()
            family = re.sub(r"(mt|ps)$", "", family)
            bold = bool(re.search(r"(?i)bold|black|heavy|semibold|demi", name)) or \
                (statistics.median(weights) if weights else 0) >= 600
            ital = bool(re.search(r"(?i)italic|oblique", name)) or italic > len(ks[:120]) / 2
            sz = round(statistics.median(heights) * 2) / 2 if heights else 0
            letters = [c for c in _rest(heads[i].text) if c.isalpha()]
            caps = len(letters) >= 3 and all(c.isupper() for c in letters)
            bb = heads[i].prov[0].bbox
            w = doc.pages[page_no].size.width
            centred = abs((bb.l + bb.r) / 2 - w / 2) < 0.04 * w and bb.l > margin + 4
            out[i] = (family, bold, ital, sz, caps, centred)
    return out


def _typography_verdict(style: tuple | None, l1: list, deep: list) -> str | None:
    """'L1' / 'deep' when the style is typical of level-1 / deeper headings only, else None."""
    if style is None or not l1:
        return None
    p1 = sum(s == style for s in l1) / len(l1)
    p2 = sum(s == style for s in deep) / len(deep) if deep else 0.0
    if p1 >= 0.5 and p2 < 0.2:
        return "L1"
    if p1 >= 0.2:
        return None
    if p2 >= 0.2:
        return "deep"
    # unseen style: deeper when it lacks an attribute (size, centring, capitals, bold) that the
    # level-1 headings consistently have
    for k, name in ((3, "size"), (5, "centred"), (4, "caps"), (1, "bold")):
        vals = Counter(s[k] for s in l1)
        v, c = vals.most_common(1)[0]
        if c / len(l1) >= 0.8 and style[k] != v and (name != "size" or style[k] < v - 0.5):
            if name == "size" or v is True:
                return "deep"
    return None


def _setting(settings, name: str, default):
    return getattr(settings, name, default) if settings is not None else default


def multi_signal_levels(texts: list[str], pages: list[int | None], entries: list[TocEntry],
                        styles=None, partial: list[int | None] | None = None, settings=None,
                        log: dict | None = None, items: list | None = None) -> list[int]:
    """Heading levels from numbering (scheme-aware) + TOC + optional partial outline levels, with
    level-1 consensus and typography as tie-breaker. Pure on its inputs (testable without a PDF).

    styles: list of style tuples per heading, or a zero-argument callable returning it (computed
    only when a tie-break is needed). partial: outline level per heading (None = not matched).
    """
    log = log if log is not None else {}
    gap = _setting(settings, "successor_max_gap", 3)
    app_re = re.compile(_setting(settings, "appendix_pattern", r"^(?:Appendix|APPENDIX)\s+(?:[A-Z]|\d+)\b"))
    consensus = _setting(settings, "heading_level1_consensus", True)
    use_typo = _setting(settings, "heading_typography", True)
    copy_check = _setting(settings, "heading_copy_check", False)
    n = len(texts)
    partial = partial or [None] * n
    items = items or [None] * n
    raw_labels = [parse_label(t) for t in texts]
    scheme = Scheme.detect(raw_labels)
    labels = scheme.resolve_all(raw_labels)
    _toc_levels(entries, scheme)
    match, offsets = _match_toc(texts, labels, pages, entries)
    toc_by_tuple: dict[tuple, TocEntry] = {}
    for e in entries:
        if e.tuple is not None and e.page is not None:
            toc_by_tuple.setdefault(e.tuple, e)
    absolute = scheme.mode == "absolute"  # contextual tuples ("A." -> prev prefix + 1) do not identify a section
    stats = Counter()
    stats["toc_entries"] = len(entries)
    stats["toc_matched"] = len(match)

    # pass 1: plain numbering (today's rule, scheme-aware) -> reference sets for consensus/typography
    p1: dict[int, int] = {}
    prev: tuple = ()
    for i, lb in enumerate(labels):
        t = scheme.tuple_for(lb, prev)
        if t and _is_successor(prev, t, gap):
            p1[i], prev = len(t), t
    l1_cands = [i for i, lv in p1.items() if lv == 1]
    confirmed = [i for i in l1_cands if i in match and match[i].level == 1]
    # the TOC is a usable second source when most of its own numbered level-1 entries match headings
    toc_l1 = [e for e in entries if e.level == 1 and e.tuple is not None]
    matched_ids = {id(e) for e in match.values()}
    toc_l1_matched = sum(1 for e in toc_l1 if id(e) in matched_ids)
    toc_available = consensus and len(toc_l1) >= 2 and toc_l1_matched >= max(2, 0.5 * len(toc_l1))
    stats["toc_available"] = int(toc_available)
    style_cache: list | None = None

    def verdict(i: int) -> str | None:
        nonlocal style_cache
        if not use_typo or styles is None:
            return None
        if style_cache is None:
            style_cache = styles() if callable(styles) else list(styles)
        l1 = [style_cache[k] for k in (confirmed or l1_cands) if style_cache[k] is not None]
        deep = [style_cache[k] for k, lv in p1.items() if lv >= 2 and style_cache[k] is not None]
        return _typography_verdict(style_cache[i], l1, deep)

    key_count = Counter(_label_key(lb) for lb in labels if lb is not None)

    def far_copy(i: int, t: tuple) -> bool:
        """A quoted copy of a section title (amendment summary): its number is listed in the TOC
        for a distant page and another heading carries the same number."""
        e = toc_by_tuple.get(t)
        return bool(copy_check and toc_available and absolute and e is not None and pages[i] is not None and e is not match.get(i)
                    and key_count[_label_key(labels[i])] >= 2 and abs(pages[i] - e.page - offsets[i]) > 3)

    disagreements = log.setdefault("heading_disagreements", [])
    demoted = log.setdefault("demoted_headings", [])
    has_numbers = len(p1) >= 3  # a numbered template: headings before the first number are front matter
    out: list[int] = []
    prev, depth, top_container, cur_app, in_body = (), 0, False, None, not has_numbers
    for i in range(n):
        lb, e, po = labels[i], match.get(i), partial[i]
        text = _clean(texts[i])
        cand = scheme.tuple_for(lb, prev)
        level, reason = None, None
        if cand and _is_successor(prev, cand, gap):
            ok = True
            if far_copy(i, cand):
                ok, reason = False, "toc_page_mismatch"
            elif len(cand) == 1 and toc_available:
                votes = 1 + (e is not None and e.level == 1) + (po == 1)
                if votes < 2:
                    v = verdict(i)
                    if v == "deep":
                        ok, reason = False, "level1_unconfirmed"
                        stats["level1_rejected"] += 1
                    elif v == "L1":
                        stats["level1_typography"] += 1
                    else:
                        stats["level1_single_source"] += 1
            if ok:
                level, prev, depth, in_body = len(cand), cand, len(cand), True
        elif cand and absolute and e is not None and e.tuple == cand:
            level, prev, depth, in_body = len(cand), cand, len(cand), True  # TOC confirms a non-successor
            reason = "toc_rescued"
            stats["toc_rescued"] += 1
        if level is None and lb is not None and lb.kind == "appendix" or (level is None and app_re.match(text)):
            key = lb.key if lb is not None and lb.kind == "appendix" else _norm(text)[:20]
            if key == cur_app:
                level = min(depth + 1, 6)  # the appendix's cover page and first page both carry the title
            else:
                level = e.level if e is not None and e.level else (2 if top_container else 1)
                depth, cur_app = level, key
        elif level is None and e is not None and e.level and in_body and \
                (lb is None or lb.kind not in NUMBERED_KINDS or cand is None):
            lv = e.level
            if lv == 1 and verdict(i) == "deep":
                lv = None
            if lv:
                level, depth = lv, lv
                if e.tuple is not None and _is_successor(prev, e.tuple, gap):
                    prev = e.tuple
                stats["toc_level"] += 1
        if level is None:
            level = min(depth + 1, 6) if depth else 1
            if cand or (lb is not None and lb.kind in NUMBERED_KINDS):
                demoted.append({"heading": items[i], "page": pages[i], "level": level,
                                "reason": reason or "number_not_successor"})
        if level == 1:
            top_container = bool(_CONTAINER.search(_rest(text, lb))) and not (lb is not None and lb.kind == "appendix")
            if not (lb is not None and lb.kind == "appendix") and not app_re.match(text):
                cur_app = None
        toc_lv = e.level if e is not None else None
        if (toc_lv is not None and toc_lv != level) or (po is not None and po != level) or \
                reason in ("level1_unconfirmed", "toc_page_mismatch", "toc_rescued"):
            disagreements.append({"heading": items[i], "page": pages[i], "level": level,
                                  "numbering": len(cand) if cand else None, "toc": toc_lv, "outline": po,
                                  "reason": reason or "sources_differ"})
        out.append(level)
    stats["disagreements"] = len(disagreements)
    log["heading_sources"] = {**{k: int(v) for k, v in stats.items()}, "scheme": scheme.mode}
    return out


def assign_heading_levels(doc: DoclingDocument, pdf=None, settings=None, log: dict | None = None) -> int:
    """Set section-header levels (drop-in for protocol_postprocess.fix_heading_levels).

    1. The PDF outline when >= outline_min_match of its bookmarks match headings (heading_source
       "auto"/"outline"): delegated to fix_heading_levels, so the result is identical to today's;
       the TOC is only checked against it (log["heading_sources"]: counts, no text).
    2. Otherwise (no outline, too few matches, or heading_source "numbering"): multi_signal_levels
       over the heading texts, the TOC entries (PDF text of the TOC pages, else the document's TOC
       tables, which exist only before rebuild_toc), the partially matched outline when there is
       one, and typography from the PDF characters. Disagreements are logged in
       log["heading_disagreements"] (refs, pages, levels, reason).
    Optional settings (read with getattr, so ProtocolSettings needs no change): heading_toc,
    heading_typography, heading_level1_consensus (default on), heading_copy_check (default off:
    reject a number whose TOC entry is on a distant page while another heading carries it).
    Returns the number of headings whose level changed. Call it where fix_heading_levels is
    called today (postprocess(), before rebuild_toc, so the layout model's TOC is still there).
    """
    import protocol_postprocess as pp
    from protocol_settings import ProtocolSettings

    settings = settings or ProtocolSettings()
    log = log if log is not None else {}
    heads = [h for h, _ in doc.iterate_items() if isinstance(h, SectionHeaderItem)]
    outline = None if settings.heading_source == "numbering" else pp._outline_levels(heads, pdf, settings, log)
    use_toc = _setting(settings, "heading_toc", True)
    if outline is not None:
        changed = pp.fix_heading_levels(doc, pdf, settings, log)
        if use_toc:
            try:
                entries = toc_entries(doc, pdf)
                texts = [h.text for h in heads]
                labels = Scheme.detect([parse_label(t) for t in texts]).resolve_all([parse_label(t) for t in texts])
                _toc_levels(entries, Scheme.detect(labels))
                match, _ = _match_toc(texts, labels, [h.prov[0].page_no if h.prov else None for h in heads], entries)
                log["heading_sources"] = {"toc_entries": len(entries), "toc_matched": len(match),
                                          "toc_level_disagree": sum(1 for i, e in match.items()
                                                                    if e.level and e.level != heads[i].level)}
            except Exception:  # a check only: never fail the conversion
                log["heading_sources"] = {"toc_check_failed": 1}
        return changed
    log["heading_source"] = "numbering"
    entries = []
    if use_toc:
        try:
            entries = toc_entries(doc, pdf)
        except Exception:
            entries = []
    partial = None
    if pdf is not None and settings.heading_source == "auto":
        try:
            lv = pp._outline_levels(heads, pdf, replace(settings, heading_source="outline"), {})
            partial = [lv.get(id(h)) for h in heads] if lv else None
        except Exception:
            partial = None
    levels = multi_signal_levels([h.text for h in heads], [h.prov[0].page_no if h.prov else None for h in heads],
                                 entries, styles=lambda: _heading_styles(doc, pdf, heads), partial=partial,
                                 settings=settings, log=log, items=heads)
    changed = 0
    for h, lv in zip(heads, levels):
        if h.level != lv:
            h.level = lv
            changed += 1
    return changed


# =========================================================================== CLI (local inspection)

def _print_index(index: SectionIndex) -> None:
    for s in index.sections:
        role = f"{s.role}({s.role_score:.2f})" if s.role else "-"
        pages = f"p{s.pages[0]}-{s.pages[-1]}" if s.pages else ""
        print(f"{'  ' * s.level}{s.section_id:16s} {role:28s} {pages:9s} items {len(s.item_refs):3d} "
              f"minor {len(s.minor_headings):2d} | {s.title[:80]}")


def main(argv: list[str] | None = None) -> int:
    """python structure.py index DOC.json            print the section tree (ids, roles, sizes)
       python structure.py levels DOC.json PDF [--numbering]
                                                     re-run the heading levels and print them
    Prints document text: for public or local inspection only, never for confidential test sets."""
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("index")
    p.add_argument("json")
    p.add_argument("--out")
    p = sub.add_parser("levels")
    p.add_argument("json")
    p.add_argument("pdf")
    p.add_argument("--numbering", action="store_true", help="ignore the PDF outline")
    args = ap.parse_args(argv)
    doc = DoclingDocument.load_from_json(args.json)
    if args.cmd == "index":
        index = build_section_index(doc)
        _print_index(index)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                json.dump(index.to_json(), fh, indent=1)
        return 0
    import pypdfium2 as pdfium

    from protocol_settings import ProtocolSettings

    settings = ProtocolSettings(heading_source="numbering" if args.numbering else "auto")
    log: dict = {}
    assign_heading_levels(doc, pdfium.PdfDocument(args.pdf), settings, log)
    for h, _ in doc.iterate_items():
        if isinstance(h, SectionHeaderItem):
            print(f"{h.prov[0].page_no if h.prov else '?':>4} L{h.level} {'  ' * h.level}{h.text[:90]}")
    print({k: v for k, v in log.items() if not isinstance(v, list)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
