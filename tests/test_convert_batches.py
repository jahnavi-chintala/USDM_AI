"""Self-test for convert_protocol.convert_batches (parallel batches): `python tests/test_convert_batches.py`.

Offline and without Docling conversions: subprocess.run is replaced by a fake worker that records each call and
writes a minimal batch file, so the batch scheduling, the progress, the log and the retry rules are tested alone.
"""

import io
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pypdfium2 as pdfium  # noqa: E402
from docling_core.types.doc import BoundingBox, DocItemLabel, DoclingDocument, ProvenanceItem, Size  # noqa: E402

import convert_protocol as cp  # noqa: E402


class Gated:
    """A scripted fake-worker result: wait until the test opens the gate, then return `rc`."""

    def __init__(self, rc: int = 0) -> None:
        self.rc = rc


def output_of(first: int, last: int, newline: bool = True) -> bytes:
    text = f"worker line 1 of {first}-{last}\nworker line 2 of {first}-{last}"
    return (text + "\n" if newline else text).encode()


def write_batch(batch_dir: Path, first: int, last: int) -> None:
    """A batch file as the real worker writes it: one page and one text item per page."""
    doc = DoclingDocument(name=f"pages-{first}-{last}")
    for page in range(first, last + 1):
        doc.add_page(page_no=page, size=Size(width=200, height=200))
        doc.add_text(label=DocItemLabel.TEXT, text=f"page {page}",
                     prov=ProvenanceItem(page_no=page, bbox=BoundingBox(l=0, t=0, r=1, b=1), charspan=(0, 1)))
    doc.save_as_json(batch_dir / f"{first:04d}-{last:04d}.json")


class FakeWorkers:
    """Stands in for subprocess.run. `script` maps (first, last, attempt) to a return code, Gated(rc) or an
    exception to raise; anything unscripted succeeds. Ranges in `together` wait on a Barrier(2): they only pass
    when two of them run at the same time. Ranges in `cut` write output that stops mid-line (a killed worker)."""

    def __init__(self, script: dict | None = None, together: tuple = (), cut: tuple = ()) -> None:
        self.script = script or {}
        self.together = set(together)
        self.cut = set(cut)
        self.barrier = threading.Barrier(2, timeout=10)
        self.gate = threading.Event()
        self.lock = threading.Lock()
        self.calls: list[dict] = []

    def __call__(self, cmd, env=None, stdout=None, stderr=None, **kwargs):
        at = cmd.index("--range")
        first, last = int(cmd[at + 1]), int(cmd[at + 2])
        with self.lock:
            attempt = 1 + sum(1 for c in self.calls if c["range"] == (first, last))
            self.calls.append({
                "range": (first, last), "attempt": attempt, "cmd": list(cmd),
                "env": dict(env), "password": env.get("PDF_PASSWORD"),
                "stdout": "inherit" if stdout is None else "pipe" if stdout is subprocess.PIPE else "file",
                "thread": threading.get_ident(),
            })
        action = self.script.get((first, last, attempt), 0)
        if (first, last) in self.together:
            self.barrier.wait()
        if isinstance(action, Gated):
            assert self.gate.wait(10), "the test never opened the gate"
            action = action.rc
        if isinstance(action, BaseException):
            raise action
        if action == 0:
            write_batch(Path(cmd[cmd.index("--batch-dir") + 1]), first, last)
        output = output_of(first, last, newline=(first, last) not in self.cut)
        if stdout is subprocess.PIPE:
            return subprocess.CompletedProcess(cmd, action, stdout=output)
        if stdout is not None:
            stdout.write(output.decode())
        return subprocess.CompletedProcess(cmd, action)

    def ranges(self) -> list[tuple[int, int]]:
        return [c["range"] for c in self.calls]


class Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


def make_pdf(path: Path, pages: int) -> None:
    pdf = pdfium.PdfDocument.new()
    for _ in range(pages):
        pdf.new_page(200, 200)
    pdf.save(path)
    pdf.close()


