"""Persistent counter + handle resolution."""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh_state(tmp_path: Path, monkeypatch):
    """Point HANDLES_JSON at a per-test temp file and reload the module."""
    import agent_handles
    state_dir = tmp_path / "state"
    monkeypatch.setattr(agent_handles, "STATE_DIR", state_dir)
    monkeypatch.setattr(agent_handles, "HANDLES_JSON", state_dir / "handles.json")
    yield agent_handles


def test_assign_increments_per_kind(fresh_state):
    h = fresh_state
    assert h.assign("t-aaaaaa", "task") == "task-1"
    assert h.assign("t-bbbbbb", "task") == "task-2"
    assert h.assign("proj-cccccccc", "proj") == "proj-1"
    assert h.assign("t-dddddd", "task") == "task-3"
    assert h.assign("proj-eeeeeeee", "proj") == "proj-2"


def test_assign_is_idempotent_for_same_uuid(fresh_state):
    h = fresh_state
    first = h.assign("t-aaaaaa", "task")
    again = h.assign("t-aaaaaa", "task")
    assert first == again == "task-1"
    # Counter must not have moved.
    assert h.assign("t-bbbbbb", "task") == "task-2"


def test_assign_persists_across_reload(fresh_state, monkeypatch):
    h = fresh_state
    h.assign("t-aaaaaa", "task")
    h.assign("t-bbbbbb", "task")
    h.assign("proj-cccccccc", "proj")
    # Simulate process restart: reload the module, but keep HANDLES_JSON
    # pointed at the same temp file.
    state_path = h.HANDLES_JSON
    state_dir = h.STATE_DIR
    importlib.reload(h)
    monkeypatch.setattr(h, "STATE_DIR", state_dir)
    monkeypatch.setattr(h, "HANDLES_JSON", state_path)
    # Counter survived; next task is task-3 not task-1.
    assert h.assign("t-zzzzzz", "task") == "task-3"
    # Existing handles still resolve.
    assert h.resolve("task-1") == ("t-aaaaaa", "task")


def test_assign_rejects_unknown_kind(fresh_state):
    with pytest.raises(ValueError, match="unknown kind"):
        fresh_state.assign("t-x", "agent")  # type: ignore[arg-type]


def test_resolve_handle_to_uuid(fresh_state):
    h = fresh_state
    h.assign("t-aaaaaa", "task")
    h.assign("proj-cccccccc", "proj")
    assert h.resolve("task-1") == ("t-aaaaaa", "task")
    assert h.resolve("proj-1") == ("proj-cccccccc", "proj")


def test_resolve_uuid_passthrough(fresh_state):
    """UUID inputs pass through (with kind classified by prefix), even
    if no handle has been assigned yet — !agent status t-XXX must work
    from the raw uuid before any handle exists."""
    h = fresh_state
    assert h.resolve("t-aaaaaa") == ("t-aaaaaa", "task")
    assert h.resolve("proj-cccccccc") == ("proj-cccccccc", "proj")


def test_resolve_returns_none_for_unrecognised(fresh_state):
    h = fresh_state
    assert h.resolve("garbage") is None
    assert h.resolve("") is None
    assert h.resolve("task-9999") is None  # well-formed handle, never assigned


def test_handle_for_returns_existing_or_none(fresh_state):
    h = fresh_state
    assert h.handle_for("t-aaaaaa", "task") is None
    h.assign("t-aaaaaa", "task")
    assert h.handle_for("t-aaaaaa", "task") == "task-1"


def test_is_handle_distinguishes_handles_from_uuids(fresh_state):
    h = fresh_state
    assert h.is_handle("task-1") is True
    assert h.is_handle("proj-12") is True
    assert h.is_handle("t-aaaaaa") is False
    assert h.is_handle("proj-833d6d3e") is False  # hex, not a counter
    assert h.is_handle("") is False
