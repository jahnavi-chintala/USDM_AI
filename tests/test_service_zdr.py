"""Self-test: every OpenRouter request of the service runtime uses zero-data-retention routing.
`python tests/test_service_zdr.py` (no pytest, no network).

Every request the runtime (service.runtime.runtime_files(): the modules of the Docker image) sends to OpenRouter must
  * go to https://openrouter.ai/api/v1/... and nowhere else;
  * carry exactly the provider block {"data_collection": "deny", "zdr": true, "allow_fallbacks": false}, on every
    attempt (retries included);
  * send only what it needs: the figure image and the prompt; the chunk or query texts; the query and the candidate
    texts (no file paths, no other document text);
  * carry the API key in the Authorization header only, never in a log record, an exception message or a cache file;
and nothing at all may be sent while OPENROUTER_OFFLINE is set. Every such request goes through one path,
openrouter_http.post_json, on one httpx.Client per process that keeps no cookie.

How: the request-building code runs for real; only httpx's network transport (httpx.HTTPTransport.handle_request) is
replaced by a fake OpenRouter that records each request and answers with deterministic vectors / scores / text (and
sets cookies on every answer, as OpenRouter's CDN may: none may come back).
Underneath, a guard on socket connect / sendto / getaddrinfo refuses (and records) any attempt to reach a non-loopback
address, and a request that reaches the transport while OPENROUTER_OFFLINE is set is recorded too: any record fails
the run, even when the code under test swallows the error. The API key is a random fake set in the environment (so the
project's .env is never read). OPENROUTER_OFFLINE=1 is the default of the run; a test that needs the online code path
unsets it inside a block and restores it. All content is synthetic (made-up text).

  0. the guards themselves: raw sockets, name lookups and un-intercepted / offline HTTP requests are refused
  1. inventory: a static scan of the runtime files finds exactly the reviewed OpenRouter call sites (a new one fails
     this test until it is reviewed and added): each a post_json call with, statically, an openrouter.ai URL constant
     and a JSON body that is a dict literal with the exact ZDR block (not modified before the call); post_json sends
     exactly the reviewed request (Authorization the only header) on the one client (a jar that refuses every cookie),
     and any other HTTP call, client or library fails; then mutated copies of the sources show the scan catches each
  2. figure descriptions (figure_vlm.describe_missing -> OpenRouterRunner) on a synthetic document with one picture
  3. embeddings (embed_chunks.embed_texts / embed_document, eval_retrieval.cached_query_vectors)
  4. rerank (retrieve.Retriever.search / rerank -> post_with_retry)
  5. the search tool end to end (protocol_api.ProtocolDocument and service.app_service.ProtocolService) online with
     the transport faked, then offline with empty caches: the offline error and zero requests
  6. offline everywhere: figure descriptions, embed_document, query embeddings and rerank send nothing
  7. retries (429 / 503 / transport error, then 200; and exhausted) resend the same body on every attempt
  8. the shared client: one per process, made once even by concurrent first requests, reused by every call site and
     thread; no cookie kept or sent; none made by importing the modules
Output: counts and booleans only.
"""

import ast
import base64
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from pathlib import Path

os.environ["OPENROUTER_OFFLINE"] = "1"  # the default of the whole run; tests unset it only around online code paths
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
FAKE_KEY = "sk-or-v1-zdrtest-" + secrets.token_hex(16)
os.environ["OPENROUTER_API_KEY"] = FAKE_KEY  # set before any module reads it: the real key / .env is never used
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

ZDR = {"data_collection": "deny", "zdr": True, "allow_fallbacks": False}
ZDR_JSON = json.dumps(ZDR, sort_keys=True)
BASE = "https://openrouter.ai/api/v1/"
DIM = 16  # dimension of the fake embeddings (as tests/test_service_api.py)
DECOY_PATH = "C:/zdr-decoy/private folder/never_sent.pdf"  # a field of the synthetic chunks that must never be sent
ALLOWED_HEADERS = {"host", "accept", "accept-encoding", "connection", "user-agent", "content-length", "content-type",
                   "authorization"}

# ---------------------------------------------------------------- network guard (installed before any other import)

VIOLATIONS: list[str] = []  # network attempts, requests outside a test or while offline: any entry fails the run
_violations_lock = threading.Lock()


def _violation(text: str) -> None:
    with _violations_lock:
        VIOLATIONS.append(text)


def _loopback(host) -> bool:
    if host is None:
        return True  # getaddrinfo(None, port): the local wildcard, no lookup
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    h = str(host).strip().strip("[]").lower()
    if h in ("", "localhost") or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class NetworkBlocked(ConnectionRefusedError):
    """Raised by the guard (and recorded, so code that swallows it still fails the run)."""


def _install_network_guard() -> None:
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_sendto, real_getaddrinfo = socket.socket.sendto, socket.getaddrinfo

    def check(family, address, what: str) -> None:
        if family in (socket.AF_INET, socket.AF_INET6) and isinstance(address, tuple) and address \
                and not _loopback(address[0]):
            _violation(f"{what} to {address[0]}")
            raise NetworkBlocked(f"{what} to a non-loopback address (blocked by tests/test_service_zdr.py)")

    def connect(self, address):
        check(self.family, address, "socket.connect")
        return real_connect(self, address)

    def connect_ex(self, address):
        check(self.family, address, "socket.connect_ex")
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        check(self.family, args[-1] if args else None, "socket.sendto")
        return real_sendto(self, data, *args)

    def getaddrinfo(host, *args, **kwargs):
        if not _loopback(host):
            _violation(f"getaddrinfo {host!r}")
            raise NetworkBlocked("name lookup of a non-loopback host (blocked by tests/test_service_zdr.py)")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = connect, connect_ex, sendto
    socket.getaddrinfo = getaddrinfo


_install_network_guard()

import httpx  # noqa: E402  (after the guard)
import numpy as np  # noqa: E402

# ---------------------------------------------------------------- fake OpenRouter at httpx's transport layer

_current: dict = {"fake": None}


def _offline_env() -> bool:
    return os.environ.get("OPENROUTER_OFFLINE", "").strip().lower() in ("1", "true", "yes", "on")


def _transport(self, request):
    """httpx.HTTPTransport.handle_request: every request any httpx client sends (httpx.post included) ends here."""
    if _offline_env():
        _violation(f"HTTP request to {request.url.host} while OPENROUTER_OFFLINE is set")
        raise httpx.ConnectError("request while OPENROUTER_OFFLINE is set (test_service_zdr)", request=request)
    fake = _current["fake"]
    if fake is None:
        _violation(f"HTTP request to {request.url.host} outside an intercepted block")
        raise httpx.ConnectError("no network in tests/test_service_zdr.py", request=request)
    return fake.handle(request, self)


async def _async_transport(self, request):
    _violation(f"async HTTP request to {request.url.host}")
    raise httpx.ConnectError("no network in tests/test_service_zdr.py", request=request)


httpx.HTTPTransport.handle_request = _transport
httpx.AsyncHTTPTransport.handle_async_request = _async_transport

CLIENTS: list[int] = []  # the thread that made each httpx.Client of this process, in order


def _counting_init(real):
    def __init__(self, *args, **kwargs):
        CLIENTS.append(threading.get_ident())
        real(self, *args, **kwargs)

    return __init__


httpx.Client.__init__ = _counting_init(httpx.Client.__init__)


def _memoized_ssl_context(real):
    """Every new Client's transport loads the CA bundle (about 1 s on Windows): httpx.post in the guard tests, the
    controls and the fresh shared clients of section 8. No TLS connection is ever made here, so the context is built
    once and shared (the request path is otherwise unchanged)."""
    made: dict = {}
    lock = threading.Lock()

    def create_ssl_context(verify=True, cert=None, trust_env=True):
        if not isinstance(verify, bool) or cert is not None:
            return real(verify=verify, cert=cert, trust_env=trust_env)
        with lock:
            if (verify, trust_env) not in made:
                made[(verify, trust_env)] = real(verify=verify, cert=cert, trust_env=trust_env)
            return made[(verify, trust_env)]

    return create_ssl_context


import httpx._transports.default as _httpx_default  # noqa: E402

_httpx_default.create_ssl_context = _memoized_ssl_context(_httpx_default.create_ssl_context)


