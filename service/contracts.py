"""Shapes shared by the pipeline, the registry, the worker and the APIs.

Pipeline entry point (service.pipeline.process_document; the worker takes any callable of this shape):

    process_document(pdf_path: Path, workdir: Path, config: ServiceConfig, *, settings=None,
                     progress: ProgressCallback | None = None, password: str | None = None) -> PipelineResult

  - pdf_path is workdir / "source.pdf" (the worker copies the upload there first).
  - Every output is written under workdir, in the final layout of documents/<doc_id>/ (see config.py), so the
    worker can publish workdir as a whole. Nothing is written elsewhere except the shared caches under
    config.cache_dir.
  - A fatal error raises; the worker records the exception type and our own file:line only (never the
    message: it can quote document text) and keeps the full traceback in workdir/logs/.
  - Non-fatal problems (a figure the model could not describe, a stage skipped because OPENROUTER_OFFLINE is
    set) come back as warnings with status "done_with_warnings".
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Protocol

# ---- states
JOB_STATES = ("queued", "running", "done", "done_with_warnings", "failed", "cancelled")
JOB_FINAL = ("done", "done_with_warnings", "failed", "cancelled")
# A document's status is its latest job's state; "none" before its first job. A failed or cancelled reprocess
# leaves the older result served: then status is "failed"/"cancelled" while the registry's has_result is true
# and stale describes the served result.
DOC_STATES = ("none", "queued", "running", "done", "done_with_warnings", "failed", "cancelled")

# ---- pipeline stages, in order (progress reports use these names)
STAGES = ("hash", "convert", "postprocess", "figures", "save", "chunk", "embed", "quality")
STAGE_STATUSES = ("done", "skipped", "skipped_offline", "partial", "failed")


class ProgressCallback(Protocol):
    def __call__(self, stage: str, done: int | None = None, total: int | None = None) -> None: ...


@dataclass
class StageResult:
    name: str                      # one of STAGES
    status: str                    # one of STAGE_STATUSES
    seconds: float = 0.0
    detail: dict = field(default_factory=dict)  # counts only, never document text


@dataclass
class PipelineResult:
    status: str                    # "done" | "done_with_warnings"
    pipeline_version: str
    fingerprint: str
    sha256: str                    # of the PDF bytes (= doc_id)
    pages: int
    identifiers: dict              # {"nct", "sponsor_protocol_id", "version", "date", ...}: values or None
    quality: dict                  # summary; the full report is workdir/quality.json
    warnings: list[str] = field(default_factory=list)
    stages: list[StageResult] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


PipelineFn = Callable[..., PipelineResult]
