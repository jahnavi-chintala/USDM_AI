"""Describe protocol figures with a local vision-language model (VLM).

For each figure, the VLM is asked two separate questions (small models do
better with one task per prompt):
  * diagrams (flow charts, study schemas): a Mermaid flowchart, validated with
    Mermaid's own parser (tools/mermaid_validate) and cross-checked against the
    figure's OCR text so invented labels are flagged;
  * every non-logo figure: a factual prose description.

Results are cached per (image, model, prompt version), so the conversion
pipeline can attach them without re-running a slow CPU model.
"""

import argparse
import hashlib
import json
import logging
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PIL import Image

log = logging.getLogger("figure_vlm")

PROMPT_VERSION = "v2"
VALIDATOR = Path(__file__).parent / "tools" / "mermaid_validate" / "validate.mjs"
# Docling PictureClassificationLabel values. Mermaid flowcharts only suit diagrams.
DIAGRAM_CLASSES = {"flow_chart", "other_chart"}
SKIP_CLASSES = {"logo", "icon", "signature", "stamp", "qr_code", "bar_code"}
# A figure is skipped (no VLM, and no chunk unless it has text) only when the
# classifier is sure AND it is small: a mis-labelled study schema is neither.
SKIP_MIN_CONFIDENCE = 0.8
SKIP_MAX_PAGE_FRACTION = 0.05

MERMAID_PROMPT = (
    "This image is a diagram from a clinical trial protocol. Convert it into a "
    "Mermaid flowchart.\n"
    "Rules:\n"
    "- Start with `flowchart LR`.\n"
    "- Create one node per box in the diagram, with a short id (A, B, C ...).\n"
    "- The node label must contain ALL the text inside that box, copied exactly "
    "as written. Separate lines with <br/>. Wrap every label in double quotes.\n"
    "- Add an arrow (-->) for each connection drawn between boxes.\n"
    "- If boxes are arranged in columns without arrows, the columns are steps from "
    "left to right: boxes stacked in the same column are parallel alternatives, so "
    "never connect boxes of the same column to each other; connect every box to "
    "each box in the next column to its right.\n"
    "- Text outside any box (such as footnotes) becomes its own node.\n"
    "- Never put a line break inside a label; use <br/> instead.\n"
    "- Do not add any text that is not in the image.\n"
    "Output only the Mermaid code inside a ```mermaid code block."
)

DESCRIPTION_PROMPT = (
    "This figure is from a clinical trial protocol. Describe it for someone who "
    "cannot see it: say what kind of figure it is, then explain what it shows, "
    "including all text in the figure exactly as written and how the parts relate "
    "to each other.\n"
    "Rules: keep every abbreviation and acronym exactly as written and never expand "
    "or explain them; do not infer details that are not stated (such as sex, age or "
    "the meaning of a term); if something is unclear, say so instead of guessing."
)


@dataclass
class FigureResult:
    model: str
    prompt_version: str
    description: str | None = None
    mermaid: str | None = None
    mermaid_valid: bool | None = None
    mermaid_error: str | None = None
    ocr_words_covered: float | None = None  # share of OCR words found in the Mermaid labels
    unsupported_words: list[str] = field(default_factory=list)  # label words absent from OCR
    seconds: float = 0.0
    page: int | None = None  # where the figure is, so results survive small crop changes
    bbox: list[float] | None = None  # (l, t, r, b), PDF points, bottom-left origin
    doc_key: str | None = None  # document_key(): page/bbox matching is only valid within one document


class VlmRunner:
    """Thin wrapper over a Hugging Face image-text-to-text model on CPU."""

    def __init__(self, repo: str, max_new_tokens: int = 700, dtype: str = "float32",
                 threads: int | None = None):
        import torch
        from transformers import AutoModelForImageTextToText, AutoProcessor

        # float32 by default: CPUs without native bfloat16 (pre-AVX512-BF16,
        # e.g. laptop i7s) emulate bf16 and run many times slower.
        if threads:
            torch.set_num_threads(threads)
        self.repo = repo
        self.max_new_tokens = max_new_tokens
        self.processor = AutoProcessor.from_pretrained(repo)
        self.model = AutoModelForImageTextToText.from_pretrained(repo, dtype=getattr(torch, dtype))
        self.model.eval()

    def ask(self, image: Image.Image, prompt: str) -> str:
        import torch

        messages = [{"role": "user", "content": [
            {"type": "image", "image": image},
            {"type": "text", "text": prompt},
        ]}]
        inputs = self.processor.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=True,
            return_dict=True, return_tensors="pt",
        )
        start = time.time()
        with torch.inference_mode():
            out = self.model.generate(**inputs, max_new_tokens=self.max_new_tokens, do_sample=False)
        n_in, n_out = inputs["input_ids"].shape[1], out.shape[1] - inputs["input_ids"].shape[1]
        log.info("%s: %d input tokens, %d generated in %.0fs", self.repo, n_in, n_out,
                 time.time() - start)
        return self.processor.decode(out[0, n_in:], skip_special_tokens=True).strip()


