"""Shared pytest fixtures for the chat service.

These fixtures keep tests hermetic:

  * ``tmp_sessions_dir`` — points ``CHAT_SESSIONS_DIR`` at a tmp path.
  * ``jwks_keypair`` — session-scoped RSA keypair so we don't burn CPU
     generating a new key per test.
  * ``fake_jwks`` — patches the single seam ``auth._fetch_jwks_json`` so
     verification never touches the network.
  * ``mint_jwt`` — factory for issuing tokens with arbitrary claims; tests
     pass overrides to drive the failure paths.
  * ``client`` — FastAPI TestClient with sessions dir + JWKS already wired.
  * ``auth_headers`` — short-hand to attach a default-valid token for an email.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
# A1: the existing worker tests drive the one-shot path (they patch
# claude_runner.run_turn). Persistent sessions default ON in prod, so pin them
# OFF for the suite — the persistent path has its own coverage (session_process
# / manager unit tests + a live per-user-container integration test). Set
# before app import so the per-call flag reader sees it.
os.environ.setdefault("CHAT_PERSISTENT_SESSIONS", "0")
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwt

# Make ``import app`` / ``import auth`` resolve to the service modules
# regardless of where pytest is invoked from.
_SERVICE_DIR = Path(__file__).resolve().parent
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))


def _b64url_uint(value: int) -> str:
    """Encode an int as a JWKS-style base64url-without-padding string."""
    byte_length = (value.bit_length() + 7) // 8 or 1
    raw = value.to_bytes(byte_length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


@pytest.fixture(scope="session")
def jwks_keypair() -> dict[str, Any]:
    """Generate one RSA keypair + the JWKS document that advertises it."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_numbers = private_key.public_key().public_numbers()
    kid = "test-key-1"
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": kid,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_uint(public_numbers.n),
                "e": _b64url_uint(public_numbers.e),
            }
        ]
    }
    return {"private_pem": pem, "kid": kid, "jwks": jwks}


@pytest.fixture
def tmp_sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    sessions = tmp_path / "sessions"
    monkeypatch.setenv("CHAT_SESSIONS_DIR", str(sessions))
    return sessions


@pytest.fixture
def fake_jwks(monkeypatch: pytest.MonkeyPatch, jwks_keypair: dict[str, Any]) -> dict[str, Any]:
    """Wire the auth module to the in-memory JWKS and set Cf-Access env."""
    import auth  # local import: ensures sys.path tweak above has taken effect

    monkeypatch.setenv("CF_ACCESS_TEAM", "test")
    monkeypatch.setenv("CF_ACCESS_AUD", "test-aud")

    def _fake_fetch(url: str) -> dict[str, Any]:
        return jwks_keypair["jwks"]

    monkeypatch.setattr(auth, "_fetch_jwks_json", _fake_fetch)
    # Pre-yield reset is load-bearing: pytest reuses one process across
    # test files, and the JWKS cache key includes the JWKS URL. If a
    # prior test left a stale entry under the same URL, the first
    # ``verify_jwt`` call here would hit it before our monkeypatched
    # fetcher ran. Resetting on entry guarantees a fresh fetch.
    auth._reset_jwks_cache()
    yield jwks_keypair["jwks"]
    auth._reset_jwks_cache()


@pytest.fixture
def mint_jwt(jwks_keypair: dict[str, Any]) -> Callable[..., str]:
    """Return a factory that mints signed JWTs.

    Defaults produce a token that PASSES verification under ``fake_jwks``.
    Override individual kwargs to exercise failure modes.
    """

    def _mint(
        *,
        email: str = "user@example.com",
        sub: str | None = None,
        aud: str = "test-aud",
        iss: str = "https://test.cloudflareaccess.com",
        exp_offset: int = 3600,
        kid: str | None = None,
        key_pem: str | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "sub": sub or f"sub-{uuid.uuid4()}",
            "email": email,
            "aud": aud,
            "iss": iss,
            "iat": now,
            "exp": now + exp_offset,
        }
        if extra_claims:
            claims.update(extra_claims)
        headers = {"kid": kid or jwks_keypair["kid"]}
        return jwt.encode(
            claims,
            key_pem or jwks_keypair["private_pem"],
            algorithm="RS256",
            headers=headers,
        )

    return _mint


@pytest.fixture
def client(
    fake_jwks: dict[str, Any],
    tmp_sessions_dir: Path,
    tmp_attachments_dir: Path,
) -> Any:
    # Importing here (rather than at module top) keeps a no-env baseline
    # check possible from other contexts — and lets ``fake_jwks`` set env
    # before app reads anything.
    #
    # ``tmp_attachments_dir`` is wired in unconditionally per the Phase 2
    # brief: SSE / message tests should not have to opt in to a sandboxed
    # attachments path, and the cost for non-attachment tests is just an
    # unused tmp directory.
    #
    # Enter TestClient as a context manager so its anyio portal persists
    # across requests within one test. Without this, Starlette spins up a
    # fresh portal per request and tears it down at request end — which
    # cancels any background asyncio.create_task() (e.g. our title task)
    # before it can run. The persistent portal lets background tasks
    # outlive the SSE response, exactly as they do in production uvicorn.
    from app import app as fastapi_app

    with TestClient(fastapi_app) as tc:
        yield tc


