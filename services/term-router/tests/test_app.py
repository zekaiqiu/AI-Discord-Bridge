"""Unit tests for services/term-router app + pty bridge.

No real docker daemon, no real network, no real Cloudflare. JWTs are
locally signed with an RSA key pair generated per-test session; the
'JWKS' is a dict our injected `certs_fetcher` returns.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time
from unittest.mock import MagicMock

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

# --- Make the hyphenated services/term-router/ importable ----------------
#
# Hyphens forbid the dotted-package import path, so we load app.py and
# pty_bridge.py as TOP-LEVEL modules from their files. This mirrors what
# the Dockerfile does at runtime (WORKDIR /app, files at /app/{app,pty_bridge}.py).

_TERM_ROUTER_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    # Idempotent: pytest may re-import this test module per worker (xdist)
    # or on collection retry; reassigning sys.modules[name] is safe.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load pty_bridge first so app's `import pty_bridge` resolves from sys.modules.
pty_bridge = _load_module("pty_bridge", _TERM_ROUTER_DIR / "pty_bridge.py")
term_app_mod = _load_module("term_router_app", _TERM_ROUTER_DIR / "app.py")

from services.chat import auth as chat_auth  # noqa: E402  (after sys.path ready)


# --- Shared fixtures ----------------------------------------------------

@pytest.fixture(scope="module")
def rsa_keypair():
    """One RSA-2048 key pair for the whole module (key gen is slow)."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    pem_priv = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_pub = pub.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return {"priv_pem": pem_priv, "pub_pem": pem_pub, "kid": "test-kid-1"}


@pytest.fixture
def jwks(rsa_keypair):
    """Minimal JWKS containing just our test public key."""
    # PyJWT exposes a helper to convert PEM to JWK.
    from jwt.algorithms import RSAAlgorithm
    pub_jwk = json.loads(RSAAlgorithm.to_jwk(
        serialization.load_pem_public_key(rsa_keypair["pub_pem"])
    ))
    pub_jwk["kid"] = rsa_keypair["kid"]
    pub_jwk["alg"] = "RS256"
    pub_jwk["use"] = "sig"
    return {"keys": [pub_jwk]}


