"""The single pipeline entry point: one PDF in, one finished document folder out.

process_document(pdf_path, workdir, config, settings=None, progress=None, password=None) -> PipelineResult

Stages (contracts.STAGES), all writing into `workdir` in the final documents/<doc_id>/ layout:
  hash         SHA-256 of the PDF bytes (the document id)
  convert      Docling in 15-page batches, each in a subprocess; batches cached under config.batch_cache_dir
  postprocess  protocol_postprocess.postprocess (decisions log -> protocol.decisions.json)
  figures      figure descriptions: figure_vlm.describe_missing runs config.figure_model through OpenRouter
               (zero-data-retention routing) for every figure that is not skipped and not cached in the shared
               config.vlm_cache_dir; then the cached results are attached. A figure that fails is a warning.
  save         protocol.json / .md / .html, images/, tables/
  chunk        chunks/ (chunks, parents, sections, tables, glossary, stats)
  embed        chunks/embeddings.npy (config.embed_model; cached per chunk content hash)
  quality      quality.json: word coverage, table QA, figure counts, stage results, warnings

OPENROUTER_OFFLINE: the figures and embed stages never call out; what they would have needed is reported as
"skipped_offline" with a warning, and the document still finishes ("done_with_warnings"). A fatal error (the
conversion itself, chunking) raises; the caller records the exception type and our file:line only, and the
stages so far from the exception's `pipeline_stages` (completed ones, then the failing one with status "failed").
Logs: workdir/logs/pipeline.log (this thread's records) and workdir/logs/convert.log (batch subprocesses).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from service.config import ServiceConfig
from service.contracts import PipelineResult, StageResult

log = logging.getLogger("service.pipeline")

STEM = "protocol"      # protocol.json / .md / .html / .decisions.json
IMAGES_DIR = "images"


class _ThreadFilter(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self.thread = threading.get_ident()

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.thread


@contextmanager
def _job_log(path: Path):
    """Records of this thread (the job) go to `path` while the job runs."""
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(_ThreadFilter())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()


IDENTIFIER_KINDS = ("nct", "sponsor_protocol_id", "eudract", "eu_ct", "ind", "who_utn")
IDENTIFIER_KEYS = (*IDENTIFIER_KINDS, "version", "amendment", "date", "acronym", "sponsor")


def _value(field):
    """A field's value: document_info reports {"value", "source", ...}, stats.json the bare value."""
    return field.get("value") if isinstance(field, dict) else field


def _identifiers_from_info(info: dict) -> dict:
    ids, version = info.get("identifiers") or {}, info.get("version") or {}
    return {**{kind: _value(ids.get(kind)) for kind in IDENTIFIER_KINDS},
            "version": version.get("label"), "amendment": version.get("amendment"), "date": _value(version.get("date")),
            "acronym": _value((info.get("titles") or {}).get("acronym")),
            "sponsor": (info.get("sponsor") or {}).get("name")}


def _identifiers_from_stats(meta: dict) -> dict:
    """The same values from chunks/stats.json's "document" block (no sponsor: only document_info finds it)."""
    ids = meta.get("identifiers") or {}
    out = {kind: _value(ids.get(kind)) for kind in IDENTIFIER_KINDS}
    out["sponsor_protocol_id"] = out["sponsor_protocol_id"] or meta.get("protocol_id")
    return {**out, "version": meta.get("version_label"), "amendment": meta.get("amendment"),
            "date": meta.get("version_date"), "acronym": meta.get("acronym"), "sponsor": None}


