"""New model lineup (2026-09-28): TokenHub glm/kimi lead the picker, the
Anthropic/claude aliases are removed, and turns carry an optional per-turn
reasoning-effort level (OpenAI-style ``reasoning_effort``).

Covers the app-level seams hermetically (fake runner; no network):
  * ``_normalize_model`` — legacy identity vs new-lineup mapping.
  * ``_validated_effort`` — per-model level allowlist.
  * POST /messages dispatch — missing / stale-claude model maps to the
    default; effort is forwarded when valid, dropped when not.
  * admin sessions get host-shell tool dispatch (workdir/home /home/felix)
    instead of losing tools on the OpenAI-compatible path.
  * settings coercion — a saved default_model of a removed alias falls
    back to glm (free migration of pre-change blobs).
"""

from __future__ import annotations

from typing import Any

import app as app_module
import claude_runner
import tokenhub_runner
from helpers import consume_sse, create_session

USER_A = "alice@example.com"
ADMIN_A = "lineup-admin@example.com"


def _install_fake_tokenhub(monkeypatch: Any) -> dict[str, Any]:
    """Replace tokenhub_runner.run_turn with a recorder fake.

    The fake streams one delta + done (the minimal contract the worker's
    consume loop needs) and records every kwarg it was called with.
    """
    captured: dict[str, Any] = {"calls": []}

    async def fake_run_turn(**kwargs: Any):
        captured["calls"].append(kwargs)
        yield {"type": "delta", "text": "hello world"}
        yield {
            "type": "done",
            "full_text": "hello world",
            "meta": {"model": kwargs.get("model")},
        }

    monkeypatch.setattr(app_module.tokenhub_runner, "run_turn", fake_run_turn)
    # Also patch the module object itself in case app re-imported it.
    monkeypatch.setattr(tokenhub_runner, "run_turn", fake_run_turn)
    return captured


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_normalize_model_identity_in_legacy_mode(monkeypatch):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "")
    assert app_module._normalize_model(None) is None
    assert app_module._normalize_model("default") == "default"
    assert app_module._normalize_model("opus5") == "opus5"
    assert app_module._normalize_model("qwen") == "qwen"


def test_normalize_model_new_lineup(monkeypatch):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    # Known non-claude aliases pass through untouched.
    assert app_module._normalize_model("glm") == "glm"
    assert app_module._normalize_model("kimi") == "kimi"
    assert app_module._normalize_model("qwen") == "qwen"
    assert app_module._normalize_model("gemma4-local") == "gemma4-local"
    # Missing / sentinel / stale claude aliases map to the default model.
    assert app_module._normalize_model(None) == "glm"
    assert app_module._normalize_model("default") == "glm"
    assert app_module._normalize_model("opus5") == "glm"
    assert app_module._normalize_model("fable") == "glm"
    assert app_module._normalize_model("sonnet") == "glm"
    assert app_module._normalize_model("haiku") == "glm"
    assert app_module._normalize_model("garbage") == "glm"


def test_validated_effort():
    v = app_module._validated_effort
    assert v("glm", "max") == "max"
    assert v("glm", "low") == "low"
    # medium is NOT a served GLM level — must be dropped, never sent.
    assert v("glm", "medium") is None
    # Kimi Code k3 declares low/high/max only (2026-09-29).
    assert v("kimi", "medium") is None
    assert v("kimi", "low") == "low"
    assert v("kimi", "max") == "max"
    assert v("deepseek", "none") == "none"
    assert v("qwen", "high") == "high"
    assert v("minimax", "high") == "high"
    # No effort control for these — always dropped.
    assert v("gemma4-local", "low") is None
    assert v(None, "low") is None
    assert v("glm", None) is None
    assert v("glm", "") is None


def test_effort_levels_match_runner_lineups():
    # Every model with served effort levels must be a real dispatch target.
    for alias in app_module.EFFORT_LEVELS:
        assert (
            tokenhub_runner.is_tokenhub_model(alias)
            or app_module.haihub_runner.is_haihub_model(alias)
            or app_module.mimo_runner.is_mimo_model(alias)
            or app_module.kimi_runner.is_kimi_model(alias)
        ), alias


