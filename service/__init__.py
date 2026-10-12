"""Protocol ingestion service: one pipeline entry point, document identity, storage, jobs, REST and MCP.

The pipeline modules stay flat in the project root (tests, evaluation and research scripts import them);
this package holds only the service layer on top of them:

  config.py     ServiceConfig.from_env(): DATA_DIR, OPENROUTER_OFFLINE, models, worker settings
  contracts.py  PipelineResult / StageResult / job and document states shared by pipeline, registry, worker
  pipeline.py   process_document(): hash -> convert -> post-process -> figures -> save -> chunk -> embed -> quality
  storage.py    the storage layer (local filesystem; a cloud adapter implements the same interface)
  registry.py   SQLite registry of documents and jobs (/data/registry.sqlite), duplicate/version rules
  worker.py     background worker running queued jobs through the pipeline
  app_service.py  ProtocolService: the one service layer REST and MCP both call (identical results)
  api.py        FastAPI REST API (`python -m service.api`; OpenAPI at /docs), MCP mounted at /mcp
  mcp_server.py MCP server (12 tools) over the same service: Streamable HTTP at /mcp
  mcp_stdio.py  MCP over stdio (`python -m service.mcp_stdio`, e.g. through `docker exec -i`)
"""
