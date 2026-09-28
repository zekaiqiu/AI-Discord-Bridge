"""Title generation: runs in background, doesn't block the SSE response,
eventually populates session.title."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest


USER_A = "alice@example.com"


def _create_session(client: Any, headers: dict[str, str]) -> str:
    resp = client.post("/api/sessions", headers=headers, json={"title": None})
    assert resp.status_code == 201
    return resp.json()["id"]


def test_title_does_not_block_sse_response_and_eventually_appears(
    client, auth_headers, fake_claude,
):
    """The "slow_title" scenario sleeps 2s inside the title spawn; the main
    turn must still complete in well under 1s. Then the title eventually
    lands on the session within a few seconds.
    """
    fake_claude.set_scenario("slow_title")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)

    t0 = time.monotonic()
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "first message"},
    )
    # Touch .text to fully consume the SSE stream.
    body = resp.text
    elapsed = time.monotonic() - t0

    assert resp.status_code == 200
    # The main response must NOT have waited for the slow title call.
    # Generous bound (1.5s) to absorb startup jitter while still proving
    # we didn't stall on a 2s sleep.
    assert elapsed < 1.5, f"SSE response blocked on title gen: {elapsed:.2f}s"
    assert "done" in body

    # Title should eventually appear. Poll for up to 5s.
    deadline = time.monotonic() + 5.0
    final_title: str | None = None
    while time.monotonic() < deadline:
        sess = client.get(f"/api/sessions/{sid}", headers=headers).json()
        if sess.get("title"):
            final_title = sess["title"]
            break
        time.sleep(0.1)
    assert final_title, "title never populated"
    assert final_title == "Brief Title"


def test_title_not_regenerated_on_second_turn(client, auth_headers, fake_claude):
    """First turn schedules title; second turn must not (no extra spawn)."""
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)

    # First turn -> 2 calls expected (turn + title) once title task drains.
    r1 = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "one"},
    )
    r1.text
    # Allow background title task to finish.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        sess = client.get(f"/api/sessions/{sid}", headers=headers).json()
        if sess.get("title"):
            break
        time.sleep(0.05)
    calls_after_first = len(fake_claude.calls)
    assert calls_after_first >= 2, f"expected turn + title calls, got {calls_after_first}"

    # Second turn: only +1 call (the turn itself, no title regen).
    r2 = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "two"},
    )
    r2.text
    # No title task should be scheduled, so call count stays at +1.
    time.sleep(0.2)
    calls_after_second = len(fake_claude.calls)
    assert calls_after_second == calls_after_first + 1, (
        f"unexpected extra spawn_claude call on second turn: "
        f"{calls_after_first} -> {calls_after_second}"
    )


def test_user_set_title_not_clobbered_by_auto_titler(
    client, auth_headers, fake_claude,
):
    """If the user creates a session WITH a title, the title task must
    not overwrite it. Brief: ``set_title`` no-ops on non-empty existing title.
    """
    fake_claude.set_scenario("happy")
    headers = auth_headers(USER_A)
    resp = client.post(
        "/api/sessions",
        headers=headers,
        json={"title": "user-chosen"},
    )
    sid = resp.json()["id"]

    r = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "hi"},
    )
    r.text
    # Wait long enough for the title task to have run and tried to set.
    time.sleep(0.3)
    sess = client.get(f"/api/sessions/{sid}", headers=headers).json()
    assert sess["title"] == "user-chosen", sess["title"]
