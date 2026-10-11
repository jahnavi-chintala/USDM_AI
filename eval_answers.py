"""Answer-level test: complete tables vs stitched table rows in the context.

For each question in reports/answer_eval.json, the same retrieved hits
(retrieve.py, rerank mode, top 5) are turned into two contexts:
  expanded  build_context(): every retrieved table part becomes the whole table
  stitched  each table reduced to the rows its retrieved parts hold
An LLM answers from each context alone, citing blocks. Each question lists the
facts a complete answer must contain (each fact: alternative phrasings); the
score is the share of facts present, in the answer and, separately, in the
context, so a context gap can be told apart from an answering error.

The questions favour whole-column / whole-table questions (expected to need the
complete table), row and footnote questions (should work both ways), and one
text question as a control. Expected facts for SoA columns were taken from the
table itself.

API use (OpenRouter, zero-data-retention routing): question embeddings and
reranking (cached), and the answering model, which receives the context, i.e.
protocol text. Answers are cached in output/chunks/answer_cache.json.
--dry-run stops before the answering model and reports context-level scores only.
"""

import argparse
import hashlib
import json
import logging
import statistics
from pathlib import Path

from eval_retrieval import cached_query_vectors
from figure_vlm import _api_key
from retrieve import ContextBuilder, Retriever, build_context, post_with_retry

ANSWER_MODEL = "anthropic/claude-sonnet-5.5"
SYSTEM = (
    "You answer questions about a clinical trial protocol using only the numbered context blocks "
    "provided. Be complete: when the question asks which items apply, list every applicable item. "
    "Cite the blocks you used like [2]. If the context does not contain the answer, say so; do not "
    "use outside knowledge."
)

log = logging.getLogger("eval_answers")


def fact_recall(text: str, facts: list[list[str]]) -> float:
    low = text.lower()
    return sum(any(alt.lower() in low for alt in fact) for fact in facts) / len(facts)


def answer(model: str, context: str, question: str, cache_file: Path) -> str:
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    key = hashlib.sha256(json.dumps([model, SYSTEM, context, question]).encode()).hexdigest()
    if key not in cache:
        body = {"model": model, "temperature": 0, "max_tokens": 4000,
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": f"Context:\n\n{context}\n\nQuestion: {question}"}],
                "provider": {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}}
        for attempt in range(1, 4):
            resp = post_with_retry("https://openrouter.ai/api/v1/chat/completions", body,
                                   _api_key("OPENROUTER_API_KEY"))
            choice = resp.json()["choices"][0]
            content = choice["message"].get("content")
            if isinstance(content, list):  # some providers return content parts
                content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content and content.strip():
                break
            # Empty answer: never cache it; retry, then report why (no content is logged).
            reason = (f"finish_reason={choice.get('finish_reason')!r}, "
                      f"refusal={bool(choice['message'].get('refusal'))}, provider={resp.json().get('provider')!r}")
            log.warning("empty answer (attempt %d): %s", attempt, reason)
        else:
            raise RuntimeError(f"model returned no answer text after 3 attempts ({reason})")
        cache[key] = content
        cache_file.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return cache[key]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", default="reports/answer_eval.json")
    parser.add_argument("--model", default=ANSWER_MODEL)
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true", help="contexts only; no answering model")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    retriever = Retriever()
    builder = ContextBuilder(retriever)
    cases = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    vecs = cached_query_vectors([retriever.query_text(retriever.expand(c["q"])[0]) for c in cases],
                                retriever.meta["model"], retriever.cache_dir / "query_embedding_cache.json")
    rows = []
    for case, vec in zip(cases, vecs):
        res = retriever.search(case["q"], vec, k=args.k, mode="rerank")
        row = {"id": case["id"], "type": case["type"]}
        for mode, expand in (("expanded", True), ("stitched", False)):
            ctx = build_context(res, builder, expand_tables=expand)
            text = ctx.render()
            row[f"{mode}_tokens"] = ctx.tokens
            row[f"{mode}_ctx"] = fact_recall(text, case["facts"])
            if not args.dry_run:
                ans = answer(args.model, text, case["q"], retriever.cache_dir / "answer_cache.json")
                row[f"{mode}_ans"] = fact_recall(ans, case["facts"])
        rows.append(row)

    show_ans = not args.dry_run
    print(f"\n{'question':14s} {'type':34s} {'ctx tokens exp/sti':>18s}  {'context facts':>14s}"
          + ("  answer facts exp/sti" if show_ans else ""))
    for r in rows:
        line = (f"{r['id']:14s} {r['type']:34s} {r['expanded_tokens']:8d}/{r['stitched_tokens']:<8d}"
                f"  {r['expanded_ctx']:5.0%}/{r['stitched_ctx']:<5.0%}")
        if show_ans:
            line += f"  {r['expanded_ans']:5.0%}/{r['stitched_ans']:<5.0%}"
        print(line)
    for mode in ("expanded", "stitched"):
        msg = (f"\n{mode:9s} mean context tokens {statistics.mean(r[f'{mode}_tokens'] for r in rows):6.0f} | "
               f"context fact recall {statistics.mean(r[f'{mode}_ctx'] for r in rows):.0%}")
        if show_ans:
            ans = [r[f"{mode}_ans"] for r in rows]
            msg += (f" | answer fact recall {statistics.mean(ans):.0%} | "
                    f"complete answers {sum(a == 1 for a in ans)}/{len(ans)}")
        print(msg, end="")
    print()


if __name__ == "__main__":
    main()
