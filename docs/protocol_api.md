# Protocol document API: tools for the USDM-building agent

`protocol_api.py` gives an LLM agent read access to **one** converted protocol (Docling JSON + `chunks/`). It is built for extraction into USDM 4, where enumeration questions ("all inclusion criteria", "every visit") matter more than point lookups, and where every value must be traceable to its source.

**Every result carries provenance**, so each USDM value can be traced back and checked:

```json
"provenance": {"section_id": "S6.1", "section_number": "6.1", "title": "6.1. Inclusion Criteria",
               "pages": [26, 27, 28], "refs": ["#/texts/179", "#/groups/6"]}
```

- `section_id` is a stable id from `structure.py`:
  - `S<number>` for a numbered section;
  - `S~<hash>-<n>` for an unnumbered one;
  - `S0` for the front matter.
- `pages` are PDF page numbers.
- `refs` are Docling item refs. `quote_check` returns them for any quote.

```python
from protocol_api import ProtocolDocument
doc = ProtocolDocument.open("output_public/Lilly_NCT03421379")   # folder holding the Docling JSON and chunks/
doc.read_section("inclusion")
doc.call_tool("quote_check", {"quote": "have a hemoglobin A1c value ≤10% at the time of screening"})
```

`open()` refuses a Docling JSON whose sha differs from the one the chunks were made from. Item refs are positional, so a regenerated JSON would otherwise return other text without any error.

## The tools

| Tool | Returns | Use it for |
|---|---|---|
| `document_info()` | <ul><li>Official and brief title, acronym.</li><li>Sponsor protocol number.</li><li>NCT / EudraCT / EU CT / IND / WHO UTN, **each with the verbatim line it was found in**, page and ref.</li><li>Version, amendment and date.</li><li>Sponsor name, with the method used to find it.</li><li>Cover-page label/value pairs.</li><li>Page, section and SoA-table counts.</li><li>The outermost section of each ICH M11 role.</li><li>Reading-order repairs.</li></ul> | First call: the identity of the study and a plan of where things are. |
| `outline(max_level)` | The section tree. Per section: id, number, title, level, role, first/last page, tokens (subtree and own text), children, number of tables/figures. | Planning which sections to read, and how big they are. |
| `find_sections(role, query)` | Ranked sections by ICH M11 role and/or fuzzy title. Section numbers work too ("6.2"). | Finding a section whose role tagging is uncertain. |
| `read_section(section)` | Complete Markdown of a section and its subsections, with per-block provenance. `section` can be an id (`S6.1`), a number (`6.1`) or a role (`inclusion`). | **Every enumeration.** This is complete; `search` is not. |
| `get_table(table)` | One whole table, chosen by id (`#/tables/5`) or by caption/heading words. Markdown, grid, merged cells, footnotes; page-split chains joined. `format="rows"` gives one line per row. | Intervention tables, objective/endpoint tables, lab panels, wide SoA tables. |
| `get_soa(visit, activity)` | The structured Schedule of Activities from `soa.py`: visits (label, header path, window, timing), activities, activity × visit cells (mark, qualifier, footnote markers), footnote texts, Markdown. Optional filter by visit and/or activity. | Encounters, Activities, ScheduledActivityInstances, Timings. |
| `search(query)` | The production retrieval context: the identity block plus ranked blocks (whole sections or parents, tables, passages). Each block has kind, text, section, pages and refs; glossary definitions are included. | Facts scattered through the body, such as a washout period or a dose rule in an appendix. |
| `abbreviation(term)` | Glossary lookup in both directions ("AE" → "adverse event", or the reverse), with where the term is defined. | Expanding abbreviations in extracted text. |
| `quote_check(quote, section_id)` | Whether the text occurs in the document, the match type, and every location. | **Verifying every extracted value** (see below). |

Tool definitions:
- `tool_schemas("anthropic")` gives `{name, description, input_schema}`.
- `tool_schemas("openai")` gives `{type: "function", function: {name, description, parameters}}`.

The schemas are JSON Schema and are tested against Draft 2020-12 and Draft 7.

`doc.call_tool(name, args)` validates the arguments and runs the tool. **It never raises**: errors come back as `{"error", "error_type"}`, so you can pass the result straight back to the model. Through `call_tool`, `read_section` returns at most 16,000 tokens per call; the rest comes through `cursor`.

## Which tool for which USDM class

