"""USDM v4 answer keys -> normalized entities, eval_v1 gold questions, and an SoA comparison.

Public answer keys only (public_protocols/<name>/usdm_v4*.json). Everything runs locally; no API calls.

  python usdm_gold.py counts [NAME ...]                  per-class counts of the normalized entities
  python usdm_gold.py dump NAME [--cls Encounter ...]    readable dump of the normalized entities
  python usdm_gold.py gold [NAME ...] [--out DIR]        build reports/eval/public/<NAME>.json (PDF-verified quotes)
  python usdm_gold.py soa-compare NAME [--json PATH] [--out FILE] [--verbose]
                                                         compare the converted SoA table(s) with the key's
                                                         ScheduledActivityInstances (activity x encounter)

NAME is a folder of public_protocols/ (a unique prefix such as LZZT is enough); "all" = every key.

Reader (`read_key`): the whole JSON is indexed by `id` (duplicates are kept and reported as quirks); every
`...Id` / `...Ids` field is resolved generically through that index (`UsdmIndex.ref`). Codes are decoded
(`Code.decode`, `AliasCode.standardCode.decode`). Eligibility and other texts are rendered from their HTML with the
`<usdm:tag name=..>` parameters of their SyntaxTemplateDictionary and `<usdm:ref ..>` references resolved.
Template variants are tolerated: `StudyDesign` as the design class, `windowLowerBound` for `windowLower`,
`Encounter.epochId`, objective/endpoint wording in `description` when `text` is empty.

Gold (`build_gold`): eval_v1 gold format (reports/eval/README.md), split "test", "source": "usdm_key". Facts carry
`key_terms` derived from the key's names/labels; evidence quotes are verbatim spans of the pypdfium2 text layer found
by a fuzzy token search (token Dice >= 0.9, word order and numbers checked) or, for short values, the PDF line that
satisfies the key terms. A question without a verified quote keeps its facts, gets `evidence: []` and
`needs_review: true`; a question whose critical fact is not verified is flagged too (`review_reasons`).

SoA compare (`soa_compare`): finds SoA-like tables in a Docling JSON (many mark cells, visit-like header), builds an
activity x column matrix, aligns key encounters to columns (text and unit-tagged day/week/cycle numbers, monotonic)
and key activities to rows (fuzzy), reports the alignment, and scores encounter recall, activity recall and
filled-cell precision/recall/F1 over the aligned grid. `--json` takes any Docling JSON or converted output folder.
Notes and the baseline: reports/eval/public/README.md.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public_protocols"
OUT_PUBLIC = ROOT / "output_public"
GOLD_DIR = ROOT / "reports" / "eval" / "public"

# Classes whose scope is covered by a partial key (Bayer, SDR sample "Partial"): SoA, objectives/endpoints/estimands,
# eligibility, plus the identifiers it carries.
PARTIAL_SCOPE = {"Encounter", "Activity", "ScheduledActivityInstance", "ScheduleTimeline", "Timing", "Objective",
                 "Endpoint", "Estimand", "EligibilityCriterion", "EligibilityCriterionItem", "StudyIdentifier",
                 "StudyDesignPopulation"}


# =========================================================================== key discovery

def key_paths() -> dict[str, Path]:
    out = {}
    for p in sorted(PUBLIC.glob("*/usdm_v4*.json")):
        out[p.parent.name] = p
    return out


def resolve_name(name: str, need_key: bool = True) -> str:
    names = sorted(key_paths()) if need_key else sorted(p.name for p in PUBLIC.iterdir() if p.is_dir())
    if name in names:
        return name
    hits = [n for n in names if n.lower().startswith(name.lower()) or name.lower() in n.lower()]
    if len(hits) == 1:
        return hits[0]
    raise SystemExit(f"unknown or ambiguous protocol name {name!r}; choose from {names}")


# =========================================================================== generic index

def decode(code: Any) -> str:
    """Code -> decode; AliasCode -> standardCode.decode; list -> '; '-joined; else ''."""
    if isinstance(code, list):
        return "; ".join(x for x in (decode(c) for c in code) if x)
    if not isinstance(code, dict):
        return ""
    if "standardCode" in code:
        return decode(code.get("standardCode"))
    return str(code.get("decode") or "")


def num(v: Any) -> str:
    """50.0 -> '50'; 0.5 -> '0.5'; None -> ''."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else f"{f:g}"


def quantity(q: Any) -> str:
    if not isinstance(q, dict):
        return ""
    v = num(q.get("value"))
    u = decode(q.get("unit")) if isinstance(q.get("unit"), dict) else str(q.get("unit") or "")
    return f"{v} {u}".strip()


class UsdmIndex:
    """Every dict that has an `id` (and usually `instanceType`), by id. Duplicated ids are kept in order."""

    def __init__(self, data: Any):
        self.by_id: dict[str, list[dict]] = defaultdict(list)
        self.by_type: dict[str, list[dict]] = defaultdict(list)
        self.parent: dict[int, tuple[dict | None, str]] = {}
        self._walk(data, None, "")

    def _walk(self, o: Any, parent: dict | None, key: str) -> None:
        if isinstance(o, dict):
            if isinstance(o.get("id"), str) and o.get("id"):
                self.by_id[o["id"]].append(o)
                self.parent[id(o)] = (parent, key)
            if o.get("instanceType"):
                self.by_type[o["instanceType"]].append(o)
            for k, v in o.items():
                self._walk(v, o if o.get("instanceType") else parent, k)
        elif isinstance(o, list):
            for v in o:
                self._walk(v, parent, key)

    def get(self, oid: Any) -> dict | None:
        if not isinstance(oid, str) or not oid:
            return None
        hits = self.by_id.get(oid)
        return hits[0] if hits else None

    def ref(self, obj: dict, fld: str) -> Any:
        """Resolve `fld` + 'Id' / 'Ids' (or the given full field name) to the object(s)."""
        for name in (fld, fld + "Id", fld + "Ids"):
            if name in obj and (name.endswith("Id") or name.endswith("Ids")):
                v = obj[name]
                if isinstance(v, list):
                    return [x for x in (self.get(i) for i in v) if x is not None]
                return self.get(v)
        return None

    def links(self, obj: dict) -> dict[str, Any]:
        """Every `...Id` / `...Ids` field of `obj` (generic), with its raw value."""
        return {k: v for k, v in obj.items() if (k.endswith("Id") or k.endswith("Ids")) and v not in (None, "", [])}

    def dangling(self) -> list[str]:
        """References that point to no object (key quirk)."""
        out = []
        for oid, objs in self.by_id.items():
            for o in objs:
                for k, v in self.links(o).items():
                    for t in (v if isinstance(v, list) else [v]):
                        if isinstance(t, str) and t and t not in self.by_id:
                            out.append(f"{o.get('instanceType')}.{k} -> {t}")
        return out

    def duplicates(self) -> list[str]:
        return [f"{k} x{len(v)}" for k, v in self.by_id.items() if len(v) > 1]


# =========================================================================== text rendering

_TAG = re.compile(r"<usdm:tag\s+name=\"([^\"]+)\"\s*/?>(?:\s*</usdm:tag>)?", re.I)
_REF = re.compile(r"<usdm:ref\s+([^>]*?)/?>(?:\s*</usdm:ref>)?", re.I)
_ATTR = re.compile(r"(\w+)=\"([^\"]*)\"")
_HTML = re.compile(r"<[^>]+>")


class TextRenderer:
    def __init__(self, idx: UsdmIndex):
        self.idx = idx
        self.tags: dict[str, dict[str, str]] = {}
        for d in idx.by_type.get("SyntaxTemplateDictionary", []):
            self.tags[d["id"]] = {m.get("tag"): m.get("reference") or "" for m in d.get("parameterMaps") or []}
        self.unresolved: list[str] = []

    def _ref(self, attrs: str, depth: int) -> str:
        a = dict(_ATTR.findall(attrs))
        obj = self.idx.get(a.get("id"))
        if obj is None:
            self.unresolved.append(f"ref {a.get('klass')}:{a.get('id')}")
            return ""
        v = obj.get(a.get("attribute") or "")
        if isinstance(v, (int, float)):
            return num(v)
        if isinstance(v, dict):
            return decode(v) or quantity(v)
        return self.render(str(v or ""), None, depth + 1)

    def render(self, text: str, dictionary_id: str | None = None, depth: int = 0) -> str:
        if not text:
            return ""
        if depth > 4:
            return _HTML.sub(" ", text)
        tags = self.tags.get(dictionary_id or "", {})
        if not tags:  # tags may refer to any dictionary
            for d in self.tags.values():
                tags = {**d, **tags}

        def tag_sub(m: re.Match) -> str:
            ref = tags.get(m.group(1))
            if ref is None:
                self.unresolved.append(f"tag {m.group(1)}")
                return m.group(1)
            return self.render(ref, dictionary_id, depth + 1)

        t = _TAG.sub(tag_sub, text)
        t = _REF.sub(lambda m: self._ref(m.group(1), depth), t)
        t = re.sub(r"</(p|li|div|tr)>|<br\s*/?>", "\n", t, flags=re.I)
        t = re.sub(r"<li[^>]*>", "\n- ", t, flags=re.I)
        t = _HTML.sub(" ", t)
        t = html.unescape(t)
        lines = [" ".join(x.split()) for x in t.splitlines()]
        return "\n".join(x for x in lines if x).strip()


# =========================================================================== normalized entities

