"""Compare installed packages with requirements.lock, and refresh the lock from a built image.

  python docker/lock_report.py                      # this interpreter's packages (the Docker build runs this)
  docker run --rm protocol-service pip freeze > freeze.txt
  python docker/lock_report.py freeze.txt           # a pip freeze of the image (PowerShell's UTF-16 is read too)
  python docker/lock_report.py freeze.txt --write   # rewrite the lock's pins from it (the header is kept)

Reports packages installed but not pinned (pip resolved them during the build: dependencies only Linux needs),
pinned but not installed, and version differences. Local version labels are ignored: torch 2.14.1+cpu is pinned
as torch==2.14.1, matching requirements.txt. Only a report: exit 0, or 2 when the input cannot be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

LOCK = Path(__file__).resolve().parents[1] / "requirements.lock"
SKIP = {"pip"}  # the installer itself, not a dependency


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(text: str) -> dict[str, tuple[str, str]]:
    """canonical name -> (name as written, version without local label) for `name==version` lines."""
    pins = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;+]+)(\+\S+)?\s*(;.*)?$", line)
        if m and canonical(m.group(1)) not in SKIP:
            pins[canonical(m.group(1))] = (m.group(1), m.group(2))
    return pins


def read_text(path: Path) -> str:
    """UTF-8, or UTF-16 with BOM (what PowerShell 5.1's `>` writes)."""
    data = path.read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    return data.decode("utf-8-sig")


def installed() -> dict[str, tuple[str, str]]:
    from importlib import metadata

    lines = [f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions() if d.metadata["Name"]]
    return parse_pins("\n".join(lines))


def report(lock: dict, found: dict) -> list[str]:
    out = [f"not pinned in the lock: {found[k][0]}=={found[k][1]}" for k in sorted(set(found) - set(lock))]
    out += [f"pinned, not installed: {lock[k][0]}=={lock[k][1]}" for k in sorted(set(lock) - set(found))]
    out += [f"version differs: {lock[k][0]} lock {lock[k][1]}, installed {found[k][1]}"
            for k in sorted(set(lock) & set(found)) if lock[k][1] != found[k][1]]
    return out


def write_lock(path: Path, lock_text: str, lock: dict, found: dict) -> None:
    header = []
    for line in lock_text.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            break
        header.append(line)
    names = {k: lock[k][0] if k in lock else found[k][0] for k in found}  # keep the lock's spelling
    pins = [f"{names[k]}=={found[k][1]}" for k in sorted(found, key=lambda k: names[k].lower())]
    path.write_text("\n".join(header + pins) + "\n", encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("freeze", nargs="?", type=Path, help="pip freeze output (default: this interpreter)")
    parser.add_argument("--lock", type=Path, default=LOCK, help="the lock file (default: requirements.lock)")
    parser.add_argument("--write", action="store_true", help="rewrite the lock's pins from the freeze")
    args = parser.parse_args(argv)
    try:
        lock_text = read_text(args.lock)
        found = parse_pins(read_text(args.freeze)) if args.freeze else installed()
    except (OSError, UnicodeDecodeError) as exc:
        print(f"cannot read input: {type(exc).__name__}", file=sys.stderr)
        return 2
    lock = parse_pins(lock_text)
    lines = report(lock, found)
    for line in lines:
        print(line)
    print(f"lock: {len(lock)} pins, installed: {len(found)} packages, {len(lines)} difference(s)")
    if args.write:
        if not args.freeze:
            print("--write needs a freeze file (the image's packages, not this interpreter's)", file=sys.stderr)
            return 2
        write_lock(args.lock, lock_text, lock, found)
        print(f"wrote {args.lock.name}: {len(found)} pins")
    return 0


if __name__ == "__main__":
    sys.exit(main())
