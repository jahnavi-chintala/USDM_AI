"""Retrieval over the protocol chunks: dense, BM25, hybrid (RRF) and reranked.

Dense embeddings (qwen3-embedding-8b) capture meaning but are weak on exact
identifiers: abbreviations, "Amendment 2", table and section numbers, drug
names. BM25 keyword search is strong exactly there but misses paraphrases
("left ventricular ejection fraction" vs "LVEF").

  hybrid  reciprocal rank fusion (RRF) of both rankings. Measured on this
          protocol it hurts top-5: when BM25 ranks a dense hit very low, the
          fused rank drops with it.
  rerank  (default) the two searches only collect candidates (top-N of each,
          so an answer either finds is kept); a cross-encoder reranker reads
          the question with each candidate and orders them.

Abbreviations in a question are expanded with their definition from the
protocol's own glossary (abbreviations.json, written by chunk_protocol.py)
before searching, and the matched definitions are returned so an answering
model can use them directly.

Reranking sends the question and the candidate chunks' text to OpenRouter
(no-data-collection / zero-data-retention routing); results are cached.

build_context() turns ranked hits into the answering model's context
(small-to-big): every retrieved table part becomes its whole table (from
tables.jsonl), every retrieved figure part the whole figure block, and the
text of mixed chunks is rebuilt from the Docling JSON. It is local; nothing is
sent anywhere.
"""

import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

RRF_K = 60  # standard RRF constant
RERANK_MODEL = os.environ.get("RERANK_MODEL") or "qwen/qwen3-reranker-8b"  # the service's RERANK_MODEL setting
RERANK_URL = "https://openrouter.ai/api/v1/rerank"
CANDIDATES_PER_RANKER = 20
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = set("""a an and are as at be by for from has have how in is it its of on or that the
this to was were what when where which who will with does do should must can""".split())


_IDENTIFIER = re.compile(r"\b([a-z]+)\s+(\d+(?:\.\d+)*[a-z]?)\b")


RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class OfflineRefused(RuntimeError):
    """OPENROUTER_OFFLINE is set and the operation would have called OpenRouter."""


def offline() -> bool:
    return os.environ.get("OPENROUTER_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def require_online() -> None:
    """Refuse API calls when OPENROUTER_OFFLINE=1 (cache-only runs: a cache miss is an error)."""
    if offline():
        raise OfflineRefused("OPENROUTER_OFFLINE=1: this run would call OpenRouter (cache miss); refusing")


def post_with_retry(url: str, body: dict, key: str, attempts: int = 6, timeout: int = 300):
    """POST to OpenRouter (the process's shared client), retrying rate limits and provider overloads with backoff."""
    import time

    import httpx

    from openrouter_http import post_json

    require_online()

    for attempt in range(1, attempts + 1):
        try:
            resp = post_json(url, body, key, timeout=timeout)
        except httpx.TransportError:
            if attempt == attempts:
                raise
        else:
            if resp.status_code == 200:
                return resp
            if resp.status_code not in RETRY_STATUS or attempt == attempts:
                raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:500]}")
        time.sleep(min(10 * 2 ** (attempt - 1), 120))  # 10, 20, 40, 80, 120 s


def tokenize(text: str) -> list[str]:
    """Words, plus one token per numbered reference ("amendment 2" -> amendment_2).

    Protocol questions are full of numbered references (Amendment 2, Table 3,
    Grade 4, Cycle 1, Section 6.5.1); as separate words "2" or "4" match almost
    everything, while the joined token is rare and therefore weighted highly.
    """
    text = text.lower()
    words = [t for t in _TOKEN.findall(text) if t not in _STOP and (len(t) > 1 or t.isdigit())]
    return words + [f"{w}_{n.rstrip('.')}" for w, n in _IDENTIFIER.findall(text) if w not in _STOP]


class BM25:
    def __init__(self, docs: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(tokenize(d)) for d in docs]
        self.lens = np.array([sum(tf.values()) for tf in self.tfs], dtype=float)
        self.avg = self.lens.mean()
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(len(self.tfs))
        for t in set(tokenize(query)):
            idf = self.idf.get(t)
            if idf is None:
                continue
            tf = np.array([d.get(t, 0) for d in self.tfs], dtype=float)
            out += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * self.lens / self.avg))
        return out


@dataclass
class Hit:
    chunk: dict
    score: float
    dense_rank: int
    bm25_rank: int


@dataclass
class Result:
    query: str
    expanded_query: str
    definitions: dict[str, str] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)


