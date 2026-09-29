"""Regression tests for the 2026-08-20 silent-wake-loss bug.

Two independent defects combined to drop two scheduled wakes with no trace
beyond an empty error message in the thread:

  1. ``_resolve_home_for_account`` let the account router move a ``--resume``
     turn onto another account's HOME. On host dispatch HOME *is* the
     transcript root, so claude died with "No conversation found with session
     ID". (Group A)

  2. ``run_turn`` folds that failure into an error EVENT rather than raising,
     so ``_fire_schedule``'s ``except`` never ran — and ``claim_due`` had
     already dropped the one-shot. Nothing retried it. (Groups B and C)

Run inside the chat image (python3.12 + deps).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

import app
import chat_scheduler
import claude_runner


SID = "d41f7b9a-8e0f-46f4-8f51-9f0558291eb8"


# ---------------------------------------------------------------------------
# Group A: transcript-aware account re-homing (claude_runner)
# ---------------------------------------------------------------------------

class _FakeHomeRouter:
    def __init__(self, homes: dict, main_home: str):
        self._homes = homes
        self.MAIN_HOME = Path(main_home)

    def home_for_account(self, name):
        return Path(self._homes.get(name, str(self.MAIN_HOME)))


def _mk_transcript(home: Path, sid: str = SID) -> None:
    """Lay down a transcript exactly where the claude CLI would."""
    d = home / ".claude" / "projects" / "-home-felix"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.jsonl").write_text('{"type":"user"}\n')


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """account-3 (preferred, has the transcript) and account-2 (router pick)."""
    a3, a2, main = tmp_path / "a3", tmp_path / "a2", tmp_path / "main"
    for p in (a3, a2, main):
        (p / ".claude" / "projects").mkdir(parents=True)
    router = _FakeHomeRouter(
        {"account-3": str(a3), "account-2": str(a2)}, str(main))
    monkeypatch.setitem(sys.modules, "account_router", router)
    monkeypatch.setattr(
        claude_runner.user_container, "resolve_usable_account",
        lambda n: "account-2")
    return {"a3": a3, "a2": a2, "main": main}


def test_resume_is_not_rehomed_away_from_its_transcript(homes):
    """THE BUG: resuming a session whose transcript lives only under the
    preferred home must stay on that account even when the router says the
    account is saturated."""
    _mk_transcript(homes["a3"])
    assert claude_runner._resolve_home_for_account(
        "account-3", claude_session_id=SID, is_first_turn=False,
    ) == str(homes["a3"])


def test_first_turn_still_rehomes(homes):
    """Overage-avoidance is preserved where it is safe: a first turn has no
    transcript to orphan, so the router pick wins."""
    _mk_transcript(homes["a3"])  # some OTHER session's transcript may exist
    assert claude_runner._resolve_home_for_account(
        "account-3", claude_session_id=SID, is_first_turn=True,
    ) == str(homes["a2"])


def test_rehomes_when_preferred_home_has_no_transcript(homes):
    """Nothing to lose: the transcript is already gone, so re-homing costs
    nothing and the stale-resume error path handles the rest."""
    assert claude_runner._resolve_home_for_account(
        "account-3", claude_session_id=SID, is_first_turn=False,
    ) == str(homes["a2"])


def test_rehomes_when_both_homes_can_serve_the_resume(homes):
    """Shared/bind-mounted transcript roots: the swap is harmless."""
    _mk_transcript(homes["a3"])
    _mk_transcript(homes["a2"])
    assert claude_runner._resolve_home_for_account(
        "account-3", claude_session_id=SID, is_first_turn=False,
    ) == str(homes["a2"])


def test_legacy_callers_keep_unconditional_rehoming(homes):
    """Callers that pass neither kwarg get the pre-fix behavior."""
    _mk_transcript(homes["a3"])
    assert claude_runner._resolve_home_for_account("account-3") == \
        str(homes["a2"])


def test_transcript_exists_ignores_other_sessions(homes):
    _mk_transcript(homes["a3"], sid="11111111-2222-3333-4444-555555555555")
    assert claude_runner._transcript_exists(homes["a3"], SID) is False
    assert claude_runner._transcript_exists(
        homes["a3"], "11111111-2222-3333-4444-555555555555") is True


def test_transcript_exists_survives_missing_dirs(tmp_path):
    assert claude_runner._transcript_exists(tmp_path / "nope", SID) is False
    assert claude_runner._transcript_exists(None, SID) is False


# ---------------------------------------------------------------------------
# Group B: _consume_into_run reports its terminal status
# ---------------------------------------------------------------------------

async def _agen(events):
    for ev in events:
        yield ev


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def quiet_storage(monkeypatch):
    """Stub the persistence + post-turn side effects _consume_into_run fires."""
    written: list[tuple] = []
    monkeypatch.setattr(
        app.storage, "update_assistant_message",
        lambda e, s, seq, content, status, **kw: written.append((status, content)))

    async def _noop_img(session_id):
        return None

    async def _noop_sched(session_id, email, errors=None):
        return 0

    monkeypatch.setattr(app, "_process_image_requests", _noop_img)
    monkeypatch.setattr(app, "_process_schedule_requests", _noop_sched)
    monkeypatch.setattr(app, "_scan_new_artifacts", lambda **kw: [])
    monkeypatch.setattr(app, "_format_artifacts_markdown", lambda a: "")
    return written


def _consume(events, run=None):
    run = run or app._TurnRun(email="f@x", session_id="s1", assistant_seq=1)
    return _run(app._consume_into_run(
        run, _agen(events), [],
        container=None, artifacts_dispatch="host", turn_start_ts=0.0,
    ))


def test_consume_returns_error_on_error_event(quiet_storage):
    assert _consume([{"type": "error", "message": "No conversation found"}]) \
        == "error"


def test_consume_returns_complete_on_done_event(quiet_storage):
    assert _consume([{"type": "delta", "text": "hi"},
                     {"type": "done", "full_text": "hi"}]) == "complete"


def test_consume_returns_error_when_stream_ends_without_terminal(quiet_storage):
    assert _consume([{"type": "delta", "text": "partial"}]) == "error"


def test_consume_returns_cancelled(quiet_storage):
    run = app._TurnRun(email="f@x", session_id="s1", assistant_seq=1)
    run.cancel_requested = True
    assert _consume([{"type": "delta", "text": "x"}], run=run) == "cancelled"


# ---------------------------------------------------------------------------
# Group C: a failed wake is re-armed, with a bound
# ---------------------------------------------------------------------------

@pytest.fixture
def captured_requeue(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(
        app.chat_scheduler, "requeue",
        lambda email, session_id, prompt, *, delay_seconds, note=None,
        attempt=0: calls.append({
            "email": email, "session_id": session_id, "prompt": prompt,
            "delay": delay_seconds, "note": note, "attempt": attempt}))
    return calls


def _rec(**over):
    base = {"id": "abc123", "email": "f@x", "session_id": "s1",
            "prompt": "check the rebuild", "note": "watch",
            "interval_seconds": None}
    base.update(over)
    return base


def test_failed_oneshot_wake_is_rearmed(captured_requeue):
    app._requeue_failed_wake(_rec(), "turn ended in error")
    assert len(captured_requeue) == 1
    c = captured_requeue[0]
    assert c["delay"] == app._WAKE_RETRY_BACKOFF_SECONDS[0]
    assert c["attempt"] == 1
    assert c["prompt"] == "check the rebuild"
    assert c["note"] == "watch"


def test_retry_backoff_widens_with_attempt(captured_requeue):
    app._requeue_failed_wake(_rec(attempt=2), "boom")
    assert captured_requeue[0]["delay"] == app._WAKE_RETRY_BACKOFF_SECONDS[2]
    assert captured_requeue[0]["attempt"] == 3


def test_retries_are_bounded(captured_requeue, caplog):
    """Must GIVE UP deliberately and say so — not fall off the end of the
    backoff table and get its IndexError swallowed by the outer handler."""
    with caplog.at_level("ERROR"):
        app._requeue_failed_wake(
            _rec(attempt=len(app._WAKE_RETRY_BACKOFF_SECONDS)), "boom")
    assert captured_requeue == []
    assert "GIVING UP" in caplog.text
    assert "Traceback" not in caplog.text


def test_recurring_wake_is_not_rearmed(captured_requeue):
    """claim_due already re-armed it; requeueing would double-fire."""
    app._requeue_failed_wake(_rec(interval_seconds=3600.0), "boom")
    assert captured_requeue == []


def test_rearm_tolerates_garbage_attempt(captured_requeue):
    app._requeue_failed_wake(_rec(attempt="not-a-number"), "boom")
    assert captured_requeue[0]["attempt"] == 1


def test_rearm_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("store on fire")
    monkeypatch.setattr(app.chat_scheduler, "requeue", boom)
    app._requeue_failed_wake(_rec(), "boom")  # must not propagate


# ---------------------------------------------------------------------------
# Group D: the attempt counter survives the durable store
# ---------------------------------------------------------------------------

def test_register_persists_attempt(tmp_sessions_dir):
    rec = chat_scheduler.register(
        "f@x", "s1", "do the thing", delay_seconds=60, attempt=2)
    assert rec["attempt"] == 2
    stored = [s for s in chat_scheduler.list_for("f@x", "s1")]
    assert len(stored) == 1


def test_requeue_forwards_attempt(tmp_sessions_dir):
    chat_scheduler.requeue("f@x", "s1", "retry me", delay_seconds=60, attempt=3)
    raw = chat_scheduler._load()["schedules"]
    assert [r["attempt"] for r in raw] == [3]


def test_claim_due_returns_attempt(tmp_sessions_dir):
    chat_scheduler.register("f@x", "s1", "p", delay_seconds=1, attempt=1)
    due = chat_scheduler.claim_due(now=chat_scheduler.time.time() + 3600)
    assert len(due) == 1 and due[0]["attempt"] == 1


# ---------------------------------------------------------------------------
# Group E: claimed one-shots are LEASED, not deleted, so a restart mid-fire
# can't lose them
# ---------------------------------------------------------------------------

def test_claim_leases_oneshot_instead_of_deleting(tmp_sessions_dir):
    """THE BUG: the record used to vanish here, so between claim and reply the
    wake existed only in the firing task's memory."""
    chat_scheduler.register("f@x", "s1", "p", delay_seconds=1)
    t = chat_scheduler.time.time() + 3600
    due = chat_scheduler.claim_due(now=t)
    assert len(due) == 1
    still = chat_scheduler._load()["schedules"]
    assert len(still) == 1, "one-shot must survive the claim as a lease"
    assert still[0]["next_fire"] == pytest.approx(t + chat_scheduler.LEASE_SECONDS)
    assert still[0]["leased_at"] == pytest.approx(t)


