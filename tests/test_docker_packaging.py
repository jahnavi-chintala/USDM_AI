"""Self-test for the Docker packaging: `python tests/test_docker_packaging.py` (no pytest, no Docker, offline).

Covers: the .dockerignore matcher of scripts/check_build_context.py against Docker's rules and on synthetic
trees (exceptions, **, parent folders; confidential folders pruned without being walked); the real build context
(PDF-free, exactly service.runtime.context_files(), .dockerignore generated from it); service/runtime.py's copy
commands the Dockerfile runs; the in-build check docker/check_context.sh on synthetic trees (skipped without a
POSIX sh); requirements.in / requirements.lock / requirements.txt in step and covering every third-party import;
docker-compose.yml, .env.example and the Dockerfile's isolation, offline and non-root settings; fetch_models.py in
step with convert_protocol / protocol_api, and its network guard. Synthetic trees hold made-up names only;
output: counts and booleans.
"""

import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from service import runtime  # noqa: E402


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cbc = load(ROOT / "scripts" / "check_build_context.py", "check_build_context")
lock_report = load(ROOT / "docker" / "lock_report.py", "lock_report")
fetch_models = load(ROOT / "docker" / "fetch_models.py", "fetch_models")

WINDOWS_ONLY = {"colorama", "pywin32", "tzdata"}
CUDA = re.compile(r"^(nvidia-|triton$|cuda-)")
DIST_ALIASES = {"docling-slim": "docling"}  # `import docling` is docling-slim, pulled in by the docling package