| USDM class / attribute | Primary tool | Then |
|---|---|---|
| `StudyIdentifier` (sponsor id, NCT, EudraCT, EU CT, IND) | `document_info().identifiers`: value + `source.text` (the verbatim line) + page + ref | `quote_check(value)` |
| `StudyTitle` (official, brief, acronym), `StudyVersion` (version, date), sponsor `Organization` | `document_info().titles / version / sponsor / cover_page` | `read_section("title_page")` if a field is missing |
| `EligibilityCriterion` (Inclusion) | `read_section("inclusion")` | `quote_check(text, section_id)` per criterion |
| `EligibilityCriterion` (Exclusion) | `read_section("exclusion")` | as above |
| `Objective`, `Endpoint` (levels) | `read_section("objectives_endpoints")` | `get_table` for an objectives/endpoints table |
| `Estimand`, `IntercurrentEvent` | `read_section("estimands")`. If the protocol has no estimands section, this falls back to `objectives_endpoints` with a warning. | `find_sections(query="estimand")` |
| `Encounter`, `Activity`, `ScheduledActivityInstance`, `Timing`, visit windows | `get_soa()` | <ul><li>`read_section("soa")` for the footnotes and the text around the table.</li><li>`get_table(id, format="rows")` to check a row.</li></ul> |
| `StudyEpoch`, `StudyArm`, `StudyCell`, cohorts | `read_section("design")` and `read_section("schema")`. The schema figure comes with its OCR text and the AI-generated Mermaid, both labelled. | Confirm arm and epoch names with `quote_check`. The Mermaid is not document text. |
| `StudyDesignPopulation` (planned enrollment, age, sex) | `read_section("population")`, plus `sample_size` for the enrollment number | `search("planned number of participants")` |
| `StudyIntervention`, `Administration` (dose, route, frequency) | `read_section("interventions")` + `get_table("study treatments")` | `read_section("dose_modification")`, `read_section("concomitant_therapy")` |
| `StudyAmendment` (number, date, reasons, changes) | `read_section("amendment_history")` | `document_info().version` |
| `Abbreviation` | `abbreviation(term)` | `read_section("abbreviations")` for the whole list |
| Anything else, or a role that returns nothing | `find_sections(query=...)`, then `read_section(id)` | `search(question)` |

Roles follow ICH M11 and the TransCelerate CPT; `structure.ROLE_INFO` lists them all. `outline()["roles"]` says what the agent finds under each role present in the document.

## Recommended loop

1. Call `document_info()`, then `outline(max_level=2)`.
2. For each USDM class, read the whole section with `read_section(role)`.
   - Enumerate from the Markdown.
   - Lists keep the protocol's own markers (`[1]`, `[1a]`, `a)`, `I 05.`) and their nesting, so identifiers come straight from the text.
3. Follow `cursor` until it is `null`. A part never ends on a bare heading, and a block is never cut.
   - If one block (a big table) is larger than `max_tokens`, it is returned whole with a warning.
4. **Verify every extracted value with `quote_check`** before writing it into USDM. Keep the returned `provenance` (section, pages, refs) as the value's evidence.
   - Quote short, distinctive verbatim spans, not paraphrases.
   - Pass `section_id` when you know where the value must come from. A match elsewhere is listed under `found_elsewhere`, and `found` stays `false`.
   - If `found` is `false`, use `closest` (the most similar passage, with a similarity score) to correct the quote. Never keep an unverified value.
   - A match only in an AI-generated figure description or Mermaid is reported under `found_in_ai_text`. It does not count as found: cite the OCR text or the body instead.
5. Use `search` only for facts no section role covers, then read the section it points to.

## Output details

### `read_section` Markdown

- Headings end with `{#<section_id>}`.
- `<!-- p. N -->` (or `<!-- pp. N-M -->`) marks content that starts on a new page.
- Tables are full GFM tables: caption first, then every row, then `Notes:` with the footnotes.
- A figure shows:
  - the title;
  - `Text in figure (OCR)`;
  - `Description (AI-generated…; verify against the image)` and `Diagram (Mermaid, AI-generated)` when present.
- `blocks` gives, for each block:
  - kind, section_id, pages, ref and character span in `markdown`;
  - `item_refs` for lists;
  - `table_id` / `figure_id` / `image`;
  - `moved_from`, for blocks moved by the read-time reading-order repair (older outputs only).

### Role mode

- `read_section("inclusion")` returns every outermost section with that role, in document order.
- Subsections come with them when they belong to the same role family; for example, inclusion and exclusion stay under eligibility.
- Other subsections are listed under `excluded_subsections` with their ids, so nothing is hidden.

### Reading-order repair

Sometimes the converter reads part of a page before that page's heading, even though the text sits below the heading. On Lilly p. 26, criteria `[1b]`, `[2]` and `[3]` ended up under 5.5.

Post-processing puts such text back under the heading it lies below, in the document itself (`structure.repair_reading_order`, the last post-processing step). The document, the chunks, the parents, the section index and every tool therefore agree. The moves are listed under `reading_order` in the converter's decisions log (`<name>.decisions.json`).