def test_complete_drops_the_leased_record(tmp_sessions_dir):
    rec = chat_scheduler.register("f@x", "s1", "p", delay_seconds=1)
    chat_scheduler.claim_due(now=chat_scheduler.time.time() + 3600)
    assert chat_scheduler.complete(rec["id"]) is True
    assert chat_scheduler._load()["schedules"] == []
    assert chat_scheduler.complete(rec["id"]) is False


def test_lapsed_lease_refires(tmp_sessions_dir):
    """A fire that never completed (process killed) must come back."""
    chat_scheduler.register("f@x", "s1", "p", delay_seconds=1)
    t = chat_scheduler.time.time() + 3600
    assert len(chat_scheduler.claim_due(now=t)) == 1
    assert chat_scheduler.claim_due(now=t + 60) == [], "still leased"
    again = chat_scheduler.claim_due(now=t + chat_scheduler.LEASE_SECONDS + 1)
    assert len(again) == 1, "lapsed lease must re-fire"


def test_complete_never_touches_recurring(tmp_sessions_dir):
    rec = chat_scheduler.register("f@x", "s1", "p", every_seconds=3600)
    assert chat_scheduler.complete(rec["id"]) is False
    assert len(chat_scheduler._load()["schedules"]) == 1


def test_recurring_still_rearms_on_interval(tmp_sessions_dir):
    """Regression guard: the lease branch must not have changed recurring."""
    chat_scheduler.register("f@x", "s1", "p", every_seconds=3600)
    t = chat_scheduler.time.time() + 7200
    assert len(chat_scheduler.claim_due(now=t)) == 1
    rows = chat_scheduler._load()["schedules"]
    assert len(rows) == 1
    assert rows[0]["next_fire"] > t
    assert rows[0]["next_fire"] - t <= 3600
    assert "leased_at" not in rows[0]


