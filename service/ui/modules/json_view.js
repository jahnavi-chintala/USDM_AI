// Raw JSON: collapsible, copyable, syntax-coloured (spans with textContent), rendered when first opened.

import { copyButton, fmtBytes, h } from "./dom.js";

const HIGHLIGHT_LIMIT = 400 * 1024; // larger documents are shown as plain text (fast)
const TOKEN_RE = /("(?:\\u[a-fA-F0-9]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g;

export function jsonText(data) {
  try {
    return typeof data === "string" ? data : JSON.stringify(data, null, 2);
  } catch (_) {
    return String(data);
  }
}

/** A <pre> with the coloured JSON of `data`. */
export function jsonPre(data, { wrap = false } = {}) {
  const text = jsonText(data);
  const code = h("code");
  if (text.length > HIGHLIGHT_LIMIT) {
    code.textContent = text;
  } else {
    let last = 0;
    for (const m of text.matchAll(TOKEN_RE)) {
      if (m.index > last) code.append(text.slice(last, m.index));
      let cls = "j-num";
      if (m[0].startsWith('"')) cls = m[2] ? "j-key" : "j-str";
      else if (/^(true|false|null)$/.test(m[0])) cls = "j-lit";
      if (m[2]) {
        code.append(h("span", { class: cls }, m[0].slice(0, m[0].length - m[2].length)), m[2]);
      } else {
        code.append(h("span", { class: cls }, m[0]));
      }
      last = m.index + m[0].length;
    }
    if (last < text.length) code.append(text.slice(last));
  }
  return h("pre", { class: ["json-pre", wrap ? "wrap" : null], tabindex: "0" }, code);
}

/** <details> "Raw JSON" with size, copy button and the coloured JSON (built on first open). */
export function jsonBlock(data, { title = "Raw JSON", open = false } = {}) {
  const text = jsonText(data);
  const details = h("details", { class: "json" });
  const summary = h("summary", null, h("span", null, title),
    h("span", { class: "json-size" }, fmtBytes(new Blob([text]).size)), copyButton(() => text, "Copy JSON"));
  details.append(summary);
  let built = false;
  const build = () => {
    if (built) return;
    built = true;
    details.append(jsonPre(data));
  };
  details.addEventListener("toggle", () => { if (details.open) build(); });
  if (open) {
    details.open = true;
    build();
  }
  return details;
}