class OpenRouterRunner:
    """Hosted VLM via OpenRouter (OpenAI-compatible chat API).

    Only the figure image and the prompt are sent. Requests ask OpenRouter to
    route only to providers that neither collect data nor retain it (ZDR);
    if none can serve the model, the request fails instead of falling back.
    The API key comes from the OPENROUTER_API_KEY environment variable or a
    .env file next to this script; it is never logged.
    """

    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, model: str, max_tokens: int = 2500):
        self.repo = f"openrouter:{model}"
        self.model = model
        self.max_tokens = max_tokens
        self.key = _api_key("OPENROUTER_API_KEY")

    def ask(self, image: Image.Image, prompt: str) -> str:
        import base64
        import io

        import httpx

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        body = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            "provider": {"data_collection": "deny", "zdr": True, "allow_fallbacks": False},
        }
        start = time.time()
        from retrieve import require_online

        require_online()  # OPENROUTER_OFFLINE=1 turns any VLM call into an error
        resp = httpx.post(self.URL, json=body, timeout=300,
                          headers={"Authorization": f"Bearer {self.key}"})
        if resp.status_code != 200:
            raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        usage = data.get("usage", {})
        log.info("%s: %s prompt tokens, %s generated in %.0fs", self.repo,
                 usage.get("prompt_tokens"), usage.get("completion_tokens"), time.time() - start)
        return data["choices"][0]["message"]["content"].strip()


def _api_key(name: str) -> str:
    import os

    if os.environ.get(name):
        return os.environ[name]
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == name and value.strip():
                return value.strip().strip('"').strip("'")
    raise RuntimeError(f"{name} not set: add it to the environment or to {env_file}")


def extract_mermaid(text: str) -> str | None:
    m = re.search(r"```(?:mermaid)?\s*\n(.*?)(?:```|$)", text, re.S)
    code = (m.group(1) if m else text).strip()
    return code if re.match(r"(flowchart|graph)\s+(LR|RL|TB|TD|BT)\b", code) else None


_NODE = re.compile(r"\b([A-Za-z]\w*)\[(?!\")(.*?)\](?=\s*(?:-->|---|$|\n|;|&))", re.S)


def repair_mermaid(code: str) -> str:
    """Fix the most common small-model syntax errors without changing content.

    Unquoted labels containing parentheses, colons or raw line breaks break
    Mermaid's parser; quote them and turn line breaks into <br/>.
    """
    def quote(m: re.Match) -> str:
        lines = [l.strip() for l in m.group(2).splitlines() if l.strip()]
        label = "<br/>".join(lines).replace('"', "#quot;")
        return f'{m.group(1)}["{label}"]'

    return _NODE.sub(quote, code)


def validate_mermaid(code: str) -> tuple[bool, str | None]:
    """Parse with Mermaid's real grammar (Node.js); returns (ok, error)."""
    with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False, encoding="utf-8") as f:
        f.write(code)
        path = f.name
    try:
        proc = subprocess.run(["node", str(VALIDATOR), path], capture_output=True,
                              text=True, encoding="utf-8", timeout=120)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("{")), None)
        if line is None:
            return False, (proc.stderr or "validator produced no output").strip()[:500]
        res = json.loads(line)
        return res["ok"], res.get("error")
    finally:
        Path(path).unlink(missing_ok=True)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1}


def label_check(mermaid: str, ocr_text: str) -> tuple[float, list[str]]:
    """Compare Mermaid label words with the figure's OCR words.

    Returns the share of OCR words present in the labels (coverage) and the
    label words OCR never saw (candidates for invented text; OCR misreads
    such as "Id·" for "PI" also show up here, so this is a review aid).
    """
    labels = " ".join(re.findall(r'"([^"]*)"', mermaid)).replace("<br/>", " ")
    label_w, ocr_w = _words(labels), _words(ocr_text)
    coverage = len(label_w & ocr_w) / len(ocr_w) if ocr_w else 0.0
    return round(coverage, 3), sorted(label_w - ocr_w)