class Retriever:
    def __init__(self, chunks_path: str | Path = "output/chunks/chunks.jsonl"):
        chunks_path = Path(chunks_path)
        out = chunks_path.parent
        self.cache_dir = out
        self.meta = json.loads((out / "embeddings_meta.json").read_text(encoding="utf-8"))
        by_id = {c["chunk_id"]: c for c in map(json.loads, chunks_path.read_text(encoding="utf-8").splitlines())}
        if self.meta["chunk_ids"] != list(by_id):
            raise RuntimeError("embeddings are out of date with chunks.jsonl; run embed_chunks.py embed")
        self.chunks = [by_id[i] for i in self.meta["chunk_ids"]]
        # One index = one document: ContextBuilder resolves positional refs (#/texts/N)
        # against a single Docling JSON, so mixed sources would give wrong text silently.
        shas = {c.get("source_sha256") for c in self.chunks}
        if len(shas) > 1:
            raise RuntimeError(f"{chunks_path} mixes chunks of {len(shas)} source documents; "
                               "use one chunks folder per document")
        self.source_sha256 = shas.pop() if shas else None
        self.matrix = np.load(out / "embeddings.npy")
        self.bm25 = BM25([c["embed_text"] for c in self.chunks])
        # glossary.json (glossary.py: abbreviation list + inline definitions, with sources);
        # abbreviations.json is the older plain {abbr: definition} form.
        from glossary import Glossary

        path = next((p for p in (out / "glossary.json", out / "abbreviations.json") if p.exists()), None)
        self.glossary = Glossary.load(path) if path else None

    def find_abbreviations(self, text: str) -> dict[str, str]:
        """Glossary abbreviations used in a text, with their definitions."""
        return self.glossary.find_in_text(text) if self.glossary else {}

    def definition_source(self, abbr: str) -> str:
        entry = self.glossary.entries.get(abbr) if self.glossary else None
        return entry.source if entry else ""

    def expand(self, query: str) -> tuple[str, dict[str, str]]:
        """Append glossary definitions of abbreviations used in the query."""
        found = self.find_abbreviations(query)
        if not found:
            return query, {}
        return f"{query} ({'; '.join(f'{k}: {v}' for k, v in found.items())})", found

    def query_text(self, expanded: str) -> str:
        """The text to embed for a query (Qwen3-Embedding is instruction-aware)."""
        return f"Instruct: {self.meta['query_instruction']}\nQuery: {expanded}"

    def search(self, query: str, query_vec: np.ndarray, k: int = 5, mode: str = "rerank",
               rerank_model: str = RERANK_MODEL) -> Result:
        """Rank chunks; query_vec is the embedding of query_text(expand(query)[0])."""
        expanded, defs = self.expand(query)
        dense = self.matrix @ query_vec
        dense_rank = np.empty(len(dense), dtype=int)
        dense_rank[np.argsort(-dense)] = np.arange(1, len(dense) + 1)
        bm = self.bm25.scores(expanded)
        bm_rank = np.empty(len(bm), dtype=int)
        bm_rank[np.argsort(-bm)] = np.arange(1, len(bm) + 1)
        fused = 1 / (RRF_K + dense_rank) + 1 / (RRF_K + bm_rank)
        if mode == "dense":
            scores, order = dense, np.argsort(-dense)
        elif mode == "bm25":
            scores, order = bm, np.argsort(-bm)
        elif mode == "hybrid":
            scores, order = fused, np.argsort(-fused)
        else:
            cand = [int(i) for i in np.argsort(-fused)
                    if dense_rank[i] <= CANDIDATES_PER_RANKER or bm_rank[i] <= CANDIDATES_PER_RANKER]
            ranked = self.rerank(expanded, [self.chunks[i] for i in cand], rerank_model)
            scores = np.full(len(self.chunks), -1.0)
            for pos, score in ranked:
                scores[cand[pos]] = score
            order = [cand[pos] for pos, _ in ranked]
        return Result(query, expanded, defs, [
            Hit(self.chunks[i], float(scores[i]), int(dense_rank[i]), int(bm_rank[i])) for i in order[:k]])

    def rerank(self, query: str, chunks: list[dict], model: str) -> list[tuple[int, float]]:
        """(candidate position, relevance) best first, via OpenRouter; cached on disk."""
        from figure_vlm import _api_key

        key = hashlib.sha256(json.dumps([model, query, [c["content_hash"] for c in chunks]]).encode()).hexdigest()
        cache_file = Path(self.cache_dir) / "rerank_cache.json"
        cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
        if key not in cache:
            body = {"model": model, "query": query, "documents": [c["embed_text"] for c in chunks],
                    "top_n": len(chunks),
                    "provider": {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}}
            resp = post_with_retry(RERANK_URL, body, _api_key("OPENROUTER_API_KEY"))
            cache[key] = [[r["index"], r["relevance_score"]] for r in resp.json()["results"]]
            cache_file.write_text(json.dumps(cache), encoding="utf-8")
        return [(int(i), float(s)) for i, s in cache[key]]


# --------------------------------------------------------------------------- context

CONTEXT_BUDGET = 8000  # tokens of the answering model's context (blocks + definitions)


BLOCK_LABELS = {
    "text": "Text", "table": "Table (complete)",
    "table_rows": "Table (retrieved rows only; complete table exceeded the budget)",
    "figure": "Figure",
    # parent-aware mode (ContextBuilder(mode="parents"))
    "document": "Document identity (title page, headers/footers)",
    "section_full": "Section (complete, with subsections)",
    "parent": "Section (complete)",
    "merged": "Sections (complete)",
    "window": "Section part",
    "table_whole": "Table (complete)",
    "table_window": "Table (retrieved rows ±1 row; complete table exceeded the budget)",
    "neighbours": "Text (retrieved passage ±1 neighbouring passage; complete section exceeded the budget)",
    "child": "Text (retrieved passage only; complete section exceeded the budget)",
    "residual": "Text (retrieved passage; its sentences already shown above are left out)",
}