@dataclass
class Entity:
    cls: str
    id: str
    name: str = ""
    label: str = ""
    description: str = ""
    text: str = ""
    attrs: dict = field(default_factory=dict)
    links: dict = field(default_factory=dict)

    @property
    def display(self) -> str:
        """The most human name: label, else name, else text/description (first line)."""
        for v in (self.label, self.name, self.text, self.description):
            if v and v.strip():
                return v.strip().splitlines()[0]
        return self.id

    def as_dict(self) -> dict:
        d = {"cls": self.cls, "id": self.id}
        for k in ("name", "label", "description", "text"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.attrs:
            d["attrs"] = self.attrs
        if self.links:
            d["links"] = self.links
        return d


@dataclass
class SoaLink:
    timeline: str
    instance: str
    encounter: str | None
    epoch: str | None
    activity: str


@dataclass
class UsdmStudy:
    name: str
    path: Path
    partial: bool
    meta: dict
    entities: dict[str, list[Entity]]
    soa: list[SoaLink]
    quirks: list[str]
    idx: UsdmIndex

    def of(self, cls: str) -> list[Entity]:
        return self.entities.get(cls, [])

    def by_id(self, cls: str) -> dict[str, Entity]:
        return {e.id: e for e in self.of(cls)}

    def counts(self) -> dict[str, int]:
        c = {k: len(v) for k, v in self.entities.items() if v}
        c["SoA links (activity x instance)"] = len(self.soa)
        return c

    def main_timeline(self) -> Entity | None:
        tls = self.of("ScheduleTimeline")
        for t in tls:
            if t.attrs.get("mainTimeline"):
                return t
        return tls[0] if tls else None


def _base(o: dict, cls: str | None = None, text_fields: Iterable[str] = ("text",), rend: TextRenderer | None = None
          ) -> Entity:
    text = ""
    for f in text_fields:
        if o.get(f):
            text = rend.render(str(o[f]), o.get("dictionaryId")) if rend else str(o[f])
            break
    return Entity(cls=cls or o.get("instanceType") or "?", id=str(o.get("id") or ""),
                  name=str(o.get("name") or ""), label=str(o.get("label") or ""),
                  description=rend.render(str(o.get("description") or "")) if rend else str(o.get("description") or ""),
                  text=text)


def iso_duration(v: Any) -> tuple[float, str] | None:
    """'P2D' -> (2, 'day'); '-P1D' -> (-1, 'day'); 'PT4H' -> (4, 'hour'); 'P2W' -> (2, 'week')."""
    if not isinstance(v, str) or not v:
        return None
    m = re.fullmatch(r"(-)?P(?:(\d+(?:\.\d+)?)Y)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)W)?(?:(\d+(?:\.\d+)?)D)?"
                     r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?", v.strip())
    if not m:
        return None
    sign = -1 if m.group(1) else 1
    units = ["year", "month", "week", "day", "hour", "minute", "second"]
    vals = [m.group(i) for i in range(2, 9)]
    nz = [(float(x), u) for x, u in zip(vals, units) if x is not None]
    if not nz:
        return None
    nz = [(x, u) for x, u in nz if x] or nz[:1]
    if len(nz) == 1:
        return sign * nz[0][0], nz[0][1]
    # mixed: express in the smallest unit (rare)
    secs = {"year": 31536000, "month": 2592000, "week": 604800, "day": 86400, "hour": 3600, "minute": 60, "second": 1}
    small = nz[-1][1]
    return sign * sum(x * secs[u] for x, u in nz) / secs[small], small


def read_key(path: Path | str, name: str | None = None) -> UsdmStudy:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    idx = UsdmIndex(data)
    rend = TextRenderer(idx)
    quirks: list[str] = []
    ents: dict[str, list[Entity]] = defaultdict(list)
    study = data.get("study") or {}
    versions = study.get("versions") or []
    v = versions[0] if versions else {}
    if len(versions) > 1:
        quirks.append(f"{len(versions)} StudyVersions; only the first is read")
    designs = v.get("studyDesigns") or []
    sd = designs[0] if designs else {}
    if len(designs) > 1:
        quirks.append(f"{len(designs)} study designs; only the first is read")
    partial = "partial" in path.name.lower()
    meta = {"usdmVersion": data.get("usdmVersion"), "systemName": data.get("systemName"),
            "systemVersion": data.get("systemVersion"), "study_name": study.get("name"),
            "study_label": study.get("label"), "versionIdentifier": v.get("versionIdentifier"),
            "design_class": sd.get("instanceType")}
    if sd.get("instanceType") not in ("InterventionalStudyDesign", "ObservationalStudyDesign"):
        quirks.append(f"design instanceType is {sd.get('instanceType')!r} (USDM 4 has Interventional/"
                      "ObservationalStudyDesign); attributes read the same way")

    # ---- titles, organizations, identifiers
    for o in v.get("titles") or []:
        e = _base(o, "StudyTitle")
        e.attrs["type"] = decode(o.get("type"))
        ents["StudyTitle"].append(e)
    for o in v.get("organizations") or []:
        e = _base(o, "Organization")
        e.attrs.update(type=decode(o.get("type")), identifier=o.get("identifier"),
                       identifierScheme=o.get("identifierScheme"))
        ents["Organization"].append(e)
    for o in v.get("studyIdentifiers") or []:
        e = _base(o, "StudyIdentifier")
        org = idx.get(o.get("scopeId"))
        e.attrs.update(scope=(org or {}).get("label") or (org or {}).get("name") or "",
                       scope_name=(org or {}).get("name") or "", scope_type=decode((org or {}).get("type")))
        if o.get("scopeId") and org is None:
            quirks.append(f"StudyIdentifier {e.text!r}: scopeId {o.get('scopeId')} resolves to nothing")
        e.links = idx.links(o)
        ents["StudyIdentifier"].append(e)
    for o in v.get("abbreviations") or []:
        e = Entity("Abbreviation", o.get("id", ""), text=o.get("abbreviatedText") or "",
                   description=o.get("expandedText") or "")
        ents["Abbreviation"].append(e)
    for o in v.get("dateValues") or []:
        e = _base(o, "GovernanceDate")
        e.attrs.update(type=decode(o.get("type")), dateValue=o.get("dateValue"))
        ents["GovernanceDate"].append(e)

    # ---- design attributes
    if sd:
        e = _base(sd, sd.get("instanceType") or "StudyDesign")
        e.cls = "InterventionalStudyDesign" if e.cls == "StudyDesign" else e.cls
        e.attrs.update(
            studyType=decode(sd.get("studyType")), studyPhase=decode(sd.get("studyPhase")),
            model=decode(sd.get("model")), blindingSchema=decode(sd.get("blindingSchema")),
            intentTypes=decode(sd.get("intentTypes")), subTypes=decode(sd.get("subTypes")),
            characteristics=decode(sd.get("characteristics")), therapeuticAreas=decode(sd.get("therapeuticAreas")),
            rationale=sd.get("rationale") or "")
        masks = [m for m in idx.by_type.get("Masking", [])]
        if masks:
            e.attrs["masking"] = "; ".join(str(m.get("text") or m.get("description") or "") for m in masks)
        ents[e.cls].append(e)
        for o in sd.get("indications") or []:
            ie = _base(o, "Indication")
            ie.attrs["isRareDisease"] = o.get("isRareDisease")
            ents["Indication"].append(ie)

    # ---- arms, epochs, elements, cells
    for o in sd.get("arms") or []:
        e = _base(o, "StudyArm")
        e.attrs.update(type=decode(o.get("type")), dataOriginType=decode(o.get("dataOriginType")))
        e.links = idx.links(o)
        ents["StudyArm"].append(e)
    for o in sd.get("epochs") or []:
        e = _base(o, "StudyEpoch")
        e.attrs["type"] = decode(o.get("type"))
        e.links = idx.links(o)
        ents["StudyEpoch"].append(e)
    for o in sd.get("elements") or []:
        e = _base(o, "StudyElement")
        e.attrs.update(start=((o.get("transitionStartRule") or {}).get("text") or ""),
                       end=((o.get("transitionEndRule") or {}).get("text") or ""))
        e.links = idx.links(o)
        ents["StudyElement"].append(e)
    for o in sd.get("studyCells") or []:
        e = Entity("StudyCell", o.get("id", ""), links=idx.links(o))
        arm, ep = idx.get(o.get("armId")), idx.get(o.get("epochId"))
        els = idx.ref(o, "elementIds") or []
        e.attrs.update(arm=(arm or {}).get("name", ""), epoch=(ep or {}).get("name", ""),
                       elements=[x.get("label") or x.get("name") for x in els])
        ents["StudyCell"].append(e)

    # ---- encounters, activities
    for o in sd.get("encounters") or []:
        e = _base(o, "Encounter")
        e.attrs.update(type=decode(o.get("type")), settings=decode(o.get("environmentalSettings")),
                       contactModes=decode(o.get("contactModes")),
                       start=((o.get("transitionStartRule") or {}).get("text") or ""),
                       end=((o.get("transitionEndRule") or {}).get("text") or ""))
        e.links = idx.links(o)
        ents["Encounter"].append(e)
    for o in sd.get("activities") or []:
        e = _base(o, "Activity")
        e.attrs.update(procedures=[p.get("name") for p in o.get("definedProcedures") or []])
        e.links = idx.links(o)
        ents["Activity"].append(e)
    for o in idx.by_type.get("Procedure", []):
        pe = _base(o, "Procedure")
        pe.attrs["procedureType"] = o.get("procedureType")
        ents["Procedure"].append(pe)

    # ---- timelines, instances, timings
    soa: list[SoaLink] = []
    enc_ids = {e.id for e in ents["Encounter"]}
    for tl in sd.get("scheduleTimelines") or []:
        e = _base(tl, "ScheduleTimeline")
        e.attrs.update(mainTimeline=bool(tl.get("mainTimeline")), entryCondition=tl.get("entryCondition") or "",
                       plannedDuration=(tl.get("plannedDuration") or {}).get("text") if isinstance(
                           tl.get("plannedDuration"), dict) else tl.get("plannedDuration"),
                       instances=len(tl.get("instances") or []), timings=len(tl.get("timings") or []))
        e.links = idx.links(tl)
        ents["ScheduleTimeline"].append(e)
        for t in tl.get("timings") or []:
            te = _base(t, "Timing")
            lo = t.get("windowLower", t.get("windowLowerBound"))
            hi = t.get("windowUpper", t.get("windowUpperBound"))
            wl = t.get("windowLabel")
            if wl is None and (t.get("windowLowerBoundLabel") or t.get("windowUpperBoundLabel")):
                wl = f"{t.get('windowLowerBoundLabel') or ''}..{t.get('windowUpperBoundLabel') or ''}"
            te.attrs.update(type=decode(t.get("type")), value=t.get("value"), valueLabel=t.get("valueLabel") or "",
                            relativeToFrom=decode(t.get("relativeToFrom")), windowLower=lo, windowUpper=hi,
                            windowLabel=wl or "", timeline=tl.get("id"))
            te.links = idx.links(t)
            ents["Timing"].append(te)
        for inst in tl.get("instances") or []:
            it = inst.get("instanceType") or "ScheduledActivityInstance"
            ie = _base(inst, it)
            ie.attrs["timeline"] = tl.get("id")
            ie.links = idx.links(inst)
            if it == "ScheduledDecisionInstance":
                ie.attrs["conditions"] = [(c.get("condition"), c.get("conditionTargetId"))
                                          for c in inst.get("conditionAssignments") or []]
            ents[it].append(ie)
            enc = inst.get("encounterId")
            epoch = inst.get("epochId")
            if enc and enc not in enc_ids:
                quirks.append(f"SAI {inst.get('id')}: encounterId {enc} is not an encounter of the design")
            seen = set()
            for a in inst.get("activityIds") or []:
                if a in seen:
                    quirks.append(f"SAI {inst.get('id')}: activity {a} listed twice")
                    continue
                seen.add(a)
                soa.append(SoaLink(tl.get("id"), inst.get("id"), enc, epoch, a))

    # ---- objectives, endpoints, estimands
    for o in sd.get("objectives") or []:
        e = _base(o, "Objective", ("text",), rend)
        if not e.text and o.get("description"):
            e.text = rend.render(o["description"])
            e.attrs["text_from"] = "description"
        e.attrs["level"] = decode(o.get("level"))
        eps = []
        for p in o.get("endpoints") or []:
            pe = _base(p, "Endpoint", ("text",), rend)
            if not pe.text and p.get("description"):
                pe.text = rend.render(p["description"])
                pe.attrs["text_from"] = "description"
            pe.attrs.update(level=decode(p.get("level")), purpose=p.get("purpose") or "", objective=o.get("id"))
            eps.append(pe.id)
            e.attrs.setdefault("endpoint_idx", []).append(len(ents["Endpoint"]))  # ids may be duplicated
            ents["Endpoint"].append(pe)
        e.links["endpoints"] = eps
        ents["Objective"].append(e)
    for o in sd.get("estimands") or []:
        e = _base(o, "Estimand")
        var = idx.get(o.get("variableOfInterestId"))
        pop = idx.get(o.get("analysisPopulationId"))
        e.attrs.update(populationSummary=o.get("populationSummary") or o.get("summaryMeasure") or "",
                       variableOfInterest=((var or {}).get("text") or (var or {}).get("description") or ""),
                       analysisPopulation=((pop or {}).get("text") or (pop or {}).get("name") or ""),
                       intercurrentEvents=[(i.get("text") or i.get("name"), i.get("strategy"))
                                           for i in o.get("intercurrentEvents") or []],
                       analysisStrategy=decode(o.get("analysisStrategy")))
        e.links = idx.links(o)
        ents["Estimand"].append(e)
    for o in sd.get("analysisPopulations") or []:
        ents["AnalysisPopulation"].append(_base(o, "AnalysisPopulation", ("text",), rend))

    # ---- eligibility
    items = {o.get("id"): o for o in v.get("eligibilityCriterionItems") or []}
    for o in sd.get("eligibilityCriteria") or []:
        e = _base(o, "EligibilityCriterion")
        item = items.get(o.get("criterionItemId")) or idx.get(o.get("criterionItemId")) or {}
        if not item:
            quirks.append(f"EligibilityCriterion {o.get('id')}: no criterion item")
        e.text = rend.render(item.get("text") or o.get("text") or "", item.get("dictionaryId"))
        e.attrs.update(category=decode(o.get("category")), identifier=str(o.get("identifier") or ""),
                       item_name=item.get("name") or "")
        e.links = idx.links(o)
        ents["EligibilityCriterion"].append(e)
    for o in items.values():
        ents["EligibilityCriterionItem"].append(_base(o, "EligibilityCriterionItem", ("text",), rend))

    # ---- population, cohorts
    pop = sd.get("population") or {}
    if pop:
        e = _base(pop, "StudyDesignPopulation")
        age = pop.get("plannedAge") or {}
        e.attrs.update(
            plannedEnrollmentNumber=_qty_or_range(pop.get("plannedEnrollmentNumber")),
            plannedCompletionNumber=_qty_or_range(pop.get("plannedCompletionNumber")),
            plannedAge=_qty_or_range(age), plannedSex=decode(pop.get("plannedSex")),
            includesHealthySubjects=pop.get("includesHealthySubjects"))
        e.links = idx.links(pop)
        ents["StudyDesignPopulation"].append(e)
        for c in pop.get("cohorts") or []:
            ce = _base(c, "StudyCohort")
            ce.attrs.update(plannedEnrollmentNumber=_qty_or_range(c.get("plannedEnrollmentNumber")),
                            plannedAge=_qty_or_range(c.get("plannedAge")), plannedSex=decode(c.get("plannedSex")))
            ce.links = idx.links(c)
            ents["StudyCohort"].append(ce)

    # ---- interventions, administrations
    for o in v.get("studyInterventions") or []:
        e = _base(o, "StudyIntervention")
        e.attrs.update(role=decode(o.get("role")), type=decode(o.get("type")),
                       codes=[c.get("decode") or c.get("code") for c in o.get("codes") or []])
        adm_ids = []
        for a in o.get("administrations") or []:
            ae = _base(a, "Administration")
            dur = a.get("duration") or {}
            ae.attrs.update(dose=quantity(a.get("dose")), route=decode(a.get("route")),
                            frequency=decode(a.get("frequency")),
                            duration=(dur.get("text") or quantity(dur.get("quantity")))
                            if isinstance(dur, dict) else "",
                            intervention=o.get("id"))
            ae.links = idx.links(a)
            adm_ids.append(ae.id)
            ents["Administration"].append(ae)
        e.links["administrations"] = adm_ids
        ents["StudyIntervention"].append(e)

    # ---- amendments
    for o in v.get("amendments") or []:
        e = _base(o, "StudyAmendment")
        e.attrs.update(number=o.get("number"), summary=o.get("summary") or "",
                       primaryReason=decode((o.get("primaryReason") or {}).get("code"))
                       or (o.get("primaryReason") or {}).get("otherReason") or "",
                       secondaryReasons=[decode(r.get("code")) or r.get("otherReason")
                                         for r in o.get("secondaryReasons") or []],
                       changes=[(c.get("summary") or "", c.get("rationale") or "") for c in o.get("changes") or []],
                       dates=[(d.get("label") or d.get("name"), d.get("dateValue")) for d in o.get("dateValues") or []],
                       enrollments=[quantity(x.get("quantity")) for x in o.get("enrollments") or []])
        e.links = idx.links(o)
        ents["StudyAmendment"].append(e)

    # ---- generic quirks
    dup = idx.duplicates()
    if dup:
        quirks.append(f"{len(dup)} duplicated ids (e.g. {', '.join(dup[:4])})")
    dang = idx.dangling()
    if dang:
        c = Counter(d.split(" -> ")[0] for d in dang)
        quirks.append(f"{len(dang)} dangling references: " + ", ".join(f"{k} x{n}" for k, n in c.most_common(6)))
    if rend.unresolved:
        quirks.append(f"{len(rend.unresolved)} unresolved text tags/refs (e.g. {rend.unresolved[:3]})")
    code_like = [e for e in ents["Encounter"] if _codeish(e.name)]
    if code_like:
        quirks.append(f"{len(code_like)}/{len(ents['Encounter'])} encounter names are codes "
                      f"(e.g. {code_like[0].name!r}); labels used")
    if partial:
        quirks.append("partial key (SDR sample): only SoA, objectives/endpoints/estimands and eligibility are scored")
    return UsdmStudy(name or path.parent.name, path, partial, meta, dict(ents), soa, _dedupe(quirks), idx)


def _qty_or_range(q: Any) -> str:
    if not isinstance(q, dict):
        return ""
    if "minValue" in q or "maxValue" in q:
        lo, hi = quantity(q.get("minValue")), quantity(q.get("maxValue"))
        return f"{lo} - {hi}".strip(" -")
    return quantity(q)


def _codeish(s: str) -> bool:
    """'E1', 'E1_2', 'TIM3', 'IN01', 'END1', 'OBJ1': identifiers rather than names."""
    s = (s or "").strip()
    return bool(re.fullmatch(r"[A-Z]{1,6}_?\d{1,3}(?:_\d{1,3})?", s) or re.fullmatch(r"[A-Z0-9]+(?:_[A-Z0-9]+)+", s))


def _dedupe(xs: list[str]) -> list[str]:
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


@lru_cache(maxsize=16)
def load_study(name: str) -> UsdmStudy:
    name = resolve_name(name)
    st = read_key(key_paths()[name], name)
    st.quirks = _dedupe(st.quirks + extra_quirks(st))
    return st


# =========================================================================== text utilities

from keyterm_match import key_terms_match, normalize as kt_normalize  # noqa: E402  (the one key-term contract)

_DASHES = "‐‑‒–—―−⁃﹘﹣－"
_DASHMAP = str.maketrans({**dict.fromkeys(_DASHES, "-"), "\ufffe": "\u00ad"})
_TOK_RAW = re.compile(r"(?<![\w.])[-‐‑‒–—―−]?\.?\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?|[^\W\d_]+|[≥≤<>±%]")
GENERIC = set("""study studies patient patients participant participants subject subjects treatment treated total
assess assessed evaluate evaluation determine change changes baseline score scores week weeks month months year years
day days dose doses dosing using following history criteria criterion least prior based defined include including
investigator protocol clinical data period visit visits safety efficacy measure measures time times number""".split())
STOP = set("""a an the of to in on at for and or with by from as is are be been was were will shall may must
can this that these those any all each per than then into its it their there which who whom whose not no
if when while during after before within up out over under via also such other only both either nor
same been being has have had do does did should would could""".split())
_UNITS = {"d": "day", "day": "day", "days": "day", "w": "week", "wk": "week", "wks": "week", "week": "week",
          "weeks": "week", "m": "month", "mo": "month", "month": "month", "months": "month", "h": "hour",
          "hr": "hour", "hrs": "hour", "hour": "hour", "hours": "hour", "min": "minute", "mins": "minute",
          "minute": "minute", "minutes": "minute", "visit": "visit", "v": "visit", "cycle": "cycle", "c": "cycle",
          "period": "period", "p": "period", "year": "year", "years": "year", "y": "year"}


def norm_tok(t: str) -> str:
    """Casefolded token; numbers unified: '1,000' -> '1000', '0,5' -> '0.5', '.05' -> '0.05', '05' -> '5'."""
    t = unicodedata.normalize("NFKC", t).translate(_DASHMAP).casefold()
    if not re.fullmatch(r"-?\.?\d[\d.,]*", t):
        return t
    if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?", t):
        t = t.replace(",", "")
    elif re.fullmatch(r"-?\d+,\d{1,2}", t):
        t = t.replace(",", ".")
    sign = "-" if t.startswith("-") else ""
    body = t.lstrip("-")
    if body.startswith("."):
        body = "0" + body
    if re.fullmatch(r"0\d+", body):
        body = body.lstrip("0") or "0"
    return sign + body


def tokens(text: str) -> list[str]:
    return [norm_tok(m.group(0)) for m in _TOK_RAW.finditer(text or "")]


def token_spans(text: str) -> list[tuple[str, int, int]]:
    return [(norm_tok(m.group(0)), m.start(), m.end()) for m in _TOK_RAW.finditer(text or "")]


def content_tokens(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in STOP]


def dice(a: list[str], b: list[str]) -> float:
    if not a or not b:
        return 0.0
    inter = sum((Counter(a) & Counter(b)).values())
    return 2 * inter / (len(a) + len(b))


def _tok_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if a[:1].isdigit() or b[:1].isdigit() or a[:1] == "-" or b[:1] == "-":
        return False
    if len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a)):
        return True
    return len(a) >= 8 and len(b) >= 8 and a[:6] == b[:6] and abs(len(a) - len(b)) <= 2  # randomised/randomized