def run_batches(fake: FakeWorkers, *, pages: int = 60, precached: tuple = (), gate_after_first: bool = False,
                expect_error: bool = False, on_progress=None, text_stdout: bool = False,
                **kwargs) -> SimpleNamespace:
    """convert_batches on a fresh PDF with the fake worker, in a caller environment without PDF_PASSWORD (restored
    afterwards). Returns the document, progress calls, log records, what went to stdout (a text-only stream with
    text_stdout), the error (with expect_error), the source, output and batch folders, the batch files left in the
    cache and the caller's environment as the workers should get it. on_progress() runs inside every progress call."""
    handler, logger = Records(), logging.getLogger("convert_protocol")
    saved = (logger.level, logger.propagate, subprocess.run, cp.wait, sys.stdout)
    saved_password = os.environ.get("PDF_PASSWORD")
    progress = []
    res = SimpleNamespace(doc=None, n_pages=None, error=None, progress=progress, log=handler, stdout=b"")

    def record(*args) -> None:
        progress.append((*args, threading.get_ident()))
        if on_progress is not None:
            on_progress()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        res.src, res.out, root = tmp / "source.pdf", tmp / "out", tmp / "batches"
        make_pdf(res.src, pages)
        res.batches = cp.batch_cache(res.out, res.src, cp.file_sha256(res.src), cp.DEFAULT_LAYOUT, pages, 15,
                                     root=root)
        for first, last in precached:
            write_batch(res.batches, first, last)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        os.environ.pop("PDF_PASSWORD", None)
        res.environ = dict(os.environ)
        subprocess.run = fake
        if gate_after_first:
            cp.wait = _wait_opening(cp.wait, fake.gate)
        sys.stdout = io.StringIO() if text_stdout else io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
        try:
            kwargs.setdefault("batch_root", root)
            res.doc, res.n_pages = cp.convert_batches(res.src, res.out, progress=record, **kwargs)
        except Exception as exc:  # noqa: BLE001
            if not expect_error:
                raise
            res.error = exc
        finally:
            sys.stdout.flush()
            res.stdout = sys.stdout.getvalue().encode() if text_stdout else sys.stdout.buffer.getvalue()
            logger.level, logger.propagate, subprocess.run, cp.wait, sys.stdout = saved
            logger.removeHandler(handler)
            if saved_password is not None:
                os.environ["PDF_PASSWORD"] = saved_password
        res.files = sorted(p.name for p in res.batches.glob("0*.json"))  # the batches, not manifest.json
    return res


def _wait_opening(real, gate):
    """concurrent.futures.wait that opens `gate` on its second call, i.e. once the calling thread has handled the
    first finished batch (and so decided whether to start more)."""
    calls = []

    def wait(*args, **kwargs):
        if calls:
            gate.set()
        calls.append(None)
        return real(*args, **kwargs)

    return wait


def page_texts(doc: DoclingDocument) -> list[str]:
    return [t.text for t in doc.texts]


def expected_texts(n: int) -> list[str]:
    return [f"page {p}" for p in range(1, n + 1)]


def done_values(res: SimpleNamespace) -> list:
    return [(a, b, c) for a, b, c, _ in res.progress]


def test_single_worker_runs_in_page_order() -> None:
    fake = FakeWorkers()
    res = run_batches(fake, pages=40)
    assert fake.ranges() == [(1, 15), (16, 30), (31, 40)]
    assert res.n_pages == 40 and page_texts(res.doc) == expected_texts(40)
    assert done_values(res) == [("convert", 1, 3), ("convert", 2, 3), ("convert", 3, 3)]
    script = Path(__file__).resolve().parents[1] / "convert_protocol.py"
    for call in fake.calls:  # today's command, spelled out, and the caller's environment as it is
        first, last = call["range"]
        assert call["cmd"] == [sys.executable, str(script), str(res.src), "--out", str(res.out), "--layout",
                               "layout_heron_101", "--batch-dir", str(res.batches), "--range", str(first), str(last)
                               ], call["cmd"]
        assert call["stdout"] == "inherit" and call["password"] is None
        assert call["env"] == res.environ  # no forced thread variables, nothing added
    assert "Converting" not in " ".join(res.log.messages())  # the sequential path logs as before


