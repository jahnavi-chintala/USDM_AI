"""Host-side check of the Docker build context, run before `docker build` (it reads no file content).

  python scripts/check_build_context.py

Computes the files Docker would send with this project's .dockerignore, by Docker's rules (moby/patternmatcher
as BuildKit applies them): patterns are relative to the context root and cleaned (a leading "/" is dropped);
`*` and `?` never cross "/", `**` spans any number of folders, `[...]` is a class, `\\` escapes the next
character; "!" re-includes; the last matching pattern wins; a path is matched when it or one of its parent
folders matches; an excluded folder is walked only when an exception pattern names something below it (BuildKit
prunes it otherwise). Fails (exit 1) on:
  * any PDF in the context (any case);
  * a forbidden path: data / output / private / cache folders, .env*, local_config.json, compact_prompt.md,
    databases, embeddings, chunk files, compiled files;
  * a forbidden folder the patterns would not prune (it is never walked here: its contents are confidential), or
    an exception pattern with wildcards (the whitelist names files, which keeps pruning decidable);
  * a context that differs from service.runtime.context_files(): a runtime file missing (stale .dockerignore:
    python -m service.runtime --write-dockerignore) or an unexpected file.
Output: counts and categories; a path is printed only when it is neither a PDF nor in a forbidden folder.
The build repeats a content-free version of this check inside Docker (docker/check_context.sh).
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Folder names never allowed in the context, at any depth; the first three hold confidential documents.
CONFIDENTIAL_DIRS = {"test_protocols", "output_tests", "output"}
FORBIDDEN_DIRS = CONFIDENTIAL_DIRS | {
    "output_public", "public_protocols", "reports", "vlm_cache", "render_comparison", "data", "_chunks",
    ".venv", "venv", ".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".claude", ".idea", ".vscode"}
SECRET_NAMES = {".env", "local_config.json", "compact_prompt.md"}
DATA_SUFFIXES = {".sqlite", ".sqlite-wal", ".sqlite-shm", ".db", ".npy", ".npz", ".jsonl", ".pyc"}
WILDCARDS = set("*?[\\")


# ------------------------------------------------------------------------------------------- .dockerignore


def read_patterns(text: str) -> list[str]:
    """Docker's .dockerignore reader: a BOM is dropped, lines starting with "#" are comments, blank lines
    skipped, whitespace trimmed, each pattern cleaned with a leading "/" removed ("!" kept in front)."""
    out = []
    for i, line in enumerate(text.splitlines()):
        if i == 0:
            line = line.lstrip("﻿")
        if line.startswith("#"):
            continue
        line = line.strip()
        if not line:
            continue
        invert = line.startswith("!")
        if invert:
            line = line[1:].strip()
        if line:
            line = posixpath.normpath(line)
            if len(line) > 1 and line.startswith("/"):
                line = line.lstrip("/") or "/"
        out.append(("!" if invert else "") + line)
    return out


def _regex(pattern: str) -> re.Pattern:
    """patternmatcher's translation of a cleaned pattern to a regular expression."""
    out, i, n = "^", 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                i += 2
                if i < n and pattern[i] == "/":  # "**/" is "**"
                    i += 1
                out += ".*" if i >= n else "(.*/)?"
                continue
            out += "[^/]*"
        elif ch == "?":
            out += "[^/]"
        elif ch == "\\":
            i += 1
            out += re.escape(pattern[i]) if i < n else r"\\"
        elif ch in "[]":
            out += ch
        else:
            out += re.escape(ch)
        i += 1
    return re.compile(out + "$")


@dataclass
class Pattern:
    text: str  # cleaned, without "!"
    exclusion: bool  # a "!" pattern (re-includes)
    regex: re.Pattern


class Matcher:
    def __init__(self, patterns: list[str]) -> None:
        self.patterns = [Pattern(p[1:] if p.startswith("!") else p, p.startswith("!"),
                                 _regex(p[1:] if p.startswith("!") else p)) for p in patterns]

    def excluded(self, path: str) -> bool:
        """patternmatcher.MatchesOrParentMatches: True when Docker leaves `path` out of the context."""
        matched = False
        parent = posixpath.dirname(path)
        parents = parent.split("/") if parent else []
        for p in self.patterns:
            if p.exclusion != matched:  # an exclude only matters while unmatched, an exception while matched
                continue
            hit = bool(p.regex.match(path)) or any(p.regex.match("/".join(parents[:i + 1]))
                                                   for i in range(len(parents)))
            if hit:
                matched = not p.exclusion
        return matched

    def wildcard_exceptions(self) -> list[str]:
        return [p.text for p in self.patterns if p.exclusion and set(p.text) & WILDCARDS]

    def exceptions_below(self, folder: str) -> list[str]:
        """Exception patterns naming something inside `folder` (BuildKit walks an excluded folder only then)."""
        return [p.text for p in self.patterns if p.exclusion and (p.text + "/").startswith(folder + "/")]


