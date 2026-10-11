"""Self-test for usdm_gold.py on tiny synthetic fixtures (made-up study, no network, no PDF needed):
`python tests/test_usdm_gold.py`. A smoke test on the public keys runs when public_protocols/ is present."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval_v1 as ev  # noqa: E402
import usdm_gold as ug  # noqa: E402
from keyterm_match import key_terms_match  # noqa: E402


def code(cid: str, decode: str) -> dict:
    return {"id": cid, "code": "C0", "codeSystem": "x", "decode": decode, "instanceType": "Code"}


def alias(cid: str, decode: str) -> dict:
    return {"id": "A" + cid, "standardCode": code(cid, decode), "standardCodeAliases": [], "instanceType": "AliasCode"}


def synthetic_key() -> dict:
    enc = []
    labels = ["Screening", "Week 2", "Week 4"]
    for i, lab in enumerate(labels, 1):
        enc.append({"id": f"Encounter_{i}", "name": f"E{i}", "label": lab, "description": f"{lab} encounter",
                    "type": code(f"ce{i}", "Visit"), "previousId": f"Encounter_{i - 1}" if i > 1 else None,
                    "nextId": f"Encounter_{i + 1}" if i < len(labels) else None,
                    "scheduledAtId": f"Timing_{i}", "instanceType": "Encounter"})
    acts = [{"id": f"Activity_{i}", "name": n, "label": n, "description": "", "instanceType": "Activity"}
            for i, n in enumerate(["Informed consent", "Vital signs", "Hematology panel", "ECG"], 1)]
    sai = [{"id": "SAI_1", "name": "S1", "encounterId": "Encounter_1", "epochId": "StudyEpoch_1",
            "activityIds": ["Activity_1", "Activity_2", "Activity_3"], "instanceType": "ScheduledActivityInstance"},
           {"id": "SAI_2", "name": "S2", "encounterId": "Encounter_2", "epochId": "StudyEpoch_2",
            "activityIds": ["Activity_2", "Activity_4"], "instanceType": "ScheduledActivityInstance"},
           {"id": "SAI_3", "name": "S3", "encounterId": "Encounter_3", "epochId": "StudyEpoch_2",
            "activityIds": ["Activity_2", "Activity_3", "Activity_4"], "instanceType": "ScheduledActivityInstance"}]
    timings = [{"id": f"Timing_{i}", "name": f"T{i}", "label": labels[i - 1], "type": code(f"ct{i}", "After"),
                "value": f"P{2 * (i - 1)}W", "valueLabel": "", "relativeToFrom": code(f"cr{i}", "Start to Start"),
                "relativeFromScheduledInstanceId": f"SAI_{i}", "relativeToScheduledInstanceId": "SAI_1",
                "windowLower": "P3D" if i > 1 else None, "windowUpper": "P3D" if i > 1 else None,
                "windowLabel": "-3..3 days" if i > 1 else "", "instanceType": "Timing"} for i in (1, 2, 3)]
    design = {
        "id": "InterventionalStudyDesign_1", "name": "D", "label": "", "description": "",
        "instanceType": "InterventionalStudyDesign", "studyType": code("c1", "Interventional Study"),
        "studyPhase": alias("c2", "Phase II Trial"), "model": code("c3", "Parallel Study"),
        "blindingSchema": alias("c4", "Double Blind Study"),
        "arms": [{"id": "StudyArm_1", "name": "Placebo", "label": "Placebo", "type": code("c5", "Placebo"),
                  "instanceType": "StudyArm"},
                 {"id": "StudyArm_2", "name": "Zorbex", "label": "Zorbex", "type": code("c6", "Active"),
                  "instanceType": "StudyArm"}],
        "epochs": [{"id": "StudyEpoch_1", "name": "Screening", "label": "Screening", "type": code("c7", "Screening"),
                    "nextId": "StudyEpoch_2", "instanceType": "StudyEpoch"},
                   {"id": "StudyEpoch_2", "name": "Treatment", "label": "Treatment", "type": code("c8", "Treatment"),
                    "previousId": "StudyEpoch_1", "instanceType": "StudyEpoch"}],
        "studyCells": [{"id": "StudyCell_1", "armId": "StudyArm_1", "epochId": "StudyEpoch_9", "elementIds": [],
                        "instanceType": "StudyCell"}],  # dangling epoch reference (a quirk)
        "encounters": enc, "activities": acts,
        "scheduleTimelines": [{"id": "ScheduleTimeline_1", "name": "Main", "label": "Main", "mainTimeline": True,
                               "entryId": "SAI_1", "instances": sai, "timings": timings,
                               "instanceType": "ScheduleTimeline"}],
        "objectives": [{"id": "Objective_1", "name": "OBJ1", "text": "To compare the zorbex response rate with placebo",
                        "level": code("c9", "Primary Objective"), "instanceType": "Objective",
                        "endpoints": [{"id": "Endpoint_1", "name": "END1", "text": "Overall response rate at week 4",
                                       "level": code("c10", "Primary Endpoint"), "instanceType": "Endpoint"}]}],
        "eligibilityCriteria": [{"id": "EligibilityCriterion_1", "name": "IN01", "identifier": "01",
                                 "category": code("c11", "Inclusion Criteria"),
                                 "criterionItemId": "EligibilityCriterionItem_1",
                                 "instanceType": "EligibilityCriterion"}],
        "population": {"id": "Pop_1", "name": "POP", "plannedEnrollmentNumber": {"id": "Q1", "value": 120.0,
                                                                                "instanceType": "Quantity"},
                       "plannedSex": [code("c12", "Both")], "instanceType": "StudyDesignPopulation"},
    }
    version = {
        "id": "StudyVersion_1", "instanceType": "StudyVersion",
        "titles": [{"id": "StudyTitle_1", "text": "ZORB-1", "type": code("c13", "Study Acronym"),
                    "instanceType": "StudyTitle"}],
        "organizations": [{"id": "Organization_1", "name": "ACME", "label": "Acme Pharma",
                           "type": code("c14", "Clinical Study Sponsor"), "instanceType": "Organization"}],
        "studyIdentifiers": [{"id": "StudyIdentifier_1", "text": "ZB-001", "scopeId": "Organization_1",
                              "instanceType": "StudyIdentifier"}],
        "eligibilityCriterionItems": [{"id": "EligibilityCriterionItem_1", "name": "IN01",
                                       "text": '<p>Adults aged at least <usdm:tag name="min_age"/> years with '
                                               'chronic zorbitis</p>',
                                       "dictionaryId": "Dict_1", "instanceType": "EligibilityCriterionItem"}],
        "dictionaries": [{"id": "Dict_1", "name": "IE", "instanceType": "SyntaxTemplateDictionary",
                          "parameterMaps": [{"id": "PM_1", "tag": "min_age", "instanceType": "ParameterMap",
                                             "reference": '<usdm:ref klass="Quantity" id="Q_age" attribute="value">'
                                                          '</usdm:ref>'}]}],
        "ageQuantity": {"id": "Q_age", "value": 18.0, "instanceType": "Quantity"},
        "studyDesigns": [design],
    }
    return {"study": {"id": "S", "name": "ZORB", "versions": [version], "instanceType": "Study"},
            "usdmVersion": "4.0.0", "systemName": "synthetic"}


PAGES = [
    "Protocol ZB-001\nA double-blind, parallel, placebo-controlled study\nApproximately 120 participants will be "
    "enrolled.\nZORB-1 synopsis\n",
    "5.1 Inclusion criteria\n1. Adults aged at least 18 years with chronic zorbitis\nMales and females\n"
    "Objectives\nTo compare the zorbex response rate with placebo\nEndpoint: overall response rate at week 4\n",
    "Schedule of Activities\nVisit Screening Week 2 Week 4\nInformed consent X\nVital signs X X X\n"
    "Hematology panel X X\nECG X X\nVisit windows are ±3 days\n",
]


def docling_table(rows: list[list[str]], header_rows: int = 1, page: int = 3) -> dict:
    grid = [[{"text": t, "column_header": r < header_rows} for t in row] for r, row in enumerate(rows)]
    return {"self_ref": "#/tables/0", "prov": [{"page_no": page}], "data": {"grid": grid}}


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    kp = tmp / "SYN_TEST" / "usdm_v4.json"
    kp.parent.mkdir()
    kp.write_text(json.dumps(synthetic_key()), encoding="utf-8")

    # ---------------------------------------------------------------- reader
    st = ug.read_key(kp, "SYN_TEST")
    st.quirks += ug.extra_quirks(st)
    c = st.counts()
    for cls, n in {"Encounter": 3, "Activity": 4, "ScheduledActivityInstance": 3, "Timing": 3, "StudyArm": 2,
                   "StudyEpoch": 2, "Objective": 1, "Endpoint": 1, "EligibilityCriterion": 1,
                   "InterventionalStudyDesign": 1, "StudyIdentifier": 1}.items():
        check(c.get(cls) == n, f"count {cls}: {c.get(cls)} != {n}")
    check(c["SoA links (activity x instance)"] == 8, "soa links")
    d = st.of("InterventionalStudyDesign")[0].attrs
    check(d["studyPhase"] == "Phase II Trial" and d["blindingSchema"] == "Double Blind Study", "alias decode")
    crit = st.of("EligibilityCriterion")[0]
    check(crit.text == "Adults aged at least 18 years with chronic zorbitis", f"tag rendering: {crit.text!r}")
    check(crit.attrs["category"] == "Inclusion Criteria" and crit.attrs["identifier"] == "01", "criterion attrs")
    ident = st.of("StudyIdentifier")[0]
    check(ident.attrs["scope_type"] == "Clinical Study Sponsor", "identifier scope resolved through scopeId")
    enc = st.idx.get("Encounter_2")
    check(st.idx.ref(enc, "scheduledAt")["id"] == "Timing_2", "generic ...Id resolution")
    check([x["id"] for x in st.idx.ref(st.idx.get("SAI_2"), "activityIds")] == ["Activity_2", "Activity_4"],
          "generic ...Ids resolution")
    check(any("dangling" in q for q in st.quirks), f"dangling reference quirk: {st.quirks}")
    check(any("names are codes" in q for q in st.quirks), "code-like encounter names quirk")

    # ---------------------------------------------------------------- helpers
    check(ug.iso_duration("P3D") == (3, "day") and ug.iso_duration("-P1D") == (-1, "day"), "iso duration")
    check(ug.iso_duration("PT4H") == (4, "hour") and ug.iso_duration("P2W") == (2, "week"), "iso duration 2")
    tv = ug.time_values("Cycle 1, Day 8")
    check(tv.get("cycle") == {1.0} and tv.get("day") == {8.0}, f"time values {tv}")
    check(ug.time_values("Days -42 to -9")["day"] >= {-42.0, -21.0, -9.0}, "time range")
    check(ug.time_values("W2 15±4") == {"week": {2.0}, "": {15.0}}, f"window dropped {ug.time_values('W2 15±4')}")
    alts = ug.encounter_alts("Cycle 1, Day 8")
    check("C1D8" in alts and "Cycle 1 Day 8" in alts, f"encounter alts {alts}")
    check(ug._codeish("E1") and ug._codeish("E1_2") and ug._codeish("INFORMED_CONSENT"), "codeish")
    check(not ug._codeish("LY900018") and not ug._codeish("Week 2"), "not codeish")
    check(ug.norm_tok(".05") == "0.05" and ug.norm_tok("05") == "5" and ug.norm_tok("1,000") == "1000", "numbers")
    check(ug.lcs_len(["a", "b", "c"], ["a", "x", "c"]) == 2, "lcs")
    kt, label = ug.iso_alts("P3D", "P3D")
    check(label == "±3 days" and key_terms_match(kt, "windows are ±3 days"), "window key terms")

    # ---------------------------------------------------------------- PDF search
    pdf = ug.PdfText(PAGES)
    h = pdf.find("To compare the zorbex overall response rate with placebo")  # one extra word: Dice 0.94
    check(h and h[0].page == 2 and h[0].quote.startswith("To compare the zorbex response rate with placebo"),
          f"fuzzy find {h}")
    check(not pdf.find("placebo compare zorbex rate response with the to"), "word order is enforced")
    check(not pdf.find("Approximately 150 participants will be enrolled"), "numbers must match")
    h = pdf.find("ECG", pages=[3], line_start=True)
    check(h and h[0].quote == "ECG X X", f"short label extended to its row: {h}")
    check(pdf.text_has([["zorbitis"]]) and not pdf.text_has([["zorbitis"]], [1]), "text_has")

    # ---------------------------------------------------------------- key visits and gold
    visits = ug.key_visits(st)
    check([v.label for v in visits] == ["Screening", "Week 2", "Week 4"], f"visit order {visits}")
    check(visits[1].window[2] == "-3..3 days", "visit window from scheduledAt timing")
    check(ug.detect_soa_pages(st, pdf, visits) == [3], f"soa pages {ug.detect_soa_pages(st, pdf, visits)}")
    qs, info = ug.build_gold_study(st, pdf)
    errors, _warnings = ev.validate_gold(qs)
    check(not errors, f"gold validates: {errors}")
    byq = {q["question"]: q for q in qs}
    enc_q = byq["List all encounters (visits) in the schedule of activities, in order."]
    check(len(enc_q["answer"]["facts"]) == 3 and enc_q["answer"]["complete_list"], "encounter list facts")
    check(enc_q["evidence"] and "Visit Screening Week 2 Week 4" in [e["quote"] for e in enc_q["evidence"][0]["all_of"]],
          f"SoA header quote {enc_q['evidence']}")
    elig = [q for q in qs if q["category"] == "eligibility.inclusion"][0]
    check(not elig.get("needs_review"), f"criterion verified: {elig.get('review_reasons')}")
    check(elig["evidence"][0]["all_of"][0]["page"] == 2, "criterion quote page")
    f = elig["answer"]["facts"][0]
    check(key_terms_match(f["key_terms"], elig["evidence"][0]["all_of"][0]["quote"]), "key terms satisfied by quote")
    phase = [q for q in qs if q["category"] == "design.phase"][0]
    check(phase.get("needs_review") and phase["evidence"] == [], "phase absent from PDF -> needs_review, no evidence")
    for q in qs:
        check(q["split"] == "test" and q["source"] == "usdm_key" and q["doc"] == "SYN_TEST", "metadata")
        for g in q["evidence"]:
            for e in g["all_of"]:
                norm_page = " ".join(PAGES[e["page"] - 1].split())
                check(e["quote"] in norm_page, f"quote verbatim on its page: {e['quote']!r}")

    # ---------------------------------------------------------------- SoA comparison
    rows = [["", "Screening", "Week 2", "Week 4", "Notes"],
            ["Informed consent", "X", "", "", ""],
            ["Vital signs", "X", "X", "X", "see 8.1"],
            ["Hematology", "X", "", "X", ""],
            ["ECG", "", "X", "", ""]]          # ECG at Week 4 lost (a false negative); Notes column ignored
    rep = ug.soa_compare_study(st, {"tables": [docling_table(rows)]}, pdf)
    check(rep["encounter_recall"] == 1.0 and rep["activity_recall"] == 1.0, f"recall {rep['encounter_recall']}")
    cells = rep["cells"]
    check((cells["tp"], cells["fp"], cells["fn"]) == (7, 0, 1), f"cells {cells}")
    check(rep["fn_cells"] == ["ECG @ Week 4"], f"fn list {rep['fn_cells']}")
    shifted = [r[:1] + r[2:4] + [""] + r[4:] for r in rows]   # a lost column shifts the marks left
    rep2 = ug.soa_compare_study(st, {"tables": [docling_table(shifted)]}, None)
    check(rep2["cells"]["f1"] < cells["f1"], "a shifted grid scores lower")
    check(not ug.find_soa_tables({"tables": [docling_table([["a", "b", "c"], ["d", "e", "f"]])]}), "non-SoA ignored")

    # ---------------------------------------------------------------- public keys (smoke, when present)
    if (ug.PUBLIC / "LZZT_CDISC_Pilot" / "usdm_v4.json").exists():
        lz = ug.load_study("LZZT")
        check(lz.counts()["Encounter"] == 12 and lz.counts()["EligibilityCriterion"] == 31, "LZZT counts")
        check(len(ug.key_visits(lz)) == 12, "LZZT visits")
        if (ug.OUT_PUBLIC / "LZZT_CDISC_Pilot" / "protocol.json").exists():
            r = ug.soa_compare("LZZT", use_pdf=False)
            check(r["encounter_recall"] == 1.0 and r["cells"]["f1"] > 0.9, f"LZZT soa compare {r['cells']}")
    print("ok")


if __name__ == "__main__":
    main()
