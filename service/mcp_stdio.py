"""MCP server over stdio (one client): `python -m service.mcp_stdio [--with-worker]`, e.g. in the container

    docker exec -i <container> python -m service.mcp_stdio

The tools of service.mcp_server over the DATA_DIR of the environment (ServiceConfig.from_env()).

Without --with-worker (the default) this process runs no worker and no recovery: the API process of the same
DATA_DIR runs the one worker, and a second one would "recover" (re-queue) the job the first is running. A document
ingested here is queued in the shared SQLite registry and picked up by that worker, which polls the queue. For the
same reason a PDF password is refused here (password_unsupported_here): it would stay in this process's memory,
out of the worker's reach; encrypted PDFs go through the REST API or the HTTP MCP endpoint.

--with-worker runs the worker (and its startup recovery) in this process and accepts passwords: only for standalone
use, when no API process and no other --with-worker process uses the same DATA_DIR.

stdout carries only MCP JSON-RPC: the SDK's stdio transport writes to a private duplicate of fd 1 and points fd 1
(inherited by child processes) at stderr while it serves, sys.stdout is pointed at sys.stderr as well, and logging
goes to stderr. The service is built in a background thread (the pipeline fingerprint imports Docling, about ten
seconds), so the handshake and tools/list answer at once; the first tool call waits for it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from functools import partial
from typing import Callable

import anyio
from mcp.server.stdio import stdio_server

from service.app_service import ProtocolService, Unavailable
from service.config import ServiceConfig
from service.mcp_server import build_server
from service.worker import describe_exception

log = logging.getLogger("service.mcp_stdio")

START_WAIT = 120.0  # seconds a shutdown waits for a service still being built


class ServiceStarter:
    """Builds and starts the ProtocolService in a background thread; get() waits for it (in a tool call's worker
    thread), stop() stops it once built."""

    def __init__(self, factory: Callable[[], ProtocolService]) -> None:
        self._factory = factory
        self._service: ProtocolService | None = None
        self._failure: str | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._build, name="service-start", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _build(self) -> None:
        try:
            service = self._factory()
            try:
                service.start()
            except BaseException:
                service.stop()
                raise
            self._service = service
            log.info("protocol service ready (worker: %s)", service.health()["worker"])
        except BaseException as exc:  # noqa: BLE001 - reported to every tool call; the message can quote paths
            self._failure, where = describe_exception(exc)
            log.error("protocol service failed to start: %s at %s", self._failure, where or "?")
        finally:
            self._ready.set()

    def get(self) -> ProtocolService:
        self._ready.wait()
        if self._service is None:
            raise Unavailable(f"the service failed to start ({self._failure})", code="not_started")
        return self._service

    def stop(self) -> None:
        if self._thread.is_alive() and not self._ready.wait(START_WAIT):
            log.warning("protocol service still starting after %.0fs; exiting without stopping it", START_WAIT)
            return
        if self._service is not None:
            self._service.stop()


async def serve(config: ServiceConfig, *, with_worker: bool = False) -> None:
    """Serve one MCP client on stdin/stdout until stdin closes."""
    starter = ServiceStarter(lambda: ProtocolService(config, run_worker=with_worker))
    async with stdio_server() as (read_stream, write_stream):
        real_stdout, sys.stdout = sys.stdout, sys.stderr  # stray print()s miss the wire even if fd 1 was not moved
        try:
            server = build_server(starter.get)
            starter.start()
            log.info("MCP stdio server started (pid=%d, worker: %s)", os.getpid(), "yes" if with_worker else "no")
            await server.run(read_stream, write_stream, server.create_initialization_options())
        finally:
            await anyio.to_thread.run_sync(starter.stop)
            sys.stdout = real_stdout
    log.info("MCP stdio server stopped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m service.mcp_stdio",
        description="MCP server of the protocol service over stdio (DATA_DIR and the other settings from the "
                    "environment).")
    parser.add_argument("--with-worker", action="store_true",
                        help="run the job worker (and its startup recovery) in this process and accept PDF "
                             "passwords: only when no API process uses the same DATA_DIR")
    args = parser.parse_args(argv)
    config = ServiceConfig.from_env()
    level = logging.getLevelName(config.log_level)
    logging.basicConfig(level=level if isinstance(level, int) else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        anyio.run(partial(serve, config, with_worker=args.with_worker))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
