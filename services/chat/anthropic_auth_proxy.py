"""Local OAuth-credential proxy for per-user containers.

Item N — guest-token isolation, intra-container.

Why this exists
---------------
Per-user containers used to run claude as uid 2000 (claude-runner) so that
claude could read its OAuth credential at /var/claude-runner/.claude/
.credentials.json (mode 0400 owner 2000). But claude's Bash tool inherits
its parent's uid, so the model's tool-call shell ALSO ran as 2000 and could
read the file. The Item 1 design separated uids at the filesystem layer
without separating them at the dispatch layer; this proxy closes the gap.

Architecture
------------
Per per-user container:
  - This proxy runs as uid 2000:2000 (sole reader of the real credential).
  - Listens on 127.0.0.1:5557 (loopback only — never reachable from outside
    the container, never reachable from another per-user container).
  - claude runs as uid 1000:1000 with HOME=/workspace and
    ANTHROPIC_BASE_URL=http://localhost:5557.
  - A DUMMY credentials.json sits at /workspace/.claude/.credentials.json
    (uid 1000 readable) so claude stays in OAuth mode without behavioral
    drift; the dummy bearer is stripped by the proxy and replaced server-
    side with the real bearer read from the uid-2000-only file.
  - Bash subprocesses claude spawns inherit uid 1000 — cannot read
    /var/claude-runner/.claude/.credentials.json (Permission denied), and
    the loopback proxy refuses to echo the bearer back.

Threat model handled
--------------------
- Model exfiltrates credential via shell: blocked. Bash uid 1000 cannot
  read the credential file; bash cannot read the proxy's memory.
- Model induces credential leak via /v1/oauth/token refresh response:
  blocked. /v1/oauth/* paths return 403 from this proxy. (Token refresh is
  handled out-of-band by the host's refresh-claude-tokens timer +
  chat-container's populate_credentials.)
- Model tampers with dummy credentials to extract real one: not possible.
  The dummy file contains no real secret; rewriting it changes only what
  claude sends as Authorization, which the proxy unconditionally strips.

Threat model NOT handled (out of scope)
---------------------------------------
- Model burns the per-user container's assigned account quota by issuing
  legitimate-shaped requests through the proxy. The proxy is a credential
  shield, not a rate limiter — quota policy lives elsewhere.
- Model abuses an upstream Anthropic API surface to leak secrets via
  *response* content (e.g., asking the API to echo headers). Anthropic's
  surface is trusted; if it ever starts echoing Authorization, this proxy
  can be extended to scrub responses.

What this proxy deliberately does NOT do
----------------------------------------
- No OAuth refresh. Trusted out-of-band path keeps the credential fresh.
  On 401 we re-read the file once (in case populate_credentials rewrote
  it between cache-load and now) and retry the request once.
- No request-body inspection or mutation. Header rewrite only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

CRED_PATH = os.environ.get(
    "ANTHROPIC_AUTH_PROXY_CRED_PATH",
    "/var/claude-runner/.claude/.credentials.json",
)
UPSTREAM_BASE = os.environ.get(
    "ANTHROPIC_AUTH_PROXY_UPSTREAM",
    "https://api.anthropic.com",
).rstrip("/")
LISTEN_HOST = os.environ.get("ANTHROPIC_AUTH_PROXY_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("ANTHROPIC_AUTH_PROXY_LISTEN_PORT", "5557"))

# Headers carrying client-supplied auth — strip before forwarding so the
# only auth that reaches Anthropic is the one this proxy attaches.
_STRIP_REQ_HEADERS = frozenset({"authorization", "x-api-key", "host"})

# Hop-by-hop headers per RFC 7230 §6.1 — strip on response. We deliberately
# do NOT strip Content-Encoding: this proxy uses httpx.aiter_raw() which
# returns bytes in upstream's wire encoding (gzip/br/...), so claude needs
# Content-Encoding intact to decode them. Likewise we strip Content-Length
# (the chunk boundaries change when we re-frame via StreamingResponse) and
# Transfer-Encoding (Starlette manages chunked framing itself).
_STRIP_RESP_HEADERS = frozenset({
    "transfer-encoding", "content-length",
    "connection", "keep-alive", "upgrade", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
})

UPSTREAM_HOST = "api.anthropic.com"

logger = logging.getLogger("anthropic_auth_proxy")


def _read_cred_token() -> str:
    """Read the current OAuth access token. Raises on any failure."""
    with open(CRED_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    token = data.get("claudeAiOauth", {}).get("accessToken")
    if not token or not isinstance(token, str):
        raise ValueError(f"{CRED_PATH} missing claudeAiOauth.accessToken")
    return token


class TokenCache:
    """In-memory accessToken cache with explicit force-reread.

    The cache lives only as long as the proxy process; populate_credentials
    rewrites the on-disk file from the chat container, and on any 401 we
    force-reread once to pick up a refreshed token before retrying.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._mtime_ns: int | None = None

    @staticmethod
    def _cred_mtime() -> int | None:
        try:
            return os.stat(CRED_PATH).st_mtime_ns
        except OSError:
            return None

    async def get(self, *, force_reread: bool = False) -> str:
        # The chat backend rewrites the file when it swaps the served account
        # (saturation, refresh). A 401 is not the only signal: re-read
        # whenever the file changed, or the OLD account's bearer keeps being
        # sent and its 429s get blamed on the new account.
        mtime = self._cred_mtime()
        if self._token is not None and not force_reread and mtime == self._mtime_ns:
            return self._token
        async with self._lock:
            mtime = self._cred_mtime()
            if self._token is not None and not force_reread and mtime == self._mtime_ns:
                return self._token
            # Off-loop: tiny file, but avoids blocking the event loop on
            # disk I/O if /var/claude-runner ever lands on slow storage.
            self._token = await asyncio.to_thread(_read_cred_token)
            self._mtime_ns = mtime
            return self._token


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # One AsyncClient lives for the proxy's lifetime; httpx keep-alive
    # + http2 disabled (claude doesn't need it and adds dependency cost).
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=600.0, pool=10.0),
        follow_redirects=False,
    )
    app.state.tokens = TokenCache()
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=_lifespan)


