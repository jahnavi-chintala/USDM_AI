"""Background worker: runs queued jobs through the pipeline.

Worker(config, registry, storage, pipeline_fn).start() recovers what a crash left behind, then runs
config.worker_concurrency threads that claim queued jobs FIFO (atomically, in the registry) and, per job:

  1. call pipeline_fn(pdf_path=work/<job_id>/source.pdf, workdir=work/<job_id>, config=config,
     settings=settings, progress=callback, password=password)  (see service.contracts); progress goes to
     the jobs table (stage, done/total);
  2. on success: write work/<job_id>/publish.json (job id + PipelineResult), publish the workdir atomically
     into documents/<doc_id>/ and record the result (done / done_with_warnings, related documents);
  3. on an exception: record "failed" with the exception type and our own file:line (frames inside the
     project, never the message: it can quote document text) and the stages the exception carries
     (`pipeline_stages`, see service.contracts), append the full traceback to work/<job_id>/logs/error.log;
     a previously published result stays untouched.

Recovery at start: an interrupted publish is finished (storage.recover()); a job left running whose result
was already published (publish.json in the document carries its job id) is recorded; any other running job
is re-queued (its workdir still holds source.pdf and the conversion resumes from the batch cache), failing
after MAX_ATTEMPTS starts. Run one worker process per DATA_DIR: recovery assumes no other process is
running jobs on the same data.

A PDF password is taken from registry.pdf_passwords (memory only) when the job starts and dropped after.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import fields
from pathlib import Path

from service.config import ROOT, ServiceConfig
from service.contracts import PipelineFn, PipelineResult, StageResult
from service.registry import Registry
from service.storage import SOURCE_NAME, Storage, utcnow

log = logging.getLogger("service.worker")

_NOT_OURS = {".venv", "venv", "env", "site-packages", "dist-packages"}


class SourceMissing(RuntimeError):
    pass


class DocIdMismatch(RuntimeError):
    pass


def _project_relative(filename: str) -> str | None:
    """'service/worker.py' for a file of this project (not the virtualenv), else None."""
    try:
        path = Path(filename).resolve()
        rel = path.relative_to(ROOT)
    except (OSError, ValueError):
        return None
    if not rel.parts or _NOT_OURS.intersection(rel.parts) or not path.is_file():
        return None
    return rel.as_posix()


def describe_exception(exc: BaseException) -> tuple[str, str | None]:
    """(exception type name, "<project file>:<line>" of the innermost frame in our own code, or None).
    Never the message: exception messages can quote document text. An exception carrying error_type /
    error_where (service.pipeline.PipelineFailed: the pipeline failed in its child process) reports those."""
    if getattr(exc, "error_type", None):
        return exc.error_type, getattr(exc, "error_where", None)
    where = None
    for frame in traceback.extract_tb(exc.__traceback__):
        rel = _project_relative(frame.filename)
        if rel:
            where = f"{rel}:{frame.lineno}"
    return type(exc).__name__, where


def stages_from_json(items: list[dict] | None) -> list[StageResult]:
    """StageResults from their asdict() form."""
    names = {f.name for f in fields(StageResult)}
    return [StageResult(**{k: v for k, v in s.items() if k in names}) for s in items or []]


def result_from_json(data: dict) -> PipelineResult:
    """PipelineResult from its to_json() form (publish.json)."""
    names = {f.name for f in fields(PipelineResult)} - {"stages"}
    return PipelineResult(**{k: v for k, v in data.items() if k in names}, stages=stages_from_json(data.get("stages")))


class Worker:
    """Runs queued jobs. start()/stop() for the service; run_once()/run_until_idle() for tests and scripts."""

    def __init__(self, config: ServiceConfig, registry: Registry, storage: Storage, pipeline_fn: PipelineFn, *,
                 settings=None, concurrency: int | None = None, poll_interval: float = 1.0,
                 progress_interval: float = 0.5, orphan_age: float = 3600.0) -> None:
        self.config = config
        self.registry = registry
        self.storage = storage
        self.pipeline_fn = pipeline_fn
        self.settings = settings
        self.concurrency = max(1, concurrency or config.worker_concurrency)
        self.poll_interval = poll_interval
        self.progress_interval = progress_interval
        self.orphan_age = orphan_age
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self._guard = threading.Lock()

    # ---- lifecycle
    def start(self) -> dict:
        """Recover, then start the worker threads (no-op when running). Returns recover()'s report."""
        with self._guard:
            if self._threads:
                return {}
            self._stop.clear()
            report = self.recover()
            for i in range(self.concurrency):
                thread = threading.Thread(target=self._loop, name=f"service-worker-{i}", daemon=True)
                thread.start()
                self._threads.append(thread)
        log.info("worker started: %d thread(s), recovery %s", self.concurrency,
                 {k: len(v) if isinstance(v, list) else v for k, v in report.items()})
        return report

    def stop(self, timeout: float | None = 30.0) -> None:
        """Stop claiming jobs and wait for the running ones to finish (up to timeout). A job still running
        when the process exits stays "running" and is re-queued by the next start()."""
        with self._guard:
            self._stop.set()
            self._wake.set()
            threads, self._threads = self._threads, []
        for thread in threads:
            thread.join(timeout)

    @property
    def alive(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    def notify(self) -> None:
        """Wake idle threads now (call after queueing a job) instead of at the next poll."""
        self._wake.set()

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued job (never started afterwards) and remove its workdir."""
        return self.registry.cancel_job(job_id, self.storage)

    # ---- recovery
    def recover(self) -> dict:
        """Finish interrupted publishes, record or re-queue jobs left running, remove orphan workdirs."""
        report = {"documents": self.storage.recover(), "recorded": [], "requeued": [], "failed": [], "orphans": 0}
        for job in self.registry.running_jobs():
            job_id, doc_id = job["job_id"], job["doc_id"]
            record = self.storage.read_publish_record(doc_id)
            if record and record.get("job_id") == job_id:  # crashed after publish, before recording it
                self.registry.record_success(job_id, result_from_json(record["result"]))
                report["recorded"].append(job_id)
                continue
            if job["kind"] == "import":  # an interrupted import is not turned into a processing job
                self.registry.record_failure(job_id, "ImportInterrupted", None)
                report["failed"].append(job_id)
                continue
            if not self.storage.has_source(job_id) and not self.storage.seed_reprocess(doc_id, job_id):
                self.registry.record_failure(job_id, "SourceMissing", None)
                report["failed"].append(job_id)
                continue
            state = self.registry.requeue_job(job_id)
            report["requeued" if state == "queued" else "failed"].append(job_id)
        report["orphans"] = self.storage.remove_orphan_workdirs(self.registry.known_job_ids(), self.orphan_age)
        return report

    # ---- running jobs
    def run_once(self) -> str | None:
        """Claim the oldest queued job and run it in this thread; its job_id, or None when the queue is empty."""
        job = self.registry.claim_next_job()
        if job is None:
            return None
        self._run(job)
        return job["job_id"]

    def run_until_idle(self, max_jobs: int = 1000) -> list[str]:
        done = []
        while len(done) < max_jobs and (job_id := self.run_once()):
            done.append(job_id)
        return done

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.registry.claim_next_job()
            except Exception as exc:  # noqa: BLE001 - e.g. database locked for longer than busy_timeout
                log.error("claiming a job failed: %s", type(exc).__name__)
                job = None
            if job is None:
                self._wake.wait(self.poll_interval)
                self._wake.clear()
                continue
            try:
                self._run(job)
            except Exception as exc:  # noqa: BLE001 - never let one job end the thread
                log.error("job %s: worker error %s at %s", job["job_id"], *describe_exception(exc))

    def _progress(self, job_id: str):
        last = {"stage": None, "t": 0.0}
        lock = threading.Lock()

        def progress(stage: str, done: int | None = None, total: int | None = None) -> None:
            now = time.monotonic()
            with lock:
                finished = done is not None and done == total
                if stage == last["stage"] and now - last["t"] < self.progress_interval and not finished:
                    return
                last.update(stage=stage, t=now)
            try:
                self.registry.update_progress(job_id, str(stage), done, total)
            except Exception as exc:  # noqa: BLE001 - progress must never fail a job
                log.debug("job %s: progress not recorded (%s)", job_id, type(exc).__name__)

        return progress

    def _run(self, job: dict) -> str:
        """Run one claimed job to its final state; returns that state."""
        job_id, doc_id = job["job_id"], job["doc_id"]
        workdir = self.storage.workdir(job_id)
        password = self.registry.pdf_passwords.pop(job_id)
        started = time.monotonic()
        log.info("job %s started (document %s, attempt %d)", job_id, doc_id[:12], job["attempts"])
        try:
            pdf_path = workdir / SOURCE_NAME
            if not pdf_path.is_file():
                raise SourceMissing("source.pdf missing from the workdir")
            result = self.pipeline_fn(pdf_path=pdf_path, workdir=workdir, config=self.config, settings=self.settings,
                                      progress=self._progress(job_id), password=password)
            password = None
            if not isinstance(result, PipelineResult):
                raise TypeError("the pipeline did not return a PipelineResult")
            if result.sha256 and result.sha256 != doc_id:
                raise DocIdMismatch("the pipeline hashed different bytes")
            self.storage.write_publish_record(job_id, {"job_id": job_id, "doc_id": doc_id,
                                                       "published_at": utcnow(), "result": result.to_json()})
            self.storage.publish(job_id, doc_id)
        except Exception as exc:  # noqa: BLE001 - any pipeline failure is recorded, not raised
            password = None
            return self._fail(job_id, exc)
        try:
            state = self.registry.record_success(job_id, result)["state"]
        except Exception as exc:  # noqa: BLE001 - published but not recorded: recover() records it at restart
            log.error("job %s: published but not recorded (%s); recorded at the next start",
                      job_id, type(exc).__name__)
            return "running"
        log.info("job %s %s in %.1fs (%d warning(s))", job_id, state, time.monotonic() - started,
                 len(result.warnings))
        return state

    def _fail(self, job_id: str, exc: Exception) -> str:
        error_type, where = describe_exception(exc)
        try:
            self.storage.write_error_log(job_id, "".join(traceback.format_exception(exc)))
        except OSError:
            log.warning("job %s: error.log could not be written", job_id)
        stages = getattr(exc, "pipeline_stages", None)  # see service.contracts; anything else is ignored
        if not (isinstance(stages, list) and all(isinstance(s, StageResult) for s in stages)):
            stages = []
        self.registry.record_failure(job_id, error_type, where, stages)
        log.warning("job %s failed: %s at %s", job_id, error_type, where or "?")
        return "failed"
