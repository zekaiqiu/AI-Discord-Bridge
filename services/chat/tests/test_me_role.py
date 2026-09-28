"""Phase 1: /api/me returns a ``role`` field for the Sidebar to gate on.

Resolution priority covered here:
  1. ``sandbox_users.json`` next to ``app.py`` (when present and email is keyed).
  2. ``FELIX_EMAIL`` env var (felix => admin, everyone else => user).
  3. ``DEFAULT_FELIX_EMAIL`` constant fallback when the env var is unset.

The shape contract is additive: ``email`` is unchanged; ``role`` is new.
Phase 2's frontend Sidebar reads ``me.role`` and conditionally renders
admin-only UI. Later phases added ``settings``, ``shared_workspace_enabled``
and ``max_message_bytes`` (SPA bootstrap), so these tests assert the
identity subset via ``_assert_me`` rather than whole-dict equality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


def _assert_me(body: dict, email: str, role: str) -> None:
    """Assert the identity subset of a ``/api/me`` body."""
    assert body["email"] == email, body
    assert body["role"] == role, body


# ---------------------------------------------------------------------------
# Env-var / constant path: sandbox_users.json is NOT present on disk.
# ---------------------------------------------------------------------------

def test_role_is_admin_for_felix_email_via_env(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When FELIX_EMAIL is set and the authed email matches, role=admin."""
    monkeypatch.setenv("FELIX_EMAIL", "felix@example.org")
    resp = client.get("/api/me", headers=auth_headers("felix@example.org"))
    assert resp.status_code == 200
    body = resp.json()
    _assert_me(body, "felix@example.org", "admin")


