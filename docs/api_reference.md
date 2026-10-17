# Protocol service API reference

What every REST route, MCP tool, protocol tool and published file of the protocol service returns, field by field,
and what each field means. Written for anyone who connects an application, an agent or a user interface to the
service. It describes pipeline version 2.0.0; `GET /version` shows what a running service has.

- How to build, run and configure the service: [DEPLOY.md](../DEPLOY.md).
- How an agent should use the protocol tools to build USDM data: [protocol_api.md](protocol_api.md).

## 1. Overview

### What to call for what

| You want to | Use | Section |
|---|---|---|
| Upload a PDF and follow its conversion | `POST /documents`, then `GET /jobs/{job_id}` | [REST API](#3-rest-api) |
| Let an LLM agent read a converted protocol | the 9 protocol tools, over REST or MCP | [Protocol tools](#5-protocol-tools) |
| Connect an MCP client such as Claude Code or Claude Desktop | `/mcp` (HTTP) or stdio | [MCP](#4-mcp) |
| Build your own search index, views or exports | the published files | [Published files](#6-published-files) |
| Show a user where a value came from in the PDF | provenance, plus boxes from `protocol.json` | [Pages and boxes](#pages-and-boxes), [Building features](#7-building-features-from-the-outputs) |

### A typical integration

1. **Upload** the PDF with `POST /documents`. A `202` means a conversion job was **queued**, not finished; a `200`
   means the service already has a current result for these bytes. Keep the `doc_id`: it is the SHA-256 of the
   PDF, so the same file always gets the same id.
2. **Poll** `GET /jobs/{job_id}` every 15–30 seconds until the job is `done`, `done_with_warnings`, `failed` or
   `cancelled`. A large protocol takes 10–25 minutes.
3. **Read** the protocol with the tools (`read_section`, `get_soa`, …), passing the `doc_id`, or download the files.
4. **Verify** every value you extract with `quote_check`, and keep the provenance it returns.
5. **Show** the source: page numbers come with every result; exact boxes come from `protocol.json`.

### How to read the field tables

Every result is described by a field table. Each row is one key that can appear in the JSON.

| Path | Means |
|---|---|
| `markdown` | a key at the top level |
| `version.date` | a key inside a nested object |
| `blocks[].pages` | a key of each object in the list `blocks` |
| `roles.<role>.count` | a key inside the values of a map whose keys are data (role names, labels, file names); the angle brackets name what the keys are |
| `[].doc_id` | a key of each object when the result itself is a list |

- **Types:** `string`, `integer`, `number`, `boolean`, `object`, `object (map)`, arrays such as `string[]` and
  `object[]`, and unions such as `string | null`.
- **Shared objects:** an object that appears in many results, such as [provenance](#provenance), is described once.
  Where it appears, its row says `object (shared: provenance)`, and all its fields are present there too.
- **Completeness:** the tables are checked against real outputs by a test, so a field you meet in a response is in
  its table (see [Keeping this reference current](#9-keeping-this-reference-current)). Fields that appear only in
  some cases say when.
- **Examples** come from the public protocols the project is tested with. Long strings are cut with `…`, and long
  lists are shortened with a `/* … */` comment, which is not valid JSON.

## 2. Conventions

### IDs

| ID | Format | Example | Stable across |
|---|---|---|---|
| `doc_id` | 64 lowercase hex characters: the SHA-256 of the PDF's bytes | `dbadb3709bb5c41f…e80e61a040` | Everything. The same bytes always give the same id; any change to the file gives a new one. |
| `job_id` | 32 lowercase hex characters | `4f5bfcd5616a45c8a8200da3633ab41d` | One conversion or reprocess run. |
| Section id | `S<number>` for a numbered section (`S6.1`), `S~<hash>-<n>` for an unnumbered one, `S0` for the front matter | `S6.1` | The result it came from. |
| Item ref | A JSON pointer into `protocol.json`: `#/texts/<i>`, `#/tables/<i>`, `#/pictures/<i>`, `#/groups/<i>` | `#/texts/179` | The result it came from. Refs are positions in the file. |
| Table id | The table's item ref | `#/tables/5` | The result it came from. |
| Chunk id | `<protocol id>-A<amendment>-<nnnn>` when both are detected (`ALXN1840-WD-204-A3-0000`), else `doc-<key>-<nnnn>`, where the key is the first 12 of the last 16 hex digits of the `doc_id` (Docling keeps the low 64 bits of the hash) | `doc-ec329cd80e61-0012` for the `doc_id` ending `…ec329cd80e61a040` | The same PDF and pipeline version, on every platform. Compare `content_hash` across versions. |

A **result** is one published conversion of a document. Reprocessing under a newer pipeline version publishes a new
result, and section ids, item refs and chunk ids may then point to different content. If you store any of them,
store the document's pipeline version and fingerprint next to them (see `GET /documents/{doc_id}`), and refresh
them when those change.

### Provenance

Every protocol tool says where its content comes from with a provenance object:

<!-- fields: shared:provenance -->
| Field | Type | Meaning |
|---|---|---|
| `section_id` | string \| null | Id of the section the content belongs to (see [IDs](#ids)); null when the content is outside every section. Pass it to `read_section` to open the section. |
| `section_number` | string \| null | The section's number as printed, such as `6.1`; null for an unnumbered section. |
| `title` | string \| null | The section heading as printed, including its number, such as `6.1. Inclusion Criteria`. |
| `pages` | integer[] | PDF page numbers the content is on, 1-based, sorted, without duplicates. |
| `refs` | string[] | [Item refs](#ids) of the document items the content comes from, without duplicates, usually in document order (the blocks of a parent unit in `search` keep the unit's own order). Resolve them in `protocol.json` to draw [boxes](#pages-and-boxes). |

Some results add keys next to these five, such as `section_ids` when the content spans several sections; each
tool's table lists them.

### Pages and boxes

- **Page numbers** are 1-based positions of pages in the PDF file (the first page is 1), not the page labels
  printed on the pages. They match what a PDF viewer shows as the page index.
- **Boxes:** the service returns pages and [item refs](#ids), not coordinates. To draw a box around the source:
  1. Download `protocol.json` once per result: `GET /documents/{doc_id}/files/protocol.json`. It is the Docling
     document; see [protocol.json](#protocoljson-docling).
  2. Resolve each ref as a JSON pointer: `#/texts/179` is `doc["texts"][179]`.
  3. Read the item's `prov` list. Each entry has `page_no` and a `bbox` `{l, t, r, b, coord_origin}` in PDF points
     (1/72 inch), with `coord_origin` `BOTTOMLEFT`: `t` is the top edge measured from the bottom of the page.
  4. Read the page size from `doc["pages"]["<page_no>"]["size"]` (`width`, `height`, in points).
  5. Convert to top-left pixel coordinates for a page rendered `W` pixels wide: `s = W / width`;
     `x = l × s`, `y = (height − t) × s`, `w = (r − l) × s`, `h = (t − b) × s`.
- **Details that matter when you implement it:**
  - A group ref (`#/groups/<i>`, a list) has no `prov` of its own: resolve its `children` refs instead.
  - An item can have several `prov` entries when it continues on the next page; draw one box per entry.
  - A table or a figure has one box for the whole object. Table cells in `data.table_cells[]` carry their own
    `bbox`, but with `coord_origin` `TOPLEFT` (`t` measured from the top) and no page number; use them only for a
    table with a single `prov` entry. Some cells have no box (for example cells rebuilt from the PDF's ruling
    lines).
  - To mark the exact words inside an item, search the quote in your PDF renderer's text layer within the item's
    box.
- **The PDF itself is not served back.** Keep your own copy of what you uploaded; its SHA-256 is the `doc_id`, so
  you can check that you have the same bytes.

### Errors

- **REST:** every error has an HTTP status and a JSON body `{"type", "message", "details"?}`. Branch on `type`,
  never on `message`, which is written for people and may change. The status says what kind of problem it is: 4xx
  for your request, 5xx for the service or OpenRouter. The full list of types is in [REST errors](#rest-errors).
- **Host and Origin:** the service answers only requests addressed to an allowed host name (`421 invalid_host`)
  and refuses browser requests from pages of other sites (`403 invalid_origin`). See `ALLOWED_HOSTS` in
  [DEPLOY.md](../DEPLOY.md).
- **Protocol tools:** over REST and MCP, a tool error comes back as one of these REST errors. An error raised by
  the tool itself carries `details.tool` naming it; errors found before the tool runs (`document_not_found`,
  `invalid_id`, `not_ready`, `unknown_tool` with `details.tools`) do not. Python callers of
  `ProtocolDocument.call_tool` get a `{"error", "error_type"}` result instead; see [Tool errors](#tool-errors).
- **MCP:** the same error body comes back as a tool result with `isError: true` (see [MCP envelope](#mcp-envelope)).

### MCP envelope

An MCP tool call returns the same JSON as the matching REST route, wrapped in an MCP tool result:

- `structuredContent` is the JSON object itself.
- `content` holds one text block with the same JSON as a string, for MCP clients without structured content.
- `isError` is `true` when `structuredContent` is an [error body](#errors).

The exact fields are in [MCP results](#mcp-results).

## 3. REST API

The REST API is the service's JSON-over-HTTP interface: upload PDFs, follow their jobs, read documents and their
published files, and run the protocol tools. The interactive page `/docs` and the schema `/openapi.json` list the
same routes; this section adds what every response field means.

**Base URL:** `http://127.0.0.1:8000`.

- Docker Compose publishes port 8000 on 127.0.0.1 only, so the service is reachable from the machine it runs on.
- The demo UI (`/ui/`), the MCP endpoint (`/mcp`) and the API share this origin, so the UI calls the API without
  CORS. `GET /` redirects to `/ui/`.

**Authentication:** none today. Anyone who can reach the port can call every route, including delete. Put an
authenticating proxy in front before the port is reachable from anywhere else
([DEPLOY.md](../DEPLOY.md#10-before-hosting-checklist)).

**Host and Origin checks** apply to every route, `/mcp` and `/ui/` included (see [Errors](#errors)):

- The `Host` header must name an `ALLOWED_HOSTS` entry, with any port. The default is `localhost`, `127.0.0.1` and
  `::1`. Any other host gets `421 invalid_host`.
- A request with an `Origin` header must come from an `http` or `https` page of an allowed host, or it gets
  `403 invalid_origin`. API and MCP clients send no `Origin`.
- `ALLOWED_HOSTS=*` turns both checks off.

**Formats:**

- Bodies are JSON (UTF-8), except the upload (`multipart/form-data`) and file downloads.
- Timestamps are ISO 8601 in UTC, to the second: `2026-10-13T08:21:09+00:00`.
- `doc_id` is 64 lowercase hex characters and `job_id` 32 (see [IDs](#ids)). Any other form, uppercase included,
  is `400 invalid_id`.
- Errors are a JSON body `{"type", "message", "details"?}` with the HTTP status; see [REST errors](#rest-errors).

**Routes:**

| Route | Purpose | Success status |
|---|---|---|
| `GET /health` | Service health | 200, or 503 when unhealthy |
| `GET /version` | Pipeline version, fingerprint, packages, models, configuration | 200 |
| `GET /documents` | List documents | 200 |
| `GET /documents/{doc_id}` | One document, with its current job | 200 |
| `DELETE /documents/{doc_id}` | Delete a document, its jobs and its files | 200 |
| `POST /documents` | Upload a PDF | 202 queued, 200 not queued |
| `POST /documents/{doc_id}/reprocess` | Convert a stored PDF again | 202 queued, 200 not queued |
| `GET /documents/{doc_id}/jobs` | Jobs of one document | 200 |
| `GET /jobs` | Jobs of all documents | 200 |
| `GET /jobs/{job_id}` | One job: state, stage, progress, error | 200 |
| `POST /jobs/{job_id}/cancel` | Cancel a queued job | 200 |
| `GET /documents/{doc_id}/files` | Files and images of the published result | 200 |
| `GET /documents/{doc_id}/files/{name}` | Download one file | 200 |
| `GET /tools` | Protocol tool definitions | 200 |
| `POST /documents/{doc_id}/tools/{tool}` | Run one protocol tool | 200 |

The examples below come from a service running offline (`OPENROUTER_OFFLINE=1`) with the public LZZT and Lilly
protocols, plus made-up one-page PDFs where a job state needed one. For the two public protocols the Docling
conversion was replaced by their stored conversion, so their job timestamps and `convert` stage times are seconds
where a real conversion takes minutes.

### Service

#### GET /health

Whether the service can work: the registry (SQLite) answers and the background worker runs. The Docker healthcheck
calls this route.

Parameters: none.

| Status | Meaning |
|---|---|
| 200 | Healthy: `status` is `ok`. |
| 503 with this body | Unhealthy: `status` is `unavailable`, because the registry cannot be read or the worker has stopped. |
| 503 `not_started` | Error body: the app runs, but its service has not started yet. |

<!-- fields: rest:GET /health -->
| Field | Type | Meaning |
|---|---|---|
| `status` | string | `ok` or `unavailable`. `ok` needs `registry` `ok` and `worker` `running` (or `disabled`). |
| `registry` | string | `ok`, or `error` when the job counts could not be read from the registry. |
| `worker` | string | `running`; `stopped` (its threads have ended); `not_started` (the service exists but was not started); `disabled` (a process without a worker; the REST app always runs one). |
| `jobs` | object (map) \| null | Number of jobs per state, keyed by state. Null when `registry` is `error`. |
| `jobs.<state>` | integer | Jobs in this state. All six states are always present, with 0 when none: `queued`, `running`, `done`, `done_with_warnings`, `failed`, `cancelled`. Jobs of deleted documents are not counted. |
| `offline` | boolean | `true` when `OPENROUTER_OFFLINE` is set: nothing is sent to OpenRouter (see the note below). |

Example:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

```json
{
  "status": "ok",
  "registry": "ok",
  "worker": "running",
  "jobs": {"queued": 0, "running": 0, "done": 1, "done_with_warnings": 0, "failed": 0, "cancelled": 0},
  "offline": true
}
```

Notes:

- For monitoring, watch the 503 and the growth of `jobs.failed`; `GET /jobs?state=failed` lists the failures.
- **Offline mode** (`offline: true`):
  - Uploads still finish. Figure descriptions and embeddings that are not in the caches are skipped: the stages
    `figures` and `embed` then report `skipped_offline`, and the job ends `done_with_warnings`.
  - `search` answers `409 search_unavailable` for a document without embeddings, and `503 offline` for a query
    whose embedding is not cached.
  - Every other route and tool works. To add what is missing, reprocess the document with the service online.

#### GET /version

What the running service makes results with. Compare `pipeline_version` and `fingerprint` with a document's to know
whether its result is current (see [Duplicate and version rules](#duplicate-and-version-rules)).

Parameters: none.

| Status | Meaning |
|---|---|
| 200 | The version information. |

<!-- fields: rest:GET /version -->
| Field | Type | Meaning |
|---|---|---|
| `pipeline_version` | string | Semantic version of the pipeline, for example `2.0.0`. A new major version changes output formats or identifiers. |
| `fingerprint` | string | 16 hex characters: a hash of `fingerprint_inputs`. A stored result with another fingerprint is stale. |
| `fingerprint_inputs` | object \| null | Everything the fingerprint covers. Null when it could not be computed (the service logs why). |
| `fingerprint_inputs.pipeline_version` | string | Same as `pipeline_version`. |
| `fingerprint_inputs.settings` | object (map) | The effective conversion settings, keyed by setting name (about 30 settings of the post-processing steps). |
| `fingerprint_inputs.settings.<setting>` | string \| number \| boolean \| null | One setting's value. |
| `fingerprint_inputs.packages` | object (map) | Versions of the Docling packages, keyed by package name: `docling`, `docling-core`, `docling-parse`, `docling-ibm-models`. |
| `fingerprint_inputs.packages.<package>` | string \| null | Installed version; null when the package is not installed. |
| `fingerprint_inputs.converter` | object (map) | The converter configuration, keyed by name: `backend`, `ocr`, `tables`, `images_scale`, `picture_classification`, `layout`. |
| `fingerprint_inputs.converter.<name>` | string \| number \| boolean | One configuration value. |
| `fingerprint_inputs.models` | object | The model ids that shape results. |
| `fingerprint_inputs.models.figure` | string | Figure-description model (`FIGURE_MODEL`). |
| `fingerprint_inputs.models.embedding` | string | Embedding model (`EMBED_MODEL`). |
| `fingerprint_inputs.models.reranker` | string | Search reranker (`RERANK_MODEL`). |
| `fingerprint_inputs.models.tokenizer` | string | Hugging Face tokenizer that counts chunk tokens. |
| `packages` | object (map) | Versions of the main packages of the running service, keyed by name: `docling`, `docling-core`, `docling-parse`, `fastapi`, `mcp`. |
| `packages.<package>` | string \| null | Installed version; null when not installed. |
| `models` | object | The models the service is configured with. |
| `models.figure` | string | `FIGURE_MODEL`, for example `openrouter:anthropic/claude-opus-5.5`; empty when figure descriptions are turned off. |
| `models.embedding` | string | `EMBED_MODEL`. |
| `models.reranker` | string | `RERANK_MODEL`. |
| `models.tokenizer` | string | The chunk tokenizer. Absent when `fingerprint_inputs` is null. |
| `offline` | boolean | `true` when `OPENROUTER_OFFLINE` is set. |
| `config` | object | The service configuration that is safe to show. It never holds the API key. |
| `config.data_dir` | string | `DATA_DIR`: `/data` in the container. |
| `config.offline` | boolean | Same as `offline`. |
| `config.figure_model` | string | Same as `models.figure`. |
| `config.embed_model` | string | Same as `models.embedding`. |
| `config.rerank_model` | string | Same as `models.reranker`. |
| `config.worker_concurrency` | integer | Documents converted at the same time (`WORKER_CONCURRENCY`). |
| `config.figure_concurrency` | integer | Figure-description requests in flight per document (`FIGURE_CONCURRENCY`). |
| `config.max_upload_mb` | integer | Largest accepted PDF in MiB (`MAX_UPLOAD_MB`). |
| `config.allowed_hosts` | string[] | `ALLOWED_HOSTS`: host names clients may address the service by. |
| `config.api_key_set` | boolean | Whether `OPENROUTER_API_KEY` is set. |

Example (trimmed):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/version | ConvertTo-Json -Depth 5
```

```json
{
  "pipeline_version": "2.0.0",
  "fingerprint": "e5b95040132248ca",
  "fingerprint_inputs": {
    "pipeline_version": "2.0.0",
    "settings": {"heading_levels": "multi_signal", "heading_source": "auto", /* 31 more */},
    "packages": {"docling": "2.131.0", "docling-core": "2.99.0", "docling-parse": "7.22.1", "docling-ibm-models": "4.0.3"},
    "converter": {"backend": "hybrid(docling-parse text, pdfium images, render_pages=False)", /* 5 more */},
    "models": {"figure": "openrouter:anthropic/claude-opus-5.5", "embedding": "qwen/qwen3-embedding-8b",
               "reranker": "qwen/qwen3-reranker-8b", "tokenizer": "Qwen/Qwen3-Embedding-8B"}
  },
  "packages": {"docling": "2.131.0", "docling-core": "2.99.0", "docling-parse": "7.22.1", "fastapi": "0.143.0", "mcp": "2.3.0"},
  "models": {"figure": "openrouter:anthropic/claude-opus-5.5", "embedding": "qwen/qwen3-embedding-8b",
             "reranker": "qwen/qwen3-reranker-8b", "tokenizer": "Qwen/Qwen3-Embedding-8B"},
  "offline": true,
  "config": {
    "data_dir": "/data",
    "offline": true,
    "figure_model": "openrouter:anthropic/claude-opus-5.5",
    "embed_model": "qwen/qwen3-embedding-8b",
    "rerank_model": "qwen/qwen3-reranker-8b",
    "worker_concurrency": 1,
    "figure_concurrency": 4,
    "max_upload_mb": 100,
    "allowed_hosts": ["localhost", "127.0.0.1", "::1"],
    "api_key_set": false
  }
}
```

### Documents

A document is one PDF, identified by the SHA-256 of its bytes. Its record says whether a result is published,
which pipeline made it, its identifiers and its quality summary. The result itself is read through the
[files](#files) and the [tools](#tools).

#### The document object

`GET /documents` returns a list of these objects; `GET /documents/{doc_id}` returns one with more fields.

<!-- fields: shared:svc-document -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | SHA-256 of the PDF's bytes, 64 lowercase hex characters. |
| `filenames` | string[] | Every file name this PDF was uploaded under, oldest first: the base name only, at most 100 names. |
| `size` | integer \| null | Size of the PDF in bytes. |
| `pages` | integer \| null | Number of pages: from the PDF at the first upload, then from the published result. Null when the PDF could not be inspected. |
| `text_fingerprint` | string \| null | SHA-256 of the PDF's text layer, normalised (Unicode NFKC, case-folded, whitespace collapsed). Two PDFs with the same value have the same text. Null for a PDF without a text layer (a scan), or when the PDF could not be inspected. |
| `identifiers` | object (shared: svc-identifiers) | Study identifiers found in the published result; `{}` before the first result. |
| `status` | string | The state of the document's latest job: `queued`, `running`, `done`, `done_with_warnings`, `failed`, `cancelled`; `none` before its first job. A failed or cancelled reprocess leaves the older result served: then `status` is `failed` or `cancelled` while `has_result` is `true`. |
| `pipeline_version` | string \| null | Pipeline version of the published result; `imported` for a result registered with `service.import_existing` (unless it was imported with `--assume-current`); null without a result. |
| `fingerprint` | string \| null | Fingerprint of the published result (see `GET /version`); empty for an `imported` result; null without a result. |
| `has_result` | boolean | `true` when a result is published: files and tools work. |
| `published_at` | string \| null | When the served result was published; null without a result. Changes with every new result. |
| `stale` | boolean | `true` when the served result was made by another pipeline version or fingerprint than the running service's: reprocess it to get a current one. Always `false` without a result. |
| `quality` | object (shared: svc-quality-summary) | Quality summary of the served result; `{}` without a result, or for an imported result without `quality.json`. |
| `error` | object \| null (shared: svc-document-error) | The latest failure. Null when there was none, and cleared when a new result is published. |
| `current_job_id` | string \| null | The document's latest job, in any state. |
| `result_job_id` | string \| null | The job whose result is served; null without a result. |
| `created_at` | string | When the document was first registered. |
| `updated_at` | string | Last change of the record: an upload, a job starting or finishing. Lists are sorted by it. |

<!-- fields: shared:svc-identifiers -->
| Field | Type | Meaning |
|---|---|---|
| `nct` | string \| null | ClinicalTrials.gov number, for example `NCT03421379`. |
| `sponsor_protocol_id` | string \| null | Sponsor protocol number, for example `H2Q-MC-LZZT`. |
| `eudract` | string \| null | EudraCT number. |
| `eu_ct` | string \| null | EU CT number (CTIS). |
| `ind` | string \| null | US IND number. |
| `who_utn` | string \| null | WHO Universal Trial Number. |
| `version` | string \| null | Version label as detected, for example `Amendment 3`. |
| `amendment` | string \| null | Amendment number. |
| `date` | string \| null | Version date, as written in the document. |
| `acronym` | string \| null | Study acronym. |
| `sponsor` | string \| null | Sponsor name, as `document_info.sponsor.name` gives it. |

The identifiers are values only, taken from `document_info` when the result is made or imported (which returns
each with the line it was found in). Pipeline results and imported results have the same keys; a record written
before this key set keeps its older keys until the document is reprocessed or imported again.

<!-- fields: shared:svc-quality-summary -->
| Field | Type | Meaning |
|---|---|---|
| `coverage` | number \| null | Share of the PDF text layer's words found in the converted document, 0 to 1, over the checked pages (table-of-contents pages are skipped). Null when the PDF has no text layer. Below 0.97 the job adds a warning. |
| `pages_below_97` | integer | Checked pages whose coverage is below 0.97. `quality.json` lists them. |
| `tables` | integer | Tables checked by the table QA. |
| `marks_missing` | integer | One-character table marks (such as `X` or a check mark) in the PDF text layer that are missing from the converted tables: a sign of a damaged Schedule of Activities. |
| `figures` | object | Figure counts. |
| `figures.pictures` | integer | Pictures in the document. |
| `figures.skipped_logo_like` | integer | Pictures classified as logos or icons, which are never described. |
| `figures.described` | integer | Pictures with an AI-generated description. |
| `chunks` | integer | Records in `chunks/chunks.jsonl`. |
| `stages` | object (map) | Status of each pipeline stage, keyed by stage name. |
| `stages.<stage>` | string | `done`, `skipped`, `skipped_offline`, `partial` or `failed` (see [the stage object](#the-job-object)). Stage names: `hash`, `convert`, `postprocess`, `figures`, `save`, `chunk`, `embed`, `quality`. |

<!-- fields: shared:svc-document-error -->
| Field | Type | Meaning |
|---|---|---|
| `type` | string | Exception type of the failed job, as in the job's `error.type`. |
| `where` | string \| null | Project file and line where it failed, as in the job's `error.where`. |
| `job_id` | string | The job that failed. |

#### GET /documents

The documents of the service, most recently updated first.

| Parameter | In | Type | Default | Meaning |
|---|---|---|---|---|
| `status` | query | string | (all) | Only documents in this status: `none`, `queued`, `running`, `done`, `done_with_warnings`, `failed`, `cancelled`. |
| `limit` | query | integer | 100 | Page size, clamped to 0–1000. |
| `offset` | query | integer | 0 | Documents to skip. |

| Status | Meaning |
|---|---|
| 200 | The page of documents (possibly empty). |
| 400 `invalid_status` | `status` is not one of the values above. |
| 422 `validation_error` | `limit` or `offset` is not an integer. |

<!-- fields: rest:GET /documents -->
| Field | Type | Meaning |
|---|---|---|
| `documents` | object[] (shared: svc-document) | The documents, sorted by `updated_at` (newest first), then `doc_id`. |
| `limit` | integer | The page size used, after clamping. |
| `offset` | integer | The offset used. |

Example (one imported, stale result):

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/documents?status=done&limit=20"
```

```json
{
  "documents": [
    {
      "doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040",
      "filenames": ["protocol.pdf"],
      "size": 240338,
      "pages": 97,
      "text_fingerprint": "29a3f75a45a7db93329936c1cb17128edf2b646506f7b9cae64beaf25246890d",
      "identifiers": {"nct": null, "sponsor_protocol_id": "H2Q-MC-LZZT", "version": null, "date": null,
                      "acronym": null, "eudract": null, "eu_ct": null, "ind": null, "who_utn": null},
      "status": "done",
      "pipeline_version": "imported",
      "fingerprint": "",
      "has_result": true,
      "published_at": "2026-10-13T08:20:56+00:00",
      "stale": true,
      "quality": {},
      "error": null,
      "current_job_id": "0e07672994924993b65e148a8bc1d27c",
      "result_job_id": "0e07672994924993b65e148a8bc1d27c",
      "created_at": "2026-10-13T08:20:56+00:00",
      "updated_at": "2026-10-13T08:20:56+00:00"
    }
  ],
  "limit": 20,
  "offset": 0
}
```

Notes:

- `updated_at` changes whenever a job of a document starts or ends, so pages can shift while jobs run. Read
  everything with one large `limit` (up to 1000) when that matters.
- The list has no related documents, current job, warnings or files: `GET /documents/{doc_id}` adds them.

#### GET /documents/{doc_id}

One document with its current job, its related documents and the files of its result. This is the route to poll
when you track a document rather than one job.

| Parameter | In | Type | Meaning |
|---|---|---|---|
| `doc_id` | path | string | The document id. |

| Status | Meaning |
|---|---|
| 200 | The document. |
| 400 `invalid_id` | `doc_id` is not 64 lowercase hex characters. |
| 404 `document_not_found` | No such document (never uploaded, or deleted). |

<!-- fields: rest:GET /documents/{doc_id} -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | As in [the document object](#the-document-object), like every field down to `updated_at`. |
| `filenames` | string[] | File names the PDF was uploaded under. |
| `size` | integer \| null | Size in bytes. |
| `pages` | integer \| null | Number of pages. |
| `text_fingerprint` | string \| null | Hash of the normalised text layer. |
| `identifiers` | object (shared: svc-identifiers) | Study identifiers of the served result. |
| `status` | string | State of the latest job, or `none`. |
| `pipeline_version` | string \| null | Pipeline version of the served result. |
| `fingerprint` | string \| null | Fingerprint of the served result. |
| `has_result` | boolean | Whether a result is published. |
| `published_at` | string \| null | When the served result was published. |
| `stale` | boolean | Whether the served result was made by another pipeline version or fingerprint. |
| `quality` | object (shared: svc-quality-summary) | Quality summary of the served result. |
| `error` | object \| null (shared: svc-document-error) | The latest failure. |
| `current_job_id` | string \| null | The latest job. |
| `result_job_id` | string \| null | The job whose result is served. |
| `created_at` | string | First registration. |
| `updated_at` | string | Last change. |
| `related` | object[] | Documents with the same NCT number or sponsor protocol id but a different text: likely another version or amendment of the protocol. Empty when none. |
| `related[].doc_id` | string | The related document. |
| `related[].reasons` | string[] | `same nct` and/or `same sponsor_protocol_id`. |
| `same_text_as` | string[] | `doc_id`s of other documents with the same `text_fingerprint`: the same text in different bytes (for example a re-saved PDF). They are never merged. |
| `current_job` | object \| null (shared: svc-job) | The latest job (see [the job object](#the-job-object)); null before the first job. |
| `warnings` | string[] | Warnings of the served result (those of the job in `result_job_id`); empty without a result. |
| `files` | string[] | Names of the result's files that the files route serves, sorted; images are not listed here (see `GET /documents/{doc_id}/files`). Empty without a result. |

Example (LZZT after a reprocess, trimmed):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/documents/dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040 | ConvertTo-Json -Depth 6
```

```json
{
  "doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040",
  "filenames": ["protocol.pdf"],
  "size": 240338,
  "pages": 97,
  "text_fingerprint": "29a3f75a45a7db93329936c1cb17128edf2b646506f7b9cae64beaf25246890d",
  "identifiers": {"nct": null, "sponsor_protocol_id": "H2Q-MC-LZZT", "eudract": null, "eu_ct": null, "ind": null,
                  "who_utn": null, "version": null, "amendment": null, "date": null},
  "status": "done",
  "pipeline_version": "2.0.0",
  "fingerprint": "e5b95040132248ca",
  "has_result": true,
  "published_at": "2026-10-13T08:21:09+00:00",
  "stale": false,
  "quality": {
    "coverage": 0.9967, "pages_below_97": 2, "tables": 22, "marks_missing": 0,
    "figures": {"pictures": 11, "skipped_logo_like": 0, "described": 0},
    "chunks": 180,
    "stages": {"hash": "done", "convert": "done", "postprocess": "done", "figures": "done", "save": "done",
               "chunk": "done", "embed": "done", "quality": "done"}
  },
  "error": null,
  "current_job_id": "7b316d956a2d4d04aa7e2291888ae6b9",
  "result_job_id": "7b316d956a2d4d04aa7e2291888ae6b9",
  "created_at": "2026-10-13T08:20:56+00:00",
  "updated_at": "2026-10-13T08:21:09+00:00",
  "related": [],
  "same_text_as": [],
  "current_job": {"job_id": "7b316d956a2d4d04aa7e2291888ae6b9", "state": "done", "kind": "reprocess", /* … */},
  "warnings": [],
  "files": ["chunks/chunks.jsonl", "chunks/glossary.json", "chunks/parents.jsonl", /* 8 more */]
}
```

A related document (made-up PDFs with the same made-up NCT number):

```json
{"related": [{"doc_id": "49f24a7bf89d28969e25e7eff6fa7b5ae84b9edc0e6582c08d8842ad385ba1ae", "reasons": ["same nct"]}]}
```

Notes:

- While a job runs, `status` is `running`, `current_job` shows its `stage` and `progress`, and a result published
  earlier stays served (`has_result`, `files`, `result_job_id` describe the old result until the new one is
  published).
- If you store section ids, item refs or chunk ids, store `pipeline_version` and `fingerprint` (or `published_at`)
  with them, and refresh them when these change.

#### DELETE /documents/{doc_id}

Deletes the record, all jobs (a queued job never starts), the published files and the job work folders. The shared
caches keep the PDF's conversion batches and figure descriptions, so a later upload of the same PDF is fast and
sends nothing to OpenRouter, unless you pass `purge_caches=true`.

| Parameter | In | Type | Default | Meaning |
|---|---|---|---|---|
| `doc_id` | path | string | | The document id. |
| `purge_caches` | query | boolean | false | Also remove this PDF's conversion batches and figure descriptions from the shared caches, so that no trace of it stays under `DATA_DIR`. Other documents' cache entries stay. |

| Status | Meaning |
|---|---|
| 200 | Deleted. |
| 400 `invalid_id` | Malformed `doc_id`. |
| 404 `document_not_found` | No such document. |
| 409 `document_busy` | A job of this document is running; try again when it has finished. Queued jobs do not block a delete. |
| 500 `delete_failed` | The files could not be deleted (in use); try again. |

<!-- fields: rest:DELETE /documents/{doc_id} -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | The deleted document. |
| `deleted` | boolean | Always `true`. |
| `purged` | object | Only with `purge_caches=true`: what was removed from the shared caches. |
| `purged.batches` | integer | Cached conversion batch files removed. |
| `purged.figures` | integer | Cached figure descriptions removed. 0 when the result had no readable `protocol.json`. |

Example:

```powershell
Invoke-RestMethod -Method Delete "http://127.0.0.1:8000/documents/a94c35b9e940b3cd1b7dc326c11ab004f88f8cea4e0df2fa20d4b28b4be16eba?purge_caches=true"
```

```json
{"doc_id": "a94c35b9e940b3cd1b7dc326c11ab004f88f8cea4e0df2fa20d4b28b4be16eba", "deleted": true, "purged": {"batches": 0, "figures": 0}}
```

Without `purge_caches` the answer is `{"doc_id": "…", "deleted": true}`. Deletion cannot be undone; the jobs of the
document are gone too (`GET /jobs/{job_id}` then answers `404 job_not_found`).

### Uploads

#### POST /documents

Uploads a PDF and decides what to do with it: queue a conversion job, or return the job or result that already
exists for the same bytes. See [Duplicate and version rules](#duplicate-and-version-rules).

| Field | In | Type | Default | Meaning |
|---|---|---|---|---|
| `file` | multipart form | file | (required) | The PDF. Its file name is recorded in `filenames`. |
| `reprocess` | multipart form | boolean | false | Reprocess a stale result (one made by another pipeline version or fingerprint). Without it a stale result is only reported. |
| `password` | multipart form | string | | Password of an encrypted PDF. Form field only: a `password` in the URL is refused. |

| Status | Meaning |
|---|---|
| 202 | A job was **queued**: the PDF is not converted yet. Poll `GET /jobs/{job_id}`. |
| 200 | Nothing was queued: the job already queued or running for these bytes, or an existing result (current, or stale without `reprocess`). |
| 400 `empty` | The file has no bytes. |
| 400 `invalid_pdf` | The file starts like a PDF but cannot be opened. |
| 400 `password_required` | The PDF is encrypted and no password was given. |
| 400 `bad_password` | The password is wrong. |
| 400 `password_in_query` | A `password` parameter in the URL; refused before the body is read. |
| 400 `bad_request` | The multipart body cannot be parsed. |
| 413 `too_large` | The request body or the PDF is larger than `MAX_UPLOAD_MB` allows. |
| 415 `not_pdf` | No `%PDF-` header in the first 1024 bytes. |
| 422 `validation_error` | No `file` part, or `reprocess` is not a boolean. |

<!-- fields: rest:POST /documents -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | SHA-256 of the uploaded bytes. Keep it: every other route takes it. |
| `job_id` | string \| null | The job that produces the result: the job this request queued, or the job already queued or running for these bytes. Null when nothing is queued (an existing result is returned). |
| `status` | string | `queued` when this request queued a job; otherwise the document's current status (`queued`, `running`, `done`, `done_with_warnings`, `failed`, `cancelled`). |
| `duplicate` | boolean | `true` when these bytes were already known with a result or an active job, and that job or result is returned. Also `true` when a reprocess was queued for a document that has a result. `false` for a new PDF, and for a known PDF without a result (its earlier job failed or was cancelled). |
| `stale` | boolean | `true` when the served result was made by another pipeline version or fingerprint. With `queued: false` the stale result stays as it is; upload again with `reprocess=true` or call `POST /documents/{doc_id}/reprocess`. |
| `queued` | boolean | `true` when this request queued a new job (HTTP 202). |
| `warnings` | string[] | `same text as <doc_id>` for every other document whose text fingerprint is the same. Never merged. |

Examples:

```powershell
curl.exe -F "file=@C:\path\to\protocol.pdf" http://127.0.0.1:8000/documents
curl.exe -F "file=@C:\path\to\protocol.pdf" -F "reprocess=true" http://127.0.0.1:8000/documents
curl.exe -F "file=@C:\path\to\encrypted.pdf" -F "password=the-pdf-password" http://127.0.0.1:8000/documents
```

A new PDF (202):

```json
{"doc_id": "a94c35b9e940b3cd1b7dc326c11ab004f88f8cea4e0df2fa20d4b28b4be16eba", "job_id": "3b93754e6ea04d8aafd99819b8e98870",
 "status": "queued", "duplicate": false, "stale": false, "queued": true, "warnings": []}
```

The same bytes while a stale result exists (200, nothing queued):

```json
{"doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040", "job_id": null,
 "status": "done", "duplicate": true, "stale": true, "queued": false, "warnings": []}
```

Notes:

- **202 means queued, not done.** Poll `GET /jobs/{job_id}` every 15 to 30 seconds until its `state` is final. A
  conversion takes about 7.5 to 11 seconds per page.
- **Idempotent by content:** `doc_id` is the SHA-256 of the bytes. Repeating an upload, for example after a timeout,
  never starts a second job for a queued or running PDF and never converts a current result again.
- **Size:** at most `MAX_UPLOAD_MB` (default 100 MiB). A request whose `Content-Length` is larger than that plus
  64 KiB of multipart overhead is refused before it is read; without `Content-Length` the service stops reading at
  that limit.
- **Passwords** are held in the service's memory until the job starts: never stored, logged or echoed. After a
  restart before the job starts the password is lost, the job fails, and the PDF must be uploaded again.
- **Data handling:** unless the service is offline, the conversion sends the PDF's figure images and chunk texts to
  OpenRouter (zero data retention); see [DEPLOY.md](../DEPLOY.md#6-data-handling).

#### POST /documents/{doc_id}/reprocess

Converts a stored PDF again from its stored copy, without uploading it. By default only a stale result is
reprocessed; `force` reprocesses a current one too. The old result stays served until the new one is published.

| Parameter | In | Type | Default | Meaning |
|---|---|---|---|---|
| `doc_id` | path | string | | The document id. |
| `force` | query | boolean | false | Reprocess even when the result is current. |
| `force` | JSON body | boolean | false | The same, as `{"force": true}`. |
| `password` | JSON body | string | none | Password of an encrypted source PDF, as `{"password": "…"}`: held in memory until the job starts, never stored, logged or echoed; dropped when nothing is queued. Never in the URL. The body may hold no keys other than `force` and `password`. |

| Status | Meaning |
|---|---|
| 202 | A reprocess job was **queued**. |
| 200 | Nothing was queued: the result is current (pass `force`), or a job is already queued or running for the document (its `job_id` is returned). |
| 400 `invalid_id` | Malformed `doc_id`. |
| 400 `password_in_query` | A `password` parameter in the URL. |
| 404 `document_not_found` | No such document, or no stored source PDF. |
| 422 `validation_error` | A body with other keys, or `force` / `password` of the wrong type (the values sent are never echoed). |

<!-- fields: rest:POST /documents/{doc_id}/reprocess -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | The document. |
| `job_id` | string \| null | The queued reprocess job, or the job already queued or running; null when nothing is queued. |
| `status` | string | `queued` when a job was queued; otherwise the document's current status. |
| `duplicate` | boolean | `true` whenever the document has a result or an active job, so in practice always `true` here. |
| `stale` | boolean | Whether the served result was made by another pipeline version or fingerprint. |
| `queued` | boolean | `true` when this request queued a job (HTTP 202). |
| `warnings` | string[] | `same text as <doc_id>` warnings, as for an upload. |

Examples:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/documents/dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040/reprocess
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/documents/dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040/reprocess?force=true"
```

A stale result, queued (202):

```json
{"doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040", "job_id": "7b316d956a2d4d04aa7e2291888ae6b9",
 "status": "queued", "duplicate": true, "stale": true, "queued": true, "warnings": []}
```

A current result without `force` (200):

```json
{"doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040", "job_id": null,
 "status": "done", "duplicate": true, "stale": false, "queued": false, "warnings": []}
```

Notes:

- The job's caches (chunk embeddings, query embeddings, reranks) are copied from the old result, so a reprocess
  sends only what changed to OpenRouter.
- This route takes no password. A reprocess of an encrypted PDF therefore fails when the job opens the PDF. For a
  stale result, upload the PDF again with `reprocess=true` and its `password` instead.

#### Duplicate and version rules

What an upload (and a reprocess request) answers, by the state of the same bytes. The same rules are in
[DEPLOY.md](../DEPLOY.md#7-duplicate-and-version-rules).

| Situation | Answer | `job_id` | `duplicate` | `stale` | `queued` |
|---|---|---|---|---|---|
| A job for these bytes is queued or running | 200 | that job | true | as the served result | false |
| A result made by the current pipeline version and fingerprint | 200 | null | true | false | false |
| A stale result, upload without `reprocess` | 200 | null | true | true | false |
| A stale result, upload with `reprocess=true`, or `POST /reprocess` | 202 | new job | true | true | true |
| A current result, `POST /reprocess?force=true` | 202 | new job | true | false | true |
| No result: a new PDF, or one whose jobs failed or were cancelled | 202 | new job | false | false | true |

- A result is **stale** when its pipeline version or fingerprint differs from `GET /version`'s. The fingerprint
  covers the settings, the Docling versions, the converter configuration and the model ids, so changing
  `FIGURE_MODEL`, `EMBED_MODEL` or `RERANK_MODEL` makes existing results stale. An imported result is always stale.
- Another document with the same text fingerprint adds the warning `same text as <doc_id>`.
- Another document with the same NCT number or sponsor protocol id but a different text is listed in `related`.

### Jobs

A job is one conversion run of one document: `process` (the first conversion), `reprocess`, or `import` (a result
registered by `service.import_existing`). One worker runs jobs first in, first out; `WORKER_CONCURRENCY` jobs run at
the same time.

#### The job object

<!-- fields: shared:svc-job -->
| Field | Type | Meaning |
|---|---|---|
| `job_id` | string | 32 lowercase hex characters. |
| `doc_id` | string | The document the job converts. |
| `state` | string | `queued`, `running`, `done`, `done_with_warnings`, `failed` or `cancelled`. The last four are final. `done_with_warnings` has a usable result with `warnings`. |
| `kind` | string | `process` (first conversion), `reprocess` (a document that has a result, converted again), or `import`. |
| `reprocess` | boolean | `true` when `kind` is `reprocess`. |
| `filename` | string \| null | File name of the upload that created the job; for `POST /reprocess`, the document's first file name. |
| `stage` | string \| null | The last stage the job reported: `hash`, `convert`, `postprocess`, `figures`, `save`, `chunk`, `embed`, `quality`. Null before the first report (queued, just started, imports, cancelled). For a failed job: the stage it failed in. |
| `progress` | object | Progress inside `stage`. After the job finished: the last stage's counts (`quality` reports none, so a finished job shows `{"done": null, "total": null}`). |
| `progress.done` | integer \| null | Units done in this stage (for example conversion batches or embedding requests); null when the stage reports no counts. |
| `progress.total` | integer \| null | Units in this stage; null when unknown. |
| `attempts` | integer | How often the job was started: 0 until it first starts (so 0 for a job cancelled while queued); more than 1 when the service restarted during the job. A job started 3 times without finishing fails with error type `Interrupted`. |
| `created_at` | string | When the job was queued. |
| `started_at` | string \| null | When it last started; null while queued. |
| `finished_at` | string \| null | When it reached a final state, cancelled included; null before. |
| `error` | object \| null | Why the job failed; null unless `state` is `failed`. |
| `error.type` | string | The exception type, such as `FileNotFoundError`, or a type set by the service: `Interrupted` (started 3 times), `SourceMissing` (the stored PDF is gone), `ImportInterrupted`, `ImportFailed`, `ChildExit<code>` (the conversion process exited with that code). Never an exception message: messages can quote document text. |
| `error.where` | string \| null | `<file>:<line>` of the innermost frame in the project's own code, such as `chunk_protocol.py:1257`; null when unknown. The full traceback stays in the volume (`work/<job_id>/logs/error.log`). |
| `warnings` | string[] | `same text as <doc_id>` warnings from the upload, then the pipeline's warnings at the end, for example `embeddings not computed: OPENROUTER_OFFLINE is set (search is unavailable until the document is reprocessed online)`. |
| `stages` | object[] (shared: svc-stage) | One entry per pipeline stage, in order. Filled when the job finished with a result (`done`, `done_with_warnings`). A failed job lists the stages that completed, then the stage it failed in with status `failed` (empty when it failed before its first stage, or its conversion process ended without reporting). Empty while queued or running, for cancelled jobs, and for imports. |

<!-- fields: shared:svc-stage -->
| Field | Type | Meaning |
|---|---|---|
| `name` | string | `hash`, `convert`, `postprocess`, `figures`, `save`, `chunk`, `embed` or `quality`. |
| `status` | string | `done`; `skipped` (figures with `FIGURE_MODEL` empty); `skipped_offline` (figures or embeddings not made because the service is offline); `partial` (some figures could not be described); `failed` (embeddings failed: search is unavailable for the document; or, in a failed job, the stage the job failed in). |
| `seconds` | number | Wall-clock time of the stage, rounded to 0.1 s. |
| `detail` | object (map) | Counts of the stage, keyed by name; `{}` when it has none. Never document text. |
| `detail.<count>` | integer \| number \| string | For example `convert`: `pages`; `save`: `tables`, `pictures`; `chunk`: `chunks`, `parents`; `embed`: `chunks`, `cached`, `embedded` (or `error`, the exception type, when it failed); `quality`: `coverage`, `pages_below_97`; `postprocess`: the number of changes per repair; `figures`: counts such as `offline` and `failed`, or `reason`. |

#### GET /documents/{doc_id}/jobs

The jobs of one document, newest first.

| Parameter | In | Type | Default | Meaning |
|---|---|---|---|---|
| `doc_id` | path | string | | The document id. |
| `state` | query | string | (all) | Only jobs in this state. |
| `limit` | query | integer | 100 | At most this many jobs, clamped to 0–1000. |

| Status | Meaning |
|---|---|
| 200 | The jobs. |
| 400 `invalid_id` | Malformed `doc_id`. |
| 400 `invalid_state` | `state` is not a job state. |
| 404 `document_not_found` | No such document (never uploaded, or deleted). |

<!-- fields: rest:GET /documents/{doc_id}/jobs -->
| Field | Type | Meaning |
|---|---|---|
| `jobs` | object[] (shared: svc-job) | The jobs, newest first. |
| `limit` | integer | The limit used, after clamping. |

Example (trimmed):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/documents/dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040/jobs
```

```json
{
  "jobs": [
    {"job_id": "7b316d956a2d4d04aa7e2291888ae6b9", "doc_id": "dbadb370…a040", "state": "done", "kind": "reprocess",
     "reprocess": true, "filename": "protocol.pdf", "stage": "quality", "progress": {"done": null, "total": null},
     "attempts": 1, "created_at": "2026-10-13T08:20:58+00:00", "started_at": "2026-10-13T08:20:58+00:00",
     "finished_at": "2026-10-13T08:21:09+00:00", "error": null, "warnings": [], "stages": [/* 8 stages */]},
    {"job_id": "0e07672994924993b65e148a8bc1d27c", "doc_id": "dbadb370…a040", "state": "done", "kind": "import",
     "reprocess": false, "filename": "protocol.pdf", "stage": null, "progress": {"done": null, "total": null},
     "attempts": 1, "created_at": "2026-10-13T08:20:56+00:00", "started_at": "2026-10-13T08:20:56+00:00",
     "finished_at": "2026-10-13T08:20:56+00:00", "error": null, "warnings": [], "stages": []}
  ],
  "limit": 100
}
```

#### GET /jobs

The jobs of all documents, newest first.

| Parameter | In | Type | Default | Meaning |
|---|---|---|---|---|
| `state` | query | string | (all) | Only jobs in this state: `queued`, `running`, `done`, `done_with_warnings`, `failed`, `cancelled`. |
| `limit` | query | integer | 100 | At most this many jobs, clamped to 0–1000. |

| Status | Meaning |
|---|---|
| 200 | The jobs. |
| 400 `invalid_state` | `state` is not a job state. |

<!-- fields: rest:GET /jobs -->
| Field | Type | Meaning |
|---|---|---|
| `jobs` | object[] (shared: svc-job) | The jobs, newest first. Jobs of deleted documents are gone. |
| `limit` | integer | The limit used, after clamping. |

Example:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/jobs?state=failed&limit=5"
```

The answer has the same shape as `GET /documents/{doc_id}/jobs`.

#### GET /jobs/{job_id}

One job. This is the route to poll after an upload.

| Parameter | In | Type | Meaning |
|---|---|---|---|
| `job_id` | path | string | The job id from the upload or reprocess answer. |

| Status | Meaning |
|---|---|
| 200 | The job. |
| 400 `invalid_id` | `job_id` is not 32 lowercase hex characters. |
| 404 `job_not_found` | No such job (or its document was deleted). |

<!-- fields: rest:GET /jobs/{job_id} -->
| Field | Type | Meaning |
|---|---|---|
| `job_id` | string | As in [the job object](#the-job-object), like every field below. |
| `doc_id` | string | The document. |
| `state` | string | `queued`, `running`, `done`, `done_with_warnings`, `failed`, `cancelled`. |
| `kind` | string | `process`, `reprocess` or `import`. |
| `reprocess` | boolean | `true` for a reprocess. |
| `filename` | string \| null | File name of the upload. |
| `stage` | string \| null | The last stage reported. |
| `progress` | object | Progress inside `stage`. |
| `progress.done` | integer \| null | Units done. |
| `progress.total` | integer \| null | Units in the stage. |
| `attempts` | integer | Starts so far. |
| `created_at` | string | Queued at. |
| `started_at` | string \| null | Last started at. |
| `finished_at` | string \| null | Final state reached at. |
| `error` | object \| null | The failure, for a failed job. |
| `error.type` | string | Exception type. |
| `error.where` | string \| null | Project file and line. |
| `warnings` | string[] | Warnings. |
| `stages` | object[] (shared: svc-stage) | Per-stage status, time and counts, once the job finished with a result; for a failed job, the completed stages and the failed one. |

Example: the public Lilly protocol converted offline (trimmed):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/jobs/3b93754e6ea04d8aafd99819b8e98870 | ConvertTo-Json -Depth 5
```

```json
{
  "job_id": "3b93754e6ea04d8aafd99819b8e98870",
  "doc_id": "a94c35b9e940b3cd1b7dc326c11ab004f88f8cea4e0df2fa20d4b28b4be16eba",
  "state": "done_with_warnings",
  "kind": "process",
  "reprocess": false,
  "filename": "protocol.pdf",
  "stage": "quality",
  "progress": {"done": null, "total": null},
  "attempts": 1,
  "created_at": "2026-10-13T08:21:09+00:00",
  "started_at": "2026-10-13T08:21:09+00:00",
  "finished_at": "2026-10-13T08:21:13+00:00",
  "error": null,
  "warnings": ["embeddings not computed: OPENROUTER_OFFLINE is set (search is unavailable until the document is reprocessed online)"],
  "stages": [
    /* hash, convert, postprocess, figures, save */
    {"name": "chunk", "status": "done", "seconds": 2.5, "detail": {"chunks": 153, "parents": 39}},
    {"name": "embed", "status": "skipped_offline", "seconds": 0.0, "detail": {}},
    {"name": "quality", "status": "done", "seconds": 1.4, "detail": {"coverage": 0.9974, "pages_below_97": 4}}
  ]
}
```

A running job, and a failed one (made-up PDFs):

```json
{"job_id": "adc04379044745c48f9e6cf758124dff", "state": "running", "stage": "convert", "progress": {"done": 2, "total": 7},
 "attempts": 1, "started_at": "2026-10-13T08:21:13+00:00", "finished_at": null, "error": null, "stages": [], /* … */}
```

```json
{"job_id": "04b4fea2c8c649eaadbeff5f40ea2791", "state": "failed", "stage": "chunk", "progress": {"done": null, "total": null},
 "error": {"type": "FileNotFoundError", "where": "chunk_protocol.py:1257"}, "warnings": [], "stages": [], /* … */}
```

Notes:

- **Polling:** every 15 to 30 seconds is enough; a large protocol takes 10 to 25 minutes. Stop at a final state:
  `done` or `done_with_warnings` (read the document), `failed` (see `error`; upload again to retry), `cancelled`.
- `progress` is updated as the job advances, not continuously; a stage without counts shows nulls.
- After a restart of the service a running job is queued again and keeps its place; `attempts` grows.

#### POST /jobs/{job_id}/cancel

Cancels a queued job: it will never start. A running job cannot be interrupted.

| Parameter | In | Type | Meaning |
|---|---|---|---|
| `job_id` | path | string | The job id. |

| Status | Meaning |
|---|---|
| 200 | Cancelled; the body is the job with `state` `cancelled`. |
| 400 `invalid_id` | Malformed `job_id`. |
| 404 `job_not_found` | No such job. |
| 409 `not_cancellable` | The job is not queued (running, or already final). |

<!-- fields: rest:POST /jobs/{job_id}/cancel -->
| Field | Type | Meaning |
|---|---|---|
| `job_id` | string | As in [the job object](#the-job-object), like every field below. |
| `doc_id` | string | The document. |
| `state` | string | `cancelled`. |
| `kind` | string | `process` or `reprocess`. |
| `reprocess` | boolean | `true` for a reprocess. |
| `filename` | string \| null | File name of the upload. |
| `stage` | string \| null | Null: the job never started. |
| `progress` | object | Progress; both values null. |
| `progress.done` | integer \| null | Null. |
| `progress.total` | integer \| null | Null. |
| `attempts` | integer | 0. |
| `created_at` | string | Queued at. |
| `started_at` | string \| null | Null. |
| `finished_at` | string \| null | When it was cancelled. |
| `error` | object \| null | Null. |
| `warnings` | string[] | Warnings recorded when it was queued. |
| `stages` | object[] (shared: svc-stage) | Empty. |

Example:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/jobs/b75cc83fddf6435e987bb16df3fc3821/cancel
```

```json
{"job_id": "b75cc83fddf6435e987bb16df3fc3821", "doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040",
 "state": "cancelled", "kind": "reprocess", "reprocess": true, "filename": "protocol.pdf", "stage": null,
 "progress": {"done": null, "total": null}, "attempts": 0, "created_at": "2026-10-13T08:21:13+00:00",
 "started_at": null, "finished_at": "2026-10-13T08:21:14+00:00", "error": null, "warnings": [], "stages": []}
```

Cancelling a reprocess keeps the old result served; the document's `status` becomes `cancelled`. Uploading a
document whose only job was cancelled queues a new job.

### Files

#### GET /documents/{doc_id}/files

What the files route serves for this document's published result: file names, images and the short names it
accepts. See [Published files](#6-published-files) for each file.

| Parameter | In | Type | Meaning |
|---|---|---|---|
| `doc_id` | path | string | The document id. |

| Status | Meaning |
|---|---|
| 200 | The listing. |
| 400 `invalid_id` | Malformed `doc_id`. |
| 404 `document_not_found` | No such document. |
| 409 `not_ready` | The document has no result yet. |

<!-- fields: rest:GET /documents/{doc_id}/files -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | The document. |
| `files` | string[] | Served files present in this result, sorted: `protocol.md`, `protocol.json`, `protocol.html`, `protocol.decisions.json`, `quality.json`, `chunks/chunks.jsonl`, `chunks/parents.jsonl`, `chunks/sections.json`, `chunks/tables.jsonl`, `chunks/glossary.json`, `chunks/stats.json`. |
| `images` | string[] | Image files, as `<folder>/<file>`: `images/…` for results converted by the service, `<stem>_images/…` (for example `protocol_images/…`) for imported results. These are the paths that `protocol.json` and the chunk records refer to. |
| `aliases` | object (map) | The short names of the files this result has, keyed by short name (an imported result without `quality.json` has no `quality`). The files route accepts these short names. |
| `aliases.<short name>` | string | The file the short name stands for, such as `chunks.jsonl` → `chunks/chunks.jsonl` or `md` → `protocol.md`. |

Example (an imported result, trimmed):

```powershell
Invoke-RestMethod http://127.0.0.1:8000/documents/dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040/files | ConvertTo-Json
```

```json
{
  "doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040",
  "files": ["chunks/chunks.jsonl", "chunks/glossary.json", "chunks/parents.jsonl", "chunks/sections.json",
            "chunks/stats.json", "chunks/tables.jsonl", "protocol.decisions.json", "protocol.html", "protocol.json",
            "protocol.md"],
  "images": ["protocol_images/image_000000_0855bad9…7114.png", /* 10 more */],
  "aliases": {
    "chunks.jsonl": "chunks/chunks.jsonl", "decisions": "protocol.decisions.json",
    "decisions.json": "protocol.decisions.json", "glossary.json": "chunks/glossary.json", "html": "protocol.html",
    "json": "protocol.json", "markdown": "protocol.md", "md": "protocol.md", "parents.jsonl": "chunks/parents.jsonl",
    "sections.json": "chunks/sections.json", "stats.json": "chunks/stats.json", "tables.jsonl": "chunks/tables.jsonl"
  }
}
```

#### GET /documents/{doc_id}/files/{name}

Downloads one file of the published result. The body is the file itself, not JSON.

| Parameter | In | Type | Meaning |
|---|---|---|---|
| `doc_id` | path | string | The document id. |
| `name` | path | string | A served file name (`protocol.md`, `chunks/chunks.jsonl`, …), a short name from `aliases` (`md`, `chunks.jsonl`, …) or an image path from `images`. |

| Status | Meaning |
|---|---|
| 200 | The file. |
| 400 `invalid_id` | Malformed `doc_id`. |
| 404 `document_not_found` | No such document. |
| 404 `file_not_found` | The name is not served (for example `source.pdf`), or this result has no such file. |
| 409 `not_ready` | The document has no result yet. |

Response headers:

- `Content-Type` by extension: `.md` `text/markdown; charset=utf-8`, `.json` `application/json`, `.jsonl`
  `application/x-ndjson`, `.html` `text/html; charset=utf-8`, images `image/png`, `image/jpeg`, `image/gif`,
  `image/webp`.
- `Content-Disposition: inline` with the file's own name, for example `filename="chunks.jsonl"`.
- `X-Content-Type-Options: nosniff`. HTML files also get `Content-Security-Policy: default-src 'none'; img-src
  data: 'self'; style-src 'unsafe-inline'; sandbox`, so they show with their images but run no script.

Example:

```powershell
curl.exe -o chunks.jsonl http://127.0.0.1:8000/documents/dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040/files/chunks.jsonl
```

The PDF itself is never served back: keep your own copy (its SHA-256 is the `doc_id`).

### Tools

#### GET /tools

The definitions of the 9 protocol tools, ready to pass to an LLM API as tool definitions. What each tool returns
is in [Protocol tools](#5-protocol-tools).

| Parameter | In | Type | Default | Meaning |
|---|---|---|---|---|
| `style` | query | string | `anthropic` | `anthropic` (`{name, description, input_schema}`) or `openai` (`{type: "function", function: {name, description, parameters}}`). |

| Status | Meaning |
|---|---|
| 200 | The definitions. |
| 400 `invalid_style` | Another `style`. |

<!-- fields: rest:GET /tools -->
| Field | Type | Meaning |
|---|---|---|
| `tools` | object[] | The 9 tools, in this order: `document_info`, `outline`, `find_sections`, `read_section`, `get_table`, `get_soa`, `search`, `abbreviation`, `quote_check`. |
| `tools[].name` | string | `anthropic` style: the tool name. |
| `tools[].description` | string | `anthropic` style: what the tool does, written for the model. |
| `tools[].input_schema` | object (shared: svc-tool-schema) | `anthropic` style: the JSON Schema of the arguments. |
| `tools[].type` | string | `openai` style: always `function`. |
| `tools[].function` | object | `openai` style: the definition. |
| `tools[].function.name` | string | The tool name. |
| `tools[].function.description` | string | What the tool does. |
| `tools[].function.parameters` | object (shared: svc-tool-schema) | The JSON Schema of the arguments, the same as `input_schema`. |

The argument schema of a tool (`input_schema`, `parameters`, and MCP's `inputSchema`):

<!-- fields: shared:svc-tool-schema -->
| Field | Type | Meaning |
|---|---|---|
| `type` | string | Always `object`. |
| `properties` | object (map) | The arguments, keyed by argument name; empty for a tool without arguments. |
| `properties.<argument>` | object | The JSON Schema of one argument. |
| `properties.<argument>.type` | string | `string`, `integer` or `boolean`. |
| `properties.<argument>.description` | string | What the argument means; absent for some arguments. |
| `properties.<argument>.enum` | string[] | The allowed values, when the argument has a fixed set (for example `role` of `find_sections`). |
| `properties.<argument>.minimum` | integer | Smallest allowed value of an integer argument. |
| `properties.<argument>.maximum` | integer | Largest allowed value of an integer argument. |
| `properties.<argument>.default` | string \| integer \| boolean | The value used when the argument is omitted. |
| `required` | string[] | Arguments that must be given; absent when none is required. |
| `additionalProperties` | boolean | Always `false`: other arguments are refused. |

Example (first tool of each style):

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/tools?style=openai" | ConvertTo-Json -Depth 8
```

```json
{"tools": [{"name": "document_info",
            "description": "Identity of the protocol (StudyTitle, StudyIdentifier, StudyVersion, sponsor): … Call it first.",
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": false}},
           /* 8 more */]}
```

With `style=openai`:

```json
{"tools": [{"type": "function",
            "function": {"name": "document_info", "description": "Identity of the protocol … Call it first.",
                         "parameters": {"type": "object", "properties": {}, "additionalProperties": false}}},
           /* 8 more */]}
```

The REST definitions have no `doc_id` argument: the document is in the route's path. Your agent loop adds it when
it calls `POST /documents/{doc_id}/tools/{tool}`.

#### POST /documents/{doc_id}/tools/{tool}

Runs one protocol tool on the document's published result. The body of a 200 answer is the tool's result, field by
field in [Protocol tools](#5-protocol-tools).

| Parameter | In | Type | Meaning |
|---|---|---|---|
| `doc_id` | path | string | The document id. |
| `tool` | path | string | `document_info`, `outline`, `find_sections`, `read_section`, `get_table`, `get_soa`, `search`, `abbreviation` or `quote_check`. |
| (arguments) | JSON body | object | The tool's arguments, as in its schema from `GET /tools`. For a tool without required arguments the body may be missing or `{}`: the tool's defaults apply. |

| Status | Meaning |
|---|---|
| 200 | The tool's result. |
| 400 `invalid_id` | Malformed `doc_id`. |
| 400 `bad_request` | The tool refused its arguments, for example `read_section` without `section` or `cursor`. |
| 404 `document_not_found` | No such document. |
| 404 `unknown_tool` | No tool of that name; `details.tools` lists the tool names. |
| 404 `not_found` | The tool found nothing: no such section, table or term. The message lists the closest titles or captions. |
| 409 `not_ready` | The document has no result yet. |
| 409 `search_unavailable` | `search` on a document without embeddings (converted offline). The message says: reprocess it online with `POST /documents/{doc_id}/reprocess?force=true`. |
| 422 `validation_error` | The arguments do not match the tool's schema: wrong type, out of range, unknown argument, required argument missing. |
| 500 `document_unreadable` | The stored result cannot be opened. |
| 500 `tool_failed` | The tool failed unexpectedly; the message names the exception type. |
| 502 `upstream_error` | `search`: the OpenRouter request failed. |
| 503 `offline` | `search` while offline, for a query whose embedding or rerank is not cached. |
| 503 `no_api_key` | `search` needs `OPENROUTER_API_KEY`, which is not set. |

Errors raised by the tool itself (`bad_request`, `not_found`, `search_unavailable`, `tool_failed`,
`upstream_error`, `offline`, `no_api_key`) carry `details.tool` with the tool name.

Example:

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/documents/dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040/tools/read_section -ContentType application/json -Body '{"section": "inclusion"}'
```

Notes:

- Each tool has its own typed route in `/docs` with its request model. A generic route with the same path
  template answers names that have no typed route, which is how an unknown name gets `404 unknown_tool`.
- The body is exactly the MCP `structuredContent` of the same tool call. It is also the result of
  `protocol_api.ProtocolDocument.call_tool` on the stored result, except that paths on the server are replaced by
  `<document>` and `<data>` (for example `document_info`'s `out_dir` is `"<document>"`).
- Only `search` can call OpenRouter (query embedding and rerank, both cached); every other tool is local.
- Error messages quote your own arguments (for example the search query) and, for `not_found`, the closest section
  titles or table captions of the same document; never other document text.

### REST errors

Every error is a JSON body with an HTTP status. Branch on `type`; `message` is for people and may change.

<!-- fields: rest-error -->
| Field | Type | Meaning |
|---|---|---|
| `type` | string | Machine-readable error type (table below). |
| `message` | string | What went wrong, for people. It never contains document text, paths under `DATA_DIR` or the values you sent, except your own tool arguments (see the tool route). |
| `details` | object | Extra data; present only for some types. |
| `details.errors` | object[] | `validation_error`: one entry per invalid part of the request. |
| `details.errors[].loc` | (string \| integer)[] | Where: `body`, `query` or `path`, then the field name (and list index). For example `["body", "max_tokens"]`. |
| `details.errors[].msg` | string | What is wrong, such as `Field required` or `Input should be a valid integer`. |
| `details.errors[].type` | string | Pydantic error type, such as `missing`, `int_type`, `bool_type`, `extra_forbidden`. The submitted value is never echoed. |
| `details.tool` | string | The tool, for errors raised by a protocol tool and for MCP argument errors. |
| `details.tools` | string[] | `unknown_tool`: the tool names there are (REST: the 9 protocol tools; MCP: all 12 tools). |

| Status | `type` | When |
|---|---|---|
| 400 | `invalid_id` | A `doc_id` that is not 64 lowercase hex characters, or a `job_id` that is not 32. |
| 400 | `invalid_status` | `GET /documents?status=` with an unknown status. |
| 400 | `invalid_state` | `GET /jobs` or `GET /documents/{doc_id}/jobs` with an unknown `state`. |
| 400 | `invalid_style` | `GET /tools?style=` other than `anthropic` or `openai`. |
| 400 | `password_in_query` | `POST /documents` with a `password` in the URL. |
| 400 | `empty` | An upload without bytes (MCP: empty `data_base64`). |
| 400 | `invalid_pdf` | An upload with a PDF header that cannot be opened. |
| 400 | `password_required` | An encrypted PDF uploaded without a password. |
| 400 | `bad_password` | An encrypted PDF uploaded with a wrong password. |
| 400 | `bad_request` | A protocol tool refused its arguments (`details.tool`), or a request body that cannot be parsed. |
| 400 | `invalid_arguments` | MCP only: arguments that do not match the tool's input schema, or `ingest_document` without exactly one of `path` and `data_base64` (`details.tool`). Over REST, invalid tool arguments are `422 validation_error`. |
| 400 | `invalid_base64` | MCP `ingest_document`: `data_base64` is not valid base64. |
| 400 | `invalid_name` | MCP `ingest_document`: `path` is not a PDF file name in the inbox (subfolders and other folders are refused). |
| 400 | `password_unsupported_here` | MCP over stdio: a password, which that process (it runs no worker) cannot hand to a job. |
| 403 | `invalid_origin` | A browser request from a page of a host outside `ALLOWED_HOSTS`. |
| 404 | `not_found` | An unknown route; or a protocol tool found nothing (no such section, table or term; `details.tool`). |
| 404 | `document_not_found` | No such document; for reprocess also: no stored source PDF. |
| 404 | `job_not_found` | No such job. |
| 404 | `file_not_found` | A file name the files route does not serve, or a file this result lacks; MCP: no such file in the inbox. |
| 404 | `unknown_tool` | No tool of that name (`details.tools`). |
| 405 | `method_not_allowed` | A method the route does not have; the `Allow` header lists the right ones. |
| 409 | `not_ready` | Files or tools of a document that has no result yet. |
| 409 | `document_busy` | Delete while a job of the document runs. |
| 409 | `not_cancellable` | Cancel of a job that is not queued. |
| 409 | `search_unavailable` | `search` on a document without embeddings (`details.tool`). |
| 413 | `too_large` | The upload (request body, PDF, or MCP `data_base64`) exceeds `MAX_UPLOAD_MB`. |
| 415 | `not_pdf` | The upload has no `%PDF-` header in its first 1024 bytes. |
| 421 | `invalid_host` | A `Host` header outside `ALLOWED_HOSTS`. |
| 422 | `validation_error` | A request that does not match the route's parameters or body (`details.errors`). |
| 500 | `internal_error` | An unexpected failure; the message names only the exception type. |
| 500 | `document_unreadable` | The stored result cannot be opened by the tools. |
| 500 | `tool_failed` | A protocol tool failed unexpectedly (`details.tool`). |
| 500 | `delete_failed` | The document's files could not be deleted; try again. |
| 500 | `read_failed` | MCP `ingest_document`: the inbox file cannot be read. |
| 502 | `upstream_error` | `search`: the OpenRouter request failed (`details.tool`). |
| 503 | `not_started` | The service has not started yet (or failed to start, over MCP stdio). |
| 503 | `offline` | `search` while offline, for a query that is not cached (`details.tool`). |
| 503 | `no_api_key` | `search` without `OPENROUTER_API_KEY` (`details.tool`). |
| other | `http_error` | Any other HTTP status raised by the web framework. |

Examples:

```json
{"type": "validation_error", "message": "invalid request: body.max_tokens: Input should be a valid integer",
 "details": {"errors": [{"loc": ["body", "max_tokens"], "msg": "Input should be a valid integer", "type": "int_type"}]}}
```

```json
{"type": "not_found", "message": "no section 'S999' (not an id, number or role); closest titles: none",
 "details": {"tool": "read_section"}}
```

```json
{"type": "invalid_host", "message": "Host header not allowed (see ALLOWED_HOSTS)"}
```

`GET /health` is the one route whose 503 can also be its normal body (`status: unavailable`), not an error body.

## 4. MCP

The service is also an MCP server with 12 tools: three of its own (`list_documents`, `get_document`,
`ingest_document`) and the 9 protocol tools. It runs on the same service layer as the REST API, so each tool
returns exactly the JSON of the matching REST route. Setting up Claude Code or Claude Desktop is described in
[DEPLOY.md](../DEPLOY.md#5-mcp).

### HTTP endpoint

- **URL:** `http://127.0.0.1:8000/mcp`, exactly this path: no trailing slash, no redirect.
- **Transport:** Streamable HTTP, stateless. There is no `Mcp-Session-Id`; every POST stands alone, and a restart of
  the service loses nothing.
- **POST only.** Each POST gets one `application/json` answer (no SSE stream). `GET` and `DELETE` answer `405` with
  `Allow: POST` and a JSON-RPC error body.
- **Headers:** `Content-Type: application/json` and `Accept: application/json, text/event-stream`.
- **Protocol versions** (MCP SDK 2.3.0): the `initialize` handshake with `2024-11-05`, `2025-03-26`, `2025-06-18`
  or `2025-11-25`, and the self-contained requests of `2026-07-28`. A handshake-era client may also skip
  `initialize` and send `tools/call` with an `MCP-Protocol-Version` header, as below.
- **Host and Origin:** the same `ALLOWED_HOSTS` checks as REST (`421` / `403` JSON error bodies), plus the MCP SDK's
  own DNS-rebinding protection.
- **Body size:** at most a base64 PDF of `MAX_UPLOAD_MB` plus 1 MiB (about 134 MiB for the default 100); larger
  bodies get `413` from the transport.
- **No authentication**, as for REST.
- **Server info:** name `protocol-service`, version = the pipeline version. Its instructions give agents the
  suggested flow: find or ingest a document, poll `get_document`, then `document_info`, `outline` /
  `find_sections`, `read_section`, and `quote_check` for every extracted value.

A raw call (any HTTP client works the same way):

```powershell
$body = '{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_document", "arguments": {"doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040"}}}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/mcp -ContentType application/json -Headers @{Accept = "application/json, text/event-stream"; "MCP-Protocol-Version" = "2025-11-25"} -Body $body
```

The answer is `{"jsonrpc": "2.0", "id": 1, "result": {…}}`, where `result` is the [tool result](#mcp-results).

### stdio

```powershell
docker exec -i protocol-service python -m service.mcp_stdio
```

- The same 12 tools over the container's `DATA_DIR`, for one client per process. stdout carries only JSON-RPC; logs
  go to stderr.
- **No worker in this process.** Documents it ingests are queued in the shared registry and converted by the API
  process's worker.
- **No passwords:** `ingest_document` with a `password` answers `password_unsupported_here`, because the password
  would stay in this process's memory, out of the worker's reach. Upload encrypted PDFs over REST or HTTP MCP.
- The handshake and `tools/list` answer at once; the first tool call waits until the service has started (about 10
  to 15 seconds).
- `--with-worker` runs a worker in the stdio process. Use it only when no API process uses the same `DATA_DIR`.

### MCP tools

| Tool | Arguments | Same as |
|---|---|---|
| `list_documents` | `status` (a document status), `limit` 0–1000 (default 100), `offset` ≥ 0 (default 0) | `GET /documents` |
| `get_document` | `doc_id` (required) | `GET /documents/{doc_id}` |
| `ingest_document` | exactly one of `path` or `data_base64`; `filename`, `reprocess` (default false), `password` | `POST /documents` |
| `document_info` | `doc_id` (required) | `POST /documents/{doc_id}/tools/document_info` |
| `outline` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/outline` |
| `find_sections` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/find_sections` |
| `read_section` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/read_section` |
| `get_table` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/get_table` |
| `get_soa` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/get_soa` |
| `search` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/search` |
| `abbreviation` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/abbreviation` |
| `quote_check` | `doc_id` (required) + the tool's arguments | `POST /documents/{doc_id}/tools/quote_check` |

- The protocol tools have the descriptions and input schemas of `GET /tools`, with `doc_id` added as the first,
  required property. Their results and errors are those of the REST route.
- Arguments are checked against the tool's input schema before anything runs. A mismatch is an error result
  `invalid_arguments` with `details.tool`; its message names the argument and the rule, never the value sent.
  Arguments that are not a JSON object are refused by the SDK with JSON-RPC error `-32602`, not as a tool result.
- `list_documents` refuses a `limit` outside 0–1000 (`invalid_arguments`), where REST clamps it.
- `ingest_document`:
  - `path` is a PDF in the inbox folder (`DATA_DIR/inbox`, `/data/inbox` in the container): its file name,
    `inbox/<name>`, or the absolute path of a file directly in the inbox. Anything else is `invalid_name`; a
    missing file is `file_not_found`. Copy the file in first, for example with `docker cp`.
  - `data_base64` is the PDF itself; a `data:application/pdf;base64,` prefix is accepted. Its size is checked
    before decoding (`too_large`). It is held in memory: use the inbox or REST for large PDFs.
  - `filename` names a `data_base64` upload in `filenames`; an inbox file keeps its own name.
  - The answer is the upload decision of `POST /documents`; then poll `get_document`.
- Delete, reprocess with `force`, jobs and files have no MCP tool: use REST for them.

### MCP results

A `tools/call` result, for success and error alike (see [MCP envelope](#mcp-envelope)):

<!-- fields: mcp:envelope -->
| Field | Type | Meaning |
|---|---|---|
| `content` | object[] | One text block, for clients without structured content. |
| `content[].type` | string | Always `text`. |
| `content[].text` | string | The same JSON as `structuredContent`, serialised (UTF-8, not ASCII-escaped). |
| `structuredContent` | object | The result: the tool's JSON, or the [error body](#rest-errors) when `isError` is `true`. |
| `isError` | boolean | `true` when `structuredContent` is an error body. |

Success (trimmed):

```json
{
  "content": [{"type": "text", "text": "{\"doc_id\": \"dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040\", \"filenames\": [\"protocol.pdf\"], …}"}],
  "structuredContent": {"doc_id": "dbadb3709bb5c41f1ede5ccf60412e9e2fa7ec18d3fdc996ec329cd80e61a040", "filenames": ["protocol.pdf"], /* … */},
  "isError": false
}
```

Error:

```json
{
  "content": [{"type": "text", "text": "{\"type\": \"document_not_found\", \"message\": \"no such document\"}"}],
  "structuredContent": {"type": "document_not_found", "message": "no such document"},
  "isError": true
}
```

An unexpected failure is `{"type": "internal_error", "message": "internal error (<exception type>)"}` with
`isError: true`. Errors of the JSON-RPC layer itself (unknown method, malformed request) are JSON-RPC `error`
objects, not tool results.

#### tools/list

<!-- fields: mcp:tools/list -->
| Field | Type | Meaning |
|---|---|---|
| `tools` | object[] | The 12 tools, in this order: `list_documents`, `get_document`, `ingest_document`, then the 9 protocol tools in the order of `GET /tools`. |
| `tools[].name` | string | The tool name. |
| `tools[].description` | string | What the tool does, written for the model. |
| `tools[].inputSchema` | object (shared: svc-tool-schema) | The JSON Schema of the arguments; the protocol tools have `doc_id` as their first, required property. |
| `tools[].annotations` | object | MCP hints for clients. |
| `tools[].annotations.readOnlyHint` | boolean | `true` for every tool except `ingest_document`. |
| `tools[].annotations.openWorldHint` | boolean | `true` for `search` (its query and candidate passages go to OpenRouter) and `ingest_document` (its processing sends figures and chunk texts to OpenRouter); `false` for every other tool. |
| `tools[].annotations.destructiveHint` | boolean | `ingest_document` only: `false`. |
| `tools[].annotations.idempotentHint` | boolean | `ingest_document` only: `true` (the same bytes give the same `doc_id` and job). |

Example (one tool):

```json
{"tools": [{"name": "get_document",
            "description": "One document: status, pipeline version and stale flag, identifiers, … Poll it after ingest_document until status is done or done_with_warnings.",
            "inputSchema": {"type": "object",
                            "properties": {"doc_id": {"type": "string", "description": "The document id (SHA-256 of the PDF) from list_documents or ingest_document."}},
                            "required": ["doc_id"], "additionalProperties": false},
            "annotations": {"readOnlyHint": true, "openWorldHint": false}},
           /* 11 more */]}
```

#### list_documents

The `structuredContent` equals the body of `GET /documents` with the same arguments.

<!-- fields: mcp:list_documents -->
| Field | Type | Meaning |
|---|---|---|
| `documents` | object[] (shared: svc-document) | The documents, most recently updated first. |
| `limit` | integer | The page size used. |
| `offset` | integer | The offset used. |

#### get_document

The `structuredContent` equals the body of `GET /documents/{doc_id}`: see that route for the meaning of each
field and an example. Poll it after `ingest_document` until `status` is final.

<!-- fields: mcp:get_document -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | As in `GET /documents/{doc_id}`, like every field below. |
| `filenames` | string[] | Upload file names. |
| `size` | integer \| null | Size in bytes. |
| `pages` | integer \| null | Number of pages. |
| `text_fingerprint` | string \| null | Hash of the normalised text layer. |
| `identifiers` | object (shared: svc-identifiers) | Study identifiers. |
| `status` | string | State of the latest job, or `none`. |
| `pipeline_version` | string \| null | Pipeline version of the served result. |
| `fingerprint` | string \| null | Fingerprint of the served result. |
| `has_result` | boolean | Whether a result is published. |
| `published_at` | string \| null | When the served result was published. |
| `stale` | boolean | Whether the served result is stale. |
| `quality` | object (shared: svc-quality-summary) | Quality summary. |
| `error` | object \| null (shared: svc-document-error) | The latest failure. |
| `current_job_id` | string \| null | The latest job. |
| `result_job_id` | string \| null | The job whose result is served. |
| `created_at` | string | First registration. |
| `updated_at` | string | Last change. |
| `related` | object[] | Other versions of the protocol (same NCT or sponsor protocol id). |
| `related[].doc_id` | string | The related document. |
| `related[].reasons` | string[] | `same nct` and/or `same sponsor_protocol_id`. |
| `same_text_as` | string[] | Documents with the same text. |
| `current_job` | object \| null (shared: svc-job) | The latest job with its stage and progress. |
| `warnings` | string[] | Warnings of the served result. |
| `files` | string[] | Served files of the result. |

#### ingest_document

The `structuredContent` is the upload decision of `POST /documents`; for the same bytes and state it is equal to
the REST answer.

<!-- fields: mcp:ingest_document -->
| Field | Type | Meaning |
|---|---|---|
| `doc_id` | string | SHA-256 of the PDF. |
| `job_id` | string \| null | The queued or active job; null when an existing result is returned. |
| `status` | string | `queued` for a new job, else the document's status. |
| `duplicate` | boolean | The bytes were already known with a result or an active job. |
| `stale` | boolean | The served result was made by another pipeline version or fingerprint. |
| `queued` | boolean | This call queued a job. |
| `warnings` | string[] | `same text as <doc_id>` warnings. |

Example (a made-up PDF, then the same bytes again after its job finished):

```json
{"doc_id": "d72c110b6c29bff8a31cced909eec735e35481389eaf7b9b19c746c7d906bcf5", "job_id": "bb36959d592441608148a46be897f63a",
 "status": "queued", "duplicate": false, "stale": false, "queued": true, "warnings": []}
```

```json
{"doc_id": "d72c110b6c29bff8a31cced909eec735e35481389eaf7b9b19c746c7d906bcf5", "job_id": null,
 "status": "done", "duplicate": true, "stale": false, "queued": false, "warnings": []}
```

## 5. Protocol tools

Nine tools give an LLM agent, or any client, read access to ONE converted protocol. The same tool returns the same
JSON result over all three surfaces:

| Surface | Call | Result |
|---|---|---|
| REST | `POST /documents/{doc_id}/tools/{tool}`, the arguments as the JSON body (an empty or missing body when the tool needs none) | The response body |
| MCP | Tool `<tool>`, with the tool's arguments plus a required `doc_id` | `structuredContent` (and the same JSON as a text block); see [MCP envelope](#mcp-envelope) |
| Python | `ProtocolDocument.open(folder).call_tool(name, args)` in `protocol_api.py` | The returned dict |

- **Definitions:** `GET /tools?style=anthropic|openai` returns `{"tools": [...]}` with each tool's name, description
  and JSON Schema (`input_schema`, or `function.parameters` for `openai`). The MCP listing has the same schemas plus
  `doc_id`.
- **Network:** every tool is local except `search`, which sends the question and the candidate passages to
  OpenRouter (embedding and reranking, zero data retention). See [search](#search).
- **Surfaces differ in two ways only:** REST and MCP replace local folder paths with `<document>` (for example
  `document_info.out_dir`), and they turn a tool error into a REST error (see [Tool errors](#tool-errors)).
- **How an agent should use the tools** (the recommended loop, which tool for which USDM class, the Markdown
  conventions of `read_section`, the `quote_check` matching tiers): [docs/protocol_api.md](protocol_api.md).
- **Conventions used below:** [IDs](#ids) (section ids, item refs, table ids), [Provenance](#provenance),
  [Pages and boxes](#pages-and-boxes), [Errors](#errors).

| Tool | Use it for |
|---|---|
| [document_info](#document_info) | Identity of the study: titles, registry ids, version, sponsor; where things are. Call it first. |
| [outline](#outline) | The section tree with roles and sizes, to plan what to read. |
| [find_sections](#find_sections) | Sections by ICH M11 role, title words or number. |
| [read_section](#read_section) | The complete Markdown of a section, a section number or a role, in parts. |
| [get_table](#get_table) | One whole table: Markdown, cell grid, merged cells, row lines. |
| [get_soa](#get_soa) | The structured Schedule of Activities: visits, activities, scheduled cells, footnotes. |
| [search](#search) | Ranked context passages for a question (calls OpenRouter). |
| [abbreviation](#abbreviation) | Glossary lookup in both directions. |
| [quote_check](#quote_check) | Whether a quote occurs in the document, and where. |

Every result has a top-level `provenance` ([shared](#provenance)). Page numbers are 1-based PDF pages. Token counts
use the tokenizer of the embedding model (Qwen3-Embedding-8B).

### Tool errors

A tool never raises. In Python, `call_tool` returns an error result instead of the tool's result:

<!-- fields: tool-error -->
| Field | Type | Meaning |
|---|---|---|
| `error` | string | What went wrong, for people (and for the agent to read). Lookup errors include hints from the same document, such as the closest section titles or table captions. Do not parse it. |
| `error_type` | string | The exception class name, or `UnknownTool` / `InvalidArguments` for `call_tool`'s own checks; see the table below. |
| `tool` | string | The tool that was called. Absent for an unknown tool name. |
| `tools` | string[] | Only for an unknown tool name: the 9 valid names. |

Over REST and MCP the error result is not returned as such: the service turns it into a REST error with an HTTP
status and the body `{"type", "message", "details": {"tool": <name>}}`, which MCP returns with `isError: true` (see
[Errors](#errors)). For `bad_request`, `not_found`, `search_unavailable` and `offline` the message is the tool's
`error` with local paths replaced; the 422 and MCP argument errors name the argument and the rule, never the
value; the other types carry a short generic message.

| `error_type` | When (Python) | REST status and `type` |
|---|---|---|
| `UnknownTool` (with `tools`) | Unknown tool name. | 404 `unknown_tool`, checked by the service before anything runs; `details.tools` lists the names. |
| `InvalidArguments` | Arguments fail the tool's JSON Schema: an unknown key, a wrong type, a value out of range or not in the enum, a missing required key. | REST: 422 `validation_error` (the tool's route checks the body first). MCP: `invalid_arguments` (checked against the advertised schema). A call that passes those checks but fails the tool's own: 400 `invalid_arguments`. |
| `ProtocolAPIError` | A request the document cannot answer as asked: `find_sections` with neither `role` nor `query`; `read_section` with neither `section` nor `cursor`; a blank `search` query or `abbreviation` term; a `quote_check` quote with fewer than 2 letters or digits. | 400 `bad_request` |
| `NotFoundError` | Nothing matches: `read_section` with no section of that id, number, role or title (the message lists the closest titles), a role that no section has and that has no fallback, an invalid cursor or one past the end; `get_table` with an id beyond the last table or words that match no table (the message lists the closest captions); `quote_check` with an unknown `section_id`. | 404 `not_found` |
| `SearchUnavailable` | `search` on a result without embeddings. | 409 `search_unavailable` |
| `OfflineCacheMiss` | `search` while `OPENROUTER_OFFLINE=1`, when the question's embedding or rerank result is not cached. | 503 `offline` |
| `OfflineRefused` | An OpenRouter call refused because `OPENROUTER_OFFLINE` is set. | 503 `offline` |
| `RuntimeError` | `search`: OpenRouter answered with an error status (the message starts `OpenRouter <status>`); `OPENROUTER_API_KEY` is not set; or the chunks and `stats.json` describe different documents. | 502 `upstream_error`; 503 `no_api_key`; 500 `tool_failed` |
| httpx transport errors (`ConnectError`, `ConnectTimeout`, `ReadTimeout`, `RemoteProtocolError`, ...) | `search` could not reach OpenRouter. | 502 `upstream_error` |
| Any other class name | An unexpected failure (a bug); logged with its traceback. | 500 `tool_failed` (the message names only the tool and the class) |

Example (Python):

```json
{"error": "abbreviation needs a term", "error_type": "ProtocolAPIError", "tool": "abbreviation"}
```

### document_info

The identity of the study: titles, registry identifiers, version and date, sponsor, cover-page label/value pairs,
counts, and the outermost section of each role. Every value comes with the verbatim line it was read from. Call it
first; it takes no arguments.

#### Arguments

None.

#### Result

<!-- fields: tool:document_info -->
| Field | Type | Meaning |
|---|---|---|
| `api_version` | string | Version of the tool result format: `"1"`. |
| `document` | string | Name of the Docling document (`protocol` for results of the service). |
| `out_dir` | string | Python: absolute path of the result folder, with `/` separators. REST and MCP: `<document>`. |
| `source_json` | string | File name of the Docling JSON the tools read (`protocol.json`). |
| `source_sha256` | string \| null | First 16 hex digits of the SHA-256 of that Docling JSON (not of the PDF: the PDF's hash is the `doc_id`). Opening the document checks it against the chunks, so the tools never read another JSON than the one the chunks were made from. |
| `titles` | object | Study titles. All four keys are always present. |
| `titles.official_title` | object (shared: tool-sourced-value) \| null | The full title: the value of a cover-page title label ("Protocol Title:", "Full Title:", "Title:", ...), else the detected title. |
| `titles.brief_title` | object (shared: tool-sourced-value) \| null | The value of a "Short / Brief / Public / Lay Title:" cover-page label; null without one. |
| `titles.acronym` | object (shared: tool-sourced-value) \| null | The value of an "Acronym:" cover-page label, else the acronym detected in the text. |
| `titles.detected_title` | object (shared: tool-sourced-value) \| null | The title found by heuristics on the first pages, without labels. It is the title `search` shows in its identity block. |
| `identifiers` | object | Study identifiers found by pattern anywhere in the document, including tables and running headers/footers. All six keys are always present; a key is null when nothing was found. A value counts as the study's own when it stands in a header or footer, on pages 1-3, or right after its label; per kind, the own value seen on the most pages wins. |
| `identifiers.sponsor_protocol_id` | object (shared: tool-identifier) \| null | Sponsor protocol number: the protocol id detected with the document metadata, else the value after a "Protocol Number:" label. |
| `identifiers.nct` | object (shared: tool-identifier) \| null | ClinicalTrials.gov number: `NCT` and 8 digits. |
| `identifiers.eudract` | object (shared: tool-identifier) \| null | EudraCT number: `YYYY-NNNNNN-NN`. |
| `identifiers.eu_ct` | object (shared: tool-identifier) \| null | EU CT (CTIS) number: `YYYY-NNNNNN-NN-NN`, optionally with another `-NN`. |
| `identifiers.ind` | object (shared: tool-identifier) \| null | US IND number, recognised only after an "IND" label. |
| `identifiers.who_utn` | object (shared: tool-identifier) \| null | WHO Universal Trial Number: `UNNNN-NNNN-NNNN`. |
| `other_identifiers_cited` | object (map) | Values of the same kinds that occur in the document but are not this study's own, such as another trial's NCT number cited in the background. Empty object when there are none. |
| `other_identifiers_cited.<kind>` | string[] | The other values of one kind, in order of appearance. `<kind>` is one of the `identifiers` keys. |
| `version` | object | Version, amendment and date. All six keys are always present. |
| `version.label` | string \| null | The detected version label, such as `Amendment 3` or `Version 2.0`. |
| `version.amendment` | string \| null | The amendment number when the label is an amendment (`3`), else null. |
| `version.date` | object (shared: tool-sourced-value) \| null | The detected version or approval date, as printed (`05-Dec-2017`). |
| `version.cover_amendment` | object (shared: tool-sourced-value) \| null | The value of an "Amendment (Number):" cover-page label. |
| `version.cover_version` | object (shared: tool-sourced-value) \| null | The value of a "Version:" cover-page label. |
| `version.cover_approval_date` | object (shared: tool-sourced-value) \| null | The value of a date label on the cover page ("Approval Date:", "Issue Date:", "Version Date:", "Date of Protocol:", ...). |
| `sponsor` | object \| null | The sponsor organisation; null when no method finds one. |
| `sponsor.name` | string | The organisation's name. From a cover-page label it is the value cut before the address (at a comma followed by a digit, a long number or a semicolon). |
| `sponsor.value` | string | The verbatim label value (with the address) for `cover-page label`; otherwise the same as `name`. |
| `sponsor.method` | string | How the name was found, tried in this order: `cover-page label`, `copyright line`, `'property of' line`, `company-name pattern on the title pages (unverified)`. Treat every method but the first as a guess and check it with `quote_check`. |
| `sponsor.source` | object | The line the name was read from. |
| `sponsor.source.page` | integer | PDF page of the line. |
| `sponsor.source.text` | string | The line, whitespace-normalised (for a cover-page label: the label line, ` / `, and the next line when the value is there). |
| `sponsor.source.ref` | string | [Item ref](#ids) of the line. |
| `sponsor.source.layer` | string | `body` (a cover-page label or a page line) or `furniture` (a running header or footer). |
| `sponsor.source.section_id` | string \| null | Section of the line; null for a header or footer. |
| `sponsor.source.refs` | string[] | Item refs of the lines `text` comes from (two for a cover-page label whose value is on the next line); the first is `ref`. |
| `sponsor.source.row` | integer \| null | Always null (the sponsor is never read from a table row). |
| `sponsor.provenance` | object (shared: provenance) | The section, page and ref of that line. |
| `cover_page` | object (map) | Label/value pairs read from pages 1-3, keyed by field. Empty object when there are none. |
| `cover_page.<field>` | object | One label/value pair; the first occurrence of each field wins. `<field>` is one of `brief_title`, `official_title`, `acronym`, `sponsor_protocol_number`, `amendment`, `version`, `sponsor`, `sponsor_address`, `phase`, `compound`, `indication`, `approval_date`. |
| `cover_page.<field>.value` | string | The text after the label's colon; when the label line ends at the colon, the next text line on the same page (at most 400 characters). |
| `cover_page.<field>.page` | integer | PDF page (1-3). |
| `cover_page.<field>.text` | string | The label line, plus ` / ` and the next line when the value came from it. |
| `cover_page.<field>.refs` | string[] | Item refs of those lines. |
| `page_count` | integer | Pages in the PDF. |
| `section_count` | integer | Sections in the section index, including the front matter `S0`. |
| `soa_table_count` | integer | Number of entries in `soa_tables`. |
| `soa_tables` | object[] (shared: tool-soa-table) | Tables (other than tables of contents) inside sections with role `soa`, in document order. `get_soa` also detects SoA tables by their content, so its list can differ. |
| `roles_found` | object (map) | The ICH M11 roles present in the document. Roles without a section are left out. |
| `roles_found.<role>` | string[] | Section ids of the outermost sections with this role (whose parent has another role), in document order. `read_section("<role>")` reads all of them. |
| `reading_order_repairs` | object[] | Text units moved under the heading they lie below, at read time. Only for results converted before post-processing made these repairs itself; empty for current results. |
| `reading_order_repairs[].ref` | string | Item ref of the moved unit. |
| `reading_order_repairs[].page` | integer \| null | Its PDF page. |
| `reading_order_repairs[].from_section` | string | Section id the converter put it in. |
| `reading_order_repairs[].to_section` | string | Section id every tool now uses for it. |
| `identity_block` | string | The identity text that `search` puts first in its context, one fact per line: title, acronym, sponsor protocol number and registry ids each with the verbatim line it was found in, a "none found" line for each missing id (except WHO UTN), and the version. |
| `provenance` | object (shared: provenance) | The first title-page section (`S0` when no section has role `title_page`), with the pages and refs of the located sources above (page 1 when none was located). |
| `warnings` | string[] | Problems with this result, such as a `sections.json` that could not be loaded (the section index was then rebuilt from the Docling JSON), or an item ref that does not resolve. Rendering by other tools can add warnings, so the list can grow during a session. |

<!-- fields: shared:tool-sourced-value -->
| Field | Type | Meaning |
|---|---|---|
| `value` | string | The value as written in the document. |
| `method` | string | How the value was found: `cover-page label`, `title page` (the first long text after the first heading on page 1; for an acronym, a page-1 heading that looks like one), `running header` (pages 1-3), `labelled pattern` (after a label such as "Version" or a date pattern in the title pages and headers/footers), `history table` (the latest row of a document-history table) or `stored` (the stored value is not what the current rules find; then `source` is the first item holding it as a whole token, or null). Absent for `version.cover_*`, which are always cover-page labels. |
| `source` | object (shared: tool-value-source) \| null | The line the value was read from; null when the value cannot be located in the text. |

<!-- fields: shared:tool-identifier -->
| Field | Type | Meaning |
|---|---|---|
| `value` | string | The identifier as written. |
| `method` | string | `registry pattern` for NCT, EudraCT, EU CT, IND and WHO UTN. For the sponsor protocol number, how the document metadata decided it: `running header` ("Protocol <id>" in the pages 1-3 header), `labelled pattern` (after a "Protocol Number:"-style label), `header/footer frequency` (the id repeated in most headers/footers); `stored` as above. The method describes how the value was chosen; `source` is the line shown as its evidence, which can be another line holding the same value. |
| `source` | object (shared: tool-value-source) \| null | The line the identifier was read from; null when it cannot be located. |
| `label` | string | Readable name of the kind: `Sponsor protocol number`, `ClinicalTrials.gov (NCT) number`, `EudraCT number`, `EU CT number`, `IND number` or `WHO Universal Trial Number`. |

<!-- fields: shared:tool-value-source -->
| Field | Type | Meaning |
|---|---|---|
| `page` | integer \| null | PDF page of the line. |
| `layer` | string | `body` (page content), `furniture` (a running header or footer) or `table` (a table row; identifiers only). |
| `text` | string | The verbatim line, whitespace-normalised. For identifiers, at most about 160 characters around the value, cut with `…`. For a cover-page label, the label line plus ` / ` and the next line when the value came from it. Show it as the evidence for the value. |
| `refs` | string[] | Item refs of the lines `text` comes from (two for a cover-page label whose value is on the next line); the first is `ref`. |
| `ref` | string \| null | [Item ref](#ids) of the item the value was read from: the label line for a cover-page value, the table for a table row, otherwise the text item quoted in `text`. |
| `row` | integer \| null | For `layer` `table`: the row of table `ref` the value was read from (0-based grid row); null otherwise. Absent for `version.cover_*`. |
| `section_id` | string \| null | Section of `ref`; null for a header or footer. |

<!-- fields: shared:tool-soa-table -->
| Field | Type | Meaning |
|---|---|---|
| `table_id` | string | [Table id](#ids). Pass it to `get_table`. |
| `section_id` | string | Section the table belongs to. |
| `pages` | integer[] | PDF pages of the table. |
| `rows` | integer | Rows of the table grid, header rows included. |
| `cols` | integer | Columns of the grid. |
| `caption` | string \| null | The caption, or null. |

#### Example

Lilly_NCT03421379, as REST returns it (trimmed):

```json
{
  "api_version": "1",
  "document": "protocol",
  "out_dir": "<document>",
  "source_json": "protocol.json",
  "source_sha256": "c08198250f21310b",
  "titles": {
    "official_title": {"value": "Protocol I8R-JE-IGBJ A Phase 3 Study of Nasal Glucagon (LY900018) Compared to …",
                       "method": "title page",
                       "source": {"page": 1, "layer": "body", "text": "Protocol I8R-JE-IGBJ A Phase 3 Study of …",
                                  "ref": "#/texts/1", "refs": ["#/texts/1"], "row": null, "section_id": "S0"}},
    "brief_title": null,
    "acronym": null,
    "detected_title": {"value": "Protocol I8R-JE-IGBJ A Phase 3 Study of Nasal Glucagon (LY900018) Compared to …",
                       "method": "title page",
                       "source": {"page": 1, "layer": "body", "text": "Protocol I8R-JE-IGBJ A Phase 3 Study of …",
                                  "ref": "#/texts/1", "refs": ["#/texts/1"], "row": null, "section_id": "S0"}}
  },
  "identifiers": {
    "sponsor_protocol_id": {"value": "I8R-JE-IGBJ", "label": "Sponsor protocol number", "method": "labelled pattern",
                            "source": {"page": 1, "layer": "furniture",
                                       "text": "I8R-JE-IGBJ(a) Clinical Pharmacology Protocol",
                                       "ref": "#/texts/0", "refs": ["#/texts/0"], "row": null, "section_id": null}},
    "nct": {"value": "NCT03421379", "label": "ClinicalTrials.gov (NCT) number", "method": "registry pattern",
            "source": {"page": 1, "layer": "body", "text": "NCT03421379", "ref": "#/texts/2", "refs": ["#/texts/2"],
                       "row": null, "section_id": "S0"}},
    "eudract": null, "eu_ct": null, "ind": null, "who_utn": null
  },
  "other_identifiers_cited": {},
  "version": {
    "label": null,
    "amendment": null,
    "date": {"value": "05-Dec-2017", "method": "labelled pattern",
             "source": {"page": 1, "layer": "body", "text": "Approval Date: 05-Dec-2017", "ref": "#/texts/3",
                        "refs": ["#/texts/3"], "row": null, "section_id": "S0"}},
    "cover_amendment": null,
    "cover_version": null,
    "cover_approval_date": {"value": "05-Dec-2017",
                            "source": {"page": 1, "layer": "body", "text": "Approval Date: 05-Dec-2017",
                                       "refs": ["#/texts/3"], "ref": "#/texts/3", "section_id": "S0"}}
  },
  "sponsor": {
    "name": "Eli Lilly and Company",
    "value": "Eli Lilly and Company",
    "method": "'property of' line",
    "source": {"page": 2, "text": "The information contained in this protocol is confidential … It is the property of Eli Lilly and Company or its subsidiaries …",
               "ref": "#/texts/9", "refs": ["#/texts/9"], "row": null, "layer": "body", "section_id": "S~bfe986b0-1"},
    "provenance": {"section_id": "S~bfe986b0-1", "section_number": null, "title": "Confidential Information",
                   "pages": [2], "refs": ["#/texts/9"]}
  },
  "cover_page": {"approval_date": {"value": "05-Dec-2017", "page": 1, "text": "Approval Date: 05-Dec-2017",
                                   "refs": ["#/texts/3"]}},
  "page_count": 73,
  "section_count": 149,
  "soa_table_count": 1,
  "soa_tables": [{"table_id": "#/tables/3", "section_id": "S~6bf46cb1-1", "pages": [12, 13, 14, 15, 16, 17, 18, 19],
                  "rows": 45, "cols": 10, "caption": null}],
  "roles_found": {"title_page": ["S0", "S~0ba26483-1", "S~bfe986b0-1"], "inclusion": ["S6.1"],
                  "interventions": ["S7", "S9.4"] /* 26 more roles */},
  "reading_order_repairs": [],
  "identity_block": "Title: Protocol I8R-JE-IGBJ A Phase 3 Study of …\nSponsor protocol number: I8R-JE-IGBJ (source p. 1, header/footer: \"I8R-JE-IGBJ(a) Clinical Pharmacology Protocol\")\nClinicalTrials.gov (NCT) number: NCT03421379 (source p. 1: \"NCT03421379\")\nEudraCT number: none found in the document text (body, tables, headers/footers)\n…\nVersion: 05-Dec-2017",
  "provenance": {"section_id": "S0", "section_number": null, "title": "Front matter", "pages": [1],
                 "refs": ["#/texts/1", "#/texts/0", "#/texts/2", "#/texts/3"]},
  "warnings": []
}
```

#### Notes

- Titles, version and sponsor come from heuristics (`method` says which). Verify each value with `quote_check`
  before you store it.
- `source.ref` is the item the value was read from, recorded when the value was extracted: it holds
  `source.text` (for a table row, row `source.row` of that table does). Highlight it directly (above,
  `sponsor_protocol_id` is read from the page-1 running header `#/texts/0`).
- The result is computed once per open document; repeated calls are cheap.

### outline

The section tree in document order, with each section's role and size. Use it to decide what to read and how many
`read_section` parts a section will take.

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `max_level` | integer, >= 0 | all levels | Only sections up to this depth: 1 = top level; 0 = only the front matter `S0`. |
| `with_roles` | boolean | `true` | `false` leaves out `role`, `role_score` and the `roles` map. |

#### Result

<!-- fields: tool:outline -->
| Field | Type | Meaning |
|---|---|---|
| `sections` | object[] | The sections within `max_level`, in document order. `S0` (text before the first heading) comes first when present. |
| `sections[].section_id` | string | [Section id](#ids). |
| `sections[].id` | string | The same as `section_id`. Deprecated: kept for older clients. |
| `sections[].number` | string \| null | The number as printed (`6.1`); null for an unnumbered section. |
| `sections[].title` | string | The heading text, with its number (`6.1. Inclusion Criteria`); `Front matter` for `S0`. |
| `sections[].level` | integer | Depth: 1 for a top-level section, 2 below it, and so on; 0 for `S0`. |
| `sections[].role` | string \| null | ICH M11 role (one of the roles in `find_sections`' enum), or null. Absent when `with_roles` is false. |
| `sections[].role_score` | number | Confidence of the role, 0 to 1, two decimals. Below 0.7 the role is inherited from the parent or weak. Absent when `with_roles` is false. |
| `sections[].pages` | integer[] | `[first, last]` PDF page of the section's own content (heading and text, not its subsections); empty when it has none. |
| `sections[].tokens` | integer | Size of the section with all its subsections, in tokens (sum of the rendered blocks, 2 tokens per block for separators). Compare it with `read_section`'s `max_tokens`. |
| `sections[].own_tokens` | integer | Size of the section's own content, without subsections. |
| `sections[].parent_id` | string \| null | Id of the parent section; null for a top-level section and `S0`. |
| `sections[].parent` | string \| null | The same as `parent_id`. Deprecated: kept for older clients. |
| `sections[].children` | string[] | Ids of the direct subsections, in order. Only those within `max_level`, so empty at the deepest level shown. |
| `sections[].tables` | integer | Tables directly in this section (not in subsections). Absent when 0. |
| `sections[].figures` | integer | Figures directly in this section. Absent when 0. |
| `count` | integer | Entries in `sections`. |
| `total_sections` | integer | All sections of the document, whatever `max_level`. |
| `page_count` | integer | Pages in the PDF. |
| `total_tokens` | integer | Size of the whole document in tokens, whatever `max_level`. |
| `roles` | object (map) | The roles present in the document, in the order of the role list. Absent when `with_roles` is false. |
| `roles.<role>` | string | What a USDM builder finds under that role, such as `EligibilityCriterion category Inclusion`. |
| `provenance` | object (shared: provenance) | `section_id` null, `title` `whole document (section tree)`, empty `pages` and `refs`. |
| `warnings` | string[] | Problems with this result (see `document_info.warnings`). |

#### Example

Lilly_NCT03421379, no arguments (trimmed):

```json
{
  "sections": [
    {"section_id": "S0", "id": "S0", "number": null, "title": "Front matter", "level": 0, "role": "title_page",
     "role_score": 0.5, "pages": [1, 2], "tokens": 147, "own_tokens": 147, "parent_id": null, "parent": null,
     "children": []},
    /* … */
    {"section_id": "S4", "id": "S4", "number": "4", "title": "4. Objectives and Endpoints", "level": 1,
     "role": "objectives_endpoints", "role_score": 1.0, "pages": [23, 23], "tokens": 440, "own_tokens": 440,
     "parent_id": null, "parent": null, "children": [], "tables": 1},
    /* … */
    {"section_id": "S6", "id": "S6", "number": "6", "title": "6. Study Population", "level": 1, "role": "population",
     "role_score": 1.0, "pages": [26, 26], "tokens": 3128, "own_tokens": 143, "parent_id": null, "parent": null,
     "children": ["S6.1", "S6.2", "S6.3", "S6.4"]},
    {"section_id": "S6.1", "id": "S6.1", "number": "6.1", "title": "6.1. Inclusion Criteria", "level": 2,
     "role": "inclusion", "role_score": 1.0, "pages": [26, 28], "tokens": 1170, "own_tokens": 1170,
     "parent_id": "S6", "parent": "S6", "children": []}
    /* 143 more */
  ],
  "count": 149,
  "total_sections": 149,
  "page_count": 73,
  "total_tokens": 33219,
  "roles": {"title_page": "StudyTitle, StudyIdentifier, sponsor Organization, version/date; investigator agreement",
            "inclusion": "EligibilityCriterion category Inclusion" /* 27 more */},
  "provenance": {"section_id": null, "section_number": null, "title": "whole document (section tree)", "pages": [],
                 "refs": []},
  "warnings": []
}
```

#### Notes

- To draw a table of contents, nest `sections` by `parent`; `children` is cut at `max_level`.
- The first `outline` or `find_sections` call on a document renders and counts every section; later calls of any
  tool reuse that work, so the first call is the slow one.

### find_sections

Sections ranked by ICH M11 role and/or a fuzzy title query. A query can also be a section number (`6.2`,
`Section 6.2`). Use it when a role read returns nothing or the wrong thing.

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `role` | string, one of `title_page`, `toc`, `synopsis`, `schema`, `soa`, `introduction`, `objectives_endpoints`, `estimands`, `design`, `population`, `eligibility`, `inclusion`, `exclusion`, `lifestyle`, `screen_failures`, `interventions`, `dose_modification`, `concomitant_therapy`, `discontinuation`, `assessments`, `assessments_efficacy`, `assessments_safety`, `ae_reporting`, `pk_pd_biomarkers`, `statistics`, `sample_size`, `interim_analysis`, `regulatory_ethics`, `abbreviations`, `references`, `amendment_history`, `appendix_other` | none | Only sections with this role. |
| `query` | string | none | Title words (`exclusion criteria`) or a section number (`6.2`). A query that names a role (`study treatments`) also ranks that role's sections. |
| `limit` | integer, 1-50 | 10 | Maximum number of results. |

At least one of `role` and `query` is required.

#### Result

<!-- fields: tool:find_sections -->
| Field | Type | Meaning |
|---|---|---|
| `role` | string \| null | The `role` argument. |
| `query` | string \| null | The `query` argument. |
| `count` | integer | Entries in `sections`. |
| `sections` | object[] | Matching sections, best first. |
| `sections[].section_id` | string | [Section id](#ids). |
| `sections[].number` | string \| null | The number as printed; null for an unnumbered section. |
| `sections[].title` | string | The heading text. |
| `sections[].level` | integer | Depth (1 = top level; 0 for `S0`). |
| `sections[].role` | string \| null | The section's role. |
| `sections[].role_score` | number | Confidence of the role, 0 to 1. |
| `sections[].score` | number | Ranking score, three decimals. Role only: `role_score`, plus 1 for an outermost section of the role (0 to 2). With a query: title similarity, about 0.2 to 1.1, exactly 1.0 for a matching section number; results below 0.2 are left out. |
| `sections[].root_of_role` | boolean \| null | With `role`: whether this is an outermost section of the role (its parent has another role). Null without `role`. |
| `sections[].path` | string | Titles from the top-level section down to this one, joined with ` > `. |
| `sections[].tokens` | integer | Size of the section with its subsections, in tokens (as in `outline`). |
| `sections[].provenance` | object (shared: provenance) | The section, with the pages of its own content and its heading's ref as `refs` (empty for `S0`). |
| `provenance` | object (shared: provenance) | The first result's provenance; all null and empty when nothing matched. |
| `hint` | string | Only when `role` gave no result. When no section has the role and the role has a usual fallback: where its content usually sits, such as `objectives_endpoints` for `estimands`. When sections with the role exist but the `query` matched none of them: how many have the role, and to call again without the query. |

#### Example

Lilly_NCT03421379, `{"role": "inclusion"}`:

```json
{
  "role": "inclusion",
  "query": null,
  "count": 1,
  "sections": [
    {"section_id": "S6.1", "number": "6.1", "title": "6.1. Inclusion Criteria", "level": 2, "role": "inclusion",
     "role_score": 1.0, "score": 2.0, "root_of_role": true, "path": "6. Study Population > 6.1. Inclusion Criteria",
     "tokens": 1170,
     "provenance": {"section_id": "S6.1", "section_number": "6.1", "title": "6.1. Inclusion Criteria",
                    "pages": [26, 27, 28], "refs": ["#/texts/178"]}}
  ],
  "provenance": {"section_id": "S6.1", "section_number": "6.1", "title": "6.1. Inclusion Criteria",
                 "pages": [26, 27, 28], "refs": ["#/texts/178"]}
}
```

#### Notes

- With both `role` and `query`, only sections of that role are ranked, by the query. If the query then matches
  none of them, `hint` says how many sections have the role (for example `no section with role 'inclusion'
  matches the query 'washout'; 1 section(s) have this role: call find_sections with role='inclusion' and no query
  to list them`).

### read_section

The complete Markdown of a section and its subsections, with per-block provenance. `section` can be a section id
(`S6.1`), a number (`6.1`), a role (`inclusion`) or, as a last resort, title words. Use it for every enumeration
(all criteria, all objectives): it is complete, `search` is not. Long content comes in parts: follow `cursor` until
it is null. The Markdown conventions (`{#S6.1}` heading anchors, `<!-- p. 26 -->` page markers, tables, figures,
list markers) are described in [docs/protocol_api.md](protocol_api.md).

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `section` | string | none | Section id, section number, role name, or title words (used when the best title match scores at least 0.6, with a warning). |
| `include_subsections` | boolean | `true` | `false` returns only the section's own content. In role mode, `true` includes the subsections of the same role family (for example inclusion and exclusion under eligibility) and lists the others in `excluded_subsections`. |
| `max_tokens` | integer, >= 200 | 16000 | Size of one part. Content beyond it continues through `cursor`. |
| `cursor` | string | none | The `cursor` of the previous part. It replaces `section` and `include_subsections`. |
| `with_blocks` | boolean | `true` | `false` leaves out `blocks`. |

`section` or `cursor` is required.

#### Result

<!-- fields: tool:read_section -->
| Field | Type | Meaning |
|---|---|---|
| `request` | object | The request this part answers. |
| `request.section` | string | The `section` argument, or the one stored in the cursor. |
| `request.include_subsections` | boolean | Whether subsections are included. |
| `request.max_tokens` | integer | The part size used (16000 when not given). |
| `mode` | string | `section` (an id, number or title) or `role`. |
| `role` | string \| null | In role mode, the role asked for (even when a fallback role was read instead); else null. |
| `section_id` | string | The first section read: the section asked for, or the first outermost section of the role. |
| `matched_sections` | string[] | All sections read, in document order: one in section mode, every outermost section of the role in role mode. |
| `provenance` | object (shared: provenance) | `section_id` and its title, with the pages of this part and the refs of its blocks. It names the section asked for even when this part holds only its subsections; use `blocks[].section_id` for each block's own section. |
| `provenance.section_ids` | string[] | Only when several sections have the role: all of `matched_sections`. |
| `sections` | object[] (shared: tool-section-summary) | The sections with blocks in this part, in order. |
| `markdown` | string | This part's Markdown. A part that starts inside a section begins with that section's heading marked `(continued)`. |
| `tokens` | integer | Tokens of `markdown`, counted exactly. |
| `total_tokens` | integer | Estimated tokens of the whole content, all parts together (blocks plus 2 tokens each). |
| `complete` | boolean | True only when this one part holds the whole content. False on every part of a multi-part read, the last one included. |
| `part` | object | Which blocks of the whole content this part holds. |
| `part.first_block` | integer | Index of its first block (0-based). |
| `part.end_block` | integer | Index after its last block. |
| `part.total_blocks` | integer | Blocks in the whole content. |
| `cursor` | string \| null | Pass it as `cursor` to read the next part; null on the last part. Treat it as opaque: it holds the request and a block index, and is valid for this result only. |
| `next` | object | Only when `cursor` is not null: the call that reads the next part. |
| `next.tool` | string | `read_section`. |
| `next.arguments` | object | The arguments of that call. |
| `next.arguments.cursor` | string | The same value as `cursor`. |
| `next.arguments.max_tokens` | integer | The same part size. |
| `next.arguments.with_blocks` | boolean | The same `with_blocks` as this call. |
| `excluded_subsections` | object[] (shared: tool-section-summary) | Role mode only: subsections of other roles that were left out, so nothing is hidden. Read them by `section_id`. Absent when there are none. |
| `blocks` | object[] | One entry per block of this part, in order. Absent when `with_blocks` is false. |
| `blocks[].block` | integer | Index of the block in the whole content (continues across parts). |
| `blocks[].kind` | string | `heading`, `text`, `list` (a whole list), `table`, `figure`, `label` (a minor heading kept inline), `footnote`, `caption`, `formula` or `toc_table` (a table of contents, replaced by a one-line note). |
| `blocks[].section_id` | string | The section the block belongs to. |
| `blocks[].pages` | integer[] | PDF pages the block spans; empty when unknown. Show them as page chips. |
| `blocks[].ref` | string \| null | Item ref of the block: the text, list group, table or figure. Null for a generated heading (that of `S0`, or of a section without a heading item). Resolve it in `protocol.json` to draw a box (see [Pages and boxes](#pages-and-boxes)). |
| `blocks[].chars` | integer[] | `[start, end)` character offsets of the block's text in `markdown` (Python string indices, that is Unicode code points). Use them to map a selection in the Markdown back to its block. |
| `blocks[].item_refs` | string[] | Lists only: refs of every list item, nested items included, in order. Use them to point at one criterion. |
| `blocks[].items` | integer | Lists only: number of list items, nested items included. |
| `blocks[].table_id` | string | Tables and `toc_table` only: the table id. Pass it to `get_table`. |
| `blocks[].rows` | integer | Tables only: rows of the grid. |
| `blocks[].cols` | integer | Tables only: columns of the grid. |
| `blocks[].chain` | string[] | Tables only, when the table is one fragment of a page-split table: the table ids of all fragments. `get_table` returns them together. |
| `blocks[].glossary` | boolean | Tables only: `true` when the table is the abbreviation list that `abbreviation` reads. Absent otherwise. |
| `blocks[].figure_id` | string | Figures only: the figure's item ref (`#/pictures/N`). |
| `blocks[].ai_generated` | boolean | Figures only: whether the block contains an AI-generated description or Mermaid diagram (labelled as such in `markdown`). That text is not document text. |
| `blocks[].image` | string | Figures only: path of the figure's image file, relative to the result folder (`images/image_000000_<hash>.png` in service results). Over REST, fetch it with `GET /documents/{doc_id}/files/{name}`, this path being the name. Absent when the image is not stored as a file. |
| `blocks[].moved_from` | string | Only for results converted before reading-order repair: the section the converter had put this block in. |
| `warnings` | string[] | Notes about this read: several sections with the role, a fallback role, a title match, a block larger than `max_tokens` returned whole, reading-order repairs. |

<!-- fields: shared:tool-section-summary -->
| Field | Type | Meaning |
|---|---|---|
| `section_id` | string | [Section id](#ids). |
| `number` | string \| null | The number as printed; null for an unnumbered section. |
| `title` | string | The heading text. |
| `level` | integer | Depth (1 = top level; 0 for `S0`). |
| `role` | string \| null | ICH M11 role, or null. |
| `pages` | integer[] | `[first, last]` PDF page of the section's own content; empty when it has none. |

#### Example

Lilly_NCT03421379, `{"section": "S9", "max_tokens": 200}`, the first of several parts (trimmed):

```json
{
  "request": {"section": "S9", "include_subsections": true, "max_tokens": 200},
  "mode": "section",
  "role": null,
  "section_id": "S9",
  "matched_sections": ["S9"],
  "provenance": {"section_id": "S9", "section_number": "9", "title": "9. Study Assessments and Procedures",
                 "pages": [36], "refs": ["#/texts/336", "#/texts/337" /* 3 more */]},
  "sections": [{"section_id": "S9", "number": "9", "title": "9. Study Assessments and Procedures", "level": 1,
                "role": "assessments", "pages": [36, 36]}],
  "markdown": "<!-- p. 36 -->\n\n# 9. Study Assessments and Procedures {#S9}\n\nSection 2 lists the Schedule of Activities, detailing the study procedures and their timing.\n\n…",
  "tokens": 135,
  "total_tokens": 6979,
  "complete": false,
  "part": {"first_block": 0, "end_block": 5, "total_blocks": 135},
  "cursor": "S9|1|5",
  "next": {"tool": "read_section", "arguments": {"cursor": "S9|1|5", "max_tokens": 200, "with_blocks": true}},
  "blocks": [
    {"block": 0, "kind": "heading", "section_id": "S9", "pages": [36], "ref": "#/texts/336", "chars": [16, 59]},
    {"block": 1, "kind": "text", "section_id": "S9", "pages": [36], "ref": "#/texts/337", "chars": [61, 153]}
    /* 3 more */
  ],
  "warnings": []
}
```

A list block from `{"section": "inclusion"}` of the same document:

```json
{"block": 2, "kind": "list", "section_id": "S6.1", "pages": [26], "ref": "#/groups/6", "chars": [182, 368],
 "item_refs": ["#/texts/180", "#/texts/181"], "items": 2}
```

#### Notes

- A part never ends on a bare heading, and a block is never cut. A heading and its first block, or a single block
  (a big table), larger than `max_tokens` is returned whole, with a warning.
- Through the tools, `max_tokens` defaults to 16,000. Parts are cut by block sizes plus 8 tokens of overhead per
  block, so `tokens` is usually below `max_tokens` (135 for 200 above). `next.arguments` carries `max_tokens` and
  `with_blocks` along, so calling it as given continues with the same settings.
- A role with no section of its own falls back to where its content usually sits (`estimands` to
  `objectives_endpoints`, `inclusion` to `eligibility`, ...), with a warning.

### get_table

One whole table, chosen by id or by caption and heading words: Markdown, the cell grid, merged cells, footnotes,
or one self-describing line per row. A table split across pages comes back as one table with all its fragments.

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `table` | string | required | A table id (`#/tables/5`), its number (`5`), or caption/heading words (`study treatments`). Words are matched against each table's caption, its section headings and its first two rows; tables of contents are skipped. |
| `format` | string: `all`, `markdown`, `grid` or `rows` | `all` | `all`: `markdown`, `grid` and `merged_cells`. `markdown`: only `markdown`. `grid`: only `grid`. `rows`: `columns` and `row_lines` instead. |

#### Result

<!-- fields: tool:get_table -->
| Field | Type | Meaning |
|---|---|---|
| `table_id` | string | Id of the (first fragment of the) table. |
| `table_ids` | string[] | Ids of all fragments, in page order; one entry for a table on one page. |
| `caption` | string \| null | Caption of the first fragment. |
| `section` | string \| null | Titles of the table's section path, joined with ` > `. |
| `role` | string \| null | Role of the table's section. |
| `glossary` | boolean | `true` when this is the abbreviation list that `abbreviation` reads. |
| `provenance` | object (shared: provenance) | The table's section, the pages of all fragments, and the fragment ids as `refs`. |
| `rows` | integer | Rows of all fragments together, header rows included. |
| `cols` | integer | The largest column count of the fragments. |
| `footnotes` | string[] | Texts of the footnotes attached to the fragments. |
| `markdown` | string | Formats `all` and `markdown`: the whole table as GFM Markdown (caption first, then every row, then the notes). Fragments are separated by a line saying that the next page lays the columns out differently. |
| `fragments` | object[] | One entry per fragment. |
| `fragments[].table_id` | string | The fragment's table id. |
| `fragments[].pages` | integer[] | Its PDF pages. |
| `fragments[].rows` | integer | Rows of its grid. |
| `fragments[].cols` | integer | Columns of its grid. |
| `fragments[].header_rows` | integer | Number of leading header rows (from the converter's header flags, else 1 when the first row looks like a header, else 0). |
| `fragments[].caption` | string \| null | Its caption. |
| `fragments[].footnotes` | string[] | Its footnotes. |
| `fragments[].grid` | string[][] | Formats `all` and `grid`: the cell texts, row by row, 0-based. A merged cell's text is repeated in every position it covers. |
| `fragments[].merged_cells` | object[] | Format `all`: the cells spanning more than one row or column. |
| `fragments[].merged_cells[].row` | integer | First row (0-based). |
| `fragments[].merged_cells[].col` | integer | First column (0-based). |
| `fragments[].merged_cells[].row_span` | integer | Rows covered. |
| `fragments[].merged_cells[].col_span` | integer | Columns covered. |
| `fragments[].merged_cells[].text` | string | The cell text. |
| `fragments[].merged_cells[].header` | boolean | Whether the converter flagged it as a column header. |
| `fragments[].columns` | string[] | Format `rows`: the entries of the column line (column and group names; long names get a short letter alias, defined here). |
| `fragments[].row_lines` | string[] | Format `rows`: one line per body row, `Row label: Column = value; ...`, the form the retriever indexes. Section rows read `Section: <label>`. |
| `matched_by` | string | Only when `table` was words: `caption/heading query`. |
| `candidates` | object[] | Only when `table` was words: the five best-scoring tables, best first. The first is the one returned. |
| `candidates[].table_id` | string | Table id. |
| `candidates[].score` | number | Match score, 0 to 1, three decimals. Words with a best score below 0.2 are an error. |
| `candidates[].caption` | string \| null | Caption. |
| `candidates[].section_id` | string | Section of the table. |
| `candidates[].pages` | integer[] | PDF pages. |
| `candidates[].rows` | integer | Rows. |
| `candidates[].cols` | integer | Columns. |

#### Example

Lilly_NCT03421379, `{"table": "study treatments"}` (trimmed):

```json
{
  "table_id": "#/tables/5",
  "table_ids": ["#/tables/5"],
  "caption": "Table IGBJ.2. Study Treatments",
  "section": "7. Treatment > 7.1. Treatment Administered",
  "role": "interventions",
  "glossary": false,
  "provenance": {"section_id": "S7.1", "section_number": "7.1", "title": "7.1. Treatment Administered", "pages": [32],
                 "refs": ["#/tables/5"]},
  "rows": 5,
  "cols": 3,
  "footnotes": [],
  "markdown": "Table IGBJ.2. Study Treatments\n\n| Treatment Name | LY900018 | GlucaGen |\n| - | - | - |\n| Dosage Formulation | Dry powder | Dry powder |\n| Dosage Levels | 3 mg glucagon | 1 mg glucagon |\n…",
  "fragments": [
    {"table_id": "#/tables/5", "pages": [32], "rows": 5, "cols": 3, "header_rows": 1,
     "caption": "Table IGBJ.2. Study Treatments", "footnotes": [],
     "grid": [["Treatment Name", "LY900018", "GlucaGen"], ["Dosage Formulation", "Dry powder", "Dry powder"] /* 3 more */],
     "merged_cells": []}
  ],
  "matched_by": "caption/heading query",
  "candidates": [
    {"table_id": "#/tables/5", "score": 0.917, "caption": "Table IGBJ.2. Study Treatments", "section_id": "S7.1",
     "pages": [32], "rows": 5, "cols": 3},
    {"table_id": "#/tables/3", "score": 0.71, "caption": null, "section_id": "S~6bf46cb1-1",
     "pages": [12, 13, 14, 15, 16, 17, 18, 19], "rows": 45, "cols": 10}
    /* 3 more */
  ]
}
```

#### Notes

- Words always return the best table when it scores at least 0.2: check `candidates` when the match matters.
- `format: "rows"` is the most compact form of a wide table (SoA tables) for an LLM; `grid` is the one to render.

### get_soa

The Schedule of Activities as data: visits (label, header path, window, timing), activities, the activity x visit
cells that are scheduled, footnotes, and a Markdown view. Optional `visit` and `activity` filters add `lookup`.

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `visit` | string | none | A visit to look up, case and abbreviation tolerant (`Day -1`, `C1D8`, `EOT`, `end of treatment`). Per SoA the best-matching visits are kept: the label, then one header part or the timing, then the whole header path, then the numbers. |
| `activity` | string | none | An activity to look up (`ecg`, `informed consent`): words in order, prefixes allowed, or initials; when no activity label matches, the activities' section labels. |
| `with_markdown` | boolean | `true` | `false` leaves out `soas[].markdown`. |

#### Result

<!-- fields: tool:get_soa -->
| Field | Type | Meaning |
|---|---|---|
| `available` | boolean | `false` only when the SoA extractor cannot run; the result then has `reason`, `soa_sections`, `soa_tables`, `hint` and `provenance` only. |
| `reason` | string | When `available` is false: why. |
| `soa_sections` | object[] (shared: tool-section-summary) | When `available` is false: the outermost sections with role `soa`. |
| `soa_tables` | object[] (shared: tool-soa-table) | When `available` is false: the tables in those sections. |
| `hint` | string | When `available` is false: read the SoA with `read_section("soa")` and `get_table(table_id, format="rows")` instead. |
| `count` | integer | Entries in `soas`. |
| `soas` | object[] | One entry per Schedule of Activities, in document order. Tables that continue each other (page splits, more activities, more visits) are combined into one. Always the whole SoA, also when filtering. |
| `soas[].table_id` | string | Id of its first table. |
| `soas[].table_ids` | string[] | Ids of all tables combined into it. |
| `soas[].caption` | string \| null | The first caption among its tables. |
| `soas[].section_id` | string \| null | Section of its first table. |
| `soas[].pages` | integer[] | PDF pages of its tables. |
| `soas[].visits` | object[] | The visit columns, left to right. |
| `soas[].visits[].id` | string | `V1`, `V2`, ... in column order; valid within this SoA only. |
| `soas[].visits[].label` | string | The lowest header cell of the column, markers removed (`Day -1`); may be empty for an unnamed column (then warned). |
| `soas[].visits[].header_path` | string[] | Header cells from the top row down to `label` (`["Period 1", "Day -1"]`). |
| `soas[].visits[].window` | string \| null | Allowed window as printed (`±2 days`, `+7`), from a window row or the header; null when none. |
| `soas[].visits[].timing` | string \| null | The timing expressions of the header path (`Period 1, Day -1`), bare numbers taking the stub's unit; null when none is recognised. |
| `soas[].visits[].column` | integer | Grid column (0-based) of the visit in its table. |
| `soas[].visits[].table_id` | string | Table the visit first appears in. |
| `soas[].visits[].epoch_hint` | string \| null | The outermost group header spanning the column (`Period 1`, `Screening`): a hint for the study epoch. |
| `soas[].visits[].footnote_markers` | string[] | Footnote markers on the visit header, keys of `footnotes`. |
| `soas[].activities` | object[] | The activity rows, top to bottom. Section rows are not activities. |
| `soas[].activities[].id` | string | `A1`, `A2`, ... in row order; valid within this SoA only. |
| `soas[].activities[].label` | string | The row label, markers removed. |
| `soas[].activities[].section` | string \| null | The section row above it (`Clinical Assessments`), or null. |
| `soas[].activities[].notes` | string \| null | Text of a notes or comments column, or null. |
| `soas[].activities[].footnote_markers` | string[] | Footnote markers on the label and on its section row. |
| `soas[].activities[].row` | integer | Grid row (0-based) of the activity in its table. |
| `soas[].activities[].table_id` | string | Table the row first appears in. |
| `soas[].cells` | object[] | The scheduled cells only (no blanks or dashes), ordered by activity, then visit. A cell spanning several visits is listed for each. |
| `soas[].cells[].activity` | string | Activity id (`A1`). |
| `soas[].cells[].visit` | string | Visit id (`V1`). |
| `soas[].cells[].mark` | boolean | `true` when the cell holds an X-like mark (also a line or arrow drawn across visits). |
| `soas[].cells[].text` | string | The cell text as printed. |
| `soas[].cells[].qualifier` | string \| null | Text other than the mark, such as `if necessary` or `Cycle 2 Day 1 only`; null when none. |
| `soas[].cells[].footnote_markers` | string[] | Footnote markers in the cell. |
| `soas[].footnotes` | object (map) | The SoA's footnotes, keyed by marker. |
| `soas[].footnotes.<marker>` | string | The footnote text of one marker (`a`, `1`, `*`). |
| `soas[].header_notes` | string[] | Notes written inside header cells. |
| `soas[].section_rows` | object[] | Rows that label a group of activities without spanning the table (see `activities[].section`). |
| `soas[].section_rows[].table_id` | string | Table of the row. |
| `soas[].section_rows[].row` | integer | Grid row (0-based). |
| `soas[].warnings` | string[] | Extraction problems: unnamed visit columns, duplicate visit headers, footnote markers without a footnote, or `extraction failed: <type>` (then the lists are empty). |
| `soas[].provenance` | object (shared: provenance) | The SoA's section, pages, and table ids as `refs`. |
| `soas[].markdown` | string | A compact Markdown view: one column per visit, period and window rows, section rows in bold, the notes column, then the footnotes. Absent when `with_markdown` is false. |
| `soas[].markdown_error` | string | Only if rendering `markdown` failed: the exception. |
| `provenance` | object (shared: provenance) | The first SoA's provenance; without one, the first `soa` section with its tables. |
| `warnings` | string[] | Only when no SoA was found. |
| `lookup` | object[] | Only with `visit` or `activity`: the matching scheduled cells, each self-contained. Empty when nothing matches. |
| `lookup[].table_id` | string | The SoA's first table id. |
| `lookup[].visit_id` | string | Visit id. |
| `lookup[].visit` | string | Visit label. |
| `lookup[].visit_path` | string[] | Visit header path. |
| `lookup[].window` | string \| null | Visit window. |
| `lookup[].timing` | string \| null | Visit timing. |
| `lookup[].activity_id` | string | Activity id. |
| `lookup[].activity` | string | Activity label. |
| `lookup[].section` | string \| null | Activity section. |
| `lookup[].notes` | string \| null | Activity notes. |
| `lookup[].mark` | boolean | As in `cells`. |
| `lookup[].text` | string | As in `cells`. |
| `lookup[].qualifier` | string \| null | As in `cells`. |
| `lookup[].footnote_markers` | string[] | Markers of the cell and of the activity. |
| `lookup[].footnotes` | object (map) | The footnotes of those markers. |
| `lookup[].footnotes.<marker>` | string | Footnote text. |
| `lookup_error` | string | Only if the lookup failed: the exception. |

#### Example

Lilly_NCT03421379, `{"activity": "Informed Consent"}` (trimmed):

```json
{
  "available": true,
  "count": 1,
  "soas": [
    {
      "table_id": "#/tables/3",
      "table_ids": ["#/tables/3"],
      "caption": null,
      "section_id": "S~6bf46cb1-1",
      "pages": [12, 13, 14, 15, 16, 17, 18, 19],
      "visits": [
        {"id": "V1", "label": "Days -28 to -2", "header_path": ["Screening", "Days -28 to -2"], "window": null,
         "timing": "Days -28 to -2", "column": 1, "table_id": "#/tables/3", "epoch_hint": null, "footnote_markers": []},
        {"id": "V2", "label": "Day -1", "header_path": ["Period 1", "Day -1"], "window": null,
         "timing": "Period 1, Day -1", "column": 2, "table_id": "#/tables/3", "epoch_hint": "Period 1",
         "footnote_markers": []}
        /* 6 more */
      ],
      "activities": [
        {"id": "A1", "label": "Informed Consent", "section": "Clinical Assessments", "notes": null,
         "footnote_markers": [], "row": 3, "table_id": "#/tables/3"}
        /* 32 more */
      ],
      "cells": [
        {"activity": "A1", "visit": "V1", "mark": true, "text": "X", "qualifier": null, "footnote_markers": []}
        /* 79 more */
      ],
      "footnotes": {"a": "Samples for immunogenicity, should be collected every 12 weeks (84±7 days) until …"},
      "header_notes": [],
      "section_rows": [{"table_id": "#/tables/3", "row": 2}, {"table_id": "#/tables/3", "row": 25} /* 1 more */],
      "warnings": [],
      "provenance": {"section_id": "S~6bf46cb1-1", "section_number": null, "title": "Study Schedule Protocol I8R-JE-IGBJ",
                     "pages": [12, 13, 14, 15, 16, 17, 18, 19], "refs": ["#/tables/3"]},
      "markdown": "**Schedule of Activities** (pages 12, 13, 14, 15, 16, 17, 18, 19)\n| Activity | Days -28 to -2 | Day -1 | Day 1 | …"
    }
  ],
  "provenance": {"section_id": "S~6bf46cb1-1", "section_number": null, "title": "Study Schedule Protocol I8R-JE-IGBJ",
                 "pages": [12, 13, 14, 15, 16, 17, 18, 19], "refs": ["#/tables/3"]},
  "lookup": [
    {"table_id": "#/tables/3", "visit_id": "V1", "visit": "Days -28 to -2", "visit_path": ["Screening", "Days -28 to -2"],
     "window": null, "timing": "Days -28 to -2", "activity_id": "A1", "activity": "Informed Consent",
     "section": "Clinical Assessments", "notes": null, "mark": true, "text": "X", "qualifier": null,
     "footnote_markers": [], "footnotes": {}}
  ]
}
```

#### Notes

- The structure is inferred by `soa.py` from the table grid: check visit names, windows and timings against
  `read_section("soa")` or `get_table(table_id, format="rows")` before you rely on them.
- The filters do not shrink `soas`: every call returns the whole SoA. Pass `with_markdown: false` to save space.
- To render a grid: columns from `visits`, rows from `activities` (grouped by `section`), and each cell from
  `cells` by `(activity, visit)`; footnote markers resolve through `footnotes`.

### search

Retrieval for facts scattered through the body (a washout period, a dose rule in an appendix). Returns the context an
answering model would see: the identity block, then ranked blocks (whole sections or parts, tables, passages), with
the glossary definitions of the abbreviations used. It is not complete: for lists, use `read_section`.

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | required | A natural-language question. |
| `k` | integer, 1-20 | 8 | Retrieved passages (chunks) to build the context from. |
| `rendered` | boolean | false | Also return `context`: the context string exactly as an answering model sees it. |

#### Result

<!-- fields: tool:search -->
| Field | Type | Meaning |
|---|---|---|
| `query` | string | The question. |
| `expanded_query` | string | The question with the glossary definitions of the abbreviations it uses appended in parentheses; the same as `query` when it uses none. This is what was embedded and reranked. |
| `k` | integer | Passages retrieved. |
| `budget` | integer | Token budget of the context: 12000. |
| `identity` | object \| null | The document identity block (title, ids, version; see `document_info.identity_block`), first in the context; null when the context has none. Same keys as `blocks[]`. |
| `identity.n` | integer | Its position in the context: 1. |
| `identity.kind` | string | `document`. |
| `identity.label` | string | `Document identity (title page, headers/footers)`. |
| `identity.section` | string | `Document identity`. |
| `identity.pages` | integer[] | Pages of the quoted sources. |
| `identity.text` | string | The identity text. |
| `identity.tokens` | integer | Its tokens. |
| `identity.note` | string | Empty. |
| `identity.chunk_ids` | string[] | Empty. |
| `identity.table_ids` | string[] | Empty. |
| `identity.parent_id` | string \| null | Always null for the identity block. |
| `identity.provenance` | object (shared: provenance) | The items the quoted source lines were read from, one ref per identifier line with a source, in the block's order (the same refs as `document_info`'s identifier sources); the section of the first ref that has one. |
| `identity.provenance.section_ids` | string[] | Only when those lines lie in several sections: all of them. |
| `blocks` | object[] | The context blocks after the identity block, in the order the model sees them. |
| `blocks[].n` | integer | Position in the context (the identity block is 1). |
| `blocks[].kind` | string | What the block holds: `section_full` (a section with its subsections), `parent` (a section), `merged` (adjacent sections), `window` (a part of a section), `table_whole`, `table_window` (retrieved rows ±1), `neighbours` (a passage ±1 passage), `child` (the passage only), `residual` (a passage without sentences already shown). |
| `blocks[].label` | string | Readable form of `kind`, as written in the rendered context (`Section (complete)`). |
| `blocks[].section` | string | Titles of the block's section path, joined with ` > `. |
| `blocks[].pages` | integer[] | PDF pages of the block. |
| `blocks[].text` | string | The block's text (Markdown). |
| `blocks[].tokens` | integer | Its tokens. |
| `blocks[].note` | string | Which retrieved passages the block holds (`retrieved: «…»`); empty when not applicable. |
| `blocks[].chunk_ids` | string[] | [Chunk ids](#ids) of the retrieved passages in the block. |
| `blocks[].table_ids` | string[] | Table ids in the block. |
| `blocks[].parent_id` | string \| null | Id of the parent (structural unit) the block comes from, or null. |
| `blocks[].provenance` | object (shared: provenance) | The block's first section, its pages, and the item refs of its content. |
| `blocks[].provenance.section_ids` | string[] | Only when the block spans several sections: all of them, in order. |
| `hits` | object[] | The retrieved passages, best first (at most `k`). |
| `hits[].rank` | integer | Rank, 1 = best. |
| `hits[].chunk_id` | string | Chunk id; the record is in `chunks/chunks.jsonl`. |
| `hits[].score` | number | Reranker relevance, four decimals. |
| `hits[].dense_rank` | integer | Rank of the chunk by embedding similarity among all chunks (1 = best). |
| `hits[].bm25_rank` | integer | Rank by BM25 keyword score among all chunks. |
| `hits[].section_id` | string \| null | Section of the chunk as the chunker recorded it. |
| `hits[].pages` | integer[] | Pages of the chunk. |
| `definitions` | object[] | Glossary definitions of abbreviations that occur in the context, by abbreviation. |
| `definitions[].abbreviation` | string | The abbreviation. |
| `definitions[].definition` | string | Its definition. |
| `definitions[].inferred_from_text` | boolean | `true` when the definition comes from running text ("long form (SF)") rather than the abbreviation list. |
| `tokens` | integer | Tokens of the whole context, definitions included. |
| `expanded_sections` | string[] | Section ids included whole because the question asks for a list and the section fits the budget. |
| `dropped` | string[] | Retrieved passages left out to stay within the budget, as `<parent id> (<chunk ids>)`. |
| `near_duplicates` | string[] | Retrieved passages left out, or reduced to their new sentences, because the context already holds their text (typically a synopsis copy). |
| `degraded` | string[] | Parents shown in a smaller form than the whole section because the whole one did not fit, as `<parent id>: <form>`. |
| `provenance` | object (shared: provenance) | The first block's provenance (the identity block's when there are no other blocks). |
| `provenance.section_ids` | string[] | As in `blocks[].provenance.section_ids`. |
| `context` | string | Only with `rendered: true`: the whole context as one string, exactly as an answering model sees it. |

#### Example

Lilly_NCT03421379, `{"query": "What is the planned number of participants?", "k": 1}` (trimmed). Produced offline
with stand-in embedding and reranking functions: the shape is real, but the chosen blocks and the `score` and
`dense_rank` values differ with the real models.

```json
{
  "query": "What is the planned number of participants?",
  "expanded_query": "What is the planned number of participants?",
  "k": 1,
  "budget": 12000,
  "identity": {
    "n": 1, "kind": "document", "label": "Document identity (title page, headers/footers)",
    "section": "Document identity", "pages": [1],
    "text": "Title: Protocol I8R-JE-IGBJ A Phase 3 Study of Nasal Glucagon (LY900018) …",
    "tokens": 232, "note": "", "chunk_ids": [], "table_ids": [], "parent_id": null,
    "provenance": {"section_id": "S0", "section_number": null, "title": "Front matter", "pages": [1],
                   "refs": ["#/texts/0", "#/texts/2"]}
  },
  "blocks": [
    {"n": 2, "kind": "parent", "label": "Section (complete)", "section": "5. Study Design", "pages": [24, 25],
     "text": "# 5. Study Design\n\n## 5.1. Overall Design\n\nThis is a Phase 3, multicenter, randomized, open-label, …",
     "tokens": 1389, "note": "retrieved: «Seventy five patients may be enrolled in order to have at least…»",
     "chunk_ids": ["doc-20d4b28b4be1-0029"], "table_ids": [], "parent_id": "doc-20d4b28b4be1-P008",
     "provenance": {"section_id": "S5", "section_number": "5", "title": "5. Study Design", "pages": [24, 25],
                    "refs": ["#/texts/150", "#/texts/144" /* 15 more */], "section_ids": ["S5", "S5.1" /* 4 more */]}}
  ],
  "hits": [{"rank": 1, "chunk_id": "doc-20d4b28b4be1-0029", "score": 0.7143, "dense_rank": 150, "bm25_rank": 1,
            "section_id": "S5.2", "pages": [25]}],
  "definitions": [{"abbreviation": "AE", "definition": "adverse event: Any untoward medical occurrence in …",
                   "inferred_from_text": false} /* 14 more */],
  "tokens": 1921,
  "expanded_sections": [],
  "dropped": [],
  "near_duplicates": [],
  "degraded": [],
  "provenance": {"section_id": "S5", "section_number": "5", "title": "5. Study Design", "pages": [24, 25],
                 "refs": ["#/texts/150", "#/texts/144" /* 15 more */], "section_ids": ["S5", "S5.1" /* 4 more */]}
}
```

#### Notes

- **Network and cost:** each call embeds the question (qwen3-embedding-8b) and reranks up to 40 candidate passages
  (the top 20 by embedding and by BM25; qwen3-reranker-8b) through OpenRouter, with zero data retention. The question
  and the candidate texts leave the service. Both results are cached per document, so a repeated question is free and
  works offline.
- With `OPENROUTER_OFFLINE=1`, an uncached question fails (503 `offline` over REST). A result without embeddings
  fails with 409 `search_unavailable`; the other tools still work.
- `identity.provenance.refs` are the items the identifier lines were read from (above: the page-1 running header
  `#/texts/0` and the NCT line `#/texts/2`). `identity.pages` are the pages of the sources themselves.
- Python callers can get the raw retrieval objects from `ProtocolDocument.search_context(query)`. `rendered: true`
  adds the context string as an answering model sees it (`context`).

### abbreviation

Glossary lookup in both directions: an abbreviation (`AE`) gives its definition, a long form (`adverse event`) gives
the abbreviations defined with it. Entries come from the protocol's abbreviation list, or from inline
"long form (SF)" definitions in the text.

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `term` | string | required | An abbreviation or a long form. Abbreviations match exactly, then ignoring case, then without a plural `s` (`AEs`). |

#### Result

<!-- fields: tool:abbreviation -->
| Field | Type | Meaning |
|---|---|---|
| `term` | string | The term, trimmed. |
| `found` | boolean | Whether anything matched. |
| `matches` | object[] | The abbreviation itself first (when `term` is one), then up to 10 entries whose definition matches `term`, best first. |
| `matches[].abbreviation` | string | The abbreviation as defined. |
| `matches[].definition` | string | Its definition. |
| `matches[].match` | string | `abbreviation` (the term is the abbreviation) or `definition` (the term matches the definition). |
| `matches[].score` | number | 1.0 for an abbreviation or an identical definition; 0.85 when one contains the other; otherwise 0.8 × word overlap. Definition matches below 0.6 are left out. |
| `matches[].source` | string | Where the definition comes from: `table` (an abbreviation table), `text_list` (a list of "AE: adverse event" lines) or `inline` (a "long form (SF)" in running text). |
| `matches[].note` | string \| null | The third column of a three-column abbreviation table (a description), or null. |
| `matches[].alternatives` | object[] | Other definitions of the same abbreviation seen in the document (an inline definition that differs from the list). Empty when none. |
| `matches[].alternatives[].definition` | string | The other definition. |
| `matches[].alternatives[].source` | string | `table`, `text_list` or `inline`. |
| `matches[].alternatives[].page` | integer \| null | Its PDF page. |
| `matches[].alternatives[].ref` | string \| null | Item ref of its table or text. |
| `matches[].alternatives[].count` | integer | How often it was seen. |
| `matches[].provenance` | object (shared: provenance) | Where the entry is defined: the table or text item, its page and section. |
| `provenance` | object (shared: provenance) | The first match's provenance; all null and empty when nothing matched. |
| `did_you_mean` | string[] | Only when nothing matched: up to 5 similar abbreviations. |
| `hint` | string | Only when nothing matched: use `quote_check(term)` to see whether and where the term occurs. |

#### Example

Lilly_NCT03421379, `{"term": "AE"}` (trimmed):

```json
{
  "term": "AE",
  "found": true,
  "matches": [
    {"abbreviation": "AE",
     "definition": "adverse event: Any untoward medical occurrence in a patient or clinical investigation patient administered a pharmaceutical product …",
     "match": "abbreviation", "score": 1.0, "source": "table", "note": null,
     "alternatives": [{"definition": "adverse events", "source": "inline", "page": 22, "ref": "#/texts/131", "count": 1}],
     "provenance": {"section_id": "S~fc349494-1", "section_number": null,
                    "title": "Appendix 1. Abbreviations and Definitions", "pages": [54], "refs": ["#/tables/7"]}}
  ],
  "provenance": {"section_id": "S~fc349494-1", "section_number": null, "title": "Appendix 1. Abbreviations and Definitions",
                 "pages": [54], "refs": ["#/tables/7"]}
}
```

#### Notes

- A reverse lookup (long form) can return several abbreviations with similar definitions (`AUECabove`,
  `AUECwithin`, ...): use `score` and `match`.

### quote_check

Whether a quote occurs verbatim in the document, how exactly it matched, and every location with its provenance.
Use it to verify every value you extract, and keep the returned provenance as the value's evidence. The matching
tiers (`exact`, `normalized`, `compact`) and the elision rule are described in
[docs/protocol_api.md](protocol_api.md).

#### Arguments

| Name | Type | Default | Meaning |
|---|---|---|---|
| `quote` | string | required | The text to find, at least 2 letters or digits. `...` or `…` elides text: the parts must occur in order, each starting within 2,000 characters after the end of the previous one. Markdown markers copied from `read_section` (`{#S6.1}`, `<!-- p. 26 -->`, leading `#`, table pipes) are ignored. |
| `section_id` | string | none | Count only matches in this section and its subsections; others are listed under `found_elsewhere`. |

#### Result

<!-- fields: tool:quote_check -->
| Field | Type | Meaning |
|---|---|---|
| `quote` | string | The quote as given. |
| `section_id` | string \| null | The `section_id` argument. |
| `found` | boolean | Whether the quote occurs in document text (body, tables, figure OCR text, headers and footers), inside `section_id` when given. A match in AI-generated figure text does not count. |
| `match` | string \| null | The tier that matched: `exact` (whitespace ignored), `normalized` (case, Unicode forms, dashes, quotes, `^` markers ignored), `compact` (only letters, digits and `≥≤<>=%±` compared; quotes of at least 12 such characters), or `elided` (an elided quote whose parts occur together, see `count`); null when not found. |
| `count` | integer | Every match counted (inside `section_id` when given). For an elided quote: the places where all parts occur in order, each part starting within 2,000 characters after the end of the previous one, no two places sharing a match. |
| `count_by_layer` | object | `count` split by layer: `{"body": n, "furniture": m}`, both keys always present. |
| `count_by_layer.body` | integer | Matches in page content, tables and figure OCR text. |
| `count_by_layer.furniture` | integer | Matches in running headers and footers. |
| `locations` | object[] (shared: tool-quote-location) | Up to 10 matches in document order, body first, then headers and footers; when both layers match, header/footer matches get at least 2 of the listed places (all of them if there are fewer), taking the last body places. For an elided quote: one entry per place, spanning from the first part to the last. |
| `more_locations` | integer | Only when `count` exceeds the matches listed: how many are not listed. |
| `found_elsewhere` | object[] (shared: tool-quote-location) | Only with `section_id`: up to 10 matches outside that section, chosen as `locations` are. `found` stays false when all matches are elsewhere. |
| `found_in_ai_text` | object[] (shared: tool-quote-location) | Only when the quote matches an AI-generated figure description or Mermaid diagram: up to 3 such matches (`layer` `ai_generated`). Cite the figure's OCR text or the body instead. |
| `warning` | string | Present with `found_in_ai_text`: that text is not document text. |
| `closest` | object | Only when not found: the passage most similar to the quote (for an elided quote, to its parts joined), when at least 60% of the quote's words (3 or more) occur in a window of body text of the same length (inside `section_id` when given). Use it to correct the quote, then check again. |
| `closest.similarity` | number | Share of the quote's words found in the passage, 0.6 to 1, three decimals. |
| `closest.text` | string | The text items of that window, at most 900 characters (then `…`). |
| `closest.provenance` | object (shared: provenance) | Where the passage is. |
| `parts` | object[] | Elided quotes only: one entry per part, in order. |
| `parts[].part` | string | The part's text. |
| `parts[].found` | boolean | Whether it occurs (inside `section_id` when given). |
| `parts[].match` | string \| null | Its tier, or null. |
| `parts[].locations` | object[] (shared: tool-quote-location) | Its first 3 matches. |
| `provenance` | object (shared: provenance) | The first listed match's provenance (for an elided quote, the first place where its parts occur together); when not found, `section_id` with empty `pages` and `refs`. |
| `provenance.section_ids` | string[] | As in `locations[].provenance.section_ids`. |

<!-- fields: shared:tool-quote-location -->
| Field | Type | Meaning |
|---|---|---|
| `match` | string | The tier of this match: `exact`, `normalized` or `compact`. |
| `layer` | string | `body` (page content, tables and figure OCR text), `furniture` (running headers and footers) or `ai_generated` (an AI figure description or Mermaid diagram). |
| `kind` | string | What was matched: the Docling label of the text item (`text`, `list_item`, `section_header`, `title`, `caption`, `footnote`, `page_header`, `page_footer`, ...), `table_row` (a table row, its cells joined with ` \| `), `figure_text` (OCR text of a figure), `figure_description` or `figure_mermaid`. |
| `text` | string | The matched items' own text, whitespace-normalised, cut to about 160 characters on each side of the match with `…`. Show it as the evidence. |
| `provenance` | object (shared: provenance) | The section, pages and item refs of the matched items. A match can span several consecutive items. Figure OCR text has the pages of its figure. |
| `provenance.section_ids` | string[] | Only when the matched items lie in several sections: all of them. |
| `table_rows` | integer[] | Only for `table_row`: the grid rows matched (0-based). |

#### Example

Lilly_NCT03421379, `{"quote": "informed consent form is signed, study site personnel will record, via CRF,",
"section_id": "S9.3"}` (trimmed):

```json
{
  "quote": "informed consent form is signed, study site personnel will record, via CRF,",
  "section_id": "S9.3",
  "found": true,
  "match": "exact",
  "count": 1,
  "count_by_layer": {"body": 1, "furniture": 0},
  "locations": [
    {"match": "exact", "layer": "body", "kind": "text",
     "text": "After the informed consent form is signed, study site personnel will record, via CRF, the occurrence and nature of each patient's preexisting conditions, …",
     "provenance": {"section_id": "S9.3", "section_number": "9.3", "title": "9.3. Adverse Events", "pages": [39],
                    "refs": ["#/texts/387"]}}
  ],
  "provenance": {"section_id": "S9.3", "section_number": "9.3", "title": "9.3. Adverse Events", "pages": [39],
                 "refs": ["#/texts/387"]}
}
```

With `"section_id": "S0"` the same quote gives `"found": false`, `"count": 0`, `"locations": []` and the match
above under `found_elsewhere`.

#### Notes

- To highlight a match: resolve `locations[].provenance.refs` in `protocol.json` for the boxes, then search the
  quote's words inside them (see [Pages and boxes](#pages-and-boxes)).
- Headers and footers count as document text: a quote that is only a running header is `found` with
  `layer: "furniture"` and a null `section_id`.
- For an elided quote, the parts must occur in order and close together (each within 2,000 characters after the
  previous one, in the same layer and at the same tier; every occurrence of a later part inside that window is
  tried); `parts[]` still reports each part on its own, so a client can see which part is missing.
- `match` is the first tier that matches document text: a closer match in an AI-generated figure description never
  hides a match in the body, tables or headers/footers. Only when no tier matches document text is the quote
  `found: false` with `found_in_ai_text`.
- Quote short, distinctive spans: a common phrase matches many places, and only 10 are listed. `count_by_layer`
  shows whether the matches are page content or running headers and footers.

## 6. Published files

A published result is a folder of files. The files route serves a fixed set of them, read-only, through
`GET /documents/{doc_id}/files/{name}` (see [Files](#files)); `GET /documents/{doc_id}/files` lists what one result
has.

| File | Short name | Media type | What it is |
|---|---|---|---|
| `protocol.md` | `md`, `markdown` | `text/markdown; charset=utf-8` | The converted document as Markdown. |
| `protocol.html` | `html` | `text/html; charset=utf-8` | The converted document as HTML, with images. |
| `protocol.json` | `json` | `application/json` | The lossless Docling document: every item with its page and box. |
| `protocol.decisions.json` | `decisions`, `decisions.json` | `application/json` | What post-processing changed. |
| `quality.json` | `quality` | `application/json` | Word coverage per page, table and figure checks, stage results. |
| `chunks/chunks.jsonl` | `chunks.jsonl` | `application/x-ndjson` | Retrieval chunks, one JSON record per line. |
| `chunks/parents.jsonl` | `parents.jsonl` | `application/x-ndjson` | Larger, non-overlapping context blocks. |
| `chunks/sections.json` | `sections.json` | `application/json` | The section tree. |
| `chunks/tables.jsonl` | `tables.jsonl` | `application/x-ndjson` | Every chunked table as Markdown. |
| `chunks/glossary.json` | `glossary.json` | `application/json` | Abbreviations and their definitions. |
| `chunks/stats.json` | `stats.json` | `application/json` | Chunking statistics, checks and document metadata. |
| `images/<file>` or `<stem>_images/<file>` | | `image/png`, `image/jpeg`, `image/gif`, `image/webp` | Figure images. |

- A result converted by the service has all eleven files. An imported result has the files of its source folder;
  the public imports, for example, have no `quality.json`. A missing file is `404 file_not_found`.
- Images are in `images/` for results converted by the service and in `<stem>_images/` (such as `protocol_images/`)
  for imported results. `protocol.json`, `protocol.md`, `protocol.html` and the chunk records refer to them by
  these relative paths, so a path from any of them can be passed to the files route as it is.
- **Not served:** `source.pdf` (keep your own copy of the PDF), `tables/*.csv`, `chunks/embeddings.npy` and
  `embeddings_meta.json`, `chunks/abbreviations.json`, the caches, `pipeline.json`, `publish.json` and `logs/`.
- A reprocess publishes a whole new set of files at once. Files you copied belong to one result: keep the
  document's `published_at` (or `result_job_id`) with them and fetch them again when it changes.

### chunks/chunks.jsonl

The retrieval chunks: the passages the service's own `search` ranks. One JSON object per line, in document order.
Each chunk is a paragraph, list, table part or figure under its heading path, normally at most 512 tokens of
embedding text (`stats.json` lists exceptions under `checks.oversized_chunks`).

<!-- fields: file:chunks/chunks.jsonl -->
| Field | Type | Meaning |
|---|---|---|
| `chunk_id` | string | Unique id: `<protocol id>-A<amendment>-<nnnn>` or `doc-<key>-<nnnn>` (see [IDs](#ids)); `nnnn` counts from 0000 in document order. |
| `content_hash` | string | First 12 hex characters of the SHA-1 of `embed_text`. It changes when the embedded text changes: compare it across results to find changed chunks. |
| `protocol_id` | string \| null | Sponsor protocol id detected in the document, the same in every record. |
| `amendment` | string \| null | Amendment number detected, the same in every record. |
| `version_date` | string \| null | Version date detected, as written. |
| `source_sha256` | string | First 16 hex characters of the SHA-256 of `protocol.json` (the converted JSON, not the PDF). |
| `doc_key` | string | `doc-` + 12 hex characters derived from the PDF's hash: the chunk id prefix when no protocol id and amendment were found. |
| `section_number` | string \| null | The section number of the chunk's heading path, when the heading-level step accepted one, such as `3.4.2.2`. |
| `section_title` | string \| null | The nearest heading (the last entry of `headings`). |
| `headings` | string[] \| null | The heading path, outermost first. `["Front matter"]` for text above the first heading; null when the chunk has none. |
| `page_start` | integer \| null | First PDF page of the chunk (1-based). |
| `page_end` | integer \| null | Last PDF page. |
| `pages` | integer[] | Every PDF page the chunk's items are on, sorted. |
| `chunk_type` | string | `text`, `list`, `table`, `figure` or `glossary` (an abbreviation table); `mixed:<a>+<b>…` when the chunk holds several, such as `mixed:list+table`. |
| `element_types` | string[] | Docling labels of the chunk's items, sorted, such as `text`, `list_item`, `table`, `picture`, `footnote`, `code`, `formula`. |
| `doc_item_refs` | string[] | [Item refs](#ids) of the chunk's items in `protocol.json`, in order: resolve them to draw [boxes](#pages-and-boxes). |
| `n_tokens` | integer | Tokens of `embed_text` (Qwen3-Embedding-8B tokenizer). |
| `text` | string | The chunk text. Tables are written one row per line (`Row: Column = value; …`) with the caption and a `Columns:` line repeated in each part. |
| `embed_text` | string | The text that is embedded: a document prefix (name, protocol id, version), the heading path joined by ` > `, then `text`. |
| `tables` | object[] | Only in chunks that hold tables: one entry per table. |
| `tables[].table_id` | string | The table's item ref, such as `#/tables/2`; pass it to `get_table` or find it in `tables.jsonl`. |
| `tables[].caption` | string \| null | The table's caption. |
| `tables[].part` | integer | Which part of the table this chunk is, from 1. |
| `tables[].parts` | integer | In how many chunks the table is split. |
| `figures` | object[] | Only in chunks that hold figures: one entry per figure. |
| `figures[].figure_id` | string | The picture's item ref, such as `#/pictures/0`. |
| `figures[].class` | string \| null | Docling's picture class, such as `flow_chart`, `icon`, `table`, `other`; null when unclassified. |
| `figures[].caption` | string \| null | The figure's caption. |
| `figures[].image_path` | string \| null | Path of the image in the result, relative to the document folder: fetch it with the files route. Null when the figure has no image file. |
| `figures[].has_description` | boolean | Whether an AI-generated description is in the chunk text. |
| `figures[].has_mermaid` | boolean | Whether a Mermaid diagram of the figure is in the chunk text. |
| `section_id` | string \| null | The [section](#chunkssectionsjson) the chunk belongs to, such as `S3.4.2.2`. |
| `role` | string \| null | ICH M11 role of that section, such as `exclusion` (the role names of `find_sections` in [Protocol tools](#5-protocol-tools)). |
| `parent_id` | string \| null | The parent in `parents.jsonl` that contains the chunk. |

Example (LZZT, one figure chunk, trimmed):

```json
{"chunk_id": "doc-ec329cd80e61-0009", "content_hash": "e5ad32d22329", "protocol_id": "H2Q-MC-LZZT", "amendment": null,
 "version_date": null, "source_sha256": "8e6c04adbc131d75", "doc_key": "doc-ec329cd80e61", "section_number": "3.1",
 "section_title": "3.1. Summary of Study Design", "headings": ["3. Investigational Plan", "3.1. Summary of Study Design"],
 "page_start": 9, "page_end": 9, "pages": [9], "chunk_type": "figure", "element_types": ["picture"],
 "doc_item_refs": ["#/pictures/0"], "n_tokens": 194,
 "text": "Figure: Figure LZZT.1. Illustration of study design for Protocol H2Q-MC-LZZT(c). [flow chart]\nText in figure: …",
 "embed_text": "protocol protocol H2Q-MC-LZZT\n3. Investigational Plan > 3.1. Summary of Study Design\nFigure: …",
 "figures": [{"figure_id": "#/pictures/0", "class": "flow_chart",
              "caption": "Figure LZZT.1. Illustration of study design for Protocol H2Q-MC-LZZT(c).",
              "image_path": "protocol_images/image_000000_0855bad9…7114.png", "has_description": false, "has_mermaid": false}],
 "section_id": "S3.1", "role": "design", "parent_id": "doc-ec329cd80e61-P003"}
```

Use it for **your own search index**: embed `embed_text` (or `text`) with your model, and store `chunk_id`,
`content_hash`, `pages`, `doc_item_refs`, `section_id` and `parent_id` with each vector. A hit then gives page
numbers at once, boxes through `doc_item_refs`, and a larger context through `parent_id`. The service's own
embeddings are not served.

### chunks/parents.jsonl

A partition of the document into larger blocks of 300 to 1,500 tokens, for "small-to-big" retrieval: find chunks,
then give the model their parent. Every chunk belongs to exactly one parent. One JSON object per line, in document
order.

<!-- fields: file:chunks/parents.jsonl -->
| Field | Type | Meaning |
|---|---|---|
| `parent_id` | string | `<chunk id prefix>-P<nnn>`, numbered from `P001` in document order, such as `doc-ec329cd80e61-P004`. |
| `kind` | string | `section` (a whole section with its subsections), `merged` (small neighbouring sections joined), `window` (part of a long section's own text) or `table` (one table larger than the maximum). |
| `section_ids` | string[] | The sections the parent covers. |
| `breadcrumb` | string | The heading path, joined by ` > `. For merged sections: the common path, then the first and last section joined by `…`. |
| `role` | string \| null | ICH M11 role of the first covered section that has content. |
| `roles` | string[] | Every role of the covered sections. |
| `pages` | integer[] | PDF pages the parent covers. |
| `tokens` | integer | Tokens of `text`. |
| `doc_item_refs` | string[] | Item refs of the parent's items, in order. |
| `text` | string | Markdown: headings as `#` lines, list items with their markers, tables as Markdown, figures as labelled blocks. |
| `chunk_ids` | string[] | The chunks inside this parent. |
| `table_ids` | string[] | Tables inside this parent. |

Example (LZZT, trimmed):

```json
{"parent_id": "doc-ec329cd80e61-P004", "kind": "merged", "section_ids": ["S3.2", "S3.3", "S3.3.1"],
 "breadcrumb": "3. Investigational Plan > 3.2. Discussion of Design and Control … 3.3. Investigator Information",
 "role": "design", "roles": ["design"], "pages": [9, 10], "tokens": 470,
 "doc_item_refs": ["#/texts/56", "#/texts/57", "#/texts/58", "#/texts/61", /* 5 more */],
 "text": "## 3.2. Discussion of Design and Control\n\nPrevious studies of the oral formulation have shown …",
 "chunk_ids": ["doc-ec329cd80e61-0010", "doc-ec329cd80e61-0011", "doc-ec329cd80e61-0012"], "table_ids": []}
```

### chunks/sections.json

The section tree that `outline`, `find_sections` and `read_section` use, with stable section ids and ICH M11 roles.

<!-- fields: file:chunks/sections.json -->
| Field | Type | Meaning |
|---|---|---|
| `version` | integer | Format version of the index (1). |
| `sections` | object[] | Every section in document order (pre-order: a section, then its subsections). |
| `sections[].section_id` | string | `S<number>` (`S3.4.2.1`), `S~<hash>-<n>` for an unnumbered section, `S0` for the front matter (see [IDs](#ids)). |
| `sections[].number` | string \| null | The accepted section number, such as `3.4.2.1`; null when unnumbered. |
| `sections[].title` | string | The heading text (with its number); `Front matter` for `S0`. |
| `sections[].level` | integer | Depth in the tree: 0 for the front matter, 1 for top-level sections. |
| `sections[].parent_id` | string \| null | The enclosing section; null for top-level sections and `S0`. |
| `sections[].heading_ref` | string \| null | Item ref of the heading in `protocol.json`; null for `S0`. |
| `sections[].item_refs` | string[] | Item refs of the section's own top-level items (paragraphs, lists, tables, pictures, inline minor headings), in reading order, without its subsections. |
| `sections[].pages` | integer[] | PDF pages of the heading and the section's own items. |
| `sections[].role` | string \| null | ICH M11 role, such as `inclusion` or `soa`; null when nothing fits. |
| `sections[].role_score` | number | Confidence of the role, 0 to 1: 1 for an exact canonical title, lower for a synonym, a content cue or a role inherited from the parent. |
| `sections[].minor_headings` | string[] | Item refs of label-like headings kept inline in this section rather than made sections of their own (for example headings ending with a colon, lettered headings, table titles). |
| `ref_section` | object (map) | Section of every body item, keyed by item ref. |
| `ref_section.<ref>` | string | The section id of that item. |
| `source` | string | Only in the fallback format (when the section analysis fails): `fallback`. |
| `ref_map` | object (map) | Only in the fallback format: item ref → section id, as `ref_section`. Fallback section ids look like `s-<number>`. |
| `ref_map.<ref>` | string | The section id of that item. |

Example (LZZT, trimmed):

```json
{"version": 1,
 "sections": [
   {"section_id": "S0", "number": null, "title": "Front matter", "level": 0, "parent_id": null, "heading_ref": null,
    "item_refs": ["#/texts/0"], "pages": [1], "role": "title_page", "role_score": 0.5, "minor_headings": []},
   {"section_id": "S~3ddc1536-1", "number": null, "title": "Protocol H2Q-MC-LZZT(c)", "level": 1, "parent_id": null,
    "heading_ref": "#/texts/2", "item_refs": ["#/texts/3", "#/texts/7"], "pages": [1, 2], "role": "title_page",
    "role_score": 0.5, "minor_headings": ["#/texts/3", "#/texts/7"]},
   /* 133 more */],
 "ref_section": {"#/texts/0": "S0", "#/texts/1": "S~5d133396-1", "#/texts/2": "S~3ddc1536-1", /* … */}}
```

Use it for **navigation**: a table of contents (`parent_id` and `level` give the tree), section pages, and
role-based links ("go to the inclusion criteria"). `ref_section` maps any item ref, for example from a search hit
or a quote, to its section.

### chunks/tables.jsonl

Every table that appears in the chunks, whole, as Markdown. One JSON object per line, in document order.
Tables of contents are not included.

<!-- fields: file:chunks/tables.jsonl -->
| Field | Type | Meaning |
|---|---|---|
| `table_id` | string | The table's item ref, such as `#/tables/3`. |
| `caption` | string \| null | The table's caption. |
| `chain` | string[] \| null | When a table split across pages could not be merged (its columns differ per page): the ids of all its fragments, in order. Null otherwise. |
| `headings` | string[] \| null | Heading path of the first chunk that holds the table. |
| `pages` | integer[] | PDF pages of the table. |
| `n_rows` | integer | Rows, header rows included. |
| `n_cols` | integer | Columns. |
| `markdown` | string | The whole table as a Markdown (GFM) table. |

Example (LZZT, trimmed):

```json
{"table_id": "#/tables/3", "caption": null, "chain": null,
 "headings": ["3. Investigational Plan", "3.4. Study Population", "3.4.2. Criteria for Enrollment", "3.4.2.2. Exclusion Criteria", /* 1 more */],
 "pages": [18], "n_rows": 6, "n_cols": 2,
 "markdown": "| Antilirium® (physostigmine) | 1 month |\n| - | - |\n| Aricept® (donepezil) | 1 month |\n…"}
```

Use it for **table views**: when a search hit is one part of a table (`tables[].part` in the chunk), show the
whole table from here; for a `chain`, show all its fragments. `get_table` gives the same tables with more formats.

### chunks/glossary.json

The abbreviations of the document, found in abbreviation tables, in text lists and inline ("adverse event (AE)").

<!-- fields: file:chunks/glossary.json -->
| Field | Type | Meaning |
|---|---|---|
| `tables` | string[] | Item refs of the tables accepted as abbreviation lists. |
| `entries` | object (map) | One entry per abbreviation, keyed by the abbreviation as written. |
| `entries.<abbreviation>` | object | The definition of one abbreviation. |
| `entries.<abbreviation>.definition` | string | The long form. |
| `entries.<abbreviation>.source` | string | Where it was found: `table`, `text_list` or `inline`. A list beats an inline definition. |
| `entries.<abbreviation>.page` | integer \| null | PDF page of the definition. |
| `entries.<abbreviation>.ref` | string \| null | Item ref of the table or text holding the definition. |
| `entries.<abbreviation>.note` | string \| null | Third column of a three-column abbreviation table (a description); null otherwise. |
| `entries.<abbreviation>.alternatives` | object[] | Other definitions seen for the same abbreviation; empty when none. |
| `entries.<abbreviation>.alternatives[].definition` | string | The other long form. |
| `entries.<abbreviation>.alternatives[].source` | string | `table`, `text_list` or `inline`. |
| `entries.<abbreviation>.alternatives[].page` | integer \| null | Its page. |
| `entries.<abbreviation>.alternatives[].ref` | string \| null | Its item ref. |
| `entries.<abbreviation>.alternatives[].count` | integer | How often it was seen. |

Example (LZZT, trimmed):

```json
{"tables": [],
 "entries": {
   "TTS": {"definition": "Therapeutic System", "source": "inline", "page": 1, "ref": "#/texts/3", "note": null, "alternatives": []},
   "AD": {"definition": "Alzheimer's disease", "source": "inline", "page": 5, "ref": "#/texts/20", "note": null, "alternatives": []},
   /* … */}}
```

Use it for abbreviation tooltips and to expand queries in your own search (`search` does the same). The
`abbreviation` tool looks entries up in both directions.

### chunks/stats.json

Statistics and checks of the chunking step, and the document metadata it detected. Mainly for diagnostics: an
integrator rarely needs more than `document`.

<!-- fields: file:chunks/stats.json -->
| Field | Type | Meaning |
|---|---|---|
| `settings` | object | The chunking settings. |
| `settings.tokenizer` | string | Tokenizer that counts tokens (`Qwen/Qwen3-Embedding-8B`). |
| `settings.max_tokens` | integer | Largest `embed_text` of a chunk, in tokens (512). |
| `settings.table_format` | string | How tables are written in chunks: `rows` (one line per row) or `markdown`. |
| `settings.merge_peers` | boolean | Whether small neighbouring chunks under the same headings are merged. |
| `settings.prefix` | string | The document prefix of every `embed_text`. |
| `document` | object | Document metadata detected from the content. |
| `document.doc_name` | string | The Docling document's name (the file stem, `protocol`). |
| `document.protocol_id` | string \| null | Sponsor protocol id. |
| `document.acronym` | string \| null | Study acronym. |
| `document.title` | string \| null | Full title from the title page. |
| `document.amendment` | string \| null | Amendment number. |
| `document.version_date` | string \| null | Version date, as written. |
| `document.version_label` | string \| null | Version label, such as `Amendment 3`. |
| `document.source_json` | string | File name of the Docling JSON (`protocol.json`). |
| `document.source_sha256` | string | First 16 hex characters of the SHA-256 of `protocol.json`. |
| `document.source_pdf_hash` | string \| null | 16 hex characters of the PDF's hash as Docling records it (the low 64 bits of the PDF's SHA-256: the end of the `doc_id`). |
| `document.doc_key` | string | `doc-` + the first 12 of those 16 characters: the chunk id prefix when no protocol id and amendment were found. |
| `document.identifiers` | object | Registry identifiers found anywhere in the document, headers and footers included. |
| `document.identifiers.sponsor_protocol_id` | string \| null | Sponsor protocol id. |
| `document.identifiers.nct` | string \| null | NCT number. |
| `document.identifiers.eu_ct` | string \| null | EU CT number. |
| `document.identifiers.eudract` | string \| null | EudraCT number. |
| `document.identifiers.ind` | string \| null | IND number. |
| `document.identifiers.who_utn` | string \| null | WHO UTN. |
| `document.identifiers.sources` | object (map) | Where each identifier found was read, keyed by identifier kind (`nct`, `sponsor_protocol_id`, …). |
| `document.identifiers.sources.<kind>.page` | integer \| null | PDF page. |
| `document.identifiers.sources.<kind>.layer` | string | `body`, `furniture` (running header or footer) or `table`. |
| `document.identifiers.sources.<kind>.text` | string | The text around the value, at most about 160 characters. |
| `document.identifiers.sources.<kind>` | object | The source of one identifier. |
| `document.identifiers.others` | object (map) | Only when other values were seen: per identifier kind, values found that are not the study's own (for example another trial's NCT number cited in the text). |
| `document.identifiers.others.<kind>` | string[] | Those values. |
| `chunks` | integer | Number of chunks. |
| `tokens` | object | Token statistics of `embed_text` over all chunks. |
| `tokens.min` | integer | Smallest. |
| `tokens.p10` | integer | 10th percentile. |
| `tokens.median` | integer | Median. |
| `tokens.p90` | integer | 90th percentile. |
| `tokens.max` | integer | Largest. |
| `tokens.mean` | number | Mean, to 0.1. |
| `tokens.total` | integer | Sum. |
| `token_histogram` | object (map) | Chunks per token range, keyed by range (`0-63`, `64-127`, …, `512+`). |
| `token_histogram.<range>` | integer | Chunks in that range. |
| `chunk_types` | object (map) | Chunks per `chunk_type`, keyed by type. |
| `chunk_types.<type>` | integer | Count. |
| `chunks_containing_label` | object (map) | Chunks that contain an item of each Docling label, keyed by label. |
| `chunks_containing_label.<label>` | integer | Count. |
| `largest` | array[] | The 10 largest chunks, each `[chunk_id, n_tokens, chunk_type, section_title]`. |
| `checks` | object | Quality checks of the chunking. Each value lists what failed the check; empty when all is well. |
| `checks.empty_chunks` | string[] | Chunk ids without text. |
| `checks.oversized_chunks` | array[] | `[chunk_id, n_tokens]` of chunks above `max_tokens`. |
| `checks.chunks_without_headings` | string[] | Chunk ids without a heading path. |
| `checks.uncovered_items` | string[] | Body items in no chunk. |
| `checks.duplicated_items` | string[] | Item refs in more than one chunk (tables excluded). |
| `checks.headings_without_content` | string[] | Headings (first 80 characters) with no chunk under them. Informational. |
| `checks.tables_split_mid_row` | string[] | Chunks that cut a table row without repeating its label. |
| `checks.long_rows_split_with_label` | string[] | Chunks that split a long table row at sentence boundaries, with its label repeated. Informational. |
| `parents` | object | Statistics of `parents.jsonl`. |
| `parents.parents` | integer | Number of parents. |
| `parents.min_tokens` | integer | Target minimum size (300). |
| `parents.max_tokens` | integer | Target maximum size (1,500). |
| `parents.kinds` | object (map) | Parents per `kind`, keyed by kind. |
| `parents.kinds.<kind>` | integer | Count. |
| `parents.tokens` | object | Token statistics of the parents. |
| `parents.tokens.min` | integer | Smallest. |
| `parents.tokens.median` | integer | Median. |
| `parents.tokens.p90` | integer | 90th percentile. |
| `parents.tokens.max` | integer | Largest. |
| `parents.tokens.total` | integer | Sum. |
| `parents.under_min` | integer | Parents below the minimum. |
| `parents.over_max_non_table` | integer | Parents above the maximum that are not single tables. |
| `parents.sections` | integer | Sections in the tree. |
| `parents.units` | integer | Document units considered. |
| `parents.atoms` | integer | Groups of units that share a chunk (never split). |
| `parents.skipped_items` | object (map) | Items left out, keyed by reason (such as `toc`, `empty`). |
| `parents.skipped_items.<reason>` | integer | Count. |
| `parents.index` | string | Which section index was used: `structure`, or `fallback`. |
| `parents.chunks` | integer | Chunks considered. |
| `parents.chunks_mapped` | integer | Chunks with a parent. |
| `parents.chunks_in_several_parents` | integer | Chunks that would belong to more than one parent (should be 0). |
| `parents.chunks_without_unit` | integer | Chunks that match no unit (should be 0). |
| `parents.chunks_spanning_sections` | integer | Chunks whose items lie in several sections. |
| `parents.atoms_spanning_sections` | integer | Atoms whose items lie in several sections. |
| `parents.atoms_without_parent` | integer | Atoms not placed in a parent (should be 0). |

Example (LZZT, trimmed):

```json
{"settings": {"tokenizer": "Qwen/Qwen3-Embedding-8B", "max_tokens": 512, "table_format": "rows", "merge_peers": true,
              "prefix": "protocol protocol H2Q-MC-LZZT"},
 "document": {"doc_name": "protocol", "protocol_id": "H2Q-MC-LZZT", "acronym": null,
              "title": "Safety and Efficacy of the Xanomeline Transdermal Therapeutic System (TTS) in Patients with Mild to Moderate Alzheimer's Disease",
              "amendment": null, "version_date": null, "source_json": "protocol.json", "source_sha256": "8e6c04adbc131d75",
              "source_pdf_hash": "ec329cd80e61a040", "version_label": null, "doc_key": "doc-ec329cd80e61",
              "identifiers": {"sponsor_protocol_id": "H2Q-MC-LZZT", "nct": null, "eu_ct": null, "eudract": null, "ind": null,
                              "who_utn": null,
                              "sources": {"sponsor_protocol_id": {"page": 1, "layer": "body", "text": "Protocol H2Q-MC-LZZT(c)"}}}},
 "chunks": 180,
 "tokens": {"min": 21, "p10": 73, "median": 193, "p90": 456, "max": 508, "mean": 229.2, "total": 41256},
 "token_histogram": {"0-63": 11, "64-127": 49, /* 6 more */},
 "chunk_types": {"text": 101, "mixed:list+text": 43, /* 7 more */},
 "chunks_containing_label": {"text": 158, "list_item": 59, /* 6 more */},
 "largest": [["doc-ec329cd80e61-0041", 508, "text", "…"], /* 9 more */],
 "checks": {"empty_chunks": [], "oversized_chunks": [], "chunks_without_headings": [], "uncovered_items": [],
            "duplicated_items": ["#/texts/225"], "headings_without_content": [/* 15 headings */],
            "tables_split_mid_row": [], "long_rows_split_with_label": []},
 "parents": {"parents": 52, "min_tokens": 300, "max_tokens": 1500, "kinds": {"merged": 21, "section": 24, "window": 7},
             "tokens": {"min": 301, "median": 625, "p90": 1177, "max": 1429, "total": 36318}, "under_min": 0,
             "over_max_non_table": 0, "sections": 135, "units": 797, "atoms": 179, "skipped_items": {"toc": 2, "empty": 1},
             "index": "structure", "chunks_spanning_sections": 0, "atoms_spanning_sections": 0, "atoms_without_parent": 0,
             "chunks": 180, "chunks_mapped": 180, "chunks_in_several_parents": 0, "chunks_without_unit": 0}}
```

### quality.json

The quality report of a conversion: word coverage per page, table checks, figure counts, and the stage results and
warnings of the run. Numbers only, no document text. Written by the service's pipeline; an imported result has it
only when its source folder had one.

<!-- fields: file:quality.json -->
| Field | Type | Meaning |
|---|---|---|
| `coverage` | object | Word coverage over the whole document: the PDF text layer's words found in the converted document. Running headers, footers and page numbers are left out on both sides. |
| `coverage.coverage` | number \| null | Coverage 0 to 1 over the checked pages, weighted by words; null when no page has text. |
| `coverage.pdf_words` | integer | Words of the PDF text layer on the checked pages. |
| `coverage.furniture_share` | number \| null | Share of those words that the converter put in headers or footers. |
| `coverage.pages_checked` | integer | Pages checked (pages with text, table-of-contents pages excluded). |
| `coverage.toc_pages_skipped` | integer | Table-of-contents pages, whose gap is expected (the contents are rebuilt from the headings). |
| `coverage.pages_below_threshold` | integer | Checked pages below 0.97. |
| `coverage.lowest_page_coverage` | number \| null | Coverage of the worst checked page. |
| `coverage.pages_below` | integer[] | Those pages' numbers. |
| `pages` | object[] | One entry per PDF page with words in its text layer (pages without text are absent). |
| `pages[].page` | integer | PDF page (1-based). |
| `pages[].coverage` | number | Coverage of this page, 0 to 1. |
| `pages[].pdf_words` | integer | Words of the page's text layer, headers and footers left out. |
| `pages[].furniture_words` | integer | Of those, words found only in header or footer items. |
| `pages[].expected` | boolean | `true` for a table-of-contents page: a low coverage is expected and the page is not checked. |
| `tables` | object | Table QA over all tables. |
| `tables.tables` | integer | Tables checked. |
| `tables.tables_with_marks` | integer | Tables with one-character marks (`X`, check marks) in the PDF or in their cells. |
| `tables.marks_pdf` | integer | Marks in the PDF text layer inside the tables' boxes. |
| `tables.marks_doc` | integer | Marks in the converted cells. |
| `tables.marks_missing` | integer | Marks in the PDF that the cells lack, summed per table. |
| `tables.tables_missing_marks` | integer | Tables with fewer marks than the PDF. |
| `tables.tables_extra_marks` | integer | Tables with more marks than the PDF. |
| `tables.lattice_raw` | object (map) | Ruling-line verdicts on the raw page fragments during post-processing, keyed by verdict. Present when the decisions log has them. |
| `tables.lattice_raw.<verdict>` | integer | Count, for verdicts such as `verified_ok`, `skipped`, `disagree_not_ruled`. |
| `tables.lattice_final` | object (map) | Ruling-line check of the final single-page tables, keyed by verdict (`<action>` or `<action>:<reason>`). |
| `tables.lattice_final.<verdict>` | integer | Count, for verdicts such as `verified_ok`, `skipped:no_rules`, `multi_page_not_checked`. |
| `tables_per_table` | object[] | The mark counts per table. |
| `tables_per_table[].table` | string | The table's item ref. |
| `tables_per_table[].pages` | integer[] | Its pages. |
| `tables_per_table[].marks_doc` | integer | Marks in its cells. |
| `tables_per_table[].marks_pdf` | integer | Marks in the PDF inside its boxes. |
| `figures` | object | Figure counts. |
| `figures.pictures` | integer | Pictures. |
| `figures.skipped_logo_like` | integer | Pictures classified as logos or icons. |
| `figures.described` | integer | Pictures with an AI-generated description. |
| `summary` | object (shared: svc-quality-summary) | The summary the document record shows as `quality`. |
| `warnings` | string[] | The run's warnings, as the job shows them. |
| `stages` | object[] (shared: svc-stage) | The run's stages, as the job shows them. |

Example (LZZT, trimmed):

```json
{"coverage": {"coverage": 0.9967, "pdf_words": 22536, "furniture_share": 0.0007, "pages_checked": 94, "toc_pages_skipped": 3,
              "pages_below_threshold": 2, "lowest_page_coverage": 0.8982, "pages_below": [9, 54]},
 "pages": [{"page": 1, "coverage": 1.0, "pdf_words": 39, "furniture_words": 0, "expected": false},
           {"page": 3, "coverage": 0.0398, "pdf_words": 226, "furniture_words": 5, "expected": true}, /* 95 more */],
 "tables": {"tables": 22, "tables_with_marks": 3, "marks_pdf": 136, "marks_doc": 136, "marks_missing": 0,
            "tables_missing_marks": 0, "tables_extra_marks": 0,
            "lattice_raw": {"skipped": 12, "verified_ok": 8, "disagree_not_ruled": 6},
            "lattice_final": {"skipped:no_rules": 9, "verified_ok": 6, "disagree_not_ruled": 6, "multi_page_not_checked": 1}},
 "tables_per_table": [{"table": "#/tables/2", "pages": [18], "marks_doc": 0, "marks_pdf": 0}, /* 21 more */],
 "figures": {"pictures": 11, "skipped_logo_like": 0, "described": 0},
 "summary": {"coverage": 0.9967, "pages_below_97": 2, "tables": 22, "marks_missing": 0,
             "figures": {"pictures": 11, "skipped_logo_like": 0, "described": 0}, "chunks": 180,
             "stages": {"hash": "done", "convert": "done", /* 6 more */}},
 "warnings": [],
 "stages": [{"name": "chunk", "status": "done", "seconds": 3.2, "detail": {"chunks": 180, "parents": 52}}, /* 7 more */]}
```

Use it to flag pages for review: pages with `expected: false` and a low `coverage` may have lost text, and a table
with `marks_pdf` above `marks_doc` may have lost Schedule of Activities marks.

### protocol.decisions.json

The decisions log of post-processing: what each repair step changed, with item refs, pages and reasons, never
document text. It is an internal log: the keys below exist today, but their inner structure is not a stable
interface and may change with any pipeline version. Only the top-level keys are listed.

<!-- fields: file:protocol.decisions.json -->
| Field | Type | Meaning |
|---|---|---|
| `heading_source` | string | How heading levels were decided: `outline` (the PDF's bookmarks) or `numbering`. |
| `heading_sources` | object | Statistics of the heading-level decision (table-of-contents matches, numbering scheme). |
| `heading_disagreements` | array | Headings where the level signals disagreed. |
| `demoted_headings` | array | Headings turned into text or minor headings. |
| `outline` | object | How the PDF's bookmarks matched the headings, when the bookmarks were evaluated. |
| `toc_items_kept` | integer | Table-of-contents items kept. |
| `toc_items_deleted` | integer | Table-of-contents items deleted. |
| `deleted_toc_items` | array | The deleted table-of-contents items (page, label, reason). |
| `furniture` | array | Text items moved to the furniture layer: in the header or footer band of the page, or repeated on many pages. |
| `restored_headers` | array | Page-header items restored as body text (a header seen on a single page). |
| `shadow_pictures` | array | Pictures over a table whose words are all in the table, dropped. |
| `kept_overlapping_pictures` | array | Pictures over a table that hold words the table lacks, kept. |
| `figure_ocr` | array | Figures whose text was read again by OCR. |
| `caption_position` | string | Where captions sit relative to their tables, detected per document, such as `above`. |
| `captions_restored` | array | Captions attached back to their table. |
| `removed_titles` | array | Table titles repeated on continuation pages, removed. |
| `footnote_markers` | array | The footnote marker families detected. |
| `footnotes` | array | Footnotes attached to tables. |
| `hyphen_joins` | array | Items split by a line-end hyphen and joined again. |
| `text_reconcile` | array | Items whose text was corrected against the PDF text layer. |
| `label_value_pairs` | array | Label and value items paired (for example on the title page). |
| `unknown_pua` | object | Private-use characters (symbol fonts) that could not be mapped, with counts. |
| `table_lattice` | array | Ruling-line checks and corrections of tables, one entry per table fragment. |
| `merges` | array | Tables split across pages and merged. |
| `joins` | array | Table rows cut by a page break and joined. |
| `kept_header_rows` | array | Header rows of a continuation page that were kept, as body rows, when tables were merged. |
| `repeated_section_rows` | array | Section rows repeated at the top of a continuation page, dropped when tables were merged. |
| `inner_header_rows` | array | Rows inside tables that start a new block of column labels, marked as header rows. |
| `overlapping_cells` | array | Overlapping table cells fixed. |
| `reading_order` | array | Items moved to repair the reading order. |
| `colon_labels_nested` | array | Headings ending with a colon nested one level below the numbered heading they follow. |

Example (LZZT, values trimmed):

```json
{"heading_source": "numbering", "caption_position": "above", "toc_items_kept": 1, "toc_items_deleted": 0,
 "table_lattice": [{"table": "#/tables/0", "page": 2, "rows_before": 29, "cols_before": 2, "text_match": 1.0,
                    "action": "skipped", "reason": "no_rules"}, /* 25 more */],
 "heading_sources": {"toc_entries": 75, "toc_matched": 74, "toc_available": 1, "level1_rejected": 6, "disagreements": 6,
                     "scheme": "absolute"},
 /* 17 more keys */}
```

Use it to audit a conversion ("why is this heading not a section?"), not as a data source.

### protocol.md and protocol.html

- `protocol.md` is Docling's Markdown export of the converted document: headings, paragraphs, lists, compact
  tables, and images referenced by their relative path (`![Image](protocol_images/…)`). Page changes are marked
  with `<!-- page break -->` lines, without page numbers; use the chunks or the tools for page numbers. Good for
  display and as plain input to a model.
- `protocol.html` is the same document as HTML with its images. The files route serves it with a sandboxing
  Content-Security-Policy, so it shows in a browser but runs no script.

Neither file carries item refs or boxes; for those use `protocol.json`.

### protocol.json (Docling)

`protocol.json` is the lossless Docling document (`schema_name` `DoclingDocument`, version `1.10.0` in the current
results). It is Docling's own format, documented by Docling; this reference does not list its fields. What an
integrator needs from it:

| Part | What it holds |
|---|---|
| `texts[]` | Text items: paragraphs, headings (`label` `section_header`, with `level`), list items (with `marker`), captions, footnotes, page headers and footers. `text` is the item's text. |
| `tables[]` | Tables: `data.table_cells[]` (each with `text`, row and column indexes and spans, `column_header`, and a `bbox`), `data.num_rows`, `data.num_cols`, `captions` and `footnotes` (refs). |
| `pictures[]` | Figures: `image.uri` (the image file, relative to the document folder), `image.size`, `meta.classification`, `captions`. |
| `groups[]` | Lists and other groupings; `children` holds the refs of their items. Groups have no `prov`. |
| `body`, `furniture` | The roots of the reading-order tree (body content; headers and footers). |
| `<item>.self_ref` | The item's own ref, such as `#/texts/179`: a JSON pointer into this file. The `refs` of tool [provenance](#provenance) and the `doc_item_refs` of chunks are these refs. |
| `<item>.parent`, `<item>.children` | Refs as `{"$ref": "#/texts/12"}`. |
| `<item>.content_layer` | `body` or `furniture` (running headers and footers). |
| `<item>.prov[]` | Where the item is in the PDF: one entry per page it is on (21 items of LZZT have more than one). |
| `<item>.prov[].page_no` | PDF page, 1-based (the page index of a PDF viewer, not the printed page label). |
| `<item>.prov[].bbox` | `{l, t, r, b, coord_origin}` in PDF points (1/72 inch), `coord_origin` `BOTTOMLEFT`: `t` and `b` are measured from the bottom of the page, so `t` > `b`. |
| `<item>.prov[].charspan` | `[start, end]` of the item's text that this entry covers. |
| `pages.<n>.size` | `{width, height}` of page `n` in points; the keys of `pages` are page numbers as strings (`"1"`). |
| `origin` | The source PDF: `filename`, `mimetype` and `binary_hash` (the low 64 bits of the PDF's SHA-256, the end of `doc_id`). |

**Verified on the public protocols:** for 200 text items each of LZZT and Lilly, the PDF's own text layer inside
`prov[].bbox` (read as bottom-left PDF points on page `page_no`) holds the item's words in every case, and in
almost none when the box is read top-down; every `pages.<n>.size` equals the PDF page size, and the page count
equals the PDF's. Table cell boxes are different: `data.table_cells[].bbox` has `coord_origin` `TOPLEFT` (`t`
measured from the top) and no page number, and some cells have no box.

**Drawing a box on a rendered page** (see also [Pages and boxes](#pages-and-boxes)):

1. Resolve the ref: `#/texts/179` is `doc["texts"][179]`. For a group, resolve its `children` instead.
2. For each entry of `prov`: take `page_no`, `bbox` and the page size `doc["pages"][str(page_no)]["size"]`.
3. For a page rendered `W` pixels wide: `s = W / width`, then `x = l × s`, `y = (height − t) × s`,
   `w = (r − l) × s`, `h = (t − b) × s` (top-left pixel coordinates).

Example: the first text item of LZZT, on page 1, has `bbox` `{"l": 229.14, "t": 635.31, "r": 430.90, "b": 615.83,
"coord_origin": "BOTTOMLEFT"}` on a 612 × 792 point page. Rendered 1224 pixels wide (`s = 2`), its box is at
`x = 458.3`, `y = 313.4`, with `w = 403.5` and `h = 39.0` pixels.

**The PDF is not served.** `GET /documents/{doc_id}/files/source.pdf` answers `404 file_not_found`: to render pages,
keep your own copy of the PDF you uploaded. Its SHA-256 is the `doc_id`, so you can check that you have the same
bytes.

## 7. Building features from the outputs

Recipes for common application and UI features: which calls to make and which fields to use. Every field named
here is described in the field tables above.

### Upload with live progress

1. `POST /documents` with the file. Store `doc_id`. With `queued` true (HTTP 202), or a non-null `job_id` on a 200
   (the same bytes are already queued or running), follow that job; with `job_id` null, the result exists: go on.
2. Poll `GET /jobs/{job_id}` every 15–30 seconds. Show `state`; while `running`, show `stage` (`convert`, `figures`,
   `embed`, …) and `progress.done` of `progress.total` when both are numbers. `attempts` above 1 means the service
   restarted during the job and resumed it.
3. Stop at a final `state`:
   - `done`: ready.
   - `done_with_warnings`: ready, but show `warnings` (for example, embeddings missing: search will not work).
   - `failed`: show `error.type`; the document keeps its older result, if any (`has_result`).
   - `cancelled`: nothing was made.
4. After a final state, `stages[]` gives per-stage seconds and counts (for example `embed.detail.embedded`) for a
   job summary.

### Document list and stale results

- `GET /documents` gives one object per PDF: show `filenames[0]`, `identifiers.nct`,
  `identifiers.sponsor_protocol_id`, `pages`, `status` and `updated_at`.
- `has_result` decides whether to enable "Open" (files and tools need a result); `status` is the latest job's
  state, so a failed reprocess shows `failed` while the older result is still served.
- `stale` true means the result was made by another pipeline version or fingerprint: offer "Update", which calls
  `POST /documents/{doc_id}/reprocess` (202 queues a job; follow it as above).
- `GET /documents/{doc_id}` adds `current_job` (with `stage` and `progress`), `files`, `related` and
  `same_text_as`: show a "related" badge for other versions or amendments of the same study (`related[].reasons`),
  and "same text as" for re-saved copies of the same PDF.

### Show where a value came from

Every protocol tool returns 1-based PDF pages and [item refs](#ids) for its content, so a UI can open the PDF at the
right page and draw a box around the source.

| Feature | Fields |
|---|---|
| A search answer with citations | `search` → `blocks[].section`, `blocks[].pages`, `blocks[].provenance.refs` |
| A paragraph or list item of a section | `read_section` → `blocks[].pages`, `blocks[].ref`, `blocks[].item_refs` (one per list item) |
| A verified quote | `quote_check` → `locations[].provenance.pages` and `.refs`, `locations[].text` as the evidence line |
| A study identifier, title or date | `document_info` → `identifiers.<kind>.source.ref` (the item it was read from; `.row` for a table row), `.page` and `.text` as the evidence line |
| A table or SoA | `get_table` / `get_soa` → `provenance.pages`, `provenance.refs` (the table ids) |

How to turn refs into boxes (see [Pages and boxes](#pages-and-boxes) for the coordinate rules):

```javascript
// protocolJson: GET /documents/{doc_id}/files/protocol.json, fetched once per result and cached.
// Returns boxes as fractions of the page, so they work at any zoom: multiply by the rendered page size.
function boxesFor(protocolJson, refs) {
  const boxes = [];
  const visit = (ref) => {
    const m = /^#\/(\w+)\/(\d+)$/.exec(ref);
    const item = m && protocolJson[m[1]] && protocolJson[m[1]][Number(m[2])];
    if (!item) return;
    if (!item.prov || item.prov.length === 0) {          // a group (a list): use its items
      (item.children || []).forEach((child) => visit(child.$ref));
      return;
    }
    for (const p of item.prov) {                         // one entry per page the item is on
      const size = protocolJson.pages[String(p.page_no)].size;
      const b = p.bbox;
      const top = b.coord_origin === "BOTTOMLEFT" ? size.height - b.t : b.t;
      boxes.push({ page: p.page_no, x: b.l / size.width, y: top / size.height,
                   w: (b.r - b.l) / size.width, h: Math.abs(b.t - b.b) / size.height });
    }
  };
  refs.forEach(visit);
  return boxes;
}
```

- The boxes cover whole items (a paragraph, a list item, a table, a figure). To mark the exact words, search the
  quote in your PDF renderer's text layer inside the box.
- The PDF comes from your own copy: the service does not serve it back.

### A table of contents and a section reader

1. `outline` gives every section: nest `sections[]` by `parent` (not by `children`, which stops at `max_level`). Show
   `number`, `title` and `pages`; `tokens` tells you how long a section is before you open it; `role` lets you label
   or filter sections (eligibility, SoA, objectives).
2. On a click, call `read_section` with `{"section": "<id>"}`. Follow `cursor` until it is null to get the whole
   section, or load the next part when the user scrolls to the end.
3. Render `markdown` with any Markdown renderer after two small conversions: turn the `{#S6.1}` at the end of a
   heading into the heading's id, and the `<!-- p. 26 -->` comments into page markers that scroll your PDF view.
4. To link the text to the PDF, use `blocks[].chars` (`[start, end)` offsets in `markdown`): a click or hover in the
   text finds its block, and the block's `ref` gives the box.

### Verified-value badges

Run `quote_check` on every value your agent extracts, with the `section_id` it should come from, and show the
outcome next to the value:

| Result | Badge |
|---|---|
| `found` true, `match` `exact` or `normalized` | Verified; show `locations[0].text` and the page. |
| `found` true, `match` `compact` | Verified, punctuation or spacing differs. |
| `found` false, `found_elsewhere` not empty | Found, but in another section than expected. |
| `found` false, `closest` present | Not verbatim; offer `closest.text` (with `closest.similarity`) as a correction. |
| `found` false, only `found_in_ai_text` | Only in an AI-generated figure description: not document text. |
| `found` false, nothing else | Not in the document. |

### A Schedule of Activities grid

`get_soa` returns each SoA as data. Build one grid per entry of `soas` (tabs when there are several):

- Columns: `visits[]`, left to right. Group them under `epoch_hint` or the upper levels of `header_path`; show
  `window` under the label.
- Rows: `activities[]`, grouped under `section`.
- Cells: index `cells[]` by `(activity, visit)`. Show an X for `mark`, `qualifier` as small text, and
  `footnote_markers` as superscripts whose text is in `footnotes`.
- Link the grid to the PDF with `soas[].provenance` (the table ids and pages), and offer
  `get_table(<table_id>, format="rows")` as the verbatim view.
- Visit labels are taken from the lowest header cell, with footnote markers removed, and `timing` is composed from
  the header: check a label with `quote_check` before you store it as verbatim text.

### Citations in a chat answer

`search` returns the context an answering model sees. Give each block a citation chip built from
`blocks[].section` and `blocks[].pages`, which opens `read_section(blocks[].provenance.section_id)` or the PDF page.
`search` is selective (by default 8 passages within 12,000 tokens): add a "read the whole section" link whenever the
answer must be complete.

### A study identity card

`document_info` gives titles, identifiers, version and sponsor, each with the line it was read from
(`source.text`, `source.page`). Show the value with that line as its evidence, and mark `sponsor` as unverified
unless `sponsor.method` is `cover-page label`. To highlight the line, search `source.text` on `source.page`:
`source.ref` locates the value, which can be another item than the quoted line.

### Abbreviation tooltips

For one term, call `abbreviation`. For a UI that underlines every abbreviation, download
`chunks/glossary.json` once (see [Published files](#6-published-files)) and match locally.

### Figures

In `read_section`, a block with `kind` `figure` has `figure_id`, `image` and `ai_generated`. Fetch the image with
`GET /documents/{doc_id}/files/{image}`. When `ai_generated` is true, the Markdown contains an AI-generated
description and Mermaid diagram, labelled as such: show the same label in your UI.

## 8. What the outputs do not give

- **The PDF.** The service does not return the uploaded file. Keep your own copy; the `doc_id` is its SHA-256.
- **Coordinates in tool results.** Tools give pages and item refs; boxes come from `protocol.json`
  ([Pages and boxes](#pages-and-boxes)). Boxes are per item, not per word, and table-cell boxes have no page number.
- **A complete answer from `search`.** It is a ranked, budgeted selection. Use `read_section` for anything that must
  be complete (every criterion, every objective).
- **Verbatim SoA visit labels.** `get_soa` cleans labels and composes `timing`; some labels are not word for word in
  the PDF.
- **Confidence scores for extracted values.** The service gives evidence (source lines, match tiers, `role_score`),
  not probabilities. Heuristic values say so (`sponsor.method`).
- **Push notifications.** There are no webhooks or streams: poll the job.
- **Users and permissions.** There is no authentication and no per-user access: every client sees every document.
- **Ids that survive a pipeline upgrade.** Section ids, item refs and chunk ids belong to one result; see
  [IDs](#ids).

### Known issues

Found while this reference was written; each is described where it occurs. They may be fixed later, which would
change the affected values.

| Where | Issue |
|---|---|
| `chunks.jsonl` `embed_text` | The embedding prefix reads `protocol protocol <id> …` when no acronym was found and the Docling JSON is named `protocol.json` (every result made by the service). Harmless for search (the word is in every chunk); a fix would change every embedding text, so it waits for the next change that re-embeds. |

## 9. Keeping this reference current

The field tables are tested against the service's real outputs:

```powershell
.venv\Scripts\python.exe tests\test_api_reference.py
```

- It runs every protocol tool with argument variants on the public protocols in `output_public/`, and drives the REST
  routes, the MCP tools and the published files in-process on a temporary data folder. Everything runs offline.
- It fails when a field appears in a real output but not in its table (`undocumented: tool:get_soa: …`): add a row
  for it. Fields documented but not seen in a run are reported, not failed, because some appear only in rare cases.
- It also checks that every field table is well formed, that every shared object is defined and used, that every
  JSON example parses (after removing `/* … */` comments) and that every link resolves.
- Without `output_public/` (a fresh clone) the inventory checks are skipped.

To list every field a surface returns, or to check a draft:

```powershell
.venv\Scripts\python.exe scripts\api_inventory_tools.py
.venv\Scripts\python.exe scripts\api_inventory_service.py
.venv\Scripts\python.exe scripts\api_inventory_common.py check docs\api_reference.md tools service
```
