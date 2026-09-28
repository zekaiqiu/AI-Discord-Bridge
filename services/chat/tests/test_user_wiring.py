"""End-to-end wiring for per-user containers.

These tests exercise the integration between:
  * app.create_session     — provisions ``ensure_user_container`` for non-admin
    emails and persists the resulting name onto the session JSON.
  * storage.create_session — accepts and writes the new ``container`` field.
  * app._run_turn_worker   — passes ``container`` through to claude_runner.
  * claude_runner.run_turn — selects ``dispatch="user"`` when role+container
    are present, ``dispatch="host"`` for admin; a role=user session with no
    container fails CLOSED (``user-fail-closed``, never legacy ``local``) —
    but app.post_message heals such sessions by re-provisioning the
    container before the turn.
  * claude_runner.spawn_claude — wraps argv as ``docker exec -i -w
    /workspace -e HOME=/workspace --user 1000:1000 -e ANTHROPIC_BASE_URL=...
    -e CLAUDE_CODE_OAUTH_TOKEN=... <container> sh -c <pidfile-wrapper> <argv>``
    for dispatch="user".

The tests use the autouse ``_stub_user_container`` fixture from conftest
(monkeypatches ``user_container.ensure_user_container`` to return
``container_name_for(email)`` without talking to the real docker daemon)
and the ``fake_claude`` recorder to capture spawn args / dispatch / container.
"""

from __future__ import annotations

import json
import os

from helpers import consume_sse, create_session  # shared test helpers
from user_container import container_name_for


USER_EMAIL = "test@example.com"
ADMIN_EMAIL = "admin@example.com"


