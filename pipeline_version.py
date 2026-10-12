"""Pipeline version and fingerprint: what a stored result was produced with.

PIPELINE_VERSION is a semver string, bumped whenever a change alters the outputs (major: output format or
identifiers change; minor: content changes, e.g. a new repair; patch: fixes that change a few documents).
pipeline_fingerprint() hashes everything else that decides the outputs: PIPELINE_VERSION, the effective
ProtocolSettings, the Docling package versions, the converter configuration and the model ids (figure
descriptions, embeddings, reranker, chunk tokenizer). Two results with the same fingerprint were made the
same way; a stored result whose fingerprint differs from the running one is stale.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from importlib.metadata import PackageNotFoundError, version

PIPELINE_VERSION = "2.0.0"  # 1.1.0: Phase 1 fixes (hyphen-split items, booktabs rows, quoted headings, ...)
#                             1.1.1: chunk figure image_path relative to the document folder (was absolute)
#                             1.1.2: protocol.json / .md / .html with LF and "/" image URIs on every platform
#                                    (Windows results now hash, and so get chunk ids, like the Linux container's)
#                             2.0.0: chunk ids of documents without protocol id + amendment derive from the
#                                    source PDF (protocol.json records it as origin), not from the JSON
#                                    (identifiers change: "doc-<pdf hash>-0000")

DEFAULT_FIGURE_MODEL = "openrouter:anthropic/claude-opus-5.5"  # convert_protocol.py --figure-model default
_PACKAGES = ("docling", "docling-core", "docling-parse", "docling-ibm-models")


def figure_model() -> str:
    """The figure-description model in effect: FIGURE_MODEL ("" turns descriptions off) or the default."""
    value = os.environ.get("FIGURE_MODEL")
    return DEFAULT_FIGURE_MODEL if value is None else value.strip()


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def model_ids() -> dict[str, str]:
    """The models whose output is stored or used to build stored results."""
    from embed_chunks import MODEL as EMBED_MODEL
    from protocol_api import TOKENIZER
    from retrieve import RERANK_MODEL

    return {"figure": figure_model(), "embedding": EMBED_MODEL, "reranker": RERANK_MODEL, "tokenizer": TOKENIZER}


def fingerprint_inputs(settings=None, models: dict | None = None) -> dict:
    """Everything pipeline_fingerprint() hashes, as a JSON-serialisable dict (for /version and logs).
    `models` overrides model_ids() entries (the service passes its configured models)."""
    from convert_protocol import CONVERTER_CONFIG, DEFAULT_LAYOUT
    from protocol_settings import ProtocolSettings

    settings = settings or ProtocolSettings()
    return {
        "pipeline_version": PIPELINE_VERSION,
        "settings": asdict(settings),
        "packages": {p: _version(p) for p in _PACKAGES},
        "converter": {**CONVERTER_CONFIG, "layout": DEFAULT_LAYOUT},
        "models": {**model_ids(), **(models or {})},
    }


def pipeline_fingerprint(settings=None, models: dict | None = None) -> str:
    """SHA-256 (hex, first 16 characters) of fingerprint_inputs(settings, models)."""
    data = json.dumps(fingerprint_inputs(settings, models), sort_keys=True, default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    print(json.dumps({**fingerprint_inputs(), "fingerprint": pipeline_fingerprint()}, indent=1, default=str))
