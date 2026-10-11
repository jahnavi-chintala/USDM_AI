"""Measure retrieval quality of the protocol chunks on a fixed question set.

Each question in reports/retrieval_eval.json names what a correct chunk looks
like: a phrase it must contain ("contains") and/or the section it belongs to
("sections", a section-number prefix), optionally restricted to a "page"
(int or list). Answers are defined by content, not chunk ids, so the set
stays valid when chunking changes. Each document has its own question file
(--questions).

Compares modes (see retrieve.py): dense only (plain question), BM25 only,
hybrid (glossary expansion + dense + BM25, RRF) and rerank (dense + BM25
candidates ordered by a reranker; one mode per --rerank-model). Reports
hit@1/3/5 and MRR@10 per mode, the share of questions whose answer was among
the rerank candidates, and the misses of the last mode.

API use: question embeddings (cached in query_embedding_cache.json) and, for
rerank modes, the question plus candidate chunk texts (cached in
rerank_cache.json). --local runs BM25 only and makes no API call.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from retrieve import CANDIDATES_PER_RANKER, Retriever


def cached_query_vectors(texts: list[str], model: str, cache_file: Path) -> np.ndarray:
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    key = lambda t: hashlib.sha256(f"{model}|{t}".encode()).hexdigest()
    todo = list(dict.fromkeys(t for t in texts if key(t) not in cache))
    if todo:
        from embed_chunks import embed_texts

        for t, v in zip(todo, embed_texts(todo, model)):
            cache[key(t)] = v.tolist()
        cache_file.write_text(json.dumps(cache), encoding="utf-8")
    return np.asarray([cache[key(t)] for t in texts], dtype=np.float32)


def is_answer(chunk: dict, case: dict) -> bool:
    """A chunk answers when it has a "contains" phrase or lies in one of the "sections"
    (number prefix); an optional "page" (int or list) must then also be among its pages.
    A case with only "page" is answered by any chunk on that page."""
    pages = case.get("page")
    if pages is not None:
        pages = {pages} if isinstance(pages, int) else set(pages)
        if not pages & set(chunk.get("pages") or []):
            return False
        if "contains" not in case and "sections" not in case:
            return True
    text = chunk["text"].lower()
    if any(p.lower() in text for p in case.get("contains", [])):
        return True
    section = chunk.get("section_number") or ""
    return any(section == s or section.startswith(s + ".") for s in case.get("sections", []))


def first_answer_rank(retriever: Retriever, case: dict, vec: np.ndarray, mode: str) -> int | None:
    kind, _, model = mode.partition(":")
    kwargs = {"rerank_model": model} if kind == "rerank" else {}
    res = retriever.search(case["q"], vec, k=len(retriever.chunks), mode=kind, **kwargs)
    return next((r for r, h in enumerate(res.hits, start=1) if is_answer(h.chunk, case)), None)


def candidate_recall(retriever: Retriever, case: dict, vec: np.ndarray) -> bool:
    """Is an answer among the rerank candidates (top-N dense or top-N BM25)?"""
    res = retriever.search(case["q"], vec, k=len(retriever.chunks), mode="hybrid")
    return any(is_answer(h.chunk, case) for h in res.hits
               if h.dense_rank <= CANDIDATES_PER_RANKER or h.bm25_rank <= CANDIDATES_PER_RANKER)


def summary(ranks: list[int | None]) -> str:
    n = len(ranks)
    hit = lambda k: sum(1 for r in ranks if r and r <= k) / n
    mrr = sum(1 / r for r in ranks if r and r <= 10) / n
    return f"hit@1 {hit(1):4.0%} | hit@3 {hit(3):4.0%} | hit@5 {hit(5):4.0%} | MRR@10 {mrr:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", default="output/chunks/chunks.jsonl")
    parser.add_argument("--questions", default="reports/retrieval_eval.json")
    parser.add_argument("--local", action="store_true", help="BM25 only; no API calls")
    # Cohere/Voyage rerankers have no zero-data-retention endpoint on OpenRouter,
    # so retrieve.py's routing policy rejects them.
    parser.add_argument("--rerank-model", nargs="*", default=["qwen/qwen3-reranker-8b"])
    args = parser.parse_args()

    retriever = Retriever(args.chunks)
    cases = json.loads(Path(args.questions).read_text(encoding="utf-8"))
    for case in cases:  # every case must have at least one answer chunk
        if not any(is_answer(c, case) for c in retriever.chunks):
            raise SystemExit(f"no chunk answers: {case['q']}")

    zero = np.zeros(retriever.matrix.shape[1], dtype=np.float32)
    modes = {"bm25": [zero] * len(cases)}
    if not args.local:
        plain = [retriever.query_text(c["q"]) for c in cases]
        expanded = [retriever.query_text(retriever.expand(c["q"])[0]) for c in cases]
        vecs = cached_query_vectors(plain + expanded, retriever.meta["model"],
                                    retriever.cache_dir / "query_embedding_cache.json")
        plain_vecs, exp_vecs = list(vecs[:len(cases)]), list(vecs[len(cases):])
        modes = {"dense": plain_vecs, "bm25": modes["bm25"], "hybrid": exp_vecs}
        modes.update({f"rerank:{m}": exp_vecs for m in args.rerank_model})
        recall = sum(candidate_recall(retriever, c, v) for c, v in zip(cases, exp_vecs)) / len(cases)

    results = {}
    for mode, qvecs in modes.items():
        results[mode] = [first_answer_rank(retriever, c, v, mode) for c, v in zip(cases, qvecs)]
    last = list(modes)[-1]
    for i, (case, rank) in enumerate(zip(cases, results[last])):
        if rank is None or rank > 5:
            others = ", ".join(f"{m.split('/')[-1]} {results[m][i]}" for m in modes if m != last)
            print(f"MISS {last.split('/')[-1]} rank {rank} ({others}): {case['q']}")
    print(f"\n{len(cases)} questions, {len(retriever.chunks)} chunks")
    if not args.local:
        print(f"  answer among rerank candidates (top {CANDIDATES_PER_RANKER} dense + top "
              f"{CANDIDATES_PER_RANKER} BM25): {recall:.0%}")
    for mode in modes:
        print(f"  {mode.split('/')[-1]:22s}  {summary(results[mode])}")


if __name__ == "__main__":
    main()
