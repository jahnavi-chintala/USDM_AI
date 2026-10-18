"""Self-test for the demo UI (service/ui, served at /ui by service.api): `python tests/test_service_ui.py`.

No pytest, offline, fast, no browser and no document data: a temporary DATA_DIR with an empty registry.
  - the UI files exist; only shippable file types; no folder named like a forbidden build-context path
  - GET /ui/ and its assets through the app (fastapi TestClient, base_url http://127.0.0.1): status, content
    types, the Content-Security-Policy (service.api.UI_CSP) and nosniff; GET / redirects to /ui/
  - no external URL (http / https / protocol-relative) in any UI file (the SVG namespace name of an .svg file is
    an identifier, never fetched, and is the only exception)
  - no inline <script> / <style>, style="" attributes or inline event handlers in the markup; no innerHTML-style
    sinks, eval, new Function or string timers in the scripts (all of it would break under the CSP or open XSS)
  - the reprocess dialog's password goes in the JSON body only: not in a query object or the URL, not logged or stored
  - every API path the scripts use is a path of the app's OpenAPI schema, or /mcp
  - every named import resolves to an export of the imported module
  - `node --input-type=module --check` on every .js file when node is on PATH (skipped with a note otherwise;
    plain `node --check file.js` does not report syntax errors of ES modules on Node 22)
Output: "N passed"; a failure prints the test, the exception type and this file's line.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UI = ROOT / "service" / "ui"
LOCAL = "http://127.0.0.1"  # the app refuses Host headers outside ALLOWED_HOSTS
REQUIRED = ["index.html", "styles.css", "app.js", "favicon.svg",
            "modules/api.js", "modules/dialog.js", "modules/dom.js", "modules/jobs.js", "modules/json_view.js",
            "modules/markdown.js", "modules/renderers.js", "modules/schema_form.js", "modules/state.js",
            "modules/view_document.js", "modules/view_documents.js", "modules/view_mcp.js", "modules/view_tools.js",
            "modules/view_usecases.js"]
SHIPPED_SUFFIXES = {".html", ".css", ".js", ".svg", ".png", ".ico"}
FORBIDDEN_DIRS = {"docs", "data", "output", "reports", "node_modules", "_chunks", "__pycache__"}
SVG_NAMESPACES = {"http://www.w3.org/2000/svg", "http://www.w3.org/1999/xlink"}
CSP_DIRECTIVES = ["default-src 'self'", "script-src 'self'", "style-src 'self'", "connect-src 'self'",
                  "frame-ancestors 'self'", "object-src 'none'", "base-uri 'none'"]
JS_TYPES = ("text/javascript", "application/javascript")


def ui_files(suffixes=None):
    out = []
    for p in sorted(UI.rglob("*")):
        if p.is_file() and (suffixes is None or p.suffix.lower() in suffixes):
            out.append(p)
    return out


def rel(p: Path) -> str:
    return p.relative_to(UI).as_posix()


def strip_js_comments(src: str) -> str:
    """Line and block comments removed (strings and regex literals are left alone well enough for these checks:
    a '//' inside a string literal only matters for URL scans, which run on the raw text)."""
    out, i, n = [], 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            i += 1
            continue
        if src.startswith("//", i) and (i == 0 or src[i - 1] not in ":\\"):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if src.startswith("/*", i):
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


@contextmanager
def ui_client():
    """A TestClient on create_app with a temporary, empty service (no worker, offline)."""
    from fastapi.testclient import TestClient

    from service import api
    from service.app_service import ProtocolService
    from service.config import ServiceConfig

    tmp = Path(tempfile.mkdtemp(prefix="svc_ui_"))
    try:
        config = ServiceConfig(data_dir=tmp / "data", offline=True)
        service = ProtocolService(config, version=("1.0.0", "fp-test"), run_worker=False)
        with TestClient(api.create_app(service), base_url=LOCAL) as client:
            yield client, api
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- files


def test_files_exist():
    assert UI.is_dir(), "service/ui is missing"
    missing = [name for name in REQUIRED if not (UI / name).is_file()]
    assert not missing, f"missing UI files: {missing}"


def test_only_shipped_file_types_and_names():
    odd = [rel(p) for p in ui_files() if p.suffix.lower() not in SHIPPED_SUFFIXES or p.name.startswith(".")]
    assert not odd, f"files the image does not ship (only {sorted(SHIPPED_SUFFIXES)}): {odd}"
    bad = [rel(p) for p in UI.rglob("*") if p.is_dir() and p.name.lower() in FORBIDDEN_DIRS]
    assert not bad, f"folder names the build context check forbids: {bad}"


def test_index_references():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert re.search(r'<script\s+type="module"\s+src="app\.js"\s*>\s*</script>', html), "index.html must load app.js as a module"
    assert 'href="styles.css"' in html, "index.html must link styles.css"
    for ref in re.findall(r'(?:src|href)="([^"#][^"]*)"', html):
        assert not ref.startswith(("/", "http:", "https:")), f"index.html: non-relative reference {ref}"
        assert (UI / ref).is_file(), f"index.html references a missing file: {ref}"
    assert '<html lang="en">' in html and 'name="viewport"' in html, "index.html: lang and viewport"


# ---------------------------------------------------------------- served by the app


def test_served_with_csp_and_types():
    with ui_client() as (client, api):
        r = client.get("/ui/")
        assert r.status_code == 200, f"/ui/ -> {r.status_code}"
        assert r.headers["content-type"].startswith("text/html"), r.headers["content-type"]
        csp = r.headers.get("content-security-policy", "")
        assert csp == api.UI_CSP, "the /ui CSP is not service.api.UI_CSP"
        for directive in CSP_DIRECTIVES:
            assert directive in csp, f"CSP lacks {directive!r}"
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp, "the CSP must not allow inline code or eval"
        assert r.headers.get("x-content-type-options") == "nosniff"
        assert '<script type="module" src="app.js">' in r.text
        expected = {"app.js": JS_TYPES, "modules/api.js": JS_TYPES, "modules/view_tools.js": JS_TYPES,
                    "styles.css": ("text/css",), "favicon.svg": ("image/svg+xml",), "index.html": ("text/html",)}
        for name, types in expected.items():
            r = client.get(f"/ui/{name}")
            assert r.status_code == 200, f"/ui/{name} -> {r.status_code}"
            ctype = r.headers["content-type"].split(";")[0].strip()
            assert ctype in types, f"/ui/{name}: content type {ctype}, expected one of {types}"
            assert r.headers.get("content-security-policy") == api.UI_CSP, f"/ui/{name}: no CSP"
        for p in ui_files({".js"}):  # every module is served as JavaScript (module scripts need it)
            r = client.get(f"/ui/{rel(p)}")
            assert r.status_code == 200 and r.headers["content-type"].split(";")[0] in JS_TYPES, rel(p)
        r = client.get("/ui/no-such-file.js")
        assert r.status_code == 404, f"missing UI file -> {r.status_code}"
        r = client.get("/", follow_redirects=False)
        assert r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location", "").endswith("/ui/"), \
            f"/ -> {r.status_code} {r.headers.get('location')}"


# ---------------------------------------------------------------- static checks


def test_no_external_urls():
    url = re.compile(r"(?i)\bhttps?://[^\s\"'`<>)]+")
    relative_proto = re.compile(r"""(?i)(?:src|href|action)\s*=\s*["']//|url\(\s*["']?//|["'`]//[a-z0-9.-]+\.[a-z]{2,}""")
    hits = []
    for p in ui_files({".html", ".css", ".js", ".svg"}):
        text = p.read_text(encoding="utf-8")
        for m in url.finditer(text):
            if p.suffix == ".svg" and m.group(0) in SVG_NAMESPACES:
                continue
            hits.append(f"{rel(p)}: {m.group(0)[:60]}")
        hits += [f"{rel(p)}: protocol-relative URL" for _ in relative_proto.finditer(text)]
        if p.suffix == ".css":
            hits += [f"{rel(p)}: @import" for _ in re.finditer(r"@import", text)]
    assert not hits, f"external URLs in the UI: {hits}"


def test_no_inline_script_style_or_handlers():
    problems = []
    for p in ui_files({".html"}):
        html = p.read_text(encoding="utf-8")
        for m in re.finditer(r"<script\b([^>]*)>", html, re.I):
            if "src=" not in m.group(1):
                problems.append(f"{rel(p)}: inline <script>")
        if re.search(r"<style\b", html, re.I):
            problems.append(f"{rel(p)}: <style> block")
        if re.search(r"\sstyle\s*=", html, re.I):
            problems.append(f"{rel(p)}: style attribute")
        for m in re.finditer(r"\s(on[a-z]+)\s*=", html, re.I):
            problems.append(f"{rel(p)}: inline handler {m.group(1)}")
        if re.search(r"javascript:", html, re.I):
            problems.append(f"{rel(p)}: javascript: URL")
    sinks = {
        "innerHTML / outerHTML assignment": r"\.(?:inner|outer)HTML\s*=",
        "insertAdjacentHTML": r"insertAdjacentHTML",
        "document.write": r"document\.write",
        "eval": r"(?<![\w.])eval\s*\(",
        "new Function": r"new\s+Function\s*\(",
        "string timer": r"set(?:Timeout|Interval)\s*\(\s*[\"'`]",
        "style attribute": r"setAttribute\(\s*[\"']style[\"']",
        "inline handler attribute": r"setAttribute\(\s*[\"']on[a-z]+[\"']",
        "javascript: URL": r"javascript:",
        "<style> element": r"createElement\(\s*[\"']style[\"']",
        "<script> element": r"createElement\(\s*[\"']script[\"']",
    }
    for p in ui_files({".js"}):
        code = strip_js_comments(p.read_text(encoding="utf-8"))
        for label, pattern in sinks.items():
            if re.search(pattern, code):
                problems.append(f"{rel(p)}: {label}")
    assert not problems, f"inline code / unsafe sinks: {problems}"


