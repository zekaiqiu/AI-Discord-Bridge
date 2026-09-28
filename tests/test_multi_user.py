"""Multi-user support: ALLOWED_USER_IDS gate + authenticated-sender note.

Ported from the diverged bridge. Contract:

  * ``ALLOWED_USER_ID`` stays the owner (DM pings, task-worker env).
  * ``ALLOWED_USER_IDS`` (comma-separated, optional) extends who may talk
    to the bot; the owner is always in the set even if unlisted.
  * ``on_message`` drops anyone not in the set before ANY further handling.
  * Every accepted message is audit-logged with its Discord-stamped
    author id/name and role (owner | allowlisted).
  * The inline chat path prepends an authenticated-sender identity note to
    the prompt so the model can tell who it is actually talking to —
    transcript display names are spoofable, author.id is not.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot

BRIDGE_DIR = Path(bot.__file__).resolve().parent

# A user id that is guaranteed not to be the configured owner.
STRANGER_ID = 987654321098765432
EXTRA_ID = 111122223333444455


# --------------------------------------------------------------- fakes

class _FakeState:
    """Minimal ConnectionState duck for constructing a real DMChannel."""

    @staticmethod
    def store_user(data: dict) -> SimpleNamespace:
        return SimpleNamespace(id=int(data["id"]), name=data["username"])


class _TypingCtx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeChannel:
    """Plain messageable for run_synchronous (no discord internals needed:
    typing() ctx manager + id is all the inline path touches when the sink
    is a black hole)."""

    def __init__(self, cid: int = 555000111):
        self.id = cid
        self.sent: list[str] = []

    def typing(self) -> _TypingCtx:
        return _TypingCtx()

    async def send(self, content=None, **kw):
        self.sent.append(content)


def _dm_channel(cid: int = 555000111) -> "discord.DMChannel":
    import discord

    return discord.DMChannel(
        me=SimpleNamespace(id=1),
        state=_FakeState(),
        data={
            "id": str(cid),
            "recipients": [
                {"id": "42", "username": "someuser", "discriminator": "0", "avatar": None}
            ],
        },
    )


def _msg(
    author_id: int, *, content: str = "hi", name: str = "tester",
    cid: int = 555000111, channel=None,
):
    return SimpleNamespace(
        author=SimpleNamespace(id=author_id, bot=False, name=name),
        channel=channel if channel is not None else _dm_channel(cid),
        content=content,
        attachments=[],
        mentions=[],
    )


# ------------------------------------------------- import-time env parsing

def _bot_set_with_env(extra_env: dict | None) -> set[int]:
    """Import bot in a fresh interpreter with an augmented env; return
    its parsed ALLOWED_USER_IDS. Import-time env needs a real subprocess
    (module-level code, no reload tricks)."""
    env = {**os.environ, **(extra_env or {})}
    env.pop("ALLOWED_USER_IDS", None) if extra_env is None else None
    code = (
        "import bot, json; "
        "print(json.dumps(sorted(bot.ALLOWED_USER_IDS))); "
        "print(bot.ALLOWED_USER_ID)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BRIDGE_DIR), env=env, capture_output=True, text=True, check=True,
    )
    ids, owner = out.stdout.strip().splitlines()
    return set(json.loads(ids)), int(owner)


def test_allowed_user_ids_unset_is_owner_only():
    ids, owner = _bot_set_with_env(None)
    assert ids == {owner}


def test_allowed_user_ids_env_extends_and_includes_owner():
    ids, owner = _bot_set_with_env({"ALLOWED_USER_IDS": f"{EXTRA_ID}, 111111111111111111"})
    assert ids == {owner, EXTRA_ID, 111111111111111111}


def test_allowed_user_ids_empty_and_junk_entries_ignored():
    ids, owner = _bot_set_with_env({"ALLOWED_USER_IDS": " ,, "})
    assert ids == {owner}


# --------------------------------------------------------- on_message gate

def _run(coro):
    return asyncio.run(coro)


def test_on_message_drops_stranger(monkeypatch):
    seen = []

    async def fake_sync(msg, prompt):
        seen.append((msg.author.id, prompt))

    monkeypatch.setattr(bot, "run_synchronous", fake_sync)
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {bot.ALLOWED_USER_ID, EXTRA_ID})
    _run(bot.on_message(_msg(STRANGER_ID)))
    assert seen == []


def test_on_message_accepts_allowlisted_non_owner(monkeypatch, caplog):
    seen = []

    async def fake_sync(msg, prompt):
        seen.append((msg.author.id, prompt))

    monkeypatch.setattr(bot, "run_synchronous", fake_sync)
    monkeypatch.setattr(bot, "ALLOWED_USER_IDS", {bot.ALLOWED_USER_ID, EXTRA_ID})
    with caplog.at_level(logging.INFO, logger="claude-bridge"):
        _run(bot.on_message(_msg(EXTRA_ID, content="knock knock")))
    assert seen == [(EXTRA_ID, "knock knock")]
    audit = [r for r in caplog.records if "inbound msg author" in r.message]
    assert audit and f"id={EXTRA_ID}" in audit[0].message
    assert "role=allowlisted" in audit[0].message


def test_on_message_owner_gets_owner_role(monkeypatch, caplog):
    seen = []

    async def fake_sync(msg, prompt):
        seen.append(msg.author.id)

    monkeypatch.setattr(bot, "run_synchronous", fake_sync)
    with caplog.at_level(logging.INFO, logger="claude-bridge"):
        _run(bot.on_message(_msg(bot.ALLOWED_USER_ID)))
    assert seen == [bot.ALLOWED_USER_ID]
    audit = [r for r in caplog.records if "inbound msg author" in r.message]
    assert audit and "role=owner" in audit[0].message


# -------------------------------------------------- identity note injection

def _patch_inline_path(monkeypatch, tmp_path, *, transcript=""):
    captured: dict = {}

    async def fake_run_claude(prompt, **kw):
        captured["prompt"] = prompt
        captured["transcript"] = kw.get("transcript")
        return "ok", SimpleNamespace(switched=False)

    async def fake_attachments(msg):
        return None, []

    async def fake_transcript(msg):
        return transcript

    async def fake_send_response(channel, content):
        captured["reply"] = content

    art = tmp_path / "art"
    art.mkdir()
    monkeypatch.setattr(bot, "run_claude", fake_run_claude)
    monkeypatch.setattr(bot, "fetch_attachments", fake_attachments)
    monkeypatch.setattr(bot, "fetch_channel_transcript", fake_transcript)
    monkeypatch.setattr(bot, "send_response", fake_send_response)
    monkeypatch.setattr(bot, "_make_artifacts_dir", lambda: art)
    monkeypatch.setattr(bot, "_process_image_requests", lambda d: None)
    monkeypatch.setattr(bot, "_process_voice_requests", lambda d: None)
    monkeypatch.setattr(bot, "_list_outbound_artifacts", lambda d: [])
    monkeypatch.setattr(bot, "BRIDGE_CONFIG_PATH", tmp_path / "cfg.json")
    return captured


def test_run_synchronous_prepends_identity_note(monkeypatch, tmp_path):
    captured = _patch_inline_path(
        monkeypatch, tmp_path, transcript="alice: earlier\nbob: stuff"
    )
    msg = _msg(
        EXTRA_ID, content="who am I talking as?", name="eve",
        channel=_FakeChannel(),
    )
    _run(bot.run_synchronous(msg, "who am I talking as?"))

    p = captured["prompt"]
    assert p.startswith("[Authenticated sender — from Discord's stamped author.id")
    assert f"id={EXTRA_ID}" in p
    assert "username='eve'" in p
    assert "role=allowlisted" in p
    # The note must precede the actual user text.
    assert p.index("trust THIS line") < p.index("who am I talking as?")
    # Transcript still flows through untouched.
    assert captured["transcript"] == "alice: earlier\nbob: stuff"


def test_run_synchronous_identity_note_owner_role(monkeypatch, tmp_path):
    captured = _patch_inline_path(monkeypatch, tmp_path)
    msg = _msg(bot.ALLOWED_USER_ID, content="", name="owner", channel=_FakeChannel())
    _run(bot.run_synchronous(msg, ""))

    assert "role=owner" in captured["prompt"]
    # Empty prompt still yields the standard placeholder after the note.
    assert captured["prompt"].endswith("(no text — see attachments)")
