"""POST /messages must answer while another turn holds the session.

The bug this covers: ``post_message`` acquired the per-session lock with a
bare ``await lock.acquire()``. The worker holds that lock for the WHOLE
lifetime of its run and pops ``_active_runs`` *before* releasing it, so the
in-flight check below it could never fire while a turn was actually running
— the request just blocked instead.

That was invisible in tests (fake turns finish instantly) and pathological
in production: agent turns run for minutes, the SSE response headers cannot
be written until the lock is won, and the edge times the request out at 100s.
The browser saw a transport failure for a message the server had not yet
read, and the client's recovery poll — which assumes a worker exists — found
somebody else's finished run and quietly gave up. Net effect: a message sent
while the assistant was mid-reply disappeared from the thread.

The contract (documented in the Phase 4 block in app.py) is 409. These tests
pin that it is answered *promptly*, under both shapes of "session is busy".
"""

from __future__ import annotations

import concurrent.futures
import time

import pytest

from helpers import create_session


USER = "alice@example.com"


def _post(client, sid, headers, text="second message"):
    return client.post(
        f"/api/sessions/{sid}/messages", headers=headers, json={"text": text},
    )


def test_busy_session_409s_instead_of_blocking(client, auth_headers, fake_claude):
    """A worker holding the session lock must not stall the next POST."""
    import app as app_module

    headers = auth_headers(USER)
    sid = create_session(client, headers)

    # Shorten the wait so the test asserts the bound, not the production one.
    app_module.SESSION_LOCK_WAIT_SEC = 0.5

    # Hold the lock exactly the way a live worker does: acquired inside the
    # app's own event loop, held across the request we're about to make.
    # Note _active_runs is deliberately left EMPTY — that is the regression.
    # The in-flight check can't save us here; only the bounded wait can.
    lock = app_module.storage.get_session_lock(USER, sid)
    portal = client.portal
    portal.call(lock.acquire)
    # Off-thread with a join deadline: on the unfixed code this POST blocks
    # until the lock is released, which here is never. Without the deadline
    # the regression hangs the suite instead of failing it.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        started = time.monotonic()
        fut = pool.submit(_post, client, sid, headers)
        try:
            resp = fut.result(timeout=10)
        except concurrent.futures.TimeoutError:
            pytest.fail(
                "POST /messages blocked on the session lock instead of "
                "answering 409 — see this module's docstring",
            )
        elapsed = time.monotonic() - started
    finally:
        portal.call(_release, lock)
        pool.shutdown(wait=False)

    assert resp.status_code == 409, resp.text
    assert "already in flight" in resp.text
    # Generous ceiling; the point is "bounded", not "fast".
    assert elapsed < 5, f"POST took {elapsed:.1f}s on a busy session"


async def _release(lock) -> None:
    lock.release()


def test_active_run_still_409s(client, auth_headers, fake_claude):
    """The pre-existing in-flight check keeps working (lock free, run live)."""
    import app as app_module

    headers = auth_headers(USER)
    sid = create_session(client, headers)

    key = (USER, sid)
    app_module._active_runs[key] = object()
    try:
        resp = _post(client, sid, headers)
    finally:
        app_module._active_runs.pop(key, None)

    assert resp.status_code == 409, resp.text
    assert "already in flight" in resp.text


def test_409_does_not_persist_the_message(client, auth_headers, fake_claude):
    """A rejected send must leave no trace, so the client owns the retry.

    If the server persisted the user message on a 409 the client's
    cancel-and-retry would duplicate it.
    """
    import app as app_module

    headers = auth_headers(USER)
    sid = create_session(client, headers)

    before = client.get(f"/api/sessions/{sid}", headers=headers).json()["messages"]

    key = (USER, sid)
    app_module._active_runs[key] = object()
    try:
        assert _post(client, sid, headers, "dropped").status_code == 409
    finally:
        app_module._active_runs.pop(key, None)

    after = client.get(f"/api/sessions/{sid}", headers=headers).json()["messages"]
    assert after == before
    assert all(m["content"] != "dropped" for m in after)


def test_session_stays_usable_after_a_409(client, auth_headers, fake_claude):
    """The bounded wait must not leak the lock on timeout.

    ``asyncio.wait_for`` cancels the pending ``acquire()``; if that
    cancellation landed after the lock had been handed over, the session
    would be wedged for good. Send a real turn afterwards to prove it isn't.
    """
    import app as app_module

    fake_claude.set_scenario("happy")
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    app_module.SESSION_LOCK_WAIT_SEC = 0.3

    lock = app_module.storage.get_session_lock(USER, sid)
    portal = client.portal
    portal.call(lock.acquire)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = pool.submit(_post, client, sid, headers)
        try:
            assert fut.result(timeout=10).status_code == 409
        except concurrent.futures.TimeoutError:
            pytest.fail("POST /messages blocked on the session lock")
    finally:
        portal.call(_release, lock)
        pool.shutdown(wait=False)

    resp = _post(client, sid, headers, "now it should work")
    assert resp.status_code == 200, resp.text
    resp.read()
    msgs = client.get(f"/api/sessions/{sid}", headers=headers).json()["messages"]
    assert any(m["content"] == "now it should work" for m in msgs)