def _openapi_paths():
    from service import api

    return set(api.create_app().openapi()["paths"])  # create_app touches nothing until startup


def _path_regex(template: str) -> re.Pattern:
    parts = re.split(r"(\{[^}]+\}|\$\{[^}]+\})", template)
    return re.compile("^" + "".join("[^/]+" if part.startswith(("{", "${")) else re.escape(part) for part in parts) + "$")


def test_api_paths_exist_in_openapi():
    paths = _openapi_paths()
    literal = re.compile(r"""(["'`])(/[A-Za-z][^"'`\s]*)\1""")
    used, unknown = set(), []
    for p in ui_files({".js"}):
        code = strip_js_comments(p.read_text(encoding="utf-8"))
        for m in literal.finditer(code):
            path = m.group(2).split("?")[0]
            used.add(path)
            if path == "/mcp" or path in paths:
                continue
            if any(_path_regex(path).match(_path_regex_sample(t)) for t in paths):
                continue
            unknown.append(f"{rel(p)}: {path}")
    assert used, "no API path found in the scripts"
    assert "/mcp" in used and "/documents/{doc_id}/tools/{tool_name}" in used, "api.js must define the tool and MCP paths"
    assert not unknown, f"API paths not in the OpenAPI schema: {unknown}"


def _path_regex_sample(template: str) -> str:
    """An OpenAPI template with each parameter replaced by a sample segment (to match a JS template literal)."""
    return re.sub(r"\{[^}]+\}", "x", template)