def fake_vector(text: str) -> np.ndarray:
    seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:8], 16)
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class Captured:
    """One request as it left httpx: method, URL parts, headers (lower-case names), raw body and parsed JSON; the
    transport (one per client) and the thread that sent it."""

    def __init__(self, request, transport=None) -> None:
        self.transport, self.thread = transport, threading.get_ident()
        self.method = request.method
        self.url = str(request.url)
        self.scheme, self.host, self.port, self.path = (request.url.scheme, request.url.host, request.url.port,
                                                        request.url.path)
        self.query = request.url.query
        self.headers = {k.lower(): v for k, v in request.headers.multi_items()}
        self.header_count = len(request.headers.multi_items())
        self.raw = bytes(request.content)
        self.body = json.loads(self.raw) if self.raw else None


# On every fake answer, cookies like those of OpenRouter's CDN: a client that kept them would send them back.
SET_COOKIES = [("set-cookie", "__cf_bm=made-up-cdn-token; Path=/; Domain=.openrouter.ai; Secure; HttpOnly"),
               ("set-cookie", "_cfuvid=made-up-visitor-id; Path=/; Secure; HttpOnly; SameSite=None")]


class FakeOpenRouter:
    """Records every request; answers by path. `script`: per request in order, "ok", an HTTP status (int) or
    "connect_error"; after the script, "ok"."""

    def __init__(self, script=()) -> None:
        self.requests: list[Captured] = []
        self.script = list(script)
        self.lock = threading.Lock()

    def handle(self, request, transport=None):
        cap = Captured(request, transport)
        with self.lock:
            self.requests.append(cap)
            step = self.script.pop(0) if self.script else "ok"
        if step == "connect_error":
            raise httpx.ConnectError("simulated connection reset", request=request)
        if step != "ok":
            return httpx.Response(int(step), text=f"simulated upstream status {step}", request=request,
                                  headers=SET_COOKIES)
        body = cap.body or {}
        if cap.path.endswith("/embeddings"):
            data = [{"object": "embedding", "index": i, "embedding": [float(x) for x in fake_vector(t)]}
                    for i, t in enumerate(body["input"])]
            answer = {"data": data[::-1], "model": body.get("model")}
        elif cap.path.endswith("/rerank"):
            answer = {"results": [{"index": i, "relevance_score": round(1.0 / (i + 1), 4)}
                                  for i in range(len(body["documents"]))]}
        elif cap.path.endswith("/chat/completions"):
            answer = {"choices": [{"message": {"role": "assistant",
                                               "content": "A made-up chart with one horizontal line."}}],
                      "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
        else:
            return httpx.Response(404, text="unknown path", request=request, headers=SET_COOKIES)
        return httpx.Response(200, json=answer, request=request, headers=SET_COOKIES)

    def paths(self) -> list[str]:
        return [r.path for r in self.requests]


@contextmanager
def intercept(fake: FakeOpenRouter):
    assert _current["fake"] is None, "nested intercept"
    _current["fake"] = fake
    try:
        yield fake
    finally:
        _current["fake"] = None


class env:
    """Temporarily set (value) or remove (None) environment variables."""

    def __init__(self, **values) -> None:
        self.values, self.old = values, {}

    def __enter__(self):
        for k, v in self.values.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def online() -> env:
    """The online code path: OPENROUTER_OFFLINE unset inside the block, restored after it."""
    return env(OPENROUTER_OFFLINE=None)


@contextmanager
def no_sleep():
    real, slept = time.sleep, []
    time.sleep = lambda s: slept.append(s)
    try:
        yield slept
    finally:
        time.sleep = real


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
        except Exception:  # noqa: BLE001
            text = str(record.msg)
        if record.exc_info:
            text += "".join(traceback.format_exception(*record.exc_info))
        self.lines.append(f"{record.name} {text} {record.args!r}")


@contextmanager
def capture_logs():
    root, cap = logging.getLogger(), LogCapture()
    old = root.level
    root.addHandler(cap)
    root.setLevel(logging.DEBUG)
    try:
        yield cap
    finally:
        root.removeHandler(cap)
        root.setLevel(old)


@contextmanager
def temp_dir():
    tmp = Path(tempfile.mkdtemp(prefix="zdr_test_"))
    try:
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def path_forms(path: Path) -> list[bytes]:
    forms = {str(path), path.as_posix(), str(path.resolve()), path.resolve().as_posix()}
    forms |= {json.dumps(f)[1:-1] for f in forms}
    return [f.encode("utf-8") for f in forms]


def shared_transport(url: str):
    """The transport the process's shared client (openrouter_http) uses for `url`; None before its first request."""
    import openrouter_http

    shared = openrouter_http._shared
    if shared is None or shared[0] != os.getpid():
        return None
    return shared[1]._transport_for_url(httpx.URL(url))


def assert_openrouter(req: Captured, path: str) -> None:
    """URL, provider block, key placement and headers of one captured request; sent by the shared client."""
    assert req.transport is not None and req.transport is shared_transport(req.url), "not sent by the shared client"
    assert "cookie" not in req.headers and "cookie2" not in req.headers, "a cookie was sent"
    assert req.method == "POST", req.method
    assert req.scheme == "https" and req.host == "openrouter.ai" and req.port in (None, 443), (req.scheme, req.host)
    assert req.path == path and not req.query, (req.path, req.query)
    assert isinstance(req.body, dict) and "provider" in req.body, sorted(req.body or {})
    assert json.dumps(req.body["provider"], sort_keys=True) == ZDR_JSON, req.body["provider"]
    assert req.headers.get("authorization") == f"Bearer {FAKE_KEY}", "Authorization header"
    assert sum(FAKE_KEY in v for v in req.headers.values()) == 1, "the key in another header"
    assert FAKE_KEY not in req.url and FAKE_KEY.encode() not in req.raw, "the key in the URL or body"
    assert set(req.headers) <= ALLOWED_HEADERS and req.header_count == len(req.headers), sorted(req.headers)
    assert DECOY_PATH.encode() not in req.raw and b"zdr-decoy" not in req.raw, "a file path was sent"


def assert_key_hidden(logs: LogCapture | None = None, texts=(), folders=()) -> None:
    for t in texts:
        assert FAKE_KEY not in t, "the key in an exception message or result"
    if logs is not None:
        assert not any(FAKE_KEY in line for line in logs.lines), "the key in a log record"
    for folder in folders:
        for p in Path(folder).rglob("*"):
            if p.is_file():
                assert FAKE_KEY.encode() not in p.read_bytes(), f"the key in a written file ({p.name})"


def expect_error(fn, cls):
    try:
        fn()
    except cls as exc:
        assert FAKE_KEY not in str(exc) and FAKE_KEY not in repr(exc), "the key in an exception message"
        return exc
    raise AssertionError(f"expected {cls.__name__}")


# ---------------------------------------------------------------- synthetic fixtures (made-up content)

def synthetic_chunks(n: int = 3) -> list[dict]:
    out = []
    for i in range(n):
        text = f"Made-up passage {i}: the zorblat dosing schedule number {i} of the fictional Quoxa study."
        embed_text = f"Fictional protocol QX-{i}\nSection {i} Zorblat dosing\n{text}"
        out.append({"chunk_id": f"zc{i:03d}", "text": text, "embed_text": embed_text,
                    "content_hash": hashlib.sha256(embed_text.encode("utf-8")).hexdigest()[:16],
                    "source_sha256": "0" * 16, "headings": [f"Section {i}"], "pages": [i + 1], "page_start": i + 1,
                    "doc_item_refs": [f"#/texts/{i}"], "chunk_type": "text", "source_path": DECOY_PATH})
    return out


def write_chunks(folder: Path, chunks: list[dict]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "chunks.jsonl"
    path.write_text("\n".join(json.dumps(c) for c in chunks) + "\n", encoding="utf-8")
    return path


def build_retriever_folder(folder: Path, chunks: list[dict] | None = None) -> Path:
    """chunks.jsonl + fake embeddings in embed_chunks.py's format: what retrieve.Retriever loads."""
    chunks = chunks or synthetic_chunks()
    path = write_chunks(folder, chunks)
    np.save(folder / "embeddings.npy", np.asarray([fake_vector(c["embed_text"]) for c in chunks], dtype=np.float32))
    (folder / "embeddings_meta.json").write_text(json.dumps({
        "model": "qwen/qwen3-embedding-8b", "dimensions": DIM, "normalized": True,
        "query_instruction": "Made-up instruction for a test", "chunk_ids": [c["chunk_id"] for c in chunks]}),
        encoding="utf-8")
    return path


def figure_doc(ocr_text: str = "Zorblat cohort label QX7"):
    """A one-page synthetic document with one unclassified picture (a line on white) and its OCR text."""
    from docling_core.types.doc import (BoundingBox, CoordOrigin, DocItemLabel, DoclingDocument, ImageRef,
                                        ProvenanceItem, Size)
    from PIL import Image

    def prov():
        return ProvenanceItem(page_no=1, bbox=BoundingBox(l=100, t=600, r=400, b=400,
                                                          coord_origin=CoordOrigin.BOTTOMLEFT), charspan=(0, 0))

    d = DoclingDocument(name="zdr-figure-fixture")
    d.add_page(page_no=1, size=Size(width=612, height=792))
    img = Image.new("RGB", (120, 80), (255, 255, 255))
    for x in range(10, 110):
        img.putpixel((x, 40), (0, 0, 0))
    pic = d.add_picture(image=ImageRef.from_pil(img, dpi=72), prov=prov())
    d.add_text(DocItemLabel.TEXT, ocr_text, prov=prov(), parent=pic)
    return d


def figure_image():
    d = figure_doc()
    return d.pictures[0].get_image(d).convert("RGB")


# ---------------------------------------------------------------- 0. the guards themselves


def test_guards_block_network() -> None:
    before = len(VIOLATIONS)

    def provoked(fn, cls, n: int = 1) -> None:
        start = len(VIOLATIONS)
        expect_error(fn, cls)
        assert len(VIOLATIONS) == start + n, VIOLATIONS[start:]

    def raw_connect() -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("192.0.2.1", 443))  # TEST-NET-1: refused before any packet
        finally:
            s.close()

    provoked(raw_connect, NetworkBlocked)
    provoked(lambda: socket.create_connection(("192.0.2.1", 443), timeout=1), NetworkBlocked)
    provoked(lambda: socket.getaddrinfo("openrouter.ai", 443), NetworkBlocked)
    provoked(lambda: httpx.post(BASE + "embeddings", json={}), httpx.ConnectError)  # offline (the default)
    with online():
        provoked(lambda: httpx.post(BASE + "embeddings", json={}), httpx.ConnectError)  # outside intercept()
        with intercept(FakeOpenRouter()) as fake:  # online and intercepted: answered by the fake, no violation
            assert httpx.post(BASE + "rerank", json={"documents": []}).status_code == 200 and len(fake.requests) == 1
    with intercept(FakeOpenRouter()) as fake:  # intercepted but OPENROUTER_OFFLINE=1: a violation, never answered
        provoked(lambda: httpx.post(BASE + "rerank", json={"documents": []}), httpx.ConnectError)
        assert len(fake.requests) == 0
    assert len(VIOLATIONS) == before + 6, VIOLATIONS[before:]
    del VIOLATIONS[before:]  # provoked on purpose
    a, b = socket.socketpair()  # loopback stays usable (asyncio's self-pipe on Windows)
    a.close(), b.close()


# ---------------------------------------------------------------- 1. inventory of the runtime's OpenRouter call sites

# (file, scope, event): every network import / HTTP call / URL constant / OpenRouter client / provider block of the
# runtime files. A new entry must be reviewed (does it carry the ZDR provider block? does it go to openrouter.ai?)
# and then added here.
EXPECTED_INVENTORY = sorted([
    ("embed_chunks.py", "<module>", "import:openrouter_http.post_json"),
    ("embed_chunks.py", "<module>", "url:https://openrouter.ai/api/v1/embeddings"),
    ("embed_chunks.py", "embed_texts", "call:openrouter_http.post_json"),
    ("embed_chunks.py", "embed_texts", "provider:zdr"),
    ("figure_vlm.py", "OpenRouterRunner", "url:https://openrouter.ai/api/v1/chat/completions"),
    ("figure_vlm.py", "OpenRouterRunner.ask", "import:openrouter_http.post_json"),
    ("figure_vlm.py", "OpenRouterRunner.ask", "call:openrouter_http.post_json"),
    ("figure_vlm.py", "OpenRouterRunner.ask", "provider:zdr"),
    ("figure_vlm.py", "describe_missing", "call:OpenRouterRunner"),
    ("figure_vlm.py", "main", "call:OpenRouterRunner"),  # the command line (--openrouter): the same client
    ("openrouter_http.py", "_client", "import:httpx"),
    ("openrouter_http.py", "_client", "call:httpx.Client"),  # the one client: shared_client_problems()
    ("retrieve.py", "<module>", "url:https://openrouter.ai/api/v1/rerank"),
    ("retrieve.py", "post_with_retry", "import:httpx"),  # httpx.TransportError, retried
    ("retrieve.py", "post_with_retry", "import:openrouter_http.post_json"),
    ("retrieve.py", "post_with_retry", "call:openrouter_http.post_json"),
    ("retrieve.py", "Retriever.rerank", "call:post_with_retry"),
    ("retrieve.py", "Retriever.rerank", "provider:zdr"),
])
SHARED = "openrouter_http.py"  # the one request path: post_json on the process's one client
REVIEWED_POST = "openrouter_http.post_json"
NET_MODULES = ("httpx", "httpcore", "requests", "urllib.request", "urllib3", "http.client", "aiohttp", "socket", "ssl",
               "openai", "anthropic", "websockets", "websocket", "pycurl", "ftplib", "smtplib", "xmlrpc.client", "grpc",
               "huggingface_hub.inference", "litellm", "openrouter_http")
REQUEST_FUNCS = {"post_with_retry", "OpenRouterRunner"}
MUTATORS = {"update", "pop", "popitem", "setdefault", "clear", "__setitem__", "__delitem__"}


def _is_net(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in NET_MODULES)


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _root_name(node) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _zdr_literal(node) -> bool:
    try:
        return json.dumps(ast.literal_eval(node), sort_keys=True) == ZDR_JSON
    except Exception:  # noqa: BLE001 - not a literal
        return False


class _Scanner(ast.NodeVisitor):
    """Network imports, HTTP calls, URL constants, OpenRouter client calls and provider blocks of one module
    (one pass in source order: an import is seen before the calls that use it)."""

    def __init__(self, rel: str, tree: ast.Module) -> None:
        self.rel, self.tree = rel, tree
        self.names: list[str] = []
        self.nodes: list[ast.AST] = []
        self.events: list[tuple[str, str, str]] = []
        self.lines: list[int] = []
        self.net_calls: list[tuple[str, ast.Call, ast.AST | None, ast.AST | None]] = []
        self.calls_by_name: dict[str, list] = {}
        self.docstrings: set[int] = set()
        self.aliases: dict[str, str] = {}  # bound name -> dotted module path

    def scope(self) -> str:
        return ".".join(self.names) or "<module>"

    def _enclosing(self, kind) -> ast.AST | None:
        return next((n for n in reversed(self.nodes) if isinstance(n, kind)), None)

    def _add(self, event: str, node: ast.AST) -> None:
        self.events.append((self.rel, self.scope(), event))
        self.lines.append(getattr(node, "lineno", 0))

    def _docstring(self, node) -> None:
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            self.docstrings.add(id(body[0].value))

    def visit_Module(self, node: ast.Module) -> None:
        self._docstring(node)
        self.generic_visit(node)

    def _push(self, node) -> None:
        self._docstring(node)
        self.names.append(node.name)
        self.nodes.append(node)
        self.generic_visit(node)
        self.names.pop()
        self.nodes.pop()

    visit_ClassDef = visit_FunctionDef = visit_AsyncFunctionDef = _push

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            if a.asname:
                self.aliases[a.asname] = a.name
            else:
                self.aliases.setdefault(a.name.split(".")[0], a.name.split(".")[0])
            if _is_net(a.name):
                self._add(f"import:{a.name}", node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and node.level == 0:
            for a in node.names:
                self.aliases[a.asname or a.name] = f"{node.module}.{a.name}"
            if node.module == SHARED[:-3]:  # name by name: post_json is its only reviewed entry (not the client)
                for a in node.names:
                    self._add(f"import:{node.module}.{a.name}", node)
                return
            for full in [node.module] + [f"{node.module}.{a.name}" for a in node.names]:
                if _is_net(full):
                    self._add(f"import:{full}", node)
                    break

    def visit_Call(self, node: ast.Call) -> None:
        d = _dotted(node.func)
        fn = self._enclosing((ast.FunctionDef, ast.AsyncFunctionDef))
        cls = self._enclosing(ast.ClassDef)
        if d:
            root, _, rest = d.partition(".")
            last = d.rsplit(".", 1)[-1]
            self.calls_by_name.setdefault(last, []).append((node, fn, cls))
            full = self.aliases.get(root)
            fq = full + ("." + rest if rest else "") if full is not None else d
            if full is not None and _is_net(fq):
                self._add(f"call:{fq}", node)
                self.net_calls.append((fq, node, fn, cls))
            if fq in ("importlib.import_module", "__import__") or d == "__import__":
                arg = node.args[0] if node.args else None
                if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                    self._add("import:<dynamic, not reviewable>", node)
                elif _is_net(arg.value):
                    self._add(f"import:{arg.value}", node)
            if last in REQUEST_FUNCS:
                self._add(f"call:{last}", node)
            if last == "describe_missing" and any(k.arg == "runner" for k in node.keywords):
                self._add("describe_missing:runner= (production must use the OpenRouter client)", node)
            if last == "dict" and any(k.arg == "provider" for k in node.keywords):
                v = next(k.value for k in node.keywords if k.arg == "provider")
                self._add("provider:zdr" if _zdr_literal(v) else f"provider:NOT ZDR {ast.unparse(v)}", node)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self.docstrings and (
                re.search(r"openrouter\.ai", node.value, re.I) or re.search(r"\b(?:https?|wss?)://", node.value)):
            self._add(f"url:{node.value}", node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and k.value == "provider":
                self._add("provider:zdr" if _zdr_literal(v) else f"provider:NOT ZDR {ast.unparse(v)}", node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)) and isinstance(node.slice, ast.Constant) \
                and node.slice.value in ("provider", "zdr", "data_collection", "allow_fallbacks"):
            self._add(f"provider:assigned [{node.slice.value!r}]", node)
        self.generic_visit(node)


# A module none of whose source contains one of these (lower-cased) can produce no event above: every rule needs the
# imported module's name, an OpenRouter function name, a URL, a provider key or an import function in the source.
SCAN_TRIGGERS = ("openrouter", "://", "provider", "zdr", "data_collection", "allow_fallbacks", "post_with_retry",
                 "describe_missing", "embed_document", "embed_texts", "cached_query_vectors", "httpx", "httpcore",
                 "requests", "urllib", "http.client", "from http import", "import http", "aiohttp", "socket", "ssl",
                 "openai", "anthropic", "websocket",
                 "pycurl", "ftplib", "smtplib", "xmlrpc", "grpc", "huggingface_hub", "litellm", "__import__",
                 "import_module", "dict(")


_SCANS: dict[tuple[str, str], _Scanner] = {}  # (file, source) -> its scan (read-only once made)
_RUNTIME_FILES: list[str] = []  # service.runtime.runtime_files(), listed once (it parses every runtime module)


def scan_runtime(overrides: dict[str, str] | None = None) -> tuple[list[_Scanner], list[str]]:
    """(scans of the runtime modules that can hold an event, every runtime module checked). `overrides`: file ->
    source text scanned instead of the file's (the mutation checks; nothing is written)."""
    from service.runtime import runtime_files

    if not _RUNTIME_FILES:
        _RUNTIME_FILES.extend(runtime_files())
    scans, checked = [], []
    for rel in _RUNTIME_FILES:
        if rel.endswith(".py"):
            checked.append(rel)
            source = (overrides or {}).get(rel) or (ROOT / rel).read_text(encoding="utf-8")
            low = source.lower()
            if not any(t in low for t in SCAN_TRIGGERS):
                continue
            if (rel, source) not in _SCANS:
                tree = ast.parse(source, filename=rel)
                s = _Scanner(rel, tree)
                s.visit(tree)
                _SCANS[(rel, source)] = s
            scans.append(_SCANS[(rel, source)])
    return scans, checked


def _params(fn) -> list[str]:
    a = fn.args
    return [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]


def _assigned(scope_node, name: str) -> list:
    """Values assigned to `name`: anywhere in a function, or in the own body of a module / class (None for an
    assignment that cannot be resolved, such as += or a loop target)."""
    if isinstance(scope_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        nodes = list(ast.walk(scope_node))
    else:
        nodes = list(scope_node.body)
    out = []
    for n in nodes:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id == name:
                    out.append(n.value)
                elif isinstance(t, (ast.Tuple, ast.List)) and any(isinstance(e, ast.Name) and e.id == name
                                                                   for e in t.elts):
                    out.append(None)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name and n.value:
            out.append(n.value)
        elif isinstance(n, (ast.AugAssign, ast.NamedExpr)) and isinstance(n.target, ast.Name) and n.target.id == name:
            out.append(None)
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and name in {
                x.id for x in ast.walk(n.target) if isinstance(x, ast.Name)}:
            out.append(None)
        elif isinstance(n, (ast.With, ast.AsyncWith)) and any(
                i.optional_vars is not None and name in {x.id for x in ast.walk(i.optional_vars)
                                                         if isinstance(x, ast.Name)} for i in n.items):
            out.append(None)
    return out


def _mutated(fn, name: str) -> bool:
    for n in ast.walk(fn):
        if isinstance(n, ast.Subscript) and isinstance(n.ctx, (ast.Store, ast.Del)) and _root_name(n.value) == name:
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS \
                and _root_name(n.func.value) == name:
            return True
        if isinstance(n, ast.Delete) and any(_root_name(t) == name for t in n.targets):
            return True
    return False


def _resolve(expr, scan: _Scanner, fn, cls, scans: list[_Scanner], depth: int = 0) -> list | None:
    """The literal values (Constant / Dict nodes) `expr` can take, following local and module assignments, self.X
    class attributes and, for a function parameter, the arguments of every call of that function in the runtime
    files; None when that cannot be established statically."""
    if expr is None or depth > 5:
        return None
    if isinstance(expr, (ast.Constant, ast.Dict)):
        return [expr]
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self" \
            and cls is not None:
        vals = _assigned(cls, expr.attr)
        return vals if vals and all(isinstance(v, ast.Constant) for v in vals) else None
    if not isinstance(expr, ast.Name):
        return None
    name = expr.id
    if fn is not None and name in _params(fn):
        return _resolve_param(fn, name, scans, depth)
    if fn is not None:
        vals = _assigned(fn, name)
        if vals:
            if _mutated(fn, name) or None in vals:
                return None
            out = []
            for v in vals:
                r = _resolve(v, scan, fn, cls, scans, depth + 1)
                if r is None:
                    return None
                out += r
            return out
    vals = _assigned(scan.tree, name)
    return vals if vals and all(isinstance(v, ast.Constant) for v in vals) else None


def _resolve_param(fn, name: str, scans: list[_Scanner], depth: int) -> list | None:
    a = fn.args
    positional = [p.arg for p in a.posonlyargs + a.args]
    offset = 1 if positional and positional[0] in ("self", "cls") else 0
    callers = [(s, c, f, k) for s in scans for (c, f, k) in s.calls_by_name.get(fn.name, [])]
    if not callers:
        return None
    out = []
    for s, call, cfn, ccls in callers:
        arg = next((kw.value for kw in call.keywords if kw.arg == name), None)
        if arg is None and name in positional:
            idx = positional.index(name) - offset
            if 0 <= idx < len(call.args) and not any(isinstance(x, ast.Starred) for x in call.args[:idx + 1]):
                arg = call.args[idx]
        if arg is None:
            return None  # a default value or **kwargs: not a reviewed literal
        r = _resolve(arg, s, cfn, ccls, scans, depth + 1)
        if r is None:
            return None
        out += r
    return out


def _keyword(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _is_openrouter_url(v) -> bool:
    return isinstance(v, ast.Constant) and isinstance(v.value, str) and v.value.startswith(BASE)


def _is_zdr_body(v) -> bool:
    if not isinstance(v, ast.Dict) or None in v.keys:  # a ** spread could override the provider block
        return False
    provider = [val for k, val in zip(v.keys, v.values) if isinstance(k, ast.Constant) and k.value == "provider"]
    return len(provider) == 1 and _zdr_literal(provider[0])


def _arg(call: ast.Call, pos: int, name: str):
    return call.args[pos] if len(call.args) > pos else _keyword(call, name)


def site_problems(scans: list[_Scanner]) -> list[str]:
    """Each HTTP call of the runtime is a post_json call with an openrouter.ai URL constant and a ZDR dict-literal
    body (resolved through post_with_retry's parameters to its callers); any other (httpx.post, another client or
    library, the shared client itself) is a problem. What post_json sends: shared_client_problems()."""
    problems = []
    for scan in scans:
        for fq, call, fn, cls in scan.net_calls:
            where = f"{scan.rel}:{call.lineno}"
            if scan.rel == SHARED and fq == "httpx.Client":
                continue  # the one client: shared_client_problems()
            if fq != REVIEWED_POST:
                problems.append(f"{where}: {fq} is not the reviewed request function {REVIEWED_POST}")
                continue
            if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
                problems.append(f"{where}: *args / **kwargs, not statically reviewable")
                continue
            for label, expr, ok in (("url", _arg(call, 0, "url"), _is_openrouter_url),
                                    ("body", _arg(call, 1, "body"), _is_zdr_body)):
                values = _resolve(expr, scan, fn, cls, scans)
                if not values or not all(ok(v) for v in values):
                    problems.append(f"{where}: {label} not statically a reviewed literal")
    return problems + shared_client_problems(scans)


# openrouter_http.py as reviewed: its one request (Authorization the only header), its one client (a cookie jar that
# refuses every cookie, nothing else configured: as httpx.post) and every call it makes, so that anything added (a
# header, an event hook, a mount, another client) fails until reviewed.
REVIEWED_REQUEST = "_client().post(url, json=body, timeout=timeout, headers={'Authorization': f'Bearer {key}'})"
REVIEWED_CLIENT = "httpx.Client(cookies=CookieJar(policy=_NoCookies()))"
SHARED_CALLS = sorted(["threading.Lock", "os.getpid", "os.getpid", "os.getpid", "_client", "_client().post",
                       "httpx.Client", "CookieJar", "_NoCookies"])


def shared_client_problems(scans: list[_Scanner]) -> list[str]:
    scan = next((s for s in scans if s.rel == SHARED), None)
    if scan is None:
        return [f"{SHARED} not scanned (no longer a runtime file?)"]
    tree, problems = scan.tree, []
    calls = sorted(ast.unparse(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call))
    if calls != SHARED_CALLS:
        problems.append(f"{SHARED}: not the reviewed calls: {calls}")
    post = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "post_json"), None)
    a = post.args if post else None
    if post is None or _params(post) != ["url", "body", "key", "timeout"] or a.vararg or a.kwarg or a.defaults \
            or any(a.kw_defaults):
        problems.append(f"{SHARED}: not post_json(url, body, key, timeout)")
    elif [ast.unparse(n) for n in ast.walk(post) if isinstance(n, ast.Call)
          and isinstance(n.func, ast.Attribute)] != [REVIEWED_REQUEST]:
        problems.append(f"{SHARED}: post_json does not send the reviewed request")
    if [(fq, ast.unparse(c)) for fq, c, _, _ in scan.net_calls] != [("httpx.Client", REVIEWED_CLIENT)]:
        problems.append(f"{SHARED}: not the one reviewed client")
    policy = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "_NoCookies"), None)
    methods = {n.name: n.body for n in (policy.body if policy else []) if isinstance(n, ast.FunctionDef)}
    for name in ("set_ok", "return_ok", "domain_return_ok", "path_return_ok"):
        body = methods.get(name, [])
        if not (len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Constant)
                and body[0].value.value is False):
            problems.append(f"{SHARED}: _NoCookies.{name} does not refuse")
    if any(isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, (ast.Store, ast.Del))
           for n in ast.walk(tree)):
        problems.append(f"{SHARED}: sets an attribute or item (of the client?)")
    return problems


