"""Which project files the service needs at runtime (the Docker image copies exactly these).

RUNTIME_ENTRY lists the modules the service runs; runtime_files() adds every project module they import,
directly or not (static scan of import statements, including imports inside functions), plus the non-Python
files they use. Evaluation, gold-set, regression and report scripts are research code: not in the image.

The Docker build context is exactly context_files(): the runtime files, the service self-tests of the image's
test target (test_files()) and the build inputs (BUILD_FILES). .dockerignore is generated from it (a whitelist),
and the Dockerfile copies the runtime and test files out of the context with --copy-to / --copy-tests-to, so this
module is the single list of what the image holds. It needs only the standard library (the build runs it as
`python service/runtime.py` before any package is installed).

  python -m service.runtime                       # print the runtime files
  python -m service.runtime --check               # exit 1 if an entry imports a research-only module
  python -m service.runtime --write-dockerignore  # regenerate .dockerignore (after adding a runtime file)
  python -m service.runtime --dockerignore        # print it
  python service/runtime.py --copy-to DIR         # copy the runtime files into DIR (Docker build)
  python service/runtime.py --copy-tests-to DIR   # copy the test target's files into DIR (Docker build)
"""

from __future__ import annotations

import argparse
import ast
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RUNTIME_ENTRY = [
    "convert_protocol", "hybrid_backend", "protocol_postprocess", "protocol_settings", "figure_ocr", "figure_vlm",
    "text_reconcile", "table_lattice", "table_merge", "structure", "chunk_protocol", "glossary", "parents",
    "retrieve", "soa", "protocol_api", "embed_chunks", "validate_output", "project_paths", "pipeline_version",
    "service", "service.config", "service.contracts", "service.pipeline", "service.storage", "service.registry",
    "service.worker", "service.runtime", "service.import_existing", "service.app_service", "service.api",
    "service.mcp_server", "service.mcp_stdio",
]
# Never in the image: evaluation, gold sets, regression, private-protocol harness. (keyterm_match,
# eval_retrieval and usdm_gold are small, data-free and used at runtime: quote_check's normalisation, search's
# query-vector cache, get_soa's numeric visit matching.)
RESEARCH_ONLY = {"eval_v1", "eval_answers", "gen_questions", "regression", "answer", "test_protocols"}
# Imports inside command-line / evaluation-only functions, never reached by the service.
LAZY_RESEARCH_EDGES = {
    ("figure_ocr", "test_protocols"),   # figure_ocr.main(): the private-protocol counts command
    ("soa", "test_protocols"),          # soa main(): command line
    ("parents", "eval_v1"),             # eval adapters (parents.env_factory and friends)
}
DATA_FILES = ["tools/mermaid_validate/package.json", "tools/mermaid_validate/package-lock.json",
              "tools/mermaid_validate/validate.mjs"]
# The demo UI (static files served at /ui by service.api); every file in the folder, hidden files excluded.
UI_DIR = "service/ui"
UI_SUFFIXES = (".html", ".css", ".js", ".svg", ".png", ".ico")
# Build inputs that are not runtime files (in the build context, never in the production image).
BUILD_FILES = ["Dockerfile", "requirements.in", "requirements.lock", "docker/check_context.sh",
               "docker/fetch_models.py", "docker/lock_report.py"]
# The offline service self-tests of the image's test target (they import runtime modules and each other).
TEST_GLOB = "tests/test_service_*.py"


def _module_path(name: str) -> Path | None:
    parts = name.split(".")
    for candidate in (ROOT.joinpath(*parts).with_suffix(".py"), ROOT.joinpath(*parts) / "__init__.py"):
        if candidate.exists():
            return candidate
    return None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
            out.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "__import__" and node.args \
                and isinstance(node.args[0], ast.Constant):
            out.add(str(node.args[0].value))
    return out


