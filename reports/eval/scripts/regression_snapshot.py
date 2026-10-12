"""Regression snapshot of the current outputs, and comparison of two snapshots (numbers, hashes, booleans).

  python reports/eval/scripts/regression_snapshot.py snapshot NAME [LABEL ...]
  python reports/eval/scripts/regression_snapshot.py compare OLD NEW

Labels: MAIN, the public protocol folders under output_public/, and P1..Pn (test_protocols.py). Default:
all of them. Per label: word coverage (validate_output), table count and marks, chunk / parent / section /
table-row counts, SHA-256 of the chunk texts, sections, tables and the Docling JSON, and for public
protocols with a USDM key the SoA activity x visit F1 and encounter recall (usdm_gold).

Snapshots go to reports/regression/snapshots/NAME.json (local only). Nothing printed is text from a document:
for P protocols the output is numbers and booleans only ("same" / "changed"), and errors show the exception
type and our file:line only.
"""

from __future__ import annotations

import hashlib
import json
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import pypdfium2 as pdfium  # noqa: E402

from test_protocols import final_doc_json, output_dir, protocols, source_pdf  # noqa: E402

SNAPSHOTS = ROOT / "reports" / "regression" / "snapshots"
HASHES = ("chunks_sha", "sections_sha", "tables_sha", "doc_sha")


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()[:16]


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def labels() -> list[str]:
    public = sorted(p.name for p in (ROOT / "output_public").iterdir() if (p / "protocol.json").exists())
    return ["MAIN", *public, *(pid for pid, _ in protocols())]


def snapshot_one(label: str) -> dict:
    from docling_core.types.doc import DoclingDocument

    from validate_output import aggregate, page_coverage

    doc_path = final_doc_json(label)
    chunks_dir = output_dir(label) / "chunks"
    doc = DoclingDocument.load_from_json(doc_path)
    pdf = pdfium.PdfDocument(str(source_pdf(label)))
    cov = aggregate(page_coverage(doc, pdf))
    chunks = _jsonl(chunks_dir / "chunks.jsonl")
    tables = _jsonl(chunks_dir / "tables.jsonl")
    sections_file = chunks_dir / "sections.json"
    sections = json.loads(sections_file.read_text(encoding="utf-8")) if sections_file.exists() else {}
    secs = sections.get("sections", []) if isinstance(sections, dict) else []
    out = {
        "coverage": cov["coverage"], "pages_below_97": cov["pages_below_threshold"],
        "pages": len(doc.pages), "tables": len(doc.tables), "pictures": len(doc.pictures),
        "chunks": len(chunks), "parents": len(_jsonl(chunks_dir / "parents.jsonl")), "sections": len(secs),
        "table_records": len(tables),
        "chunks_sha": _sha("\n".join(c.get("text", "") for c in chunks)),
        "sections_sha": _sha(json.dumps([(s.get("section_id"), s.get("title"), s.get("parent_id"), s.get("role"))
                                         for s in secs], ensure_ascii=False)),
        "tables_sha": _sha(json.dumps([(t.get("table_id"), t.get("markdown"), t.get("chain")) for t in tables],
                                      ensure_ascii=False)),
        "doc_sha": _sha(doc_path.read_bytes()),
    }
    public = ROOT / "public_protocols" / label
    if any((public / k).exists() for k in ("usdm_v4.json", "usdm_v4_partial.json")):
        import usdm_gold

        rep = usdm_gold.soa_compare(label)
        out["soa_f1"] = rep["cells"]["f1"]
        out["soa_encounter_recall"] = rep["encounter_recall"]
    return out


def cmd_snapshot(name: str, which: list[str]) -> None:
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    result = {}
    for label in which or labels():
        try:
            result[label] = snapshot_one(label)
            r = result[label]
            soa = f", SoA F1 {r['soa_f1']:.3f}" if "soa_f1" in r else ""
            print(f"{label}: coverage {r['coverage']:.2%}, chunks {r['chunks']}, sections {r['sections']}, "
                  f"tables {r['tables']}{soa}", flush=True)
        except Exception as exc:  # noqa: BLE001
            frames = [f"{Path(f.filename).name}:{f.lineno}" for f in traceback.extract_tb(exc.__traceback__)
                      if ROOT in Path(f.filename).resolve().parents or Path(f.filename).parent == ROOT]
            result[label] = {"error": f"{type(exc).__name__} at {' > '.join(frames[-3:])}"}
            print(f"{label}: {result[label]['error']}", flush=True)
    path = SNAPSHOTS / f"{name}.json"
    path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"written {path.relative_to(ROOT)}")


def cmd_compare(old: str, new: str) -> int:
    a = json.loads((SNAPSHOTS / f"{old}.json").read_text(encoding="utf-8"))
    b = json.loads((SNAPSHOTS / f"{new}.json").read_text(encoding="utf-8"))
    changed = 0
    for label in sorted(set(a) | set(b), key=lambda x: (x != "MAIN", x.startswith("P") and x[1:].isdigit(), x)):
        x, y = a.get(label, {}), b.get(label, {})
        diffs = []
        for k in sorted(set(x) | set(y)):
            if x.get(k) == y.get(k):
                continue
            diffs.append(f"{k} changed" if k in HASHES else f"{k} {x.get(k)} -> {y.get(k)}")
        changed += bool(diffs)
        print(f"{label}: {'same' if not diffs else '; '.join(diffs)}")
    return 1 if changed else 0


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "snapshot":
        cmd_snapshot(sys.argv[2], sys.argv[3:])
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        sys.exit(cmd_compare(sys.argv[2], sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(2)