def inventory_problems(scans: list[_Scanner]) -> list[str]:
    """Events not reviewed (or reviewed and no longer found), then the site problems: empty when all is reviewed."""
    found = sorted(e for s in scans for e in s.events)
    lines = {e: f"{e[0]}:{ln}" for s in scans for e, ln in zip(s.events, s.lines)}
    unexpected = [f"not reviewed: {lines[e]} {e[1]} {e[2]}" for e in sorted(set(found))
                  if found.count(e) > EXPECTED_INVENTORY.count(e)]
    missing = [f"no longer found: {e}" for e in sorted(set(EXPECTED_INVENTORY))
               if EXPECTED_INVENTORY.count(e) > found.count(e)]
    return unexpected + missing + site_problems(scans)


def test_inventory_of_openrouter_call_sites() -> None:
    scans, checked = scan_runtime()
    assert {"figure_vlm.py", "embed_chunks.py", "retrieve.py", "eval_retrieval.py", "protocol_api.py",
            "convert_protocol.py", "service/pipeline.py", SHARED} <= {s.rel for s in scans}, sorted(s.rel for s in scans)
    assert {"service/app_service.py", "service/api.py", "service/mcp_server.py"} <= set(checked) \
        and len(checked) >= 30, len(checked)
    assert inventory_problems(scans) == [], inventory_problems(scans)
    # the pipeline's figure stage calls describe_missing without a runner (so the OpenRouter client is used) and
    # the embed stage calls embed_document: the request code tested below is the code the service runs
    calls = {(s.rel, name) for s in scans for name in s.calls_by_name}
    assert ("convert_protocol.py", "describe_missing") in calls and ("service/pipeline.py", "embed_document") in calls
    assert ("protocol_api.py", "cached_query_vectors") in calls and ("eval_retrieval.py", "embed_texts") in calls