def closure(entry: list[str] | None = None) -> dict[str, set[str]]:
    """Project module -> the project modules it imports, for everything reachable from `entry`."""
    seen: dict[str, set[str]] = {}
    todo = list(entry or RUNTIME_ENTRY)
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        path = _module_path(name)
        if path is None:
            continue
        local = {m for m in _imports(path) if _module_path(m) is not None
                 and (name, m.split(".")[0]) not in LAZY_RESEARCH_EDGES}
        seen[name] = local
        todo.extend(local - set(seen))
    return seen


def ui_files() -> list[str]:
    """The UI's static files (UI_SUFFIXES only: anything else in the folder is not shipped)."""
    base = ROOT / UI_DIR
    return sorted(p.relative_to(ROOT).as_posix() for p in base.rglob("*")
                  if p.is_file() and p.suffix.lower() in UI_SUFFIXES
                  and not any(part.startswith(".") for part in p.relative_to(base).parts)) if base.is_dir() else []


def runtime_files() -> list[str]:
    files = {_module_path(m).relative_to(ROOT).as_posix() for m in closure()}
    return sorted(files) + DATA_FILES + ui_files()


def test_files() -> list[str]:
    return sorted(p.relative_to(ROOT).as_posix() for p in ROOT.glob(TEST_GLOB))


def context_files() -> list[str]:
    """Every file of the Docker build context, nothing else."""
    return runtime_files() + test_files() + BUILD_FILES


_PLAIN = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-/")


def dockerignore_text() -> str:
    """The .dockerignore whitelist: exclude everything, re-include context_files() by exact path. A name with
    characters .dockerignore treats as pattern syntax (*, ?, [, \\, spaces) is refused rather than escaped."""
    files = context_files()
    odd = [f for f in files if not set(f) <= _PLAIN]
    if odd:
        raise ValueError(f"file names with pattern characters cannot be whitelisted: {odd}")
    head = ["# Generated by `python -m service.runtime --write-dockerignore`; do not edit by hand.",
            "# A whitelist: everything is excluded, then exactly service.runtime.context_files() is re-included",
            "# (runtime modules, the self-tests of the image's test target, build inputs), so protocol PDFs,",
            "# outputs, reports, caches, .env, local_config.json and research code never enter the build context.",
            "# Check before every build: python scripts/check_build_context.py",
            "*"]
    return "\n".join(head + [f"!{f}" for f in files]) + "\n"


def copy_files(files: list[str], dest: Path) -> int:
    """Copy `files` (relative to the project root) into dest, keeping their paths; all must exist."""
    missing = [f for f in files if not (ROOT / f).is_file()]
    if missing:
        print("missing from the build context:", missing, file=sys.stderr)
        return 1
    for f in files:
        (dest / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f, dest / f)
    print(f"copied {len(files)} files to {dest}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m service.runtime",
                                     description="The project files the service image holds.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 if an entry imports a research-only module")
    mode.add_argument("--dockerignore", action="store_true", help="print the .dockerignore whitelist")
    mode.add_argument("--write-dockerignore", action="store_true", help="write .dockerignore in the project root")
    mode.add_argument("--copy-to", type=Path, metavar="DIR", help="copy the runtime files into DIR")
    mode.add_argument("--copy-tests-to", type=Path, metavar="DIR", help="copy the test target's files into DIR")
    args = parser.parse_args(argv)
    if args.check:
        graph = closure()
        research = sorted({m for m, deps in graph.items() for d in deps if d.split(".")[0] in RESEARCH_ONLY}
                          | {m for m in graph if m.split(".")[0] in RESEARCH_ONLY})
        if research:
            print("runtime modules reaching research-only code:", research)
            return 1
        print(f"ok: {len(graph)} runtime modules, none research-only")
        return 0
    if args.dockerignore:
        sys.stdout.write(dockerignore_text())
        return 0
    if args.write_dockerignore:
        (ROOT / ".dockerignore").write_text(dockerignore_text(), encoding="utf-8", newline="\n")
        print(f"wrote .dockerignore: {len(context_files())} files whitelisted")
        return 0
    if args.copy_to:
        return copy_files(runtime_files(), args.copy_to)
    if args.copy_tests_to:
        return copy_files(test_files(), args.copy_tests_to)
    print("\n".join(runtime_files()))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
