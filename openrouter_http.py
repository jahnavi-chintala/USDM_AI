"""The one HTTP client of the runtime's OpenRouter requests (figure descriptions, embeddings, rerank).

httpx.post builds a new client per call: a new SSL context (the certifi CA bundle parsed again) and a new TLS
connection every time. One client per process, made on first use, keeps both and pools connections. What is sent
does not change: the caller's URL, JSON body and timeout, Authorization as the only header set here (httpx adds its
usual Host / Accept / Accept-Encoding / Connection / User-Agent / Content-Length / Content-Type, as for httpx.post),
the environment honoured as httpx.post does (trust_env: proxies, SSL_CERT_FILE; read once, when the client is made).
No cookies: OpenRouter's CDN sets some, and a shared client would send them back on later requests; its jar refuses
every one.

Thread-safe (figure descriptions run in threads, the API serves searches from several). One client per process: the
service runs pipeline jobs in spawned interpreters, each makes its own; a forked child would make a new one rather
than share its parent's connections. Nothing is made at import, so importing the request modules stays cheap and
offline; the OPENROUTER_OFFLINE refusal stays at the call sites, before post_json is called.
"""

import os
import threading
from http.cookiejar import CookieJar, CookiePolicy


class _NoCookies(CookiePolicy):
    """Keep no cookie a response sets, send none."""

    netscape, rfc2965, hide_cookie2 = True, False, False

    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False

    def domain_return_ok(self, domain, request) -> bool:
        return False

    def path_return_ok(self, path, request) -> bool:
        return False


_lock = threading.Lock()
_shared = None  # (process id, httpx.Client), set once on first use


def _client():
    """This process's client; concurrent first requests wait for the one being made."""
    global _shared
    shared = _shared
    if shared is None or shared[0] != os.getpid():
        with _lock:
            shared = _shared
            if shared is None or shared[0] != os.getpid():
                import httpx  # here, not at import: importing this module loads no HTTP stack

                shared = _shared = (os.getpid(), httpx.Client(cookies=CookieJar(policy=_NoCookies())))
    return shared[1]


def post_json(url: str, body: dict, key: str, timeout: float):
    """POST `body` as JSON to `url` with the API key as a Bearer token; the httpx.Response, already read."""
    return _client().post(url, json=body, timeout=timeout, headers={"Authorization": f"Bearer {key}"})