_ZDR_SRC = '{"data_collection": "deny", "zdr": True, "allow_fallbacks": False}'
# (what, file, text replaced (None: appended), replacement): each must make inventory_problems() non-empty.
MUTATIONS = [
    ("a modified ZDR block", "figure_vlm.py", '"zdr": True', '"zdr": False'),
    ("no ZDR block", "embed_chunks.py", f'"provider": {_ZDR_SRC}}}', '"n": 1}'),
    ("the block changed before the call", "retrieve.py", "            resp = post_with_retry(",
     '            body["provider"] = {}\n            resp = post_with_retry('),
    ("another URL", "retrieve.py", '"https://openrouter.ai/api/v1/rerank"', '"https://example.invalid/api/v1/rerank"'),
    ("an extra header", SHARED, '{"Authorization": f"Bearer {key}"}', '{"Authorization": f"Bearer {key}", "X-A": "1"}'),
    ("a header set on the client", SHARED, "    return shared[1]\n",
     '    shared[1].headers["X-A"] = "1"\n    return shared[1]\n'),
    ("cookies kept (the default jar)", SHARED, "httpx.Client(cookies=CookieJar(policy=_NoCookies()))", "httpx.Client()"),
    ("a cookie policy that keeps them", SHARED, "bool:\n        return False", "bool:\n        return True"),
    ("a second client in the module", SHARED, None, "\n\ndef _other():\n    import httpx\n\n    return httpx.Client()\n"),
    ("a second client elsewhere", "embed_chunks.py", "            resp = post_json(URL, body, key, timeout=120)",
     "            import httpx\n            resp = httpx.Client().post(URL, json=body, timeout=120)"),
    ("httpx.post again", "figure_vlm.py", "        resp = post_json(self.URL, body, self.key, timeout=300)",
     '        import httpx\n        resp = httpx.post(self.URL, json=body, timeout=300, headers={"Authorization": "x"})'),
    ("a new call site", "retrieve.py", None,
     f'\n\ndef _made_up(key):\n    from openrouter_http import post_json\n\n'
     f'    return post_json(RERANK_URL, {{"provider": {_ZDR_SRC}}}, key, 1)\n'),
    ("the client used directly", "retrieve.py", "    from openrouter_http import post_json\n",
     "    from openrouter_http import _client, post_json\n"),
    ("requests", "embed_chunks.py", None, "\n\ndef _made_up():\n    import requests\n\n    return requests.post(URL)\n"),
    ("urllib", "figure_vlm.py", None,
     "\n\ndef _made_up():\n    from urllib.request import urlopen\n\n    return urlopen(OpenRouterRunner.URL)\n"),
]