def test_two_workers_run_two_batches_at_once() -> None:
    fake = FakeWorkers(together=[(1, 15), (16, 30)])  # the Barrier breaks if these two ran one after the other
    res = run_batches(fake, pages=60, workers=2)
    assert sorted(fake.ranges()) == [(1, 15), (16, 30), (31, 45), (46, 60)]
    assert page_texts(res.doc) == expected_texts(60) and res.n_pages == 60
    assert done_values(res) == [("convert", i, 4) for i in range(1, 5)]
    assert all(c["stdout"] == "pipe" for c in fake.calls)
    here = threading.get_ident()  # logging and progress stay in the calling thread, pool threads only run workers
    assert all(r.thread == here for r in res.log.records) and all(p[-1] == here for p in res.progress)
    assert all(c["thread"] != here for c in fake.calls)
    messages = res.log.messages()
    assert "Converting 4 batches, 2 at a time" in messages
    assert sum(m.startswith("Pages ") and " done (" in m for m in messages) == 4
    for first, last in fake.ranges():  # no log file: output and header go to stdout, once each
        assert res.stdout.count(f"=== pages {first}-{last} ===\n".encode() + output_of(first, last)) == 1


def test_cached_batches_are_not_run() -> None:
    for workers in (1, 2):
        fake = FakeWorkers()
        res = run_batches(fake, pages=60, precached=[(1, 15), (31, 45)], workers=workers)
        assert sorted(fake.ranges()) == [(16, 30), (46, 60)], workers
        assert done_values(res) == [("convert", i, 4) for i in range(1, 5)], workers
        messages = res.log.messages()
        assert "Pages 1-15 cached" in messages and "Pages 31-45 cached" in messages
        assert messages.index("Pages 31-45 cached") < min(i for i, m in enumerate(messages) if " done (" in m)
        assert page_texts(res.doc) == expected_texts(60)


def test_not_converted_in_parallel_stops_the_run() -> None:
    """(16,30) fails first, (1,15) fails later: the error names the lowest range and no further batch is started
    (at most `workers` are in flight, the next is submitted only after a result was handled). The gate keeps (1,15)
    running until the calling thread has handled (16,30), so the outcome does not depend on timing."""
    fake = FakeWorkers({(16, 30, 1): 3, (1, 15, 1): Gated(3)})
    res = run_batches(fake, pages=90, workers=2, gate_after_first=True, expect_error=True)
    assert isinstance(res.error, RuntimeError) and str(res.error) == (
        "Pages 1-15: Docling did not convert them completely (see log)"), res.error
    assert sorted(fake.ranges()) == [(1, 15), (16, 30)], fake.ranges()
    assert res.files == []
    for first, last in fake.ranges():  # the output of both is still logged
        assert f"=== pages {first}-{last} ===\n".encode() in res.stdout


def test_running_batches_finish_when_another_is_not_converted() -> None:
    """(1,15) fails at once while (16,30) is running: (16,30) finishes and stays cached, (31,45)+ never start."""
    fake = FakeWorkers({(1, 15, 1): 3, (16, 30, 1): Gated(0)})
    res = run_batches(fake, pages=60, workers=2, gate_after_first=True, expect_error=True)
    assert isinstance(res.error, RuntimeError) and str(res.error).startswith("Pages 1-15: "), res.error
    assert sorted(fake.ranges()) == [(1, 15), (16, 30)] and res.files == ["0016-0030.json"]
    assert [m for m in res.log.messages() if " done (" in m][0].startswith("Pages 16-30 done")


def test_exception_in_this_thread_starts_no_further_batch() -> None:
    """progress() raising on its first call (as a broken pipe to the service would): the exception propagates and
    only the batches already in flight ran."""
    def boom() -> None:
        raise ValueError("progress failed")

    fake = FakeWorkers()
    res = run_batches(fake, pages=60, workers=2, expect_error=True, on_progress=boom)
    assert isinstance(res.error, ValueError) and str(res.error) == "progress failed", res.error
    assert len(res.progress) == 1 and set(fake.ranges()) <= {(1, 15), (16, 30)} and len(fake.ranges()) <= 2