def soft_dice(a: list[str], b: list[str]) -> float:
    """Dice where word tokens also match by prefix (exam ~ examination); numbers must be equal."""
    if not a or not b:
        return 0.0
    used = [False] * len(b)
    inter = 0
    for x in a:
        for j, y in enumerate(b):
            if not used[j] and _tok_eq(x, y):
                used[j] = True
                inter += 1
                break
    return 2 * inter / (len(a) + len(b))


def collapse(s: str) -> str:
    return " ".join((s or "").split())


def lcs_len(a: list[str], b: list[str]) -> int:
    """Length of the longest common subsequence of two token lists."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b):
            cur.append(prev[j] + 1 if x == y else max(prev[j + 1], cur[j]))
        prev = cur
    return prev[-1]


def stem(w: str) -> str:
    """A prefix that the key-term matcher treats as a stem (>= 6 characters match inflections)."""
    return w[:max(6, len(w) - 2)] if len(w) >= 8 else w


# =========================================================================== PDF text layer

_TOC = re.compile(r"\.{5,}|…{2,}|(?:\. ){4,}")  # table-of-contents leader dots: never evidence


@dataclass
class Hit:
    page: int          # 1-based
    quote: str         # verbatim span of the page text, whitespace collapsed
    sim: float
    start: int
    end: int


class PdfText:
    """pypdfium2 text layer, one string per page; U+FFFE (pdfium soft hyphen) is written as U+00AD."""

    def __init__(self, pages: list[str]):
        self.pages = [p.replace("\ufffe", "\u00ad") for p in pages]
        self.spans = [token_spans(p) for p in self.pages]
        self.sets = [set(t for t, _, _ in sp) for sp in self.spans]
        self.df: Counter = Counter()
        for st in self.sets:
            self.df.update(st)
        self.norm_pages = [kt_normalize(p) for p in self.pages]
        lines: Counter = Counter()
        for p in self.pages:
            lines.update({re.sub(r"\d+", "#", collapse(x).casefold()) for x in p.splitlines() if x.strip()})
        self.furniture = {k for k, v in lines.items() if v >= max(3, 0.3 * len(self.pages))}

    @classmethod
    def from_pdf(cls, path: Path | str) -> "PdfText":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        try:
            return cls([pdf[i].get_textpage().get_text_range() for i in range(len(pdf))])
        finally:
            pdf.close()

    def __len__(self) -> int:
        return len(self.pages)

    def idf(self, tok: str) -> float:
        return math.log((len(self.pages) + 1) / (1 + self.df.get(tok, 0)))

    def is_furniture(self, line: str) -> bool:
        return re.sub(r"\d+", "#", collapse(line).casefold()) in self.furniture

    def lines(self, page: int) -> list[tuple[str, int, int]]:
        text = self.pages[page - 1]
        out, pos = [], 0
        for raw in text.splitlines(keepends=True):
            body = raw.rstrip("\r\n")
            if body.strip():
                out.append((body, pos, pos + len(body)))
            pos += len(raw)
        return out

    def _quote(self, page: int, s: int, e: int, max_chars: int) -> str:
        q = collapse(self.pages[page - 1][s:e])
        if len(q) > max_chars:
            cut = q[:max_chars]
            q = cut[:cut.rfind(" ")] if " " in cut else cut
        return q

    def find(self, probe: str, pages: Iterable[int] | None = None, min_sim: float = 0.9, max_tokens: int = 32,
             max_chars: int = 220, line_start: bool = False, extend_line_min: int = 12, best_k: int = 1
             ) -> list[Hit]:
        """Best windows (one per page, best first) whose token Dice with the probe is >= min_sim.

        The probe is cut to its first `max_tokens` tokens. Every number of the (cut) probe must occur in the window.
        With line_start, a window must start at the beginning of a text line (SoA row labels). A quote shorter
        than `extend_line_min` characters is extended to the end of its line (a table row as the PDF reads it)."""
        P = tokens(probe)[:max_tokens]
        if not P:
            return []
        pc = Counter(P)
        pset = set(P)
        nums = {t for t in P if re.fullmatch(r"-?\d+(?:\.\d+)?", t)}
        heads = set(P[:3])
        L = len(P)
        cand = []
        for pg in (pages or range(1, len(self.pages) + 1)):
            if not 1 <= pg <= len(self.pages):
                continue
            cov = len(pset & self.sets[pg - 1]) / len(pset)
            if cov >= min_sim * 0.85:
                cand.append((cov, pg))
        cand.sort(key=lambda x: (-x[0], x[1]))
        hits: list[Hit] = []
        for _, pg in cand[:8]:
            sp = self.spans[pg - 1]
            text = self.pages[pg - 1]
            n = len(sp)
            maxlen = int(L * 1.3) + 2
            best = None
            for i in range(n):
                if sp[i][0] not in heads:
                    continue
                if line_start:
                    k = sp[i][1] - 1
                    while k >= 0 and text[k] in " \t":
                        k -= 1
                    if k >= 0 and text[k] not in "\r\n":
                        continue
                wc: Counter = Counter()
                inter = 0
                for j in range(i, min(n, i + maxlen)):
                    t = sp[j][0]
                    if wc[t] < pc[t]:
                        inter += 1
                    wc[t] += 1
                    if t not in pset:
                        continue
                    d = 2 * inter / (L + j - i + 1)
                    if d >= min_sim and (best is None or d > best[0] + 1e-9):
                        # every number of the probe must be in the window (one may be missing in a long probe,
                        # where the key paraphrases, e.g. 'ADAS-Cog (11)' vs 'ADAS-Cog (see Attachment)')
                        if nums and len(nums - set(wc)) > (1 if L >= 20 else 0):
                            continue
                        # word order must agree too (Dice is a bag measure: "one treatment" != "Treatment One")
                        need = L if L <= 4 else math.ceil(0.85 * L)
                        if lcs_len(P, [x[0] for x in sp[i:j + 1]]) < need:
                            continue
                        best = (d, i, j)
                if best and best[0] >= 0.999:
                    break
            if best and not _TOC.search(text[sp[best[1]][1]:sp[best[2]][2]]):
                d, i, j = best
                s, e = sp[i][1], sp[j][2]
                while e < len(text) and text[e] in ")]+%":  # a bracket/plus that belongs to the last token
                    e += 1
                q = self._quote(pg, s, e, max_chars)
                if len(q) < extend_line_min:
                    le = text.find("\n", e)
                    le = len(text) if le < 0 else le
                    q = self._quote(pg, s, le, max_chars)
                hits.append(Hit(pg, q, round(d, 3), s, e))
        hits.sort(key=lambda h: (-h.sim, h.page))
        return hits[:best_k]

    def text_has(self, key_terms: list[list[str]], pages: Iterable[int] | None = None) -> bool:
        pages = list(pages or range(1, len(self.pages) + 1))
        return key_terms_match(key_terms, " ".join(self.norm_pages[p - 1] for p in pages), normalized=True)


@lru_cache(maxsize=8)
def load_pdf(name: str) -> PdfText:
    return PdfText.from_pdf(PUBLIC / name / "protocol.pdf")


# =========================================================================== key visits (SoA columns of the key)

_ENC_BOILER = re.compile(r"\s*\b(?:encounter|visit)\s*$", re.I)


@dataclass
class KeyVisit:
    id: str
    label: str
    description: str
    alts: list[str]
    activities: list[str]           # activity ids, SoA order
    epoch: str = ""
    timing: str = ""
    window: tuple | None = None     # (lower, upper, label) from the scheduledAt timing
    source: str = "encounter"       # or "instance" when encounter labels contradict their instances


def time_values(text: str) -> dict[str, set[float]]:
    """Unit-tagged numbers: 'Cycle 1, Day 8' -> {cycle:{1}, day:{8}}; 'Days -28 to -2' -> {day: -28..-2};
    'W2 15±4' -> {week:{2}, '':{15}}. Windows (±n, '(-1 day)') are dropped. Short ranges are expanded."""
    t = collapse(unicodedata.normalize("NFKC", text or "").translate(_DASHMAP)).casefold()
    t = re.sub(r"(?:±|\+/-|\+/−)\s*\d+(?:\.\d+)?", " ", t)
    t = re.sub(r"\(\s*[-+]?\s*\d+[^)]*\)", " ", t)
    t = re.sub(r"\b(c)(\d+)\s*(d)(-?\d+)\b", r"cycle \2 day \4", t)
    t = re.sub(r"\b(w|wk|d|v|p)(-?\d+)\b", r"\1 \2", t)
    out: dict[str, set[float]] = defaultdict(set)
    rng = re.compile(r"(?:(?P<u>[a-z]+)\s*)?(?P<a>-?\d+(?:\.\d+)?)\s*(?:to|through|-|→)\s*(?:[a-z]+\s*)?"
                     r"(?P<b>-?\d+(?:\.\d+)?)(?![\d.])")
    spans = []
    for m in rng.finditer(t):
        u = _UNITS.get(m.group("u") or "", "")
        a, b = float(m.group("a")), float(m.group("b"))
        lo, hi = min(a, b), max(a, b)
        if hi - lo <= 60 and lo.is_integer() and hi.is_integer():
            out[u].update(float(x) for x in range(int(lo), int(hi) + 1))
        else:
            out[u].update({lo, hi})
        spans.append((m.start("a"), m.end()))
    rest = list(t)
    for s, e in spans:
        rest[s:e] = " " * (e - s)
    for m in re.finditer(r"(?:(?P<u>[a-z]+)\s*)?(?P<n>-?\d+(?:\.\d+)?)", "".join(rest)):
        out[_UNITS.get(m.group("u") or "", "")].add(float(m.group("n")))
    return {k: v for k, v in out.items() if v}


def encounter_alts(label: str, extra: Iterable[str] = ()) -> list[str]:
    """Spellings of a visit name: as given, without commas, and compact forms (Cycle 1, Day 8 -> C1D8)."""
    out: list[str] = []

    def add(x: str) -> None:
        x = collapse(x).strip(" ,;:")
        if x and x.casefold() not in {o.casefold() for o in out}:
            out.append(x)

    for base in [label, *extra]:
        if not base:
            continue
        b = _ENC_BOILER.sub("", collapse(base))
        add(b)
        add(b.replace(",", ""))
        m = re.fullmatch(r"(?i)cycle\s*(\d+)\s*,?\s*day\s*(-?\d+)", b)
        if m:
            add(f"C{m.group(1)}D{m.group(2)}")
            add(f"C{m.group(1)} D{m.group(2)}")
            add(f"Cycle {m.group(1)} / Day {m.group(2)}")
            add(f"Cycle {m.group(1)} (Day {m.group(2)})")
        m = re.fullmatch(r"(?i)period\s*(\d+)\s*,?\s*day\s*(-?\d+)", b)
        if m:
            add(f"P{m.group(1)}D{m.group(2)}")
            add(f"Period {m.group(1)} / Day {m.group(2)}")
            add(f"Period {m.group(1)} (Day {m.group(2)})")
        m = re.fullmatch(r"(?i)week\s*(-?\d+)", b)
        if m:
            add(f"Wk {m.group(1)}")
            add(f"W{m.group(1)}")
        m = re.fullmatch(r"(?i)day\s*(-?\d+)", b)
        if m:
            add(f"D{m.group(1)}")
        m = re.fullmatch(r"(?i)visit\s*(\d+\w?)", b)
        if m:
            add(f"V{m.group(1)}")
        paren = re.fullmatch(r"(.+?)\s*\(([^)]+)\)", b)
        if paren:
            add(paren.group(1))
            add(paren.group(2))
    return out


def _label_numbers(s: str) -> set[float]:
    return {v for vals in time_values(s).values() for v in vals}


def key_visits(st: UsdmStudy) -> list[KeyVisit]:
    """The key's SoA columns: encounters that have scheduled activities, in encounter order (previous/next chain,
    else list order). Activities come from every timeline's instances at that encounter, plus sub-timelines an
    instance points to (timelineId) whose own instances have no encounter."""
    enc = st.of("Encounter")
    by_enc: dict[str, list[str]] = defaultdict(list)
    tl_acts: dict[str, list[str]] = defaultdict(list)
    sai_desc: dict[str, list[str]] = defaultdict(list)
    for link in st.soa:
        if link.encounter is None:
            tl_acts[link.timeline].append(link.activity)
    for inst in st.of("ScheduledActivityInstance"):
        e = inst.links.get("encounterId")
        if not e:
            continue
        for a in inst.links.get("activityIds") or []:
            if a not in by_enc[e]:
                by_enc[e].append(a)
        for a in tl_acts.get(inst.links.get("timelineId") or "", []):
            if a not in by_enc[e]:
                by_enc[e].append(a)
        for x in (inst.description, inst.label):
            if x and x.strip() not in ("-", "") and not re.fullmatch(r"\d+", x.strip()):
                sai_desc[e].append(x)
    ids = [e.id for e in enc]
    nxt = {e.id: e.links.get("nextId") for e in enc}
    prev = {e.id: e.links.get("previousId") for e in enc}
    order = ids
    heads = [i for i in ids if not prev.get(i) and nxt.get(i)]
    if len(heads) == 1:
        chain, cur, seen = [], heads[0], set()
        while cur and cur in nxt and cur not in seen:
            chain.append(cur)
            seen.add(cur)
            cur = nxt.get(cur)
        if len(chain) >= 0.8 * len(ids):
            order = chain + [i for i in ids if i not in seen]
    byid = {e.id: e for e in enc}
    timings = st.by_id("Timing")
    epochs = st.by_id("StudyEpoch")
    disagree = total = 0
    for e in enc:
        for d in sai_desc.get(e.id, [])[:1]:
            a, b = _label_numbers(e.label), _label_numbers(d)
            if a and b:
                total += 1
                disagree += not (a & b)
    use_sai = total >= 3 and disagree > total / 2
    out = []
    for i in order:
        e = byid[i]
        if not by_enc.get(i):
            continue
        desc = e.description
        label = e.label or (e.name if not _codeish(e.name) else "") or desc
        source = "encounter"
        if use_sai and sai_desc.get(i):
            label, desc, source = re.sub(r"(\d)\.0\b", r"\1", sai_desc[i][0]), e.label, "instance"
        extra = []
        d0 = _ENC_BOILER.sub("", collapse(desc))
        if source == "encounter" and d0 and d0.casefold() != label.casefold() and len(d0) <= 60:
            extra.append(d0)
        if e.name and not _codeish(e.name) and e.name.casefold() != label.casefold():
            extra.append(e.name)
        t = timings.get(e.links.get("scheduledAtId") or "")
        window = None
        if t and (t.attrs.get("windowLower") or t.attrs.get("windowUpper")):
            window = (t.attrs.get("windowLower"), t.attrs.get("windowUpper"), t.attrs.get("windowLabel"))
        epoch = ""
        for inst in st.of("ScheduledActivityInstance"):
            if inst.links.get("encounterId") == i and inst.links.get("epochId") in epochs:
                epoch = epochs[inst.links["epochId"]].display
                break
        out.append(KeyVisit(i, label, desc, encounter_alts(label, extra), by_enc[i], epoch,
                            t.label if t else "", window, source))
    return out


def extra_quirks(st: UsdmStudy) -> list[str]:
    q = []
    visits = key_visits(st)
    if visits and visits[0].source == "instance":
        q.append("encounter labels contradict their ScheduledActivityInstances (day numbers differ); "
                 "instance descriptions are used as visit names")
    epochs = [e.id for e in st.of("StudyEpoch")]
    pos = {e: i for i, e in enumerate(epochs)}
    encs = st.by_id("Encounter")
    for tl in st.of("ScheduleTimeline"):
        seq = [i for i in st.of("ScheduledActivityInstance") if i.attrs.get("timeline") == tl.id
               and i.links.get("epochId") in pos]
        for a, b in zip(seq, seq[1:]):
            if pos[b.links["epochId"]] < pos[a.links["epochId"]]:
                eb = encs.get(b.links.get("encounterId") or "")
                q.append(f"timeline {tl.display!r}: instance {b.name or b.id} goes back to an earlier epoch "
                         f"(encounter {eb.display if eb else '-'}); probably mis-linked")
                break
    referenced = {i.links.get("timelineId") for i in st.of("ScheduledActivityInstance")}
    orphan = []
    for tl in st.of("ScheduleTimeline"):
        if tl.attrs.get("mainTimeline") or tl.id in referenced:
            continue
        insts = [i for i in st.of("ScheduledActivityInstance") if i.attrs.get("timeline") == tl.id]
        if insts and not any(i.links.get("encounterId") for i in insts) and any(i.links.get("activityIds")
                                                                               for i in insts):
            orphan.append(f"{tl.display} ({len(insts)})")
    if orphan:
        q.append(f"{len(orphan)} sub-timeline(s) without encounters and not referenced by any instance "
                 f"(timelineId): {', '.join(orphan)}; their activities are outside the activity x encounter matrix")
    for a in st.of("Administration"):
        dn = _label_numbers(a.attrs.get("dose", ""))
        dd = _label_numbers(a.description)
        if dn and dd and not dn & dd and re.search(r"mg|g/|kg|ml|unit", a.description, re.I):
            q.append(f"Administration {a.name}: dose {a.attrs.get('dose')!r} disagrees with its description "
                     f"{a.description!r}")
    for cls in ("StudyTitle", "Endpoint", "Objective"):
        bad = [e for e in st.of(cls) if re.search(r"\bTBD\b|to be determined|\*\*\*", e.text or "", re.I)]
        if bad:
            q.append(f"{len(bad)} {cls} text(s) are placeholders (e.g. {bad[0].text[:40]!r})")
    seen_obj = Counter(collapse(o.text).casefold() for o in st.of("Objective") if o.text)
    dup = [k for k, v in seen_obj.items() if v > 1]
    if dup:
        q.append(f"{len(dup)} objective text(s) repeated under different levels (e.g. {dup[0][:50]!r})")
    return q


# =========================================================================== key terms

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
_ARABIC = {v: k for k, v in _ROMAN.items()}
_FOOTNOTE_TAIL = re.compile(r"(?:\s+[a-z](?:\s*,\s*[a-z])*|\s*[*†‡§¶#]+)$")
_BRACKETS = re.compile(r"\s*\[[^\]]*\]")
_UNIT_ALTS = {"milligram per kilogram": ["mg/kg"], "milligram": ["mg", "milligram"], "microgram": ["µg", "mcg", "ug"],
              "gram": ["g", "gram"], "milliliter": ["ml", "mL"], "kilogram": ["kg"], "international unit": ["IU"],
              "unit": ["unit", "U"]}
_ROUTE_ALTS = {"oral": ["oral", "orally", "by mouth", "p.o."], "intravenous": ["intravenous", "IV", "i.v."],
               "nasal": ["nasal", "intranasal", "intranasally"], "intramuscular": ["intramuscular", "IM", "i.m."],
               "subcutaneous": ["subcutaneous", "SC", "s.c."], "transdermal": ["transdermal", "patch"],
               "topical": ["topical"], "inhal": ["inhaled", "inhalation"]}


def clean_label(s: str) -> str:
    s = collapse(_BRACKETS.sub("", s or ""))
    return _FOOTNOTE_TAIL.sub("", s).strip(" :;,")


def name_alts(*names: str, min_len: int = 2) -> list[str]:
    """OR-alternatives for a named item: each name, without brackets/footnote letters, the part before and the
    abbreviation inside a trailing parenthesis ('... Scale (NIHSS)' -> both)."""
    out: list[str] = []

    def add(x: str) -> None:
        x = collapse(x).strip(" ,;:-")
        if len(x) >= min_len and x.casefold() not in {o.casefold() for o in out}:
            out.append(x)

    for n in names:
        if not n or _codeish(n):
            continue
        c = clean_label(n)
        add(n)
        add(c)
        m = re.fullmatch(r"(.+?)\s*\(([^()]+)\)\s*(.*)", c)
        if m:
            add(f"{m.group(1)} {m.group(3)}")
            if len(m.group(2)) <= 12:
                add(m.group(2))
        parts = [x for x in re.split(r"\s+-\s+|:\s+", c) if x.strip()]
        if len(parts) > 1 and len(tokens(parts[-1])) >= 2:
            add(parts[-1])  # 'Phase 2 - Cohort A' -> 'Cohort A'
    return out


def distinct_alts(groups: list[list[str]]) -> list[list[str]]:
    """Drop alternatives of one fact that would also satisfy a sibling fact's first (primary) name."""
    out = []
    for i, g in enumerate(groups):
        others = [x[0] for j, x in enumerate(groups) if j != i and x]
        keep = [a for a in g if not any(key_terms_match([[a]], o) and not key_terms_match([[o]], g[0])
                                        for o in others)]
        out.append(keep or g[:1])
    return out