def test_inventory_catches_changes() -> None:
    """The scan above on mutated copies of the reviewed sources (in memory, nothing written): each change fails."""
    for what, rel, old, new in MUTATIONS:
        source = (ROOT / rel).read_text(encoding="utf-8")
        assert old is None or old in source, f"mutation {what!r}: the reviewed text is no longer in {rel}"
        mutated = source + new if old is None else source.replace(old, new, 1)
        assert inventory_problems(scan_runtime({rel: mutated})[0]), f"not caught: {what}"


# ---------------------------------------------------------------- 2. figure descriptions


def test_figure_descriptions_zdr() -> None:
    import figure_vlm as fv
    from PIL import Image

    from service.config import DEFAULT_FIGURE_MODEL

    assert DEFAULT_FIGURE_MODEL.startswith("openrouter:")
    for model in (DEFAULT_FIGURE_MODEL, "openrouter:made-up-vendor/vision-model-x"):
        with temp_dir() as tmp, online(), intercept(FakeOpenRouter()) as fake, capture_logs() as logs:
            doc = figure_doc()
            stats = fv.describe_missing(doc, model, tmp / "vlm", concurrency=2, retries=0, backoff=0)
            assert stats["to_describe"] == 1 and stats["described_now"] == 1 and stats["failed"] == 0, stats
            assert len(fake.requests) == 1, fake.paths()
            req = fake.requests[0]
            assert_openrouter(req, "/api/v1/chat/completions")
            body = req.body
            assert set(body) == {"model", "temperature", "max_tokens", "messages", "provider"}, sorted(body)
            assert body["model"] == model.split(":", 1)[1] and body["temperature"] == 0
            assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
            content = body["messages"][0]["content"]
            assert [c["type"] for c in content] == ["text", "image_url"], [c["type"] for c in content]
            assert content[0]["text"] == fv.DESCRIPTION_PROMPT  # the prompt, nothing of the document
            url = content[1]["image_url"]["url"]
            assert url.startswith("data:image/png;base64,") and set(content[1]) == {"type", "image_url"}
            sent = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
            assert sent.size == doc.pictures[0].get_image(doc).size  # the figure itself
            assert b"Zorblat" not in req.raw and b"zdr-figure-fixture" not in req.raw  # no OCR text, no doc name
            assert not any(f in req.raw for f in path_forms(tmp))
            assert fv.attach_cached(doc, model, tmp / "vlm")["described"] == 1
            assert_key_hidden(logs, folders=[tmp])
    # an upstream error: counted as failed; neither the log nor the exception carries the key
    with temp_dir() as tmp, online(), intercept(FakeOpenRouter(script=[401, 401])) as fake, capture_logs() as logs:
        stats = fv.describe_missing(figure_doc(), DEFAULT_FIGURE_MODEL, tmp / "vlm", retries=0, backoff=0)
        assert stats["failed"] == 1 and stats["described_now"] == 0, stats
        runner = fv.OpenRouterRunner("made-up-vendor/vision-model-x")
        exc = expect_error(lambda: runner.ask(figure_image(), "prompt"), RuntimeError)
        assert str(exc).startswith("OpenRouter 401") and len(fake.requests) == 2
        for req in fake.requests:
            assert_openrouter(req, "/api/v1/chat/completions")
        assert_key_hidden(logs, texts=[str(exc)], folders=[tmp])