def test_crashed_batch_is_retried_alone() -> None:
    for crash in (137, -9):
        fake = FakeWorkers({(16, 30, 1): crash})
        res = run_batches(fake, pages=30, workers=2)
        assert fake.ranges()[-1] == (16, 30) and fake.ranges().count((16, 30)) == 2, fake.ranges()
        assert [c["stdout"] for c in fake.calls if c["range"] == (16, 30)] == ["pipe", "inherit"]
        warnings = [r.getMessage() for r in res.log.records if r.levelno == logging.WARNING]
        assert warnings == [f"Pages 16-30: worker crashed in parallel mode (exit code {crash})"], warnings
        messages = res.log.messages()
        done = [m for m in messages if " done (" in m]
        assert len(done) == 2 and done[-1].startswith("Pages 16-30 done"), done
        retry = messages.index("Pages 16-30: retrying alone after a crash in parallel mode")
        assert messages.index(done[0]) < retry < messages.index(done[1])
        assert done_values(res) == [("convert", i, 2) for i in range(1, 3)]
        assert page_texts(res.doc) == expected_texts(30)
        assert b"=== pages 16-30 (retried alone) ===\n" in res.stdout
        assert res.stdout.count(b"=== pages 16-30 ===\n") == 1


def test_after_a_crash_no_batch_starts_in_the_pool() -> None:
    """(1,15) crashes while (16,30) is in flight (held by the gate until the crash was handled): the pool starts
    nothing more, then the crashed batch and every batch not yet started run alone, in page order."""
    fake = FakeWorkers({(1, 15, 1): 137, (16, 30, 1): Gated(0)})
    res = run_batches(fake, pages=75, workers=2, gate_after_first=True)
    assert [(c["range"], c["stdout"]) for c in fake.calls[:2]] in (
        [((1, 15), "pipe"), ((16, 30), "pipe")], [((16, 30), "pipe"), ((1, 15), "pipe")]), fake.calls[:2]
    assert [(c["range"], c["attempt"], c["stdout"]) for c in fake.calls[2:]] == [
        ((1, 15), 2, "inherit"), ((31, 45), 1, "inherit"), ((46, 60), 1, "inherit"), ((61, 75), 1, "inherit")]
    messages = res.log.messages()
    assert "Pages 1-15: retrying alone after a crash in parallel mode" in messages
    for pages in ("31-45", "46-60", "61-75"):
        assert f"Pages {pages}: converting alone after a crash in parallel mode" in messages
    assert [m.split(" done")[0] for m in messages if " done (" in m] == [
        "Pages 16-30", "Pages 1-15", "Pages 31-45", "Pages 46-60", "Pages 61-75"]
    assert done_values(res) == [("convert", i, 5) for i in range(1, 6)] and page_texts(res.doc) == expected_texts(75)
    assert res.stdout.count(b"(retried alone)") == 1  # only the crashed batch gets that header


def test_not_converted_wins_over_a_crash() -> None:
    """One batch crashes and another is not converted in the same run: NOT_CONVERTED is raised, no retry happens."""
    fake = FakeWorkers({(1, 15, 1): 137, (16, 30, 1): 3})
    res = run_batches(fake, pages=60, workers=2, expect_error=True)
    assert isinstance(res.error, RuntimeError) and str(res.error).startswith("Pages 16-30: "), res.error
    assert sorted(fake.ranges()) == [(1, 15), (16, 30)] and res.files == []
    assert not any("retrying alone" in m for m in res.log.messages())


def test_a_failing_log_file_starts_no_further_batch() -> None:
    """The log file cannot be written (its folder does not exist): the OSError propagates from this thread."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = FakeWorkers()
        res = run_batches(fake, pages=60, workers=2, subprocess_log=Path(tmp) / "missing" / "convert.log",
                          expect_error=True)
        assert isinstance(res.error, OSError), res.error
        assert len(fake.calls) <= 2 and set(fake.ranges()) <= {(1, 15), (16, 30)}, fake.ranges()


def test_one_uncached_batch_takes_the_serial_path() -> None:
    """workers=2 but only one batch left to convert: nothing can run in parallel, so it is today's path."""
    fake = FakeWorkers()
    res = run_batches(fake, pages=30, precached=[(1, 15)], workers=2)
    assert fake.ranges() == [(16, 30)] and [c["stdout"] for c in fake.calls] == ["inherit"]
    assert not any(m.startswith("Converting") for m in res.log.messages()) and b"=== pages" not in res.stdout
    assert done_values(res) == [("convert", 1, 2), ("convert", 2, 2)]


