"""Regression tests for the 2026-09-28 sweep (six-area review)."""
from __future__ import annotations

import asyncio
import io
import json
import os
import time
from pathlib import Path

import pytest
from fastapi import HTTPException

import app as app_module
import account_router
import attachments
import chat_scheduler
import haihub_runner
import local_auth
import local_auth_routes
import storage
import tokenhub_runner
import user_container
from helpers import consume_sse, create_session

USER = "round6@example.com"


# --- export: non-ASCII titles must not 500 ---------------------------------

def test_export_with_cjk_title(client, auth_headers):
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    storage.set_title(USER, sid, "市场分析 report (v2)")
    for fmt in ("json", "md"):
        r = client.get(f"/api/sessions/{sid}/export?format={fmt}", headers=headers)
        assert r.status_code == 200, r.text
        cd = r.headers["content-disposition"]
        assert cd.startswith('attachment; filename="report-v2.' + fmt) or 'filename="' in cd
        assert "filename*=UTF-8''%E5%B8%82%E5%9C%BA" in cd
        cd.encode("latin-1")  # header must be encodable


# --- storage: owner-marked user dirs, byte-capped memory, merge ------------

def test_slug_collision_gets_separate_dir(tmp_sessions_dir):
    storage.create_session("a+b@x.com", role="user")
    d1 = storage._user_dir("a+b@x.com")
    d2 = storage._user_dir("a_b@x.com")
    assert d1 != d2 and d2.startswith(d1 + "-")
    assert Path(d1, "_owner").read_text().strip() == "a+b@x.com"
    storage.set_memory("a+b@x.com", "secret of A")
    assert storage.get_memory("a_b@x.com") == ""


def test_user_dir_is_case_insensitive_for_same_user(tmp_sessions_dir):
    storage.create_session("Case@X.com", role="user")
    assert storage._user_dir("case@x.com") == storage._user_dir("CASE@x.com")


def test_memory_cap_is_bytes(tmp_sessions_dir):
    cjk = "中" * 6000  # 18 kB
    stored = storage.set_memory(USER, cjk)
    assert len(stored.encode("utf-8")) <= storage.MAX_MEMORY_BYTES


def test_memory_merge_keeps_concurrent_additions(tmp_sessions_dir):
    base = "- likes tea\n- has a cat"
    storage.set_memory(USER, base)
    # another window added a line meanwhile
    storage.set_memory(USER, base + "\n- moved to HK")
    merged = storage.set_memory_merged(USER, "- likes tea\n- has a cat\n- prefers dark mode", base)
    assert "- prefers dark mode" in merged and "- moved to HK" in merged
    # unchanged on disk → plain overwrite
    assert storage.set_memory_merged(USER, "fresh", merged) == "fresh"


def test_fork_keeps_workspace(tmp_sessions_dir):
    src = storage.create_session(USER, role="user", workspace="shared", container="c")
    storage.append_user_message(USER, src["id"], "hi")
    new = storage.fork_session_seed(USER, src["id"], 1)
    assert new["workspace"] == "shared"


def test_user_message_records_turn_and_image_turns_are_not_recovered(tmp_sessions_dir):
    s = storage.create_session(USER, role="admin")
    storage.append_user_message(USER, s["id"], "draw", turn={"mode": "image", "model": None})
    storage.append_assistant_placeholder(USER, s["id"])
    recs = storage.find_streaming_placeholders()
    assert recs and recs[0]["recoverable"] is False and recs[0]["turn"] == {"mode": "image"}


# --- scheduler --------------------------------------------------------------

def test_recurring_interval_floor(tmp_sessions_dir):
    rec = chat_scheduler.register(USER, "11111111-2222-3333-4444-555555555555", "p",
                                  delay_seconds=60, every_seconds=5)
    assert rec["interval_seconds"] >= chat_scheduler._MIN_INTERVAL_SECONDS


def test_corrupt_store_is_set_aside_not_wiped(tmp_sessions_dir):
    path = chat_scheduler._store_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Path(path).write_text("{not json")
    assert chat_scheduler._load() == {"schedules": [], "counter": 0}
    aside = [p for p in os.listdir(os.path.dirname(path)) if p.startswith("_schedules.json.corrupt-")]
    assert aside and not os.path.exists(path)


# --- attachments ------------------------------------------------------------

