# Clinical protocol ingestion and retrieval

Turns one clinical trial protocol PDF at a time into structured, verifiable data for an agent that
builds USDM 4 study definitions: Markdown and lossless Docling JSON, a section tree with ICH M11
roles, a structured Schedule of Activities, RAG chunks and parent units, hybrid retrieval, and a
tool API with provenance and quote checks.

This repository holds code only. Protocol PDFs, converted outputs, caches, embeddings, evaluation
gold sets and reports stay local and are never committed (see `.gitignore`).

## Setup

- Python 3.13 (tested), CPU is enough (float32). `python -m venv .venv`, then
  `.venv\Scripts\python -m pip install -r requirements.txt`.
- `tools/mermaid_validate`: `npm install` there (validates AI-generated Mermaid diagrams).
- Copy `.env.example` to `.env` and set `OPENROUTER_API_KEY` (only needed for the steps that call
  OpenRouter; every request asks for zero-data-retention routing).
- Point the scripts at your main protocol: set `MAIN_PROTOCOL_PDF`, or create `local_config.json`
  (untracked) with `{"main_pdf": "<file name or path>"}` (`project_paths.py`).

## Run

```powershell
# 1. Convert: Docling in 15-page batches (cached), then post-processing
.venv\Scripts\python convert_protocol.py [protocol.pdf --out output_dir] [--figure-model ""]
# 2. Check nothing was lost: per-page word coverage and table checks against the PDF text layer
.venv\Scripts\python validate_output.py
# 3. Chunks, parent units, sections, tables, glossary
.venv\Scripts\python chunk_protocol.py [output_dir\protocol.json --out output_dir\chunks]
# 4. Embeddings (sends chunk text to OpenRouter; only changed chunks are re-embedded)
.venv\Scripts\python embed_chunks.py --chunks output_dir\chunks\chunks.jsonl embed
# 5. Agent toolset over one converted protocol
.venv\Scripts\python protocol_api.py --help
```

Set `OPENROUTER_OFFLINE=1` to forbid any API call: cache misses then raise instead of calling out.

## Pipeline

| Stage | Module | What it does |
|---|---|---|
| Conversion | `convert_protocol.py`, `hybrid_backend.py` | Docling standard PDF pipeline (docling-parse text, pdfium page images), `layout_heron_101`, TableFormer ACCURATE, RapidOCR on bitmaps only; batches run in subprocesses and are cached per configuration |
| Post-processing | `protocol_postprocess.py`, `protocol_settings.py` | furniture, figure OCR (`figure_ocr.py`), list repair, symbol glyphs, repeated table titles and footnotes, text reconciliation, table lattice, table merge, heading levels, TOC, reading-order repair |
| Text repair | `text_reconcile.py` | superscripts / subscripts / footnote markers ("10^9/L", "Response^a"), dropped line-end hyphens, merged words, cover-page label/value pairs, all from pdfium glyph geometry |
| Tables | `table_lattice.py`, `table_merge.py` | TableFormer grids verified against the PDF's ruling lines and rebuilt on fully ruled (and row-ruled) tables; page-split tables merged |
| Structure | `structure.py` | heading levels from outline / numbering / TOC, section tree with stable ids and ICH M11 roles, reading-order repair |
| Figures | `figure_vlm.py` | optional AI figure descriptions and Mermaid, attached from a cache (the pipeline never calls a model itself) |
| Chunking | `chunk_protocol.py`, `parents.py`, `glossary.py` | 512-token chunks with section ids and roles, structural parent units, self-describing table rows, abbreviation glossary |
| Retrieval | `retrieve.py`, `embed_chunks.py` | BM25 + dense (qwen3-embedding-8b) + qwen3-reranker-8b; parent-aware context builder (`production_builder`) |
| Schedule of Activities | `soa.py` | visits with windows, activities, marks with qualifiers and footnotes |
| Agent toolset | `protocol_api.py` | `document_info`, `outline`, `find_sections`, `read_section`, `get_table`, `get_soa`, `search`, `abbreviation`, `quote_check`; see `docs/protocol_api.md` |
| Answers | `answer.py`, `eval_answers.py` | prose answers on the production context; claims-first grounded mode and answer-then-verify annotation |
| Evaluation | `eval_v1.py`, `keyterm_match.py`, `gen_questions.py`, `usdm_gold.py`, `regression.py`, `test_protocols.py` | retrieval/answer evaluation with paired statistics, synthetic questions, USDM key comparison, regression runs, numbers-only reports for private test protocols |

## Tests

No pytest needed: `.venv\Scripts\python tests\test_<name>.py` for each file in `tests/`. Most
tests build synthetic documents and PDFs; `test_protocol_api.py`, `test_soa.py` and
`test_usdm_gold.py` also read converted public protocols from `output_public/` when present.

## Data handling

- Documents and everything derived from them (outputs, chunks, embeddings, caches, gold sets,
  reports) are local only.
- Steps that send content off-machine (figure descriptions, embeddings, answers, judging) are
  explicit commands, use OpenRouter with `{"data_collection": "deny", "zdr": true,
  "allow_fallbacks": false}`, and refuse to run under `OPENROUTER_OFFLINE=1`.
- `test_protocols.py` reports private protocols as P1, P2, ... with aggregate numbers only.