def cache_key(image: Image.Image, repo: str) -> str:
    h = hashlib.sha256(image.tobytes())
    h.update(f"{repo}|{PROMPT_VERSION}|{MERMAID_PROMPT}|{DESCRIPTION_PROMPT}".encode())
    return h.hexdigest()[:24]


def describe(runner, image: Image.Image, figure_class: str | None, ocr_text: str,
             cache_dir: Path, page: int | None = None,
             bbox: list[float] | None = None, doc_key: str | None = None) -> FigureResult:
    """Describe one figure, using the cache when possible (the caller decides skipping)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"{cache_key(image, runner.repo)}.json"
    if cached.exists():
        return FigureResult(**json.loads(cached.read_text(encoding="utf-8")))

    start = time.time()
    res = FigureResult(model=runner.repo, prompt_version=PROMPT_VERSION, page=page, bbox=bbox,
                       doc_key=doc_key)
    res.description = runner.ask(image, DESCRIPTION_PROMPT)
    if figure_class in DIAGRAM_CLASSES:
        raw = runner.ask(image, MERMAID_PROMPT)
        res.mermaid = extract_mermaid(raw)
        if res.mermaid is None:
            res.mermaid_valid, res.mermaid_error = False, f"no Mermaid flowchart in output: {raw[:300]}"
        else:
            res.mermaid_valid, res.mermaid_error = validate_mermaid(res.mermaid)
            if not res.mermaid_valid:
                repaired = repair_mermaid(res.mermaid)
                ok, err = validate_mermaid(repaired)
                if ok:
                    res.mermaid, res.mermaid_valid, res.mermaid_error = repaired, True, None
            res.ocr_words_covered, res.unsupported_words = label_check(res.mermaid, ocr_text)
    res.seconds = round(time.time() - start, 1)
    cached.write_text(json.dumps(asdict(res), indent=2, ensure_ascii=False), encoding="utf-8")
    return res


def figure_class(pic) -> tuple[str | None, float]:
    """Docling's most confident picture class and its confidence."""
    if not (pic.meta and pic.meta.classification and pic.meta.classification.predictions):
        return None, 0.0
    pred = pic.meta.classification.get_main_prediction()
    return pred.class_name, pred.confidence or 0.0


def _figure_class(pic) -> str | None:
    return figure_class(pic)[0]


def skip_figure(pic, doc) -> bool:
    """Logo/icon/signature/...: classified with confidence >= 0.8 and under 5 % of the page."""
    cls, conf = figure_class(pic)
    if cls not in SKIP_CLASSES or conf < SKIP_MIN_CONFIDENCE or not pic.prov:
        return False
    page = doc.pages.get(pic.prov[0].page_no)
    if page is None or not page.size.width or not page.size.height:
        return False
    return pic.prov[0].bbox.area() / (page.size.width * page.size.height) < SKIP_MAX_PAGE_FRACTION


def document_key(doc) -> str:
    """Identity of the source document for the figure cache, stable across re-assembly.

    The assembled document carries no PDF hash, and its text changes with
    post-processing; page geometry plus the original (pre-cleanup) words of the
    title page identifies the PDF without depending on the cleanup steps. Words,
    not item strings: post-processing may merge title-page items (label/value
    pairing), which changes the items but not the words.
    """
    h = hashlib.sha256(str(len(doc.pages)).encode())
    for no in sorted(doc.pages):
        h.update(f"|{doc.pages[no].size.width:.1f}x{doc.pages[no].size.height:.1f}".encode())
    words = sorted(w for t in doc.texts if t.prov and t.prov[0].page_no == 1 for w in (t.orig or "").split())
    h.update(("\n" + " ".join(words)).encode())
    return h.hexdigest()[:16]


def _iou(a: list[float], b: list[float]) -> float:
    l, t, r, bot = max(a[0], b[0]), min(a[1], b[1]), min(a[2], b[2]), max(a[3], b[3])
    inter = max(0.0, r - l) * max(0.0, t - bot)
    area = lambda x: (x[2] - x[0]) * (x[1] - x[3])
    return inter / (area(a) + area(b) - inter) if inter else 0.0


