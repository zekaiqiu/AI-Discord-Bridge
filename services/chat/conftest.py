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

Hermetic guards (autouse, every test — see the block at the bottom):

  * ``no_network`` — ``urllib.request.urlopen`` / ``httpx.get`` & co raise
    AssertionError, so nothing can reach api.anthropic.com or Cloudflare.
  * ``hermetic_accounts`` — ``account_router`` is re-pointed at a tmp
    ``main`` home + a tmp wizerith pool (``account-1``, ``account-2``) with
    fake credential files, and ``_fetch_usage`` returns a canned snapshot.
  * ``fake_docker`` — ``docker.from_env()`` returns a MagicMock client whose
    ``containers.get`` raises ``docker.errors.NotFound`` (opt in by
    requesting the fixture and configuring the mock).
  * ``hermetic_host_paths`` — ``/data/*`` and the host token-refresh script
    are redirected under ``tmp_path``.
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
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable
from unittest.mock import MagicMock

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


# ---------------------------------------------------------------------------
# Hermetic guards (autouse for EVERY test).
#
# Before these existed the suite reached production from inside pytest:
#   * ``account_router.pick()`` / ``is_usable()`` / ``snapshot_usage()`` hit
#     the real https://api.anthropic.com/api/oauth/usage with real tokens read
#     from /home/felix/.claude and /opt/wizerith/claude-accounts — which
#     429-rate-limited the LIVE chat service's router as a side effect of
#     running the tests.
#   * ``docker.from_env()`` was called from app startup (``_heal_auth_proxies``
#     — which then docker-exec'd into every live portfolio-user-* container),
#     the ``post_message`` heal path, ``_collect_user_container_artifacts``,
#     ``claude_runner._stage_attachments_into_user_container`` and
#     ``user_container.ensure_*_container``.
#   * ``user_container._host_credentials_path`` (via
#     ``account_router.home_for_account``) streamed REAL credential bytes into
#     the exec recorder, and the refresh path could shell out to
#     ~/.local/bin/refresh-claude-tokens.
#
# Rule of thumb for new tests: never undo these; opt in to a richer fake by
# requesting ``fake_docker`` / ``hermetic_accounts`` and configuring them.
# ---------------------------------------------------------------------------

FAKE_USAGE_SNAPSHOT: dict[str, Any] = {
    "five_hour": {"utilization": 5.0, "resets_at": "2099-01-01T00:00:00Z"},
    "seven_day": {"utilization": 10.0, "resets_at": "2099-01-01T00:00:00Z"},
}
# Wizerith pool seeded under the tmp accounts root. ``main`` also exists but
# is excluded from ``pick()`` by production's CHAT_ACCOUNT_EXCLUDE default.
FAKE_POOL_ACCOUNTS: tuple[str, ...] = ("account-1", "account-2")


def write_fake_credentials(home: Path, name: str) -> Path:
    """Write a well-shaped, NON-secret ``.claude/.credentials.json`` under
    ``home`` and return its path. Token ``tok-<name>``; expiry far enough out
    that ``account_router.is_usable`` treats the account as alive."""
    creds = home / ".claude" / ".credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({
        "claudeAiOauth": {
            "accessToken": f"tok-{name}",
            "refreshToken": f"refresh-{name}",
            "expiresAt": int((time.time() + 24 * 3600) * 1000),
            "scopes": ["user:inference", "user:profile"],
            "subscriptionType": "max",
            "rateLimitTier": "default",
        }
    }), encoding="utf-8")
    return creds


@pytest.fixture(autouse=True)
def hermetic_token_ledger(tmp_path_factory: pytest.TempPathFactory,
                          monkeypatch: pytest.MonkeyPatch) -> Any:
    """Point the token ledger at a per-test tmp DB so the suite never writes
    rows into the real /home/felix/.local/state/token-ledger/ledger.db."""
    import token_ledger
    d = tmp_path_factory.mktemp("ledger")
    monkeypatch.setenv("TOKEN_LEDGER_DB", str(d / "ledger.db"))
    monkeypatch.setenv("TOKEN_LEDGER_PRICES", str(d / "prices.json"))
    token_ledger._current.set(None)
    yield


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Any]:
    """Make every outbound HTTP call from the service modules fail loudly.

    ``account_router._fetch_usage`` and ``auth._default_certs_fetcher`` use
    ``urllib.request.urlopen``; ``auth._fetch_jwks_json`` uses ``httpx.get``.
    The in-container auth-proxy health probe also calls ``urlopen`` but does
    so INSIDE the per-user container via ``docker exec`` (i.e. through
    ``subprocess.run``), so it is unaffected by this process-level guard.
    """
    import urllib.request

    import httpx

    def _blocked(*args: Any, **kwargs: Any) -> Any:
        target = args[0] if args else kwargs.get("url")
        target = getattr(target, "full_url", target)
        raise AssertionError(
            f"network access attempted from a test: {target!r} — tests must "
            "stay hermetic (see conftest.py hermetic guards)"
        )

    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    for name in ("get", "post", "put", "patch", "delete", "head", "request", "stream"):
        monkeypatch.setattr(httpx, name, _blocked)
    return _blocked


@pytest.fixture(autouse=True)
def hermetic_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_network: Any,
) -> Any:
    """Re-point ``account_router`` at a tmp account layout and can its usage
    fetch, so routing never reads real credentials or calls the usage API.

    Returns a namespace: ``main_home``, ``accounts_root``, ``accounts``,
    ``usage`` (the canned snapshot) and ``real_fetch_usage`` (the un-patched
    function, so a test can prove the network guard trips it).
    """
    import account_router
    import user_container

    root = tmp_path / "hermetic-accounts"
    main_home = root / "main_home"
    write_fake_credentials(main_home, "main")
    accounts_root = root / "claude-accounts"
    for name in FAKE_POOL_ACCOUNTS:
        write_fake_credentials(accounts_root / name, name)

    # The module constants are computed at import time from these env vars;
    # set both so a re-import in a test sees the same layout.
    monkeypatch.setenv("CHAT_MAIN_HOME", str(main_home))
    monkeypatch.setenv("WIZERITH_ACCOUNTS_ROOT", str(accounts_root))
    monkeypatch.setattr(account_router, "MAIN_HOME", main_home)
    monkeypatch.setattr(
        account_router, "MAIN_CREDENTIALS", main_home / ".claude" / ".credentials.json",
    )
    monkeypatch.setattr(account_router, "WIZERITH_ACCOUNTS_ROOT", accounts_root)

    real_fetch_usage = account_router._fetch_usage

    def _canned_usage(access_token: str) -> dict[str, Any] | None:
        if not access_token:
            return None
        return json.loads(json.dumps(FAKE_USAGE_SNAPSHOT))  # fresh copy per call

    monkeypatch.setattr(account_router, "_fetch_usage", _canned_usage)

    # Module-level caches: never let one test's routing state leak into the
    # next (the usage cache is keyed by account NAME, which tests reuse).
    account_router._reset_for_tests()
    user_container._REFRESH_CACHE.clear()
    user_container._STREAMED_ACCOUNT.clear()
    yield SimpleNamespace(
        main_home=main_home,
        accounts_root=accounts_root,
        accounts=FAKE_POOL_ACCOUNTS,
        usage=FAKE_USAGE_SNAPSHOT,
        real_fetch_usage=real_fetch_usage,
    )
    account_router._reset_for_tests()
    user_container._REFRESH_CACHE.clear()
    user_container._STREAMED_ACCOUNT.clear()


@pytest.fixture(autouse=True)
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``docker.from_env`` with a MagicMock client for every test.

    Default posture is "empty daemon": ``containers.get`` / ``networks.get``
    / ``volumes.get`` raise ``docker.errors.NotFound`` and
    ``containers.list`` returns ``[]``. A test that needs a container to
    exist requests this fixture and sets e.g.
    ``fake_docker.containers.get.side_effect = None;
    fake_docker.containers.get.return_value = MagicMock()``.

    All production call sites use ``docker.from_env()`` on the module object
    (``app``, ``claude_runner``, ``user_container``, ``haihub_runner``,
    ``user_container_eviction``), so patching the one attribute covers them.
    """
    import docker
    import docker.errors

    client = MagicMock(name="hermetic_docker_client")
    client.containers.get.side_effect = docker.errors.NotFound(
        "hermetic test double: no such container",
    )
    client.containers.list.return_value = []
    client.networks.get.side_effect = docker.errors.NotFound(
        "hermetic test double: no such network",
    )
    client.volumes.get.side_effect = docker.errors.NotFound(
        "hermetic test double: no such volume",
    )
    monkeypatch.setattr(docker, "from_env", lambda *a, **kw: client)
    return client


@pytest.fixture(autouse=True)
def hermetic_host_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every host/``/data`` path the service reads or writes.

    ``tmp_sessions_dir`` / ``tmp_attachments_dir`` set the same two values
    (same ``tmp_path`` sub-dirs), so tests that request them explicitly see
    no difference; this just makes the redirect unconditional.
    """
    import user_container

    monkeypatch.setenv("CHAT_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("CHAT_ATTACHMENTS_DIR", str(tmp_path / "attachments"))
    monkeypatch.setenv(
        "CHAT_ATTACHMENT_PREVIEWS_DIR", str(tmp_path / "attachment_previews"),
    )
    monkeypatch.setenv("CHAT_GENERATED_DIR", str(tmp_path / "generated"))
    monkeypatch.setenv(
        user_container.USER_NETWORK_ALLOCATIONS_ENV,
        str(tmp_path / "user-network-allocations.json"),
    )
    # The host token-refresh script is resolved into a module constant at
    # import time; point both the env and the constant at a path that does
    # not exist so ``_host_refresh_tokens_if_needed`` can only ever no-op.
    refresh_script = str(tmp_path / "no-such-refresh-claude-tokens")
    monkeypatch.setenv("ANTHROPIC_HOST_REFRESH_SCRIPT", refresh_script)
    monkeypatch.setattr(user_container, "HOST_TOKEN_REFRESH_SCRIPT", refresh_script)
    return tmp_path
