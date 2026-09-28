"""``/api/me`` publishes the message-text cap, and the cap is enforced.

Why the SPA needs the number: the composer converts an oversized paste into
a ``.txt`` attachment *before* sending (frontend/src/limits.ts). It can only
pick that threshold correctly if it knows the server's real ceiling, which is
``CHAT_MAX_MESSAGE_BYTES`` and therefore per-deployment. Hardcoding it in the
bundle meant any deployment that lowered the env var went back to silently
413ing long messages.
"""

from __future__ import annotations

import pytest


USER = "alice@example.com"


def test_me_publishes_max_message_bytes(client, auth_headers) -> None:
    # Imported inside the test (like tests/test_static.py) so the module is
    # first imported AFTER the ``client`` fixture has wired up env + JWKS.
    import app as app_module

    resp = client.get("/api/me", headers=auth_headers(USER))
    assert resp.status_code == 200
    body = resp.json()
    assert body["max_message_bytes"] == app_module.MAX_MESSAGE_TEXT_BYTES
    assert body["max_message_bytes"] > 0


def test_me_tracks_a_reconfigured_cap(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint reads the module global per call, so a deployment that
    overrides the cap is reflected without a frontend rebuild."""
    import app as app_module

    monkeypatch.setattr(app_module, "MAX_MESSAGE_TEXT_BYTES", 4096)
    resp = client.get("/api/me", headers=auth_headers(USER))
    assert resp.status_code == 200
    assert resp.json()["max_message_bytes"] == 4096


def test_oversized_message_text_is_rejected_with_413(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure the frontend now avoids: a too-long POST is refused
    outright, so no worker is ever created for it."""
    import app as app_module

    monkeypatch.setattr(app_module, "MAX_MESSAGE_TEXT_BYTES", 1024)
    created = client.post(
        "/api/sessions", headers=auth_headers(USER), json={"title": "t"}
    )
    assert created.status_code == 201
    sid = created.json()["id"]

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=auth_headers(USER),
        json={"text": "x" * 2000},
    )
    assert resp.status_code == 413
    assert "exceeds" in resp.json()["detail"]


def test_message_text_at_the_cap_is_accepted(
    client, auth_headers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: exactly-at-cap must NOT 413, otherwise the frontend's
    budget arithmetic would be off by one message."""
    import app as app_module

    monkeypatch.setattr(app_module, "MAX_MESSAGE_TEXT_BYTES", 1024)
    monkeypatch.setattr(
        app_module.claude_runner, "run_turn", _stub_run_turn, raising=False
    )
    created = client.post(
        "/api/sessions", headers=auth_headers(USER), json={"title": "t"}
    )
    sid = created.json()["id"]

    with client.stream(
        "POST",
        f"/api/sessions/{sid}/messages",
        headers=auth_headers(USER),
        json={"text": "x" * 1024},
    ) as resp:
        assert resp.status_code == 200
        resp.read()


async def _stub_run_turn(*args, **kwargs):
    """Minimal stand-in for the worker so the accept path terminates."""
    if False:
        yield ""
    return
