"""Show the answer, facts and deterministic verdicts for chosen MAIN questions (local, cache only)."""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
import eval_v1 as ev  # noqa: E402

ids = sys.argv[1:]
gold = {q["id"]: q for q in json.loads((ROOT / "reports/eval/gold/MAIN.json").read_text(encoding="utf-8"))}
opts = argparse.Namespace(allow_api=False, s0_only=False)
chunks = ROOT / "output/chunks/chunks.jsonl"
env = ev.default_env("MAIN", chunks, opts)
env.answerer = ev.CachedAnswerer(chunks.parent / "answer_cache.json")
env.pipeline.prepare([gold[i] for i in ids])
for qid in ids:
    q = gold[qid]
    ret = env.pipeline.retrieve(q)
    ctx = env.assembler.build(q, ret, 5)
    ans = env.answerer.get(q["question"], ctx.text) or "<no cached answer>"
    print(f"\n==== {qid} [{q['kind']}] {q['question']}")
    for f in ev.facts_of(q):
        num = ""
        if f.get("value") not in (None, ""):
            num = f" | numeric_exact={ev.numeric_exact(f, ev.prep(ans))} (value={f.get('value')} unit={f.get('unit')} cmp={f.get('comparator')})"
        print(f"  [{'Y' if ev.fact_found(f, ev.prep(ans)) else '-'}] ctx={'Y' if ev.fact_found(f, ev.prep(ctx.text)) else '-'} {f['text'][:90]} :: {f.get('key_terms')}{num}")
    print("  ANSWER:", " ".join(ans.split())[:1200])