@dataclass
class Block:
    kind: str  # "text" | "table" | "table_rows" | "figure" | parent-mode kinds (BLOCK_LABELS)
    text: str
    section: str
    pages: list[int]
    chunk_ids: list[str]
    table_id: str | None = None
    tokens: int = 0
    table_ids: list[str] = field(default_factory=list)  # all fragments of a page-split chain
    note: str = ""  # parent mode: which retrieved passages the block holds
    parent_id: str | None = None

    def render(self, number: int) -> str:
        label = BLOCK_LABELS[self.kind]
        if not self.pages:
            pages = "p. ?"
        elif min(self.pages) == max(self.pages):
            pages = f"p. {self.pages[0]}"
        else:
            pages = f"pp. {min(self.pages)}-{max(self.pages)}"
        note = f" | {self.note}" if self.note else ""
        return f"[{number}] {label} | {self.section or 'Front matter'} | {pages}{note}\n{self.text}"


@dataclass
class Context:
    query: str
    blocks: list[Block]
    definitions: dict[str, str]
    tokens: int
    fallbacks: list[str] = field(default_factory=list)  # tables reduced to their retrieved rows
    dropped: list[str] = field(default_factory=list)  # blocks dropped to meet the budget
    inferred: set[str] = field(default_factory=set)  # definitions taken from running text
    # parent mode
    duplicates: list[str] = field(default_factory=list)  # near-duplicate blocks/hits left out
    expanded: list[str] = field(default_factory=list)  # sections included whole (small-to-big)
    parents: list[str] = field(default_factory=list)  # parents placed (any representation)

    def render(self) -> str:
        parts = [b.render(i) for i, b in enumerate(self.blocks, start=1)]
        if self.definitions:
            lines = [f"- {k}: {v}" + (" (inferred from the protocol text)" if k in self.inferred else "")
                     for k, v in sorted(self.definitions.items())]
            parts.append("Abbreviations used above (from the protocol's glossary):\n" + "\n".join(lines))
        return "\n\n".join(parts)


def source_json(chunks_dir: Path) -> Path:
    """The Docling JSON the chunks were made from, per the chunks' stats.json.

    stats.json stores the file name; the JSON sits next to the chunks folder
    (<out>/<name>.json with chunks in <out>/chunks/), or at that path itself.
    """
    stats = chunks_dir / "stats.json"
    if not stats.exists():
        raise FileNotFoundError(f"{stats} not found; pass doc_json explicitly")
    name = json.loads(stats.read_text(encoding="utf-8"))["document"]["source_json"]
    for path in (Path(name), chunks_dir.parent / name, chunks_dir / name):
        if path.is_file():
            return path
    raise FileNotFoundError(f"source JSON {name!r} of {chunks_dir} not found next to it")


IDENTITY_FIELDS = (("sponsor_protocol_id", "Sponsor protocol number"), ("nct", "ClinicalTrials.gov (NCT) number"),
                   ("eudract", "EudraCT number"), ("eu_ct", "EU CT number"), ("ind", "IND number"),
                   ("who_utn", "WHO Universal Trial Number"))
IDENTITY_REPORT_MISSING = {"sponsor_protocol_id", "nct", "eudract", "eu_ct", "ind"}


def identity_block(meta: dict, ids: dict) -> Block:
    """The "Document" block: title, sponsor protocol id, registry ids and version, each identifier with
    the verbatim text it was found in (page, layer) so a value can be traced and checked. Registry ids
    sit in page headers or on the cover page, which no chunk holds (headers/footers are furniture)."""
    lines = []
    if meta.get("title"):
        lines.append(f"Title: {' '.join(str(meta['title']).split())}")
    if meta.get("acronym"):
        lines.append(f"Acronym: {meta['acronym']}")
    sources = ids.get("sources") or {}
    pages = set()
    for key, label in IDENTITY_FIELDS:
        value = ids.get(key)
        if value:
            src = sources.get(key)
            if src:
                where = ", ".join(x for x in (f"p. {src['page']}" if src.get("page") else "",
                                              "header/footer" if src.get("layer") == "furniture" else
                                              "table" if src.get("layer") == "table" else "") if x)
                lines.append(f'{label}: {value} (source{" " + where if where else ""}: "{src["text"]}")')
                if src.get("page"):
                    pages.add(src["page"])
            else:
                lines.append(f"{label}: {value}")
        elif key in IDENTITY_REPORT_MISSING:
            lines.append(f"{label}: none found in the document text (body, tables, headers/footers)")
    version = ", ".join(str(v) for v in (meta.get("version_label"), meta.get("version_date")) if v)
    if version:
        lines.append(f"Version: {version}")
    return Block("document", "\n".join(lines), "Document identity", sorted(pages), [])