# ---------------------------------------------------------------- 3. embeddings


def test_embeddings_zdr() -> None:
    import embed_chunks as ec
    from eval_retrieval import cached_query_vectors

    chunks = synthetic_chunks()
    with temp_dir() as tmp, online(), intercept(FakeOpenRouter()) as fake, capture_logs() as logs:
        path = write_chunks(tmp / "Private Folder QX" / "chunks", chunks)
        assert ec.embed_document(path) == {"chunks": 3, "cached": 0, "embedded": 3}
        assert len(fake.requests) == 1
        req = fake.requests[0]
        assert_openrouter(req, "/api/v1/embeddings")
        assert set(req.body) == {"model", "input", "provider"}, sorted(req.body)
        assert req.body["model"] == ec.MODEL and req.body["input"] == [c["embed_text"] for c in chunks]
        assert not any(f in req.raw for f in path_forms(tmp)) and b"Made-up passage 0:" in req.raw
        assert ec.MODEL == (os.environ.get("EMBED_MODEL") or "qwen/qwen3-embedding-8b")
        # a model override; then a cached run sends nothing; then a changed chunk is the only one sent
        assert ec.embed_document(path, "made-up-vendor/embed-model-x")["embedded"] == 3
        assert len(fake.requests) == 2 and fake.requests[1].body["model"] == "made-up-vendor/embed-model-x"
        assert fake.requests[1].body["input"] == [c["embed_text"] for c in chunks]
        assert ec.embed_document(path)["embedded"] == 0 and len(fake.requests) == 2
        changed = [dict(c) for c in chunks]
        changed[1]["embed_text"] += " Amended."
        changed[1]["content_hash"] = hashlib.sha256(changed[1]["embed_text"].encode()).hexdigest()[:16]
        write_chunks(path.parent, changed)
        assert ec.embed_document(path)["embedded"] == 1 and len(fake.requests) == 3
        assert fake.requests[2].body["input"] == [changed[1]["embed_text"]]
        # batches: every request carries the block
        vecs = ec.embed_texts(["made-up query one", "made-up query two", "made-up query three"], batch=2)
        assert vecs.shape == (3, DIM) and [r.body["input"] for r in fake.requests[3:]] == [
            ["made-up query one", "made-up query two"], ["made-up query three"]]
        # the search tool's query embeddings (eval_retrieval.cached_query_vectors): the query text only, cached
        q = "Instruct: Made-up instruction\nQuery: What is the zorblat dose?"
        cached_query_vectors([q], ec.MODEL, tmp / "query_embedding_cache.json")
        cached_query_vectors([q], ec.MODEL, tmp / "query_embedding_cache.json")
        assert len(fake.requests) == 6 and fake.requests[5].body["input"] == [q]
        for req in fake.requests:
            assert_openrouter(req, "/api/v1/embeddings")
            assert set(req.body) == {"model", "input", "provider"}
        assert_key_hidden(logs, folders=[tmp])


# ---------------------------------------------------------------- 4. rerank


def test_rerank_zdr() -> None:
    import retrieve

    chunks = synthetic_chunks()
    with temp_dir() as tmp, online(), intercept(FakeOpenRouter()) as fake, capture_logs() as logs:
        r = retrieve.Retriever(build_retriever_folder(tmp / "Private Folder QX" / "chunks", chunks))
        res = r.search("What is the zorblat dosing schedule?", fake_vector("q"), k=2, mode="rerank")
        assert len(res.hits) == 2 and len(fake.requests) == 1
        req = fake.requests[0]
        assert_openrouter(req, "/api/v1/rerank")
        assert set(req.body) == {"model", "query", "documents", "top_n", "provider"}, sorted(req.body)
        assert req.body["model"] == retrieve.RERANK_MODEL and req.body["query"] == res.expanded_query
        assert sorted(req.body["documents"]) == sorted(c["embed_text"] for c in chunks)
        assert req.body["top_n"] == len(req.body["documents"])
        assert not any(f in req.raw for f in path_forms(tmp))
        assert retrieve.RERANK_MODEL == (os.environ.get("RERANK_MODEL") or "qwen/qwen3-reranker-8b")
        # cached: nothing sent; another model: sent with that model
        r.search("What is the zorblat dosing schedule?", fake_vector("q"), k=2, mode="rerank")
        assert len(fake.requests) == 1
        r.rerank("Made-up rerank question", r.chunks[:2], "made-up-vendor/rerank-model-x")
        assert len(fake.requests) == 2 and fake.requests[1].body["model"] == "made-up-vendor/rerank-model-x"
        assert fake.requests[1].body["documents"] == [c["embed_text"] for c in r.chunks[:2]]
        assert_openrouter(fake.requests[1], "/api/v1/rerank")
        assert_key_hidden(logs, folders=[tmp])


# ---------------------------------------------------------------- 5. the search tool end to end


@contextmanager
def shared_tokenizers():
    """Chunking, each ProtocolDocument's context builder and the service's copy each load the same Qwen3 tokenizer
    (about 1.5 s per load here); inside this block one instance per name is shared. It only counts tokens for the
    context budget: what is sent to OpenRouter does not depend on it."""
    from transformers import AutoTokenizer

    own = "from_pretrained" in AutoTokenizer.__dict__
    original = AutoTokenizer.__dict__.get("from_pretrained")
    load = AutoTokenizer.from_pretrained
    made: dict = {}

    def from_pretrained(*args, **kwargs):
        options = sorted((k, repr(v)) for k, v in kwargs.items() if k != "local_files_only")
        key = (tuple(map(str, args)), tuple(options))
        if key not in made:
            made[key] = load(*args, **kwargs)
        return made[key]

    AutoTokenizer.from_pretrained = from_pretrained
    try:
        yield
    finally:
        if own:
            AutoTokenizer.from_pretrained = original
        else:
            del AutoTokenizer.from_pretrained


def test_search_tool_end_to_end() -> None:
    with shared_tokenizers():
        _search_tool_end_to_end()


