"""One Hugging Face tokenizer per name and process, safe to use from several threads.

Loading the Qwen3 tokenizer takes 0.5-1.5 s, and every ProtocolDocument and context builder used to load its own.
shared_tokenizer(name) loads it once; the instance is a LockedTokenizer because a Hugging Face fast tokenizer
raises "Already borrowed" when one object is used from several threads at once (the service runs tool calls in
threads). Counting only: nothing here depends on the document.
"""

from __future__ import annotations

import functools
import logging
import threading
from typing import Any

_LOAD_LOCK = threading.Lock()
_TOKENIZERS: dict[str, "LockedTokenizer"] = {}


class LockedTokenizer:
    """A tokenizer whose calls run one at a time. tok(...) and every other callable attribute (encode, decode,
    convert_tokens_to_ids, ...) take the same per-instance lock; plain attributes (vocab_size, ...) are passed
    through, so hasattr / callable / getattr behave as on the wrapped tokenizer (parents.TokenCounter picks its
    counting path that way)."""

    def __init__(self, tokenizer: Any):
        self._tok = tokenizer
        self._lock = threading.Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return self._tok(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        tok = self.__dict__.get("_tok")  # not set yet while the object is being copied or unpickled
        if tok is None:
            raise AttributeError(name)
        attr = getattr(tok, name)
        if not callable(attr):
            return attr
        lock = self._lock

        @functools.wraps(attr)
        def locked(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return attr(*args, **kwargs)

        return locked


def _load(name: str) -> Any:
    from transformers import AutoTokenizer

    logging.getLogger("transformers").setLevel(logging.ERROR)
    try:
        return AutoTokenizer.from_pretrained(name, local_files_only=True)
    except Exception:  # noqa: BLE001 - not cached locally: allow the download
        return AutoTokenizer.from_pretrained(name)


def shared_tokenizer(name: str) -> LockedTokenizer:
    """The process-wide tokenizer called `name` (a Hugging Face id), loaded on first use: from the local cache
    when it is there, else from the Hub."""
    tok = _TOKENIZERS.get(name)
    if tok is None:
        with _LOAD_LOCK:  # two threads asking for the same new name load it once
            tok = _TOKENIZERS.get(name)
            if tok is None:
                tok = _TOKENIZERS[name] = LockedTokenizer(_load(name))
    return tok