def _read_session_json(tmp_sessions_dir, email, sid):
    """Locate the on-disk session JSON the chat service just wrote."""
    from storage import email_slug
    path = os.path.join(str(tmp_sessions_dir), email_slug(email), f"{sid}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Session creation: persists role + container onto the JSON.
# ---------------------------------------------------------------------------

def test_create_session_persists_container_for_user_role(
    client, auth_headers, monkeypatch, tmp_sessions_dir
):
    """A non-admin email's session JSON has role='user' AND a container
    field naming the deterministic per-user container."""
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    sid = create_session(client, auth_headers(USER_EMAIL))
    on_disk = _read_session_json(tmp_sessions_dir, USER_EMAIL, sid)

    assert on_disk["role"] == "user"
    assert on_disk["container"] == container_name_for(USER_EMAIL)
    # Sanity: the field shape matches the user_container hash convention.
    assert on_disk["container"].startswith("portfolio-user-")


def test_create_session_admin_has_no_container(
    client, auth_headers, monkeypatch, tmp_sessions_dir
):
    """An admin email's session JSON has role='admin' and container=None
    (admin sessions still go through the chat-host-shell sidecar, not a
    per-user container — see acceptance bar item 6)."""
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    sid = create_session(client, auth_headers(ADMIN_EMAIL))
    on_disk = _read_session_json(tmp_sessions_dir, ADMIN_EMAIL, sid)

    assert on_disk["role"] == "admin"
    # storage.create_session always writes the key for forward-compat;
    # admin sessions get None.
    assert on_disk.get("container") in (None, "")


def test_create_session_provisions_via_ensure_user_container(
    client, auth_headers, monkeypatch, tmp_sessions_dir, _stub_user_container
):
    """The session-create handler must call ensure_user_container exactly
    once per non-admin session, and not at all for admin sessions."""
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    # _stub_user_container is the calls-list returned by the autouse fixture.
    create_session(client, auth_headers(USER_EMAIL))
    assert USER_EMAIL in _stub_user_container

    before = list(_stub_user_container)
    create_session(client, auth_headers(ADMIN_EMAIL))
    # No additional ensure-call for admin.
    assert _stub_user_container == before


# ---------------------------------------------------------------------------
# Message dispatch: routes through dispatch=user with the persisted name.
# ---------------------------------------------------------------------------

def test_user_session_message_dispatches_to_user_container(
    client, auth_headers, fake_claude, monkeypatch
):
    """A non-admin session's message turn calls spawn_claude with
    dispatch='user' and container_name=<persisted name>."""
    fake_claude.set_scenario("happy")
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    sid = create_session(client, auth_headers(USER_EMAIL))
    r = client.post(
        f"/api/sessions/{sid}/messages",
        headers=auth_headers(USER_EMAIL),
        json={"text": "ping"},
    )
    assert r.status_code == 200
    consume_sse(r)

    # main_turn_dispatch filters out the parallel title call.
    assert fake_claude.main_turn_dispatch() == "user"
    # Find the main turn entry to read container_name (last_container is racy
    # because the title call also lands).
    main = next(
        d for d in reversed(fake_claude.dispatches)
        if not any(isinstance(a, str) and "Summarize" in a for a in d["args"])
    )
    assert main["container_name"] == container_name_for(USER_EMAIL)


def test_admin_session_message_does_not_use_user_dispatch(
    client, auth_headers, fake_claude, monkeypatch
):
    """Acceptance bar item 6: admin behaviour unchanged. The admin
    session dispatches host, with no container_name."""
    fake_claude.set_scenario("happy")
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    sid = create_session(client, auth_headers(ADMIN_EMAIL))
    r = client.post(
        f"/api/sessions/{sid}/messages",
        headers=auth_headers(ADMIN_EMAIL),
        json={"text": "ping"},
    )
    assert r.status_code == 200
    consume_sse(r)

    assert fake_claude.main_turn_dispatch() == "host"
    main = next(
        d for d in reversed(fake_claude.dispatches)
        if not any(isinstance(a, str) and "Summarize" in a for a in d["args"])
    )
    assert main["container_name"] is None


# ---------------------------------------------------------------------------
# claude_runner.spawn_claude argv shape for dispatch="user".
# Verified through the public run_turn path so we exercise the full chain.
# ---------------------------------------------------------------------------

def test_user_dispatch_wraps_argv_correctly(
    client, auth_headers, fake_claude, monkeypatch
):
    """The argv recorded by the fake spawn for a user dispatch is the
    raw claude argv. The docker-exec wrapping happens INSIDE
    spawn_claude (after our fake intercepts it), so what fake_claude
    sees is the unwrapped argv plus the container_name kwarg.

    This test pins the contract: the runner hands the un-wrapped argv
    plus dispatch+container_name to spawn_claude, and the wrapping is
    spawn_claude's responsibility. A separate unit test in
    test_dispatch_routing covers the actual wrapped shape via
    build_argv (which spawn_claude reuses)."""
    fake_claude.set_scenario("happy")
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    sid = create_session(client, auth_headers(USER_EMAIL))
    r = client.post(
        f"/api/sessions/{sid}/messages",
        headers=auth_headers(USER_EMAIL),
        json={"text": "ping"},
    )
    assert r.status_code == 200
    consume_sse(r)

    main = next(
        d for d in reversed(fake_claude.dispatches)
        if not any(isinstance(a, str) and "Summarize" in a for a in d["args"])
    )
    # The argv begins with `claude` — i.e. raw, NOT pre-wrapped with
    # `docker exec` (that wrapping is spawn_claude's job, downstream of
    # the fake's intercept).
    assert main["args"][0] == "claude"
    assert main["dispatch"] == "user"
    assert main["container_name"] == container_name_for(USER_EMAIL)


# ---------------------------------------------------------------------------
# spawn_claude direct contract: dispatch="user" wraps as expected.
# ---------------------------------------------------------------------------

def test_spawn_claude_user_dispatch_requires_container_name():
    """spawn_claude must reject dispatch='user' without a container_name."""
    import asyncio
    import claude_runner

    async def _drive():
        gen = claude_runner.spawn_claude(
            ["claude"], dispatch="user", container_name=None,
        )
        # The generator should raise ClaudeRunnerError on first iteration.
        try:
            await gen.__anext__()
        except claude_runner.ClaudeRunnerError as exc:
            return str(exc)
        return None

    msg = asyncio.run(_drive())
    assert msg is not None and "container_name" in msg


# ---------------------------------------------------------------------------
# Heal-on-send: role="user" but container missing → post_message re-provisions
# the container and the turn dispatches into it. There is NO legacy-local
# fallback for user sessions any more (that would leak /home/felix).
# ---------------------------------------------------------------------------

def test_user_role_without_container_is_healed_into_user_dispatch(
    client, auth_headers, fake_claude, monkeypatch, tmp_sessions_dir,
    _stub_user_container,
):
    """If a session JSON has role='user' but no container (e.g. the
    ensure_user_container call failed at create time, or the session
    pre-dates per-user containers), ``post_message`` calls
    ``ensure_user_container`` again, persists the healed name, and the turn
    dispatches ``user`` into that container — never ``local``."""
    import claude_runner

    fake_claude.set_scenario("happy")
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_EMAIL)
    monkeypatch.setenv("FELIX_EMAIL", "felix-other@example.org")

    sid = create_session(client, auth_headers(USER_EMAIL))
    # Hand-edit the session JSON to drop the container field, simulating
    # a session created when docker was unavailable.
    on_disk = _read_session_json(tmp_sessions_dir, USER_EMAIL, sid)
    on_disk["container"] = None
    from storage import email_slug
    path = os.path.join(
        str(tmp_sessions_dir), email_slug(USER_EMAIL), f"{sid}.json",
    )
    with open(path, "w", encoding="utf-8") as f:
        json.dump(on_disk, f)

    ensure_calls_before = _stub_user_container.count(USER_EMAIL)
    r = client.post(
        f"/api/sessions/{sid}/messages",
        headers=auth_headers(USER_EMAIL),
        json={"text": "ping"},
    )
    assert r.status_code == 200
    consume_sse(r)

    # Healed: ensure_user_container ran again, the name is persisted, and
    # the main turn went into the per-user container.
    assert _stub_user_container.count(USER_EMAIL) == ensure_calls_before + 1
    healed = _read_session_json(tmp_sessions_dir, USER_EMAIL, sid)
    assert healed["container"] == container_name_for(USER_EMAIL)
    assert fake_claude.main_turn_dispatch() == "user"
    main = next(
        d for d in reversed(fake_claude.dispatches)
        if not any(isinstance(a, str) and "Summarize" in a for a in d["args"])
    )
    assert main["container_name"] == container_name_for(USER_EMAIL)

    # Runner-side contract the heal exists to satisfy: an un-healed
    # role=user/no-container session fails closed, it never goes local.
    assert claude_runner.resolve_dispatch("user", None) == "user-fail-closed"
    assert claude_runner.resolve_dispatch("user", healed["container"]) == "user"