The repair only applies under all of these conditions:
- text and list units only (never tables or figures), plus unnumbered sections, which move as a block with their content (on Lilly p. 9 the "1. Protocol Synopsis" heading was read after the synopsis fields);
- the same page;
- the left half of the page;
- backwards moves only, and only under a heading with an accepted section number;
- the rebuilt section index must agree: a move that would not put the text in the target section is undone.

Outputs converted before this step get the same repair at read time instead (units only). `read_section`, `outline` and the provenance of every tool then use the repaired assignment, and every move is reported:
- in `document_info().reading_order_repairs`;
- as a `read_section` warning.

In that case the chunks keep the converter's order: `search` blocks keep the chunker's assignment, but their provenance uses the repaired one.

### `quote_check` matching

| Tier | What it ignores |
|---|---|
| `exact` | Whitespace only |
| `normalized` | Case, Unicode forms, dashes, quotes, superscript `^`, `>=`/`≥`, thousands separators (`keyterm_match.normalize`) |
| `compact` | All punctuation and spacing; letters, digits and `≥≤<>=%±` only. Used for quotes with at least 12 such characters. |

- Markdown markers (`{#S..}`, `<!-- p. -->`, leading `#`, table pipes) are stripped from the quote.
- `...` or `…` in a quote means elision: every part must occur.
- Table cells are matched row by row: a row is its cells joined with ` | `.
- Headers and footers are searched too; they appear with `layer: "furniture"`.

### `search`

- It is the production path: `retrieve.production_builder`, top `PRODUCTION_K = 8` passages, a 12K-token budget, parent-aware, with the identity block. Its `blocks` are the context blocks an answering model sees; `rendered=True` adds the context string itself.
- Python callers can get the raw `retrieve.Result` and `retrieve.Context` from `doc.search_context(q)`.
- Network use:
  - `search` sends the question and the candidate chunk texts to OpenRouter (qwen3-embedding-8b, qwen3-reranker-8b) with zero-data-retention routing.
  - Both results are cached in `chunks/` (`query_embedding_cache.json`, `rerank_cache.json`).
  - With `OPENROUTER_OFFLINE=1`, a question that is not cached raises `OfflineCacheMiss`, with a message naming the cache file.
  - `ProtocolDocument.open(out, embed_fn=..., rerank_fn=...)` plugs in another client, such as a fake one in tests.
  - Every other tool is local.

## Wiring into an agent

**Anthropic Messages API**

```python
tools = doc.tool_schemas("anthropic")
# in the loop, for each tool_use block:
result = doc.call_tool(block.name, block.input)
messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id,
                 "content": json.dumps(result, ensure_ascii=False), "is_error": "error" in result}]})
```

**OpenAI-style function calling**

```python
tools = doc.tool_schemas("openai")
result = doc.call_tool(call.function.name, json.loads(call.function.arguments))
```

## CLI

```
python protocol_api.py OUT_DIR info
python protocol_api.py OUT_DIR outline --max-level 2
python protocol_api.py OUT_DIR find "exclusion criteria"        (or: find --role soa)
python protocol_api.py OUT_DIR section inclusion --md           (section id, number or role; --max-tokens N, --cursor C)
python protocol_api.py OUT_DIR table "study treatments" --format rows
python protocol_api.py OUT_DIR soa --md
python protocol_api.py OUT_DIR search "What is the washout period?"
python protocol_api.py OUT_DIR quote "have a hemoglobin A1c value ≤10%" --section S6.1
python protocol_api.py OUT_DIR abbr AE
python protocol_api.py OUT_DIR schemas --style openai
```

The main protocol (`output/`) is confidential: run the CLI on it locally only, and do not paste its output anywhere.

## Limits

- **Content is only as complete as the conversion.**
  - Text the converter dropped cannot be returned.
  - Reading-order defects that the conservative repair does not cover can still leave text in the wrong section, for example across pages or in two-column layouts. If a list looks short, `quote_check` the missing item's wording, or read the neighbouring sections.
- **Roles are tagged automatically** (`role_score` < 0.7 means inherited or weak).
  - Some headings get the wrong role. For example, Lilly's 3.1–3.2 inherit `soa` because heading 3 is missing.
  - When a role read returns something unexpected, use `outline` and read by section id.
- **The sponsor name comes from heuristics.**
  - The methods, in order: a cover-page label, a copyright or "property of" line, then a company-name pattern (marked "unverified").
  - Check it with `quote_check`.
- **The SoA structure comes from `soa.py`**, so its visit names, windows and timings are what that module infers.
  - Check them against `read_section("soa")` and the table.