class ContextBuilder:
    """Small-to-big context assembly for answering (see build_context).

    The Docling JSON defaults to the chunks' own source (stats.json) and must
    have the source_sha256 stored in the chunks: refs are positional, so a
    different or regenerated JSON would silently return other text.

    mode="chunks" (default) is the original assembly (every retrieved table/figure part expanded whole,
    stitched or dropped to fit). mode="parents" assembles from the structural partition (parents.py):
      * a "Document" identity block first, always (title, sponsor id, NCT/EudraCT/EU CT/IND, version);
      * the top-k children grouped by parent (score = best child score + parent_bonus per extra child);
      * enumeration-like questions: when the top parents lie in one section whose whole subtree fits in
        the remaining budget, that section is included whole (complete lists);
      * greedy fill in score order: a text parent whole (the retrieved passages named in its header),
        else the retrieved chunk(s) +-1 neighbouring chunk of the same parent, else the chunk(s) alone; a
        table parent whole when <= table_whole_max tokens or >= 2 of its parts were retrieved (and it
        fits), else the retrieved rows +-1 row, else the chunk alone; abbreviation tables never expand;
      * near-duplicates (5-word-shingle containment >= dup_threshold with what is already placed) are
        reduced to their sentences/rows that are not repeats (containment < residual_threshold), or
        left out when nothing new remains; a synopsis parent is considered after the body parent that
        repeats it, so the body copy is the one shown whole;
      * definitions of abbreviations as in chunk mode; page-split table chains stay together.

    The keyword-only doc / serializer / tok / index / tables take what the caller already holds for this document
    (a ProtocolDocument's own objects; index as parents.section_index returns it, tables as {table_id: record} of
    tables.jsonl) instead of loading or building it again. The tokenizer, when not given, is the process-wide one
    of tokenizer_cache.

    Not thread-safe: use one lock per document, as service.app_service does (the document, its section index and the
    search builder share lazily built state).
    """

    _index = None  # the section index given to __init__; None: the parent store builds it from self.doc

    def __init__(self, retriever: Retriever, doc_json: str | Path | None = None,
                 tokenizer: str = "Qwen/Qwen3-Embedding-8B", budget: int = CONTEXT_BUDGET,
                 mode: str = "chunks", identity: bool = True, section_expand: bool = True, *,
                 doc=None, serializer=None, tok=None, index=None, tables: dict | None = None):
        from docling_core.types.doc import DoclingDocument

        from chunk_protocol import ProtocolSerializerProvider
        from tokenizer_cache import shared_tokenizer

        if mode not in ("chunks", "parents"):
            raise ValueError(f"mode must be 'chunks' or 'parents', not {mode!r}")
        self.retriever = retriever
        self.budget = budget
        self.mode = mode
        self.identity = identity  # parent mode: the Document block
        self.section_expand = section_expand  # parent mode: section-level small-to-big
        self.table_whole_max = 2000  # parent mode: a table parent this small is always offered whole
        self.dup_threshold = 0.5  # 5-word-shingle containment that makes a block a near-duplicate
        self.residual_threshold = 0.9  # ... and a sentence of a near-duplicate block a repeat (left out)
        self.parent_bonus = 0.02  # per extra retrieved child of the same parent
        self.parent_min, self.parent_max = 300, 1500  # partition limits (parents.py)
        self._store = None
        self._identity = None
        path = Path(doc_json) if doc_json is not None else source_json(retriever.cache_dir)
        sha = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
        if retriever.source_sha256 and sha != retriever.source_sha256:
            raise RuntimeError(f"{path} (sha {sha}) is not the JSON these chunks were made from "
                               f"(sha {retriever.source_sha256}); re-run chunk_protocol.py or pass the right file")
        # What the caller already holds (a ProtocolDocument: its document, serializer, tokenizer, section index and
        # tables) replaces the load or build; the SHA check above covers doc_json in every case.
        self.doc = doc if doc is not None else DoclingDocument.load_from_json(path)
        self.serializer = (serializer if serializer is not None
                           else ProtocolSerializerProvider().get_serializer(self.doc))
        self.tok = tok if tok is not None else shared_tokenizer(tokenizer)
        self._index = index
        if tables is None:
            tables_file = retriever.cache_dir / "tables.jsonl"
            tables = {t["table_id"]: t for t in map(json.loads, tables_file.read_text(encoding="utf-8").splitlines())}
        self.tables = tables
        # Page-split fragments that could not be merged (column layout differs per page) are
        # expanded together (table_merge.continuation_chains, stored per table by the chunker).
        self.chain_of = {ref: t["chain"] for ref, t in self.tables.items() if t.get("chain")}
        # The abbreviations table is never expanded: definitions are added separately.
        self.glossary_tables = {ref for c in retriever.chunks if c["chunk_type"] == "glossary"
                                for ref in c["doc_item_refs"] if ref.startswith("#/tables/")}

    def count(self, text: str) -> int:
        return len(self.tok(text)["input_ids"])

    def _resolve(self, ref: str):
        from docling_core.types.doc import RefItem

        return RefItem(cref=ref).resolve(self.doc)

    def _render_item(self, item) -> str:
        from docling_core.types.doc import ListItem

        if isinstance(item, ListItem):
            return f"{item.marker or '-'} {item.text}".strip()
        return item.text.strip()

    def _figure_block(self, ref: str, chunk: dict) -> Block:
        from chunk_protocol import figure_blocks, figure_parts, figure_title

        pic = self._resolve(ref)
        f = figure_parts(pic, self.doc)
        body = "\n\n".join([figure_title(pic, f)] + [f"{label}:\n{text}" for label, text in figure_blocks(f)])
        return Block("figure", body, " > ".join(chunk["headings"]), [p.page_no for p in pic.prov],
                     [chunk["chunk_id"]])

    def _table_block(self, ref: str, chunk: dict) -> Block:
        refs = [r for r in self.chain_of.get(ref, [ref]) if r in self.tables]
        texts = [self.tables[r]["markdown"] for r in refs]
        if len(refs) > 1:
            texts = [f"{t}\n\n(Continued on the next page; the columns are laid out differently there.)"
                     for t in texts[:-1]] + texts[-1:]
        pages = sorted({p for r in refs for p in self.tables[r]["pages"]})
        return Block("table", "\n\n".join(texts), " > ".join(self.tables[refs[0]]["headings"]), pages,
                     [chunk["chunk_id"]], table_id=refs[0], table_ids=refs)

    def _stitched(self, block: Block, around: int = 0) -> Block:
        """The table reduced to the rows its retrieved parts hold (header once, notes last); with
        around=n also the n rows before and after each of them."""
        from chunk_protocol import LONG_ROW_LABEL, ProtocolTableSerializer, short_label

        parts =["\n" + c["text"] for c in self.retriever.chunks if c["chunk_id"] in block.chunk_ids]

        def retrieved(row: str) -> bool:
            label, sep, _ = row.partition(": ")
            if label and len(label) <= LONG_ROW_LABEL:
                return any(f"\n{label}: " in p or f"\n{label} (cont.): " in p for p in parts)
            # A very long label is repeated shortened on continuation pieces (chunk_protocol._fit_rows).
            return any(row[:80] in p or (sep and f"\n{short_label(label)} (cont.): " in p) for p in parts)

        lines = []
        for ref in block.table_ids or [block.table_id]:  # each fragment of a chain, with its own columns
            table = self._resolve(ref)
            cols, rows = ProtocolTableSerializer.columns_and_rows(table, self.doc, self.serializer)
            hit = [retrieved(r) for r in rows]
            if around:
                near = {j for i, h in enumerate(hit) if h for j in range(i - around, i + around + 1)}
                hit = [i in near for i in range(len(rows))]
            kept = [r for r, h in zip(rows, hit) if h]
            if not kept and ref != block.table_id:
                continue
            notes = " ".join(r.resolve(self.doc).text for r in table.footnotes)
            lines += [table.caption_text(self.doc), ProtocolTableSerializer.rows_prefix("", cols).strip(),
                      *kept, f"Notes: {notes}" if notes else ""]
        return Block("table_rows", "\n".join(line for line in lines if line), block.section,
                     block.pages, block.chunk_ids, table_id=block.table_id, table_ids=block.table_ids)

    def build(self, result: Result, expand_tables: bool = True) -> Context:
        """Blocks in rank order; expand_tables=False stitches every table (for comparison).
        In parent mode see _build_parents (expand_tables is ignored there)."""
        if self.mode == "parents":
            return self._build_parents(result)
        blocks: list[Block] = []
        placed: dict[str, Block] = {}  # table/picture ref -> its block
        emitted: set[str] = set()  # text refs already in the context
        for hit in result.hits:
            c = hit.chunk
            refs = c["doc_item_refs"]
            special = [r for r in refs if (r.startswith("#/tables/") and r not in self.glossary_tables)
                       or r.startswith("#/pictures/")]
            if not special:  # plain text (or a glossary part): the chunk text as retrieved
                if not set(refs) <= emitted:
                    blocks.append(Block("text", c["text"], " > ".join(c["headings"]),
                                        list(c["pages"]), [c["chunk_id"]]))
                    emitted.update(refs)
                continue
            texts: list[str] = []

            def flush_text() -> None:
                if texts:
                    pages = sorted({p.page_no for r in texts for p in self._resolve(r).prov})
                    blocks.append(Block("text", "\n".join(self._render_item(self._resolve(r)) for r in texts),
                                        " > ".join(c["headings"]), pages, [c["chunk_id"]]))
                    texts.clear()

            for ref in refs:
                if ref in placed:  # another part of a table/figure already in the context
                    if c["chunk_id"] not in placed[ref].chunk_ids:
                        placed[ref].chunk_ids.append(c["chunk_id"])
                    continue
                if ref in special:
                    flush_text()
                    block = (self._figure_block(ref, c) if ref.startswith("#/pictures/")
                             else self._table_block(ref, c))
                    for r in block.table_ids or [ref]:
                        placed[r] = block
                    blocks.append(block)
                elif ref not in emitted:
                    texts.append(ref)
                    emitted.add(ref)
            flush_text()
        if not expand_tables:
            blocks = [self._stitched(b) if b.kind == "table" else b for b in blocks]
        return self._fit(result, blocks)

    def _fit(self, result: Result, blocks: list[Block]) -> Context:
        """Apply the token budget: stitch the largest tables first, then drop the lowest-ranked blocks."""
        fallbacks, dropped = [], []
        while True:
            for b in blocks:
                b.tokens = self.count(b.render(0))
            defs = self.retriever.find_abbreviations(" ".join([result.query, *(b.text for b in blocks)]))
            inferred = {k for k in defs if self.retriever.definition_source(k) == "inline"}
            def_tokens = self.count("\n".join(f"- {k}: {v}" for k, v in defs.items())) if defs else 0
            total = sum(b.tokens for b in blocks) + def_tokens
            if total <= self.budget or not blocks:
                return Context(result.query, blocks, defs, total, fallbacks, dropped, inferred)
            full_tables = [b for b in blocks if b.kind == "table"]
            if full_tables:
                big = max(full_tables, key=lambda b: b.tokens)
                blocks[blocks.index(big)] = self._stitched(big)
                fallbacks.append(big.table_id)
            else:
                gone = blocks.pop()
                dropped.append(f"{gone.kind}: {gone.section} ({', '.join(gone.chunk_ids)})")

    # ------------------------------------------------------------------ parent-aware mode
    def with_mode(self, mode: str = "parents", **opts) -> "ContextBuilder":
        """A copy of this builder in another mode / with other options (the Docling JSON, serializer, tokenizer,
        section index and tables are shared), e.g. builder.with_mode("parents", budget=12000)."""
        import copy

        if mode not in ("chunks", "parents"):
            raise ValueError(f"mode must be 'chunks' or 'parents', not {mode!r}")
        clone = copy.copy(self)
        clone.mode = mode
        for key, value in opts.items():
            if key.startswith("_") or not hasattr(clone, key):
                raise TypeError(f"unknown ContextBuilder option {key!r}")
            setattr(clone, key, value)
        if {"parent_min", "parent_max"} & set(opts):
            clone._store = None
        return clone

    @property
    def store(self):
        """The document's parents (parents.ParentStore), built on first use from the Docling JSON and the
        chunks, with the same code (and section index) as chunk_protocol's parents.jsonl."""
        if self._store is None:
            import logging

            from parents import ParentStore, section_index

            index = self._index if self._index is not None else section_index(self.doc)
            self._store = ParentStore(self.doc, index, self.retriever.chunks, self.tok,
                                      self.parent_min, self.parent_max, serializer=self.serializer)
            stale = sum(1 for c in self.retriever.chunks if c.get("parent_id")
                        and c["parent_id"] != self._store.parent_of_chunk.get(c["chunk_id"]))
            if stale:
                logging.getLogger("retrieve").warning(
                    "%d chunk records carry a parent_id other than the current partition (re-run chunk_protocol.py); "
                    "using the current partition", stale)
        return self._store

    def _identity_block(self) -> Block:
        import copy

        if self._identity is None:
            stats = self.retriever.cache_dir / "stats.json"
            meta = json.loads(stats.read_text(encoding="utf-8")).get("document", {}) if stats.exists() else {}
            ids = meta.get("identifiers")
            if ids is None:  # chunks written before identifiers were detected
                from chunk_protocol import registry_identifiers

                ids = registry_identifiers(self.doc, meta)
            self._identity = identity_block(meta, ids)
            self._identity.tokens = self.count(self._identity.render(0))
        return copy.copy(self._identity)

    def _definitions(self, query: str, blocks: list[Block]) -> tuple[dict[str, str], int]:
        defs = self.retriever.find_abbreviations(" ".join([query, *(b.text for b in blocks)]))
        return defs, (self.count("\n".join(f"- {k}: {v}" for k, v in defs.items())) if defs else 0)

    def _fits(self, query: str, blocks: list[Block], cand: Block) -> bool:
        cand.tokens = self.count(cand.render(0))
        used = sum(b.tokens for b in blocks) + cand.tokens
        if used > self.budget:
            return False
        return used + self._definitions(query, [*blocks, cand])[1] <= self.budget

    @staticmethod
    def _hit_note(chunks: list[dict]) -> str:
        """Which retrieved passages a whole parent holds: their "Rows:" line or first words."""
        snippets = []
        for c in chunks[:3]:
            text = c["text"]
            rows = next((ln[6:] for ln in text.splitlines() if ln.startswith("Rows: ")), None)
            words = (rows or " ".join(text.split())).split()
            s = " ".join(words[:12])
            snippets.append(f"«{s[:90]}{'…' if len(words) > 12 or len(s) > 90 else ''}»")
        more = f" +{len(chunks) - 3} more" if len(chunks) > 3 else ""
        return f"retrieved: {'; '.join(snippets)}{more}" if snippets else ""

    def _chunk_block(self, kind: str, chunks: list[dict], section: str, pid: str | None) -> Block:
        pages = sorted({p for c in chunks for p in c.get("pages") or []})
        return Block(kind, "\n\n".join(c["text"] for c in chunks), section, pages,
                     [c["chunk_id"] for c in chunks], parent_id=pid)

    def _parent_reps(self, pid: str | None, hits: list[Hit]):
        """(name, Block) for one parent in degradation order (preferred first)."""
        st = self.store
        pos = self._chunk_pos
        chunks = sorted((h.chunk for h in hits), key=lambda c: pos.get(c["chunk_id"], 0))
        p = st.by_id.get(pid) if pid else None
        if p is None:  # a chunk outside the partition: as retrieved
            yield "child", self._chunk_block("text", chunks, " > ".join(chunks[0].get("headings") or []), None)
            return
        note = self._hit_note(chunks)
        glossary = any(t in self.glossary_tables for t in p.table_ids)
        if p.kind == "table" and not glossary:
            if p.tokens <= self.table_whole_max or len(chunks) >= 2:
                text = p.text
                chain = [r for t in p.table_ids for r in self.chain_of.get(t, []) if r not in p.table_ids]
                if chain:  # page-split fragments in other parents travel with the table
                    text += "".join(f"\n\n(Continued on the next page; the columns are laid out differently there.)"
                                    f"\n\n{self.tables[r]['markdown']}" for r in dict.fromkeys(chain) if r in self.tables)
                yield "whole", Block("table_whole", text, p.breadcrumb, p.pages, [c["chunk_id"] for c in chunks],
                                     table_id=p.table_ids[0] if p.table_ids else None, table_ids=list(p.table_ids),
                                     note=note, parent_id=pid)
            touched = [r for c in chunks for r in c["doc_item_refs"] if r in p.table_ids]
            if touched:
                tid = touched[0]
                base = Block("table", "", p.breadcrumb, p.pages, [c["chunk_id"] for c in chunks], table_id=tid,
                             table_ids=[r for r in self.chain_of.get(tid, [tid]) if r in self.tables] or [tid])
                rows = self._stitched(base, around=1)
                lead = list(dict.fromkeys(  # text items of the retrieved chunks (a mixed chunk's lead-in)
                    self._render_item(self._resolve(r)) for c in chunks for r in c["doc_item_refs"]
                    if not r.startswith(("#/tables/", "#/pictures/"))))
                text = "\n".join([*lead, rows.text]) if lead else rows.text
                yield "rows±1", Block("table_window", text, p.breadcrumb, p.pages, rows.chunk_ids, table_id=tid,
                                      table_ids=rows.table_ids, parent_id=pid)
            yield "child", self._chunk_block("child", chunks, p.breadcrumb, pid)
            return
        if not glossary:
            kind = {"section": "parent", "merged": "merged"}.get(p.kind, "window")
            yield "whole", Block(kind, p.text, p.breadcrumb, p.pages, [c["chunk_id"] for c in chunks],
                                 table_ids=list(p.table_ids), note=note, parent_id=pid)
            mine = [i for i, c in enumerate(self.retriever.chunks) if st.parent_of_chunk.get(c["chunk_id"]) == pid]
            near = sorted({j for c in chunks for i in [pos.get(c["chunk_id"])] if i is not None
                           for j in (i - 1, i, i + 1) if j in mine})
            if len(near) > len(chunks):
                yield "±1", self._chunk_block("neighbours", [self.retriever.chunks[j] for j in near], p.breadcrumb, pid)
        yield "child", self._chunk_block("child", chunks, p.breadcrumb, pid)

    _SENTENCE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z0-9(\[•·\-])")

    def _residual(self, pid: str | None, hits: list[Hit], placed_sh: set, placed_norm: str) -> Block | None:
        """The retrieved chunks without the sentences the context already holds (5-word-shingle
        containment >= residual_threshold, or the whole short sentence present); None when nothing new.
        The bar is higher than dup_threshold on purpose: a summary copy often differs from the body in
        one cell or clause (measured: objectives tables of 3 public protocols), and that difference
        must survive."""
        from parents import containment, shingles

        pos = self._chunk_pos
        chunks = sorted((h.chunk for h in hits), key=lambda c: pos.get(c["chunk_id"], 0))
        lines = []
        for c in chunks:
            for line in c["text"].splitlines():
                keep = []
                for s in self._SENTENCE.split(line):
                    norm = " ".join(s.casefold().split())
                    if not norm:
                        continue
                    ssh = shingles(s)
                    if (ssh and containment(ssh, placed_sh) >= self.residual_threshold) or (not ssh and norm in placed_norm):
                        continue
                    keep.append(s.strip())
                if keep:
                    lines.append(" ".join(keep))
        text = "\n".join(lines)
        if not shingles(text):
            return None
        p = self.store.by_id.get(pid) if pid else None
        section = p.breadcrumb if p else " > ".join(chunks[0].get("headings") or [])
        return Block("residual", text, section, sorted({x for c in chunks for x in c.get("pages") or []}),
                     [c["chunk_id"] for c in chunks], parent_id=pid)

    def _expansion(self, query: str, hit_pids: list[str], blocks: list[Block]) -> tuple[str, Block, set] | None:
        """Section-level small-to-big: the smallest section holding the parents of the hits ranked 1-3
        (then 1-2) whose whole subtree fits in the remaining budget and adds to them; or, when the top
        parent holds two or more of the hits, the section of that parent. A single hit never expands."""
        st = self.store
        tried = []
        groups = [hit_pids[:3], hit_pids[:2]]
        if hit_pids and hit_pids.count(hit_pids[0]) >= 2:
            groups.append(hit_pids[:1])
        for group in groups:
            group = list(dict.fromkeys(p for p in group if p in st.by_id))
            if not group or (len(group) == 1 and hit_pids.count(group[0]) < 2):
                continue
            sid = st.lca([s for p in group for s in st.by_id[p].section_ids])
            if sid is None or sid in tried:
                continue
            tried.append(sid)
            if st.section_est(sid) > self.budget:
                continue
            info = st.section_block(sid)
            if set(info["parents"]) <= set(group):  # nothing beyond the top parents themselves
                continue
            block = Block("section_full", info["text"], info["breadcrumb"], info["pages"], [], note="",
                          parent_id=None)
            if self._fits(query, blocks, block):
                return sid, block, set(info["parents"])
        return None

    def _build_parents(self, result: Result) -> Context:
        from parents import containment, enumeration_like, shingles

        st = self.store
        if not hasattr(self, "_chunk_pos") or len(self._chunk_pos) != len(self.retriever.chunks):
            self._chunk_pos = {c["chunk_id"]: i for i, c in enumerate(self.retriever.chunks)}
        query = result.query
        blocks: list[Block] = [self._identity_block()] if self.identity else []
        if blocks and blocks[0].tokens > self.budget:
            blocks = []
        degraded, dropped, dups, expanded, placed = [], [], [], [], []

        # Group the hits by parent: best child score + a bonus per extra child; rank breaks ties.
        groups: dict[str, dict] = {}
        hit_pids: list[str] = []  # the parent of each hit, in rank order
        for rank, h in enumerate(result.hits):
            p = st.parent_of(h.chunk)
            key = p.parent_id if p is not None else f"chunk:{h.chunk['chunk_id']}"
            g = groups.setdefault(key, {"pid": p.parent_id if p else None, "hits": [], "best": rank,
                                        "score": h.score})
            g["hits"].append(h)
            g["score"] = max(g["score"], h.score)
            hit_pids.append(key)
        for g in groups.values():
            g["score"] += self.parent_bonus * (len(g["hits"]) - 1)
        order = sorted(groups, key=lambda k: (-groups[k]["score"], groups[k]["best"]))
        # Prefer the body: a synopsis parent whose retrieved text a body parent among the hits repeats
        # is considered right after that body parent (then usually left out as a duplicate).
        sh_cache: dict[str, set] = {}

        def sh(key: str, text: str) -> set:
            if key not in sh_cache:
                sh_cache[key] = shingles(text)
            return sh_cache[key]

        for key in list(order):
            g = groups[key]
            if g["pid"] is None or st.by_id[g["pid"]].role != "synopsis":
                continue
            hit_sh = [sh(h.chunk["chunk_id"], h.chunk["text"]) for h in g["hits"]]
            body = next((k for k in order if k != key and groups[k]["pid"]
                         and st.by_id[groups[k]["pid"]].role != "synopsis"
                         and all(containment(s, sh(groups[k]["pid"], st.by_id[groups[k]["pid"]].text))
                                 >= self.dup_threshold for s in hit_sh if s)), None)
            if body is not None and order.index(body) > order.index(key):
                order.remove(key)
                order.insert(order.index(body) + 1, key)

        covered: set[str] = set()
        placed_sh: set = set()
        placed_norm: list[str] = []

        def place(block: Block) -> None:
            blocks.append(block)
            placed_sh.update(shingles(block.text))
            placed_norm.append(" ".join(block.text.casefold().split()))

        if self.section_expand and enumeration_like(query):
            hit = self._expansion(query, hit_pids, blocks)
            if hit is not None:
                sid, block, inside = hit
                place(block)
                expanded.append(sid)
                covered |= inside
        for key in order:
            g = groups[key]
            pid = g["pid"]
            ids = ", ".join(h.chunk["chunk_id"] for h in g["hits"])
            if pid in covered:
                placed.append(key)
                continue
            hit_sh = [sh(h.chunk["chunk_id"], h.chunk["text"]) for h in g["hits"]]
            all_dup = bool(placed_sh) and any(hit_sh) and all(
                containment(s, placed_sh) >= self.dup_threshold for s in hit_sh if s)
            over_budget = False
            done = False
            for name, block in ([] if all_dup else self._parent_reps(pid, g["hits"])):
                if placed_sh and containment(shingles(block.text), placed_sh) >= self.dup_threshold:
                    continue  # mostly in the context already: try the smaller representation
                if self._fits(query, blocks, block):
                    place(block)
                    placed.append(key)
                    if over_budget:
                        degraded.append(f"{key}: {name}")
                    done = True
                    break
                over_budget = True
            if not done and not over_budget:
                # A near-duplicate (a synopsis copy of body text already placed): keep only its sentences
                # that are not in the context yet, so a summary's extra detail is not lost with the copy.
                rest = self._residual(pid, g["hits"], placed_sh, " ".join(placed_norm))
                if rest is None:
                    dups.append(f"{key} ({ids}): already in the context")
                    done = True
                elif self._fits(query, blocks, rest):
                    place(rest)
                    placed.append(key)
                    dups.append(f"{key} ({ids}): near-duplicate; only its new sentences kept")
                    done = True
                else:
                    over_budget = True
            if not done:
                dropped.append(f"{key} ({ids})")
        defs, def_tokens = self._definitions(query, blocks)
        inferred = {k for k in defs if self.retriever.definition_source(k) == "inline"}
        total = sum(b.tokens for b in blocks) + def_tokens
        return Context(query, blocks, defs, total, degraded, dropped, inferred, dups, expanded, placed)


def build_context(result: Result, builder: ContextBuilder, expand_tables: bool = True) -> Context:
    """Small-to-big context for the answering model from ranked hits."""
    return builder.build(result, expand_tables)


# --------------------------------------------------------------------------- production defaults
# Chosen on the 2026-10-07 A/B evaluation (reports/pipeline_audit_plan.md §7): parent-aware context,
# top-8 reranked children, 12K-token budget. Against the chunk-level builder (k=5, 8K) it raised answer
# fact recall on MAIN by +2.0 pts (95% CI +0.4..+4.1) and on the five public protocols by +7.8 pts
# (+3.4..+12.4), at about 1.8-2.5x the context tokens. The class default stays "chunks" so earlier
# evaluation runs remain reproducible; production callers use these.
PRODUCTION_K = 8
PRODUCTION_BUDGET = 12000


def production_builder(retriever: "Retriever", doc_json: str | Path | None = None, **shared) -> "ContextBuilder":
    """The context builder the answering / USDM agent path should use. `shared`: objects the caller already holds
    for this document (doc=, serializer=, tok=, index=, tables=; see ContextBuilder), used instead of loading them."""
    return ContextBuilder(retriever, doc_json, **shared).with_mode("parents", budget=PRODUCTION_BUDGET)
