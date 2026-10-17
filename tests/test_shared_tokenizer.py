"""Self-test for tokenizer_cache.py and the shared opening path: `python tests/test_shared_tokenizer.py` (no pytest).

Public protocols only (output_public/); offline: OPENROUTER_OFFLINE=1 is forced, embeddings / reranking are stand-in
functions, nothing is sent anywhere. Without output_public/ the document checks print a note and are skipped.

Checks: one LockedTokenizer per name (created once even from many threads, the local-files-first load); its token ids
equal a plain Hugging Face tokenizer's (single / batch, with and without special tokens); 8 threads x 200 mixed calls
on one tokenizer lose nothing; attribute parity (parents.py picks its counting path with hasattr); a loaded
sections.json is the section index of the document, a missing one is rebuilt silently, a damaged one is rebuilt with a
warning; the search builder of a ProtocolDocument reuses its document, serializer, tokenizer, section index and tables
(and still checks the JSON's SHA); two documents searched and token-counted from 6 threads give the sequential results
(one lock per document, as the service holds it around every tool call; both documents in use at the same time, so the
shared tokenizer serves both).
"""

import contextlib
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import parents  # noqa: E402
import protocol_api as api  # noqa: E402
import structure  # noqa: E402
from tokenizer_cache import LockedTokenizer, shared_tokenizer  # noqa: E402

PUBLIC = ["LZZT_CDISC_Pilot", "Lilly_NCT03421379", "Alexion_NCT04573309", "Bayer_NCT04304508",
          "PrECOG_NCT01797120", "Sanofi_NCT03637764"]
TWO = ["LZZT_CDISC_Pilot", "Lilly_NCT03421379"]
QUERIES = ["What is the planned number of participants?", "What is the washout period?",
           "Which concomitant medications are prohibited?"]
SAMPLES = ["", "Hello", "The planned number of participants is 120.", "µg/mL ≥ 18 years; HbA1c < 7.5 % (n=3)",
           "Inclusion criterion 4: a reliable caregiver who is in frequent contact with the subject.\n\n" * 12,
           "Tab\tseparated | table | row\n| a | b |\n| 1 | 2 |"]
CASES = []
_DOCS: dict[str, api.ProtocolDocument] = {}
_PLAIN: dict = {}


def case(fn):
    CASES.append(fn)
    return fn


def fakes(dim: int):
    """Deterministic stand-ins for the query embedding and the reranker (as in the other offline checks)."""
    def embed_fn(text):
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        return np.random.default_rng(seed).standard_normal(dim).astype(np.float32)

    def rerank_fn(query, chunks, model):
        q = set(query.lower().split())
        scores = [len(q & set((c.get("embed_text") or "").lower().split())) / (len(q) or 1) for c in chunks]
        return sorted(enumerate(scores), key=lambda x: (-x[1], x[0]))

    return embed_fn, rerank_fn


def open_doc(name: str, folder: Path | None = None) -> api.ProtocolDocument:
    out = folder or ROOT / "output_public" / name
    dim = np.load(ROOT / "output_public" / name / "chunks" / "embeddings.npy", mmap_mode="r").shape[1]
    embed_fn, rerank_fn = fakes(dim)
    return api.ProtocolDocument.open(out, embed_fn=embed_fn, rerank_fn=rerank_fn)


def doc(name: str) -> api.ProtocolDocument:
    if name not in _DOCS:
        _DOCS[name] = open_doc(name)
    return _DOCS[name]


def have_public() -> bool:
    if all((ROOT / "output_public" / n / "chunks" / "stats.json").is_file() for n in PUBLIC):
        return True
    print("   (skipped: output_public/ is not complete)")
    return False


def plain_tokenizer():
    """A plain Hugging Face tokenizer, loaded the way tokenizer_cache loads it (not through it); None when it is not
    cached locally."""
    from transformers import AutoTokenizer

    if "tok" not in _PLAIN:
        try:
            _PLAIN["tok"] = AutoTokenizer.from_pretrained(api.TOKENIZER, local_files_only=True)
        except Exception:  # noqa: BLE001
            print(f"   (skipped: tokenizer {api.TOKENIZER} is not in the local Hugging Face cache)")
            _PLAIN["tok"] = None
    return _PLAIN["tok"]


def raises(exc, fn, *a, **kw) -> Exception:
    try:
        fn(*a, **kw)
    except exc as e:  # noqa: PERF203
        return e
    raise AssertionError(f"{getattr(fn, '__name__', fn)} did not raise {exc.__name__}")


