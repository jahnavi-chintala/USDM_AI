"""docs/api_reference.md -> a self-contained HTML page (the web version of the reference).

python scripts/build_reference_page.py docs/api_reference.md <out.html>
Handles the Markdown the reference uses: ATX headings (GitHub anchors), paragraphs, nested lists, GFM tables with
escaped pipes, fenced code, inline code / bold / italic / links, <a id> lines, and the <!-- fields: X --> markers
(the next table becomes a field table with a surface label and per-row ids for the field finder).
"""

import html
import json
import re
import sys
from pathlib import Path

REPO = "https://github.com/roopchandvanga/ingest_retrieve/blob/main/"
MARKER = re.compile(r"^\s*<!--\s*fields:\s*(\S.*?)\s*-->\s*$")
COMMENT = re.compile(r"^\s*<!--.*-->\s*$")
ANCHOR_LINE = re.compile(r'^\s*<a id="([\w.-]+)"></a>\s*$')
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s+(.*)$")
FENCE = re.compile(r"^(\s*)```(\w*)\s*$")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
METHOD = re.compile(r"^(GET|POST|DELETE|PUT|PATCH)\s+(.*)$")
CELL_SPLIT = re.compile(r"(?<!\\)\|")


def slug(text: str) -> str:
    t = re.sub(r"`|\*\*|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", text).lower()
    return re.sub(r"[^\w\- ]", "", t).replace(" ", "-")


def href(target: str) -> str:
    if target.startswith(("#", "http://", "https://", "mailto:")):
        return target
    path, _, anchor = target.partition("#")
    base = (Path("docs") / path).as_posix()
    parts = []
    for p in base.split("/"):
        if p == "..":
            parts.pop()
        elif p not in ("", "."):
            parts.append(p)
    return REPO + "/".join(parts) + (f"#{anchor}" if anchor else "")


def inline(text: str) -> str:
    codes: list[str] = []

    def keep(m):
        codes.append(m.group(2).strip() if m.group(2).strip() else m.group(2))
        return f"\x00{len(codes) - 1}\x00"

    s = CODE_SPAN.sub(keep, text.replace("\\|", "|"))
    s = html.escape(s, quote=False)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: f'<a href="{html.escape(href(m.group(2)))}"'
                         + (' target="_blank" rel="noopener"' if not m.group(2).startswith("#") else "")
                         + f">{m.group(1)}</a>", s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
    return re.sub(r"\x00(\d+)\x00", lambda m: f"<code>{html.escape(codes[int(m.group(1))], quote=False)}</code>", s)


