"""Embed the RAG chunks with qwen/qwen3-embedding-8b via OpenRouter, and search them.

  embed   Embeds each chunk's embedding text (protocol prefix + heading path +
          chunk text, from chunk_protocol.py). Results are cached per chunk
          content hash, so a re-run only embeds chunks that changed.
  search  Embeds a question and prints the best chunks (retrieve.py; default
          rerank: dense + BM25 candidates ordered by qwen3-reranker-8b).

Qwen3-Embedding is instruction-aware: documents are embedded as-is, queries
with a one-line task instruction (as its model card recommends).

Data handling: `embed` sends the text of every chunk, i.e. the whole protocol,
to OpenRouter; `search` sends only the question. Requests ask OpenRouter to
route only to providers that neither collect nor retain data, without
fallbacks. The API key comes from OPENROUTER_API_KEY or .env and is never logged.
"""

import argparse
import json
import logging
import os
import time
from pathlib import Path

import numpy as np

from figure_vlm import _api_key
from openrouter_http import post_json

log = logging.getLogger("embed_chunks")

MODEL = os.environ.get("EMBED_MODEL") or "qwen/qwen3-embedding-8b"  # the service's EMBED_MODEL setting
URL = "https://openrouter.ai/api/v1/embeddings"
QUERY_INSTRUCTION = ("Given a question about a clinical trial protocol, retrieve the protocol "
                     "passages that answer it")


def embed_texts(texts: list[str], model: str = MODEL, batch: int = 32, progress=None) -> np.ndarray:
    from retrieve import require_online

    require_online()  # OPENROUTER_OFFLINE=1 turns any embedding call into an error
    key = _api_key("OPENROUTER_API_KEY")
    out = []
    n_batches = (len(texts) + batch - 1) // batch
    for i in range(0, len(texts), batch):
        body = {"model": model, "input": texts[i:i + batch],
                "provider": {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}}
        for attempt in range(1, 4):
            resp = post_json(URL, body, key, timeout=120)
            if resp.status_code == 200:
                break
            if resp.status_code in (429, 500, 502, 503) and attempt < 3:
                time.sleep(5 * attempt)
                continue
            raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:500]}")
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        out += [d["embedding"] for d in data]
        log.info("embedded %d/%d", min(i + batch, len(texts)), len(texts))
        if progress is not None:
            progress("embed", i // batch + 1, n_batches)
    vecs = np.asarray(out, dtype=np.float32)
    return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def embed_document(chunks_path: Path, model: str = MODEL, progress=None) -> dict[str, int]:
    """Embeddings for every chunk of chunks_path into its folder (embeddings.npy + embeddings_meta.json).

    Vectors are cached per chunk content hash in embedding_cache.<model>.json next to the chunks,
    so only new or changed chunks are sent. With OPENROUTER_OFFLINE set and chunks missing from
    the cache, retrieve.OfflineRefused is raised before anything is written.
    progress("embed", done, total) per request batch. Returns counts."""
    chunks = [json.loads(l) for l in Path(chunks_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    out = Path(chunks_path).parent
    cache_file = out / f"embedding_cache.{model.replace('/', '_')}.json"
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}

    todo = [c for c in chunks if c["content_hash"] not in cache]
    log.info("%d chunks, %d cached, %d to embed with %s", len(chunks), len(chunks) - len(todo), len(todo), model)
    if todo:
        vecs = embed_texts([c["embed_text"] for c in todo], model, progress=progress)
        for c, v in zip(todo, vecs):
            cache[c["content_hash"]] = v.tolist()
        cache_file.write_text(json.dumps(cache), encoding="utf-8")

    matrix = np.asarray([cache[c["content_hash"]] for c in chunks], dtype=np.float32)
    np.save(out / "embeddings.npy", matrix)
    (out / "embeddings_meta.json").write_text(json.dumps({
        "model": model, "dimensions": int(matrix.shape[1]), "normalized": True,
        "query_instruction": QUERY_INSTRUCTION,
        "chunk_ids": [c["chunk_id"] for c in chunks],
    }, indent=1), encoding="utf-8")
    log.info("Wrote %s (%d x %d)", out / "embeddings.npy", *matrix.shape)
    return {"chunks": len(chunks), "cached": len(chunks) - len(todo), "embedded": len(todo)}


def cmd_embed(args) -> None:
    embed_document(Path(args.chunks), args.model)


def cmd_search(args) -> None:
    from retrieve import Retriever

    retriever = Retriever(args.chunks)
    expanded, defs = retriever.expand(args.query)
    vec = embed_texts([retriever.query_text(expanded)], retriever.meta["model"])[0]
    res = retriever.search(args.query, vec, k=args.k, mode=args.mode)
    for abbr, meaning in res.definitions.items():
        print(f"Glossary: {abbr} = {meaning}")
    for rank, h in enumerate(res.hits, start=1):
        c = h.chunk
        print(f"{rank}. {h.score:.4f} (dense #{h.dense_rank}, bm25 #{h.bm25_rank})  {c['chunk_id']}  "
              f"p{c['page_start']}  {' > '.join(c['headings'][-2:])}\n   {c['text'][:300]!r}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chunks", default="output/chunks/chunks.jsonl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("embed")
    p.add_argument("--model", default=MODEL)
    p.set_defaults(func=cmd_embed)
    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--mode", choices=["rerank", "hybrid", "dense", "bm25"], default="rerank")
    p.set_defaults(func=cmd_search)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args.func(args)


if __name__ == "__main__":
    main()