def document_identifiers(out_dir: Path) -> dict:
    """The identifiers of a finished document folder (protocol.json + chunks/), values only: nct,
    sponsor_protocol_id, eudract, eu_ct, ind, who_utn, version, amendment, date, acronym, sponsor (IDENTIFIER_KEYS;
    str or None). The pipeline records them in its result and the import of an existing folder in the registry.

    From ProtocolDocument.document_info; when the folder cannot be opened, from chunks/stats.json (best effort:
    a folder with neither gives all None). Only the exception type is logged."""
    out_dir = Path(out_dir)
    try:
        from protocol_api import ProtocolDocument

        return _identifiers_from_info(ProtocolDocument.open(out_dir).document_info())
    except Exception as exc:  # noqa: BLE001 - identity is best effort; stats.json is the fallback
        log.warning("document_info unavailable (%s); using chunks/stats.json", type(exc).__name__)
    try:
        return _identifiers_from_stats(
            json.loads((out_dir / "chunks" / "stats.json").read_text(encoding="utf-8"))["document"])
    except Exception:  # noqa: BLE001
        return dict.fromkeys(IDENTIFIER_KEYS)


def process_document(pdf_path: Path, workdir: Path, config: ServiceConfig, *, settings=None, progress=None,
                     password: str | None = None) -> PipelineResult:
    """Run the whole pipeline for one PDF into `workdir` (see the module docstring)."""
    import pypdfium2 as pdfium
    from docling_core.types.doc import DoclingDocument

    from chunk_protocol import chunk_document
    from convert_protocol import convert_document, file_sha256
    from embed_chunks import embed_document
    from pipeline_version import PIPELINE_VERSION, pipeline_fingerprint
    from protocol_settings import ProtocolSettings
    from retrieve import OfflineRefused
    from validate_output import quality_report

    import os

    pdf_path, workdir = Path(pdf_path), Path(workdir)
    settings = settings or ProtocolSettings()
    if config.offline:  # the modules read the environment; turning offline on is always safe
        os.environ["OPENROUTER_OFFLINE"] = "1"
    (workdir / "logs").mkdir(parents=True, exist_ok=True)
    stages: list[StageResult] = []
    warnings: list[str] = []
    starts: dict[str, float] = {}  # stage -> first report, in order

    def report(stage: str, done: int | None = None, total: int | None = None) -> None:
        starts.setdefault(stage, time.time())
        if progress is not None:
            progress(stage, done, total)

    def finish(name: str, status: str = "done", **detail) -> None:
        """A stage lasts from its first report to the next stage's first report (or now)."""
        begin = starts.get(name)
        later = [t for t in starts.values() if begin is not None and t > begin]
        seconds = (min(later) if later else time.time()) - begin if begin is not None else 0.0
        stages.append(StageResult(name, status, round(seconds, 1), detail))

    @contextmanager
    def stages_on_failure():
        """A failure carries the stages so far (exc.pipeline_stages, see service.contracts): those finished, then
        the failing one, the last stage reported and not finished, with status "failed". A stage reported
        before it and never finished ran through (the conversion runs convert, postprocess, figures and save
        in one call)."""
        try:
            yield
        except Exception as exc:
            pending = [name for name in starts if name not in {s.name for s in stages}]
            for name in pending[:-1]:
                finish(name)
            if pending:
                finish(pending[-1], "failed")
            exc.pipeline_stages = list(stages)
            raise

    with _job_log(workdir / "logs" / "pipeline.log"), stages_on_failure():
        log.info("Pipeline %s for %s", PIPELINE_VERSION, pdf_path.name)
        report("hash")
        sha = file_sha256(pdf_path)
        finish("hash")

        report("convert")
        conv = convert_document(
            pdf_path, workdir, settings=settings, stem=STEM, images_dir=IMAGES_DIR, force=True,
            batch_root=config.batch_cache_dir, figure_model=config.figure_model, vlm_cache=config.vlm_cache_dir,
            describe_figures=bool(config.figure_model), figure_concurrency=config.figure_concurrency,
            password=password, progress=report, subprocess_log=workdir / "logs" / "convert.log")
        for name in ("convert", "postprocess"):
            finish(name, **({"pages": conv["pages"]} if name == "convert" else
                            {k: v for k, v in conv["postprocess"].items() if isinstance(v, int) and v}))

        figs = conv.get("figures") or {}
        if not config.figure_model:
            finish("figures", "skipped", reason="no figure model")
        else:
            fig_status = "done"
            if figs.get("offline"):
                fig_status = "skipped_offline"
                warnings.append(f"{figs['offline']} figure(s) not described: OPENROUTER_OFFLINE is set")
            if figs.get("failed"):
                fig_status = "partial"
                warnings.append(f"{figs['failed']} figure(s) could not be described")
            if figs.get("unsupported_model"):
                fig_status = "partial"
                warnings.append(f"{figs['unsupported_model']} figure(s) not described: FIGURE_MODEL is not an "
                                "openrouter: model")
            finish("figures", fig_status, **{k: v for k, v in figs.items() if v})
        finish("save", tables=conv["tables"], pictures=conv["pictures"])

        report("chunk")
        chunks_dir = workdir / "chunks"
        stats = chunk_document(workdir / f"{STEM}.json", chunks_dir, quiet_text=True)
        finish("chunk", chunks=stats["chunks"], parents=stats.get("parents", {}).get("parents", 0))

        report("embed")
        try:
            emb = embed_document(chunks_dir / "chunks.jsonl", config.embed_model, progress=report)
            finish("embed", **emb)
        except OfflineRefused:
            finish("embed", "skipped_offline")
            warnings.append("embeddings not computed: OPENROUTER_OFFLINE is set (search is unavailable until "
                            "the document is reprocessed online)")
        except Exception as exc:  # noqa: BLE001 - the document stays readable without search
            finish("embed", "failed", error=type(exc).__name__)
            warnings.append(f"embeddings failed ({type(exc).__name__}): search is unavailable for this document")

        report("quality")
        doc = DoclingDocument.load_from_json(workdir / f"{STEM}.json")
        pdf = pdfium.PdfDocument(str(pdf_path), password=password)
        dec_path = workdir / f"{STEM}.decisions.json"
        decisions = json.loads(dec_path.read_text(encoding="utf-8")) if dec_path.exists() else None
        quality = quality_report(doc, pdf, decisions)
        pdf.close()
        cov = quality["coverage"]
        if cov["coverage"] is not None and cov["coverage"] < 0.97:
            warnings.append(f"word coverage {cov['coverage']:.1%} is below 97%")
        finish("quality", coverage=cov["coverage"], pages_below_97=cov["pages_below_threshold"])

        status = "done_with_warnings" if warnings else "done"
        summary = {"coverage": cov["coverage"], "pages_below_97": cov["pages_below_threshold"],
                   "tables": quality["tables"].get("tables"), "marks_missing": quality["tables"].get("marks_missing"),
                   "figures": quality["figures"], "chunks": stats["chunks"],
                   "stages": {s.name: s.status for s in stages}}
        result = PipelineResult(
            status=status, pipeline_version=PIPELINE_VERSION,
            fingerprint=pipeline_fingerprint(settings, config.models()), sha256=sha, pages=conv["pages"],
            identifiers=document_identifiers(workdir), quality=summary, warnings=warnings, stages=stages)
        (workdir / "quality.json").write_text(json.dumps({**quality, "summary": summary, "warnings": warnings,
                                                          "stages": [s.__dict__ for s in stages]},
                                                         indent=1), encoding="utf-8")
        (workdir / "pipeline.json").write_text(json.dumps(result.to_json(), indent=1), encoding="utf-8")
        log.info("Pipeline finished: %s, %d warning(s)", status, len(warnings))
        return result