# ===========================================================================
# User-dispatch argv runs claude as uid 1000 with HOME=/workspace and points
# it at the in-container auth proxy (the real credential is 0400 uid 2000 and
# unreadable to claude; the proxy injects the real bearer). Admin-dispatch
# argv carries NONE of the per-user identity additions.
# These tests exercise spawn_claude DIRECTLY (no fake_claude intercept) so
# we observe the post-wrap argv as it would hit asyncio.create_subprocess_exec.
# ===========================================================================


import asyncio as _asyncio  # noqa: E402  (intentional late import)

import claude_runner as _cr  # noqa: E402
import user_container as _uc  # noqa: E402


class _RecordedProc:
    """Drop-in for the asyncio Process returned by create_subprocess_exec.

    Yields one stream-json line then EOF, then exits clean. Just enough for
    spawn_claude to iterate once and complete without raising. Tests only
    care about the argv we recorded, not the streaming behavior.
    """

    def __init__(self):
        self.returncode = None
        self.pid = 1
        self.stdin = None
        self._lines = [b'{"type":"text","text":"x"}\n', b""]
        self._idx = 0

        class _Stream:
            def __init__(self, parent):
                self._parent = parent

            async def readline(self):
                if self._parent._idx >= len(self._parent._lines):
                    return b""
                line = self._parent._lines[self._parent._idx]
                self._parent._idx += 1
                return line

            async def read(self):
                return b""

        self.stdout = _Stream(self)
        self.stderr = _Stream(self)

    async def wait(self):
        self.returncode = 0
        return 0