@pytest.fixture
def cf_env(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_AUD", "test-audience-tag")
    monkeypatch.setenv("CF_ACCESS_TEAM", "myteam")


@pytest.fixture
def make_token(rsa_keypair):
    """Factory for signing arbitrary CF-Access-shaped JWTs."""
    def _make(claims_overrides=None, *, kid=None, alg="RS256"):
        now = int(time.time())
        claims = {
            "iss": "https://myteam.cloudflareaccess.com",
            "aud": "test-audience-tag",
            "iat": now,
            "exp": now + 600,
            "nbf": now,
            "email": "alice@example.com",
            "sub": "alice-sub-1",
        }
        if claims_overrides:
            claims.update(claims_overrides)
        headers = {"kid": kid or rsa_keypair["kid"]}
        return pyjwt.encode(
            claims, rsa_keypair["priv_pem"], algorithm=alg, headers=headers
        )
    return _make


# --- Group A: verify_cf_access_jwt ---------------------------------------

def test_verify_jwt_happy_path(cf_env, make_token, jwks):
    token = make_token()
    claims = chat_auth.verify_cf_access_jwt(token, certs_fetcher=lambda team: jwks)
    assert claims["email"] == "alice@example.com"
    assert claims["aud"] == "test-audience-tag"


def test_verify_jwt_missing_aud_env_raises(monkeypatch, make_token, jwks):
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    monkeypatch.setenv("CF_ACCESS_TEAM", "myteam")
    token = make_token()
    with pytest.raises(chat_auth.JWTVerificationError) as exc:
        chat_auth.verify_cf_access_jwt(token, certs_fetcher=lambda team: jwks)
    assert "CF_ACCESS_AUD" in str(exc.value)


def test_verify_jwt_missing_team_env_raises(monkeypatch, make_token, jwks):
    monkeypatch.setenv("CF_ACCESS_AUD", "test-audience-tag")
    monkeypatch.delenv("CF_ACCESS_TEAM", raising=False)
    token = make_token()
    with pytest.raises(chat_auth.JWTVerificationError) as exc:
        chat_auth.verify_cf_access_jwt(token, certs_fetcher=lambda team: jwks)
    assert "CF_ACCESS_TEAM" in str(exc.value)


def test_verify_jwt_wrong_audience_raises(cf_env, make_token, jwks):
    bad_token = make_token({"aud": "someone-elses-tag"})
    with pytest.raises(chat_auth.JWTVerificationError):
        chat_auth.verify_cf_access_jwt(bad_token, certs_fetcher=lambda team: jwks)


def test_verify_jwt_wrong_issuer_raises(cf_env, make_token, jwks):
    bad_token = make_token({"iss": "https://attacker.example/"})
    with pytest.raises(chat_auth.JWTVerificationError):
        chat_auth.verify_cf_access_jwt(bad_token, certs_fetcher=lambda team: jwks)


def test_verify_jwt_expired_raises(cf_env, make_token, jwks):
    # Issue a token that expired well outside the leeway.
    expired = make_token({"exp": int(time.time()) - 600, "iat": int(time.time()) - 1200})
    with pytest.raises(chat_auth.JWTVerificationError):
        chat_auth.verify_cf_access_jwt(expired, certs_fetcher=lambda team: jwks)


def test_verify_jwt_unknown_kid_raises(cf_env, make_token, jwks):
    token = make_token(kid="kid-not-in-jwks")
    with pytest.raises(chat_auth.JWTVerificationError) as exc:
        chat_auth.verify_cf_access_jwt(token, certs_fetcher=lambda team: jwks)
    assert "kid" in str(exc.value)


def test_verify_jwt_empty_token_raises(cf_env):
    with pytest.raises(chat_auth.JWTVerificationError):
        chat_auth.verify_cf_access_jwt("", certs_fetcher=lambda team: {"keys": []})


# --- Group B: email_from_claims -----------------------------------------

def test_email_from_claims_top_level():
    assert chat_auth.email_from_claims({"email": "Alice@Example.COM"}) == "alice@example.com"


def test_email_from_claims_identity_nonce_fallback():
    claims = {"identity_nonce": {"email": " bob@example.com "}}
    assert chat_auth.email_from_claims(claims) == "bob@example.com"


def test_email_from_claims_missing_raises():
    with pytest.raises(chat_auth.JWTVerificationError):
        chat_auth.email_from_claims({"sub": "no-email"})


def test_email_from_claims_non_dict_raises():
    with pytest.raises(chat_auth.JWTVerificationError):
        chat_auth.email_from_claims("not-a-dict")  # type: ignore[arg-type]


# --- Group C: pty_bridge.bridge ----------------------------------------

import asyncio


class FakeWebSocket:
    """Minimal stand-in for FastAPI's WebSocket — only the surface bridge
    actually touches: receive(), send_bytes(), close()."""

    def __init__(self, incoming, *, block_after=False):
        # incoming is a list of receive() return-value dicts. The bridge
        # consumes them in order; once exhausted, we either yield a
        # disconnect (default) or block forever (block_after=True), the
        # latter being how we let the exec→ws direction drain on its own.
        self._incoming = list(incoming)
        self.block_after = block_after
        self.sent_bytes = []
        self.closed = False
        self.close_code = None

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        if self.block_after:
            # Block forever: lets the exec→ws pump finish naturally on its
            # own EOF without the ws→exec pump winning the race.
            await asyncio.Event().wait()
            raise AssertionError("unreachable")
        return {"type": "websocket.disconnect", "code": 1000}

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, code=1000):
        self.closed = True
        self.close_code = code


class FakeExecStream:
    """Stand-in for the docker-py exec socket. Supports read/write surface.

    `outgoing` is a deque of bytes chunks the stream will emit when read.
    Once empty, read() returns b"" (EOF) so the exec→ws pump exits.
    """

    def __init__(self, outgoing=()):
        self.outgoing = list(outgoing)
        self.written = []
        self.closed = False

    def read(self, n):
        if not self.outgoing:
            return b""
        chunk = self.outgoing.pop(0)
        return chunk[:n]

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


def test_parse_resize_well_formed():
    assert pty_bridge._parse_resize('{"type":"resize","cols":80,"rows":24}') == (80, 24)


def test_parse_resize_rejects_garbage():
    assert pty_bridge._parse_resize("not json") is None
    assert pty_bridge._parse_resize('{"type":"other"}') is None
    assert pty_bridge._parse_resize('{"type":"resize","cols":"x","rows":24}') is None
    assert pty_bridge._parse_resize('{"type":"resize","cols":0,"rows":24}') is None


def test_bridge_forwards_exec_output_to_ws():
    ws = FakeWebSocket(incoming=[], block_after=True)
    exec_stream = FakeExecStream(outgoing=[b"hello", b"world"])
    docker_client = MagicMock()

    asyncio.run(pty_bridge.bridge(ws, exec_stream, "exec-id-1", docker_client=docker_client))

    assert ws.sent_bytes == [b"hello", b"world"]


def test_bridge_forwards_ws_bytes_to_exec_stdin():
    ws = FakeWebSocket(incoming=[
        {"type": "websocket.receive", "bytes": b"echo hi\n"},
    ])
    exec_stream = FakeExecStream(outgoing=[])  # exec EOF immediately
    docker_client = MagicMock()

    asyncio.run(pty_bridge.bridge(ws, exec_stream, "exec-id-1", docker_client=docker_client))

    assert exec_stream.written == [b"echo hi\n"]


