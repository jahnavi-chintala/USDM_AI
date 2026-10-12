"""Service configuration from environment variables (documented defaults; nothing hard-coded elsewhere).

  DATA_DIR             root of all service data (default: ./data next to the code; /data in the container)
  OPENROUTER_API_KEY   key for figure descriptions, embeddings and reranking (never logged)
  OPENROUTER_OFFLINE   "1": never call OpenRouter; a stage that needs it is skipped with a warning
  FIGURE_MODEL         figure descriptions (default openrouter:anthropic/claude-opus-5.5; "" turns them off)
  EMBED_MODEL          chunk and query embeddings (default qwen/qwen3-embedding-8b)
  RERANK_MODEL         search reranker (default qwen/qwen3-reranker-8b)
  WORKER_CONCURRENCY   documents processed at the same time (default 1)
  FIGURE_CONCURRENCY   figure-description requests in flight per document (default 4)
  MAX_UPLOAD_MB        largest accepted PDF (default 100)
  LOG_LEVEL            default INFO
  HOST, PORT           where `python -m service.api` listens (default 127.0.0.1:8000; 0.0.0.0 in the container)
  ALLOWED_HOSTS        host names clients may address the service by, comma-separated (default
                       localhost,127.0.0.1,::1; "*" turns the check off). A request with another Host header, or
                       from a browser page of another host (Origin header), is refused: an unauthenticated
                       service on localhost is otherwise reachable by any web page (DNS rebinding, cross-site
                       form posts). Set it to the public host name(s) when the service is put behind a domain.

DATA_DIR must be on a local filesystem (in Docker: a named volume): the registry is SQLite in WAL mode, which
does not work on NFS / SMB shares or Windows bind mounts. Run one service process per DATA_DIR.

Layout under DATA_DIR (the storage layer owns it):
  registry.sqlite            documents and jobs
  documents/<doc_id>/        source.pdf, protocol.json/.md/.html, protocol.decisions.json, images/, tables/,
                             chunks/ (chunk files, embeddings, caches), quality.json, logs/
  work/<job_id>/             scratch space of a running job, moved into documents/ atomically on success
  cache/vlm/                 figure-description cache, shared across documents (image + model + prompt version)
  cache/batches/             Docling conversion batch cache (per PDF hash and converter configuration)
  inbox/                     PDFs placed for ingestion by path (MCP ingest_document)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWED_HOSTS = ("localhost", "127.0.0.1", "::1")

# The same defaults as pipeline_version.DEFAULT_FIGURE_MODEL, embed_chunks.MODEL and retrieve.RERANK_MODEL
# (tests/test_service_pipeline.py checks they agree).
DEFAULT_FIGURE_MODEL = "openrouter:anthropic/claude-opus-5.5"
DEFAULT_EMBED_MODEL = "qwen/qwen3-embedding-8b"
DEFAULT_RERANK_MODEL = "qwen/qwen3-reranker-8b"


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def host_name(value: str) -> str:
    """The host of a Host header or ALLOWED_HOSTS entry, lower case, without port or IPv6 brackets:
    'Example.org:8000' -> 'example.org', '[::1]:8000' -> '::1', '::1' -> '::1'."""
    value = value.strip().lower()
    if value.startswith("["):
        return value[1:value.index("]")] if "]" in value else value
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _hosts(name: str) -> tuple[str, ...]:
    value = os.environ.get(name, "").strip()
    return tuple(dict.fromkeys(host_name(h) for h in value.split(",") if h.strip())) or DEFAULT_ALLOWED_HOSTS


@dataclass(frozen=True)
class ServiceConfig:
    data_dir: Path
    offline: bool = False
    figure_model: str = DEFAULT_FIGURE_MODEL
    embed_model: str = DEFAULT_EMBED_MODEL
    rerank_model: str = DEFAULT_RERANK_MODEL
    worker_concurrency: int = 1
    figure_concurrency: int = 4
    max_upload_mb: int = 100
    log_level: str = "INFO"
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS

    @classmethod
    def from_env(cls) -> "ServiceConfig":
        figure = os.environ.get("FIGURE_MODEL")
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR") or ROOT / "data").resolve(),
            offline=_flag("OPENROUTER_OFFLINE"),
            figure_model=DEFAULT_FIGURE_MODEL if figure is None else figure.strip(),
            embed_model=os.environ.get("EMBED_MODEL") or DEFAULT_EMBED_MODEL,
            rerank_model=os.environ.get("RERANK_MODEL") or DEFAULT_RERANK_MODEL,
            worker_concurrency=max(1, _int("WORKER_CONCURRENCY", 1)),
            figure_concurrency=max(1, _int("FIGURE_CONCURRENCY", 4)),
            max_upload_mb=max(1, _int("MAX_UPLOAD_MB", 100)),
            log_level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
            allowed_hosts=_hosts("ALLOWED_HOSTS"),
        )

    # ---- request origin checks (service.api applies them to every route, /mcp included)
    def host_allowed(self, host: str | None) -> bool:
        """A Host header naming one of allowed_hosts (any port)."""
        if "*" in self.allowed_hosts:
            return True
        return bool(host) and host_name(host) in self.allowed_hosts

    def origin_allowed(self, origin: str | None) -> bool:
        """No Origin header (non-browser clients, same-origin GET), or an http(s) page of an allowed host."""
        if not origin or "*" in self.allowed_hosts:
            return True
        try:
            parts = urlsplit(origin.strip())
            return parts.scheme in ("http", "https") and (parts.hostname or "") in self.allowed_hosts
        except ValueError:
            return False

    # ---- layout (the storage layer creates and owns these)
    @property
    def registry_path(self) -> Path:
        return self.data_dir / "registry.sqlite"

    @property
    def documents_dir(self) -> Path:
        return self.data_dir / "documents"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def vlm_cache_dir(self) -> Path:
        return self.cache_dir / "vlm"

    @property
    def batch_cache_dir(self) -> Path:
        return self.cache_dir / "batches"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    def models(self) -> dict:
        """Model ids as pipeline_version.fingerprint_inputs() names them."""
        return {"figure": self.figure_model, "embedding": self.embed_model, "reranker": self.rerank_model}

    def public(self) -> dict:
        """Safe to show (/version, logs): no key, no paths beyond the data root."""
        return {"data_dir": str(self.data_dir), "offline": self.offline, "figure_model": self.figure_model,
                "embed_model": self.embed_model, "rerank_model": self.rerank_model,
                "worker_concurrency": self.worker_concurrency, "figure_concurrency": self.figure_concurrency,
                "max_upload_mb": self.max_upload_mb, "allowed_hosts": list(self.allowed_hosts),
                "api_key_set": bool(os.environ.get("OPENROUTER_API_KEY"))}