def attach_cached(doc, model: str, cache_dir: Path) -> dict[str, int]:
    """Attach cached VLM results to the document's figures (no model is run).

    Each figure gets its description as picture meta and, when valid, its
    Mermaid diagram as a code item placed first under the figure. A cache entry
    matches on the exact figure pixels, or else on page and position (IoU >=
    0.8) within the same document (doc_key), since another layout model may
    crop the same figure slightly differently. Figures without a result are
    listed so the explicit `figure_vlm.py` step can be run for them.
    """
    from docling_core.types.doc.document import DescriptionMetaField
    from docling_core.types.doc.labels import CodeLanguageLabel

    entries = []
    for f in cache_dir.glob("*.json"):
        r = FigureResult(**json.loads(f.read_text(encoding="utf-8")))
        if r.model == model and r.prompt_version == PROMPT_VERSION:
            entries.append((f.stem, r))
    stats = {"described": 0, "mermaid": 0, "missing": 0}
    doc_key = document_key(doc)
    for pic in doc.pictures:
        if skip_figure(pic, doc) or not pic.prov:
            continue
        page = pic.prov[0].page_no
        b = pic.prov[0].bbox.to_bottom_left_origin(doc.pages[page].size.height)
        box = [b.l, b.t, b.r, b.b]
        img = pic.get_image(doc)
        key = cache_key(img.convert("RGB"), model) if img is not None else None
        res = next((r for k, r in entries if k == key), None) or next(
            (r for _, r in entries if r.doc_key == doc_key and r.page == page and r.bbox
             and _iou(r.bbox, box) >= 0.8), None)
        if res is None:
            stats["missing"] += 1
            log.warning("No cached %s result for the figure on p.%d; run figure_vlm.py for it", model, page)
            continue
        if res.description:
            if pic.meta is None:
                from docling_core.types.doc.document import PictureMeta
                pic.meta = PictureMeta()
            # Mark generated text as such, so readers and RAG answers can tell it
            # apart from text taken from the PDF.
            label = f"*Figure description generated by {model.split(':')[-1]}; verify against the image.*"
            pic.meta.description = DescriptionMetaField(text=f"{label}\n\n{res.description}",
                                                        created_by=model)
            stats["described"] += 1
        if res.mermaid and res.mermaid_valid:
            doc.add_code(text=res.mermaid, code_language=CodeLanguageLabel.UNKNOWN, parent=pic)
            pic.children.insert(0, pic.children.pop())  # diagram first, then OCR text
            stats["mermaid"] += 1
    return stats


def label_mermaid_fences(markdown: str) -> str:
    """Docling writes code blocks without a language; tag Mermaid ones for renderers."""
    return re.sub(r"```\n((?:flowchart|graph)\s+(?:LR|RL|TB|TD|BT)\b)", r"```mermaid\n\1", markdown)


def main() -> None:
    """Run one model over the figures of a converted document (for comparison)."""
    from docling_core.types.doc import DoclingDocument, TextItem

    from project_paths import main_json

    parser = argparse.ArgumentParser()
    parser.add_argument("--doc", default=str(main_json()))
    parser.add_argument("--model", default="ibm-granite/granite-vision-3.3-2b")
    parser.add_argument("--cache", default="vlm_cache")
    parser.add_argument("--pages", type=int, nargs="*", help="only figures on these pages")
    parser.add_argument("--dtype", default="float32", choices=["float32", "bfloat16", "float16"])
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--openrouter", action="store_true",
                        help="treat --model as an OpenRouter model id (sends figure images off-machine)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, force=True)
    for noisy in ("httpx", "docling_core"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    doc_path = Path(args.doc)
    doc = DoclingDocument.load_from_json(doc_path)
    runner = (OpenRouterRunner(args.model) if args.openrouter
              else VlmRunner(args.model, dtype=args.dtype, threads=args.threads))
    doc_key = document_key(doc)
    for pic in doc.pictures:
        page = pic.prov[0].page_no if pic.prov else 0
        if args.pages and page not in args.pages:
            continue
        cls = _figure_class(pic)
        if skip_figure(pic, doc):
            log.info("p%d %s: skipped", page, cls)
            continue
        ocr = " ".join(
            it.text for it, _ in doc.iterate_items(root=pic, traverse_pictures=True)
            if isinstance(it, TextItem)
        )
        image = Image.open(doc_path.parent / str(pic.image.uri)).convert("RGB")
        b = pic.prov[0].bbox.to_bottom_left_origin(doc.pages[page].size.height)
        res = describe(runner, image, cls, ocr, Path(args.cache), page, [b.l, b.t, b.r, b.b], doc_key)
        log.info("p%d %s: %s", page, cls, json.dumps(asdict(res), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