# ------------------------------------------------------------------------------------------- the context


def category(path: str) -> str | None:
    """Why a file must not be in the context, or None."""
    name = path.rsplit("/", 1)[-1]
    lower = name.lower()
    if lower.endswith(".pdf"):
        return "PDF"
    if name in SECRET_NAMES or name.startswith(".env."):
        return "secret or local configuration"
    if any(lower.endswith(s) for s in DATA_SUFFIXES):
        return "data or cache file"
    return None


@dataclass
class Context:
    files: list[str]  # what Docker would send
    problems: list[tuple[str, str | None]]  # (category, printable path or None)
    pruned: int  # forbidden folders left out without being walked


def compute(root: Path, dockerignore: str | None) -> Context:
    """The files Docker would send for `root` with this .dockerignore text (None: no .dockerignore)."""
    matcher = Matcher(read_patterns(dockerignore or ""))
    problems: list[tuple[str, str | None]] = []
    if dockerignore is None:
        problems.append(("no .dockerignore: Docker would send the whole folder", None))
    wild = matcher.wildcard_exceptions()
    if wild:
        problems.append((f"exception patterns with wildcards ({len(wild)}): the whitelist must name files", None))
    files, pruned, pending = [], 0, [""]
    while pending:
        folder = pending.pop()
        with os.scandir(root / folder if folder else root) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            rel = f"{folder}/{entry.name}" if folder else entry.name
            excluded = matcher.excluded(rel)
            if entry.is_dir(follow_symlinks=False):
                below = matcher.exceptions_below(rel)
                if entry.name in FORBIDDEN_DIRS:
                    if excluded and not below and not wild:
                        pruned += 1
                    else:  # never walked; the folder name is printed, never what is inside
                        shown = entry.name if entry.name in CONFIDENTIAL_DIRS else rel
                        problems.append(("forbidden folder not pruned by .dockerignore", shown))
                    continue
                if not excluded or below or wild:
                    pending.append(rel)
            elif not excluded:
                files.append(rel)
    for f in files:
        cat = category(f)
        if cat:
            problems.append((cat, None if cat == "PDF" else f))
    return Context(sorted(files), problems, pruned)


def check(root: Path = ROOT) -> tuple[int, list[str]]:
    """(exit code, report lines) for the project at `root`."""
    sys.path.insert(0, str(root))
    from service import runtime

    ignore = root / ".dockerignore"
    ctx = compute(root, ignore.read_text(encoding="utf-8") if ignore.is_file() else None)
    expected = set(runtime.context_files())
    found = set(ctx.files)
    lines, failed = [], bool(ctx.problems)
    pdfs = sum(1 for cat, _ in ctx.problems if cat == "PDF")
    for cat in sorted({c for c, _ in ctx.problems}):
        shown = sorted({p for c, p in ctx.problems if c == cat and p})
        count = sum(1 for c, _ in ctx.problems if c == cat)
        lines.append(f"FAIL {cat}: {count}" + (f" ({', '.join(shown)})" if shown else ""))
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    if missing:
        failed = True
        lines.append(f"FAIL whitelisted files missing from the context ({len(missing)}): {', '.join(missing)}; "
                     "regenerate .dockerignore: python -m service.runtime --write-dockerignore")
    printable = [f for f in extra if category(f) != "PDF"]
    if extra:
        failed = True
        lines.append(f"FAIL files outside service.runtime.context_files() in the context ({len(extra)})"
                     + (f": {', '.join(printable)}" if printable else ""))
    n_runtime, n_tests = len(runtime.runtime_files()), len(runtime.test_files())
    lines.append(f"build context: {len(ctx.files)} files (runtime {n_runtime}, tests {n_tests}, build "
                 f"{len(runtime.BUILD_FILES)}); PDFs: {pdfs}; forbidden folders pruned unwalked: {ctx.pruned}")
    lines.append("context check failed" if failed else
                 "ok: no PDF, no forbidden path, the context is exactly service.runtime.context_files()")
    return (1 if failed else 0), lines


def main() -> int:
    code, lines = check()
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