TEMPLATE = r'''<title>{{TITLE}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Condensed:wght@500;600&family=IBM+Plex+Sans:ital,wght@0,400;0,500;0,600;1,400&display=swap">
<style>
/* Layout: a reference manual. Sticky index with a field finder on the left, one reading column on the right whose
   field tables may run wider; the same tokens as the deployment guide so the two pages read as one set. */
:root {
  --bg: #f6f7f8;
  --surface: #ffffff;
  --ink: #17232b;
  --muted: #55636c;
  --rule: #d9dfe3;
  --accent: #0d5c6e;
  --accent-soft: #e2eff2;
  --warn: #8a5a00;
  --warn-soft: #fbf1dc;
  --ok: #1d6b45;
  --ok-soft: #e3f2ea;
  --code-bg: #eef1f3;
  --font-display: "IBM Plex Sans Condensed", "Arial Narrow", "Helvetica Neue", Arial, sans-serif;
  --font-body: "IBM Plex Sans", "Segoe UI", "Helvetica Neue", Arial, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "Cascadia Mono", Consolas, monospace;
  --step--1: 0.84rem; --step-0: 1rem; --step-1: 1.2rem; --step-2: 1.5rem; --step-3: 2.1rem;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #11181d; --surface: #172127; --ink: #e3e9ec; --muted: #9aa9b1; --rule: #2a373f;
    --accent: #6fc3d4; --accent-soft: #1b3139; --warn: #f0c063; --warn-soft: #33290f;
    --ok: #7fd3a5; --ok-soft: #173327; --code-bg: #0d1418; color-scheme: dark;
  }
}
:root[data-theme="dark"] {
  --bg: #11181d; --surface: #172127; --ink: #e3e9ec; --muted: #9aa9b1; --rule: #2a373f;
  --accent: #6fc3d4; --accent-soft: #1b3139; --warn: #f0c063; --warn-soft: #33290f;
  --ok: #7fd3a5; --ok-soft: #173327; --code-bg: #0d1418; color-scheme: dark;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
@media (prefers-reduced-motion: reduce) { html { scroll-behavior: auto; } }
body { background: var(--bg); color: var(--ink); font-family: var(--font-body); font-size: var(--step-0); line-height: 1.6; }
a { color: var(--accent); text-underline-offset: 2px; }
a:focus-visible, button:focus-visible, summary:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 3px; }
code, pre { font-family: var(--font-mono); font-size: 0.88em; }
:not(pre) > code { background: var(--code-bg); padding: 0.08em 0.35em; border-radius: 4px; overflow-wrap: anywhere; }

header.top { max-width: 1240px; margin: 0 auto; padding-inline: 16px; padding-block: 2.5rem 1.5rem; }
.wrap { max-width: 1240px; margin: 0 auto; padding-inline: 16px; padding-block: 0 4rem; display: grid; grid-template-columns: minmax(0, 1fr); gap: 2rem; }
@media (min-width: 980px) {
  header.top { padding-inline: 32px; }
  .wrap { grid-template-columns: 250px minmax(0, 1fr); padding-inline: 32px; gap: 3rem; }
}
.eyebrow { font-family: var(--font-mono); font-size: var(--step--1); color: var(--muted); letter-spacing: 0.04em; }
h1 { font-family: var(--font-display); font-weight: 600; font-size: var(--step-3); line-height: 1.1; margin: 0.4rem 0 0.8rem; text-wrap: balance; }
.lede { max-width: 72ch; margin: 0; }
.facts { display: flex; flex-wrap: wrap; gap: 0.5rem 1.5rem; margin: 1.25rem 0 0; padding: 0; list-style: none; font-size: var(--step--1); color: var(--muted); }
.facts b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }

nav.index { font-size: var(--step--1); min-width: 0; }
.finder { display: grid; gap: 0.35rem; margin-bottom: 1rem; }
.finder label { font-family: var(--font-mono); color: var(--muted); letter-spacing: 0.04em; }
.finder input { width: 100%; font: inherit; font-size: 0.92rem; color: var(--ink); background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; padding: 0.45rem 0.6rem; }
.results { list-style: none; margin: 0; padding: 0.3rem; background: var(--surface); border: 1px solid var(--rule); border-radius: 6px; display: grid; gap: 0.1rem; max-height: 22rem; overflow-y: auto; }
.results a { display: grid; gap: 0.05rem; padding: 0.3rem 0.45rem; border-radius: 4px; text-decoration: none; color: var(--ink); }
.results a:hover, .results a:focus-visible { background: var(--accent-soft); }
.results .rp { font-family: var(--font-mono); overflow-wrap: anywhere; }
.results .rs { color: var(--muted); font-size: 0.78rem; overflow-wrap: anywhere; }
.results .none { padding: 0.3rem 0.45rem; color: var(--muted); }
nav.index details { background: var(--surface); border: 1px solid var(--rule); border-radius: 8px; padding: 0.6rem 0.9rem; }
nav.index summary { cursor: pointer; font-weight: 600; }
nav.index ol { list-style: none; padding: 0; margin: 0.6rem 0 0; display: grid; gap: 0.05rem; }
nav.index a { display: flex; gap: 0.55rem; padding: 0.2rem 0.4rem; border-radius: 5px; color: var(--muted); text-decoration: none; }
nav.index .l2 { margin-top: 0.4rem; }
nav.index .l2 a { color: var(--ink); font-weight: 500; }
nav.index .l3 a { padding-left: 2.2rem; }
nav.index a .n { font-family: var(--font-mono); color: var(--accent); min-width: 1.2rem; }
nav.index a:hover, nav.index a.active { background: var(--accent-soft); color: var(--ink); }
nav.index code { background: none; padding: 0; }
@media (min-width: 980px) {
  nav.index .sticky { position: sticky; top: calc(env(safe-area-inset-top, 0px) + 1.25rem); max-height: calc(100vh - 2.5rem); overflow-y: auto; padding-right: 0.3rem; }
  nav.index details { border: 0; background: none; padding: 0; }
  nav.index summary { list-style: none; color: var(--muted); font-family: var(--font-mono); font-weight: 400; letter-spacing: 0.04em; pointer-events: none; }
  nav.index summary::-webkit-details-marker { display: none; }
}

main { min-width: 0; }
section { padding-block: 1.6rem; border-top: 1px solid var(--rule); scroll-margin-top: 1rem; }
section:first-child { border-top: 0; padding-top: 0; }
h2 { font-family: var(--font-display); font-weight: 600; font-size: var(--step-2); line-height: 1.2; margin: 0 0 0.9rem; display: flex; gap: 0.7rem; align-items: baseline; text-wrap: balance; }
h2 .n { font-family: var(--font-mono); font-size: var(--step-0); color: var(--accent); font-weight: 500; }
h3 { font-family: var(--font-display); font-weight: 600; font-size: var(--step-1); margin: 2rem 0 0.5rem; scroll-margin-top: 1rem; text-wrap: balance; }
h4 { font-family: var(--font-display); font-weight: 600; font-size: var(--step-0); margin: 1.4rem 0 0.4rem; scroll-margin-top: 1rem; }
h3 code, h4 code { font-size: 0.9em; }
h3 .path, h4 .path { font-family: var(--font-mono); font-weight: 500; font-size: 0.92em; overflow-wrap: anywhere; }
.method { display: inline-block; font: 600 0.72rem var(--font-mono); letter-spacing: 0.04em; padding: 0.12rem 0.45rem; border-radius: 4px; vertical-align: 0.12em; }
.method.get { background: var(--ok-soft); color: var(--ok); }
.method.post { background: var(--accent-soft); color: var(--accent); }
.method.delete { background: var(--warn-soft); color: var(--warn); }
p, main ul, main ol { max-width: 76ch; }
main ul, main ol { padding-left: 1.2rem; }
main li { margin-block: 0.2rem; }

.table { max-width: 100%; overflow-x: auto; margin: 0.6rem 0 1.1rem; border: 1px solid var(--rule); border-radius: 8px; background: var(--surface); }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
th, td { text-align: left; vertical-align: top; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--rule); }
tr:last-child td { border-bottom: 0; }
th { font-family: var(--font-display); font-weight: 600; background: var(--code-bg); white-space: nowrap; }
.surface { display: flex; gap: 0.6rem; align-items: baseline; padding: 0.4rem 0.75rem; border-bottom: 1px solid var(--rule); font-size: var(--step--1); color: var(--muted); }
.surface span { font-family: var(--font-mono); letter-spacing: 0.04em; text-transform: uppercase; font-size: 0.72rem; }
.surface code { background: none !important; padding: 0 !important; color: var(--ink); }
.fields td:first-child { min-width: 12rem; }
.fields td:first-child code { background: none; padding: 0; font-weight: 500; color: var(--ink); }
.fields td:nth-child(2) { font-family: var(--font-mono); font-size: 0.8rem; color: var(--muted); white-space: nowrap; }
.fields td:nth-child(2) code { background: none; padding: 0; }
.fields tr:target td { background: var(--accent-soft); }
.fields tr { scroll-margin-top: 5rem; }

.code { max-width: 100%; margin: 0.6rem 0 1.1rem; border: 1px solid var(--rule); border-radius: 8px; background: var(--code-bg); }
.code-bar { display: flex; justify-content: space-between; align-items: center; padding: 0.3rem 0.45rem 0.3rem 0.8rem; border-bottom: 1px solid var(--rule); font: 500 0.72rem var(--font-mono); letter-spacing: 0.04em; color: var(--muted); }
.code pre { margin: 0; padding: 0.8rem 1rem; overflow: auto; max-height: 30rem; line-height: 1.5; }
.copy { font: 500 0.75rem var(--font-body); color: var(--muted); background: var(--surface); border: 1px solid var(--rule); border-radius: 5px; padding: 0.15rem 0.55rem; cursor: pointer; }
.copy:hover { color: var(--ink); border-color: var(--muted); }
footer { color: var(--muted); font-size: var(--step--1); margin-top: 2rem; max-width: 76ch; }
</style>

<header class="top">
  <div class="eyebrow">docs/api_reference.md · ingest_retrieve · protocol service</div>
  <h1>{{TITLE}}</h1>
  <p class="lede">{{LEDE}}</p>
  <ul class="facts" aria-label="Key figures">{{FACTS}}</ul>
</header>

<div class="wrap">
  <nav class="index" aria-label="Sections">
    <div class="sticky">
      <div class="finder" role="search">
        <label for="find">FIND A FIELD, ROUTE OR TOOL</label>
        <input id="find" type="search" placeholder="pages, cursor, job_id, quote_check…" autocomplete="off" spellcheck="false">
        <ol id="results" class="results" aria-live="polite" hidden></ol>
      </div>
      <details open id="toc">
        <summary>CONTENTS</summary>
        <ol>
{{NAV}}
        </ol>
      </details>
    </div>
  </nav>

  <main>
{{BODY}}
    <footer>Generated from docs/api_reference.md in the ingest_retrieve repository. The field tables are checked against the service's real outputs by tests/test_api_reference.py. Links to repository files open GitHub (private repository).</footer>
  </main>
</div>

<script id="ref-data" type="application/json">{{DATA}}</script>
<script>
(function () {
  "use strict";
  var data = { fields: [], headings: [] };
  try { data = JSON.parse(document.getElementById("ref-data").textContent); } catch (e) { /* the page still reads without the finder */ }

  // Copy buttons on code blocks.
  document.querySelectorAll(".code .copy").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var pre = btn.closest(".code").querySelector("pre");
      var text = pre.innerText;
      var done = function () { btn.textContent = "Copied"; setTimeout(function () { btn.textContent = "Copy"; }, 1400); };
      var select = function () { var r = document.createRange(); r.selectNodeContents(pre); var s = getSelection(); s.removeAllRanges(); s.addRange(r); btn.textContent = "Selected"; };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, select);
      else select();
    });
  });

  // The contents start closed on phones, where they would push the reference down.
  var toc = document.getElementById("toc");
  if (window.matchMedia && !window.matchMedia("(min-width: 980px)").matches) toc.open = false;

  // Field finder: field paths of every field table, and route / tool / file headings.
  var input = document.getElementById("find");
  var results = document.getElementById("results");
  function item(href, primary, secondary) {
    var li = document.createElement("li");
    var a = document.createElement("a");
    a.href = href;
    var p = document.createElement("span"); p.className = "rp"; p.textContent = primary;
    var s = document.createElement("span"); s.className = "rs"; s.textContent = secondary;
    a.appendChild(p); a.appendChild(s); li.appendChild(a);
    return li;
  }
  function search() {
    var q = input.value.trim().toLowerCase();
    results.textContent = "";
    if (q.length < 2) { results.hidden = true; return; }
    var heads = data.headings.filter(function (h) { return h.t.toLowerCase().indexOf(q) >= 0; }).slice(0, 8);
    var fields = data.fields.filter(function (f) { return f.p.toLowerCase().indexOf(q) >= 0; });
    fields.sort(function (a, b) {
      var al = a.p.toLowerCase().split(/[.\[\]]/).pop() === q ? 0 : 1, bl = b.p.toLowerCase().split(/[.\[\]]/).pop() === q ? 0 : 1;
      return al - bl || a.p.length - b.p.length;
    });
    heads.forEach(function (h) { results.appendChild(item("#" + h.id, h.t, "section")); });
    fields.slice(0, 60).forEach(function (f) { results.appendChild(item("#" + f.id, f.p, f.s + (f.h && f.h !== f.s ? " · " + f.h : ""))); });
    if (!heads.length && !fields.length) {
      var li = document.createElement("li"); li.className = "none"; li.textContent = "No field or section matches."; results.appendChild(li);
    } else if (fields.length > 60) {
      var more = document.createElement("li"); more.className = "none"; more.textContent = (fields.length - 60) + " more fields: type more of the name."; results.appendChild(more);
    }
    results.hidden = false;
  }
  input.addEventListener("input", search);
  input.addEventListener("keydown", function (e) {
    if (e.key === "Enter") { var first = results.querySelector("a"); if (first) { e.preventDefault(); first.click(); } }
    if (e.key === "Escape") { input.value = ""; search(); }
  });

  // Highlight the section being read in the contents.
  var links = {};
  document.querySelectorAll("nav.index ol a").forEach(function (a) { links[a.getAttribute("href").slice(1)] = a; });
  var marks = Array.prototype.slice.call(document.querySelectorAll("main section, main h3[id]"));
  if ("IntersectionObserver" in window) {
    var current = null;
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (en) {
        if (!en.isIntersecting) return;
        var id = en.target.id;
        if (!links[id]) return;
        if (current) current.classList.remove("active");
        current = links[id]; current.classList.add("active");
      });
    }, { rootMargin: "0px 0px -75% 0px" });
    marks.forEach(function (m) { io.observe(m); });
  }
})();
</script>
'''


