// A small Markdown renderer that escapes everything: it builds DOM nodes (text nodes for all text), never HTML
// strings. Supported: ATX headings (with '{#S5.1}' anchors shown as section chips), paragraphs (single newlines
// kept as line breaks: protocol lists rely on them), '-'/'*'/'+' and '1.'/'1)' lists with nesting (ordered markers
// are shown verbatim), GFM pipe tables, block quotes, fenced code, rules, '<!-- p. N -->' page markers, and inline
// code, **strong**, *emphasis*, links (http, https, mailto and '#' only), '<br>' and caret footnote markers
// ('Response^a' -> superscript).

import { frag, h } from "./dom.js";

const LIST_RE = /^(\s*)([-*+]|\d{1,3}[.)])\s+(.*)$/;
const FENCE_RE = /^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)\s*$/;
const PAGE_RE = /^\s*<!--\s*(pp?\.\s*[^>]*?)\s*-->\s*$/i;
const COMMENT_RE = /^\s*<!--[\s\S]*?-->\s*$/;
const HEADING_RE = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const HR_RE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE_RE = /^\s{0,3}>\s?/;
const ANCHOR_RE = /^(.*?)\s*\{#([^}\s]+)\}\s*$/;

export function renderMarkdown(text, opts = {}) {
  const root = h("div", { class: ["md", opts.class] });
  renderBlocks(String(text ?? "").replace(/\r\n?/g, "\n").split("\n"), root, opts);
  return root;
}

export function renderInline(text, opts = {}) {
  return frag(...inline(String(text ?? ""), opts));
}

function startsBlock(line, next) {
  return FENCE_RE.test(line) || PAGE_RE.test(line) || HEADING_RE.test(line) || HR_RE.test(line)
    || QUOTE_RE.test(line) || LIST_RE.test(line) || (line.includes("|") && next !== undefined && isSeparator(next));
}

function renderBlocks(lines, root, opts) {
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i += 1; continue; }
    let m = line.match(FENCE_RE);
    if (m) {
      const fence = m[1];
      const body = [];
      i += 1;
      while (i < lines.length && !(lines[i].trim().startsWith(fence[0].repeat(fence.length)) && lines[i].trim().replace(/[`~]/g, "") === "")) {
        body.push(lines[i]);
        i += 1;
      }
      i += 1;
      root.append(h("pre", null, h("code", m[2] ? { class: `lang-${m[2]}` } : null, body.join("\n"))));
      continue;
    }
    m = line.match(PAGE_RE);
    if (m) {
      root.append(h("div", { class: "md-page", role: "separator", "aria-label": `page marker ${m[1]}` }, m[1]));
      i += 1;
      continue;
    }
    if (COMMENT_RE.test(line)) {
      root.append(h("div", { class: "md-comment" }, line.trim()));
      i += 1;
      continue;
    }
    m = line.match(HEADING_RE);
    if (m) {
      root.append(heading(m[1].length, m[2], opts));
      i += 1;
      continue;
    }
    if (HR_RE.test(line)) {
      root.append(h("hr"));
      i += 1;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && isSeparator(lines[i + 1])) {
      i = table(lines, i, root, opts);
      continue;
    }
    if (QUOTE_RE.test(line)) {
      const inner = [];
      while (i < lines.length && lines[i].trim() && QUOTE_RE.test(lines[i])) {
        inner.push(lines[i].replace(QUOTE_RE, ""));
        i += 1;
      }
      const bq = h("blockquote");
      renderBlocks(inner, bq, opts);
      root.append(bq);
      continue;
    }
    if (LIST_RE.test(line)) {
      i = list(lines, i, root, opts);
      continue;
    }
    // paragraph: until a blank line or the start of another block; single newlines are kept as line breaks
    const para = h("p");
    let first = true;
    while (i < lines.length && lines[i].trim() && (first || !startsBlock(lines[i], lines[i + 1]))) {
      if (!first) para.append(h("br"));
      para.append(...inline(lines[i].trim(), opts));
      first = false;
      i += 1;
    }
    root.append(para);
  }
}

function heading(level, text, opts) {
  const tag = `h${Math.min(6, level + 2)}`;
  const m = text.match(ANCHOR_RE);
  const el = h(tag, { class: `md-h${level}` });
  if (m) {
    el.append(h("span", null, ...inline(m[1], opts)), sectionChip(m[2], opts));
    el.dataset.section = m[2];
  } else {
    el.append(...inline(text, opts));
  }
  return el;
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|") && !s.endsWith("\\|")) s = s.slice(0, -1);
  const cells = [];
  let cur = "";
  for (let k = 0; k < s.length; k += 1) {
    const c = s[k];
    if (c === "\\" && s[k + 1] === "|") { cur += "|"; k += 1; continue; }
    if (c === "|") { cells.push(cur.trim()); cur = ""; continue; }
    cur += c;
  }
  cells.push(cur.trim());
  return cells;
}

function isSeparator(line) {
  if (!line || !line.includes("-")) return false;
  const cells = splitRow(line);
  return cells.length > 0 && cells.every((c) => /^:?-+:?$/.test(c.replace(/\s+/g, "")));
}

function table(lines, i, root, opts) {
  const header = splitRow(lines[i]);
  const aligns = splitRow(lines[i + 1]).map((c) => {
    const s = c.replace(/\s+/g, "");
    if (s.startsWith(":") && s.endsWith(":")) return "center";
    if (s.endsWith(":")) return "right";
    return null;
  });
  i += 2;
  const rows = [];
  while (i < lines.length && lines[i].trim() && lines[i].includes("|") && !PAGE_RE.test(lines[i])) {
    rows.push(splitRow(lines[i]));
    i += 1;
  }
  const width = Math.max(header.length, ...rows.map((r) => r.length));
  const cell = (tag, text, col) => {
    const el = h(tag, null, ...inline(text || "", opts));
    if (aligns[col]) el.style.textAlign = aligns[col];
    return el;
  };
  const thead = h("thead", null, h("tr", null, Array.from({ length: width }, (_, c) => cell("th", header[c], c))));
  const tbody = h("tbody", null, rows.map((r) => h("tr", null, Array.from({ length: width }, (_, c) => cell("td", r[c], c)))));
  root.append(h("div", { class: "md-table-wrap" }, h("table", null, thead, tbody)));
  return i;
}

function list(lines, i, root, opts) {
  const top = h("ul", { class: "md-list" });
  const stack = [{ indent: -1, ul: top, last: null }];
  stack[0].indent = (lines[i].match(LIST_RE) || [""])[1].length;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // a blank line ends the list unless the next line continues it
      if (i + 1 < lines.length && LIST_RE.test(lines[i + 1])) { i += 1; continue; }
      break;
    }
    const m = line.match(LIST_RE);
    if (!m) {
      const cur = stack[stack.length - 1];
      if (cur.last && /^\s{2,}\S/.test(line) && !startsBlock(line, lines[i + 1])) {
        cur.last.append(h("br"), ...inline(line.trim(), opts));
        i += 1;
        continue;
      }
      break;
    }
    const indent = m[1].replace(/\t/g, "    ").length;
    while (stack.length > 1 && indent < stack[stack.length - 1].indent) stack.pop();
    let cur = stack[stack.length - 1];
    if (indent > cur.indent && cur.last) {
      const nested = h("ul", { class: "md-list" });
      cur.last.append(nested);
      cur = { indent, ul: nested, last: null };
      stack.push(cur);
    }
    const marker = m[2];
    const ordered = /\d/.test(marker);
    const li = h("li", { class: ordered ? "li-marked" : null },
      ordered ? h("span", { class: "li-marker" }, marker) : null, ...inline(m[3], opts));
    cur.ul.append(li);
    cur.last = li;
    i += 1;
  }
  root.append(top);
  return i;
}

// ---------------------------------------------------------------- inline

const SUP_RE = /\^([A-Za-z0-9†‡§¶*#](?:[A-Za-z0-9†‡§¶*#,]{0,10}[A-Za-z0-9†‡§¶*#])?)/y;

const INLINE = [
  { start: "\\", re: /\\([\\`*_{}[\]()#+\-.!|^~<>])/y, make: (m) => m[1] },
  { start: "`", re: /(`+)([^`]|[^`][\s\S]*?[^`])\1(?!`)/y, make: (m) => h("code", null, m[2]) },
  { start: "<", re: /<!--\s*(pp?\.\s*[^>]*?)\s*-->/iy, make: (m) => h("span", { class: "md-page-inline", title: "page marker" }, m[1]) },
  { start: "<", re: /<br\s*\/?>/iy, make: () => h("br") },
  { start: "{", re: /\{#([A-Za-z0-9~._:-]+)\}/y, make: (m, o) => sectionChip(m[1], o) },
  { start: "*", re: /\*\*(?=\S)([\s\S]*?\S)\*\*/y, make: (m, o) => h("strong", null, ...inline(m[1], o)) },
  { start: "_", re: /__(?=\S)([\s\S]*?\S)__(?![A-Za-z0-9])/y, wordStart: true, make: (m, o) => h("strong", null, ...inline(m[1], o)) },
  { start: "*", re: /\*(?=[^\s*])([\s\S]*?[^\s*])\*(?!\*)/y, make: (m, o) => h("em", null, ...inline(m[1], o)) },
  { start: "_", re: /_(?=[^\s_])([\s\S]*?[^\s_])_(?![A-Za-z0-9])/y, wordStart: true, make: (m, o) => h("em", null, ...inline(m[1], o)) },
  { start: "[", re: /\[([^\]\n]+)\]\(([^()\s]+)(?:\s+"[^"\n]*")?\)/y, make: (m, o) => link(m[1], m[2], o) },
  { start: "^", re: SUP_RE, afterNonSpace: true, make: (m) => h("sup", { class: "fn", title: "footnote marker" }, m[1]) },
];
const TRIGGERS = new Set(INLINE.map((r) => r.start));

function inline(text, opts) {
  const out = [];
  let buf = "";
  let i = 0;
  while (i < text.length) {
    const c = text[i];
    let matched = false;
    if (TRIGGERS.has(c)) {
      for (const rule of INLINE) {
        if (rule.start !== c) continue;
        if (rule.wordStart && i > 0 && /[A-Za-z0-9]/.test(text[i - 1])) continue;
        if (rule.afterNonSpace && (i === 0 || /\s/.test(text[i - 1]))) continue;
        rule.re.lastIndex = i;
        const m = rule.re.exec(text);
        if (m && m.index === i) {
          if (buf) { out.push(buf); buf = ""; }
          out.push(rule.make(m, opts));
          i += m[0].length;
          matched = true;
          break;
        }
      }
    }
    if (!matched) {
      buf += c;
      i += 1;
    }
  }
  if (buf) out.push(buf);
  return out;
}

function link(label, url, opts) {
  const safe = /^(https?:|mailto:)/i.test(url) || url.startsWith("#");
  if (!safe) return frag(...inline(label, opts), ` (${url})`);
  const external = !url.startsWith("#");
  return h("a", { href: url, target: external ? "_blank" : null, rel: external ? "noopener noreferrer" : null },
    ...inline(label, opts));
}

export function sectionChip(id, opts = {}) {
  if (opts.onSection) {
    const b = h("button", { type: "button", class: "chip", title: `Read section ${id}` }, id);
    b.addEventListener("click", (event) => {
      event.preventDefault(); // inside a <summary>: run the action, do not toggle
      event.stopPropagation();
      opts.onSection(id);
    });
    return b;
  }
  return h("span", { class: "chip", title: "section id" }, id);
}