# =========================================================================== isolated run (one process per job)

class PipelineFailed(RuntimeError):
    """The pipeline failed in its child process. error_type / error_where: the original exception's type and
    our own file:line (never its message); the traceback is in workdir/logs/error.log. pipeline_stages: the
    stages the child got through (service.contracts), none when it died without reporting."""

    def __init__(self, error_type: str, error_where: str | None, stages: list[StageResult] | None = None):
        super().__init__(f"{error_type} at {error_where or 'library code'}")
        self.error_type, self.error_where = error_type, error_where
        self.pipeline_stages = list(stages or [])


_MARK = "@@pipeline "


def process_document_isolated(pdf_path: Path, workdir: Path, config: ServiceConfig, *, settings=None, progress=None,
                              password: str | None = None) -> PipelineResult:
    """process_document in a fresh Python process (same signature; the worker's default pipeline_fn).

    pdfium and the models are not thread-safe and native code can crash: a child process per job keeps the
    service (API, MCP, other jobs) alive and lets WORKER_CONCURRENCY > 1 run safely. Settings, configuration
    and the password go to the child on stdin (never on the command line or in the environment); progress
    and the outcome come back as marked stdout lines (a failure with the stages the child got through); the child's
    other output goes to workdir/logs/."""
    import os
    import subprocess
    import sys
    from dataclasses import asdict

    workdir = Path(workdir)
    (workdir / "logs").mkdir(parents=True, exist_ok=True)
    payload = {"pdf_path": str(pdf_path), "workdir": str(workdir),
               "config": {**asdict(config), "data_dir": str(config.data_dir)},
               "settings": asdict(settings) if settings is not None else None, "password": password}
    env = dict(os.environ)
    if config.offline:
        env["OPENROUTER_OFFLINE"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    root = Path(__file__).resolve().parents[1]
    error: dict | None = None
    with open(workdir / "logs" / "child.stderr.log", "a", encoding="utf-8") as err, \
            open(workdir / "logs" / "child.stdout.log", "a", encoding="utf-8") as other:
        proc = subprocess.Popen([sys.executable, "-m", "service.pipeline"], cwd=root, env=env, text=True,
                                encoding="utf-8", stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err)
        proc.stdin.write(json.dumps(payload))
        proc.stdin.close()
        payload = None  # drop the password
        for line in proc.stdout:
            if not line.startswith(_MARK):
                other.write(line)
                continue
            kind, _, body = line[len(_MARK):].partition(" ")
            data = json.loads(body)
            if kind == "progress" and progress is not None:
                progress(data["stage"], data.get("done"), data.get("total"))
            elif kind == "error":
                error = data
        rc = proc.wait()
    from service.worker import result_from_json, stages_from_json

    if rc != 0 or error is not None:
        error = error or {"type": f"ChildExit{rc}", "where": None}
        raise PipelineFailed(error["type"], error.get("where"), stages_from_json(error.get("stages")))
    return result_from_json(json.loads((workdir / "pipeline.json").read_text(encoding="utf-8")))


def _child_main() -> int:
    """Entry point of the child process: payload on stdin, marked lines on stdout."""
    import sys
    import traceback
    from dataclasses import asdict

    from protocol_settings import ProtocolSettings

    payload = json.loads(sys.stdin.read())
    cfg = payload["config"]
    config = ServiceConfig(**{**cfg, "data_dir": Path(cfg["data_dir"])})
    settings = ProtocolSettings(**payload["settings"]) if payload.get("settings") else None
    workdir = Path(payload["workdir"])
    logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO), stream=sys.stderr)

    def emit(kind: str, data: dict) -> None:
        sys.stdout.write(f"{_MARK}{kind} {json.dumps(data, default=str)}\n")
        sys.stdout.flush()

    try:
        process_document(Path(payload["pdf_path"]), workdir, config, settings=settings,
                         password=payload.get("password"),
                         progress=lambda stage, done=None, total=None: emit(
                             "progress", {"stage": stage, "done": done, "total": total}))
        return 0
    except Exception as exc:  # noqa: BLE001
        from service.worker import describe_exception

        etype, where = describe_exception(exc)
        with open(workdir / "logs" / "error.log", "a", encoding="utf-8") as fh:
            traceback.print_exc(file=fh)
        emit("error", {"type": etype, "where": where,
                       "stages": [asdict(s) for s in getattr(exc, "pipeline_stages", None) or []]})
        return 1


if __name__ == "__main__":
    raise SystemExit(_child_main())