class Builder:
    def __init__(self, md: str):
        self.lines = md.splitlines()
        self.out: list[str] = []
        self.headings: list[tuple[int, str, str]] = []   # level, text, id
        self.fields: list[dict] = []                     # field finder index
        self.seen: dict[str, int] = {}
        self.section_open = False
        self.surface: str | None = None
        self.current_h3 = ""
        self.counts = {"routes": 0, "mcp_tools": 0, "field_tables": 0}

    def anchor(self, text: str) -> str:
        s = slug(text)
        n = self.seen.get(s, 0)
        self.seen[s] = n + 1
        return s if n == 0 else f"{s}-{n}"

    def build(self) -> str:
        i, n = 0, len(self.lines)
        para: list[str] = []

        def flush():
            if para:
                self.out.append(f"<p>{inline(' '.join(l.strip() for l in para))}</p>")
                para.clear()

        while i < n:
            line = self.lines[i]
            if not line.strip():
                flush(); i += 1; continue
            m = FENCE.match(line)
            if m:
                flush()
                lang, body, i = m.group(2), [], i + 1
                indent = len(m.group(1))
                while i < n and not FENCE.match(self.lines[i]):
                    body.append(self.lines[i][indent:] if self.lines[i][:indent].isspace() else self.lines[i])
                    i += 1
                i += 1
                code = html.escape("\n".join(body), quote=False)
                cls = f' class="lang-{lang}"' if lang else ""
                label = {"json": "JSON", "powershell": "PowerShell", "python": "Python", "bash": "Shell",
                         "javascript": "JavaScript"}.get(lang, "")
                self.out.append(f'<div class="code"><div class="code-bar"><span>{label}</span>'
                                f'<button type="button" class="copy">Copy</button></div>'
                                f"<pre><code{cls}>{code}</code></pre></div>")
                continue
            m = MARKER.match(line)
            if m:
                flush(); self.surface = m.group(1); i += 1; continue
            m = ANCHOR_LINE.match(line)
            if m:
                flush(); self.out.append(f'<a id="{m.group(1)}"></a>'); i += 1; continue
            if COMMENT.match(line):
                flush(); i += 1; continue
            m = HEADING.match(line)
            if m:
                flush(); self.heading(len(m.group(1)), m.group(2)); i += 1; continue
            if line.lstrip().startswith("|"):
                flush()
                rows = []
                while i < n and self.lines[i].lstrip().startswith("|"):
                    rows.append(self.lines[i]); i += 1
                self.table(rows)
                continue
            if LIST_ITEM.match(line):
                flush()
                block = []
                while i < n:
                    l = self.lines[i]
                    if not l.strip():
                        nxt = self.lines[i + 1] if i + 1 < n else ""
                        if LIST_ITEM.match(nxt) and len(nxt) - len(nxt.lstrip()) > 0:
                            i += 1; continue
                        break
                    if FENCE.match(l) or HEADING.match(l) or l.lstrip().startswith("|") and not block:
                        break
                    if not (LIST_ITEM.match(l) or l.startswith((" ", "\t"))) and block:
                        break
                    block.append(l); i += 1
                self.out.append(self.listing(block))
                continue
            para.append(line); i += 1
        flush()
        if self.section_open:
            self.out.append("</section>")
        return "\n".join(self.out)

    def heading(self, level: int, text: str):
        hid = self.anchor(text)
        if level == 1:
            self.headings.append((1, text, hid))
            return  # the page header carries the title
        if level == 2:
            if self.section_open:
                self.out.append("</section>")
            self.out.append(f'<section id="{hid}">')
            self.section_open = True
            m = re.match(r"^(\d+)\.\s+(.*)$", text)
            inner = f'<span class="n">{m.group(1)}</span>{inline(m.group(2))}' if m else inline(text)
            self.out.append(f"<h2>{inner}</h2>")
        else:
            m = METHOD.match(text)
            inner = (f'<span class="method {m.group(1).lower()}">{m.group(1)}</span> <span class="path">'
                     f"{inline(m.group(2))}</span>") if m else inline(text)
            self.counts["routes"] += bool(m)
            self.out.append(f'<h{level} id="{hid}">{inner}</h{level}>')
            if level == 3:
                self.current_h3 = re.sub(r"`", "", text)
        self.headings.append((level, text, hid))

    def table(self, rows: list[str]):
        cells = [[c.strip() for c in CELL_SPLIT.split(r.strip())[1:-1]] for r in rows]
        head, body = cells[0], cells[2:]
        surface, self.surface = self.surface, None
        self.counts["field_tables"] += bool(surface)
        if self.current_h3 == "MCP tools" and not self.counts["mcp_tools"]:
            self.counts["mcp_tools"] = len(body)  # the first table under "MCP tools" lists them
        cls = "table fields" if surface else "table"
        out = [f'<div class="{cls}">']
        if surface:
            out.append(f'<div class="surface"><span>Fields</span><code>{html.escape(surface)}</code></div>')
        out.append("<table><thead><tr>" + "".join(f"<th>{inline(h)}</th>" for h in head) + "</tr></thead><tbody>")
        for row in body:
            rid = ""
            if surface and row:
                path = re.search(r"`([^`]+)`", row[0])
                if path:
                    rid = f"f{len(self.fields)}"
                    self.fields.append({"p": path.group(1), "s": surface, "id": rid,
                                        "h": self.current_h3 if not surface.startswith("shared:") else surface})
            attr = f' id="{rid}"' if rid else ""
            out.append(f"<tr{attr}>" + "".join(f"<td>{inline(c)}</td>" for c in row) + "</tr>")
        out.append("</tbody></table></div>")
        self.out.append("".join(out))

    def listing(self, block: list[str]) -> str:
        items: list[tuple[int, bool, str]] = []
        for l in block:
            m = LIST_ITEM.match(l)
            if m:
                items.append((len(m.group(1)), m.group(2)[0].isdigit(), m.group(3)))
            elif items:
                ind, ordered, text = items[-1]
                items[-1] = (ind, ordered, text + " " + l.strip())
        html_out: list[str] = []
        stack: list[tuple[int, str]] = []
        for ind, ordered, text in items:
            tag = "ol" if ordered else "ul"
            while stack and ind < stack[-1][0]:
                html_out.append(f"</li></{stack.pop()[1]}>")
            if not stack or ind > stack[-1][0]:
                html_out.append(f"<{tag}>")
                stack.append((ind, tag))
            else:
                html_out.append("</li>")
            html_out.append(f"<li>{inline(text)}")
        while stack:
            html_out.append(f"</li></{stack.pop()[1]}>")
        return "".join(html_out)