class Stub:
    """A tokenizer stand-in that records how many calls run at once."""

    def __init__(self):
        self.active = 0
        self.peak = 0
        self.bump = threading.Lock()
        self.vocab_size = 7

    def _work(self):
        with self.bump:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.002)
        with self.bump:
            self.active -= 1

    def __call__(self, text, **kw):
        self._work()
        return {"input_ids": [len(text)]}

    def encode(self, text, **kw):
        self._work()
        return [len(text)]


# =========================================================================== the cache

@case
def test_one_instance_per_name_and_local_first_load():
    from transformers import AutoTokenizer

    calls = []
    original = AutoTokenizer.__dict__.get("from_pretrained")
    inner = AutoTokenizer.from_pretrained

    def stand_in(name, *args, **kwargs):
        calls.append((name, bool(kwargs.get("local_files_only"))))
        time.sleep(0.05)  # long enough for every thread to be waiting inside shared_tokenizer
        if kwargs.get("local_files_only") and name.endswith("-offline-miss"):
            raise OSError("not cached")
        return Stub()

    AutoTokenizer.from_pretrained = stand_in
    try:
        for name in ("test/stub-tokenizer-a", "test/stub-tokenizer-b-offline-miss"):
            found = []
            start = threading.Barrier(8)

            def grab(_=None, name=name, found=found, start=start):
                start.wait()
                found.append(shared_tokenizer(name))

            with ThreadPoolExecutor(8) as pool:
                list(pool.map(grab, range(8)))
            assert len(found) == 8 and all(t is found[0] for t in found) and isinstance(found[0], LockedTokenizer)
            assert shared_tokenizer(name) is found[0]
            assert [c for c in calls if c[0] == name] == (
                [(name, True)] if "miss" not in name else [(name, True), (name, False)]), calls
        assert shared_tokenizer("test/stub-tokenizer-a") is not shared_tokenizer("test/stub-tokenizer-b-offline-miss")
        assert logging.getLogger("transformers").level == logging.ERROR
    finally:
        if original is not None:
            AutoTokenizer.from_pretrained = original
        else:
            del AutoTokenizer.from_pretrained
    assert AutoTokenizer.from_pretrained == inner


@case
def test_locked_calls_never_overlap():
    stub = Stub()
    locked = LockedTokenizer(stub)
    assert locked.vocab_size == 7 and not hasattr(locked, "no_such_attribute")
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda i: (locked("x" * i)["input_ids"], locked.encode("y" * i)), range(40)))
    assert results == [([i], [i]) for i in range(40)]
    assert stub.peak == 1, f"{stub.peak} calls ran at once"


@case
def test_same_object_ids_equal_plain_tokenizer():
    plain = plain_tokenizer()
    if plain is None:
        return
    tok = shared_tokenizer(api.TOKENIZER)
    assert tok is shared_tokenizer(api.TOKENIZER) and isinstance(tok, LockedTokenizer)
    for flag in (True, False):
        for payload in [*SAMPLES, SAMPLES, SAMPLES[1:3], SAMPLES[:1]]:  # single strings, then batches
            assert (tok(payload, add_special_tokens=flag)["input_ids"]
                    == plain(payload, add_special_tokens=flag)["input_ids"]), (flag, payload)
    assert tok(SAMPLES[2])["input_ids"] == plain(SAMPLES[2])["input_ids"]  # default: special tokens as the HF default
    assert tok.encode(SAMPLES[2]) == plain.encode(SAMPLES[2])
    assert tok.decode(tok.encode(SAMPLES[2])) == plain.decode(plain.encode(SAMPLES[2]))
    assert tok.convert_tokens_to_ids(["the"]) == plain.convert_tokens_to_ids(["the"])


@case
def test_threads_on_one_tokenizer():
    plain = plain_tokenizer()
    if plain is None:
        return
    tok = shared_tokenizer(api.TOKENIZER)
    specs = []  # single and batch input; truncation / padding change the tokenizer's state while a call runs
    for i, s in enumerate(SAMPLES):
        specs.append((s, False, {}))
        specs.append((SAMPLES[i:] + SAMPLES[:i], False, {}))
        specs.append((SAMPLES[: i + 1], True, {}))
        specs.append((s, True, {}))
        specs.append((s, False, {"truncation": True, "max_length": 16}))
        specs.append((SAMPLES[i:] + SAMPLES[:i], False, {"padding": True}))
    expected = [plain(p, add_special_tokens=flag, **kw)["input_ids"] for p, flag, kw in specs]
    start = threading.Barrier(8)

    def work(t: int) -> int:
        start.wait()
        done = 0
        for j in range(200):
            n = (t * 7 + j * 5) % len(specs)
            payload, flag, kw = specs[n]
            got = tok(payload, add_special_tokens=flag, **kw)["input_ids"]
            assert got == expected[n], f"thread {t} call {j}: ids differ"
            done += 1
        return done

    with ThreadPoolExecutor(8) as pool:
        assert list(pool.map(work, range(8))) == [200] * 8


