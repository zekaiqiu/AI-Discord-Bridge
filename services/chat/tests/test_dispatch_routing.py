"""Per-user dispatch routing tests.

Covers the multi-user-containers code paths added in:
  - auth.resolve_role (env-driven admin allowlist)
  - claude_runner.build_argv (host vs user dispatch shape)
  - run_turn.create_session / run_turn / run_title_call
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# user_container is imported here so test_run_turn_reentry_no_reprovision can
# monkeypatch.setattr(user_container, "ensure_user_container", ...).
import auth
import claude_runner
import run_turn
import sessions
import user_container
from claude_runner import DOCKER_EXEC_PREFIX, build_argv


# Group A: auth.resolve_role

def test_resolve_role_admin_exact_match(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "victorchiu2003@gmail.com,supzekai@gmail.com")
    assert auth.resolve_role("victorchiu2003@gmail.com") == "admin"
    assert auth.resolve_role("supzekai@gmail.com") == "admin"


def test_resolve_role_admin_case_insensitive(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "alice@example.com")
    assert auth.resolve_role("ALICE@Example.COM") == "admin"
    assert auth.resolve_role("Alice@Example.com") == "admin"


def test_resolve_role_admin_whitespace_tolerant(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "  alice@example.com ,  bob@example.com  ")
    assert auth.resolve_role("alice@example.com") == "admin"
    assert auth.resolve_role("bob@example.com") == "admin"


def test_resolve_role_user_default(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "alice@example.com")
    assert auth.resolve_role("eve@example.com") == "user"


def test_resolve_role_no_env_all_user(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    assert auth.resolve_role("alice@example.com") == "user"
    assert auth.resolve_role("anyone@anywhere.com") == "user"


# Group B: claude_runner.build_argv

def test_build_argv_host_passthrough():
    argv_in = ["claude", "-p", "hi"]
    out = build_argv(argv_in, dispatch="host")
    # Element-wise equal — copy or same list both acceptable.
    assert out == argv_in
    assert len(out) == 3


def test_build_argv_user_prefix_exact():
    out = build_argv(
        ["claude", "-p", "hi"],
        dispatch="user",
        user_container_name="portfolio-user-abc123def456",
    )
    expected = [
        "docker", "exec", "-i", "-w", "/workspace", "-e", "HOME=/workspace",
        "portfolio-user-abc123def456",
        "claude", "-p", "hi",
    ]
    assert out == expected


def test_build_argv_user_requires_container():
    with pytest.raises(ValueError):
        build_argv(["claude"], dispatch="user", user_container_name=None)
    with pytest.raises(ValueError):
        build_argv(["claude"], dispatch="user", user_container_name="")


def test_build_argv_unknown_dispatch_raises():
    with pytest.raises(ValueError):
        build_argv(["claude"], dispatch="banana")


def test_build_argv_uses_module_constant():
    out = build_argv(
        ["claude", "-p", "hi"],
        dispatch="user",
        user_container_name="portfolio-user-aaaaaaaaaaaa",
    )
    # First N elements of build_argv output (under user dispatch) must equal
    # DOCKER_EXEC_PREFIX exactly. Proves no drift between constant and code.
    assert out[: len(DOCKER_EXEC_PREFIX)] == DOCKER_EXEC_PREFIX


# Group C: run_turn.create_session

@pytest.fixture
def session_env(monkeypatch, tmp_path):
    """Point sessions at tmp_path and clear ADMIN_EMAILS by default."""
    monkeypatch.setenv("PORTFOLIO_SESSIONS_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    return tmp_path


def test_create_session_user_provisions_container(session_env, monkeypatch):
    # No admin emails → role=="user".
    fake_ensure = MagicMock(return_value="portfolio-user-deadbeef0000")
    session = run_turn.create_session(
        "sess-1", "alice@example.com", ensure_container=fake_ensure,
    )
    assert fake_ensure.call_count == 1
    # Called with normalized email.
    assert fake_ensure.call_args.args[0] == "alice@example.com"
    assert session["role"] == "user"
    assert session["container"] == "portfolio-user-deadbeef0000"

    # And what was saved on disk matches.
    on_disk = sessions.load_session("sess-1")
    assert on_disk["role"] == "user"
    assert on_disk["container"] == "portfolio-user-deadbeef0000"


def test_create_session_admin_no_container(session_env, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    fake_ensure = MagicMock()
    session = run_turn.create_session(
        "sess-2", "boss@example.com", ensure_container=fake_ensure,
    )
    fake_ensure.assert_not_called()
    assert session["role"] == "admin"
    assert "container" not in session

    on_disk = sessions.load_session("sess-2")
    assert on_disk["role"] == "admin"
    assert "container" not in on_disk


def test_create_session_normalizes_email(session_env):
    fake_ensure = MagicMock(return_value="portfolio-user-aaaaaaaaaaaa")
    session = run_turn.create_session(
        "sess-3", "  Alice@Example.COM  ", ensure_container=fake_ensure,
    )
    assert session["email"] == "alice@example.com"
    on_disk = sessions.load_session("sess-3")
    assert on_disk["email"] == "alice@example.com"


def test_create_session_persists_to_disk(session_env):
    fake_ensure = MagicMock(return_value="portfolio-user-bbbbbbbbbbbb")
    session = run_turn.create_session(
        "sess-4", "alice@example.com", ensure_container=fake_ensure,
    )
    round_trip = sessions.load_session("sess-4")
    assert round_trip == session


# Group D: run_turn.run_turn dispatch

def _write_session(tmp_path, session_id, data):
    """Write session JSON directly so we can assert run_turn re-reads it."""
    path = os.path.join(str(tmp_path), f"{session_id}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def test_run_turn_user_routes_through_container(session_env):
    _write_session(session_env, "sess-u", {
        "session_id": "sess-u",
        "email": "alice@example.com",
        "role": "user",
        "container": "portfolio-user-cafebabe1234",
        "created_at": "2025-01-01T00:00:00.000000Z",
    })
    fake_runner = MagicMock(return_value="ok")
    run_turn.run_turn("sess-u", ["claude", "-p", "hi"], runner=fake_runner)
    assert fake_runner.call_count == 1
    argv = fake_runner.call_args.args[0]
    assert argv == [
        "docker", "exec", "-i", "-w", "/workspace", "-e", "HOME=/workspace",
        "portfolio-user-cafebabe1234",
        "claude", "-p", "hi",
    ]


def test_run_turn_admin_uses_host_path(session_env):
    _write_session(session_env, "sess-a", {
        "session_id": "sess-a",
        "email": "boss@example.com",
        "role": "admin",
        "created_at": "2025-01-01T00:00:00.000000Z",
    })
    fake_runner = MagicMock(return_value="ok")
    claude_argv = ["claude", "-p", "hi"]
    run_turn.run_turn("sess-a", claude_argv, runner=fake_runner)
    assert fake_runner.call_count == 1
    argv = fake_runner.call_args.args[0]
    assert argv == claude_argv


def test_run_turn_reentry_no_reprovision(session_env, monkeypatch):
    _write_session(session_env, "sess-re", {
        "session_id": "sess-re",
        "email": "alice@example.com",
        "role": "user",
        "container": "portfolio-user-cafebabe1234",
        "created_at": "2025-01-01T00:00:00.000000Z",
    })
    # Sentinel: any call to ensure_user_container during run_turn is a bug.
    fake_ensure = MagicMock(side_effect=AssertionError("ensure_user_container must not be called by run_turn"))
    monkeypatch.setattr(user_container, "ensure_user_container", fake_ensure)

    fake_runner = MagicMock(return_value="ok")
    run_turn.run_turn("sess-re", ["claude", "-p", "hi"], runner=fake_runner)
    run_turn.run_turn("sess-re", ["claude", "-p", "again"], runner=fake_runner)

    fake_ensure.assert_not_called()
    assert fake_runner.call_count == 2
    # Both calls used the same container name read from disk.
    argv1 = fake_runner.call_args_list[0].args[0]
    argv2 = fake_runner.call_args_list[1].args[0]
    assert "portfolio-user-cafebabe1234" in argv1
    assert "portfolio-user-cafebabe1234" in argv2


def test_run_turn_missing_session_raises(session_env):
    # Documented behavior: missing session JSON surfaces as FileNotFoundError
    # (sessions.load_session opens the file directly).
    with pytest.raises(FileNotFoundError):
        run_turn.run_turn("does-not-exist", ["claude"], runner=MagicMock())


# Group E: title-call parity

def test_title_call_user_uses_container(session_env):
    _write_session(session_env, "sess-tu", {
        "session_id": "sess-tu",
        "email": "alice@example.com",
        "role": "user",
        "container": "portfolio-user-cafebabe1234",
        "created_at": "2025-01-01T00:00:00.000000Z",
    })
    fake_runner = MagicMock(return_value="ok")
    run_turn.run_title_call("sess-tu", ["claude", "-p", "title"], runner=fake_runner)
    argv = fake_runner.call_args.args[0]
    assert argv[: len(DOCKER_EXEC_PREFIX)] == DOCKER_EXEC_PREFIX
    assert "portfolio-user-cafebabe1234" in argv


def test_title_call_admin_uses_host(session_env):
    _write_session(session_env, "sess-ta", {
        "session_id": "sess-ta",
        "email": "boss@example.com",
        "role": "admin",
        "created_at": "2025-01-01T00:00:00.000000Z",
    })
    fake_runner = MagicMock(return_value="ok")
    claude_argv = ["claude", "-p", "title"]
    run_turn.run_title_call("sess-ta", claude_argv, runner=fake_runner)
    argv = fake_runner.call_args.args[0]
    assert argv == claude_argv


# ---------------------------------------------------------------------------
# Group E: claude_runner._resolve_home_for_account — host-dispatch sessions
# resolve their stored account as a preference, not a pin (the same
# semantics container dispatch gets via refresh_credentials_if_stale).
# ---------------------------------------------------------------------------

class _FakeHomeRouter:
    """Minimal account_router stand-in for home_for_account/MAIN_HOME."""

    def __init__(self, homes: dict, main_home: str):
        self._homes = homes
        self.MAIN_HOME = Path(main_home)

    def home_for_account(self, name):
        return Path(self._homes.get(name, str(self.MAIN_HOME)))


def _patch_home_router(monkeypatch, router):
    monkeypatch.setitem(sys.modules, "account_router", router)


def test_home_resolution_keeps_usable_stored_account(monkeypatch):
    _patch_home_router(monkeypatch, _FakeHomeRouter(
        {"account-3": "/opt/accts/account-3"}, "/home/felix"))
    monkeypatch.setattr(
        claude_runner.user_container, "resolve_usable_account", lambda n: n)
    assert claude_runner._resolve_home_for_account("account-3") == \
        "/opt/accts/account-3"


def test_home_resolution_routes_off_saturated_stored_account(monkeypatch):
    _patch_home_router(monkeypatch, _FakeHomeRouter(
        {"account-2": "/opt/accts/account-2",
         "account-3": "/opt/accts/account-3"}, "/home/felix"))
    monkeypatch.setattr(
        claude_runner.user_container, "resolve_usable_account",
        lambda n: "account-2")
    assert claude_runner._resolve_home_for_account("account-3") == \
        "/opt/accts/account-2"


def test_home_resolution_to_main_inherits(monkeypatch):
    _patch_home_router(monkeypatch, _FakeHomeRouter({}, "/home/felix"))
    monkeypatch.setattr(
        claude_runner.user_container, "resolve_usable_account",
        lambda n: "main")
    assert claude_runner._resolve_home_for_account("account-3") is None


def test_home_resolution_fails_static_on_error(monkeypatch):
    _patch_home_router(monkeypatch, _FakeHomeRouter(
        {"account-3": "/opt/accts/account-3"}, "/home/felix"))
    def boom(name):
        raise OSError("resolution exploded")
    monkeypatch.setattr(
        claude_runner.user_container, "resolve_usable_account", boom)
    assert claude_runner._resolve_home_for_account("account-3") == \
        "/opt/accts/account-3"


def test_home_resolution_none_skips_resolution(monkeypatch):
    called = []
    monkeypatch.setattr(
        claude_runner.user_container, "resolve_usable_account",
        lambda n: called.append(n))
    assert claude_runner._resolve_home_for_account(None) is None
    assert called == []