@pytest.fixture
def auth_headers(mint_jwt: Callable[..., str]) -> Callable[[str], dict[str, str]]:
    def _headers(email: str) -> dict[str, str]:
        return {"Cf-Access-Jwt-Assertion": mint_jwt(email=email)}
    return _headers


# ---------------------------------------------------------------------------
# Phase 2 fixtures: attachments dir, fake claude runner.
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_attachments_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    attachments_dir = tmp_path / "attachments"
    monkeypatch.setenv("CHAT_ATTACHMENTS_DIR", str(attachments_dir))
    return attachments_dir


@pytest.fixture(autouse=True)
def _stub_user_container(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace ``user_container.ensure_user_container`` with a deterministic
    fake for ALL chat-service tests.

    Without this, the multi-user wiring in ``app.create_session`` would
    talk to the real docker daemon every time a test creates a non-admin
    session. The fake returns the same deterministic name the real
    function would (``container_name_for(email)``) so tests that assert
    on the persisted ``container`` field still see a meaningful value.

    The fake is keyed by email so a test that wants to assert "my fake
    was called for THIS email" can still check ``recorder.calls``.
    """
    import user_container as _uc

    calls: list[str] = []

    def _fake_ensure(email: str, client: Any = None) -> str:
        calls.append(email)
        return _uc.container_name_for(email)

    monkeypatch.setattr(_uc, "ensure_user_container", _fake_ensure)
    # Also patch the binding inside app.py which imported the module
    # by name (`import user_container`). monkeypatch.setattr on the
    # module object covers both `user_container.ensure_user_container`
    # and `app.user_container.ensure_user_container` because they are
    # the same module object.
    return calls


class _FakeClaudeRecorder:
    """Captures every (args, stdin) tuple ``spawn_claude`` was called with.

    Tests use this to assert that the JWT email was never inlined into
    args/stdin (the brief's email-leak rule), and to inspect the argv for
    presence/absence of ``--add-dir``.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        # See fake_spawn — list of {args, dispatch, home} per call. Useful
        # when the main turn and the parallel title task both spawn and
        # the test wants to assert the main turn's dispatch specifically.
        self.dispatches: list[dict[str, Any]] = []

    def record(self, args: list[str], stdin: str | None) -> None:
        self.calls.append({"args": list(args), "stdin": stdin})

    @property
    def all_text(self) -> str:
        """One blob containing every arg + stdin we've ever seen.

        Convenient for ``assert email not in recorder.all_text``.
        """
        parts: list[str] = []
        for c in self.calls:
            parts.extend(str(a) for a in c["args"])
            if c["stdin"]:
                parts.append(c["stdin"])
        return "\n".join(parts)


def _stream_json_lines(*objs: dict[str, Any]) -> list[bytes]:
    return [json.dumps(o).encode("utf-8") for o in objs]


# Pre-baked stream-json line sets per scenario. Kept module-level so a test
# can import and assemble custom scenarios if needed.
_HAPPY_LINES = _stream_json_lines(
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hello "}},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "world"}},
)
_TOOL_LINES = _stream_json_lines(
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "thinking... "}},
    {"type": "content_block_start",
     "content_block": {"type": "tool_use", "name": "search", "input": {"q": "x"}}},
    {"type": "tool_result", "name": "search"},
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}},
)
_TITLE_LINES = _stream_json_lines(
    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Brief Title"}},
)