# ---------------------------------------------------------------------------
# Dispatch through POST /messages (fake tokenhub runner, no network)
# ---------------------------------------------------------------------------

def test_missing_model_routes_to_default_tokenhub(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_tokenhub(monkeypatch)
    headers = auth_headers(USER_A)
    sid = create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping"},
    )
    assert resp.status_code == 200
    events = consume_sse(resp)
    types = [e.get("event") for e in events]
    assert types.count("done") == 1, types

    assert len(captured["calls"]) == 1
    call = captured["calls"][0]
    assert call["model"] == "glm"
    assert call["prompt"] == "ping"
    # Non-admin sessions never take the host-shell branch: tool targets
    # stay /workspace-shaped (or plain chat when no container exists).
    assert call["effort"] is None
    assert call["tool_workdir"] == "/workspace"
    assert call["tool_home"] == "/workspace"
    assert call["artifacts_path"] is None or call["artifacts_path"].startswith(
        "/workspace/.artifacts/"
    )


def test_stale_claude_alias_routes_to_default_tokenhub(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    """A pre-change tab still posting model="opus5" must NOT reach the
    claude CLI path — it maps to the default model."""
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_tokenhub(monkeypatch)
    headers = auth_headers(USER_A)
    sid = create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping", "model": "opus5"},
    )
    assert resp.status_code == 200
    events = consume_sse(resp)
    assert [e.get("event") for e in events].count("done") == 1
    assert captured["calls"][0]["model"] == "glm"
    # The claude path must not have been touched.
    assert fake_claude.calls == [] or all(
        "Summarize" in " ".join(str(a) for a in c.get("args", []))
        for c in fake_claude.calls
    )


def test_effort_forwarded_when_valid(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_tokenhub(monkeypatch)
    headers = auth_headers(USER_A)
    sid = create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping", "model": "glm", "effort": "max"},
    )
    assert resp.status_code == 200
    consume_sse(resp)
    assert captured["calls"][0]["model"] == "glm"
    assert captured["calls"][0]["effort"] == "max"


def test_effort_dropped_when_not_served_for_model(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    captured = _install_fake_tokenhub(monkeypatch)
    headers = auth_headers(USER_A)
    sid = create_session(client, headers)

    # "medium" is not a served GLM level; "xhigh" is claude-only.
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping", "model": "glm", "effort": "xhigh"},
    )
    assert resp.status_code == 200
    consume_sse(resp)
    assert captured["calls"][0]["effort"] is None


def test_admin_session_tools_target_host_shell(
    client, auth_headers, fake_claude, monkeypatch, drain_background_tasks
):
    """Admin sessions have no per-user container; their OpenAI-model turns
    must exec tools in the host-shell container (parity with the claude
    path's "host" dispatch) instead of silently losing tools."""
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    monkeypatch.setenv("ADMIN_EMAILS", ADMIN_A)
    captured = _install_fake_tokenhub(monkeypatch)
    headers = auth_headers(ADMIN_A)
    sid = create_session(client, headers)

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "ping", "model": "glm"},
    )
    assert resp.status_code == 200
    consume_sse(resp)
    call = captured["calls"][0]
    assert call["model"] == "glm"
    assert call["container"] == claude_runner.host_shell_container()
    assert call["tool_workdir"] == "/home/felix"
    assert call["tool_home"] == "/home/felix"
    # Artifacts block points at the host tenant dir for this session.
    assert call["artifacts_path"] == (
        claude_runner.artifacts_path_for("host", sid)
    )


# ---------------------------------------------------------------------------
# Settings coercion (free migration of pre-change blobs)
# ---------------------------------------------------------------------------

def test_settings_reject_removed_alias_and_coerce_to_glm(
    client, auth_headers, monkeypatch
):
    headers = auth_headers(USER_A)
    resp = client.put(
        "/api/settings",
        headers=headers,
        json={"default_model": "opus5"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_model"] == "glm"

    # And it round-trips through GET.
    resp = client.get("/api/settings", headers=headers)
    assert resp.json()["default_model"] == "glm"