def test_next_filename_never_collides(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    n1 = attachments._next_filename(tmp_path, "a.txt")
    (tmp_path / n1).write_text("y")
    n2 = attachments._next_filename(tmp_path, "a.txt")
    assert n1 != "a.txt" and n2 not in ("a.txt", n1)


def test_session_upload_cap(tmp_attachments_dir, monkeypatch):
    from fastapi import UploadFile
    monkeypatch.setattr(attachments, "MAX_SESSION_BYTES", 30)
    sid = "11111111-2222-3333-4444-555555555555"
    async def go():
        await attachments.save_uploads(sid, [UploadFile(io.BytesIO(b"x" * 20), filename="a.txt")])
        with pytest.raises(attachments.AttachmentError) as ei:
            await attachments.save_uploads(sid, [UploadFile(io.BytesIO(b"y" * 20), filename="b.txt")])
        assert ei.value.status_code == 413
    asyncio.run(go())
    assert not (attachments.session_attachments_dir(sid) / "b.txt").exists()


# --- account router / credentials ------------------------------------------

def test_fail_open_skips_expired_token(hermetic_accounts, monkeypatch):
    monkeypatch.setattr(account_router, "_fetch_usage", lambda tok: None)
    account_router._reset_for_tests()
    names = [n for n, _ in account_router.list_accounts()]
    assert len(names) >= 2
    first, second = names[0], names[1]
    for name, exp in ((first, int(time.time() * 1000) - 1000), (second, int(time.time() * 1000) + 3_600_000)):
        creds = account_router.home_for_account(name) / ".claude" / ".credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-x", "expiresAt": exp}}))
    assert account_router.pick().name == second


def test_populate_refuses_main_credentials(monkeypatch):
    monkeypatch.delenv("CHAT_ACCOUNT_EXCLUDE", raising=False)
    with pytest.raises(RuntimeError):
        user_container._refuse_main_credentials("main")
    monkeypatch.setattr(account_router, "home_for_account", lambda n: account_router.MAIN_HOME)
    with pytest.raises(RuntimeError):
        user_container._refuse_main_credentials("removed-account")
    monkeypatch.setenv("CHAT_ACCOUNT_EXCLUDE", "")
    user_container._refuse_main_credentials("main")  # explicitly re-allowed


# --- auth -------------------------------------------------------------------

def test_password_endpoints_are_off_in_passwordless_mode(monkeypatch):
    monkeypatch.setattr(local_auth, "_passwordless", lambda: True)
    with pytest.raises(HTTPException) as ei:
        local_auth_routes._passwordless_only()
    assert ei.value.status_code == 404
    monkeypatch.setattr(local_auth, "_passwordless", lambda: False)
    local_auth_routes._passwordless_only()


def test_reset_flow_is_allowlisted(monkeypatch):
    monkeypatch.setattr(local_auth, "_allowed_emails", lambda: set())
    monkeypatch.setattr(local_auth, "_allowed_email_domains", lambda: {"wizerith.com"})
    with pytest.raises(HTTPException) as ei:
        local_auth_routes._allowlist_gate("attacker@gmail.com")
    assert ei.value.status_code == 403
    local_auth_routes._allowlist_gate("someone@wizerith.com")