def _run_spawn_and_capture(monkeypatch, *, dispatch, container_name=None, account=None):
    """Drive spawn_claude once and return the argv of the claude exec.

    spawn_claude makes a SECOND create_subprocess_exec after the turn (the
    in-container pidfile reap), so record every spawn and return the first.
    """
    captured = {"spawns": []}

    async def fake_create(*argv, **kwargs):
        captured["spawns"].append({"argv": list(argv), "env": kwargs.get("env")})
        return _RecordedProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_create)

    # Stub the credential refresh so the user branch doesn't need a real
    # subprocess / docker daemon.
    monkeypatch.setattr(
        _uc, "refresh_credentials_if_stale", lambda *_a, **_kw: None,
    )

    async def _drive():
        gen = _cr.spawn_claude(
            ["claude", "-p", "hi"],
            dispatch=dispatch,
            container_name=container_name,
            account=account,
        )
        async for _ in gen:
            pass

    _asyncio.run(_drive())
    assert captured["spawns"], "spawn_claude never exec'd anything"
    return captured["spawns"][0]["argv"]


def test_user_dispatch_argv_runs_as_uid_1000_with_workspace_home(monkeypatch):
    argv = _run_spawn_and_capture(
        monkeypatch,
        dispatch="user",
        container_name="portfolio-user-abc123def456",
        account="account-1",
    )
    container_idx = argv.index("portfolio-user-abc123def456")
    prefix = argv[:container_idx]
    # `--user 1000:1000` is a contiguous pair in the docker-exec prefix
    # (before the container name). uid 1000 is the user shell; the real
    # credential (0400 uid 2000) is unreadable to it by design.
    idx = prefix.index("--user")
    assert prefix[idx + 1] == "1000:1000"
    assert "2000:2000" not in argv
    # `-e HOME=/workspace` is a contiguous pair; the old runner home is gone.
    home_indices = [i for i, a in enumerate(prefix) if a == "HOME=/workspace"]
    assert home_indices, f"expected HOME=/workspace in argv: {argv}"
    assert prefix[home_indices[-1] - 1] == "-e"
    assert not any(a.startswith("HOME=/var/claude-runner") for a in argv)
    # claude is pointed at the loopback auth proxy with a read-only placeholder
    # bearer (env, not file — so claude can't self-wipe it).
    assert prefix[prefix.index("-w") + 1] == "/workspace"
    env_pairs = [prefix[i + 1] for i, a in enumerate(prefix[:-1]) if a == "-e"]
    assert f"ANTHROPIC_BASE_URL=http://127.0.0.1:{_uc.AUTH_PROXY_PORT}" in env_pairs
    assert f"CLAUDE_CODE_OAUTH_TOKEN={_uc.DUMMY_ACCESS_TOKEN}" in env_pairs
    # The original `claude` argv is the tail, after the pidfile wrapper.
    assert argv[-3:] == ["claude", "-p", "hi"]


def test_admin_dispatch_argv_has_neither_user_nor_home_var_claude_runner(monkeypatch):
    argv = _run_spawn_and_capture(monkeypatch, dispatch="host")
    # The admin/host path must NOT carry the per-user identity additions.
    assert "--user" not in argv
    assert "1000:1000" not in argv
    assert "2000:2000" not in argv
    assert "HOME=/workspace" not in argv
    assert "HOME=/var/claude-runner" not in argv
    assert not any(a.startswith("ANTHROPIC_BASE_URL=") for a in argv)
    assert not any(
        isinstance(a, str) and a.startswith("HOME=/var/claude-runner") for a in argv
    )
    # Sanity: it IS the host-shell wrap.
    assert "portfolio-chat-host-shell" in argv


