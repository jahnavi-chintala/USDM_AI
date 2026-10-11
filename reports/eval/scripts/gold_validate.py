"""Validate the MAIN gold set and compute the S0 extraction-recall baseline.

Checks
  1. JSON schema of reports/eval/gold/MAIN.json (shared gold contract).
  2. Every quote occurs (whitespace-normalised) on its stated page of the PDF text layer
     (pypdfium2 get_text_range). Soft hyphens emitted by pdfium at line breaks (U+FFFE /
     U+00AD) are mapped to '-' before matching; nothing else is altered.
  3. S0 baseline: is each quote also present in the converted output?
       md     - output/<stem>.md (whole document)
       chunk  - inside at least one single chunk of output/chunks/chunks.jsonl
                (chunk 'text'; tables.jsonl markdown counted as extra table chunks)
     Normalisation (spec): NFKC, unify dashes/quotes, collapse whitespace, remove spaces.
     A 'lenient' variant additionally strips markdown/table syntax (| * _ # `) and hyphens.
     A question is S0-supported if at least one alternative evidence set has all quotes found.
  4. Counts by category / kind / split / answerable / USDM class.
  5. Key terms (scoring fields; matcher = gold_match.py):
       a. schema of fact["key_terms"] / forbidden["key_terms"] (AND-groups of OR-alternatives);
       b. evidence check: each fact's key terms must be satisfied by the concatenated quotes of at
          least one evidence set, unless the fact carries a "key_terms_note" (table layout,
          inference ...). Facts that fail on the quotes are also tried against the full text of the
          pages their quotes come from (diagnostic only);
       c. every forbidden entry's key terms must match its own text (a precise wrong claim) and
          must not match the question's facts (text + alts) or its evidence quotes.

Usage: python reports/eval/scripts/gold_validate.py [--gold PATH] [--verbose] [--json OUT]
Exit code 1 if any schema error, any quote failing the PDF check, or any key-term error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gold_match import key_terms_match, missing_groups, normalize as kt_normalize  # noqa: E402
from project_paths import main_pdf  # noqa: E402  (the project root is on sys.path via gold_match)

ROOT = Path(__file__).resolve().parents[3]
PDF = main_pdf()
STEM = PDF.stem
MD = ROOT / "output" / f"{STEM}.md"
CHUNKS = ROOT / "output" / "chunks" / "chunks.jsonl"
TABLES = ROOT / "output" / "chunks" / "tables.jsonl"
GOLD = ROOT / "reports" / "eval" / "gold" / "MAIN.json"

USDM_V4_CLASSES = {
    "Abbreviation", "Activity", "Address", "AdministrableProduct", "AdministrableProductIdentifier",
    "AdministrableProductProperty", "Administration", "AliasCode", "AnalysisPopulation", "AssignedPerson",
    "BiomedicalConcept", "BiomedicalConceptCategory", "BiomedicalConceptProperty", "BiomedicalConceptSurrogate",
    "BiospecimenRetention", "Characteristic", "Code", "CommentAnnotation", "Condition", "ConditionAssignment",
    "DocumentContentReference", "Duration", "EligibilityCriterion", "EligibilityCriterionItem", "Encounter",
    "Endpoint", "Estimand", "GeographicScope", "GovernanceDate", "Identifier", "Indication", "Ingredient",
    "IntercurrentEvent", "InterventionalStudyDesign", "Masking", "MedicalDevice", "MedicalDeviceIdentifier",
    "NarrativeContent", "NarrativeContentItem", "Objective", "ObservationalStudyDesign", "Organization",
    "ParameterMap", "PersonName", "PopulationDefinition", "Procedure", "ProductOrganizationRole", "Quantity",
    "QuantityRange", "Range", "ReferenceIdentifier", "ResponseCode", "ScheduleTimeline", "ScheduleTimelineExit",
    "ScheduledActivityInstance", "ScheduledDecisionInstance", "ScheduledInstance", "Strength", "Study",
    "StudyAmendment", "StudyAmendmentImpact", "StudyAmendmentReason", "StudyArm", "StudyCell", "StudyChange",
    "StudyCohort", "StudyDefinitionDocument", "StudyDefinitionDocumentVersion", "StudyDesign",
    "StudyDesignPopulation", "StudyElement", "StudyEpoch", "StudyIdentifier", "StudyIntervention", "StudyRole",
    "StudySite", "StudyTitle", "StudyVersion", "SubjectEnrollment", "Substance", "SyntaxTemplate",
    "SyntaxTemplateDictionary", "Timing", "TransitionRule",
}
KINDS = {"lookup", "enumeration", "multi_evidence", "cross_reference", "unanswerable"}
MODALITIES = {"text", "list", "table", "figure"}
COMPARATORS = {None, "≥", "≤", "<", ">", "=", "±"}
ID_RE = re.compile(r"^MAIN-[A-Z]+-\d{3}$")
MAX_QUOTE = 220  # contract says "<= ~200"; hard limit with small tolerance

DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹘﹣－"), "-")
QUOTES = {ord(c): "'" for c in "‘’‚‛′"} | {ord(c): '"' for c in "“”„‟″"}


# ----------------------------------------------------------------------------- normalisers
def norm_pdf(s: str) -> str:
    """PDF check: whitespace collapse only (+ pdfium soft hyphen -> '-')."""
    s = s.replace("￾", "-").replace("­", "-").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip()


def norm_s0(s: str) -> str:
    """Spec normalisation: NFKC, unify dashes/quotes, collapse whitespace, remove spaces."""
    s = s.replace("￾", "-").replace("­", "-")
    s = unicodedata.normalize("NFKC", s).translate(DASHES).translate(QUOTES)
    s = re.sub(r"\s+", " ", s)
    return s.replace(" ", "")


def norm_lenient(s: str) -> str:
    s = norm_s0(s)
    return re.sub(r"[|*_#`\-]", "", s)


# ----------------------------------------------------------------------------- loaders
def load_pdf_pages() -> dict[int, str]:
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(PDF))
    return {i + 1: pdf[i].get_textpage().get_text_range() for i in range(len(pdf))}


def load_output():
    md = MD.read_text(encoding="utf-8") if MD.exists() else ""
    chunks = []
    if CHUNKS.exists():
        for line in CHUNKS.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                chunks.append(r.get("text") or "")
    if TABLES.exists():
        for line in TABLES.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                chunks.append(r.get("markdown") or "")
    return md, chunks


# ----------------------------------------------------------------------------- key terms
FACT_KEYS = {"id", "text", "alts", "critical", "value", "unit", "comparator", "key_terms"}
FACT_OPTIONAL = {"key_terms_note"}
FORBIDDEN_KEYS = {"text", "why", "key_terms"}


def key_terms_errors(kt) -> list[str]:
    """Shape check: non-empty list of non-empty lists of non-empty, distinct strings."""
    if not isinstance(kt, list) or not kt:
        return ["key_terms must be a non-empty list of groups"]
    errs = []
    for gi, group in enumerate(kt):
        if not isinstance(group, list) or not group:
            errs.append(f"key_terms[{gi}] must be a non-empty list")
            continue
        if not all(isinstance(a, str) and a.strip() for a in group):
            errs.append(f"key_terms[{gi}] has an empty or non-string alternative")
            continue
        if len(set(group)) != len(group):
            errs.append(f"key_terms[{gi}] has duplicate alternatives")
        for a in group:
            if "|" in a:
                errs.append(f"key_terms[{gi}] alternative contains '|': {a!r}")
            if not kt_normalize(a):
                errs.append(f"key_terms[{gi}] alternative normalises to empty: {a!r}")
    return errs


def check_key_terms(qs, npages: dict[int, str]) -> tuple[list[str], dict]:
    """Evidence check of fact key terms and consistency checks of forbidden key terms."""
    errs: list[str] = []
    rows = []
    norm_pages = {p: kt_normalize(t) for p, t in npages.items()}
    for q in qs:
        a = q["answer"]
        facts = a.get("facts", [])
        fact_text = " || ".join(s for f in facts for s in [f["text"], *f["alts"]])
        quote_text = " || ".join(i["quote"] for s in q["evidence"] for i in s["all_of"])
        for fb in a.get("forbidden", []):
            kt = fb.get("key_terms")
            if key_terms_errors(kt):
                continue  # reported by the schema check
            if not key_terms_match(kt, fb["text"]):
                errs.append(f"{q['id']}: forbidden key terms do not match their own text {fb['text']!r}")
            if facts and key_terms_match(kt, fact_text):
                errs.append(f"{q['id']}: forbidden key terms match the gold facts ({fb['text']!r})")
            if quote_text and key_terms_match(kt, quote_text):
                errs.append(f"{q['id']}: forbidden key terms match the evidence quotes ({fb['text']!r})")
        if not q["answerable"]:
            continue
        sets = [" || ".join(i["quote"] for i in s["all_of"]) for s in q["evidence"]]
        pages = [" || ".join(norm_pages.get(i["page"], "") for i in s["all_of"]) for s in q["evidence"]]
        for f in facts:
            kt = f.get("key_terms")
            if key_terms_errors(kt):
                continue
            quote_ok = any(key_terms_match(kt, s) for s in sets)
            page_ok = quote_ok or any(key_terms_match(kt, p, normalized=True) for p in pages)
            self_ok = key_terms_match(kt, " || ".join([f["text"], *f["alts"]]))
            note = f.get("key_terms_note")
            missing = [] if quote_ok else min((missing_groups(kt, s) for s in sets), key=len)
            rows.append({"id": q["id"], "fact": f["id"], "critical": f["critical"], "quote_ok": quote_ok,
                         "page_ok": page_ok, "self_ok": self_ok, "note": note,
                         "missing": [kt[i] for i in missing]})
            if not quote_ok and not note:
                errs.append(f"{q['id']} {f['id']}: key terms not satisfied by any evidence set "
                            f"(missing groups {[kt[i][:3] for i in missing]}) and no key_terms_note")
    return errs, {"facts": rows}


# ----------------------------------------------------------------------------- schema
def check_schema(qs) -> list[str]:
    errs: list[str] = []
    if not isinstance(qs, list):
        return ["top level is not a list"]
    seen = set()
    for q in qs:
        qid = q.get("id", "?")
        def e(msg):
            errs.append(f"{qid}: {msg}")
        req = ["id", "doc", "split", "usdm", "category", "kind", "modality", "question", "answerable", "evidence", "answer"]
        for k in req:
            if k not in q:
                e(f"missing key {k}")
        if errs and errs[-1].startswith(f"{qid}: missing"):
            continue
        if not ID_RE.match(qid):
            e("bad id format")
        if qid in seen:
            e("duplicate id")
        seen.add(qid)
        if q["doc"] != "MAIN":
            e("doc != MAIN")
        if q["split"] not in ("dev", "test"):
            e("bad split")
        u = q["usdm"]
        if not isinstance(u, dict) or u.get("class") not in USDM_V4_CLASSES:
            e(f"usdm.class not a USDM v4 class: {u}")
        if not isinstance(u.get("attributes"), list) or not u["attributes"]:
            e("usdm.attributes empty")
        if not re.match(r"^[a-z_]+(\.[a-z_]+)*$", q["category"]):
            e("category format")
        if q["kind"] not in KINDS:
            e("bad kind")
        if not q["modality"] or not set(q["modality"]) <= MODALITIES:
            e("bad modality")
        if not isinstance(q["question"], str) or len(q["question"]) < 10:
            e("question too short")
        a = q["answer"]
        for k in ("facts", "complete_list", "forbidden"):
            if k not in a:
                e(f"answer missing {k}")
        if not isinstance(a.get("complete_list"), bool):
            e("complete_list not bool")
        for f in a.get("forbidden", []):
            if set(f) != FORBIDDEN_KEYS or not f["text"] or not f["why"]:
                e(f"bad forbidden entry {f}")
                continue
            for msg in key_terms_errors(f["key_terms"]):
                e(f"forbidden {f['text'][:40]!r}: {msg}")
        if q["answerable"] is False:
            if q["kind"] != "unanswerable":
                e("answerable=false but kind != unanswerable")
            if q["evidence"] != []:
                e("unanswerable with evidence")
            if a.get("facts"):
                e("unanswerable with facts")
            continue
        if q["kind"] == "unanswerable":
            e("kind unanswerable but answerable=true")
        if not q["evidence"]:
            e("answerable without evidence")
        for s in q["evidence"]:
            if set(s) != {"all_of"} or not s["all_of"]:
                e("evidence set must be {'all_of': [...]} and non-empty")
                continue
            for item in s["all_of"]:
                if set(item) != {"quote", "page", "section"}:
                    e(f"bad evidence keys {sorted(item)}")
                if not isinstance(item.get("page"), int):
                    e("page not int")
                sec = item.get("section")
                if sec is not None and not re.match(r"^\d+(\.\d+)*$", str(sec)):
                    e(f"bad section {sec}")
                qt = item.get("quote", "")
                if not qt or len(qt) > MAX_QUOTE:
                    e(f"quote empty or too long ({len(qt)})")
        facts = a.get("facts", [])
        if not facts:
            e("answerable without facts")
        fids = set()
        for f in facts:
            if not FACT_KEYS <= set(f) <= FACT_KEYS | FACT_OPTIONAL:
                e(f"fact keys {sorted(f)}")
                continue
            for msg in key_terms_errors(f["key_terms"]):
                e(f"fact {f['id']}: {msg}")
            if "key_terms_note" in f and not (isinstance(f["key_terms_note"], str) and f["key_terms_note"].strip()):
                e(f"fact {f['id']}: key_terms_note must be a non-empty string")
            if f["id"] in fids:
                e("duplicate fact id")
            fids.add(f["id"])
            if not isinstance(f["critical"], bool) or not isinstance(f["alts"], list):
                e("fact critical/alts type")
            if f["value"] is not None:
                try:
                    float(f["value"])
                except (TypeError, ValueError):
                    e(f"fact value not numeric: {f['value']}")
            if f["comparator"] not in COMPARATORS:
                e(f"bad comparator {f['comparator']}")
        if q["kind"] == "enumeration" and not a["complete_list"] and len(facts) < 2:
            e("enumeration with <2 facts")
    return errs


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(GOLD))
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", help="optional path for a machine-readable report (e.g. scratch dir)")
    args = ap.parse_args()

    qs = json.loads(Path(args.gold).read_text(encoding="utf-8"))
    errs = check_schema(qs)
    print(f"questions: {len(qs)}   schema errors: {len(errs)}")
    for x in errs:
        print("  SCHEMA", x)

    pages = load_pdf_pages()
    npages = {p: norm_pdf(t) for p, t in pages.items()}
    md, chunks = load_output()
    md_s0, md_len = norm_s0(md), norm_lenient(md)
    ch_s0 = [norm_s0(c) for c in chunks]
    ch_len = [norm_lenient(c) for c in chunks]

    pdf_fail = []
    other_pages = []
    per_q = []
    nquotes = Counter()
    for q in qs:
        if not q.get("answerable"):
            continue
        set_res = []
        for si, s in enumerate(q["evidence"]):
            res = []
            for item in s["all_of"]:
                quote, page = item["quote"], item["page"]
                nq = norm_pdf(quote)
                ok = page in npages and nq in npages[page]
                if not ok:
                    found_on = [p for p, t in npages.items() if nq in t]
                    pdf_fail.append((q["id"], page, quote, found_on))
                elif args.verbose:
                    found_on = [p for p, t in npages.items() if nq in t and p != page]
                    if found_on:
                        other_pages.append((q["id"], page, quote[:60], found_on[:5]))
                s0q, lq = norm_s0(quote), norm_lenient(quote)
                r = {
                    "quote": quote, "page": page, "pdf": ok,
                    "md": s0q in md_s0, "chunk": any(s0q in c for c in ch_s0),
                    "md_lenient": lq in md_len, "chunk_lenient": any(lq in c for c in ch_len),
                }
                nquotes["total"] += 1
                for k in ("md", "chunk", "md_lenient", "chunk_lenient"):
                    nquotes[k] += r[k]
                res.append(r)
            set_res.append(res)
        sup = {k: any(all(r[k] for r in res) for res in set_res) for k in ("md", "chunk", "md_lenient", "chunk_lenient")}
        per_q.append({"id": q["id"], "category": q["category"], "kind": q["kind"], "split": q["split"],
                      "sets": set_res, **{f"s0_{k}": v for k, v in sup.items()}})

    print(f"\nPDF text-layer check: {nquotes['total']} quotes, {len(pdf_fail)} failures")
    for qid, page, quote, found_on in pdf_fail:
        print(f"  FAIL {qid} p{page}: {quote[:110]!r}  found_on={found_on[:6]}")
    if args.verbose and other_pages:
        print(f"\n(info) {len(other_pages)} quotes also occur on other pages")

    # ---------------- key terms
    kt_errs, kt_rep = check_key_terms(qs, npages)
    rows = kt_rep["facts"]
    nq = sum(r["quote_ok"] for r in rows)
    noted = [r for r in rows if r["note"]]
    stale = [r for r in noted if r["quote_ok"]]
    page_only = [r for r in rows if not r["quote_ok"] and r["page_ok"]]
    not_self = [r for r in rows if not r["self_ok"]]
    nforb = sum(len(q["answer"]["forbidden"]) for q in qs)
    print(f"\nKey terms: {sum(len(q['answer']['facts']) for q in qs)} facts, {nforb} forbidden entries "
          f"in {sum(bool(q['answer']['forbidden']) for q in qs)} questions")
    print(f"  evidence check (answerable facts): {nq}/{len(rows)} ({nq / max(1, len(rows)):.1%}) satisfied by the quotes "
          f"of at least one evidence set; critical {sum(r['quote_ok'] for r in rows if r['critical'])}/"
          f"{sum(r['critical'] for r in rows)}")
    print(f"  not satisfied by the quotes: {len(rows) - nq} ({len(noted) - len(stale)} with key_terms_note, "
          f"{len(rows) - nq - (len(noted) - len(stale))} without); {len(page_only)} of them are satisfied by the full "
          f"text of the quoted pages")
    if stale:
        print(f"  (info) {len(stale)} facts carry a key_terms_note but pass on the quotes: "
              + ", ".join(f"{r['id']}/{r['fact']}" for r in stale))
    print(f"  (info) facts whose own text+alts do not satisfy their key terms: {len(not_self)}")
    if args.verbose:
        for r in rows:
            if not r["quote_ok"]:
                print(f"    {r['id']:16s} {r['fact']:4s} page={'Y' if r['page_ok'] else '-'} note={r['note']!r}")
        for r in not_self:
            print(f"    not-self {r['id']} {r['fact']}")
    print(f"  key-term errors: {len(kt_errs)}")
    for x in kt_errs:
        print("  KT", x)

    # ---------------- counts
    def table(title, key):
        c = Counter(key(q) for q in qs)
        print(f"\n{title}")
        for k, v in sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0]))):
            print(f"  {str(k):45s} {v:4d}")

    table("By kind", lambda q: q["kind"])
    table("By split", lambda q: q["split"])
    table("By answerable", lambda q: q["answerable"])
    table("By top-level category", lambda q: q["category"].split(".")[0])
    table("By category", lambda q: q["category"])
    table("By USDM class", lambda q: q["usdm"]["class"])
    table("By id area", lambda q: q["id"].split("-")[1])
    cs = Counter((q["category"].split(".")[0], q["split"]) for q in qs)
    print("\nSplit by top-level category (dev/test)")
    for cat in sorted({c for c, _ in cs}):
        print(f"  {cat:25s} dev={cs[(cat, 'dev')]:3d} test={cs[(cat, 'test')]:3d}")

    # ---------------- S0 baseline
    na = len(per_q)
    print("\nS0 extraction-recall baseline (answerable questions)")
    print(f"  quotes found  md={nquotes['md']}/{nquotes['total']} ({nquotes['md'] / max(1, nquotes['total']):.1%})"
          f"  chunk={nquotes['chunk']}/{nquotes['total']} ({nquotes['chunk'] / max(1, nquotes['total']):.1%})")
    print(f"  quotes found (lenient)  md={nquotes['md_lenient']} ({nquotes['md_lenient'] / max(1, nquotes['total']):.1%})"
          f"  chunk={nquotes['chunk_lenient']} ({nquotes['chunk_lenient'] / max(1, nquotes['total']):.1%})")
    for k in ("md", "chunk", "md_lenient", "chunk_lenient"):
        n = sum(r[f"s0_{k}"] for r in per_q)
        print(f"  questions fully supported [{k:13s}] {n}/{na} ({n / max(1, na):.1%})")
    by_cat = defaultdict(lambda: Counter())
    for r in per_q:
        c = r["category"].split(".")[0]
        by_cat[c]["n"] += 1
        by_cat[c]["md"] += r["s0_md"]
        by_cat[c]["chunk"] += r["s0_chunk"]
        by_cat[c]["chunk_lenient"] += r["s0_chunk_lenient"]
    print("\n  S0 by top-level category        n   md  chunk chunk_len")
    for c, v in sorted(by_cat.items()):
        print(f"  {c:28s} {v['n']:4d} {v['md']:4d} {v['chunk']:5d} {v['chunk_lenient']:6d}")
    by_kind = defaultdict(lambda: Counter())
    for r in per_q:
        by_kind[r["kind"]]["n"] += 1
        by_kind[r["kind"]]["md"] += r["s0_md"]
        by_kind[r["kind"]]["chunk"] += r["s0_chunk"]
        by_kind[r["kind"]]["chunk_lenient"] += r["s0_chunk_lenient"]
    print("\n  S0 by kind                      n   md  chunk chunk_len")
    for c, v in sorted(by_kind.items()):
        print(f"  {c:28s} {v['n']:4d} {v['md']:4d} {v['chunk']:5d} {v['chunk_lenient']:6d}")

    print("\nPer-question S0 (md / chunk / chunk_lenient); missing quotes listed")
    for r in per_q:
        flag = lambda b: "Y" if b else "-"
        print(f"  {r['id']:16s} {flag(r['s0_md'])} {flag(r['s0_chunk'])} {flag(r['s0_chunk_lenient'])}  {r['category']}")
        if not r["s0_chunk_lenient"] or args.verbose:
            best = min(r["sets"], key=lambda res: sum(not x["chunk_lenient"] for x in res))
            for x in best:
                if not x["chunk_lenient"]:
                    print(f"      missing(chunk,lenient) p{x['page']}: {x['quote'][:90]!r}"
                          f"{'  [in md]' if x['md_lenient'] else ''}")

    if args.json:
        Path(args.json).write_text(json.dumps({"schema_errors": errs, "pdf_failures": pdf_fail, "per_question": per_q,
                                               "key_term_errors": kt_errs, "key_terms": kt_rep},
                                              ensure_ascii=False, indent=1), encoding="utf-8")
    return 1 if errs or pdf_fail or kt_errs else 0


if __name__ == "__main__":
    sys.exit(main())