def test_bridge_resize_calls_exec_resize():
    ws = FakeWebSocket(incoming=[
        {"type": "websocket.receive", "text": '{"type":"resize","cols":120,"rows":40}'},
    ])
    exec_stream = FakeExecStream(outgoing=[])
    docker_client = MagicMock()

    asyncio.run(pty_bridge.bridge(ws, exec_stream, "exec-id-7", docker_client=docker_client))

    docker_client.api.exec_resize.assert_called_once_with(
        "exec-id-7", height=40, width=120
    )


def test_bridge_malformed_resize_does_not_crash():
    """A text frame that doesn't parse as resize must NOT raise and must NOT
    call exec_resize; it falls through to the stdin path (which the brief
    requires: 'all other frames are forwarded as bytes to the exec stdin').

    test_bridge_text_frames_forwarded_as_stdin covers the stdin assertion
    explicitly; this test is just the no-crash invariant."""
    ws = FakeWebSocket(incoming=[
        {"type": "websocket.receive", "text": "not-json-at-all"},
        {"type": "websocket.receive", "text": '{"type":"resize"}'},  # missing cols/rows
    ])
    exec_stream = FakeExecStream(outgoing=[])
    docker_client = MagicMock()

    # Should complete without raising.
    asyncio.run(pty_bridge.bridge(ws, exec_stream, "x", docker_client=docker_client))

    docker_client.api.exec_resize.assert_not_called()


def test_bridge_text_frames_forwarded_as_stdin():
    """Brief: 'All other frames are forwarded as bytes to the exec stdin.'

    xterm.js's `terminal.onData(d => ws.send(d))` produces a text frame for
    every keystroke (ws.send of a string is a TEXT frame, not binary). If
    we drop these the terminal is read-only — AR2 caught this regression
    in round 1.

    A non-resize text frame must be UTF-8 encoded and written to the exec
    stdin in the same order it arrived."""
    ws = FakeWebSocket(incoming=[
        # Plain ASCII keystroke.
        {"type": "websocket.receive", "text": "ls\n"},
        # Multibyte UTF-8 — must encode to bytes, not be silently dropped.
        {"type": "websocket.receive", "text": "café\n"},
    ])
    exec_stream = FakeExecStream(outgoing=[])
    docker_client = MagicMock()

    asyncio.run(pty_bridge.bridge(ws, exec_stream, "x", docker_client=docker_client))

    assert exec_stream.written == [b"ls\n", "café\n".encode("utf-8")]
    docker_client.api.exec_resize.assert_not_called()


def test_bridge_resize_then_keystroke_in_order():
    """Resize envelope followed by a keystroke text frame: resize triggers
    exec_resize, keystroke writes to stdin. Ordering preserved."""
    ws = FakeWebSocket(incoming=[
        {"type": "websocket.receive", "text": '{"type":"resize","cols":80,"rows":24}'},
        {"type": "websocket.receive", "text": "echo\n"},
    ])
    exec_stream = FakeExecStream(outgoing=[])
    docker_client = MagicMock()

    asyncio.run(pty_bridge.bridge(ws, exec_stream, "x", docker_client=docker_client))

    docker_client.api.exec_resize.assert_called_once_with("x", height=24, width=80)
    assert exec_stream.written == [b"echo\n"]


# --- Group D: app routes ----------------------------------------------

from fastapi import WebSocketDisconnect


def _make_test_app(*, jwt_verifier=None, ensure_container=None, docker_client=None):
    """Build an app with all three deps injected. Default verifier returns
    a 'user' email; default ensure returns a fixed container; default
    docker_client is a MagicMock with usable exec_create/exec_start."""
    if jwt_verifier is None:
        def jwt_verifier(_token):
            return {"email": "alice@example.com", "aud": "x"}
    if ensure_container is None:
        ensure_container = MagicMock(return_value="portfolio-user-deadbeef0000")
    if docker_client is None:
        docker_client = MagicMock()
        docker_client.api.exec_create.return_value = {"Id": "exec-id-test"}

        class _NoopStream:
            def read(self, n):
                return b""
            def write(self, data):
                pass
            def close(self):
                pass

        docker_client.api.exec_start.return_value = _NoopStream()
    return term_app_mod.create_app(
        docker_client=docker_client,
        jwt_verifier=jwt_verifier,
        ensure_container=ensure_container,
    ), ensure_container, docker_client


def test_healthz_returns_ok():
    app, _, _ = _make_test_app()
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_serves_static_html():
    app, _, _ = _make_test_app()
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "<div id=\"terminal\"></div>" in r.text


def test_static_files_served():
    app, _, _ = _make_test_app()
    client = TestClient(app)
    r = client.get("/static/term.css")
    assert r.status_code == 200
    assert "#terminal" in r.text