@case
def test_hasattr_parity_and_counting_path():
    plain = plain_tokenizer()
    if plain is None:
        return
    tok = shared_tokenizer(api.TOKENIZER)
    for name in ("count_tokens", "encode", "decode", "__call__", "convert_tokens_to_ids", "get_tokenizer",
                 "vocab_size", "no_such_attribute"):
        assert hasattr(tok, name) == hasattr(plain, name), name
    assert not hasattr(tok, "count_tokens") and hasattr(tok, "convert_tokens_to_ids") and callable(tok)
    a, b = parents.TokenCounter(tok), parents.TokenCounter(plain)  # the HF path of parents.py, same numbers
    assert [a.count(s) for s in SAMPLES] == [b.count(s) for s in SAMPLES]
    assert a.many(SAMPLES) == b.many(SAMPLES)


# =========================================================================== the section index

@case
def test_index_of_open_documents_equals_a_rebuild():
    if not have_public():
        return
    for name in PUBLIC:
        d = doc(name)
        assert d.index.to_json() == parents.section_index(d.doc).to_json(), name
        assert not any("sections.json" in w for w in d.warnings), (name, d.warnings)
        assert d.index.to_json() == json.loads((d.chunks_dir / "sections.json").read_text(encoding="utf-8")), name


def chunks_copy(name: str, tmp: Path) -> Path:
    """The files ProtocolDocument.open needs, copied to tmp (output_public/ itself is never modified)."""
    src = ROOT / "output_public" / name
    (tmp / "chunks").mkdir()
    shutil.copy(src / "protocol.json", tmp / "protocol.json")
    for f in ("stats.json", "chunks.jsonl", "tables.jsonl", "glossary.json", "abbreviations.json", "sections.json"):
        if (src / "chunks" / f).is_file():
            shutil.copy(src / "chunks" / f, tmp / "chunks" / f)
    return tmp


@case
def test_sections_json_loaded_absent_and_damaged():
    if not have_public():
        return
    name = TWO[0]
    stored = (ROOT / "output_public" / name / "chunks" / "sections.json").read_bytes()
    broken = ["{", "[]", "[1, 2", '{"hello": 1}', '{"version": 1, "sections": [{"bogus": 1}]}',
              '{"version": 1}', '{"source": "fallback", "sections": [{"bogus": 1}]}']
    note = "sections.json could not be loaded; the section index was rebuilt from the Docling JSON"
    with tempfile.TemporaryDirectory() as t:
        tmp = chunks_copy(name, Path(t))
        sj = tmp / "chunks" / "sections.json"
        original = parents.section_index

        def refuse(_doc):
            raise AssertionError("the section index was rebuilt although sections.json is fine")

        parents.section_index = refuse  # a good sections.json is loaded, never rebuilt
        try:
            d = open_doc(name, tmp)
        finally:
            parents.section_index = original
        rebuilt = d.index.to_json()
        assert d.warnings == [] and rebuilt == original(d.doc).to_json() and isinstance(d.index, structure.SectionIndex)

        sj.unlink()  # absent: rebuilt, no warning
        d = open_doc(name, tmp)
        assert d.warnings == [] and d.index.to_json() == rebuilt

        for text in broken:  # unreadable / unknown format / from_json raises: rebuilt, with the warning
            sj.write_text(text, encoding="utf-8")
            d = open_doc(name, tmp)
            assert d.warnings == [note], (text, d.warnings)
            assert d.index.to_json() == rebuilt, text
        sj.write_bytes(b"\xff\xfe\x00 not utf-8")
        d = open_doc(name, tmp)
        assert d.warnings == [note] and d.index.to_json() == rebuilt
        assert d.document_info()["warnings"] == [note]

        fallback = parents.build_fallback_index(d.doc)  # the fallback format is read back as a fallback index
        sj.write_text(json.dumps(fallback.to_json(), ensure_ascii=False), encoding="utf-8")
        d = open_doc(name, tmp)
        assert isinstance(d.index, parents.FallbackSectionIndex) and d.warnings == []
        assert d.index.to_json() == fallback.to_json()
    assert (ROOT / "output_public" / name / "chunks" / "sections.json").read_bytes() == stored  # only the copy changed


# =========================================================================== the search builder