def test_admin_emails_fail_closed_in_local_mode(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    monkeypatch.setenv("AUTH_MODE", "local")
    assert app_module._admin_emails() == frozenset()
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@wizerith.com")
    assert "boss@wizerith.com" in app_module._admin_emails()


# --- app helpers ------------------------------------------------------------

def test_memory_block_in_code_fence_is_ignored():
    text = "The protocol is:\n```\n<memory_update>\nexample\n</memory_update>\n```\nDone."
    out, mem = app_module._extract_memory_update(text)
    assert mem is None and out == text


def test_nested_memory_wrapper_uses_strict_closer():
    text = "Answer.\n<memory_update>\n<memory>\nuser is felix\n</memory>\n</memory_update>"
    out, mem = app_module._extract_memory_update(text)
    assert mem == "<memory>\nuser is felix\n</memory>" and out == "Answer."


def test_empty_memory_block_does_not_wipe():
    out, mem = app_module._extract_memory_update("ok\n<memory_update></memory_update>")
    assert mem is None and out == "ok"


def test_scrub_partial_strips_memory_block():
    assert app_module._scrub_partial("half\n<memory_update>\nx\n</memory_update>") == "half"


def test_history_skips_errored_turns_and_cuts_at_turn_boundary(monkeypatch):
    msgs = [
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "boom _⚠ generation failed_", "status": "error"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2", "status": "complete"},
    ]
    h = app_module._format_prior_history(msgs)
    assert "generation failed" not in h and "User: q2" in h
    monkeypatch.setattr(app_module, "_STATELESS_HISTORY_MAX_CHARS", 30)
    long = [{"role": "user", "content": "x" * 40}, {"role": "assistant", "content": "y" * 40, "status": "complete"},
            {"role": "user", "content": "last question"}]
    t = app_module._stateless_history(long)
    assert t.endswith("User: last question") and "truncated" in t


def test_done_meta_is_persisted_and_emitted(client, auth_headers, fake_claude, monkeypatch, drain_background_tasks):
    monkeypatch.setattr(app_module, "CHAT_DEFAULT_MODEL", "glm")
    async def fake_run_turn(**kw):
        yield {"type": "delta", "text": "hi"}
        yield {"type": "done", "full_text": "hi", "meta": {"model": "glm-5.3", "tokens": {"input": 1, "output": 2}}}
    monkeypatch.setattr(app_module.tokenhub_runner, "run_turn", fake_run_turn)
    monkeypatch.setattr(tokenhub_runner, "run_turn", fake_run_turn)
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    r = client.post(f"/api/sessions/{sid}/messages", headers=headers, json={"text": "ping", "model": "glm"})
    events = consume_sse(r)
    done = [e for e in events if e["event"] == "done"][0]
    assert done["data"]["meta"]["model"] == "glm-5.3"
    last = storage.get_session(USER, sid)["messages"][-1]
    assert last["meta"]["tokens"]["output"] == 2
    assert storage.get_session(USER, sid)["messages"][-2]["turn"]["model"] == "glm"


# --- haihub stream parsing ------------------------------------------------------

class _FakeResp:
    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = chunks
        self.status_code = status_code
    async def aiter_bytes(self):
        for c in self._chunks:
            yield c
    async def aread(self):
        return b""


class _FakeStream:
    def __init__(self, resp): self._resp = resp
    async def __aenter__(self): return self._resp
    async def __aexit__(self, *a): return False


class _FakeClient:
    def __init__(self, chunks): self._chunks = chunks
    def stream(self, *a, **kw): return _FakeStream(_FakeResp(self._chunks))


def _collect_step(chunks):
    async def go():
        out = []
        async for ev in haihub_runner._stream_step(_FakeClient(chunks), {"model": "m"}, base_url="http://x", api_key="k"):
            out.append(ev)
        return out
    return asyncio.run(go())


def _sse(obj) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n\n"


def test_unicode_line_separator_in_content_survives():
    chunk = _sse({"choices": [{"delta": {"content": "line one line two"}, "finish_reason": None}]})
    # split the bytes mid-way to exercise buffering as well
    events = _collect_step([chunk[:10], chunk[10:], b"data: [DONE]\n\n"])
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert text == "line one line two"
    assert events[-1]["type"] == "_meta" and events[-1]["content"] == text


def test_provider_error_object_mid_stream_is_an_error():
    events = _collect_step([
        _sse({"choices": [{"delta": {"content": "partial"}}]}),
        _sse({"error": {"message": "upstream exploded"}}),
    ])
    assert events[-1]["type"] == "_error" and "upstream exploded" in events[-1]["message"]


def test_tool_calls_without_index_keep_separate_slots():
    events = _collect_step([
        _sse({"choices": [{"delta": {"tool_calls": [
            {"id": "call_a", "function": {"name": "run_bash", "arguments": '{"command":"a"}'}},
            {"id": "call_b", "function": {"name": "run_bash", "arguments": '{"command":"b"}'}},
        ]}, "finish_reason": "tool_calls"}]}),
        b"data: [DONE]\n\n",
    ])
    meta = events[-1]
    assert [t["id"] for t in meta["tool_calls"]] == ["call_a", "call_b"]
    assert meta["finish_reason"] == "tool_calls"


def test_unclosed_think_tag_is_kept_as_text():
    s = haihub_runner._ThinkStripper()
    out = s.feed("Use the <think> tag like this: ") + s.feed("rest of the answer") + s.flush()
    assert out == "Use the <think> tag like this: rest of the answer"
    s2 = haihub_runner._ThinkStripper()
    assert s2.feed("<think>hidden</think>visible") + s2.flush() == "visible"