def test_user_dispatch_calls_refresh_credentials_if_stale(monkeypatch):
    """The user branch MUST call refresh_credentials_if_stale before exec
    so credentials are fresh on every turn (subject to the 60s rate cap)."""
    seen = []

    def fake_refresh(container_name, account):
        seen.append((container_name, account))

    monkeypatch.setattr(_uc, "refresh_credentials_if_stale", fake_refresh)

    async def fake_create(*argv, **kwargs):
        return _RecordedProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_create)

    async def _drive():
        gen = _cr.spawn_claude(
            ["claude", "-p", "hi"],
            dispatch="user",
            container_name="portfolio-user-cafe1234abcd",
            account="account-1",
        )
        async for _ in gen:
            pass

    _asyncio.run(_drive())
    assert seen == [("portfolio-user-cafe1234abcd", "account-1")]


def test_admin_dispatch_does_not_call_refresh_credentials_if_stale(monkeypatch):
    """Acceptance bar: admin path is byte-for-byte unchanged. No refresh call."""
    seen = []

    def fake_refresh(container_name, account):
        seen.append((container_name, account))

    monkeypatch.setattr(_uc, "refresh_credentials_if_stale", fake_refresh)

    async def fake_create(*argv, **kwargs):
        return _RecordedProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_create)

    async def _drive():
        gen = _cr.spawn_claude(["claude", "-p", "hi"], dispatch="host")
        async for _ in gen:
            pass

    _asyncio.run(_drive())
    assert seen == []


# ===========================================================================
# Credential isolation negative case (unit form): the file populate_credentials
# writes goes in mode 0400 owner 2000:2000, which is what produces the
# permission-denied for uid 1000 inside the container.
# Per the brief, either a unit assertion of the file mode/owner OR a gated
# integration test is acceptable — we pick the unit form to match the rest
# of the chat suite's mocked-subprocess style.
# ===========================================================================


def test_credentials_isolation_unit_assertion_mode_0400_owner_2000(monkeypatch, tmp_path):
    """Drive populate_credentials and assert the shell snippet that lands
    on the per-user container chmods 0400 + chowns 2000:2000. uid 1000
    inside the container therefore gets EACCES on read — that's the
    isolation property under test."""
    captured = []

    class _CP:
        def __init__(self):
            self.returncode = 0
            self.stdout = b""
            self.stderr = b""

    def fake_run(argv, *, input=None, capture_output=True, check=True):
        captured.append({"argv": list(argv), "stdin": input})
        return _CP()

    monkeypatch.setattr(_uc.subprocess, "run", fake_run)

    # _host_credentials_path resolves the account HOME through
    # account_router.home_for_account (the old CLAUDE_ACCOUNTS_HOST_PATH
    # constant is dead); point that seam at a tmp layout.
    import account_router

    fake_root = tmp_path / "claude-accounts"
    (fake_root / "account-1" / ".claude").mkdir(parents=True)
    src_bytes = b'{"access_token":"sekret","refresh_token":"r"}'
    (fake_root / "account-1" / ".claude" / ".credentials.json").write_bytes(src_bytes)
    monkeypatch.setattr(account_router, "home_for_account", lambda name: fake_root / name)

    _uc.populate_credentials("portfolio-user-isolate", "account-1")

    assert len(captured) == 1
    call = captured[0]
    joined = " ".join(call["argv"])
    # The exact triple that produces the isolation property:
    #   1. file written from stdin
    #   2. chowned to 2000:2000 (so uid 1000 is no longer the owner)
    #   3. chmoded 0400 (owner-read-only; group=none, other=none → EACCES
    #      for uid 1000 even if it had been left as the implicit owner).
    assert "cat > /var/claude-runner/.claude/.credentials.json" in joined
    assert "chown 2000:2000 /var/claude-runner/.claude/.credentials.json" in joined
    assert "chmod 0400 /var/claude-runner/.claude/.credentials.json" in joined
    # Source bytes match what we wrote on the host.
    assert call["stdin"] == src_bytes
    # And the exec runs as root — required to chown to a different uid.
    assert call["argv"][:5] == ["docker", "exec", "-i", "--user", "root"]