def text_terms(text: str, src: str, pdf: PdfText | None, k: int = 2, with_number: bool = False) -> list[list[str]]:
    """Distinctive AND-groups for a long text, each satisfied by `src` (normally the verified quote):
    acronyms and rare content words (stemmed), ranked by inverse page frequency in the PDF; optionally the first
    number of the text."""
    src_n = kt_normalize(src or text)
    cands = []
    seen = set()
    for pos, m in enumerate(re.finditer(r"[^\W\d_][\w'+\-/]*[\w+]|[^\W\d_]{2,}", text or "")):
        w = m.group(0).strip("-'/")
        lw = w.casefold()
        upp = sum(ch.isupper() for ch in w)
        acro = (upp >= 2 and len(w) <= 12 and not w.isupper()) or (w.isupper() and 2 <= len(w) <= 10)
        if lw in STOP or lw in GENERIC or (len(lw) < 5 and not acro):
            continue
        term = w if acro else stem(lw)
        if "/" in term and not acro:
            continue
        key = term.casefold()[:6]
        if key in seen or not key_terms_match([[term]], src_n, normalized=True):
            continue
        first = tokens(lw)[:1]
        if pdf is not None and first and not pdf.df.get(first[0]):
            continue  # a key spelling the protocol never uses
        seen.add(key)
        score = (pdf.idf(first[0]) if (pdf and first) else 1.0) + (1.0 if acro else 0.0) - pos * 1e-3
        cands.append((score, term))
    cands.sort(key=lambda x: -x[0])
    groups = [[t] for _, t in cands[:k]]
    if with_number:
        for m in re.finditer(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", text or ""):
            n = m.group(0)
            if key_terms_match([[n]], src_n, normalized=True) and n not in {"1", "2", "3"}:
                groups.append([n])
                break
    return groups


def phase_alts(decode_: str) -> list[str]:
    d = (decode_ or "").casefold()
    m = re.search(r"phase\s+([ivx]+|\d)\s*([ab]?)(?:\s*/\s*(?:phase\s+)?([ivx]+|\d)\s*([ab]?))?", d)
    if not m:
        return [re.sub(r"\s*trial$", "", decode_ or "").strip()] if decode_ else []

    def ar(x: str) -> str:
        return _ROMAN.get(x, x)

    a, sa, b, sb = m.group(1), m.group(2), m.group(3), m.group(4)
    out = []
    if b:
        for x, y in ((ar(a), ar(b)), (_ARABIC.get(ar(a), a), _ARABIC.get(ar(b), b))):
            out += [f"phase {x}{sa}/{y}{sb}", f"phase {x}{sa}/phase {y}{sb}", f"phase {x}{sa}-{y}{sb}",
                    f"phase {x}{sa} and {y}{sb}"]
    else:
        for x in (ar(a), _ARABIC.get(ar(a), a)):
            out += [f"phase {x}{sa}", f"phase-{x}{sa}"]
    return list(dict.fromkeys(out))


def blinding_alts(decode_: str) -> list[str]:
    d = (decode_ or "").casefold()
    if "open" in d:
        return ["open-label", "open label", "open-labeled", "unblinded", "non-blinded"]
    for k in ("double", "single", "triple", "quadruple"):
        if k in d:
            return [f"{k}-blind", f"{k} blind", f"{k}-masked", f"{k} masked"]
    return [re.sub(r"\s*study$", "", decode_, flags=re.I)] if decode_ else []


def model_alts(decode_: str) -> list[str]:
    d = (decode_ or "").casefold()
    if "parallel" in d:
        return ["parallel"]
    if "cross" in d:
        return ["crossover", "cross-over"]
    if "single group" in d:
        return ["single group", "single-group", "single arm", "single-arm"]
    if "sequential" in d:
        return ["group sequential", "group-sequential", "sequential"]
    if "factorial" in d:
        return ["factorial"]
    return [re.sub(r"\s*(study|design)$", "", decode_, flags=re.I)] if decode_ else []


def route_alts(decode_: str) -> list[str]:
    d = (decode_ or "").casefold()
    for k, v in _ROUTE_ALTS.items():
        if k in d:
            return v
    return [re.sub(r"\s*route of administration$", "", decode_, flags=re.I)] if decode_ else []


def unit_alts(unit: str) -> list[str]:
    u = (unit or "").casefold().strip()
    return _UNIT_ALTS.get(u, [unit] if unit else [])


def iso_alts(lower: Any, upper: Any) -> tuple[list[list[str]], str] | None:
    """Window key terms: symmetric 'P3D'/'P3D' -> [['±3', '+/-3', ...], ['day']], text '±3 days'."""
    lo, hi = iso_duration(str(lower or "")), iso_duration(str(upper or ""))
    if not lo and not hi:
        return None
    unit = (lo or hi)[1]
    a, b = abs((lo or (0, unit))[0]), abs((hi or (0, unit))[0])
    ua = [unit, unit[0] if unit in ("day", "hour", "week") else unit]
    if unit == "minute":
        ua = ["minute", "min"]
    if a == b:
        n = num(a)
        return [[f"±{n}", f"+/-{n}", f"+ /-{n}", f"plus or minus {n}", f"-{n} to +{n}", f"-{n}/+{n}"], ua], \
            f"±{n} {unit}s"
    return [[f"-{num(a)}", f"{num(a)} {unit}s before", f"{num(a)} {unit} before"],
            [f"+{num(b)}", f"{num(b)} {unit}s after", f"{num(b)} {unit} after"] if b else [f"{num(b)}"], ua], \
        f"-{num(a)} to +{num(b)} {unit}s"


# =========================================================================== gold questions

AREA_CLASS = {"ENC": "Encounter", "SOA": "ScheduledActivityInstance", "TIMING": "Timing", "ARM": "StudyArm",
              "EPOCH": "StudyEpoch", "OBJ": "Objective", "ELIG": "EligibilityCriterion",
              "DESIGN": "InterventionalStudyDesign", "ID": "StudyIdentifier", "POP": "StudyDesignPopulation",
              "INT": "Administration", "TITLE": "StudyTitle", "AMEND": "StudyAmendment"}


@dataclass
class GoldCtx:
    st: UsdmStudy
    pdf: PdfText
    name: str
    prefix: str
    visits: list[KeyVisit]
    soa_pages: list[int]
    header_hits: list[Hit]
    out: list[dict] = field(default_factory=list)
    seq: Counter = field(default_factory=Counter)
    stats: Counter = field(default_factory=Counter)

    def in_scope(self, cls: str) -> bool:
        return not self.st.partial or cls in PARTIAL_SCOPE


def detect_soa_pages(st: UsdmStudy, pdf: PdfText, visits: list[KeyVisit]) -> list[int]:
    acts = st.by_id("Activity")
    names = []
    for a in {a for v in visits for a in v.activities}:
        e = acts.get(a)
        if e:
            alts = [x for x in name_alts(e.label, e.name) if len(x) >= 4]
            if alts:
                names.append(alts)
    scores = []
    for pg in range(1, len(pdf) + 1):
        xl = sum(1 for body, _, _ in pdf.lines(pg)
                 if len(re.findall(r"(?<![\w])[Xx✓√](?:[a-z]{0,1})(?![\w])", body)) >= 2)
        hits = sum(1 for alts in names if key_terms_match([alts], pdf.norm_pages[pg - 1], normalized=True))
        scores.append(hits + xl / 2 if (xl >= 2 or (xl >= 1 and hits >= 3)) else 0)
    if not scores or max(scores) <= 2:
        return []
    top = max(scores)
    keep = [i + 1 for i, s in enumerate(scores) if s >= 0.35 * top]
    best = scores.index(top) + 1
    cluster = {best}
    changed = True
    while changed:
        changed = False
        for p in keep:
            if p not in cluster and any(abs(p - c) <= 3 for c in cluster):
                cluster.add(p)
                changed = True
    # continuation pages repeat the SoA header lines
    def head(pg: int) -> set[str]:
        return {collapse(b).casefold() for b, _, _ in pdf.lines(pg)[:25]
                if len(collapse(b)) >= 6 and not pdf.is_furniture(b)}
    core = set.intersection(*[head(p) for p in sorted(cluster)[:2]]) if len(cluster) >= 2 else head(best)
    for direction in (1, -1):
        p = (max(cluster) if direction > 0 else min(cluster)) + direction
        while 1 <= p <= len(pdf) and len(core & head(p)) >= 3:
            cluster.add(p)
            p += direction
    return sorted(cluster)


def soa_header_hits(st: UsdmStudy, pdf: PdfText, pages: list[int], visits: list[KeyVisit]) -> list[Hit]:
    """The column-header lines of each SoA page, located structurally: the non-furniture lines above the first
    activity row (a line that starts with a key activity name or ends with an X mark), at least 12 characters
    long, ranked by their visit-like tokens (numbers, visit words); up to 3 per page, deduplicated across pages."""
    acts = st.by_id("Activity")
    act_alts = [name_alts(acts[a].label, acts[a].name) for a in {a for v in visits for a in v.activities}
                if a in acts]
    visit_tok = re.compile(r"(?i)\b(visit|day|days|week|wk|cycle|screening|baseline|period|follow-up|follow|"
                           r"treatment|eot|eos|ed|et|end|wash|unscheduled|predose|d-?\d+|w\d+|c\d+d\d+|v\d+)\b"
                           r"|(?<![\w.])[-+]?\d+(?:\.\d+)?(?![\w.])")
    out, seen = [], set()
    for pg in pages:
        lines = pdf.lines(pg)
        first = None
        for li, (body, _, _) in enumerate(lines):
            b = collapse(body)
            starts = any(alts and kt_normalize(b).startswith(kt_normalize(clean_label(alts[0]))[:8])
                         for alts in act_alts)
            if starts or re.search(r"(?<!\w)[Xx✓√][a-z]?\s*$", b):
                first = li
                break
        if first is None:
            continue
        cand = []
        for li, (body, s, e) in enumerate(lines[:first]):
            b = collapse(body)
            if pdf.is_furniture(body) or len(b) < 12 or re.search(r"(?i)schedule of|^table \d|page \d", b):
                continue
            score = len(visit_tok.findall(b))
            if score >= 2 and len(b) <= 150 and score >= 0.25 * max(1, len(tokens(b))):
                cand.append((score, li, b, s, e))
        top = sorted(cand, key=lambda x: (-x[0], x[1]))[:3]
        for _, li, b, s, e in sorted(top, key=lambda x: x[1]):
            key = b.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(Hit(pg, b[:220], 1.0, s, e))
    return out


def _qid(ctx: GoldCtx, area: str) -> str:
    ctx.seq[area] += 1
    return f"{ctx.prefix}-{area}-{ctx.seq[area]:03d}"


def _fact(fid: str, text: str, key_terms: list[list[str]], critical: bool = True, **kw: Any) -> dict:
    f = {"id": fid, "text": text, "alts": [], "critical": critical, "value": kw.pop("value", None),
         "unit": kw.pop("unit", None), "comparator": kw.pop("comparator", None), "key_terms": key_terms}
    f.update({k: v for k, v in kw.items() if v is not None})
    return f


def _emit(ctx: GoldCtx, area: str, question: str, cls: str, attrs: list[str], category: str, kind: str,
          modality: list[str], facts: list[tuple[dict, list[Hit]]], extra_hits: list[Hit] | None = None,
          complete_list: bool = False, notes: str = "", ids: list[str] | None = None,
          answerable: bool = True, forbidden: list[dict] | None = None, reasons: list[str] | None = None) -> dict:
    """Assemble one question: evidence = the distinct verified quotes of its facts (+ structural extra quotes);
    a fact is verified when its key terms match its own quotes (or, noted, the text of the quotes' pages)."""
    reasons = list(reasons or [])
    quotes: list[Hit] = []
    seen = set()
    for h in [h for _, hs in facts for h in hs] + list(extra_hits or []):
        k = (h.page, h.quote.casefold())
        if k not in seen:
            seen.add(k)
            quotes.append(h)
    out_facts = []
    minor: list[str] = []
    for f, hs in facts:
        sink = reasons if f.get("critical", True) else minor  # a non-critical fact never flags the question
        kt = f.get("key_terms") or []
        own = " ".join(h.quote for h in hs)
        allq = " ".join(h.quote for h in quotes)
        if not kt:
            pass
        elif hs and key_terms_match(kt, own):
            pass
        elif quotes and key_terms_match(kt, allq):
            if not hs:
                f.setdefault("key_terms_note", "matched in the question's quotes (table header / row)")
        elif quotes and ctx.pdf.text_has(kt, sorted({h.page for h in quotes})):
            f.setdefault("key_terms_note", "outside quote: matched in the text of the quoted pages")
        elif ctx.pdf.text_has(kt):
            f.setdefault("key_terms_note", "not near the quotes: matched elsewhere in the PDF")
            sink.append(f"{f['id']}: key terms not on the quoted pages")
        else:
            f.setdefault("key_terms_note", "key value not found in the PDF text layer")
            sink.append(f"{f['id']}: key value not found in the PDF ({f['text'][:60]!r})")
        if not hs and not extra_hits:
            sink.append(f"{f['id']}: no verified quote")
        out_facts.append(f)
    evidence = [{"all_of": [{"quote": h.quote, "page": h.page, "sim": h.sim} for h in quotes]}] if quotes else []
    if not answerable:
        evidence = []
    q = {"id": _qid(ctx, area), "doc": ctx.name, "split": "test", "source": "usdm_key",
         "usdm": {"class": cls, "attributes": attrs}, "category": category, "kind": kind, "modality": modality,
         "question": question, "answerable": answerable, "evidence": evidence,
         "answer": {"facts": out_facts if answerable else [], "complete_list": complete_list,
                    "forbidden": forbidden or []},
         "notes": notes, "usdm_ids": ids or []}
    reasons = _dedupe(reasons)
    if minor:
        q["notes"] = (notes + " " if notes else "") + "Non-critical, unverified: " + "; ".join(_dedupe(minor)) + "."
    if answerable and (reasons or not evidence):
        q["needs_review"] = True
        q["review_reasons"] = reasons or ["no verified quote"]
    ctx.out.append(q)
    ctx.stats["questions"] += 1
    ctx.stats["facts"] += len(q["answer"]["facts"])
    return q


def _find_text(ctx: GoldCtx, text: str, pages: Iterable[int] | None = None, **kw: Any) -> list[Hit]:
    """A long key text: its first 32 tokens, else its first sentence / clause (>= 6 tokens) at >= 0.9."""
    hits = ctx.pdf.find(text, pages=pages, **kw)
    if not hits:
        parts = [p for p in re.split(r"(?<=[.;:])\s+|\n", text or "") if len(tokens(p)) >= 6]
        if parts and collapse(parts[0]) != collapse(text):
            hits = ctx.pdf.find(parts[0], pages=pages, **kw)
    ctx.stats["probes"] += 1
    ctx.stats["probes_verified"] += bool(hits)
    return hits


def name_terms(ctx: GoldCtx, alts: list[str], pages: list[int] | None = None) -> tuple[list[list[str]], str]:
    """Key terms for a named item. The phrase alternatives when one occurs in the PDF; else, when all content words
    of the name occur within two lines of the PDF (word order differs: 'Xanomeline Low Dose' vs 'low-dose
    xanomeline'), one AND-group per word; else the phrase alternatives (unverified)."""
    alts = [a for a in alts if a]
    if not alts:
        return [], "no name"
    if ctx.pdf.text_has([alts], pages) or ctx.pdf.text_has([alts]):
        return [alts], ""
    words = [w for w in content_tokens(clean_label(alts[0])) if len(w) >= 3 or w.isdigit()]
    rare = [w for w in words if w not in GENERIC and not w.isdigit() and len(w) >= 4
            and 0 < ctx.pdf.df.get(w, 0) <= 0.3 * len(ctx.pdf)]
    if 2 <= len(words) <= 5 and rare and words_near(ctx.pdf, words, span=8):
        return [[stem(w)] for w in words], "words of the key name, not the verbatim name"
    return [alts], "key name not found in the PDF"


def words_near(pdf: PdfText, words: list[str], span: int = 8) -> bool:
    """All words (prefix match for words >= 4 characters) within `span` tokens somewhere in the PDF."""
    for sp in pdf.spans:
        toks = [t for t, _, _ in sp]
        firsts = [i for i, t in enumerate(toks) if _tok_eq(words[0], t)]
        for i in firsts:
            win = toks[max(0, i - span):i + span + 1]
            if all(any(_tok_eq(w, t) for t in win) for w in words[1:]):
                return True
    return False


def _find_name(ctx: GoldCtx, alts: list[str], pages: list[int] | None, line_start: bool = True,
               fallback_all: bool = True) -> list[Hit]:
    """A named item (activity row label, arm, epoch): fuzzy >= 0.9 on the preferred pages (row starts first),
    then (fallback_all) anywhere."""
    for scope in (([pages, None] if fallback_all else [pages]) if pages else [None]):
        for ls in ([True, False] if line_start else [False]):
            for a in alts:
                if len(tokens(a)) == 0:
                    continue
                hits = ctx.pdf.find(a, pages=scope, line_start=ls)
                if hits:
                    ctx.stats["probes"] += 1
                    ctx.stats["probes_verified"] += 1
                    return hits
    ctx.stats["probes"] += 1
    return []


def _find_lines(ctx: GoldCtx, key_terms: list[list[str]], pages: Iterable[int] | None = None, window: int = 2,
                limit: int = 1, count: bool = True) -> list[Hit]:
    """Lines (joined with up to window-1 following lines) whose text satisfies all key-term groups; one per page.
    A quote shorter than 12 characters takes the next line too."""
    out = []
    for pg in (pages or range(1, len(ctx.pdf) + 1)):
        if not 1 <= pg <= len(ctx.pdf) or not ctx.pdf.text_has(key_terms, [pg]):
            continue
        lines = ctx.pdf.lines(pg)
        for li in range(len(lines)):
            if ctx.pdf.is_furniture(lines[li][0]) or _TOC.search(lines[li][0]):
                continue
            for w in range(1, window + 1):
                if li + w > len(lines):
                    break
                s, e = lines[li][1], lines[li + w - 1][2]
                body = ctx.pdf.pages[pg - 1][s:e]
                if key_terms_match(key_terms, body) and not _TOC.search(body):
                    if len(collapse(body)) < 12 and li + w < len(lines):
                        e = lines[li + w][2]
                        body = ctx.pdf.pages[pg - 1][s:e]
                    q = collapse(body)
                    if len(q) > 220:
                        continue
                    out.append(Hit(pg, q, 1.0, s, e))
                    break
            if out and out[-1].page == pg:
                break
        if len(out) >= limit:
            break
    if count:
        ctx.stats["probes"] += 1
        ctx.stats["probes_verified"] += bool(out)
    return out


def _find_cooccur(ctx: GoldCtx, kts: list[list[list[str]]], window: int = 4, max_chars: int = 300) -> Hit | None:
    """The shortest text window (<= `window` lines, <= max_chars) that satisfies the key terms of the most items of
    a list (>= 2): one quote for a short enumeration (arms, epochs)."""
    best = None
    for pg in range(1, len(ctx.pdf) + 1):
        if sum(ctx.pdf.text_has(kt, [pg]) for kt in kts if kt) < 2:
            continue
        lines = ctx.pdf.lines(pg)
        for li in range(len(lines)):
            if ctx.pdf.is_furniture(lines[li][0]) or _TOC.search(lines[li][0]):
                continue
            for w in range(1, window + 1):
                if li + w > len(lines):
                    break
                s, e = lines[li][1], lines[li + w - 1][2]
                body = ctx.pdf.pages[pg - 1][s:e]
                if len(collapse(body)) > max_chars or _TOC.search(body):
                    break
                n = sum(1 for kt in kts if kt and key_terms_match(kt, body))
                if n >= 2 and (best is None or n > best[0] or (n == best[0] and len(body) < best[2])):
                    best = (n, Hit(pg, collapse(body), 1.0, s, e), len(body))
    return best[1] if best else None


def _sample(items: list, k: int) -> list:
    if len(items) <= k:
        return list(items)
    idx = sorted({round(i * (len(items) - 1) / (k - 1)) for i in range(k)})
    return [items[i] for i in idx]


def _visit_name(v: KeyVisit) -> str:
    d = _ENC_BOILER.sub("", collapse(v.description))
    if d and d.casefold() != v.label.casefold() and v.source == "encounter" and len(d) <= 60:
        return f"{v.label} ({d})"
    return v.label


def q_soa(ctx: GoldCtx) -> None:
    st, pdf = ctx.st, ctx.pdf
    if not ctx.visits:
        return
    acts = st.by_id("Activity")
    pages = ctx.soa_pages or None
    groups = distinct_alts([v.alts for v in ctx.visits])
    # ---- encounter list
    facts = []
    for v, g in zip(ctx.visits, groups):
        facts.append((_fact(f"f{len(facts) + 1}", _visit_name(v), [g], critical=True), []))
    notes = ("Evidence = the SoA column-header lines (located structurally on the SoA pages: "
             f"{ctx.soa_pages}). Key encounter labels are matched against the text of those pages.")
    _emit(ctx, "ENC", "List all encounters (visits) in the schedule of activities, in order.", "Encounter",
          ["name", "label", "description"], "soa.encounters", "enumeration", ["table"], facts,
          extra_hits=ctx.header_hits, complete_list=True, notes=notes, ids=[v.id for v in ctx.visits])
    # ---- activities at a sample of encounters
    galts = {v.id: g for v, g in zip(ctx.visits, groups)}
    soa_acts = [a.id for a in st.of("Activity") if any(a.id in v.activities for v in ctx.visits)]
    aalts = dict(zip(soa_acts, distinct_alts([name_alts(acts[a].label, acts[a].name, acts[a].description
                                                        if len(acts[a].description) <= 40 else "") or [acts[a].display]
                                              for a in soa_acts])))
    rich = [v for v in ctx.visits if len(v.activities) >= 2]
    row_cache: dict[str, list[Hit]] = {}

    def row_hits(aid: str) -> list[Hit]:
        if aid not in row_cache:
            row_cache[aid] = _find_name(ctx, aalts.get(aid) or [acts[aid].display], pages, fallback_all=False)
        return row_cache[aid]

    for v in _sample(rich, 4):
        alist = [a for a in v.activities if a in acts]
        ag = [aalts.get(a) or [acts[a].display] for a in alist]
        facts = []
        for i, (a, g) in enumerate(zip(alist, ag)):
            kt, note = name_terms(ctx, g, pages)
            f = _fact(f"f{i + 1}", acts[a].display, kt)
            if note:
                f["key_terms_note"] = note
            facts.append((f, row_hits(a)))
        _emit(ctx, "SOA", f"Which activities are performed at the {_visit_name(v)} visit?", "ScheduledActivityInstance",
              ["encounterId", "activityIds"], "soa.activities", "enumeration", ["table"], facts,
              extra_hits=ctx.header_hits[:2], complete_list=True, ids=[v.id] + alist,
              notes="Row-label quotes anchor the activities; the column assignment is visible only in the table.")
    # ---- encounters of a sample of activities
    n_vis = len(ctx.visits)
    per_act: dict[str, list[KeyVisit]] = defaultdict(list)
    for v in ctx.visits:
        for a in v.activities:
            per_act[a].append(v)
    order = [a.id for a in st.of("Activity") if a.id in per_act]
    mid = [a for a in order if 2 <= len(per_act[a]) < max(3, n_vis)]
    for aid in _sample(mid or order, 4):
        vs = per_act[aid]
        vg = [galts[v.id] for v in vs]
        facts = [(_fact(f"f{i + 1}", _visit_name(v), [g]), []) for i, (v, g) in enumerate(zip(vs, vg))]
        _emit(ctx, "SOA", f"At which encounters (visits) is '{acts[aid].display}' performed?",
              "ScheduledActivityInstance", ["activityIds", "encounterId"], "soa.activities", "enumeration", ["table"],
              facts, extra_hits=row_hits(aid) + ctx.header_hits, complete_list=True, ids=[aid] + [v.id for v in vs],
              notes="Evidence = the activity's row label and the SoA column headers.")
    # ---- visit windows (distinct windows, up to 3)
    seen = set()
    for v in ctx.visits:
        if not v.window:
            continue
        w = iso_alts(v.window[0], v.window[1])
        if not w or w[1] in seen:
            continue
        seen.add(w[1])
        kt, txt = w
        hits = _find_lines(ctx, kt[:-1], pages=pages, window=1) or _find_lines(ctx, kt[:-1], window=1)
        _emit(ctx, "TIMING", f"What is the allowed time window for the {_visit_name(v)} visit?", "Timing",
              ["windowLower", "windowUpper", "windowLabel"], "soa.timing", "lookup", ["table", "text"],
              [(_fact("f1", txt, kt, value=txt.split()[0].lstrip("±+-") or None,
                      unit=kt[-1][0]), hits)], ids=[v.id],
              notes=f"Key window: {v.window[2] or v.window}. The quote is located by the window value; "
                    "check that it belongs to this visit.")
        if len(seen) >= 3:
            break


def q_design(ctx: GoldCtx) -> None:
    st = ctx.st
    d = (st.of("InterventionalStudyDesign") or [None])[0]
    if d is None:
        return
    specs = [("studyPhase", phase_alts, "What is the study phase?", "design.phase"),
             ("blindingSchema", blinding_alts, "What is the blinding (masking) of the study?", "design.masking"),
             ("model", model_alts, "What is the intervention model (e.g. parallel, crossover, single group)?",
              "design.model")]
    for attr, fn, question, cat in specs:
        val = d.attrs.get(attr)
        alts = fn(val)
        if not val or not alts or not ctx.in_scope("InterventionalStudyDesign"):
            continue
        hits = _find_lines(ctx, [alts], window=1, limit=2)
        _emit(ctx, "DESIGN", question, "InterventionalStudyDesign", [attr], cat, "lookup", ["text"],
              [(_fact("f1", val, [alts]), hits[:1])], ids=[d.id],
              notes=f"Key value: {val!r} (coded decode).")


def q_arms_epochs(ctx: GoldCtx) -> None:
    st = ctx.st
    for cls, area, question, cat, attrs in (
            ("StudyArm", "ARM", "What are the study arms?", "design.arms", ["name", "label", "description"]),
            ("StudyEpoch", "EPOCH", "What are the study epochs (periods), in order?", "design.epochs",
             ["name", "label", "type"])):
        ents = st.of(cls)
        if not ents or not ctx.in_scope(cls):
            continue
        if cls == "StudyArm" and len(ents) == 1:
            alts = ["single arm", "single-arm", "one arm", "1 arm", "single group", "single-group",
                    "single treatment arm", "non-randomized", "nonrandomized"]
            hits = _find_lines(ctx, [alts], window=1, limit=1)
            _emit(ctx, area, "How many study arms are there (single-arm or multi-arm design)?", cls,
                  ["name", "type"], cat, "lookup", ["text"],
                  [(_fact("f1", f"single arm ({ents[0].display})", [alts]), hits)], ids=[ents[0].id],
                  notes=f"Single-arm key (arm {ents[0].name!r} / {ents[0].label!r}).")
            continue
        groups = distinct_alts([name_alts(e.label, e.name) or name_alts(e.description) or [e.display]
                                for e in ents])
        kts = [name_terms(ctx, g) for g in groups]
        dup = Counter(json.dumps(kt) for kt, _ in kts)
        kts = [([g], "key name not found in the PDF") if note and dup[json.dumps(kt)] > 1 else (kt, note)
               for g, (kt, note) in zip(groups, kts)]
        cooc = _find_cooccur(ctx, [kt if note != "key name not found in the PDF" else [] for kt, note in kts])
        facts = []
        for i, (e, g, (kt, note)) in enumerate(zip(ents, groups, kts)):
            if cooc and key_terms_match(kt, cooc.quote):
                hits = [cooc]
            else:
                hits = _find_lines(ctx, kt, window=1 if not note else 2, limit=1)
            f = _fact(f"f{i + 1}", e.display, kt)
            if note:
                f["key_terms_note"] = note
            facts.append((f, hits))
        _emit(ctx, area, question, cls, attrs, cat, "enumeration", ["text"], facts, complete_list=True,
              ids=[e.id for e in ents])


def q_objectives(ctx: GoldCtx) -> None:
    st = ctx.st
    all_eps = st.of("Endpoint")
    by_level: dict[str, list[Entity]] = defaultdict(list)
    for o in st.of("Objective"):
        lvl = re.sub(r"(?i)^trial\s+|\s*objective$", "", o.attrs.get("level") or "").strip() or "Other"
        by_level[lvl].append(o)
    for lvl, objs in by_level.items():
        facts = []
        ids = []
        for o in objs:
            if not o.text or re.search(r"\*\*\*|to be determined", o.text, re.I):
                continue
            hits = _find_text(ctx, o.text)
            facts.append((_fact(f"f{len(facts) + 1}", o.text,
                                text_terms(o.text, hits[0].quote if hits else o.text, ctx.pdf, k=2)), hits))
            ids.append(o.id)
            for k in o.attrs.get("endpoint_idx") or []:
                e = all_eps[k]
                if not e or not e.text or re.search(r"\*\*\*|to be determined", e.text, re.I):
                    continue
                eh = _find_text(ctx, e.text)
                facts.append((_fact(f"f{len(facts) + 1}", f"Endpoint: {e.text}",
                                    text_terms(e.text, eh[0].quote if eh else e.text, ctx.pdf, k=2),
                                    critical=False), eh))
                ids.append(e.id)
        if not facts:
            continue
        _emit(ctx, "OBJ", f"List the {lvl.lower()} objectives of the study and their endpoints.", "Objective",
              ["text", "level", "endpoints"], f"objectives.{lvl.lower().replace(' ', '_')}", "enumeration",
              ["text", "table"], facts, ids=ids,
              notes="Objective facts are critical; endpoint facts are not. Key terms are the rarest words of each "
                    "text that occur in its quote.")


def q_eligibility(ctx: GoldCtx) -> None:
    st = ctx.st
    cats: dict[str, list[Entity]] = defaultdict(list)
    for c in st.of("EligibilityCriterion"):
        cat = "inclusion" if "inclusion" in (c.attrs.get("category") or "").lower() else (
            "exclusion" if "exclusion" in (c.attrs.get("category") or "").lower() else "other")
        cats[cat].append(c)
    for cat, crits in cats.items():
        facts = []
        for c in crits:
            if not c.text:
                continue
            hits = _find_text(ctx, c.text)
            kt = text_terms(c.text, hits[0].quote if hits else c.text, ctx.pdf, k=2, with_number=True)
            ident = (c.attrs.get("identifier") or c.name).strip().rstrip(".")
            facts.append((_fact(f"f{len(facts) + 1}", f"{ident}. {c.text}", kt or [[c.text[:30]]]), hits))
        if not facts:
            continue
        _emit(ctx, "ELIG", f"List the {cat} criteria.", "EligibilityCriterion",
              ["category", "identifier", "criterionItemId"], f"eligibility.{cat}", "enumeration", ["list", "text"],
              facts, complete_list=True, ids=[c.id for c in crits],
              notes="One fact per criterion (identifier + text); key terms are distinctive words/numbers of the "
                    "criterion that occur in its quote.")


def q_identifiers(ctx: GoldCtx) -> None:
    for i in ctx.st.of("StudyIdentifier"):
        scope = f"{i.attrs.get('scope_name')} {i.attrs.get('scope')} {i.attrs.get('scope_type')}".casefold()
        txt = i.text.strip()
        if not txt:
            continue
        if re.fullmatch(r"NCT\d{8}", txt) or "ct-gov" in scope or "clinicaltrials" in scope:
            question, cat = "What is the ClinicalTrials.gov (NCT) number of the study?", "identifiers.registry"
        elif "sponsor" in scope or "drug company" in scope or "pharma" in scope:
            question, cat = "What is the sponsor's protocol identifier (protocol number)?", "identifiers.sponsor"
        elif "ema" in scope.split() or "european" in scope:
            question, cat = "What is the EU trial (EudraCT) number?", "identifiers.registry"
        elif "fda" in scope.split() or "food" in scope:
            question, cat = "What is the IND number?", "identifiers.regulatory"
        elif "who" in scope.split() or "world health" in scope:
            question, cat = "What is the WHO Universal Trial Number (UTN)?", "identifiers.registry"
        else:
            question, cat = f"What is the study identifier assigned by {i.attrs.get('scope') or 'the registry'}?", \
                "identifiers.other"
        hits = _find_lines(ctx, [[txt]], window=1, limit=1)
        placeholder = bool(re.fullmatch(r"NCT(?:12345678|0{8}|9{8})", txt))
        if not hits and placeholder:
            _emit(ctx, "ID", question, "StudyIdentifier", ["text", "scopeId"], cat, "unanswerable", ["text"], [],
                  answerable=False, ids=[i.id],
                  forbidden=[{"text": txt, "why": "placeholder identifier in the USDM key, absent from the protocol",
                              "key_terms": [[txt]]}],
                  notes=f"The key holds the placeholder {txt!r}; the protocol PDF has no such number.")
            continue
        _emit(ctx, "ID", question, "StudyIdentifier", ["text", "scopeId"], cat, "lookup", ["text"],
              [(_fact("f1", txt, [[txt]]), hits)], ids=[i.id])


def q_population(ctx: GoldCtx) -> None:
    pop = (ctx.st.of("StudyDesignPopulation") or [None])[0]
    if pop is None or not ctx.in_scope("StudyDesignPopulation"):
        return
    n = pop.attrs.get("plannedEnrollmentNumber") or ""
    words = ["patient", "participant", "subject", "enrol", "randomi", "approximately", "total"]
    if n:
        nums = re.findall(r"\d+", n)
        target = nums[-1] if nums else n
        kt = [[target]]
        hits = _find_lines(ctx, [[target], words], window=2, limit=1)
        _emit(ctx, "POP", "What is the planned number of participants to be enrolled?", "StudyDesignPopulation",
              ["plannedEnrollmentNumber"], "population.enrollment", "lookup", ["text"],
              [(_fact("f1", f"{n} participants", kt, value=target), hits)], ids=[pop.id],
              notes=f"Key plannedEnrollmentNumber: {n!r}." + (" A range in the key: the maximum is asked."
                                                               if len(nums) > 1 else ""))
    age = pop.attrs.get("plannedAge") or ""
    m = re.match(r"(\d+(?:\.\d+)?)\s*(\w+)?(?:\s*-\s*(\d+(?:\.\d+)?)\s*(\w+)?)?", age)
    if m:
        lo, hi = m.group(1), m.group(3)
        facts = []
        h_lo = _find_lines(ctx, [[lo], ["year", "age", "older"]], window=1, limit=1)
        facts.append((_fact("f1", f"minimum age {lo} years", [[lo], ["year", "age", "older"]], value=lo,
                            unit="years", comparator="≥"), h_lo))
        if hi:
            h_hi = _find_lines(ctx, [[hi], ["year", "age"]], window=1, limit=1)
            facts.append((_fact("f2", f"maximum age {hi} years", [[hi], ["year", "age"]], critical=False, value=hi,
                                unit="years", comparator="≤"), h_hi))
        _emit(ctx, "POP", "What is the planned age range of the participants?", "StudyDesignPopulation",
              ["plannedAge"], "population.age", "lookup", ["text"], facts, ids=[pop.id],
              notes=f"Key plannedAge: {age!r}. Keys often carry a nominal maximum (e.g. 100) that the protocol "
                    "does not state; such a fact is non-critical and flagged when absent.")
    sex = pop.attrs.get("plannedSex") or ""
    if sex:
        if sex.casefold() == "both":
            kt = [["male", "men", "both sexes", "either sex", "both genders"],
                  ["female", "women", "both sexes", "either sex", "both genders"]]
        else:
            kt = [[sex.casefold()]]
        hits = _find_lines(ctx, kt, window=1, limit=1)
        _emit(ctx, "POP", "Which sexes are eligible for the study?", "StudyDesignPopulation", ["plannedSex"],
              "population.sex", "lookup", ["text"], [(_fact("f1", sex, kt), hits)], ids=[pop.id])


def q_interventions(ctx: GoldCtx) -> None:
    st = ctx.st
    if not ctx.in_scope("Administration"):
        return
    adms = st.by_id("Administration")
    for si in st.of("StudyIntervention"):
        names = name_alts(si.label, si.name, si.description if len(si.description) <= 40 else "",
                          *[c for c in si.attrs.get("codes") or [] if c and len(c) > 2])
        names = [n for n in names if len(n) >= 3] or names
        in_pdf = [n for n in names if ctx.pdf.text_has([[n]])]
        display = in_pdf[0] if in_pdf else si.display
        base_reasons = [] if in_pdf else [f"intervention name {si.display!r} not found in the PDF"]
        for aid in si.links.get("administrations") or []:
            a = adms.get(aid)
            if not a:
                continue
            reasons = list(base_reasons)
            dn, dd = _label_numbers(a.attrs.get("dose", "")), _label_numbers(a.description)
            if dn and dd and not dn & dd and re.search(r"mg|kg|ml|unit", a.description, re.I):
                reasons.append(f"key dose {a.attrs.get('dose')!r} disagrees with the key's own description "
                               f"{a.description!r}")
            facts = []
            dose = a.attrs.get("dose") or ""
            dm = re.match(r"(\d+(?:\.\d+)?)\s*(.*)", dose)
            bind = [in_pdf] if in_pdf else []
            if dm:
                ua = unit_alts(dm.group(2))
                kt = [[dm.group(1)]] + ([ua] if ua else [])
                h = (_find_lines(ctx, bind + kt, window=2, limit=1) if bind else []) or \
                    _find_lines(ctx, kt, window=1, limit=1)
                facts.append((_fact("f1", f"{display}: {dose}", kt, value=dm.group(1), unit=(ua or [None])[0]), h))
                bind_dose = [[dm.group(1)]]
            else:
                bind_dose = []
            ra = route_alts(a.attrs.get("route") or "")
            if ra:
                h = (_find_lines(ctx, bind + [ra], window=2, limit=1) if bind else []) or \
                    (_find_lines(ctx, bind_dose + [ra], window=2, limit=1) if bind_dose else [])
                facts.append((_fact(f"f{len(facts) + 1}", f"route: {a.attrs.get('route')}", [ra]), h))
            fr = a.attrs.get("frequency") or ""
            if fr:
                fa = {"daily": ["daily", "once a day", "once-daily", "qd", "every day", "/day"],
                      "once": ["single", "once"], "once weekly": ["weekly", "once a week", "qw", "every week"],
                      "every three weeks": ["every 3 weeks", "q3w", "every three weeks"]}.get(fr.casefold(),
                                                                                              [fr.casefold()])
                h = (_find_lines(ctx, bind + [fa], window=2, limit=1) if bind else []) or \
                    (_find_lines(ctx, bind_dose + [fa], window=2, limit=1) if bind_dose else [])
                facts.append((_fact(f"f{len(facts) + 1}", f"frequency: {fr}", [fa], critical=False), h))
            if not facts:
                continue
            q = f"What dose, route and frequency are used for {display}" + \
                (f" ({a.description})" if a.description and len(a.description) <= 60 else "") + "?"
            _emit(ctx, "INT", q, "Administration", ["dose", "route", "frequency"], "intervention.dosing", "lookup",
                  ["text", "table"], facts, ids=[si.id, a.id], reasons=reasons,
                  notes="Route/frequency quotes must name the intervention or the dose within two lines.")


def q_titles(ctx: GoldCtx) -> None:
    if not ctx.in_scope("StudyTitle"):
        return
    for t in ctx.st.of("StudyTitle"):
        ty = (t.attrs.get("type") or "").casefold()
        if not t.text or re.fullmatch(r"(?i)tbd|n/?a", t.text.strip()):
            continue
        if "official" in ty:
            hits = _find_text(ctx, t.text)
            kt = text_terms(t.text, hits[0].quote if hits else t.text, ctx.pdf, k=3)
            _emit(ctx, "TITLE", "What is the official title of the study?", "StudyTitle", ["text", "type"],
                  "titles.official", "lookup", ["text"], [(_fact("f1", t.text, kt), hits)], ids=[t.id])
        elif "acronym" in ty:
            hits = _find_lines(ctx, [[t.text]], window=1, limit=1)
            _emit(ctx, "TITLE", "What is the study acronym?", "StudyTitle", ["text", "type"], "titles.acronym",
                  "lookup", ["text"], [(_fact("f1", t.text, [[t.text]]), hits)], ids=[t.id])


def q_amendments(ctx: GoldCtx) -> None:
    ams = ctx.st.of("StudyAmendment")
    if not ams or not ctx.in_scope("StudyAmendment"):
        return
    facts = []
    for a in ams:
        nb = str(a.attrs.get("number") or "").strip()
        summ = TextRenderer(ctx.st.idx).render(a.attrs.get("summary") or "")
        hits = _find_text(ctx, summ) if len(tokens(summ)) >= 4 else []
        kt = [[f"amendment {nb}", f"amendment ({nb.lower()})", f"amendment {nb.lower()}"]] if nb else []
        kt += text_terms(summ, hits[0].quote if hits else summ, ctx.pdf, k=1)
        facts.append((_fact(f"f{len(facts) + 1}", f"Amendment {nb}: {summ[:200]}", kt), hits))
    _emit(ctx, "AMEND", "List the protocol amendments and the main reason for each.", "StudyAmendment",
          ["number", "summary", "primaryReason"], "amendments.history", "enumeration", ["text", "table"], facts,
          ids=[a.id for a in ams], notes="Amendment numbers with the key's summaries.")


def build_gold(name: str) -> tuple[list[dict], dict]:
    name = resolve_name(name)
    return build_gold_study(load_study(name), load_pdf(name))


def build_gold_study(st: UsdmStudy, pdf: PdfText) -> tuple[list[dict], dict]:
    """All key-derived questions of one protocol (ids <PREFIX>-<AREA>-<nnn>, doc = the protocol folder name)."""
    name = st.name
    visits = key_visits(st)
    pages = detect_soa_pages(st, pdf, visits)
    ctx = GoldCtx(st, pdf, name, re.sub(r"[^A-Z0-9]", "", name.split("_")[0].upper()), visits, pages,
                  soa_header_hits(st, pdf, pages, visits))
    q_titles(ctx)
    q_identifiers(ctx)
    q_design(ctx)
    q_arms_epochs(ctx)
    q_population(ctx)
    q_interventions(ctx)
    q_objectives(ctx)
    q_eligibility(ctx)
    q_soa(ctx)
    q_amendments(ctx)
    info = {"name": name, "soa_pages": pages, "visits": len(visits), "stats": dict(ctx.stats),
            "quirks": st.quirks, "header_quotes": [h.quote for h in ctx.header_hits]}
    return ctx.out, info


# =========================================================================== SoA comparison

_MARK = re.compile(r"^[xX✓✔√•●■☑](?:[a-z](?=$|[\s,;]))?(?:$|[\s,;(*†‡§¶0-9])")
_EMPTY = {"", "-", "–", "—", "n/a", "na", "(-)", "0", "no", "none", "not applicable"}
_NOTE_COL = re.compile(r"(?i)\b(notes?|comments?|protocol section|see section|section|reference|remarks?)\b")
_VISIT_HDR = re.compile(r"(?i)\b(visit|days?|weeks?|wk|cycles?|screening|baseline|period|follow|treatment|"
                        r"eot|eos|end of|wash|randomi[sz]ation|unscheduled|inpatient|outpatient)\b|\b[vdwc]-?\d")
_UNIT_WORD = re.compile(r"(?i)\b(days?|weeks?|wk|visits?|cycles?|periods?|hours?|months?)\b")


def is_mark_like(t: str) -> bool:
    t = collapse(t)
    return bool(t) and len(t) <= 60 and bool(_MARK.match(t))


def is_filled(t: str) -> bool:
    """A cell that schedules something: any content except dashes / n/a (Lilly writes '240 min')."""
    return collapse(t).casefold() not in _EMPTY


@dataclass
class SoaGrid:
    ref: str
    page: int
    cells: list[list[str]]
    header_rows: list[int]
    label_cols: list[int]
    visit_cols: list[int]
    col_desc: dict[int, str]
    col_time: dict[int, dict[str, set[float]]]
    row_label: dict[int, str]
    marks: int

    @property
    def shape(self) -> tuple[int, int]:
        return len(self.cells), (len(self.cells[0]) if self.cells else 0)


def _grid_texts(t: dict) -> tuple[list[list[str]], list[list[bool]]]:
    g = (t.get("data") or {}).get("grid") or []
    texts = [[collapse((c or {}).get("text") or "") for c in row] for row in g]
    hdr = [[bool((c or {}).get("column_header")) for c in row] for row in g]
    return texts, hdr


def find_soa_tables(doc: dict, min_marks: int = 4) -> list[SoaGrid]:
    """SoA-like tables of a Docling JSON: >= min_marks X/✓ cells, >= 3 columns, a visit-like header."""
    out = []
    for ti, t in enumerate(doc.get("tables") or []):
        cells, hflags = _grid_texts(t)
        if len(cells) < 2 or len(cells[0]) < 3:
            continue
        marks = sum(is_mark_like(x) for row in cells for x in row)
        if marks < min_marks:
            continue
        first_mark = next((r for r, row in enumerate(cells) if any(is_mark_like(x) for x in row)), len(cells))
        head_text = " ".join(" ".join(r) for r in cells[:max(1, min(first_mark, 6))])
        if not _VISIT_HDR.search(head_text):
            continue
        n_cols = len(cells[0])

        def section_row(row: list[str]) -> bool:
            ne = [x for x in row if x]
            return len(ne) >= 2 and len(set(ne)) == 1

        header_rows = [r for r in range(first_mark) if not section_row(cells[r])
                       and (any(hflags[r]) or sum(bool(x) for x in cells[r][1:]) >= 2)]
        body = [r for r in range(len(cells)) if r not in header_rows]
        label_cols = [0]
        for c in range(1, min(3, n_cols - 1)):
            vals = [cells[r][c] for r in body if c < len(cells[r])]
            ne = [v for v in vals if v]
            hdr = " ".join(cells[r][c] for r in header_rows)
            if re.search(r"\d", hdr):
                break
            if not ne:  # an empty column under a header such as 'VISIT' / 'WEEK': part of the label block
                label_cols.append(c)
                continue
            mk = sum(is_mark_like(v) for v in ne) / len(ne)
            if mk < 0.15 and len(ne) >= 0.3 * len(vals):
                label_cols.append(c)
            else:
                break
        visit_cols = []
        col_desc: dict[int, str] = {}
        col_time: dict[int, dict[str, set[float]]] = {}
        for c in range(n_cols):
            if c in label_cols:
                continue
            parts, tv = [], defaultdict(set)
            for r in header_rows:
                txt = cells[r][c]
                rl = " ".join(dict.fromkeys(cells[r][lc] for lc in label_cols if cells[r][lc]))
                if txt and (not parts or parts[-1] != txt):
                    parts.append(f"{rl} {txt}".strip() if rl and len(rl) <= 40 and rl != txt else txt)
                unit = ""
                um = _UNIT_WORD.findall(rl)
                if um:
                    unit = _UNITS.get(um[-1].casefold().rstrip("s"), _UNITS.get(um[-1].casefold(), ""))
                for u, vals in time_values(txt).items():
                    tv[u or unit].update(vals)
            desc = " | ".join(parts)
            vals = [cells[r][c] for r in body]
            ne = [v for v in vals if v]
            long_text = ne and sum(len(v) > 25 for v in ne) / len(ne) > 0.5 and \
                sum(is_mark_like(v) for v in ne) / len(ne) < 0.1
            if _NOTE_COL.search(desc) or long_text:
                continue
            visit_cols.append(c)
            col_desc[c] = desc
            col_time[c] = dict(tv)
        row_label = {}
        for r in body:
            lab = " ".join(dict.fromkeys(cells[r][lc] for lc in label_cols if cells[r][lc]))
            if lab and not section_row(cells[r]):
                row_label[r] = lab
        prov = (t.get("prov") or [{}])[0]
        out.append(SoaGrid(t.get("self_ref") or f"#/tables/{ti}", prov.get("page_no") or 0, cells, header_rows,
                           label_cols, visit_cols, col_desc, col_time, row_label, marks))
    return out


def _act_alts(a: Entity) -> list[str]:
    return name_alts(a.label, a.name, a.description if 0 < len(a.description) <= 40 else "")


def _label_sim(alts: list[str], label: str) -> float:
    lt = content_tokens(clean_label(label))
    ln = kt_normalize(clean_label(label))
    best = 0.0
    for a in alts:
        at = content_tokens(a)
        s = soft_dice(at, lt)
        an = kt_normalize(clean_label(a))
        if an and ln and (ln == an or ln.startswith(an + " ") or an.startswith(ln + " ")
                          or ((ln.startswith(an) or an.startswith(ln)) and min(len(an), len(ln)) >= 5)):
            s = max(s, 0.9)
        elif at and lt and len(lt) <= 4:
            # every word of the activity inside a short row label ('Chemistry' ~ 'Laboratory (Chem/Hemat)')
            used = [False] * len(lt)
            hit = 0
            for x in at:
                for j, y in enumerate(lt):
                    if not used[j] and _tok_eq(x, y):
                        used[j] = True
                        hit += 1
                        break
            if hit == len(at):
                s = max(s, 0.7)
        best = max(best, s)
    return best


def _visit_col_score(v: KeyVisit, desc: str, ctime: dict[str, set[float]]) -> float:
    dt = content_tokens(desc)
    text = 0.0
    for a in v.alts:
        at = content_tokens(a)
        if not at:
            continue
        used = [False] * len(dt)
        hit = 0
        for x in at:
            for j, y in enumerate(dt):
                if not used[j] and _tok_eq(x, y):
                    used[j] = True
                    hit += 1
                    break
        cont = hit / len(at)
        if cont == 1.0:
            t = 0.75 + 0.25 * soft_dice(at, dt) if len(at) >= 2 else 0.6 + 0.2 * soft_dice(at, dt)
            an = kt_normalize(a)
            if any(kt_normalize(part).startswith(an) for part in desc.split(" | ")):
                t += 0.1  # the column's own header starts with the visit name ('Follow-up/ED')
            text = max(text, t)
        else:
            text = max(text, 0.6 * cont * soft_dice(at, dt) * 2)
    vt: dict[str, set[float]] = defaultdict(set)
    for s in [v.label, v.description] + v.alts[:1]:
        for u, vals in time_values(s).items():
            if u:
                vt[u].update(vals)
    numeric, conflict = 0.0, False
    shared = [u for u in vt if u in ctime and u not in ("visit",)]
    if shared:
        oks = []
        for u in shared:
            inter = vt[u] & ctime[u]
            if not inter:
                conflict = True
            else:
                oks.append(1.0 if len(ctime[u]) <= 2 else 0.8)
        if oks and not conflict:
            numeric = min(oks) if len(shared) == len(oks) else 0.0
    elif vt and "" in ctime:
        main = vt.get("day") or vt.get("week") or set()
        if main and main <= ctime[""]:
            numeric = 0.6
    s = max(text, numeric)
    return s * 0.3 if conflict else s


def _align_monotonic(scores: list[list[float]], thr: float) -> list[int | None]:
    """Visits in order -> columns, non-decreasing column order (a range column may take several visits)."""
    E = len(scores)
    C = len(scores[0]) if scores else 0
    if not E or not C:
        return [None] * E
    best = [[0.0] * (C + 1) for _ in range(E + 1)]   # best[i][j]: visits < i, last column < j
    choice: list[list[tuple]] = [[None] * (C + 1) for _ in range(E + 1)]  # type: ignore[list-item]
    for i in range(E):
        for j in range(C + 1):
            # skip visit i
            cand = [(best[i][j], ("skip", j))]
            # assign visit i to column j-1 (last column becomes j-1 < j)
            if j >= 1 and scores[i][j - 1] >= thr:
                cand.append((best[i][j] + scores[i][j - 1], ("take", j - 1)))
            v, ch = max(cand, key=lambda x: x[0])
            if j >= 1 and best[i + 1][j - 1] > v:
                v, ch = best[i + 1][j - 1], ("carry", j - 1)
            best[i + 1][j] = v
            choice[i + 1][j] = ch
    # backtrack
    res: list[int | None] = [None] * E
    i, j = E, C
    while i > 0:
        ch = choice[i][j]
        if ch[0] == "carry":
            j = ch[1]
            continue
        if ch[0] == "take":
            res[i - 1] = ch[1]
            j = ch[1] + 1
        i -= 1
    return res


def soa_compare(name: str, json_path: Path | str | None = None, use_pdf: bool = True) -> dict:
    """Compare the SoA tables of a converted Docling JSON (default output_public/<name>/protocol.json) with the
    key's activity x encounter matrix; the PDF text layer (if present) gives the activities the PDF SoA shows."""
    name = resolve_name(name)
    json_path = Path(json_path) if json_path else OUT_PUBLIC / name / "protocol.json"
    if json_path.is_dir():  # a converted output folder
        json_path = json_path / "protocol.json"
    doc = json.loads(Path(json_path).read_text(encoding="utf-8"))
    pdf = load_pdf(name) if use_pdf and (PUBLIC / name / "protocol.pdf").exists() else None
    rep = soa_compare_study(load_study(name), doc, pdf)
    rep["json"] = str(json_path)
    return rep


def soa_compare_study(st: UsdmStudy, doc: dict, pdf: PdfText | None = None) -> dict:
    name = st.name
    visits = key_visits(st)
    acts = st.by_id("Activity")
    tables = find_soa_tables(doc)
    key_links = {(a, v.id) for v in visits for a in v.activities if a in acts}
    key_acts = [a.id for a in st.of("Activity") if any(a.id in v.activities for v in visits)]
    # ---- activities -> rows (per table)
    act_rows: dict[str, dict[int, tuple[int, float]]] = defaultdict(dict)
    for ti, g in enumerate(tables):
        for aid in key_acts:
            alts = _act_alts(acts[aid])
            best = (None, 0.0)
            for r, lab in g.row_label.items():
                s = _label_sim(alts, lab)
                if s > best[1] + 1e-9:
                    best = (r, s)
            if best[0] is not None and best[1] >= 0.6:
                act_rows[aid][ti] = (best[0], round(best[1], 3))
    # ---- visits -> columns (per table, monotonic)
    vis_cols: dict[str, dict[int, tuple[int, float, str]]] = defaultdict(dict)
    extra_cols: dict[tuple[str, int], list[int]] = defaultdict(list)
    for ti, g in enumerate(tables):
        cols = g.visit_cols
        if not cols:
            continue
        sc = [[_visit_col_score(v, g.col_desc[c], g.col_time[c]) for c in cols] for v in visits]
        assign = _align_monotonic(sc, 0.55)
        for vi, k in enumerate(assign):
            if k is not None:
                vis_cols[visits[vi].id][ti] = (cols[k], round(sc[vi][k], 3), "text/number")
        # a key visit may span several adjacent PDF columns ('Follow-up/ED' + 'Additional Follow-up'): an unused
        # column takes the neighbouring visit (previous or next anchor) whose score for it is >= 0.6
        used0 = {k for k in assign if k is not None}
        for k in range(len(cols)):
            if k in used0:
                continue
            prev_v = max((vi for vi, kk in enumerate(assign) if kk is not None and kk < k), default=None)
            next_v = min((vi for vi, kk in enumerate(assign) if kk is not None and kk > k), default=None)
            cands = [vi for vi in (prev_v, next_v) if vi is not None and sc[vi][k] >= 0.6]
            if cands:
                vi = max(cands, key=lambda x: sc[x][k])
                extra_cols[(visits[vi].id, ti)].append(cols[k])
        # order fill: equal-sized runs of unassigned visits and unused columns between anchors
        anchors = [(vi, k) for vi, k in enumerate(assign) if k is not None]
        used = used0 | {k for k in range(len(cols)) if any(cols[k] in extra_cols[(v.id, ti)] for v in visits)}
        bounds = [(-1, -1)] + anchors + [(len(visits), len(cols))]
        for (v0, k0), (v1, k1) in zip(bounds, bounds[1:]):
            run = [vi for vi in range(v0 + 1, v1) if assign[vi] is None]
            free = [k for k in range(k0 + 1, k1) if k not in used]
            if run and len(run) == len(free) and (v0 >= 0 or v1 < len(visits)):
                if v0 < 0 and v1 >= len(visits):
                    continue
                for vi, k in zip(run, free):
                    vis_cols[visits[vi].id][ti] = (cols[k], round(sc[vi][k], 3), "order")
    # ---- cells
    tp = fp = fn = 0
    fp_cells, fn_cells = [], []
    observable = set()
    for aid in key_acts:
        for v in visits:
            pairs = [(ti, act_rows[aid][ti][0], c)
                     for ti in act_rows.get(aid, {}) if ti in vis_cols.get(v.id, {})
                     for c in [vis_cols[v.id][ti][0]] + extra_cols.get((v.id, ti), [])]
            if not pairs:
                continue
            observable.add((aid, v.id))
            pred = any(is_filled(tables[ti].cells[r][c]) for ti, r, c in pairs)
            key = (aid, v.id) in key_links
            if pred and key:
                tp += 1
            elif pred:
                fp += 1
                fp_cells.append(f"{acts[aid].display} @ {v.label}")
            elif key:
                fn += 1
                fn_cells.append(f"{acts[aid].display} @ {v.label}")
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    end_to_end_rec = tp / len(key_links) if key_links else 0.0
    # ---- PDF presence of key activities (separates key/PDF mismatch from extraction loss)
    pdf_present = None
    soa_pages: list[int] = []
    if pdf is not None:
        soa_pages = detect_soa_pages(st, pdf, visits)
        txt = " ".join(pdf.norm_pages[p - 1] for p in soa_pages)
        pdf_present = [a for a in key_acts if key_terms_match([_act_alts(acts[a]) or [acts[a].display]], txt,
                                                              normalized=True)]
    aligned_acts = [a for a in key_acts if act_rows.get(a)]
    aligned_vis = [v.id for v in visits if vis_cols.get(v.id)]
    rep = {
        "name": name, "json": "", "soa_pages_pdf": soa_pages,
        "tables": [{"ref": g.ref, "page": g.page, "shape": list(g.shape), "header_rows": g.header_rows,
                    "label_cols": g.label_cols, "visit_cols": len(g.visit_cols), "marks": g.marks}
                   for g in tables],
        "key": {"visits": len(visits), "activities": len(key_acts), "links": len(key_links)},
        "encounter_recall": round(len(aligned_vis) / len(visits), 3) if visits else 0.0,
        "activity_recall": round(len(aligned_acts) / len(key_acts), 3) if key_acts else 0.0,
        "cells": {"tp": tp, "fp": fp, "fn": fn, "precision": round(prec, 3), "recall": round(rec, 3),
                  "f1": round(f1, 3), "observable_pairs": len(observable),
                  "key_links_observable": sum(1 for x in key_links if x in observable),
                  "end_to_end_recall": round(end_to_end_rec, 3)},
        "alignment": {
            "encounters": [{"id": v.id, "label": v.label,
                            "columns": [{"table": tables[ti].ref, "col": c, "header": tables[ti].col_desc.get(c, ""),
                                         "score": s, "method": m} for ti, (c, s, m) in vis_cols.get(v.id, {}).items()]
                            + [{"table": tables[ti].ref, "col": c, "header": tables[ti].col_desc.get(c, ""),
                                "score": None, "method": "extra"} for (vid, ti), cs in extra_cols.items()
                               if vid == v.id for c in cs]}
                           for v in visits],
            "activities": [{"id": a, "label": acts[a].display,
                            "rows": [{"table": tables[ti].ref, "row": r, "label": tables[ti].row_label.get(r, ""),
                                      "score": s} for ti, (r, s) in act_rows.get(a, {}).items()]}
                           for a in key_acts]},
        "missing_encounters": [v.label for v in visits if not vis_cols.get(v.id)],
        "missing_activities": [acts[a].display for a in key_acts if not act_rows.get(a)],
        "unaligned_rows": [f"{g.ref}#{r}: {lab}" for ti, g in enumerate(tables) for r, lab in g.row_label.items()
                           if not any(act_rows[a].get(ti, (None,))[0] == r for a in act_rows)][:80],
        "unaligned_columns": [f"{g.ref}#{c}: {g.col_desc[c]}" for ti, g in enumerate(tables) for c in g.visit_cols
                              if not any(vis_cols[v].get(ti, (None,))[0] == c for v in vis_cols)
                              and not any(c in cs for (vid, t2), cs in extra_cols.items() if t2 == ti)],
        "fp_cells": fp_cells[:60], "fn_cells": fn_cells[:60],
    }
    if pdf_present is not None:
        rep["activities_on_pdf_soa_pages"] = len(pdf_present)
        rep["activity_recall_pdf"] = round(sum(1 for a in pdf_present if act_rows.get(a)) / len(pdf_present), 3) \
            if pdf_present else 0.0
    return rep


def print_soa_report(rep: dict, verbose: bool = False) -> None:
    c = rep["cells"]
    print(f"== SoA compare {rep['name']}  ({rep['json']})")
    print(f"   SoA tables found: {len(rep['tables'])}  "
          + ", ".join(f"p{t['page']} {t['shape'][0]}x{t['shape'][1]}" for t in rep["tables"]))
    print(f"   key: {rep['key']['visits']} visits, {rep['key']['activities']} activities, {rep['key']['links']} links;"
          f" PDF SoA pages {rep.get('soa_pages_pdf')}")
    print(f"   encounter recall {rep['encounter_recall']:.3f}   activity recall {rep['activity_recall']:.3f}"
          + (f" (vs key activities on the PDF SoA pages: {rep['activity_recall_pdf']:.3f}, "
             f"n={rep['activities_on_pdf_soa_pages']})" if "activity_recall_pdf" in rep else ""))
    print(f"   cells: P {c['precision']:.3f}  R {c['recall']:.3f}  F1 {c['f1']:.3f}  (tp {c['tp']} fp {c['fp']} "
          f"fn {c['fn']}; {c['key_links_observable']}/{rep['key']['links']} key links observable; "
          f"end-to-end recall {c['end_to_end_recall']:.3f})")
    if rep["missing_encounters"]:
        print(f"   missing encounters ({len(rep['missing_encounters'])}): {rep['missing_encounters'][:20]}")
    if rep["missing_activities"]:
        print(f"   missing activities ({len(rep['missing_activities'])}): {rep['missing_activities'][:20]}")
    if verbose:
        for e in rep["alignment"]["encounters"]:
            cols = "; ".join(f"{x['table'].split('/')[-1]}c{x['col']} '{x['header'][:40]}' {x['score']} {x['method']}"
                             for x in e["columns"])
            print(f"     enc {e['label'][:30]:30s} -> {cols or '-'}")
        for a in rep["alignment"]["activities"]:
            rows = "; ".join(f"{x['table'].split('/')[-1]}r{x['row']} '{x['label'][:40]}' {x['score']}"
                             for x in a["rows"])
            print(f"     act {a['label'][:40]:40s} -> {rows or '-'}")
        print(f"   unaligned rows: {rep['unaligned_rows'][:30]}")
        print(f"   unaligned columns: {rep['unaligned_columns'][:30]}")
        print(f"   FP cells: {rep['fp_cells'][:25]}")
        print(f"   FN cells: {rep['fn_cells'][:25]}")


# =========================================================================== CLI

def _names(args: list[str]) -> list[str]:
    if not args or args == ["all"]:
        return sorted(key_paths())
    return [resolve_name(a) for a in args]


def cmd_counts(names: list[str]) -> None:
    for n in names:
        s = load_study(n)
        print(f"== {s.name} ({s.meta.get('systemName')} {s.meta.get('systemVersion')}, usdm {s.meta.get('usdmVersion')}"
              f"{', PARTIAL' if s.partial else ''})")
        for k, c in sorted(s.counts().items()):
            print(f"   {k:34s} {c}")
        print(f"   {'key visits (encounters with activities)':34s} {len(key_visits(s))}")
        for q in s.quirks:
            print(f"   quirk: {q}")


def cmd_dump(name: str, classes: list[str]) -> None:
    s = load_study(name)
    for cls, es in s.entities.items():
        if classes and cls not in classes:
            continue
        print(f"== {cls} ({len(es)})")
        for e in es:
            print("  ", json.dumps(e.as_dict(), ensure_ascii=False))
    if not classes or "KeyVisit" in classes:
        print("== KeyVisit")
        for v in key_visits(s):
            print("  ", json.dumps({"id": v.id, "label": v.label, "alts": v.alts, "n_activities": len(v.activities),
                                    "epoch": v.epoch, "window": v.window, "source": v.source}, ensure_ascii=False))


def cmd_gold(names: list[str], out_dir: Path, write: bool = True) -> dict:
    summary = {}
    for n in names:
        qs, info = build_gold(n)
        by_cls = Counter(q["usdm"]["class"] for q in qs)
        quotes = sum(len(g["all_of"]) for q in qs for g in q["evidence"])
        nr = sum(bool(q.get("needs_review")) for q in qs)
        noev = sum(1 for q in qs if q["answerable"] and not q["evidence"])
        summary[n] = {"questions": len(qs), "facts": sum(len(q["answer"]["facts"]) for q in qs),
                      "quotes": quotes, "needs_review": nr, "answerable_without_evidence": noev,
                      "unanswerable": sum(not q["answerable"] for q in qs), "by_class": dict(sorted(by_cls.items())),
                      "probes": info["stats"].get("probes", 0),
                      "probes_verified": info["stats"].get("probes_verified", 0),
                      "soa_pages": info["soa_pages"], "quirks": info["quirks"]}
        print(f"== {n}: {len(qs)} questions, {summary[n]['facts']} facts, {quotes} quotes, needs_review {nr} "
              f"(no evidence {noev}); probes verified {summary[n]['probes_verified']}/{summary[n]['probes']}; "
              f"SoA pages {info['soa_pages']}")
        if write:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"{n}.json").write_text(json.dumps(qs, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("counts")
    p.add_argument("names", nargs="*")
    p = sub.add_parser("dump")
    p.add_argument("name")
    p.add_argument("--cls", nargs="*", default=[])
    p = sub.add_parser("gold")
    p.add_argument("names", nargs="*")
    p.add_argument("--out", default=str(GOLD_DIR))
    p.add_argument("--no-write", action="store_true")
    p.add_argument("--summary", default=None, help="also write the per-protocol summary JSON here")
    p = sub.add_parser("soa-compare")
    p.add_argument("name")
    p.add_argument("--json", default=None, help="Docling JSON of the converted output, or a converted output "
                                                "folder holding protocol.json (default: output_public/<name>/)")
    p.add_argument("--out", default=None, help="write the full report (JSON) here")
    p.add_argument("--no-pdf", action="store_true", help="skip the PDF presence check")
    p.add_argument("--verbose", "-v", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "counts":
        cmd_counts(_names(a.names))
    elif a.cmd == "dump":
        cmd_dump(a.name, a.cls)
    elif a.cmd == "gold":
        s = cmd_gold(_names(a.names), Path(a.out), write=not a.no_write)
        if a.summary:
            Path(a.summary).write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")
    elif a.cmd == "soa-compare":
        names = _names([a.name])
        for n in names:
            rep = soa_compare(n, a.json if len(names) == 1 else None, use_pdf=not a.no_pdf)
            print_soa_report(rep, a.verbose)
            if a.out:
                out = Path(a.out)
                if len(names) > 1:
                    out = out / f"{n}.json"
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