def page(md: str) -> str:
    b = Builder(md)
    body = b.build()
    title = next((t for lvl, t, _ in b.headings if lvl == 1), "API reference")
    lede = ""
    m = re.search(r"^# .*?\n\n(.*?)\n\n", md, re.S)
    if m:
        lede = inline(" ".join(x.strip() for x in m.group(1).splitlines()))
        body = body.replace(f"<p>{lede}</p>", "", 1)
    nav = []
    for level, text, hid in b.headings:
        if level == 2:
            mm = re.match(r"^(\d+)\.\s+(.*)$", text)
            num, label = (mm.group(1), mm.group(2)) if mm else ("", text)
            nav.append(f'<li class="l2"><a href="#{hid}"><span class="n">{num}</span>{inline(label)}</a></li>')
        elif level == 3:
            nav.append(f'<li class="l3"><a href="#{hid}">{inline(text)}</a></li>')
    tools = {f["s"] for f in b.fields if f["s"].startswith("tool:")}
    facts = [("REST routes", b.counts["routes"]), ("MCP tools", b.counts["mcp_tools"]),
             ("protocol tools", len(tools)), ("field tables, tested against real outputs", b.counts["field_tables"])]
    index = [{"t": re.sub(r"`", "", t), "id": hid, "l": lvl} for lvl, t, hid in b.headings if lvl in (3, 4)]
    data = json.dumps({"fields": b.fields, "headings": index}, ensure_ascii=False).replace("</", "<\\/")
    tpl = TEMPLATE
    return (tpl.replace("{{TITLE}}", html.escape(title))
               .replace("{{LEDE}}", lede)
               .replace("{{FACTS}}", "".join(f"<li><b>{v}</b> {k}</li>" for k, v in facts))
               .replace("{{NAV}}", "\n".join(nav))
               .replace("{{BODY}}", body)
               .replace("{{DATA}}", data))


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(page(src.read_text(encoding="utf-8")), encoding="utf-8")
    print(f"wrote {dst} ({dst.stat().st_size // 1024} KB)")