def test_named_imports_resolve():
    exported: dict[Path, set[str]] = {}
    for p in ui_files({".js"}):
        code = strip_js_comments(p.read_text(encoding="utf-8"))
        names = set(re.findall(r"export\s+(?:async\s+)?(?:function\*?|const|let|var|class)\s+([A-Za-z_$][\w$]*)", code))
        for group in re.findall(r"export\s*\{([^}]*)\}", code):
            names |= {part.split(" as ")[-1].strip() for part in group.split(",") if part.strip()}
        exported[p.resolve()] = names
    problems = []
    for p in ui_files({".js"}):
        code = strip_js_comments(p.read_text(encoding="utf-8"))
        for names, spec in re.findall(r"import\s*\{([^}]*)\}\s*from\s*[\"']([^\"']+)[\"']", code):
            target = (p.parent / spec).resolve()
            if not spec.startswith("./") and not spec.startswith("../"):
                problems.append(f"{rel(p)}: non-relative import {spec}")
                continue
            if target not in exported:
                problems.append(f"{rel(p)}: missing module {spec}")
                continue
            for name in (n.split(" as ")[0].strip() for n in names.split(",") if n.strip()):
                if name not in exported[target]:
                    problems.append(f"{rel(p)}: {name} is not exported by {spec}")
        for spec in re.findall(r"import\s+\*\s+as\s+\w+\s+from\s*[\"']([^\"']+)[\"']", code):
            if (p.parent / spec).resolve() not in exported:
                problems.append(f"{rel(p)}: missing module {spec}")
    assert not problems, f"unresolved imports: {problems}"