@app.get("/__proxy_health")
async def health() -> dict:
    return {"ok": True}


def _is_oauth_path(path: str) -> bool:
    # Block ANY /v1/oauth/* regardless of method. Token refresh stays
    # the responsibility of the trusted host-side path.
    return path.startswith("/v1/oauth")


async def _send_upstream(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
) -> httpx.Response:
    """Build + send an upstream request with stream=True. Caller owns close."""
    req = client.build_request(method, url, headers=headers, content=body)
    return await client.send(req, stream=True)


@app.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def forward(full_path: str, request: Request):
    path = "/" + full_path.lstrip("/")
    if _is_oauth_path(path):
        # Telemetry-friendly log — never include token material.
        logger.warning("blocked oauth path: %s %s", request.method, path)
        return JSONResponse(
            {"error": "oauth_blocked_by_proxy"},
            status_code=403,
        )

    upstream_url = f"{UPSTREAM_BASE}{path}"
    if request.url.query:
        upstream_url = f"{upstream_url}?{request.url.query}"

    base_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQ_HEADERS
    }
    body = await request.body()
    client: httpx.AsyncClient = request.app.state.client
    tokens: TokenCache = request.app.state.tokens

    token = await tokens.get()
    headers = dict(base_headers)
    headers["Authorization"] = f"Bearer {token}"
    headers["Host"] = UPSTREAM_HOST

    upstream_resp = await _send_upstream(client, request.method, upstream_url, headers, body)

    if upstream_resp.status_code == 401:
        # populate_credentials may have rewritten the on-disk file since
        # we cached. Re-read once and retry exactly once.
        await upstream_resp.aclose()
        token = await tokens.get(force_reread=True)
        headers["Authorization"] = f"Bearer {token}"
        upstream_resp = await _send_upstream(
            client, request.method, upstream_url, headers, body,
        )

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in _STRIP_RESP_HEADERS
    }

    return StreamingResponse(
        upstream_resp.aiter_raw(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        background=BackgroundTask(upstream_resp.aclose),
    )


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info(
        "starting anthropic auth proxy on %s:%d -> %s (cred=%s)",
        LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE, CRED_PATH,
    )
    # Fail fast at startup if the credential is unreadable — running
    # without it would just produce 5xx for every claude request.
    try:
        _read_cred_token()
        logger.info("credential read OK at startup")
    except Exception as exc:
        logger.error("credential unreadable at startup: %s", exc)
        sys.exit(1)
    uvicorn.run(
        app,
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":
    main()
