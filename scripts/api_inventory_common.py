"""Shared helpers for the API reference inventories (docs/api_reference.md).

An inventory lists every field that a surface (a tool, a REST route, an MCP tool, a published file) really
returns, as paths in the reference's notation:

  a          top-level key
  a.b        key of a nested object
  a[].b      key of the objects in list a (a[][].b for a list of lists)
  a.*.b      key of the values of a map whose keys are data (roles, labels, names): the map path is listed
             in `dynamic`; the reference writes the segment as a placeholder such as a.<role>
  [].b       key of the objects of a top-level list

Every key that occurs anywhere is one path; lists of scalars add no path of their own beyond their key.
tests/test_api_reference.py compares these paths with the field tables of docs/api_reference.md.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

PLACEHOLDER = re.compile(r"<[^<>]+>")


def paths(obj: Any, dynamic: Iterable[str] = (), prefix: str = "") -> set[str]:
    """Every field path in obj (see the module docstring). Maps listed in `dynamic` get '*' segments."""
    dyn = set(dynamic)
    out: set[str] = set()

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                seg = "*" if path in dyn else str(key)
                child_path = f"{path}.{seg}" if path else seg
                out.add(child_path)
                walk(child, child_path)
        elif isinstance(value, list):
            for item in value:
                walk(item, f"{path}[]")

    walk(obj, prefix)
    return out


def normalize(path: str) -> str:
    """A documented path with placeholders (a.<role>.b) in inventory form (a.*.b)."""
    return PLACEHOLDER.sub("*", path.strip())


def merge(into: dict[str, set[str]], surface: str, found: Iterable[str]) -> None:
    into.setdefault(surface, set()).update(found)


MARKER = re.compile(r"^\s*<!--\s*fields:\s*(\S.*?)\s*-->\s*$")
CELL_SPLIT = re.compile(r"(?<!\\)\|")
CODE = re.compile(r"`([^`]+)`")
SHARED_REF = re.compile(r"\(shared:\s*([\w-]+)\)")


def field_tables(markdown: str) -> dict[str, list[tuple[str, str]]]:
    """The field tables of a reference: surface -> [(path, type cell)], from each `<!-- fields: X -->` marker
    and the table right after it (header and separator rows skipped)."""
    tables: dict[str, list[tuple[str, str]]] = {}
    surface, rows_seen = None, 0
    for line in markdown.splitlines():
        m = MARKER.match(line)
        if m:
            surface, rows_seen = m.group(1), 0
            tables.setdefault(surface, [])
            continue
        if surface is None:
            continue
        if not line.lstrip().startswith("|"):
            if rows_seen:          # the table ended
                surface = None
            continue
        rows_seen += 1
        if rows_seen <= 2:         # header, separator
            continue
        cells = CELL_SPLIT.split(line.strip())[1:-1]
        code = CODE.search(cells[0]) if cells else None
        if code:
            tables[surface].append((code.group(1), cells[1].strip() if len(cells) > 1 else ""))
    return tables


def documented(markdown: str) -> dict[str, set[str]]:
    """surface -> documented paths in inventory form, with `object (shared: x)` rows expanded."""
    tables = field_tables(markdown)

    def expand(surface: str, depth: int = 0) -> set[str]:
        out: set[str] = set()
        for path, typ in tables.get(surface, []):
            p = normalize(path)
            out.add(p)
            ref = SHARED_REF.search(typ)
            if ref and depth < 8:
                base = p + ("[]" if typ.lstrip().startswith("object[]") else "")
                out.update(f"{base}.{q}" for q in expand(f"shared:{ref.group(1)}", depth + 1))
        return out

    return {s: expand(s) for s in tables if not s.startswith("shared:")}


def compare(inventory: dict[str, set[str]], docs: dict[str, set[str]]):
    """(missing, unobserved): per surface, fields returned but not documented, and documented but never seen."""
    missing = {s: sorted(p - docs.get(s, set())) for s, p in inventory.items() if p - docs.get(s, set())}
    unobserved = {s: sorted(docs[s] - inventory.get(s, set())) for s in docs
                  if s in inventory and docs[s] - inventory[s]}
    return missing, unobserved


def print_inventory(inventory: dict[str, set[str]], as_json: bool = False) -> None:
    """Field names only (never values): safe to print for any document."""
    if as_json:
        print(json.dumps({k: sorted(v) for k, v in sorted(inventory.items())}, indent=1))
        return
    for surface in sorted(inventory):
        print(f"== {surface} ({len(inventory[surface])} fields)")
        for p in sorted(inventory[surface]):
            print(f"   {p}")


def main(argv: list[str]) -> int:
    """python scripts/api_inventory_common.py check <reference.md> <part> [<part> ...]
    where <part> names scripts/api_inventory_<part>.py (tools, service). Exit 1 on undocumented fields."""
    import importlib.util
    from pathlib import Path

    if len(argv) < 3 or argv[0] != "check":
        print(main.__doc__)
        return 2
    docs = documented(Path(argv[1]).read_text(encoding="utf-8"))
    inventory: dict[str, set[str]] = {}
    for part in argv[2:]:
        path = Path(__file__).with_name(f"api_inventory_{part}.py")
        spec = importlib.util.spec_from_file_location(f"api_inventory_{part}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for surface, found in module.inventory().items():
            merge(inventory, surface, found)
    missing, unobserved = compare(inventory, docs)
    print(f"surfaces: observed {len(inventory)}, documented {len(docs)}; "
          f"fields observed {sum(map(len, inventory.values()))}")
    for s, ps in sorted(missing.items()):
        print(f"UNDOCUMENTED {s}: {', '.join(ps)}")
    for s, ps in sorted(unobserved.items()):
        print(f"documented, not observed {s}: {', '.join(ps)}")
    print("ok" if not missing else f"{sum(map(len, missing.values()))} undocumented field(s)")
    return 1 if missing else 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
