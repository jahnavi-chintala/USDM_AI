// Validate Mermaid diagrams with Mermaid's own parser.
// Usage: node validate.mjs <file.mmd> [...]  -> prints one JSON result per file.
import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// Mermaid expects a browser; give it a minimal DOM.
const dom = new JSDOM("<!doctype html><html><body></body></html>");
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.DOMParser = dom.window.DOMParser;
globalThis.Element = dom.window.Element;

const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });

for (const file of process.argv.slice(2)) {
  const text = readFileSync(file, "utf8");
  try {
    const result = await mermaid.parse(text);
    console.log(JSON.stringify({ file, ok: true, diagramType: result.diagramType }));
  } catch (err) {
    console.log(JSON.stringify({ file, ok: false, error: String(err?.message ?? err).slice(0, 500) }));
  }
}
// jsdom's window keeps the event loop alive; exit explicitly.
dom.window.close();
process.exit(0);
