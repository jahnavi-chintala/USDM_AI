"""Print Markdown coverage tables for reports/eval/gold/MAIN.json (used for MAIN_notes.md)."""
import json
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
qs = json.loads((ROOT / "reports" / "eval" / "gold" / "MAIN.json").read_text(encoding="utf-8"))

cls = defaultdict(list)
for q in qs:
    cls[q["usdm"]["class"]].append(q)
print("| USDM v4 class | n | attributes probed | categories |")
print("|---|---|---|---|")
for c, items in sorted(cls.items(), key=lambda kv: (-len(kv[1]), kv[0])):
    attrs = sorted({a for q in items for a in q["usdm"]["attributes"]})
    cats = sorted({q["category"] for q in items})
    print(f"| {c} | {len(items)} | {', '.join(attrs)} | {', '.join(cats)} |")

print()
top = defaultdict(Counter)
for q in qs:
    t = q["category"].split(".")[0]
    top[t]["n"] += 1
    top[t][q["split"]] += 1
    top[t]["unans"] += not q["answerable"]
print("| category | n | unanswerable | dev | test |")
print("|---|---|---|---|---|")
for t, c in sorted(top.items(), key=lambda kv: -kv[1]["n"]):
    print(f"| {t} | {c['n']} | {c['unans']} | {c['dev']} | {c['test']} |")

print()
ks = Counter((q["kind"], q["split"]) for q in qs)
print("| kind | dev | test | total |")
print("|---|---|---|---|")
for k in ["lookup", "enumeration", "multi_evidence", "cross_reference", "unanswerable"]:
    print(f"| {k} | {ks[(k, 'dev')]} | {ks[(k, 'test')]} | {ks[(k, 'dev')] + ks[(k, 'test')]} |")

print()
print("modality:", dict(Counter(m for q in qs for m in q["modality"])))
print("complete_list:", sum(q["answer"]["complete_list"] for q in qs),
      "| with forbidden:", sum(bool(q["answer"]["forbidden"]) for q in qs),
      "| forbidden entries:", sum(len(q["answer"]["forbidden"]) for q in qs),
      "| alt evidence sets (>1):", sum(len(q["evidence"]) > 1 for q in qs),
      "| facts:", sum(len(q["answer"]["facts"]) for q in qs),
      "| critical facts:", sum(f["critical"] for q in qs for f in q["answer"]["facts"]),
      "| quotes:", sum(len(s["all_of"]) for q in qs for s in q["evidence"]))

# scoring fields (key terms; see gold_match.py)
facts = [f for q in qs for f in q["answer"]["facts"]]
forb = [f for q in qs for f in q["answer"]["forbidden"]]
groups = Counter(len(f.get("key_terms") or []) for f in facts)
alts = [len(g) for f in facts for g in f.get("key_terms") or []]
print("key_terms: facts with key_terms", sum(bool(f.get("key_terms")) for f in facts), "/", len(facts),
      "| AND-groups per fact", dict(sorted(groups.items())),
      "| mean OR-alternatives per group", round(sum(alts) / max(1, len(alts)), 1),
      "| facts with key_terms_note", sum("key_terms_note" in f for f in facts),
      "(critical", sum("key_terms_note" in f and f["critical"] for f in facts), ")",
      "| forbidden with key_terms", sum(bool(f.get("key_terms")) for f in forb), "/", len(forb))
notes = Counter(f["key_terms_note"].split(":")[0] for f in facts if "key_terms_note" in f)
print("key_terms_note kinds:", dict(notes.most_common()))
