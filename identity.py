"""The identity record of a protocol: titles, sponsor, registry identifiers and version, each value with the exact
item it was read from (ref, page, layer, text, section), recorded where the rules extract it, so no tool has to
search the document for where a value came from.

    from identity import build_identity
    ident = build_identity(doc, stats["document"])    # the stored values; meta=None computes them from doc

Values and methods come from chunk_protocol's rules (detect_metadata: titles, acronym, protocol id, version, date,
each with its source item; registry_identifiers(with_refs=True): NCT, EudraCT, EU CT, IND, WHO UTN with the item or
table row of their source text) and from the cover-page and sponsor rules below (cover_fields and the sponsor
patterns, copied from protocol_api so that module can drop them).

A stored value (stats.json "document") that the rules no longer produce, e.g. an older output or a value given on
chunk_protocol's command line, is kept with method "stored" and located in the first item that contains it as a
whole token (not inside a longer word or number: "4" is not in "NCT03421379"); a stored amendment number is located
through the stored version label ("Amendment 4"), in that label's item (without a stored label: the number itself).

    {"schema": 1,
     "fields": {"official_title", "brief_title", "acronym", "detected_title", "sponsor", "sponsor_protocol_id", "nct",
                "eudract", "eu_ct", "ind", "who_utn", "version_label", "amendment", "version_date":
                Field | None},
     "cover_page": {label: {"value", "page", "text", "refs"}},
     "others": {kind: [values cited elsewhere in the text]}}
    Field  = {"value", "method", "source": Source | None}                 (the sponsor also has "verbatim")
    Source = {"page", "layer": "body" | "furniture" | "table", "text", "ref", "refs", "row", "section_id"}

ref is the item the text was taken from (a table row: the table's ref, with row = the index in table.data.grid),
refs every item the text spans (a cover label and its value on the next line: both; the first is ref), section_id
the section of ref (None for header/footer items).

The stored record, chunks/identity.json (write_record / load_record), is this record plus where it comes from:
    {"schema", "source_sha256" (of the Docling JSON, as stats.json "document"), "meta_sha256" (meta_stamp of the
     stats.json "document" block the rule values were built from), "pipeline_version", "model" (None: rules only),
     "prompt_version", "fields", "cover_page", "others", "llm" (None: rules only)}
A field a model chose (identity_llm) has method MODEL_METHOD, its source from the lines it cited, and "rules":
{"value", "method"} when it replaced a rule value. A record whose meta_sha256 is not the stamp of the stats.json the
reader holds (chunk_protocol.py was run again, or the record predates the stamp) is "stale": the reader rebuilds the
rules record from that stats.json.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from docling_core.types.doc import ContentLayer, DoclingDocument, TableItem, TextItem

# chunk_protocol is imported where it is used: it pulls in the chunker's tokenizer stack (seconds).

SCHEMA = 1
FIELDS = ("official_title", "brief_title", "acronym", "detected_title", "sponsor",
          "sponsor_protocol_id", "nct", "eudract", "eu_ct", "ind", "who_utn",
          "version_label", "amendment", "version_date")
IDENTIFIER_KINDS = ("sponsor_protocol_id", "nct", "eudract", "eu_ct", "ind", "who_utn")


# =========================================================================== cover page and sponsor rules
# Copied from protocol_api (ProtocolDocument._cover_fields / _sponsor); the property pattern is fixed so that
# "property of Eli Lilly and Company" gives the whole name ("and" no longer swallows the space before "Company").

_COVER_FIELDS = [  # (field, label regex); order matters ("short title" before "title")
    ("brief_title", r"(?:short|brief|public|lay)\s+(?:protocol\s+|study\s+)?title"),
    ("official_title", r"(?:official|full|scientific|protocol|study|trial)\s+title|title\s+of\s+(?:the\s+)?(?:study|trial|protocol)|title"),
    ("acronym", r"(?:study\s+|trial\s+)?acronym"),
    ("sponsor_protocol_number", r"(?:sponsor\s+)?(?:protocol|study|trial)\s+(?:number|no\.?|code|identifier|id)"),
    ("amendment", r"amendment\s+(?:number|no\.?)|amendment"),
    ("version", r"(?:protocol\s+|document\s+)?version(?:\s+(?:number|no\.?))?"),
    ("sponsor", r"sponsor(?:\s+name)?(?:\s+and\s+(?:legal\s+)?(?:registered\s+)?address)?"),
    ("sponsor_address", r"(?:sponsor\s+)?legal\s+registered\s+address"),
    ("phase", r"(?:study\s+|trial\s+|clinical\s+)?phase"),
    ("compound", r"compound(?:\s+(?:number|name|code))?|investigational\s+(?:medicinal\s+)?product"),
    ("indication", r"(?:study\s+)?indication"),
    ("approval_date", r"(?:protocol\s+)?(?:approval|issue|release|effective|version|document|protocol)\s+date|date\s+of\s+(?:protocol|issue|approval)"),
]
_COVER_RX = [(f, re.compile(rf"(?i)^\s*(?:{rx})\s*(?:\([^)]{{0,40}}\))?\s*[:：]\s*(?P<value>.*)$")) for f, rx in _COVER_FIELDS]
_LABEL_LINE = re.compile(r"[^:：]{1,60}[:：]")  # a short line ending with a colon: a label of its own, not a value
_COPYRIGHT = re.compile(r"(?i:copyright)\s*(?:©|\((?i:c)\))?\s*(?:\d{4}(?:\s*[-–,]\s*\d{4})?\s*)?(?:(?i:by)\s+)?"
                        r"(?P<name>[A-Z][\w&'.-]*(?:\s+(?:[A-Z][\w&'.-]*|and|&|of))*)")
_PROPERTY = re.compile(r"(?i:property\s+of)\s+(?:(?i:the)\s+)?"
                       r"(?P<name>[A-Z][\w&'.-]*(?:\s+(?:[A-Z][\w&'.-]*|and(?=\s+[A-Z])|&(?=\s+[A-Z]))){0,5})")
_COMPANY = re.compile(r"\b(?P<name>[A-Z][A-Za-z&'.-]+(?:\s+(?:[A-Z][A-Za-z&'.-]+|and|&|de|of)){0,5},?\s+"
                      r"(?:Inc|Incorporated|Ltd|Limited|GmbH|AG|S\.A|SA|N\.V|LLC|plc|PLC|Company|Corporation|Corp|"
                      r"K\.K|KK|Co\.,?\s*Ltd|Pharmaceuticals?|Pharma|Therapeutics|Biosciences|Biotech|Oncology)\b\.?)")


def _clean(text: str | None) -> str:
    return " ".join((text or "").split())


def _pages_of(item: Any) -> list[int]:
    return sorted({p.page_no for p in getattr(item, "prov", None) or []})


def _layer_of(item: Any) -> str:
    return "furniture" if item.content_layer == ContentLayer.FURNITURE else "body"


def _sponsor_name(value: str) -> str:
    """'Non-US: Bayer AG, 51368 Leverkusen, Germany' -> 'Bayer AG' (the verbatim value is kept separately)."""
    v = re.sub(r"^[\w\- ]{1,15}:\s*", "", value.strip())
    m = re.match(r"(.+?)(?:,\s*(?=\d)|\s+\d{4,}|\s*;|$)", v)
    return (m.group(1) if m else v).strip(" ,.")


def cover_fields(doc: DoclingDocument) -> dict[str, dict]:
    """Label/value pairs of the title pages (pp. 1-3: "Short Title: ...", "Sponsor Name:" + the next line, ...):
    {field: {"value", "page", "text", "refs"}}; the first item per label wins, a value on the line after its
    label lists both items (text "label / value"). That next line is a value only when it is not itself a label:
    one of the cover labels, or a short line ending with a colon ("Approval Date:" followed by "Sponsor
    Signatory:" gives no approval date)."""
    items = [(t, _pages_of(t)[0]) for t, _ in doc.iterate_items(with_groups=False)
             if isinstance(t, TextItem) and t.text.strip() and _pages_of(t) and _pages_of(t)[0] <= 3]
    out: dict[str, dict] = {}

    def label_of(text: str):
        for f, rx in _COVER_RX:
            m = rx.match(text)
            if m:
                return f, m
        return None, None

    for i, (t, page) in enumerate(items):
        text = _clean(t.text)
        f, m = label_of(text)
        if f is None or f in out:
            continue
        value, src = m.group("value").strip(), [t]
        if not value and i + 1 < len(items) and items[i + 1][1] == page:
            nxt = _clean(items[i + 1][0].text)
            if label_of(nxt)[0] is None and not _LABEL_LINE.fullmatch(nxt) and len(nxt) <= 400:
                value, src = nxt, [t, items[i + 1][0]]
        if value:
            out[f] = {"value": value, "page": page, "text": " / ".join(_clean(x.text) for x in src),
                      "refs": [x.self_ref for x in src]}
    return out


def _cover_loc(c: dict) -> dict[str, Any]:
    return {"page": c["page"], "layer": "body", "text": c["text"], "ref": c["refs"][0], "refs": list(c["refs"]),
            "row": None}


def _item_loc(t: Any) -> dict[str, Any]:
    return {"page": _pages_of(t)[0], "layer": _layer_of(t), "text": _clean(t.text), "ref": t.self_ref,
            "refs": [t.self_ref], "row": None}


def detect_sponsor(doc: DoclingDocument, cover: dict[str, dict]) -> dict[str, Any] | None:
    """The sponsor: {"value" (the name), "verbatim" (as written), "method", "loc"} or None.

    Rules in order: the cover-page "Sponsor" label; a copyright line, then a "property of" line, on pp. 1-3 or in a
    header/footer; the most frequent company-name pattern there (unverified). loc is the item it was read from."""
    c = cover.get("sponsor")
    if c:
        return {"value": _sponsor_name(c["value"]), "verbatim": c["value"], "method": "cover-page label",
                "loc": _cover_loc(c)}
    texts = [t for t in doc.texts if t.text and _pages_of(t)]
    early = [t for t in texts if _pages_of(t)[0] <= 3]
    furniture = [t for t in texts if t.content_layer.value == "furniture"]
    for method, rx in (("copyright line", _COPYRIGHT), ("'property of' line", _PROPERTY)):
        for t in early + furniture:
            m = rx.search(_clean(t.text))
            if m:
                name = m.group("name").strip(" .,")
                return {"value": name, "verbatim": name, "method": method, "loc": _item_loc(t)}
    counts: Counter = Counter()
    first: dict[str, Any] = {}
    for t in early + furniture:
        for m in _COMPANY.finditer(_clean(t.text)):
            name = m.group("name").strip(" ,")
            counts[name] += 1
            first.setdefault(name, t)
    if counts:
        name = max(counts, key=lambda n: (counts[n], -list(first).index(n)))
        return {"value": name, "verbatim": name, "method": "company-name pattern on the title pages (unverified)",
                "loc": _item_loc(first[name])}
    return None


# =========================================================================== stored values: locate them

class _ItemIndex:
    """The text items and table rows of a document, to locate a stored value the rules do not produce."""

    def __init__(self, doc: DoclingDocument):
        self.doc = doc
        self._entries: list[tuple[int | None, str, str, str, int | None]] | None = None

    def entries(self) -> list[tuple[int | None, str, str, str, int | None]]:
        """(page, layer, text, ref, row): body texts and table rows in document order, then headers/footers
        by page."""
        if self._entries is None:
            from chunk_protocol import _row_text

            out, furniture = [], []
            for item, _ in self.doc.iterate_items(with_groups=False, traverse_pictures=True,
                                                  included_content_layers={ContentLayer.BODY}):
                page = item.prov[0].page_no if getattr(item, "prov", None) else None
                if isinstance(item, TableItem):
                    for r, row in enumerate(item.data.grid or []):
                        if text := _row_text(row):
                            out.append((page, "table", text, item.self_ref, r))
                elif isinstance(item, TextItem) and item.text.strip():
                    out.append((page, "body", _clean(item.text), item.self_ref, None))
            for t in self.doc.texts:
                if t.content_layer == ContentLayer.FURNITURE and t.text.strip():
                    furniture.append((t.prov[0].page_no if t.prov else None, "furniture", _clean(t.text),
                                      t.self_ref, None))
            self._entries = out + sorted(furniture, key=lambda e: e[0] if e[0] is not None else 10 ** 6)
        return self._entries

    def find(self, value: str, page: int | None = None, layer: str | None = None) -> dict[str, Any] | None:
        """The first item whose whitespace-normalised text contains the value as a whole (not preceded or
        followed by a letter or digit: "4" is not in "NCT03421379"), one on the preferred page and layer first;
        None when no item holds it."""
        from chunk_protocol import _snippet

        needle = _clean(value)
        rx = re.compile(rf"(?<![^\W_]){re.escape(needle)}(?![^\W_])")  # [^\W_]: a letter or digit
        hits = [(e, m) for e in self.entries() if (m := rx.search(e[2]))]
        if not hits:
            return None
        (pg, ly, text, ref, row), m = min(hits, key=lambda h: (page is not None and h[0][0] != page)
                                          + (layer is not None and h[0][1] != layer))
        return {"page": pg, "layer": ly, "text": _clean(_snippet(text, m.start(), m.end())), "ref": ref,
                "refs": [ref], "row": row}


# =========================================================================== the record

def _section_function(doc: DoclingDocument, section_of: Callable[[str], str | None] | None):
    """section_of, else parents.section_index(doc).section_of (built on first use) as a section id function."""
    if section_of is not None:
        return section_of
    cache: dict[str, Any] = {}

    def of(ref: str) -> str | None:
        import parents

        if "index" not in cache:
            cache["index"] = parents.section_index(doc)
        return parents._sid(cache["index"].section_of(ref))

    return of


def _source(loc: dict[str, Any] | None, section_of: Callable[[str], str | None]) -> dict[str, Any] | None:
    if loc is None:
        return None
    ref = loc.get("ref")
    return {"page": loc["page"], "layer": loc["layer"], "text": loc["text"], "ref": ref,
            "refs": list(loc.get("refs") or ([ref] if ref else [])), "row": loc.get("row"),
            "section_id": section_of(ref) if ref else None}


def build_identity(doc: DoclingDocument, meta: dict | None = None, *,
                   section_of: Callable[[str], str | None] | None = None) -> dict:
    """The identity record of a protocol (see the module docstring).

    meta: the stored stats.json "document" (doc_metadata's fields plus "identifiers"); None computes the values
    from doc with the same rules. section_of: item ref -> section id; None uses parents.section_index(doc).
    """
    from chunk_protocol import detect_metadata, registry_identifiers

    section = _section_function(doc, section_of)
    index = _ItemIndex(doc)
    detected = detect_metadata(doc)
    value = lambda key: detected[key]["value"] if detected[key] else None  # noqa: E731
    version = value("version")
    computed = {"protocol_id": value("protocol_id"), "acronym": value("acronym"), "title": value("title"),
                "amendment": version[1] if version and version[0] == "Amendment" else None,
                "version_date": value("date"), "version_label": " ".join(version) if version else None}
    stored = computed if meta is None else meta
    rec = registry_identifiers(doc, stored, with_refs=True)
    stored_ids = rec if meta is None or meta.get("identifiers") is None else meta["identifiers"]
    cover = cover_fields(doc)

    def field(val: str, method: str, loc: dict | None, **extra: Any) -> dict[str, Any]:
        return {"value": val, "method": method, "source": _source(loc, section), **extra}

    def cover_field(key: str) -> dict[str, Any] | None:
        c = cover.get(key)
        return field(c["value"], "cover-page label", _cover_loc(c)) if c else None

    def stored_field(key: str, rule: dict | None, rule_value: Any, via: dict | None = None) -> dict[str, Any] | None:
        """A stored meta value: the rule's method and source when the rule gives the same value, else "stored",
        located in the first item that holds it (or, with `via`, in the source of that other field)."""
        val = stored.get(key)
        if not val:
            return None
        if rule and rule_value == val:
            return field(val, rule["method"], rule["source"])
        return field(val, "stored", via["source"] if via else index.find(val))

    def identifier(kind: str) -> dict[str, Any] | None:
        val = stored_ids.get(kind)
        if not val:
            return None
        found, was = rec["sources"].get(kind), (stored_ids.get("sources") or {}).get(kind)
        if kind != "sponsor_protocol_id":
            method = "registry pattern"
        elif stored.get("protocol_id"):  # doc_metadata's id: the way it was decided
            rule = detected["protocol_id"]
            method = rule["method"] if rule and rule["value"] == stored["protocol_id"] else None
        else:  # no protocol id in the metadata: registry_identifiers found a "Protocol Number: X"
            method = "labelled pattern"
        same = was is None or (found is not None and all(was.get(k) == found.get(k) for k in ("page", "layer", "text")))
        if method and rec.get(kind) == val and same:
            return field(val, method, found)
        return field(val, "stored", index.find(val, (was or {}).get("page"), (was or {}).get("layer")))

    fields: dict[str, dict[str, Any] | None] = {}
    title_rule = detected["title"]
    fields["official_title"] = cover_field("official_title") or stored_field("title", title_rule, value("title"))
    fields["brief_title"] = cover_field("brief_title")
    fields["acronym"] = cover_field("acronym") or stored_field("acronym", detected["acronym"], value("acronym"))
    fields["detected_title"] = stored_field("title", title_rule, value("title"))
    sponsor = detect_sponsor(doc, cover)
    fields["sponsor"] = (field(sponsor["value"], sponsor["method"], sponsor["loc"], verbatim=sponsor["verbatim"])
                         if sponsor else None)
    for kind in IDENTIFIER_KINDS:
        fields[kind] = identifier(kind)
    fields["version_label"] = stored_field("version_label", detected["version"], computed["version_label"])
    # A stored amendment number is located through the stored version label ("Amendment 4"), the same item: the
    # bare number would match any item with that digit. Without a label the number itself is searched.
    fields["amendment"] = stored_field("amendment", detected["version"], computed["amendment"],
                                       via=fields["version_label"])
    fields["version_date"] = stored_field("version_date", detected["date"], value("date"))
    return {
        "schema": SCHEMA,
        "fields": {k: fields[k] for k in FIELDS},
        "cover_page": {k: {kk: (list(v[kk]) if kk == "refs" else v[kk]) for kk in ("value", "page", "text", "refs")}
                       for k, v in cover.items()},
        "others": {k: list(v) for k, v in (stored_ids.get("others") or {}).items()},
    }


# =========================================================================== the stored record (chunks/identity.json)
# Written by the identity stage (identity_llm.run_identity) after chunking: the rules record, or the rules record with
# the fields a model chose and the code verified in the cited lines. document_info, search, the production context's
# identity block and the service's identifiers read it when it belongs to this Docling JSON, else build the rules
# record themselves.

RECORD_FILE = "identity.json"
MODEL_METHOD = "model selection, verified in the cited lines"


def meta_stamp(meta: dict) -> str:
    """The stamp of a stats.json "document" block (first 16 hex digits of the SHA-256 of its sorted JSON): ties a
    stored record to the stats values its rule values come from."""
    text = json.dumps(meta, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_record(chunks_dir: str | Path, record: dict, *, source_sha256: str | None, meta: dict,
                 model: str | None = None, prompt_version: str | None = None, llm: dict | None = None) -> Path:
    """Write chunks/identity.json (the layout in the module docstring) and return its path. meta: the stats.json
    "document" block the record was built from (stored as its meta_stamp). The same input gives the same bytes (no
    timestamps; LF line ends); written to a temporary file first, then moved into place. Without a model,
    prompt_version and llm are stored as None."""
    from pipeline_version import PIPELINE_VERSION

    out = {"schema": SCHEMA, "source_sha256": source_sha256, "meta_sha256": meta_stamp(meta),
           "pipeline_version": PIPELINE_VERSION,
           "model": model or None, "prompt_version": prompt_version if model else None,
           "fields": {k: record["fields"].get(k) for k in FIELDS},
           "cover_page": record.get("cover_page") or {}, "others": record.get("others") or {},
           "llm": llm if model else None}
    path = Path(chunks_dir) / RECORD_FILE
    tmp = path.with_name(RECORD_FILE + ".tmp")
    tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    os.replace(tmp, path)
    return path


_SOURCE_KEYS = ("page", "layer", "text", "ref", "refs", "row", "section_id")


def _valid_source(src: Any) -> bool:
    return (isinstance(src, dict) and all(k in src for k in _SOURCE_KEYS) and isinstance(src["text"], str)
            and isinstance(src["refs"], list) and all(isinstance(r, str) for r in src["refs"])
            and (src["ref"] is None or isinstance(src["ref"], str)))


def _valid_field(key: str, f: Any) -> bool:
    if f is None:
        return True
    if not (isinstance(f, dict) and isinstance(f.get("value"), str) and f["value"]
            and (f.get("method") is None or isinstance(f["method"], str))
            and (f.get("source") is None or _valid_source(f["source"]))):
        return False
    if "rules" in f and not (isinstance(f["rules"], dict) and {"value", "method"} <= set(f["rules"])):
        return False
    return key != "sponsor" or (isinstance(f.get("verbatim"), str) and _valid_source(f.get("source")))


def _valid_record(data: Any) -> bool:
    """The shape the readers rely on: every field None or {"value", "method", "source"} (a sponsor with its
    "verbatim" and a source), cover-page pairs with value / page / text / refs, others as lists."""
    if not (isinstance(data, dict) and data.get("schema") == SCHEMA and isinstance(data.get("fields"), dict)
            and isinstance(data.get("cover_page"), dict) and isinstance(data.get("others"), dict)):
        return False
    if not all(k in data["fields"] and _valid_field(k, data["fields"][k]) for k in FIELDS):
        return False
    if not all(isinstance(c, dict) and isinstance(c.get("value"), str) and isinstance(c.get("text"), str)
               and isinstance(c.get("refs"), list) and c["refs"] and "page" in c for c in data["cover_page"].values()):
        return False
    return all(isinstance(v, list) for v in data["others"].values())


def load_record(chunks_dir: str | Path, source_sha256: str | None,
                meta: dict | None = None) -> tuple[dict | None, str | None]:
    """(the stored record, None), or (None, why not), checked in this order: "missing"; "unreadable" (not JSON);
    "schema" (another schema or a shape the readers cannot use, see _valid_record); "other_document" (written for
    another Docling JSON: its source_sha256 differs, or either is unknown); "stale" (meta given: the record's
    meta_sha256 is missing or is not meta_stamp(meta), i.e. it was built from other stats.json values)."""
    path = Path(chunks_dir) / RECORD_FILE
    if not path.is_file():
        return None, "missing"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "unreadable"
    if not _valid_record(data):
        return None, "schema"
    if not source_sha256 or data.get("source_sha256") != source_sha256:
        return None, "other_document"
    if meta is not None and data.get("meta_sha256") != meta_stamp(meta):
        return None, "stale"
    return data, None