def test_role_is_user_for_non_felix_email(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different authed email gets role=user."""
    monkeypatch.setenv("FELIX_EMAIL", "felix@example.org")
    resp = client.get("/api/me", headers=auth_headers("someone@example.com"))
    assert resp.status_code == 200
    body = resp.json()
    _assert_me(body, "someone@example.com", "user")


def test_role_falls_back_to_default_felix_email_constant(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With FELIX_EMAIL unset, the ``DEFAULT_FELIX_EMAIL`` constant is used."""
    import app as chat_app

    monkeypatch.delenv("FELIX_EMAIL", raising=False)
    resp = client.get(
        "/api/me", headers=auth_headers(chat_app.DEFAULT_FELIX_EMAIL)
    )
    assert resp.status_code == 200
    body = resp.json()
    _assert_me(body, chat_app.DEFAULT_FELIX_EMAIL, "admin")

    # Negative sub-case: same default-felix configuration, a different
    # authed email must still resolve to "user". Locks in that the
    # constant fallback hasn't accidentally promoted everyone to admin.
    other = client.get("/api/me", headers=auth_headers("other@example.com"))
    assert other.status_code == 200
    _assert_me(other.json(), "other@example.com", "user")


def test_email_field_is_preserved_unchanged(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-Phase-1 ``email`` field still exists with the same name + type.

    Locks in the additive-only shape contract from the brief.
    """
    monkeypatch.setenv("FELIX_EMAIL", "felix@example.org")
    resp = client.get("/api/me", headers=auth_headers("alice@example.com"))
    assert resp.status_code == 200
    body = resp.json()
    assert "email" in body and isinstance(body["email"], str)
    assert body["email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# sandbox_users.json branch: stub the path so the file appears to exist
# without us actually creating it on the live tree (the brief is explicit
# that we must NOT create sandbox_users.json).
# ---------------------------------------------------------------------------

def test_role_from_sandbox_users_json_overrides_email_match(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When sandbox_users.json is present and keys the email, it wins.

    Even for felix's email, an explicit ``"user"`` mapping demotes them.
    Demonstrates priority 1 beating priority 2.
    """
    import app as chat_app

    sandbox_file = tmp_path / "sandbox_users.json"
    sandbox_file.write_text(
        json.dumps({
            "felix@example.org": "user",
            "ops@example.com": "admin",
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(chat_app, "_SANDBOX_USERS_PATH", sandbox_file)
    monkeypatch.setenv("FELIX_EMAIL", "felix@example.org")

    # Felix demoted to "user" by the sandbox file.
    resp = client.get("/api/me", headers=auth_headers("felix@example.org"))
    assert resp.status_code == 200
    _assert_me(resp.json(), "felix@example.org", "user")

    # Random ops user promoted to "admin" by the sandbox file.
    resp2 = client.get("/api/me", headers=auth_headers("ops@example.com"))
    assert resp2.status_code == 200
    _assert_me(resp2.json(), "ops@example.com", "admin")


def test_role_falls_through_when_email_not_in_sandbox(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If sandbox_users.json exists but doesn't list the email, fall through."""
    import app as chat_app

    sandbox_file = tmp_path / "sandbox_users.json"
    sandbox_file.write_text(
        json.dumps({"someone@else.com": "admin"}), encoding="utf-8"
    )
    monkeypatch.setattr(chat_app, "_SANDBOX_USERS_PATH", sandbox_file)
    monkeypatch.setenv("FELIX_EMAIL", "felix@example.org")

    # Email not keyed → fall through to env-var path → felix=admin.
    resp = client.get("/api/me", headers=auth_headers("felix@example.org"))
    assert resp.status_code == 200
    _assert_me(resp.json(), "felix@example.org", "admin")

    # Email not keyed and not felix → "user".
    resp2 = client.get("/api/me", headers=auth_headers("nobody@example.com"))
    assert resp2.status_code == 200
    _assert_me(resp2.json(), "nobody@example.com", "user")


def test_malformed_sandbox_users_json_does_not_crash(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A bad ops file must NOT 500 the identity endpoint — fall through."""
    import app as chat_app

    sandbox_file = tmp_path / "sandbox_users.json"
    sandbox_file.write_text("this is { not json", encoding="utf-8")
    monkeypatch.setattr(chat_app, "_SANDBOX_USERS_PATH", sandbox_file)
    monkeypatch.setenv("FELIX_EMAIL", "felix@example.org")

    resp = client.get("/api/me", headers=auth_headers("felix@example.org"))
    assert resp.status_code == 200
    # Falls through to env-var path; felix → admin.
    _assert_me(resp.json(), "felix@example.org", "admin")


# ---------------------------------------------------------------------------
# ADMIN_EMAILS env: comma-separated allowlist (replaces FELIX_EMAIL for the
# multi-admin case). The two real admin addresses are admin by default even
# without ADMIN_EMAILS set.
# ---------------------------------------------------------------------------

def test_default_admin_emails_are_admin_without_env(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """victorchiu2003@gmail.com and supzekai@gmail.com are admin by default."""
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("FELIX_EMAIL", raising=False)
    for email in ("victorchiu2003@gmail.com", "supzekai@gmail.com"):
        resp = client.get("/api/me", headers=auth_headers(email))
        assert resp.status_code == 200, email
        _assert_me(resp.json(), email, "admin")


def test_admin_emails_env_overrides_defaults(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Setting ADMIN_EMAILS replaces the default tuple (FELIX_EMAIL still
    folded in for legacy single-address compat)."""
    monkeypatch.setenv("ADMIN_EMAILS", "ops@example.com, admin2@example.com")
    monkeypatch.setenv("FELIX_EMAIL", "felix@example.com")

    # Listed in env => admin
    for email in ("ops@example.com", "admin2@example.com", "felix@example.com"):
        resp = client.get("/api/me", headers=auth_headers(email))
        assert resp.status_code == 200, email
        assert resp.json()["role"] == "admin", email

    # Default emails are NOT admin once ADMIN_EMAILS is set
    resp = client.get("/api/me", headers=auth_headers("victorchiu2003@gmail.com"))
    assert resp.json()["role"] == "user"


def test_admin_emails_match_is_case_insensitive(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed-case emails resolve the same role as the lower-case form."""
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.delenv("FELIX_EMAIL", raising=False)
    resp = client.get("/api/me", headers=auth_headers("VictorChiu2003@Gmail.com"))
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"
