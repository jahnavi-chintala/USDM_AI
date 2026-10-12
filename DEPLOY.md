# Deploying the protocol service

One container holds the whole service:

- a **REST API** (OpenAPI at `/docs`);
- an **MCP server**: Streamable HTTP at `/mcp`, plus stdio through `docker exec -i`;
- a **background worker** that converts uploaded protocol PDFs.

Documents, results, the job queue and the caches live in one named Docker volume.

Today the service runs locally only. Before it is hosted anywhere it needs authentication, TLS and a decision on storage; see [the checklist](#10-before-hosting-checklist).

Commands are for PowerShell on Windows, run from the repository root; the Docker commands are the same on Linux and macOS.

## 1. Quick start

**Prerequisites:**
- Docker Desktop (or Docker Engine) with BuildKit; Docker 28 uses it by default.
- About 12 GB of RAM for Docker: the container is limited to 8 GB.
- About 10 GB of free disk for the build.

```powershell
Copy-Item .env.example .env                          # once; then set OPENROUTER_API_KEY in .env
python scripts\check_build_context.py                # any Python 3.11+; refuses PDFs / outputs / secrets in the build context
docker build -t protocol-service .                   # first build: ~20-40 min, downloads ~2 GB
docker compose up -d                                 # start (REST + MCP + worker)
docker compose ps                                    # wait for "(healthy)", about 30 s
```

- **Using it:** open http://127.0.0.1:8000/ui/ for the demo UI (below), or http://127.0.0.1:8000/docs for the interactive API. `GET /health` answers `{"status": "ok", ...}`.
- **Stopping:** `docker compose down` stops and removes the container. The volume, and with it every document, stays.

**Checks** (optional, offline):

```powershell
docker build --target test -t protocol-service:test .
docker run --rm --network none protocol-service:test           # 6 service test suites, ~1.5 min, no network
```

**End-to-end smoke test** against the running container:

```powershell
.venv\Scripts\python.exe scripts\smoke_test.py --allow-openrouter
```

- It uploads the two **public** protocols (LZZT, Lilly), then checks REST, MCP over HTTP and MCP over stdio, the known facts and the duplicate rules.
- It takes about 30 minutes and sends about 27 requests to OpenRouter (see [Data handling](#6-data-handling)).
- Without `--allow-openrouter` it stops before uploading anything.

**Full end-to-end test** of the service as it is really used:

```powershell
.venv\Scripts\python.exe scripts\e2e_test.py --allow-openrouter --report $env:TEMP\e2e_report.json
```

- **What it covers:**
  - Uploads Alexion, Bayer, PrECOG and Sanofi through every upload path: REST, MCP base64, and the inbox via `docker cp`.
  - Runs a USDM-building agent over MCP HTTP and stdio, scored against the public USDM keys: identifiers, every inclusion/exclusion criterion, SoA visits, activities and cells, and quote_check of every extracted value.
  - Runs several clients at once while a document converts; REST and MCP must give identical results.
  - Restarts the container mid-conversion.
  - Covers the duplicate/reprocess rules, and failure cases: corrupt, non-PDF, encrypted (none, wrong and right password), oversized, cancel, delete while running.
  - Checks the cache purge.
  - Runs a backup/restore drill on a scratch volume, in a throwaway container on port 8001.
  - Measures stage times, tool latency, derived OpenRouter request counts and peak memory.
- **Cost:** measured 74 min and about 76 OpenRouter requests when nothing is cached. Without `--allow-openrouter` it prints the estimate and stops.
- **Last run** (2026-10-12): 129 passed, 0 failed, 1 skipped (PrECOG has no USDM key).
- **What it changes in the live service:** it deletes and re-uploads its four public protocols, restarts the container once, and removes the made-up test documents it creates.
- `--numbers-only --pdf-label MAIN|P1|P2|P3` runs the same flow on one private protocol in its own scratch container (port 8004, removed afterwards), printing only counts, booleans and timings.

### Demo UI

http://127.0.0.1:8000/ui/ is served by the service itself: same origin, plain HTML/CSS/JS with no external resources, and a strict Content-Security-Policy. It has five screens:

- **Documents:** drag-and-drop upload with live per-stage progress; the document list with status, stale and related badges; reprocess, and delete with an optional cache purge.
- **Document view:** identifiers, quality and warnings, plus the converted document, sections, chunks, images, quality report and files.
- **Tool explorer:** every protocol tool with a form generated from `GET /tools`, the raw JSON and a readable view: read_section as Markdown with page and section markers, get_soa as a visit × activity grid, search as ranked blocks, quote_check with its locations.
- **MCP:** the same calls as JSON-RPC to `/mcp`, side by side with REST and a "matches REST" badge.
- **Use cases:** guided walkthroughs: eligibility criteria for USDM, SoA encounters and activities, verifying a value, identifiers and version.

Upload, reprocess and search are marked "Calls OpenRouter" and ask for confirmation first.

**Day to day:**

| Task | Command |
|---|---|
| Logs | `docker compose logs -f` |
| Restart | `docker compose restart` |
| Update after a code change | `python scripts\check_build_context.py`, then `docker build -t protocol-service .` and `docker compose up -d` (recreates the container; the volume stays) |
| Image size | about 3.3 GB: Python packages 1.9 GB, models 0.7 GB, Node + Mermaid 0.3 GB, system 0.4 GB |

**How the image is built (Dockerfile):**
- The build context is a whitelist (`.dockerignore`, generated by `python -m service.runtime --write-dockerignore`), and a build stage fails on any PDF or forbidden path. Protocols, outputs, reports, caches, `.env` and `local_config.json` cannot reach an image.
- torch is the CPU build only; the build fails on any CUDA package.
- All models are downloaded at build time into `/opt/models`: the Docling layout, TableFormer and picture-classifier models, the RapidOCR models, and the Qwen3 tokenizer files only.
- The models are then verified with the network switched off, as the runtime user, so the running container never downloads anything.
- The service runs as uid 10001 under `tini`.

## 2. Configuration

Settings come from environment variables. In compose they come from `.env` (documented in `.env.example`), except the variables `docker-compose.yml` fixes. After changing a setting, run `docker compose up -d` to recreate the container.

| Variable | Default | Meaning |
|---|---|---|
| `OPENROUTER_API_KEY` | (empty) | Key for figure descriptions, embeddings and reranking. Only ever sent to `https://openrouter.ai` as the Authorization header; never logged. |
| `OPENROUTER_OFFLINE` | unset | `1` / `true` / `yes` / `on`: never call OpenRouter (see [offline mode](#offline-mode)). |
| `FIGURE_MODEL` | `openrouter:anthropic/claude-opus-5.5` | Vision model for figure descriptions; an empty value turns them off. |
| `EMBED_MODEL` | `qwen/qwen3-embedding-8b` | Chunk and query embeddings. |
| `RERANK_MODEL` | `qwen/qwen3-reranker-8b` | Search reranker. |
| `WORKER_CONCURRENCY` | `1` | Documents converted at the same time. Each needs about 5 GB at peak: raise `mem_limit` with it. |
| `FIGURE_CONCURRENCY` | `4` | Figure-description requests in flight per document. |
| `MAX_UPLOAD_MB` | `100` | Largest accepted PDF. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `ALLOWED_HOSTS` | `localhost,127.0.0.1,::1` | Host names clients may use (see below); `*` turns the check off. Keep `127.0.0.1`: the healthcheck uses it. |
| `DATA_DIR`, `HOST`, `PORT` | `/data`, `0.0.0.0`, `8000` | Fixed by the image and compose. Change them only when running on the host (`python -m service.api`). |
| `HF_HOME`, `HF_HUB_OFFLINE` | `/opt/models`, `1` | Fixed by the image: models are read locally, never downloaded. |

**What `ALLOWED_HOSTS` blocks:** a request whose `Host` header names another host gets `421 invalid_host`. A browser request from a page of another host (`Origin` header) gets `403 invalid_origin`. These checks stop any web page you visit from reaching the unauthenticated service through DNS rebinding or cross-site form posts. API and MCP clients send no `Origin`.

To reach the service from another container by its service name, add that name to `ALLOWED_HOSTS`.

## 3. Data volume

The compose project's named volume `protocol-service_protocol-data` is mounted at `/data`:

```
/data
  registry.sqlite (+ -wal, -shm)  documents, jobs and related documents (SQLite, WAL mode)
  documents/<doc_id>/             the published result of one PDF (doc_id = SHA-256 of the PDF bytes)
    source.pdf                    the uploaded PDF
    protocol.json / .md / .html   lossless Docling JSON, Markdown, HTML (images referenced from images/)
    protocol.decisions.json       what post-processing changed
    images/  tables/              figure images, one CSV per table
    chunks/                       chunks.jsonl, parents.jsonl, sections.json, tables.jsonl, glossary.json,
                                  stats.json, embeddings.npy, embeddings_meta.json, caches (embeddings,
                                  queries, reranks)
    quality.json  pipeline.json   word coverage / table / figure checks, stage results; the run's summary
    publish.json  logs/           crash-recovery record; pipeline.log and convert.log
  work/<job_id>/                  scratch space of a running job, moved into documents/ atomically when done
  cache/vlm/                      figure descriptions, shared across documents (image + model + prompt)
  cache/batches/                  Docling conversion batches (per PDF hash and converter configuration)
  inbox/                          PDFs placed for ingestion by path (MCP ingest_document)
```

**Rules for the volume:**
- It must be a named volume or another local filesystem. SQLite in WAL mode does not work on NFS/SMB shares or Windows bind mounts.
- Run one service process per volume. The worker, its restart recovery and the in-memory PDF passwords assume it.
- A reprocess builds the new result in `work/` and swaps it in atomically, so the old result is served until then. An interrupted swap is repaired at the next start.

**Copy files out:**

```powershell
docker cp protocol-service:/data/documents/<doc_id>/protocol.md .
```

Or use `GET /documents/{doc_id}/files/{name}`.

### Backup

Stop the service for a consistent copy (SQLite and a possible running job), archive the volume, then start again:

```powershell
New-Item -ItemType Directory -Force backups | Out-Null
docker compose stop
docker run --rm --user 0:0 --entrypoint tar -v protocol-service_protocol-data:/data:ro -v "${PWD}\backups:/backup" protocol-service -czf /backup/protocol-data-$(Get-Date -Format yyyy-MM-dd).tgz -C /data .
docker compose start
```

The archive keeps file ownership (uid 10001). Store it like the documents themselves: it contains every PDF and result.

### Restore

This replaces everything in the volume:

```powershell
docker compose down -v                 # removes the container AND the volume
docker compose up --no-start           # recreates an empty volume and the container, not started
docker run --rm --user 0:0 --entrypoint tar -v protocol-service_protocol-data:/data -v "${PWD}\backups:/backup:ro" protocol-service -xzf /backup/protocol-data-2026-10-12.tgz -C /data
docker compose start
```

At start the worker re-queues any job that was running when the backup was taken. The archive and extract steps were tested by restoring into a scratch volume: registry integrity was ok, every document was present and owned by uid 10001.

## 4. REST API

The interactive documentation, with every route, argument and response, is at http://127.0.0.1:8000/docs; the schema is at `/openapi.json`.

| Route | Purpose |
|---|---|
| `GET /health`, `GET /version` | Health (`503` when the registry or worker is down); pipeline version, fingerprint, packages, models, offline flag. |
| `POST /documents` | Multipart upload: `file` (the PDF), optional `reprocess` (bool), optional `password` (form field only, never in the URL). Answers `{doc_id, job_id, status, duplicate, stale, queued, warnings}`: `202` when a job was queued, `200` otherwise. |
| `GET /documents`, `GET /documents/{doc_id}`, `DELETE /documents/{doc_id}` | List (`status`, `limit`, `offset`); one document (status, version, stale flag, identifiers, quality summary, warnings, files, related and same-text documents, current job with stage and progress); delete (`purge_caches=true` also removes its cached conversion batches and figure descriptions). |
| `POST /documents/{doc_id}/reprocess` | Reprocess from the stored PDF when the result is stale, or always with `force=true`. |
| `GET /jobs`, `GET /jobs/{job_id}`, `GET /documents/{doc_id}/jobs`, `POST /jobs/{job_id}/cancel` | Jobs: state, stage, progress, per-stage seconds, warnings, error type and location. Only a queued job can be cancelled. |
| `GET /documents/{doc_id}/files`, `GET /documents/{doc_id}/files/{name}` | The published files: `protocol.md`, `protocol.json`, `protocol.html`, `protocol.decisions.json`, `quality.json`, `chunks/*.jsonl` and `chunks/*.json`, `images/<file>`. Short names such as `chunks.jsonl`, `sections.json`, `decisions` and `md` also work. |
| `GET /tools` | The 9 protocol tool definitions (`style=anthropic` or `openai`). |
| `POST /documents/{doc_id}/tools/{tool}` | Run a protocol tool on the document: `document_info`, `outline`, `find_sections`, `read_section`, `get_table`, `get_soa`, `search`, `abbreviation`, `quote_check`. The JSON body holds the tool's arguments; each tool has a typed route in `/docs`. See [docs/protocol_api.md](docs/protocol_api.md). |

**Errors** are always JSON `{"type", "message"[, "details"]}`. The main types:

| Status | Types |
|---|---|
| 400 | `bad_request`, `invalid_arguments`, `invalid_id`, `password_required`, `bad_password`, `invalid_pdf`, `empty` |
| 404 | `document_not_found`, `job_not_found`, `not_found`, `unknown_tool`, `file_not_found` |
| 409 | `not_ready`, `document_busy`, `search_unavailable`, `not_cancellable` |
| 413 | `too_large` |
| 415 | `not_pdf` |
| 422 | `validation_error` (never echoes what was sent) |
| 502 | `upstream_error` (OpenRouter failed) |
| 503 | `offline` |
| 500 | `internal_error` (exception type only) |

**Examples** (PowerShell; `curl.exe` ships with Windows):

```powershell
# upload (sends the PDF's figures and chunk texts to OpenRouter unless the service is offline)
curl.exe -F "file=@C:\path\to\protocol.pdf" http://127.0.0.1:8000/documents
# poll the job, then read the document
Invoke-RestMethod http://127.0.0.1:8000/jobs/<job_id>
Invoke-RestMethod http://127.0.0.1:8000/documents/<doc_id>
# run a tool
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/documents/<doc_id>/tools/read_section -ContentType application/json -Body '{"section": "inclusion"}'
# download a file
curl.exe -o protocol.md http://127.0.0.1:8000/documents/<doc_id>/files/protocol.md
```

## 5. MCP

The MCP server offers 12 tools over the same service layer as REST, so the results are identical:

- `list_documents`;
- `get_document`, which shows status and job progress: poll it after ingesting;
- `ingest_document`, taking exactly one of `path` (a PDF in `/data/inbox`) or `data_base64` (the PDF itself);
- the 9 protocol tools, each with a required `doc_id`.

Tool errors come back as results with `isError` and the same JSON body as REST. The server's instructions describe the suggested flow for agents.

**Claude Code:** either over stdio inside the container (no port needed):

```powershell
claude mcp add protocol-service -- docker exec -i protocol-service python -m service.mcp_stdio
```

or over HTTP:

```powershell
claude mcp add --transport http protocol-service http://127.0.0.1:8000/mcp
```

**Claude Desktop:** add to `claude_desktop_config.json` (Windows: `%APPDATA%\Claude\`, macOS: `~/Library/Application Support/Claude/`), then restart Claude Desktop:

```json
{
  "mcpServers": {
    "protocol-service": {
      "command": "docker",
      "args": ["exec", "-i", "protocol-service", "python", "-m", "service.mcp_stdio"]
    }
  }
}
```

**Other MCP clients:** use `http://127.0.0.1:8000/mcp`. It is Streamable HTTP, stateless, answering POST only with JSON responses (no SSE), and accepts the handshake (2025-11-25) and self-contained (2026-07-28) protocol versions. Host and Origin must name an `ALLOWED_HOSTS` entry.

**Notes:**
- **The stdio process runs no worker of its own.** Documents it ingests are queued in the shared registry and converted by the service's worker. It refuses PDF passwords (`password_unsupported_here`), because a password is held in the memory of the process whose worker runs the job: upload encrypted PDFs through REST or HTTP MCP.
- **First call is slow:** the stdio server answers the handshake in about 2–3 s, but the first tool result takes up to about 15 s while the service loads.
- **Ingest by path:** copy the PDF into the volume, then call `ingest_document` with `{"path": "protocol.pdf"}`:
  ```powershell
  docker cp C:\path\to\protocol.pdf protocol-service:/data/inbox/
  ```
- **Large PDFs:** `data_base64` is held in memory (about 134 MB of JSON for a 100 MB PDF), so use the inbox or REST for those.

## 6. Data handling

**What leaves the container, and when.** Only these requests, all to `https://openrouter.ai/api/v1`, with the key as the only header:

| When | What is sent | Model |
|---|---|---|
| Ingest, figures stage | each figure image (PNG crop) not yet in the figure cache, with the description prompt | `FIGURE_MODEL` |
| Ingest, embed stage | each chunk's text (with its section heading and document prefix) not yet in the embedding cache, in batches of 32 | `EMBED_MODEL` |
| `search` tool | the query text (query embedding) | `EMBED_MODEL` |
| `search` tool | the query plus the candidate chunk texts (rerank) | `RERANK_MODEL` |

**Zero data retention:** every one of these requests carries `"provider": {"data_collection": "deny", "zdr": true, "allow_fallbacks": false}`. OpenRouter routes them only to providers that neither collect nor retain data, with no fallback to others.

**Never sent:** the PDF itself, whole documents, file names or anything else.

**Caches:** figure descriptions are keyed by image, model and prompt; embeddings by chunk content; query embeddings and reranks by query and candidate set. A reprocess or a repeated search sends nothing new: the smoke test checks that a forced reprocess sends zero requests.

### Offline mode

With `OPENROUTER_OFFLINE=1` nothing is sent at all:
- figure descriptions and embeddings are skipped, and the document finishes as `done_with_warnings`;
- `search` answers `409 search_unavailable` (no embeddings) or `503 offline` (query not cached);
- every other tool works.

To add the missing parts later, reprocess the document online.

### Nothing else leaves the container

- The models are inside the image and loaded with `HF_HUB_OFFLINE=1`.
- Hugging Face telemetry and FastAPI's OpenTelemetry auto-configuration are off. The MCP SDK's telemetry hook has no exporter.
- The build verifies the models with the network disabled.
- `tests/test_service_zdr.py` checks that the runtime code holds exactly the three OpenRouter call sites above, each with the ZDR block, and that nothing is sent when offline.

### Logs, passwords and deletion

- **Logs and error responses** carry ids, counts and exception types with our own file:line, never document text.
- **PDF passwords** are held in memory until their job starts: never stored, logged or echoed.
- **Deleting a document** (`DELETE /documents/{doc_id}`) removes its record, jobs and folder. The shared caches keep its conversion batches (`cache/batches/<pdf hash>`) and figure descriptions (`cache/vlm`), so a re-upload is fast and sends nothing. To remove every trace, delete with `?purge_caches=true`: that also removes its batches and its figure descriptions, leaving other documents' entries in place. The answer reports what was removed (`"purged": {"batches": n, "figures": n}`).

## 7. Duplicate and version rules

| Situation | Upload answer |
|---|---|
| The same bytes (same `doc_id`) have a queued or running job | that job (`duplicate: true`) |
| A result made by the current pipeline version and fingerprint exists | that result, no job (`duplicate: true`, `job_id: null`) |
| A result made by another version or fingerprint exists | `stale: true`, no job. With `reprocess=true` (upload) or `POST /documents/{id}/reprocess` a reprocess is queued; the old result stays served until the new one is published. |
| No result (new, failed or cancelled) | a new job (`202`) |
| Another document has the same text (text fingerprint) | warning `same text as <doc_id>`, never merged: amendments are near-identical but stay separate |
| Another document has the same NCT or sponsor protocol id but different text | linked as `related` in `GET /documents/{doc_id}` (likely another version or amendment) |

`POST /documents/{id}/reprocess?force=true` reprocesses even a current result.

The fingerprint covers the pipeline version, the settings, the Docling versions, the converter configuration and the model ids, so changing `FIGURE_MODEL`, `EMBED_MODEL` or `RERANK_MODEL` makes existing results stale. Check the running values with `GET /version`.

## 8. Limits

- **No authentication.** The port is published on 127.0.0.1 only. Never expose it without adding authentication in front (see the checklist).
- **One process per data volume, one replica.** `WORKER_CONCURRENCY` documents at a time (default 1); a running job cannot be cancelled, only a queued one.
- **Speed and memory:** measured on a 4-core laptop CPU, Docker with 8 logical CPUs.
  - Conversion takes about 7.5–11 s per page: LZZT, 97 pages, 14 min end to end; Sanofi, 152 pages, about 30 min.
  - Figure descriptions take 70–90 s per document; embeddings 15–20 s.
  - Peak memory is about 5 GB per job while converting (1.7 GB otherwise). The end-to-end test measured 6.0 GB for the whole container with agents querying during a conversion; the 8 GB limit covers that.
  - The converter uses 4 threads per job, so more CPUs only help with more concurrent jobs.
- **Uploads:** at most `MAX_UPLOAD_MB` (100 MB). MCP `data_base64` uploads are held in memory.
- **Lost passwords:** a PDF password is lost if the service restarts before its job starts; the job then fails and the PDF must be uploaded again.
- **Search needs embeddings,** made online at ingest. A document processed offline must be reprocessed online to search it.
- **Chunk ids:** `<protocol id>-A<amendment>-0000` when both are detected, else `doc-<12 hex of the PDF's hash>-0000`. The same PDF gets the same ids on every platform and in every run. After a reprocess with a newer pipeline, an id may cover slightly different text: compare `content_hash`. Fresh conversions on Windows and Linux can differ in tiny model floats in `protocol.json` (relative 1e-5); the text content is the same.
- **Content limits** of the conversion and of the tools (reading order, unruled tables, footnote markers, ...) are listed in [docs/protocol_api.md](docs/protocol_api.md#limits). Tool provenance has section, page and item references, but no bounding boxes.
- **No system fonts in the image.** PDFs with non-embedded fonts render page images with pdfium's built-in fallback fonts; the text layer is unaffected.

## 9. Adding a cloud storage adapter

All file access goes through `service/storage.py`: the abstract class `Storage` and the local implementation `LocalStorage`. The pipeline, registry, worker, REST and MCP never touch paths themselves.

**An S3 / GCS / Azure adapter implements the same methods:**

- **Jobs (local scratch):** `workdir`, `create_workdir`, `remove_workdir`, `list_workdirs`, `remove_orphan_workdirs`, `has_source`, `save_upload`, `seed_reprocess`, `seed_caches`, `write_error_log`, `write_publish_record`. The pipeline needs real paths, so `work/` stays on the worker's local disk; `seed_*` download the stored PDF and caches into it.
- **Publish:** `publish(job_id, doc_id)` uploads the workdir under a fresh prefix, for example `documents/<doc_id>/<job_id>/`, then flips a small pointer object to it. Readers see either the old or the new result, never a mix. `recover()` finishes or removes half-done uploads at start. Old prefixes can be deleted after the flip.
- **Read side:** `document_dir` must return a local folder, because `ProtocolDocument` opens the Docling JSON and chunk files from disk. Mirror the current prefix into a local cache on first access, keyed by the pointer. `resolve_file`, `open_file`, `read_publish_record`, `list_documents` and `delete_document` work on the bucket or the mirror. Keep `safe_relative_name()`'s whitelist.
- **Inbox:** `list_inbox` and `inbox_file` can read an inbox prefix.

**Wiring:**
1. Add a factory, for example `make_storage(config)` selecting by a new `STORAGE_BACKEND` variable.
2. Use it in `ProtocolService.__init__` (`service/app_service.py`), which today falls back to `LocalStorage(config)`.
3. Add the bucket settings to `ServiceConfig` and `.env.example`.

**What stays local:**
- The registry is SQLite: it needs a persistent local disk and a single instance. Moving to several replicas means another registry backend (e.g. Postgres) and a shared job queue.
- `cache/batches` and `cache/vlm` can stay local; they only speed things up. Share them only if several workers convert the same PDFs.

**Tests:** `tests/test_service_core.py` holds the storage contract (atomic swap, crash recovery, safe file lookup). Run the same cases against the new adapter with a local emulator (MinIO, Azurite, fake-gcs-server).

## 10. Before hosting: checklist

- [ ] **Authentication** in front of everything, both REST and `/mcp`: an authenticating reverse proxy (OIDC / OAuth 2), API keys at a gateway, or MCP authorization (the MCP SDK supports OAuth 2.1 resource servers). Nothing in the service checks identity today.
- [ ] **TLS** terminated at the proxy or platform; HTTP only inside the private network.
- [ ] **`ALLOWED_HOSTS`** set to the public host name(s) plus `127.0.0.1` for the healthcheck. Add CORS only if a browser front end needs it, and only for its origin.
- [ ] **Secrets:**
  - `OPENROUTER_API_KEY` comes from the platform's secret store, never baked into an image; `.env` is excluded from the build.
  - Set a spending limit on the key at OpenRouter and rotate it.
  - Confirm that the protocols you will process may be sent to OpenRouter under its zero-data-retention terms; otherwise run with `OPENROUTER_OFFLINE=1` or `FIGURE_MODEL=` (no search then).
- [ ] **Storage:**
  - Either a persistent local volume (one instance, SQLite) or a cloud adapter (section 9).
  - Never a network file share for the registry.
- [ ] **Resource sizing:**
  - At least 4 vCPU and 12 GB RAM for one job at a time (container limit 8 GB); 8 vCPU and 24–32 GB for `WORKER_CONCURRENCY=2` (raise `mem_limit` by about 5 GB per extra job).
  - Disk: the image (3.3 GB) plus about 75 MB per large protocol, plus backups.
- [ ] **Backups** scheduled (section 3), stored as securely as the documents, and a restore tested.
- [ ] **Image hygiene:**
  - Pin the base images by digest and add hashes to `requirements.lock` (`--require-hashes`).
  - Move to a current Python 3.13 patch image and rebuild regularly for OS security updates.
  - After a rebuild, fold any packages pip added on Linux into the lock (`docker run --rm protocol-service pip freeze`, then `docker\lock_report.py`).
- [ ] **One replica only** until the registry and the in-memory passwords move to shared backends.
- [ ] **Request limits** at the proxy: body size (`MAX_UPLOAD_MB`), rate limits on uploads and on `search`, which calls OpenRouter.
- [ ] **Monitoring:** `/health` (it reports registry and worker), container restarts, job failures (`GET /jobs?state=failed`), disk usage of the volume. Logs carry no document text, but review them before shipping them to a third party.
- [ ] **Smoke test** (`scripts/smoke_test.py --base-url https://<host>`) against the hosted service with the public protocols, once authentication is in place (the script would need its credentials added).

## Troubleshooting

- **`RemoteProtocolError` or connection errors right after `docker compose up -d`:** the service is still starting (about 15–30 s). Wait for `docker compose ps` to show `(healthy)`. The smoke test waits up to 3 minutes.
- **Healthcheck fails with 421:** `ALLOWED_HOSTS` lacks `127.0.0.1`.
- **Build fails in the `models` stage with missing shared libraries:** the stage lists every library OpenCV cannot find. Add the Debian packages to `RUNTIME_PACKAGES` in the Dockerfile.
- **Build fails in `context-check`:** a PDF or forbidden file is in the build context. Run `python scripts\check_build_context.py`, and after adding a runtime module run `python -m service.runtime --write-dockerignore`.
- **A job failed:** `GET /jobs/<job_id>` shows the exception type and our file:line. The full traceback stays in the volume:
  ```powershell
  docker exec protocol-service cat /data/work/<job_id>/logs/error.log
  ```
- **`search` returns 409:** the document has no embeddings (processed offline). Reprocess it online.