@contextmanager
def temp_dir():
    tmp = Path(tempfile.mkdtemp(prefix="docker_pkg_"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def make_tree(root: Path, files: list[str]) -> None:
    for f in files:
        (root / f).parent.mkdir(parents=True, exist_ok=True)
        (root / f).write_text("made-up\n", encoding="utf-8")


@contextmanager
def scanned_dirs():
    """Every folder the context walker lists (to prove it never lists a confidential one)."""
    seen: list[str] = []
    real = os.scandir

    def scandir(path="."):
        seen.append(Path(path).as_posix())
        return real(path)

    os.scandir = scandir
    try:
        yield seen
    finally:
        os.scandir = real


def pins(path: Path) -> dict[str, tuple[str, str]]:
    return lock_report.parse_pins(path.read_text(encoding="utf-8"))


def requirement_names(path: Path) -> set[str]:
    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(lock_report.canonical(re.split(r"[\s\[<>=!~;]", line, maxsplit=1)[0]))
    return names


# ---------------------------------------------------------------------------------- 1. .dockerignore matcher


def test_matcher_follows_docker_rules() -> None:
    m = cbc.Matcher(cbc.read_patterns("*\n!keep.py\n!service/api.py\n"))
    assert m.excluded("other.py") and m.excluded(".env") and not m.excluded("keep.py")
    assert not m.excluded("service/api.py") and m.excluded("service/worker.py") and m.excluded("service")
    assert m.exceptions_below("service") == ["service/api.py"] and m.exceptions_below("serv") == []
    # ** spans folders; the last matching pattern wins
    m = cbc.Matcher(cbc.read_patterns("**/*.pdf\n!keep/**\nkeep/deep/*.pdf\n"))
    assert m.excluded("a.pdf") and m.excluded("x/y/z.pdf") and not m.excluded("x/y/z.py")
    assert not m.excluded("keep/a.pdf") and m.excluded("keep/deep/b.pdf") and not m.excluded("keep/deep/b.py")
    # a folder pattern excludes everything below it (parent match); an exception re-includes a subtree
    m = cbc.Matcher(cbc.read_patterns("build\n!build/keep\n"))
    assert m.excluded("build/a.txt") and not m.excluded("build/keep/b.txt") and not m.excluded("buildx/a.txt")
    # anchored at the context root; * and ? never cross "/"; classes; escapes; leading "/" and "./" cleaned;
    # comments only at column 0; BOM and CRLF; trailing spaces trimmed
    text = "﻿# comment\r\n/top?.txt\r\n./a/*.log  \n  # pattern, not comment?\n[ab]c.md\n\\*.txt\nx/../y\n"
    m = cbc.Matcher(cbc.read_patterns(text))
    assert m.excluded("top1.txt") and not m.excluded("top12.txt") and not m.excluded("sub/top1.txt")
    assert m.excluded("a/x.log") and not m.excluded("a/b/x.log") and not m.excluded("x.log")
    assert m.excluded("ac.md") and m.excluded("bc.md") and not m.excluded("cc.md")
    assert m.excluded("*.txt") and not m.excluded("a.txt") and m.excluded("y/z") and not m.excluded("x/y")
    assert m.excluded("# pattern, not comment!") and not m.excluded("comment")
    assert cbc.read_patterns("!/abs/path\n!  spaced\n") == ["!abs/path", "!spaced"]
    # wildcard exceptions are flagged (the whitelist must name files)
    assert cbc.Matcher(cbc.read_patterns("*\n!**/*.py\n")).wildcard_exceptions() == ["**/*.py"]


SYNTHETIC = ["service/api.py", "service/worker.py", "service/__pycache__/api.cpython-313.pyc", "service/notes.pdf",
             "tools/mermaid_validate/validate.mjs", "tools/mermaid_validate/node_modules/pkg/index.js",
             "main_protocol.pdf", "Study Report.PDF", "test_protocols/p1/source.pdf", "output_tests/p1/protocol.json",
             "output/protocol.md", "output_public/x/chunks/chunks.jsonl", "public_protocols/x/protocol.pdf",
             "reports/eval/gold.json", "vlm_cache/a.json", "data/registry.sqlite", ".env", "local_config.json",
             "compact_prompt.md", ".venv/Lib/site.py", "docs/notes.md", "research.py", "Dockerfile"]
WHITELIST = "*\n!service/api.py\n!service/worker.py\n!tools/mermaid_validate/validate.mjs\n!Dockerfile\n"
NEVER_LISTED = {"test_protocols", "output_tests", "output", "output_public", "public_protocols", "data", ".venv",
                "reports", "vlm_cache"}


def listed_forbidden(seen: list[str], root: Path) -> list[str]:
    rel = [Path(s).relative_to(root).as_posix() if Path(s) != root else "" for s in seen]
    return [r for r in rel if set(r.split("/")) & NEVER_LISTED]


def test_synthetic_trees() -> None:
    with temp_dir() as tmp:
        make_tree(tmp, SYNTHETIC)
        with scanned_dirs() as seen:
            ctx = cbc.compute(tmp, WHITELIST)
        assert ctx.files == ["Dockerfile", "service/api.py", "service/worker.py",
                             "tools/mermaid_validate/validate.mjs"], ctx.files
        assert ctx.problems == [] and ctx.pruned == 11, (ctx.problems, ctx.pruned)
        assert not listed_forbidden(seen, tmp) and len(seen) == 4  # root, service, tools, tools/mermaid_validate
        # a PDF and a secret re-included: reported, the PDF by category only
        ctx = cbc.compute(tmp, WHITELIST + "!service/notes.pdf\n!Study Report.PDF\n!service/../.env\n")
        assert sorted(c for c, _ in ctx.problems) == ["PDF", "PDF", "secret or local configuration"], ctx.problems
        assert all(p is None for c, p in ctx.problems if c == "PDF") and ("secret or local configuration", ".env") \
            in ctx.problems
        # no "*": forbidden folders are reported, never walked
        with scanned_dirs() as seen:
            ctx = cbc.compute(tmp, "service/__pycache__\n")
        flagged = {p for c, p in ctx.problems if c == "forbidden folder not pruned by .dockerignore"}
        assert flagged == {"test_protocols", "output_tests", "output", "output_public", "public_protocols", "reports",
                           "vlm_cache", "data", ".venv", "tools/mermaid_validate/node_modules"}, flagged
        assert not listed_forbidden(seen, tmp) and "research.py" in ctx.files and "PDF" in {c for c, _ in ctx.problems}
        # an exception reaching into a confidential folder, or a wildcard exception: refused without walking it
        for text, expect in (("*\n!test_protocols/readme.py\n", {"test_protocols"}),
                             ("*\n!**/*.py\n", None)):
            with scanned_dirs() as seen:
                ctx = cbc.compute(tmp, text)
            flagged = {p for c, p in ctx.problems if c == "forbidden folder not pruned by .dockerignore"}
            assert ctx.problems and not listed_forbidden(seen, tmp), text
            if expect:
                assert flagged == expect, flagged
            else:
                assert any(c.startswith("exception patterns with wildcards") for c, _ in ctx.problems)
                assert "test_protocols" in flagged and "output" in flagged
        assert cbc.compute(tmp, None).problems[0][0].startswith("no .dockerignore")


def test_real_context() -> None:
    code, lines = cbc.check(ROOT)
    assert code == 0, lines
    assert "PDFs: 0" in lines[-2] and lines[-1].startswith("ok:"), lines
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ignore == runtime.dockerignore_text(), "stale .dockerignore: python -m service.runtime --write-dockerignore"
    whitelist = [p[1:] for p in cbc.read_patterns(ignore) if p.startswith("!")]
    assert cbc.read_patterns(ignore)[0] == "*" and whitelist == runtime.context_files()
    assert whitelist == runtime.runtime_files() + runtime.test_files() + runtime.BUILD_FILES
    assert set(runtime.test_files()) == {"tests/test_service_api.py", "tests/test_service_core.py",
                                         "tests/test_service_mcp.py", "tests/test_service_pipeline.py",
                                         "tests/test_service_ui.py", "tests/test_service_zdr.py"}
    ui = [f for f in runtime.runtime_files() if f.startswith(runtime.UI_DIR + "/")]
    assert ui and all(f.endswith(runtime.UI_SUFFIXES) for f in ui) and "service/ui/index.html" in ui
    assert not any(f.lower().endswith(".pdf") or f.startswith(("output", "test_protocols", "reports", "data/"))
                   for f in whitelist)
    assert not ({"eval_v1.py", "eval_answers.py", "gen_questions.py", "regression.py", "answer.py",
                 "test_protocols.py", ".env", "local_config.json", "compact_prompt.md"} & set(whitelist))


def test_test_target_imports_runtime_modules_only() -> None:
    """The test target adds tests only: what they import from the project must be in the runtime image."""
    modules = {f[:-3].replace("/", ".").removesuffix(".__init__") for f in runtime.runtime_files() if f.endswith(".py")}
    tests = {Path(f).stem for f in runtime.test_files()}
    for f in runtime.test_files():
        for name in runtime._imports(ROOT / f):
            top = name.split(".")[0]
            if runtime._module_path(top) is not None and top != "tests":  # a project module
                assert name in modules or top in modules or runtime._module_path(name) is None, (f, name)
                assert top not in runtime.RESEARCH_ONLY, (f, name)
            elif (ROOT / "tests" / f"{top}.py").exists():
                assert top in tests, (f, name)


def test_runtime_copy_commands() -> None:
    """The commands the Dockerfile's context-check stage runs (as a script, from another folder)."""
    with temp_dir() as tmp:
        for flag, expect in (("--copy-to", runtime.runtime_files()), ("--copy-tests-to", runtime.test_files())):
            out = subprocess.run([sys.executable, str(ROOT / "service" / "runtime.py"), flag, str(tmp / flag)],
                                 cwd=tmp, capture_output=True, text=True, timeout=60)
            assert out.returncode == 0, out.stderr[-500:]
            copied = sorted(p.relative_to(tmp / flag).as_posix() for p in (tmp / flag).rglob("*") if p.is_file())
            assert copied == sorted(expect), flag
        out = subprocess.run([sys.executable, "-m", "service.runtime", "--dockerignore"], cwd=ROOT,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0 and out.stdout == runtime.dockerignore_text()
        with redirect_stderr(io.StringIO()) as err:
            missing = runtime.copy_files(["no_such_module.py"], tmp / "x")
        assert missing == 1 and not (tmp / "x").exists() and "no_such_module.py" in err.getvalue()


def find_sh() -> list[str] | None:
    """A POSIX sh with GNU find (Git for Windows' on Windows), or None."""
    if os.name != "nt":
        sh = shutil.which("sh")
        return [sh] if sh else None
    git = shutil.which("git")
    sh = Path(git).resolve().parents[1] / "usr" / "bin" / "sh.exe" if git else None
    return [str(sh)] if sh and sh.exists() else None


def test_in_build_context_check() -> None:
    sh = find_sh()
    if sh is None:
        print("   (skipped: no POSIX sh)")
        return
    env = dict(os.environ)
    env["PATH"] = str(Path(sh[0]).parent) + os.pathsep + env.get("PATH", "")  # GNU find before Windows' find.exe
    clean = ["service/api.py", "tools/mermaid_validate/validate.mjs", "tools/mermaid_validate/package.json",
             "Dockerfile", "requirements.lock", "docker/check_context.sh"]
    cases = {"clean": ([], 0), "pdf": (["sub/Made Up Trial.PDF"], 1), "confidential": (["test_protocols/a.py"], 1),
             "secret": ([".env"], 1), "cache": (["service/__pycache__/a.py"], 1), "data": (["x/chunks.jsonl"], 1),
             "unknown type": (["notes.txt"], 1)}
    for name, (extra, code) in cases.items():
        with temp_dir() as tmp:
            make_tree(tmp, clean + extra)
            out = subprocess.run(sh + [str(ROOT / "docker" / "check_context.sh"), tmp.as_posix()], env=env,
                                 capture_output=True, text=True, timeout=60)
            assert out.returncode == code, (name, out.returncode, out.stderr[-300:])
            text = out.stdout + out.stderr
            assert "Made Up Trial" not in text and "a.py" not in text and "notes.txt" not in text, name
            assert ("ok (6 files" in text) if code == 0 else ("context check failed" in text), (name, text[-300:])


# ---------------------------------------------------------------------------------- 2. requirements


def test_requirements_in_step() -> None:
    txt, lock = pins(ROOT / "requirements.txt"), pins(ROOT / "requirements.lock")
    shared = set(txt) & set(lock)
    assert all(txt[k][1] == lock[k][1] for k in shared), [k for k in shared if txt[k][1] != lock[k][1]]
    assert set(txt) - set(lock) == WINDOWS_ONLY and not set(lock) - set(txt), (set(txt) ^ set(lock))
    assert not [k for k in lock if CUDA.match(k)] and lock["torch"][1] == txt["torch"][1]
    assert "torchvision" in lock and "opencv-python" in lock
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9._-]+==[A-Za-z0-9.]+", line), line  # exact pins, no markers needed
    direct = requirement_names(ROOT / "requirements.in")
    assert direct <= set(lock), direct - set(lock)
    assert {"fastapi", "uvicorn", "mcp", "python-multipart", "jsonschema", "httpx", "docling", "torch"} <= direct


def test_lock_holds_the_linux_closure() -> None:
    """requirements.in's dependency closure for Linux / CPython 3.13, from the installed metadata, is pinned
    (torch's CUDA requirements excepted: the CPU build has none)."""
    from importlib import metadata

    from packaging.requirements import Requirement

    try:
        metadata.distribution("docling")
    except metadata.PackageNotFoundError:
        print("   (skipped: dependencies not installed)")
        return
    env = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
           "python_version": "3.13", "python_full_version": "3.13.6", "implementation_name": "cpython",
           "platform_python_implementation": "CPython"}
    installed = {lock_report.canonical(d.metadata["Name"]): d for d in metadata.distributions()}
    seen, todo, missing = set(), [(n, frozenset()) for n in requirement_names(ROOT / "requirements.in")], set()
    while todo:
        name, extras = todo.pop()
        if (name, extras) in seen:
            continue
        seen.add((name, extras))
        if name not in installed:
            missing.add(name)
            continue
        for spec in installed[name].requires or []:
            req = Requirement(spec)
            if req.marker is None or any(req.marker.evaluate({**env, "extra": e}) for e in (extras or {""})):
                todo.append((lock_report.canonical(req.name), frozenset(req.extras)))
    closure = {n for n, _ in seen} - missing
    lock = pins(ROOT / "requirements.lock")
    assert closure <= set(lock), closure - set(lock)
    assert not {m for m in missing if not CUDA.match(m)}, missing
    assert set(lock) - closure == set(), set(lock) - closure  # nothing pinned that the image does not need


def test_imports_covered_by_requirements_in() -> None:
    from importlib.metadata import packages_distributions

    project = {p.stem for p in ROOT.glob("*.py")} | {"service", "tests"} | {Path(f).stem for f in runtime.test_files()}
    direct = requirement_names(ROOT / "requirements.in")
    by_module = packages_distributions()
    files = [f for f in runtime.runtime_files() + runtime.test_files() if f.endswith(".py")]
    files += [f"docker/{p.name}" for p in (ROOT / "docker").glob("*.py")]
    checked = set()
    for f in files:
        for name in runtime._imports(ROOT / f):
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top in project or top == "__future__" or top in checked:
                continue
            checked.add(top)
            if top in fetch_models.OPTIONAL_IMPORTS:
                continue
            dists = {lock_report.canonical(d) for d in by_module.get(top, [])}
            assert dists, f"{top} (imported by {f}) is not installed"
            assert {DIST_ALIASES.get(d, d) for d in dists} & direct, f"{top} ({sorted(dists)}) not in requirements.in"
    assert {"fastapi", "docling", "torch", "transformers", "mcp", "numpy"} <= checked, checked


def test_lock_report() -> None:
    with temp_dir() as tmp:
        lock = tmp / "requirements.lock"
        lock.write_text("# header line\n#  second\nAlpha==1.0\ntorch==2.0.0\n", encoding="utf-8")
        freeze = tmp / "freeze.txt"
        freeze.write_bytes("alpha==1.0\r\ntorch==2.0.0+cpu\r\nnew_pkg==3.1\r\npip==25.0\r\n".encode("utf-16"))
        found = lock_report.parse_pins(lock_report.read_text(freeze))
        assert found == {"alpha": ("alpha", "1.0"), "torch": ("torch", "2.0.0"), "new-pkg": ("new_pkg", "3.1")}
        assert lock_report.report(lock_report.parse_pins(lock.read_text()), found) == \
            ["not pinned in the lock: new_pkg==3.1"]
        with redirect_stdout(io.StringIO()) as out:
            assert lock_report.main([str(freeze), "--lock", str(lock), "--write"]) == 0
        assert "1 difference(s)" in out.getvalue()
        assert lock.read_text().splitlines() == ["# header line", "#  second", "Alpha==1.0", "new_pkg==3.1",
                                                 "torch==2.0.0"]


# ---------------------------------------------------------------------------------- 3. compose, .env, Dockerfile


def test_compose() -> None:
    import yaml

    spec = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert list(spec["services"]) == ["protocol-service"]
    svc = spec["services"]["protocol-service"]
    assert svc["container_name"] == "protocol-service" and svc["build"]["context"] == "."
    assert svc["build"].get("target") == "production"
    assert svc["ports"] and all(str(p).startswith("127.0.0.1:") for p in svc["ports"]), svc["ports"]
    assert "protocol-data:/data" in svc["volumes"] and "protocol-data" in spec["volumes"]
    assert not any(str(v).startswith((".", "/", "C:", "~")) for v in svc["volumes"])  # no bind mounts
    assert svc["env_file"] in (".env", [".env"]) and svc["environment"]["DATA_DIR"] == "/data"
    assert str(svc["environment"]["HF_HUB_OFFLINE"]) == "1" and "OPENROUTER_OFFLINE" not in svc["environment"]
    hc = svc["healthcheck"]
    assert "/health" in " ".join(hc["test"]) and {"interval", "timeout", "retries", "start_period"} <= set(hc)
    assert int(hc["start_period"].rstrip("s")) >= 30 and svc["restart"] == "unless-stopped"
    assert int(svc["stop_grace_period"].rstrip("s")) > 30  # the worker waits up to 30 s for a running job
    assert "mem_limit" in svc and "Measured" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert not svc.get("init"), "tini is the image's PID 1: no second init"
    assert svc.get("privileged") is not True and "ALL" in svc.get("cap_drop", [])


def config_variables() -> list[str]:
    from service import config

    names = []
    for line in config.__doc__.splitlines():
        m = re.match(r"^  ([A-Z][A-Z_]+(?:, [A-Z][A-Z_]+)*)\s", line)
        if m:
            names += m.group(1).split(", ")
    return names


def test_env_example() -> None:
    names = config_variables()
    assert {"DATA_DIR", "OPENROUTER_API_KEY", "OPENROUTER_OFFLINE", "FIGURE_MODEL", "EMBED_MODEL", "RERANK_MODEL",
            "WORKER_CONCURRENCY", "FIGURE_CONCURRENCY", "MAX_UPLOAD_MB", "LOG_LEVEL", "HOST", "PORT",
            "ALLOWED_HOSTS"} <= set(names), names
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in names:
        assert re.search(rf"^(# )?{name}=", text, re.M), f"{name} not documented in .env.example"
    assigned = re.findall(r"^([A-Z_]+)=(.*)$", text, re.M)
    assert assigned == [("OPENROUTER_API_KEY", "")], assigned  # every default stays commented out
    assert "zero-data-retention" in text


def dockerfile() -> tuple[dict[str, str], list[dict]]:
    """(global ARG defaults, stages [{name, base, lines: [(instruction, args)]}]) with continuations joined."""
    lines, current = [], ""
    for raw in (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if not current and (not raw.strip() or raw.lstrip().startswith("#")):
            continue
        current += raw.rstrip("\\").strip() + " " if raw.rstrip().endswith("\\") else raw.strip()
        if not raw.rstrip().endswith("\\"):
            lines.append(current)
            current = ""
    args, stages = {}, []
    for line in lines:
        instr, _, rest = line.partition(" ")
        instr = instr.upper()
        if instr == "FROM":
            m = re.match(r"(\S+)\s+AS\s+(\S+)", rest, re.I)
            stages.append({"base": m.group(1), "name": m.group(2), "lines": []})
        elif not stages and instr == "ARG":
            k, _, v = rest.partition("=")
            args[k] = v
        else:
            stages[-1]["lines"].append((instr, rest))
    return args, stages


def test_dockerfile() -> None:
    import platform

    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert not re.search(r"^#\s*syntax=", text, re.M), "the built-in frontend: no frontend image to pull"
    args, stages = dockerfile()
    by_name = {s["name"]: s for s in stages}
    assert stages[-1]["name"] == "production" and stages[-1]["base"] == "runtime"
    assert {"context-check", "python-deps", "models", "node-deps", "runtime", "verify", "test"} <= set(by_name)
    py = re.fullmatch(r"python:(\d+\.\d+)\.\d+-slim-[a-z]+", args["PYTHON_IMAGE"])
    assert py and py.group(1) == ".".join(platform.python_version_tuple()[:2]), args["PYTHON_IMAGE"]
    assert re.fullmatch(r"node:22\.\d+\.\d+-[a-z]+-slim", args["NODE_IMAGE"]), args["NODE_IMAGE"]
    assert args["TORCH_INDEX_URL"] == "https://download.pytorch.org/whl/cpu"
    # the whole context is copied in the context-check stage only
    for s in stages:
        for instr, rest in s["lines"]:
            if instr in ("COPY", "ADD") and re.match(r"^(--\S+\s+)*\.\/?\s", rest):
                assert s["name"] == "context-check", (s["name"], rest)
    runtime_stage = by_name["runtime"]
    assert all("--from=" in rest for instr, rest in runtime_stage["lines"] if instr in ("COPY", "ADD"))
    env = " ".join(rest for instr, rest in runtime_stage["lines"] if instr == "ENV")
    for setting in ("HF_HUB_OFFLINE=1", "HF_HOME=/opt/models", "DATA_DIR=/data", "HOST=0.0.0.0", "PORT=8000",
                    "PYTHONUNBUFFERED=1"):
        assert setting in env, setting
    assert "OPENROUTER_OFFLINE" not in env
    users = [rest for instr, rest in runtime_stage["lines"] if instr == "USER"]
    assert users and users[-1].split(":")[0] not in ("root", "0"), users
    get = lambda instr: [rest for i, rest in runtime_stage["lines"] if i == instr]  # noqa: E731
    assert get("EXPOSE") == ["8000"] and json.loads(get("CMD")[0]) == ["python", "-m", "service.api"]
    assert "tini" in get("ENTRYPOINT")[0] and "/health" in get("HEALTHCHECK")[0] and "urllib" in get("HEALTHCHECK")[0]
    assert any("install -d -o 10001 -g 10001" in rest for instr, rest in runtime_stage["lines"] if instr == "RUN")
    for stage in ("production", "test"):  # each depends on the context check and the offline verification
        assert {"context-check", "verify"} <= dependencies(by_name, stage), stage
    verify = " ".join(rest for instr, rest in by_name["verify"]["lines"] if instr == "RUN")
    assert "--network=none" in verify and "--verify --app /app" in verify
    models = [rest for instr, rest in by_name["models"]["lines"] if instr == "RUN"]
    assert any("--download" in r for r in models) and any("--network=none" in r and "--verify" in r for r in models)
    deps = " ".join(rest for instr, rest in by_name["python-deps"]["lines"] if instr == "RUN")
    assert '--no-deps --index-url "$TORCH_INDEX_URL"' in deps and "-c /build/torch-constraints.txt" in deps
    assert "nvidia-" in deps and "torch.version.cuda" in deps and "pip check" in deps
    assert "build-essential" not in " ".join(rest for instr, rest in runtime_stage["lines"])
    assert "npm ci --omit=dev" in " ".join(rest for instr, rest in by_name["node-deps"]["lines"])


def dependencies(by_name: dict, stage: str) -> set[str]:
    """Stages a stage needs: its FROM parent and COPY --from sources, transitively."""
    out, todo = set(), [stage]
    while todo:
        s = by_name[todo.pop()]
        direct = {s["base"]} | {m for instr, rest in s["lines"] if instr == "COPY"
                                for m in re.findall(r"--from=(\S+)", rest)}
        for d in direct & set(by_name):
            if d not in out:
                out.add(d)
                todo.append(d)
    return out


# ---------------------------------------------------------------------------------- 4. fetch_models.py


def test_fetch_models_in_step() -> None:
    from docling.datamodel.base_models import InputFormat

    import convert_protocol
    import protocol_api

    assert fetch_models.LAYOUT_PRESET == convert_protocol.DEFAULT_LAYOUT
    assert fetch_models.TOKENIZER == protocol_api.TOKENIZER
    real = convert_protocol.build_converter().format_to_options[InputFormat.PDF].pipeline_options
    mine = fetch_models.pipeline_options()
    for path in ("layout_options.model_spec", "layout_options.engine_options", "do_ocr", "ocr_options.kind",
                 "ocr_options.backend", "ocr_options.lang", "do_table_structure", "table_structure_options",
                 "do_picture_classification", "picture_classification_options", "accelerator_options",
                 "do_code_enrichment", "do_formula_enrichment", "do_picture_description", "do_chart_extraction"):
        a, b = real, mine
        for part in path.split("."):
            a, b = getattr(a, part), getattr(b, part)
        assert a == b, path
    from huggingface_hub import try_to_load_from_cache

    cached = try_to_load_from_cache(fetch_models.TOKENIZER, "tokenizer.json")
    if isinstance(cached, str):  # the host's tokenizer is the one the image pins
        snapshot = Path(cached).parent
        assert snapshot.name == fetch_models.TOKENIZER_REVISION, snapshot.name
        assert sorted(p.name for p in snapshot.iterdir()) == sorted(fetch_models.TOKENIZER_FILES)
    else:
        print("   (tokenizer snapshot check skipped: not in the local Hugging Face cache)")


GUARD = """
import importlib.util, socket, sys
spec = importlib.util.spec_from_file_location("fm", sys.argv[1])
fm = importlib.util.module_from_spec(spec); spec.loader.exec_module(fm)
def reached(self, address):
    raise AssertionError("real connect reached")
socket.socket.connect = reached
fm.block_network()
results = []
for call in (lambda: socket.getaddrinfo("huggingface.co", 443), lambda: socket.getaddrinfo("203.0.113.7", 80),
             lambda: socket.socket().connect(("203.0.113.7", 80)), lambda: socket.socket().connect(("127.0.0.1", 9))):
    try:
        call(); results.append("passed")
    except AssertionError:
        results.append("local")
    except OSError as exc:
        results.append(type(exc).__name__)
print(",".join(results))
"""


def test_fetch_models_network_guard() -> None:
    """--verify refuses connections before any packet: the real connect is never reached (it would raise)."""
    out = subprocess.run([sys.executable, "-c", GUARD, str(ROOT / "docker" / "fetch_models.py")],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.strip() == "gaierror,gaierror,ConnectionRefusedError,local", out.stdout


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    start_all = time.time()
    for t in tests:
        start = time.time()
        try:
            t()
            print(f"ok {t.__name__} ({time.time() - start:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"{len(tests) - failed} passed in {time.time() - start_all:.1f}s" if not failed
          else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