CHILD = (
    "import shutil, sys\n"
    "from pathlib import Path\n"
    "batches, prepared, first, last = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]\n"
    "name = f'{int(first):04d}-{int(last):04d}.json'\n"
    "sys.stdout.buffer.write(f'child line 1 of {first}-{last}\\n'.encode())\n"
    "sys.stdout.buffer.flush()\n"
    "sys.stderr.buffer.write(f'child line 2 of {first}-{last}\\n'.encode())\n"
    "sys.stderr.buffer.flush()\n"
    "shutil.copy(prepared / name, batches / (name + '.tmp'))\n"
    "(batches / (name + '.tmp')).replace(batches / name)\n"
)


def test_real_subprocesses_through_real_pipes() -> None:
    """No fake subprocess.run: small Python children behind real pipes, two at a time. Each batch's output (stdout and
    stderr merged) appears once, whole, under its header; the document comes out in page order."""
    ranges = [(1, 15), (16, 30), (31, 45), (46, 60)]
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src, prepared, log_file = tmp / "source.pdf", tmp / "prepared", tmp / "convert.log"
        prepared.mkdir()
        make_pdf(src, 60)
        for first, last in ranges:
            write_batch(prepared, first, last)
        script = tmp / "fake_batch_worker.py"
        script.write_text(CHILD, encoding="utf-8")
        saved = cp._worker_command
        cp._worker_command = lambda src, out, layout, batches, first, last, password: (
            [sys.executable, str(script), str(batches), str(prepared), str(first), str(last)], dict(os.environ))
        try:
            doc, n_pages = cp.convert_batches(src, tmp / "out", batch_root=tmp / "batches", workers=2,
                                              subprocess_log=log_file)
        finally:
            cp._worker_command = saved
        data = log_file.read_bytes()
    assert n_pages == 60 and page_texts(doc) == expected_texts(60)
    assert data.count(b"=== pages ") == 4
    for first, last in ranges:
        block = (f"=== pages {first}-{last} ===\n" f"child line 1 of {first}-{last}\n"
                 f"child line 2 of {first}-{last}\n").encode()
        assert data.count(block) == 1, data


def test_retry_alone_has_its_own_header_in_the_log_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "convert.log"
        res = run_batches(FakeWorkers({(16, 30, 1): 137}), pages=30, workers=2, subprocess_log=log_file)
        text = log_file.read_text(encoding="utf-8")
        retry = text.index("=== pages 16-30 (retried alone) ===\n")
        assert text.index("=== pages 1-15 ===\n") < retry  # after the pool's blocks
        assert text[retry:].split("\n")[1] == "worker line 1 of 16-30"  # its streamed output follows the header
        assert res.stdout == b""


def test_log_blocks_start_on_their_own_line() -> None:
    """A worker killed mid-line must not swallow the next block's header."""
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "convert.log"
        run_batches(FakeWorkers(cut=[(1, 15), (16, 30), (31, 45)]), pages=45, workers=2, subprocess_log=log_file)
        data = log_file.read_bytes()
        headers = [m.start() for m in re.finditer(rb"=== pages ", data)]
        assert len(headers) == 3 and all(i == 0 or data[i - 1:i] == b"\n" for i in headers), data


def test_stdout_without_a_buffer() -> None:
    """A text-only sys.stdout (notebook, redirected stream) gets the decoded blocks instead of failing."""
    res = run_batches(FakeWorkers(), pages=30, workers=2, text_stdout=True)
    assert page_texts(res.doc) == expected_texts(30)
    for first, last in [(1, 15), (16, 30)]:
        assert res.stdout.count(f"=== pages {first}-{last} ===\n".encode() + output_of(first, last)) == 1


def test_crash_that_repeats_fails_after_three_attempts_alone() -> None:
    fake = FakeWorkers({(16, 30, n): 137 for n in range(1, 5)})
    res = run_batches(fake, pages=45, workers=2, expect_error=True)
    assert isinstance(res.error, RuntimeError) and str(res.error) == "Pages 16-30 failed after 3 attempts", res.error
    assert fake.ranges().count((16, 30)) == 4  # one attempt in the pool, three alone