def _js_call(code: str, start: int) -> str:
    """The text of the call whose '(' is at `start`, up to its matching ')' (string literals skipped)."""
    depth, quote, i = 0, None, start
    while i < len(code):
        c = code[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in "\"'`":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
        i += 1
    raise AssertionError("unbalanced call in the script")


REPROCESS_FLOW_SAMPLE = """export async function reprocessFlow(doc) {
  const pw = h("input", { type: "password", autocomplete: "off" });
  const conf = await confirmOpenRouter("reprocess", { extra: pw });
  const typed = pw.value;
  pw.value = "";
  const payload = { force: Boolean(conf.checks.force) };
  if (typed) payload.password = typed;
  const res = await post(PATHS.reprocess, { params: { doc_id: doc.doc_id }, json: payload });
  toast("queued", "ok");
  return res.data;
}

function unrelated() { console.log("not part of reprocessFlow"); }
"""


def _reprocess_flow_problems(code: str) -> list[str]:
    """Why reprocessFlow in `code` (script text without comments) could leak the PDF password; [] when it cannot.
    Independent of the variable names: it follows the password input, the value read from it and the JSON body."""
    start = code.find("export async function reprocessFlow")
    if start < 0:
        return ["reprocessFlow is missing"]
    end = re.compile(r"^(?:export\s|function\s|async\s+function\s)", re.M).search(code, start + 1)
    flow = code[start:end.start() if end else len(code)]  # up to the next top-level declaration
    problems = []
    fields = re.finditer(r"""(?:const|let)\s+(\w+)\s*=\s*h\(\s*"input"\s*,\s*(\{[^}]*\})""", flow)
    field = next((m for m in fields if re.search(r'\btype\s*:\s*"password"', m.group(2))), None)
    secrets = {"password"}
    if field is None:
        problems.append("the dialog has no input with type: \"password\"")
    else:
        if not re.search(r'\bautocomplete\s*:\s*"off"', field.group(2)):
            problems.append("the password input lacks autocomplete: \"off\"")
        name = re.escape(field.group(1))
        secrets |= {field.group(1)} | {m.group(1) for m in re.finditer(
            r"(?:const|let|var)\s+(\w+)\s*=\s*" + name + r"\.value\b", flow)}
        if not re.search(r"\b" + name + r"""\.value\s*=\s*(?:""|'')""", flow):  # this field's own value, not any other
            problems.append(f"nothing clears the password field ({field.group(1)}.value = \"\")")
    call = re.search(r"post\(\s*PATHS\.reprocess\b", flow)
    if not call:
        return problems + ["reprocessFlow does not post to PATHS.reprocess"]
    args = _js_call(flow, call.start() + len("post"))
    if re.search(r"\bquery\s*:", args):
        problems.append("the reprocess call has a query object (anything there ends up in the URL)")
    body = re.search(r"\bjson\s*:\s*(\{|[\w$.]+)", args)
    if not body:
        problems.append("the reprocess call has no JSON body")
    elif body.group(1) == "{":
        if not re.search(r"\bpassword\b", args[body.end():]):
            problems.append("the JSON body has no password key")
    else:
        secrets.add(body.group(1))
        var = re.escape(body.group(1))
        key = (r"(?<!delete )\b" + var + r"""\s*(?:\.\s*password\b|\[\s*['"]password['"]\s*\])\s*=(?!=)"""
               r"|\b" + var + r"\s*=\s*\{[^}]*\bpassword\b")
        if not re.search(key, flow):
            problems.append("the JSON body never gets a password key")
    alt = "|".join(re.escape(n) for n in sorted(secrets))
    if re.search(r"\btoast\([^;]*\b(?:" + alt + r")\b", flow, re.S):
        problems.append("a toast shows the password or the body")
    if re.search(r"\bconsole\s*\.", flow):
        problems.append("reprocessFlow logs to the console")
    if re.search(r"\.request\b", flow):  # an ApiError keeps its request, and with it the JSON body
        problems.append("reprocessFlow reads the request object of an error (it holds the body)")
    for sink in (r"storageSet|localStorage|sessionStorage", r"encodeURIComponent|location\.|URLSearchParams"):
        if re.search(sink, flow):
            problems.append(f"reprocessFlow touches {sink}")
    return problems


def test_reprocess_password_only_in_json_body():
    """The reprocess dialog's password goes in the JSON body: never in a query object or the URL, not logged, not
    stored, not shown in a toast, and the field is cleared. The check is itself tried on unsafe variants."""
    code = strip_js_comments((UI / "modules" / "view_documents.js").read_text(encoding="utf-8"))
    problems = _reprocess_flow_problems(code)
    assert not problems, f"reprocessFlow: {problems}"
    assert not _reprocess_flow_problems(REPROCESS_FLOW_SAMPLE), "the sample of a safe flow was rejected"
    unsafe = {
        "force in the query (the old form)": ("json: payload", 'query: payload.force ? { force: "true" } : null'),
        "password in the query": ("json: payload", "json: payload, query: { password: typed }"),
        "password never put in the body": ("if (typed) payload.password = typed;", "delete payload.password;"),
        "field not cleared": ('pw.value = "";', ""),
        "another field cleared instead": ('pw.value = "";', 'conf.value = "";'),
        "visible field": ('type: "password"', 'type: "text"'),
        "autocomplete left on": ('autocomplete: "off"', 'autocomplete: "on"'),
        "password in a toast": ('toast("queued", "ok");', "toast(`failed for ${typed}`, \"error\");"),
        "password logged": ('toast("queued", "ok");', "console.log(typed);"),
        "request object of an error shown": ('toast("queued", "ok");', 'toast(JSON.stringify(err.request), "error");'),
    }
    for name, (old, new) in unsafe.items():
        variant = REPROCESS_FLOW_SAMPLE.replace(old, new)
        assert variant != REPROCESS_FLOW_SAMPLE and _reprocess_flow_problems(variant), f"not caught: {name}"
    for p in ui_files({".js"}):  # and no script puts a password into a query object
        text = strip_js_comments(p.read_text(encoding="utf-8"))
        assert not re.search(r"\bquery\s*:\s*\{[^}]*password", text), f"{rel(p)}: password in a query object"


def test_node_syntax_check():
    node = shutil.which("node")
    if not node:
        print("  note: node is not on PATH: the ES module syntax check is skipped")
        return
    failed = []
    for p in ui_files({".js"}):
        result = subprocess.run([node, "--input-type=module", "--check"], input=p.read_bytes(), capture_output=True,
                                timeout=60)
        if result.returncode != 0:
            line = next((ln for ln in result.stderr.decode("utf-8", "replace").splitlines() if "Error" in ln), "")
            failed.append(f"{rel(p)}: {line[:120]}")
    assert not failed, f"node syntax check failed: {failed}"


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
            detail = str(exc).splitlines()[0][:400] if str(exc) else ""
            print(f"FAIL {t.__name__}: {type(exc).__name__} at {_where(exc)}" + (f": {detail}" if detail else ""))
    print(f"{len(tests) - failed} passed" if not failed else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