def test_static_path_traversal_blocked():
    """StaticFiles must refuse to escape STATIC_DIR via ../../."""
    app, _, _ = _make_test_app()
    client = TestClient(app)
    # Common traversal attempts. Any non-2xx status is acceptable; we just
    # need to confirm the file outside STATIC_DIR is NOT served.
    for attempt in [
        "/static/../app.py",
        "/static/..%2fapp.py",
        "/static/%2e%2e/app.py",
    ]:
        r = client.get(attempt)
        assert r.status_code != 200 or "create_app" not in r.text, attempt


def test_ws_no_jwt_closes_4401():
    app, _, _ = _make_test_app()
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws"):
            pass
    assert exc.value.code == 4401


def test_ws_invalid_jwt_closes_4401():
    def bad_verifier(_token):
        raise chat_auth.JWTVerificationError("forged")
    app, _, _ = _make_test_app(jwt_verifier=bad_verifier)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/ws", headers={"cf-access-jwt-assertion": "anything"}
        ):
            pass
    assert exc.value.code == 4401


def test_ws_jwt_with_no_email_closes_4401():
    def verifier_no_email(_token):
        return {"sub": "no-email-claim"}
    app, _, _ = _make_test_app(jwt_verifier=verifier_no_email)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/ws", headers={"cf-access-jwt-assertion": "anything"}
        ):
            pass
    assert exc.value.code == 4401


def test_ws_jwt_from_cookie_fallback(monkeypatch):
    """Header absent + cookie present should still authenticate."""
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    seen_tokens = []
    def verifier(token):
        seen_tokens.append(token)
        return {"email": "alice@example.com"}
    ensure = MagicMock(return_value="portfolio-user-aaaaaaaaaaaa")
    app, _, _ = _make_test_app(jwt_verifier=verifier, ensure_container=ensure)
    client = TestClient(app)
    # Set the cookie via TestClient's cookie jar — handshake forwards it.
    client.cookies.set("CF_Authorization", "cookie-token-value")
    try:
        with client.websocket_connect("/ws") as ws:
            # Server accepted; close from our side.
            ws.close()
    except WebSocketDisconnect:
        pass  # close-after-accept also surfaces here
    assert seen_tokens == ["cookie-token-value"]


def test_ws_admin_dispatches_to_ttyd(monkeypatch):
    """role==admin must NOT call ensure_container, and exec_create must
    target portfolio-ttyd."""
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    def verifier(_token):
        return {"email": "boss@example.com"}
    ensure = MagicMock(side_effect=AssertionError("ensure_container must not be called for admin"))
    app, _, docker_client = _make_test_app(jwt_verifier=verifier, ensure_container=ensure)
    client = TestClient(app)
    try:
        with client.websocket_connect(
            "/ws", headers={"cf-access-jwt-assertion": "x"}
        ) as ws:
            ws.close()
    except WebSocketDisconnect:
        pass
    ensure.assert_not_called()
    docker_client.api.exec_create.assert_called_once()
    call_kwargs = docker_client.api.exec_create.call_args
    # exec_create is called as (target_container, cmd=..., stdin=..., stdout=..., stderr=..., tty=...)
    target = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("container")
    assert target == term_app_mod.ADMIN_TTYD_CONTAINER == "portfolio-ttyd"
    assert call_kwargs.kwargs.get("tty") is True
    assert call_kwargs.kwargs.get("cmd") == term_app_mod.ADMIN_SHELL_ARGV


def test_ws_user_dispatches_to_ensure_container(monkeypatch):
    """role==user must call ensure_container and exec_create that name."""
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    def verifier(_token):
        return {"email": "alice@example.com"}
    ensure = MagicMock(return_value="portfolio-user-cafebabe1234")
    app, _, docker_client = _make_test_app(jwt_verifier=verifier, ensure_container=ensure)
    client = TestClient(app)
    try:
        with client.websocket_connect(
            "/ws", headers={"cf-access-jwt-assertion": "x"}
        ) as ws:
            ws.close()
    except WebSocketDisconnect:
        pass
    ensure.assert_called_once_with("alice@example.com")
    docker_client.api.exec_create.assert_called_once()
    call_kwargs = docker_client.api.exec_create.call_args
    target = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("container")
    assert target == "portfolio-user-cafebabe1234"
    assert call_kwargs.kwargs.get("cmd") == term_app_mod.USER_SHELL_ARGV


def test_ws_ensure_container_failure_closes_4500(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    def verifier(_token):
        return {"email": "alice@example.com"}
    ensure = MagicMock(side_effect=RuntimeError("docker says no"))
    app, _, _ = _make_test_app(jwt_verifier=verifier, ensure_container=ensure)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/ws", headers={"cf-access-jwt-assertion": "x"}
        ):
            pass
    assert exc.value.code == 4500