def _search_tool_end_to_end() -> None:
    import test_service_api as tsa

    if not tsa.tokenizer_cached():
        print(f"   (skipped: tokenizer {tsa.TOKENIZER} not in the local Hugging Face cache)")
        return
    import retrieve
    from protocol_api import ProtocolDocument
    from service.app_service import ProtocolService, ServiceError
    from service.config import ServiceConfig
    from service.import_existing import import_folder

    assert tsa.DIM == DIM
    query, other, third = ("What dose of quillmorex is given?", "How many participants are planned?",
                           "Which sites enrol adults?")
    with temp_dir() as tmp, capture_logs() as logs:
        folder = tmp / "src" / "Quillmore"
        tsa.build_converted_folder(folder)
        chunks_dir = folder / "chunks"
        doc = ProtocolDocument.open(folder)  # no embed_fn / rerank_fn: the real OpenRouter code path
        with online(), intercept(FakeOpenRouter()) as fake:
            out = doc.call_tool("search", {"query": query, "k": 3})
            assert "error" not in out, out.get("error_type")
            assert out["blocks"] and len(out["hits"]) == 3
            assert fake.paths() == ["/api/v1/embeddings", "/api/v1/rerank"], fake.paths()
            r = doc._builder.retriever
            emb, rr = fake.requests
            assert_openrouter(emb, "/api/v1/embeddings")
            assert_openrouter(rr, "/api/v1/rerank")
            assert emb.body["model"] == r.meta["model"]
            assert emb.body["input"] == [r.query_text(out["expanded_query"])]  # the query only
            assert rr.body["model"] == retrieve.RERANK_MODEL and rr.body["query"] == out["expanded_query"]
            assert set(rr.body["documents"]) <= {c["embed_text"] for c in r.chunks}
            for req in fake.requests:
                assert not any(f in req.raw for f in path_forms(tmp))
            again = doc.call_tool("search", {"query": query, "k": 3})  # cached: nothing sent
            assert "error" not in again and len(fake.requests) == 2
        # offline: a cached query is answered, an uncached one is the offline error; nothing is sent
        with env(OPENROUTER_OFFLINE="1"), intercept(FakeOpenRouter()) as fake:
            assert "error" not in doc.call_tool("search", {"query": query, "k": 3})
            miss = doc.call_tool("search", {"query": other})
            assert miss.get("error_type") == "OfflineCacheMiss", miss.get("error_type")
            assert fake.requests == []
        # offline with empty caches (a fresh document object, no cache files): the offline error, nothing sent
        qcache, rcache = chunks_dir / "query_embedding_cache.json", chunks_dir / "rerank_cache.json"
        qcache.unlink()
        rcache.unlink()
        with env(OPENROUTER_OFFLINE="1"), intercept(FakeOpenRouter()) as fake:
            fresh = ProtocolDocument.open(folder)
            res = fresh.call_tool("search", {"query": query})
            assert res.get("error_type") == "OfflineCacheMiss" and "embedding" in res["error"], res.get("error_type")
            assert type(expect_error(lambda: fresh.search(query), Exception)).__name__ == "OfflineCacheMiss"
            assert not qcache.exists() and not rcache.exists()
            # the query embedding cached, the rerank not: refused at the rerank
            r = fresh._builder.retriever
            text = r.query_text(r.expand(query)[0])
            qcache.write_text(json.dumps({hashlib.sha256(f"{r.meta['model']}|{text}".encode()).hexdigest():
                                          fake_vector(text).tolist()}), encoding="utf-8")
            res = fresh.call_tool("search", {"query": query})
            assert res.get("error_type") == "OfflineCacheMiss" and "rerank" in res["error"], res.get("error_type")
            assert fake.requests == [] and not rcache.exists()
        # every spelling of the flag reads the same in protocol_api: a cache miss is OfflineCacheMiss
        with env(OPENROUTER_OFFLINE="true"), intercept(FakeOpenRouter()) as fake:
            for q in (query, other):  # rerank missing / query embedding missing
                res = fresh.call_tool("search", {"query": q})
                assert res.get("error_type") == "OfflineCacheMiss", res.get("error_type")
            assert fake.requests == []
        # through the service (what the REST API and MCP call): ZDR online, 503 "offline" offline, 502 upstream
        pdf = tmp / "src" / "quillmore.pdf"
        pdf.write_bytes(tsa.make_pdf("Quillmore study protocol", "Made-up text for a test"))
        service = ProtocolService(ServiceConfig(data_dir=tmp / "data"), run_worker=False, version=("1.0.0", "fp-zdr"))
        try:
            doc_id = import_folder(service.config, folder, pdf=pdf, version=("1.0.0", "fp-zdr"),
                                   registry=service.registry, storage=service.storage)["doc_id"]
            with online(), intercept(FakeOpenRouter()) as fake:
                result = service.call_tool(doc_id, "search", {"query": third, "k": 2})
                assert result["blocks"] and fake.paths() == ["/api/v1/embeddings", "/api/v1/rerank"], fake.paths()
                for req in fake.requests:
                    assert_openrouter(req, req.path)
                    assert not any(f in req.raw for f in path_forms(tmp))
            with env(OPENROUTER_OFFLINE="1"), intercept(FakeOpenRouter()) as fake:
                exc = expect_error(lambda: service.call_tool(doc_id, "search", {"query": other}), ServiceError)
                assert exc.status == 503 and exc.to_json()["type"] == "offline", (exc.status, exc.to_json()["type"])
                assert fake.requests == []
            with online(), intercept(FakeOpenRouter(script=[401])) as fake:
                exc = expect_error(lambda: service.call_tool(doc_id, "search", {"query": other}), ServiceError)
                assert exc.status == 502 and len(fake.requests) == 1, (exc.status, len(fake.requests))
                assert_openrouter(fake.requests[0], "/api/v1/embeddings")
                assert_key_hidden(texts=[str(exc), json.dumps(exc.to_json())])
        finally:
            service.stop()
        assert_key_hidden(logs, folders=[tmp])


# ---------------------------------------------------------------- 6. offline everywhere


def test_offline_sends_nothing() -> None:
    import embed_chunks as ec
    import figure_vlm as fv
    import retrieve
    from eval_retrieval import cached_query_vectors
    from retrieve import OfflineRefused

    from service.config import DEFAULT_FIGURE_MODEL

    with temp_dir() as tmp, intercept(FakeOpenRouter()) as fake, capture_logs() as logs:
        path = write_chunks(tmp / "doc" / "chunks", synthetic_chunks())
        with online():  # fill the embedding cache first
            ec.embed_document(path)
        filled = len(fake.requests)
        for k, spelling in enumerate(("1", "true", "yes", "on", " 1 ")):
            with env(OPENROUTER_OFFLINE=spelling):
                stats = fv.describe_missing(figure_doc(), DEFAULT_FIGURE_MODEL, tmp / "vlm")
                assert stats["offline"] == 1 and stats["described_now"] == 0, stats
                assert not list((tmp / "vlm").glob("*.json"))
                img = figure_image()
                expect_error(lambda: fv.OpenRouterRunner("made-up-vendor/vision-model-x").ask(img, "prompt"),
                             OfflineRefused)
                # cached chunks: answered from the cache; a missing one: refused before anything is written
                assert ec.embed_document(path)["embedded"] == 0
                other = write_chunks(tmp / f"other{k}" / "chunks", synthetic_chunks(2))
                expect_error(lambda: ec.embed_document(other), OfflineRefused)
                assert sorted(p.name for p in other.parent.iterdir()) == ["chunks.jsonl"]
                expect_error(lambda: ec.embed_texts(["made-up query"]), OfflineRefused)
                expect_error(lambda: cached_query_vectors(["made-up query"], ec.MODEL, tmp / "q.json"), OfflineRefused)
                r = retrieve.Retriever(build_retriever_folder(tmp / f"r{k}"))
                expect_error(lambda: r.search("zorblat dosing", fake_vector("q"), k=2, mode="rerank"), OfflineRefused)
                expect_error(lambda: retrieve.post_with_retry(retrieve.RERANK_URL, {"provider": ZDR}, FAKE_KEY),
                             OfflineRefused)
        assert len(fake.requests) == filled
        assert_key_hidden(logs, folders=[tmp])


# ---------------------------------------------------------------- 7. retries keep the body