@case
def test_search_builder_reuses_the_document():
    if not have_public() or plain_tokenizer() is None:
        return
    for name in PUBLIC:
        d = doc(name)
        b = d._search_builder()
        assert b.doc is d.doc and b.serializer is d.serializer and b.tok is d.tokenizer and b.tables is d.tables, name
        assert b.tok is shared_tokenizer(api.TOKENIZER) and b.store.index is d.index, name
        assert b.mode == "parents" and b.budget == 12000
        assert b.with_mode("chunks").doc is d.doc and b.with_mode("chunks", budget=5000).store.index is d.index
        assert b.chain_of == {ref: t["chain"] for ref, t in d.tables.items() if t.get("chain")}


@case
def test_builder_defaults_and_sha_check():
    if not have_public() or plain_tokenizer() is None:
        return
    from retrieve import ContextBuilder, Retriever, production_builder

    name = TWO[0]
    d = doc(name)
    r = Retriever(d.chunks_dir / "chunks.jsonl")
    own = ContextBuilder(r, d.doc_path)  # nothing given: loads its own document, shares the cached tokenizer
    assert own.doc is not d.doc and own.tok is shared_tokenizer(api.TOKENIZER)
    assert own.tables == d.tables and own.store.index.to_json() == d.index.to_json()
    assert own.store.index is not d.index
    some = ContextBuilder(r, d.doc_path, tok=d.tokenizer, index=d.index)  # each keyword replaces only its own piece
    assert some.doc is not d.doc and some.tok is d.tokenizer and some.store.index is d.index
    assert production_builder(r, d.doc_path, doc=d.doc, index=d.index).store.index is d.index
    with tempfile.TemporaryDirectory() as t:
        other = Path(t) / "protocol.json"
        other.write_bytes(d.doc_path.read_bytes() + b" ")  # not the JSON the chunks were made from
        for given in ({}, {"doc": d.doc, "serializer": d.serializer, "tok": d.tokenizer, "index": d.index,
                           "tables": d.tables}):
            e = raises(RuntimeError, ContextBuilder, r, other, **given)
            assert "is not the JSON these chunks were made from" in str(e)


# =========================================================================== threads over documents

class DocLocks:
    """One lock per document, held around every call on it, as service.app_service does (a ProtocolDocument is not
    thread-safe); records how many documents were in use at once."""

    def __init__(self, names):
        self.locks = {n: threading.Lock() for n in names}
        self.bump = threading.Lock()
        self.active = 0
        self.peak = 0

    @contextlib.contextmanager
    def use(self, name: str):
        with self.locks[name]:
            with self.bump:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                yield
            finally:
                with self.bump:
                    self.active -= 1


def run_tasks(docs: dict[str, api.ProtocolDocument], tasks: list[tuple], locks: DocLocks | None = None) -> dict:
    out = {}
    for task in tasks:
        name, kind, arg = task
        d = docs[name]
        with locks.use(name) if locks else contextlib.nullcontext():
            if kind == "search":
                out[task] = d.search(arg)
            elif kind == "count":
                out[task] = d.count_tokens(arg)
            else:
                out[task] = d.tokenizer(list(arg), add_special_tokens=False)["input_ids"]
    return out


@case
def test_two_documents_searched_and_counted_from_threads():
    if not have_public() or plain_tokenizer() is None:
        return
    tasks = []
    for name in TWO:
        tasks += [(name, "search", q) for q in QUERIES]
        tasks += [(name, "count", s) for s in SAMPLES[2:]]
        tasks += [(name, "batch", tuple(SAMPLES))]
    sequential = run_tasks({n: open_doc(n) for n in TWO}, tasks)
    fresh = {n: open_doc(n) for n in TWO}  # not searched yet: the first calls also build the search builders
    locks = DocLocks(TWO)  # each document used by one thread at a time; the tokenizer shared by all of them
    start = threading.Barrier(6)

    def work(t: int) -> dict:
        start.wait()
        k = t * len(tasks) // 6  # threads 0-2 start on the first document, 3-5 on the second
        return run_tasks(fresh, tasks[k:] + tasks[:k], locks)

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(work, range(6)))
    for t, got in enumerate(results):
        assert got == sequential, f"thread {t}: " + str([k for k in sequential if got.get(k) != sequential[k]][:3])
    assert locks.peak == 2, f"{locks.peak} document(s) in use at once"  # both documents at the same time
    assert fresh[TWO[0]].tokenizer is fresh[TWO[1]].tokenizer is shared_tokenizer(api.TOKENIZER)


def main() -> int:
    failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(CASES) - failed}/{len(CASES)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
