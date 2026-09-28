"""Tests for the bridge ops HTTP API.

Stubs out bot.py + usage_report.py so the suite can run without a live
Discord token, real claude subprocesses, or the Anthropic OAuth flow.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Fixtures: redirect bridge_api state paths at a tmp dir, stub out bot.
# ---------------------------------------------------------------------------

@pytest.fixture
def admin_email() -> str:
    return "victorchiu2003@gmail.com"


@pytest.fixture
def stub_bot(monkeypatch):
    """Inject a fake ``bot`` module so write paths don't try to import the
    real one (which requires DISCORD_BOT_TOKEN at module load).

    The stub exposes the same surface the API touches: core_task_create,
    core_task_stop, agent_handles, task_module, LEVELS, etc.
    """
    fake_bot = types.ModuleType("bot")

    # Minimal LEVELS dict so the verbose endpoint validates input correctly.
    fake_bot.LEVELS = {
        "quiet": {"interval_min": 60},
        "normal": {"interval_min": 30},
        "verbose": {"interval_min": 10},
        "firehose": {"interval_min": 3},
    }
    fake_bot.VERBOSE_OVERRIDES = {}
    fake_bot.ping_interval_sec = lambda level: fake_bot.LEVELS[level]["interval_min"] * 60

    class TaskCapacityError(RuntimeError):
        pass

    fake_bot.TaskCapacityError = TaskCapacityError

    async def core_task_create(prompt, *, verbose_level=None):
        if not prompt:
            raise ValueError("description required")
        if getattr(fake_bot, "_at_capacity", False):
            raise TaskCapacityError("too many active tasks")
        return {
            "id": "t-stub01", "handle": "task-9", "status": "running",
            "verbose_level": verbose_level or "normal", "interval_min": 30,
        }

    async def core_task_stop(task_id):
        return {"id": task_id, "status": "stopped", "killed": True}

    fake_bot.core_task_create = core_task_create
    fake_bot.core_task_stop = core_task_stop

    fake_bot.agent_handles = MagicMock()
    # Default: resolve("task-1") -> ("t-301324", "task")
    fake_bot.agent_handles.resolve = MagicMock(return_value=("t-301324", "task"))
    fake_bot.agent_handles.assign = MagicMock(return_value="proj-99")

    fake_bot.task_module = MagicMock()
    fake_bot.task_module.WORKERS = {}
    fake_bot.task_module.find_task = MagicMock(return_value=(None, None))
    fake_bot.task_module.update_task_fields = MagicMock()
    fake_bot.task_module.archive = MagicMock(return_value=None)
    fake_bot.task_module.RESUME_PROMPT = "resume"
    fake_bot.task_module.spawn_worker = AsyncMock()

    fake_bot.dm_user = AsyncMock()

    fake_bot.PIPELINE_BIN = Path("/nonexistent/pipeline")
    fake_bot.PIPELINE_ROOT = Path("/nonexistent/root")
    fake_bot.PIPELINE_REPORTER = Path("/nonexistent/reporter")

    async def _run_pipeline_cli(*args, capture=True):
        return 0, "ok", ""
    fake_bot._run_pipeline_cli = _run_pipeline_cli
    fake_bot._stop_project_reporter = MagicMock()

    monkeypatch.setitem(sys.modules, "bot", fake_bot)
    yield fake_bot
    sys.modules.pop("bot", None)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch):
    """Point bridge_api at a per-test state dir with a deterministic
    tasks.json + handles.json. Returns the dir path."""
    from bridge_api import app as appmod
    monkeypatch.setattr(appmod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(appmod, "TASKS_JSON", tmp_path / "tasks.json")
    monkeypatch.setattr(appmod, "HANDLES_JSON", tmp_path / "handles.json")
    monkeypatch.setattr(appmod, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(appmod, "ACTIVE_PROJECT_POINTER", tmp_path / "active_project.txt")
    monkeypatch.setattr(appmod, "PROJECTS_ROOT", tmp_path / "projects")

    (tmp_path / "tasks.json").write_text(json.dumps({
        "active": [
            {
                "id": "t-aaa111",
                "session_id": "sess-1",
                "description": "hello",
                "created_at": 1700000000,
                "status": "idle",
                "working_dir": str(tmp_path / "work" / "t-aaa111"),
                "verbose_level": "normal",
            },
        ],
        "archived": [
            {
                "id": "t-old001",
                "session_id": "sess-old",
                "description": "old task",
                "created_at": 1600000000,
                "status": "complete",
                "working_dir": str(tmp_path / "work" / "t-old001"),
                "verbose_level": "normal",
            },
        ],
    }))
    (tmp_path / "handles.json").write_text(json.dumps({
        "counters": {"task": 5, "proj": 3},
        "task_handles": {"t-aaa111": "task-5"},
        "proj_handles": {},
    }))
    return tmp_path


@pytest.fixture
def admin_env(monkeypatch, admin_email):
    monkeypatch.setenv("ADMIN_EMAILS", f"{admin_email},supzekai@gmail.com")
    monkeypatch.setenv("BRIDGE_API_DEV_BYPASS", "1")
    yield


@pytest.fixture
def asgi_app(state_dir, admin_env, stub_bot):
    """Sync fixture returning the FastAPI app — pytest-asyncio's strict
    mode means an `async def client` fixture would need explicit decoration
    on every test consumer; doing the AsyncClient construction inline keeps
    the test bodies short."""
    from bridge_api.app import app
    return app


@pytest.fixture
def make_client(asgi_app):
    """Return an async-context-manager factory for httpx clients."""
    def _make() -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=asgi_app),
            base_url="http://testserver",
        )
    return _make


# ---------------------------------------------------------------------------
# /api/healthz — no auth.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthz_no_auth(make_client):
    async with make_client() as client:
        r = await client.get("/api/healthz")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Auth gate.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agents_requires_auth_no_jwt(make_client, monkeypatch):
    monkeypatch.delenv("BRIDGE_API_DEV_BYPASS", raising=False)
    async with make_client() as client:
        r = await client.get("/api/agents")
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_agents_dev_bypass_wrong_email_403(make_client):
    async with make_client() as client:
        r = await client.get("/api/agents", headers={"X-Dev-Email": "evil@example.com"})
        assert r.status_code == 403


@pytest.mark.asyncio
async def test_agents_dev_bypass_admin_email_200(make_client, admin_email):
    async with make_client() as client:
        r = await client.get("/api/agents", headers={"X-Dev-Email": admin_email})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_agents_dev_bypass_missing_dev_header_401(make_client):
    # Bypass is on but no X-Dev-Email header.
    async with make_client() as client:
        r = await client.get("/api/agents")
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# /api/agents — read path; data should match what's in tasks.json + handles.json.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agents_listing_matches_state(make_client, admin_email):
    async with make_client() as client:
        r = await client.get("/api/agents", headers={"X-Dev-Email": admin_email})
        assert r.status_code == 200
        body = r.json()
        assert len(body["active"]) == 1
        assert body["active"][0]["id"] == "t-aaa111"
        assert body["active"][0]["kind"] == "task"
        assert body["active"][0]["handle"] == "task-5"
        assert body["active"][0]["status"] == "idle"
        assert len(body["archived"]) == 1
        assert body["archived"][0]["id"] == "t-old001"
        assert body["archived"][0]["status"] == "complete"


@pytest.mark.asyncio
async def test_agents_includes_active_project(make_client, admin_email, state_dir):
    (state_dir / "active_project.txt").write_text("proj-cafef00d")
    proj_dir = state_dir / "projects" / "proj-cafef00d" / "phases" / "phase1"
    proj_dir.mkdir(parents=True)
    async with make_client() as client:
        r = await client.get("/api/agents", headers={"X-Dev-Email": admin_email})
        assert r.status_code == 200
        body = r.json()
        proj_entries = [a for a in body["active"] if a["kind"] == "project"]
        assert len(proj_entries) == 1
        assert proj_entries[0]["id"] == "proj-cafef00d"
        assert proj_entries[0]["status"].startswith("phase ")


# ---------------------------------------------------------------------------
# /api/agents/task — POST.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_task_empty_prompt_422(make_client, admin_email):
    async with make_client() as client:
        r = await client.post(
            "/api/agents/task",
            json={"prompt": ""},
            headers={"X-Dev-Email": admin_email},
        )
        assert r.status_code == 422


@pytest.mark.asyncio
async def test_create_task_returns_id_and_handle(make_client, admin_email):
    async with make_client() as client:
        r = await client.post(
            "/api/agents/task",
            json={"prompt": "do a thing"},
            headers={"X-Dev-Email": admin_email},
        )
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == "t-stub01"
        assert body["handle"] == "task-9"
        assert body["status"] == "running"


@pytest.mark.asyncio
async def test_create_task_capacity_409(make_client, admin_email, stub_bot):
    stub_bot._at_capacity = True
    async with make_client() as client:
        r = await client.post(
            "/api/agents/task",
            json={"prompt": "do a thing"},
            headers={"X-Dev-Email": admin_email},
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# /api/usage — mocks the OAuth + ccusage seams.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_usage_returns_valid_payload(make_client, admin_email, monkeypatch):
    import usage_report

    fake_payload = {
        "accounts": [
            {
                "name": "main",
                "is_active": True,
                "available": True,
                "windows": [
                    {
                        "key": "five_hour", "label": "5h",
                        "utilization": 42.0,
                        "resets_at": "2026-05-02T12:00:00+00:00",
                        "resets_in_minutes": 90.0,
                    },
                ],
                "extra_usage": None,
            },
        ],
        "today_utc": {
            "date": "2026-05-02",
            "total_cost_usd": 1.23,
            "models": [
                {
                    "model": "opus-4-7",
                    "is_known_pricing": True,
                    "cost_usd": 1.23,
                    "input_cost_usd": 0.5,
                    "output_cost_usd": 0.7,
                    "cache_write_cost_usd": 0.02,
                    "cache_read_cost_usd": 0.01,
                },
            ],
        },
        "active_block": None,
        "rolling_5d": {"days": 5, "cost_usd": 4.56, "tokens": 1_234_567},
    }
    monkeypatch.setattr(usage_report, "collect_usage_data", lambda: fake_payload)

    async with make_client() as client:
        r = await client.get("/api/usage", headers={"X-Dev-Email": admin_email})
        assert r.status_code == 200
        body = r.json()
        assert body["accounts"][0]["name"] == "main"
        assert body["today_utc"]["date"] == "2026-05-02"
        assert body["rolling_5d"]["tokens"] == 1_234_567


# ---------------------------------------------------------------------------
# /api/agents/{id} — archived-UUID fallback (overnight brief item 1).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_agent_archived_task_fallback(make_client, admin_email, state_dir):
    """Force the request through the archived-task fallback by using a
    `task-<n>` handle whose ``task_handles`` entry points at a UUID that
    isn't in the on-disk task list. The pre-existing ``active+archived``
    scan looks up by the *resolved* uuid and misses; the new fallback
    loop matches on the original ``agent_id`` and recovers the archived
    record. Removing the fallback turns this into a 404.
    """
    request_id = "task-77"
    archived_task_id = "task-77"  # archived record literally keyed by the handle
    # handle map points the handle at a different uuid that does NOT exist
    # in tasks.json — this is what makes the existing scan miss.
    (state_dir / "handles.json").write_text(json.dumps({
        "counters": {"task": 77, "proj": 3},
        "task_handles": {"t-orphaned": "task-77"},
        "proj_handles": {},
    }))
    (state_dir / "tasks.json").write_text(json.dumps({
        "active": [],
        "archived": [
            {
                "id": archived_task_id,
                "session_id": "sess-archived",
                "description": "an archived task",
                "created_at": 1600000001,
                "status": "archived",
                "working_dir": str(state_dir / "work" / archived_task_id),
            },
        ],
    }))
    async with make_client() as client:
        r = await client.get(
            f"/api/agents/{request_id}",
            headers={"X-Dev-Email": admin_email},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == archived_task_id
        assert body["kind"] == "task"
        assert body["status"] == "archived"
        assert body["description"] == "an archived task"


@pytest.mark.asyncio
async def test_get_agent_archived_project_dir_fallback(
    make_client, admin_email, state_dir, monkeypatch, tmp_path,
):
    """A `proj-<id>` UUID with no live state but with a matching
    `proj-<id>-<ts>/` directory under PROJECTS_ARCHIVED_DIR must resolve
    via the directory-scan fallback."""
    from bridge_api import app as appmod
    archived_root = tmp_path / "archived_projects"
    archived_root.mkdir()
    proj_id = "proj-deadbeef"
    pdir = archived_root / f"{proj_id}-1700000000"
    pdir.mkdir()
    (pdir / "INITIAL_BRIEF.md").write_text("archived project brief")
    monkeypatch.setattr(appmod, "PROJECTS_ARCHIVED_DIR", archived_root)

    async with make_client() as client:
        r = await client.get(
            f"/api/agents/{proj_id}",
            headers={"X-Dev-Email": admin_email},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == proj_id
        assert body["kind"] == "project"
        assert body["status"] == "archived"
        assert body["description"] == "archived project brief"
        assert body["working_dir"] == str(pdir)