def test_retries_keep_the_zdr_body() -> None:
    import embed_chunks as ec
    import figure_vlm as fv
    import retrieve

    from service.config import DEFAULT_FIGURE_MODEL

    def same_body(fake: FakeOpenRouter, path: str, n: int) -> None:
        assert len(fake.requests) == n, (len(fake.requests), n)
        for req in fake.requests:
            assert_openrouter(req, path)
            assert req.raw == fake.requests[0].raw and req.headers == fake.requests[0].headers

    with temp_dir() as tmp, online(), no_sleep() as slept, capture_logs() as logs:
        r = retrieve.Retriever(build_retriever_folder(tmp / "r"))
        cases = (([429, "ok"], 2, [10]), (["connect_error", "ok"], 2, [10]), ([503, 502, 500, "ok"], 4, [10, 20, 40]))
        for k, (script, n, sleeps) in enumerate(cases):
            slept.clear()
            with intercept(FakeOpenRouter(script=script)) as fake:
                r.rerank(f"Made-up rerank question {k}", r.chunks, retrieve.RERANK_MODEL)
            same_body(fake, "/api/v1/rerank", n)
            assert slept == sleeps, slept
        slept.clear()
        with intercept(FakeOpenRouter(script=[429] * 6)) as fake:  # exhausted: an error, nothing cached
            exc = expect_error(lambda: r.rerank("Made-up rerank question x", r.chunks, retrieve.RERANK_MODEL),
                               RuntimeError)
        same_body(fake, "/api/v1/rerank", 6)
        assert slept == [10, 20, 40, 80, 120] and str(exc).startswith("OpenRouter 429"), slept
        with intercept(FakeOpenRouter(script=[400])) as fake:  # not retryable: one attempt
            exc2 = expect_error(lambda: r.rerank("Made-up rerank question y", r.chunks, retrieve.RERANK_MODEL),
                                RuntimeError)
        same_body(fake, "/api/v1/rerank", 1)
        # embeddings: 429 / 503 then 200, and exhausted after 3 attempts
        for script, n in (([429, "ok"], 2), ([503, 502, "ok"], 3)):
            with intercept(FakeOpenRouter(script=script)) as fake:
                ec.embed_texts(["made-up text one", "made-up text two"])
            same_body(fake, "/api/v1/embeddings", n)
        with intercept(FakeOpenRouter(script=[503] * 3)) as fake:
            exc3 = expect_error(lambda: ec.embed_texts(["made-up text one"]), RuntimeError)
        same_body(fake, "/api/v1/embeddings", 3)
        # figure descriptions: describe_missing retries the figure; the request is rebuilt identically
        with intercept(FakeOpenRouter(script=[500, 429, "ok"])) as fake:
            stats = fv.describe_missing(figure_doc(), DEFAULT_FIGURE_MODEL, tmp / "vlm", retries=2, backoff=0)
        assert stats["described_now"] == 1 and stats["failed"] == 0, stats
        same_body(fake, "/api/v1/chat/completions", 3)
        assert_key_hidden(logs, texts=[str(exc), str(exc2), str(exc3)], folders=[tmp])


# ---------------------------------------------------------------- 8. the shared client


def made_up_rerank_body() -> dict:
    import retrieve

    return {"model": retrieve.RERANK_MODEL, "query": "Made-up question", "documents": ["Made-up passage"],
            "top_n": 1, "provider": dict(ZDR)}


def test_one_client_per_process() -> None:
    """One httpx.Client per process: made on first use, once even when the first requests of several threads start
    together, then reused by every call site and thread; under another process id (a forked child) a new one, the
    parent's left alone."""
    from concurrent.futures import ThreadPoolExecutor

    import embed_chunks as ec
    import figure_vlm as fv
    import openrouter_http
    import retrieve

    saved, made, n = openrouter_http._shared, [], 12
    images, body = [figure_image() for _ in range(n // 3 + 1)], made_up_rerank_body()  # an image per thread
    sites = (lambda i: ec.embed_texts([f"made-up text {i}"]),
             lambda i: retrieve.post_with_retry(retrieve.RERANK_URL, body, FAKE_KEY),
             lambda i: fv.OpenRouterRunner("made-up-vendor/vision-model-x").ask(images[i // 3], "prompt"))
    openrouter_http._shared = None  # as in a new process
    try:
        with online(), intercept(FakeOpenRouter()) as fake:
            before = len(CLIENTS)
            start = threading.Barrier(n)

            def together(i: int) -> None:
                start.wait(timeout=60)
                sites[i % 3](i)

            with ThreadPoolExecutor(max_workers=n) as pool:
                list(pool.map(together, range(n)))
            for i in range(n, n + 3):  # then one after the other, from this thread
                sites[i % 3](i)
            pid, client = openrouter_http._shared
            made.append(client)
            assert len(CLIENTS) == before + 1 and pid == os.getpid(), len(CLIENTS) - before
            assert len(fake.requests) == n + 3 and len({r.thread for r in fake.requests}) == n + 1
            assert all(r.transport is client._transport_for_url(httpx.URL(r.url)) for r in fake.requests)
            for req in fake.requests:
                assert_openrouter(req, req.path)
            openrouter_http._shared = (-1, client)  # another process id: what a forked child would see
            ec.embed_texts(["made-up text"])
            assert len(CLIENTS) == before + 2 and openrouter_http._shared[1] is not client
            made.append(openrouter_http._shared[1])
            assert_openrouter(fake.requests[-1], "/api/v1/embeddings")
            assert not client.is_closed
    finally:
        openrouter_http._shared = saved
        for client in made:
            client.close()


def test_no_cookie_is_kept() -> None:
    """Every fake answer sets cookies (error answers too): the shared client keeps none and sends none back, so the
    requests carry the reviewed headers only. Control: a default httpx.Client sends them back, so the fake's cookies
    are ones a client would keep."""
    import embed_chunks as ec
    import openrouter_http
    import retrieve

    with online(), intercept(FakeOpenRouter()) as fake:
        with httpx.Client() as plain:
            for _ in range(2):
                plain.post(BASE + "rerank", json={"documents": []})
        first, second = fake.requests
        assert "cookie" not in first.headers and {"__cf_bm", "_cfuvid"} == {
            c.split("=", 1)[0] for c in second.headers.get("cookie", "").split("; ") if c}, "control"
    body = made_up_rerank_body()
    with online(), no_sleep(), intercept(FakeOpenRouter(script=[503, "ok", 429, "ok"])) as fake:
        for k in range(3):
            ec.embed_texts([f"made-up text {k}"])
            retrieve.post_with_retry(retrieve.RERANK_URL, body, FAKE_KEY)
        assert len(fake.requests) == 8, len(fake.requests)
        for req in fake.requests:
            assert_openrouter(req, req.path)  # no Cookie header
        assert len(openrouter_http._shared[1].cookies.jar) == 0


# A fresh interpreter (this one imported the modules long ago): importing the request modules loads no HTTP stack, and
# importing the service's entry modules (API and pipeline) on top makes no client; no network either.
IMPORT_PROBE = r"""
import ipaddress, json, socket, sys

attempts = []


def loopback(host):
    try:
        return host is None or host == "localhost" or ipaddress.ip_address(str(host).strip("[]")).is_loopback
    except ValueError:
        return False


real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo


def connect(self, address):
    if isinstance(address, tuple) and not loopback(address[0]):
        attempts.append("connect")
        raise OSError("blocked")
    return real_connect(self, address)


def getaddrinfo(host, *args, **kwargs):
    if not loopback(host):
        attempts.append("getaddrinfo")
        raise OSError("blocked")
    return real_getaddrinfo(host, *args, **kwargs)


socket.socket.connect, socket.getaddrinfo = connect, getaddrinfo
import embed_chunks, figure_vlm, openrouter_http, retrieve
httpx_loaded = "httpx" in sys.modules
import httpx

made = []
real_init = httpx.Client.__init__


def init(self, *args, **kwargs):
    made.append(1)
    real_init(self, *args, **kwargs)


httpx.Client.__init__ = init
import service.api, service.pipeline
print(json.dumps({"httpx_loaded": httpx_loaded, "clients": len(made), "shared": openrouter_http._shared is not None,
                  "network": attempts}))
"""


def test_import_makes_no_client() -> None:
    out = subprocess.run([sys.executable, "-c", IMPORT_PROBE], cwd=ROOT, env=dict(os.environ), capture_output=True,
                         text=True, encoding="utf-8", timeout=600)
    assert out.returncode == 0, f"exit {out.returncode}: {out.stderr[-1500:]}"
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result == {"httpx_loaded": False, "clients": 0, "shared": False, "network": []}, result


# ---------------------------------------------------------------- runner


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    start_all = time.time()
    for t in tests:
        start = time.time()
        before = len(VIOLATIONS)
        try:
            t()
            assert len(VIOLATIONS) == before, f"network / offline violations: {VIOLATIONS[before:]}"
            print(f"ok {t.__name__} ({time.time() - start:.1f}s)")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    if VIOLATIONS and not failed:
        failed += 1
        print(f"FAIL network guard: {VIOLATIONS}")
    print(f"{len(tests) - failed} passed in {time.time() - start_all:.1f}s" if not failed
          else f"{failed} of {len(tests)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