def test_fire_schedule_releases_the_lease(monkeypatch):
    """The wrapper must complete() whatever the inner fire did."""
    completed: list = []
    monkeypatch.setattr(app.chat_scheduler, "complete", completed.append)

    async def _ok(rec):
        return None

    monkeypatch.setattr(app, "_fire_schedule_locked", _ok)
    _run(app._fire_schedule({"id": "lease1"}))
    assert completed == ["lease1"]


def test_fire_schedule_releases_the_lease_even_when_the_fire_raises(monkeypatch):
    completed: list = []
    monkeypatch.setattr(app.chat_scheduler, "complete", completed.append)

    async def _boom(rec):
        raise RuntimeError("fire exploded")

    monkeypatch.setattr(app, "_fire_schedule_locked", _boom)
    with pytest.raises(RuntimeError):
        _run(app._fire_schedule({"id": "lease2"}))
    assert completed == ["lease2"]


def test_pre_fix_records_without_attempt_default_to_zero(captured_requeue):
    """Records already sitting in the live store have no ``attempt`` key."""
    legacy = {"id": "old", "email": "f@x", "session_id": "s1",
              "prompt": "p", "note": None, "interval_seconds": None}
    app._requeue_failed_wake(legacy, "boom")
    assert captured_requeue[0]["attempt"] == 1
