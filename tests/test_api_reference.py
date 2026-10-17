"""Self-test for docs/api_reference.md: `python tests/test_api_reference.py`.

No pytest, offline (OPENROUTER_OFFLINE=1, no network, no port). The reference stays complete and well formed:
  - every field table is parseable: a `<!-- fields: <surface> -->` marker, a table with at least one row, a valid
    surface name, and every `object (shared: x)` type resolving to a `shared:x` table
  - every field the protocol tools really return is documented (scripts/api_inventory_tools.py runs every tool
    with argument variants on the public protocols in output_public/, with fake embedding and rerank functions)
  - every field the REST routes, MCP tools and published files really return is documented
    (scripts/api_inventory_service.py drives the app in-process on a temporary DATA_DIR)
  - every ```json example parses (after removing /* ... */ comments and the trailing commas they leave)
  - every link to an anchor (#...) names a heading or an <a id> of the reference; every relative file link exists
The inventory checks are skipped with a note when output_public/ is absent (a fresh clone). Fields documented but
not observed in this run are reported, not failed: some appear only in rare cases.
Output: "N passed"; a failure prints the test, the exception type and this file's line, and the field paths.
"""

import importlib.util
import json
import os
import re
import sys
import traceback
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REFERENCE = ROOT / "docs" / "api_reference.md"
SCRIPTS = ROOT / "scripts"
SURFACE = re.compile(r"^(tool:\w+|tool-error|rest:(GET|POST|DELETE|PUT|PATCH) /\S*( \d{3})?|rest-error|"
                     r"mcp:[\w/]+|file:[\w./-]+|shared:[\w-]+)$")
JSON_BLOCK = re.compile(r"```json\n(.*?)```", re.S)
FENCE = re.compile(r"^```.*?^```[ \t]*$", re.S | re.M)
INLINE_CODE = re.compile(r"(`+).+?\1")
COMMENT = re.compile(r"/\*.*?\*/", re.S)
TRAILING_COMMA = re.compile(r",(\s*[\]}])")
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*$", re.M)
ANCHOR_TAG = re.compile(r"<a\s+id=\"([^\"]+)\"")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMMON = _load("api_inventory_common")


def _text() -> str:
    return REFERENCE.read_text(encoding="utf-8")


def _check_part(part: str) -> None:
    if not (ROOT / "output_public").is_dir():
        print(f"   (skipped: no output_public/ for the {part} inventory)")
        return
    inventory = _load(f"api_inventory_{part}").inventory()
    assert inventory, f"the {part} inventory is empty"
    missing, unobserved = COMMON.compare(inventory, COMMON.documented(_text()))
    observed = sum(map(len, inventory.values()))
    print(f"   ({part}: {len(inventory)} surfaces, {observed} fields observed; documented but not observed in this "
          f"run: {sum(map(len, unobserved.values()))})")
    assert not missing, "undocumented: " + "; ".join(f"{s}: {', '.join(p)}" for s, p in sorted(missing.items()))


def _slug(heading: str) -> str:
    """GitHub's heading anchor: markdown stripped, lower case, punctuation removed (letters, digits, '_' and '-'
    kept), each space to a hyphen. A repeated heading gets -1, -2, ... (see _anchors)."""
    text = re.sub(r"`|\*\*|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", heading).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _prose(text: str) -> str:
    """The reference without fenced code blocks (a '# comment' line in a code block is no heading)."""
    return FENCE.sub("", text)


def _anchors(text: str) -> set[str]:
    text = _prose(text)
    seen: dict[str, int] = {}
    out = set(ANCHOR_TAG.findall(text))
    for heading in HEADING.findall(text):
        slug = _slug(heading)
        n = seen.get(slug, 0)
        out.add(slug if n == 0 else f"{slug}-{n}")
        seen[slug] = n + 1
    return out


def test_field_tables_well_formed():
    tables = COMMON.field_tables(_text())
    assert tables, "no field table found"
    bad = [s for s in tables if not SURFACE.match(s)]
    assert not bad, f"invalid surface names: {bad}"
    empty = [s for s, rows in tables.items() if not rows]
    assert not empty, f"markers without a table: {empty}"
    shared = {s.split(":", 1)[1] for s in tables if s.startswith("shared:")}
    refs = {m.group(1) for rows in tables.values() for _, typ in rows for m in COMMON.SHARED_REF.finditer(typ)}
    assert refs <= shared, f"unknown shared objects: {sorted(refs - shared)}"
    unused = shared - refs
    assert not unused, f"shared objects never used: {sorted(unused)}"
    dupes = {s: p for s, rows in tables.items() for p in [r[0] for r in rows] if [r[0] for r in rows].count(p) > 1}
    assert not dupes, f"fields listed twice: {dupes}"


def test_tool_results_documented():
    _check_part("tools")


def test_service_surfaces_documented():
    _check_part("service")


def test_json_examples_parse():
    blocks = JSON_BLOCK.findall(_text())
    assert blocks, "no json example"
    for i, block in enumerate(blocks, 1):
        cleaned = TRAILING_COMMA.sub(r"\1", COMMENT.sub("", block))
        try:
            json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"json example {i} of {len(blocks)}: {exc.msg} at line {exc.lineno}") from None


def test_links_resolve():
    text = _text()
    anchors = _anchors(text)
    broken = []
    for target in LINK.findall(INLINE_CODE.sub("", _prose(text))):  # Markdown shown as code is no link
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        if path:
            if not (REFERENCE.parent / path).exists():
                broken.append(target)
        elif anchor not in anchors:
            broken.append(target)
    assert not broken, f"broken links: {sorted(set(broken))}"


# ---------------------------------------------------------------- runner


def _where(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if Path(f.filename).resolve() == Path(__file__).resolve()]
    return f"{Path(__file__).name}:{frames[-1].lineno}" if frames else "?"


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok {t.__name__}")
        except Exception as exc:  # noqa: BLE001 - report and continue
            failed += 1
            detail = str(exc).splitlines()[0][:2000] if str(exc) else ""
            print(f"FAIL {t.__name__}: {type(exc).__name__} at {_where(exc)}" + (f": {detail}" if detail else ""))
    print(f"{len(tests) - failed} passed" if not failed else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
