"""Tests for scheduler.py."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any, List, Tuple

import pytest

from scheduler import (
    ATTACHMENT_REJECT_MSG,
    Scheduler,
    VALID_KINDS,
    _kind_error,
)
from state_store import StateStore

from _helpers import FakeClock


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clock():
    return FakeClock(datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def store(tmp_path, clock):
    return StateStore(str(tmp_path / "s.json"), clock=clock)


class SpawnRecorder:
    def __init__(self, fail: bool = False):
        self.calls: List[Tuple[str, str]] = []
        self.fail = fail

    async def __call__(self, kind: str, spec: str) -> str:
        self.calls.append((kind, spec))
        if self.fail:
            raise RuntimeError("simulated spawn failure")
        return f"spawned-{len(self.calls)}"


def _make_scheduler(store, clock, spawn=None):
    return Scheduler(store, spawn or SpawnRecorder(), clock=clock)


# ---------------------------------------------------------- register

def test_register_mints_sequential_handles(store, clock):
    sched = _make_scheduler(store, clock)
    h1 = run(sched.register("*/5 * * * *", "task", "do thing", "u1"))
    h2 = run(sched.register("0 9 * * 1", "task", "weekly", "u1"))
    h3 = run(sched.register("0 0 * * *", "project", "nightly", "u1"))
    assert "sched-1" in h1
    assert "sched-2" in h2
    assert "sched-3" in h3


def test_register_invalid_cron_returns_error(store, clock):
    sched = _make_scheduler(store, clock)
    msg = run(sched.register("not a cron expr", "task", "x", "u1"))
    assert msg.startswith("invalid cron expression")


def test_register_unknown_kind_returns_exact_error(store, clock):
    sched = _make_scheduler(store, clock)
    msg = run(sched.register("* * * * *", "robot", "x", "u1"))
    assert msg == "unknown agent kind 'robot'. Valid: task, project."


def test_register_response_includes_handle_cron_kind_nextfire(store, clock):
    sched = _make_scheduler(store, clock)
    msg = run(sched.register("*/5 * * * *", "task", "do thing", "u1"))
    assert "sched-1" in msg
    assert "cron=*/5 * * * *" in msg
    assert "kind=task" in msg
    assert "next-fire=" in msg


# ---------------------------------------------------------- counter persistence

def test_schedule_counter_persists_across_restart(tmp_path, clock):
    store1 = StateStore(str(tmp_path / "s.json"), clock=clock)
    sched1 = _make_scheduler(store1, clock)
    run(sched1.register("* * * * *", "task", "a", "u1"))
    run(sched1.register("* * * * *", "task", "b", "u1"))
    run(sched1.register("* * * * *", "task", "c", "u1"))
    # delete sched-2
    assert sched1.unregister("sched-2") is True

    # restart: new state_store + new Scheduler
    store2 = StateStore(str(tmp_path / "s.json"), clock=clock)
    sched2 = _make_scheduler(store2, clock)
    sched2.load_from_state()
    msg = run(sched2.register("* * * * *", "task", "d", "u1"))
    # next handle is sched-4, NOT sched-3 (counter is monotonic)
    assert "sched-4" in msg
    handles = sorted(r.handle for r in sched2.list_all())
    assert handles == ["sched-1", "sched-3", "sched-4"]


# ---------------------------------------------------------- tick / firing

def test_tick_at_fire_time_invokes_spawn(store, clock):
    spawn = SpawnRecorder()
    sched = _make_scheduler(store, clock, spawn=spawn)
    run(sched.register("*/5 * * * *", "task", "spec-A", "u1"))
    # Advance to 12:05 — the first */5 fire after 12:00
    fire_time = datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)
    fired = run(sched.tick(fire_time))
    assert fired == ["sched-1"]
    assert spawn.calls == [("task", "spec-A")]


def test_tick_at_non_fire_time_does_nothing(store, clock):
    spawn = SpawnRecorder()
    sched = _make_scheduler(store, clock, spawn=spawn)
    run(sched.register("0 9 * * 1", "task", "weekly", "u1"))
    # Tuesday at 12:00 is not a fire time
    fired = run(sched.tick(clock()))
    assert fired == []
    assert spawn.calls == []


def test_every_5_minutes_fires_correct_count(store, clock):
    spawn = SpawnRecorder()
    sched = _make_scheduler(store, clock, spawn=spawn)
    run(sched.register("*/5 * * * *", "task", "X", "u1"))
    # base time = 12:00; advance to 12:05 -> exactly 1 fire
    run(sched.tick(datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)))
    assert len(spawn.calls) == 1
    # then advance to 12:11 from t0 -> total 2 fires (12:05 + 12:10)
    run(sched.tick(datetime(2025, 1, 1, 12, 11, 0, tzinfo=timezone.utc)))
    assert len(spawn.calls) == 2


def test_spawn_failure_does_not_remove_schedule(store, clock):
    spawn = SpawnRecorder(fail=True)
    sched = _make_scheduler(store, clock, spawn=spawn)
    run(sched.register("*/5 * * * *", "task", "X", "u1"))
    # First fire raises
    run(sched.tick(datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)))
    # schedule still present and re-armed
    recs = sched.list_all()
    assert len(recs) == 1
    assert recs[0].handle == "sched-1"
    assert recs[0].next_fire_time > datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------- restart persistence

def test_schedules_survive_restart(tmp_path, clock):
    store1 = StateStore(str(tmp_path / "s.json"), clock=clock)
    sched1 = _make_scheduler(store1, clock)
    run(sched1.register("*/5 * * * *", "task", "spec-A", "u1"))
    run(sched1.register("0 9 * * 1", "project", "weekly review", "u2"))

    store2 = StateStore(str(tmp_path / "s.json"), clock=clock)
    sched2 = _make_scheduler(store2, clock)
    sched2.load_from_state()
    recs = {r.handle: r for r in sched2.list_all()}
    assert "sched-1" in recs and "sched-2" in recs
    assert recs["sched-1"].cron == "*/5 * * * *"
    assert recs["sched-2"].agent_kind == "project"
    assert recs["sched-2"].spec == "weekly review"
    # next_fire recomputed
    assert recs["sched-1"].next_fire_time is not None
    assert recs["sched-2"].next_fire_time is not None


def test_corrupted_cron_dropped_on_load_others_survive(tmp_path, clock):
    # Hand-craft a state file with one good and one bad schedule.
    state = {
        "schedule_counter": 2,
        "schedules": [
            {"handle": "sched-1", "cron": "*/5 * * * *",
             "agent_kind": "task", "spec": "A", "owner_user_id": "u1",
             "created_at": clock().isoformat(), "last_fired_at": None},
            {"handle": "sched-2", "cron": "lol not a cron",
             "agent_kind": "task", "spec": "B", "owner_user_id": "u1",
             "created_at": clock().isoformat(), "last_fired_at": None},
        ],
    }
    p = tmp_path / "s.json"
    p.write_text(json.dumps(state))
    store2 = StateStore(str(p), clock=clock)
    sched2 = _make_scheduler(store2, clock)
    sched2.load_from_state()
    handles = [r.handle for r in sched2.list_all()]
    assert "sched-1" in handles
    assert "sched-2" not in handles


# ---------------------------------------------------------- unschedule

def test_unschedule_removes_and_subsequent_tick_no_fire(store, clock):
    spawn = SpawnRecorder()
    sched = _make_scheduler(store, clock, spawn=spawn)
    run(sched.register("*/5 * * * *", "task", "X", "u1"))
    assert sched.unregister("sched-1") is True
    # advance past the would-be fire time
    run(sched.tick(datetime(2025, 1, 1, 12, 5, 0, tzinfo=timezone.utc)))
    assert spawn.calls == []


def test_unschedule_unknown_returns_false(store, clock):
    sched = _make_scheduler(store, clock)
    assert sched.unregister("sched-99") is False


# ---------------------------------------------------------- attachment reject

def test_attachment_reject_message_is_documented():
    """The dispatcher boundary uses this constant; assert its exact text."""
    assert ATTACHMENT_REJECT_MSG == (
        "v1 schedules accept text-only specs; attachments are not serialized."
    )