def test_error_in_a_pool_task_is_reraised() -> None:
    fake = FakeWorkers({(1, 15, 1): OSError("cannot start"), (16, 30, 1): Gated(0)})
    res = run_batches(fake, pages=30, workers=2, gate_after_first=True, expect_error=True)
    assert isinstance(res.error, OSError) and str(res.error) == "cannot start", res.error
    assert res.files == ["0016-0030.json"]  # the batch that was running finishes and stays cached


def test_password_and_validation() -> None:
    for workers in (1, 2):
        fake = FakeWorkers()
        res = run_batches(fake, pages=30, workers=workers, password="secret")
        assert len(fake.calls) == 2
        for call in fake.calls:
            assert call["password"] == "secret" and "secret" not in call["cmd"]  # environment only, never the cmd
            assert call["env"] == {**res.environ, "PDF_PASSWORD": "secret"} and "--threads" not in call["cmd"]
    for workers in (0, -1):
        fake = FakeWorkers()
        res = run_batches(fake, pages=30, expect_error=True, workers=workers)
        assert isinstance(res.error, ValueError) and not fake.calls, workers


def test_parallel_output_goes_to_the_log_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "convert.log"
        log_file.write_bytes(b"earlier output\n")
        fake = FakeWorkers()
        res = run_batches(fake, pages=45, workers=2, subprocess_log=log_file)
        data = log_file.read_bytes()
        assert data.startswith(b"earlier output\n") and res.stdout == b""
        for first, last in [(1, 15), (16, 30), (31, 45)]:
            assert data.count(output_of(first, last)) == 1
            assert data.count(f"=== pages {first}-{last} ===\n".encode() + output_of(first, last)) == 1
        assert data.count(b"=== pages ") == 3


def test_single_worker_streams_to_the_log_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "convert.log"
        fake = FakeWorkers()
        run_batches(fake, pages=30, subprocess_log=log_file)
        assert [c["stdout"] for c in fake.calls] == ["file", "file"]
        text = log_file.read_text(encoding="utf-8")
        assert text.count("worker line 1 of 1-15") == 1 and text.count("worker line 1 of 16-30") == 1
        assert "=== pages" not in text  # streamed as today, no headers


def test_command_line_options() -> None:
    """--workers reaches convert_document; worker mode calls convert_chunk as before; there is no --threads."""
    seen = {}
    root = logging.getLogger()
    saved = (cp.main_pdf, cp.convert_chunk, cp.convert_document, sys.argv, root.level, root.handlers[:])
    cp.main_pdf = lambda: "unused.pdf"
    cp.convert_chunk = lambda *a, **k: seen.update(chunk=(a, k))
    cp.convert_document = lambda *a, **k: seen.update(document=(a, k))
    try:
        sys.argv = ["convert_protocol.py", "x.pdf", "--batch-dir", "b", "--range", "1", "15"]
        cp.main()
        assert len(seen["chunk"][0]) == 6 and seen["chunk"][0][1:3] == (1, 15) and not seen["chunk"][1], seen["chunk"]
        sys.argv = ["convert_protocol.py", "x.pdf", "--workers", "2"]
        cp.main()
        assert seen["document"][1]["convert_workers"] == 2 and "convert_threads" not in seen["document"][1]
        sys.argv = ["convert_protocol.py", "x.pdf"]
        cp.main()
        assert seen["document"][1]["convert_workers"] == 1
        seen.clear()
        for extra, message in ((["--threads", "3"], "unrecognized arguments"),  # the option no longer exists
                               (["--workers", "0"], "must be at least 1"), (["--workers", "-2"], "must be at least 1")):
            sys.argv = ["convert_protocol.py", "x.pdf", *extra]
            stderr = io.StringIO()
            try:
                with redirect_stderr(stderr):
                    cp.main()
            except SystemExit as exc:  # argparse rejects it before anything runs (no output folder is claimed)
                assert exc.code == 2 and message in stderr.getvalue(), (extra, stderr.getvalue())
            else:
                raise AssertionError(f"{extra} was accepted")
        assert not seen
    finally:
        cp.main_pdf, cp.convert_chunk, cp.convert_document, sys.argv, root.level = saved[:5]
        root.handlers[:] = saved[5]


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed} tests passed" if not failed else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