@pytest.fixture
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Replace ``claude_runner.spawn_claude`` with a configurable in-memory fake.

    Returns a controller object with:
      * ``set_scenario(name)`` — switch the active scenario.
      * ``calls`` / ``all_text`` — recorded invocations.
      * ``set_lines(lines)`` — drop in a custom byte-line list (per turn).

    Scenarios:
      * ``"happy"`` — two delta lines.
      * ``"tool"`` — delta, tool_use, tool_result, delta.
      * ``"crash"`` — raises ``ClaudeRunnerError`` on first iteration.
      * ``"malformed"`` — yields one non-JSON line.
      * ``"timeout"`` — sleeps past the runner's wait_for; tests pass a
        small ``timeout`` to ``run_turn`` so this is fast.
      * ``"slow_title"`` — main turn behaves like ``"happy"``; the title
        invocation (detected by "Summarize" appearing in the prompt
        argv) sleeps 2s before yielding a title line. Used by the
        title-doesn't-block-the-response test.
    """
    import claude_runner as _runner

    recorder = _FakeClaudeRecorder()
    state: dict[str, Any] = {"scenario": "happy", "lines": None}

    class _Controller:
        scenario = "happy"

        def set_scenario(self, name: str) -> None:
            state["scenario"] = name

        def set_lines(self, lines: list[bytes]) -> None:
            state["lines"] = list(lines)

        @property
        def calls(self) -> list[dict[str, Any]]:
            return recorder.calls

        @property
        def all_text(self) -> str:
            return recorder.all_text

        @property
        def last_home(self) -> str | None:
            return getattr(recorder, "last_home", None)

        @property
        def last_dispatch(self) -> str | None:
            return getattr(recorder, "last_dispatch", None)

        @property
        def last_container(self) -> str | None:
            return getattr(recorder, "last_container", None)

        @property
        def dispatches(self) -> list[dict[str, Any]]:
            return recorder.dispatches

        def main_turn_dispatch(self) -> str | None:
            """Return the dispatch of the most recent main-turn spawn.

            Filters out title calls (which use a Summarize prompt). Useful
            when both the main turn and a parallel title task spawned.
            """
            for d in reversed(recorder.dispatches):
                args = d["args"]
                is_title = any(isinstance(a, str) and "Summarize" in a for a in args)
                if not is_title:
                    return d["dispatch"]
            return None

    controller = _Controller()

    async def _yield_lines(lines: list[bytes]) -> AsyncIterator[bytes]:
        for ln in lines:
            await asyncio.sleep(0)  # cooperative scheduling
            yield ln

    def _is_title_call(args: list[str]) -> bool:
        # Heuristic: the title prompt template begins with "Summarize".
        for a in args:
            if isinstance(a, str) and "Summarize" in a:
                return True
        return False

    async def fake_spawn(
        args: list[str],
        stdin: str | None = None,
        *,
        home: str | None = None,
        dispatch: str = "local",
        container_name: str | None = None,
        account: str | None = None,
    ) -> AsyncIterator[bytes]:
        # Capture ``home``, ``dispatch``, and ``container_name`` alongside
        # args so tests can assert which account the runner picked
        # (multi-account routing), whether the admin host-shell dispatch
        # fired, and which per-user container a user-dispatch landed in.
        # Recorder API is extended via attribute rather than positional
        # to keep existing callers untouched.
        recorder.record(args, stdin)
        recorder.last_home = home
        recorder.last_dispatch = dispatch
        recorder.last_container = container_name
        # Also append to a per-call dispatch list — the main turn and the
        # parallel title task both call spawn_claude, so a single
        # ``last_dispatch`` is racy when tests want to assert on the main
        # turn specifically. Title calls have "Summarize" in args; the
        # main turn does not. Tests can filter by that.
        recorder.dispatches.append({
            "args": list(args),
            "dispatch": dispatch,
            "home": home,
            "container_name": container_name,
        })
        scenario = state["scenario"]
        custom = state["lines"]

        if custom is not None:
            async for ln in _yield_lines(custom):
                yield ln
            return

        if scenario == "happy":
            async for ln in _yield_lines(_HAPPY_LINES):
                yield ln
            return
        if scenario == "tool":
            async for ln in _yield_lines(_TOOL_LINES):
                yield ln
            return
        if scenario == "crash":
            # Raise BEFORE yielding anything so the runner's first __anext__
            # surfaces the error event. The ``yield`` below is unreachable
            # but is what makes Python treat this function as an async
            # generator (which the seam contract requires).
            from claude_runner import ClaudeRunnerError
            raise ClaudeRunnerError("boom")
            yield b""  # pragma: no cover
        if scenario == "malformed":
            yield b"not-json {"
            return
        if scenario == "timeout":
            # Sleep well past any sensible test timeout.
            await asyncio.sleep(60)
            yield b'{"type":"text","text":"too late"}'
            return
        if scenario == "slow_title":
            if _is_title_call(args):
                await asyncio.sleep(2.0)
                async for ln in _yield_lines(_TITLE_LINES):
                    yield ln
                return
            async for ln in _yield_lines(_HAPPY_LINES):
                yield ln
            return

        raise AssertionError(f"unknown fake_claude scenario: {scenario}")

    monkeypatch.setattr(_runner, "spawn_claude", fake_spawn)
    return controller


@pytest.fixture
async def drain_background_tasks() -> Any:
    """Helper fixture: ``await drain()`` to wait for all spawned title tasks.

    Tests use this rather than ``asyncio.sleep``-and-pray so the title
    test is deterministic regardless of scheduler timing.
    """
    from app import _background_tasks

    async def _drain(timeout: float = 5.0) -> None:
        # Snapshot then await; new tasks added during drain are NOT awaited
        # (a title task can't legitimately spawn another title task).
        pending = list(_background_tasks)
        if not pending:
            return
        try:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=timeout)
        except asyncio.TimeoutError:
            return

    return _drain
