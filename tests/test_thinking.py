"""!thinking — thinking-transcript level-of-detail (off | brief | full).

Covers the config resolver (per-channel override > DM default > guild
off) and StreamSink's enforcement: off drops thinking, brief caps the
total thinking chars shown per turn, full streams everything.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import bot


class FakeMessage:
    def __init__(self) -> None:
        self.edits: list[str] = []

    async def edit(self, content: str | None = None, **kw) -> None:
        self.edits.append(content or "")


class FakeChannel:
    def __init__(self, cid: int = 333) -> None:
        self.id = cid
        self.sent: list[str] = []
        self.last: FakeMessage | None = None

    async def send(self, content: str | None = None, **kw) -> FakeMessage:
        self.sent.append(content or "")
        self.last = FakeMessage()
        return self.last


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "bridge-config.json"
    monkeypatch.setattr(bot, "BRIDGE_CONFIG_PATH", path)
    return path


def _write(cfg, data: dict) -> None:
    cfg.write_text(json.dumps(data), encoding="utf-8")


async def _drive(sink: "bot.StreamSink", events: list[tuple[str, str]]) -> None:
    for kind, text in events:
        await sink.feed(kind, text)
    await sink.finalize()


# ------------------------------------------------------------- resolver


def test_resolver_guild_defaults_to_off(cfg):
    # FakeChannel is not a discord DM/Group instance → guild path.
    assert bot.thinking_level_for(FakeChannel()) == "off"


def test_resolver_dm_uses_configured_default(cfg, monkeypatch):
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: True)
    assert bot.thinking_level_for(FakeChannel()) == "brief"
    _write(cfg, {"thinking_default": "full"})
    assert bot.thinking_level_for(FakeChannel()) == "full"


def test_resolver_channel_override_beats_default(cfg, monkeypatch):
    _write(cfg, {"thinking_default": "off", "thinking_channels": {"333": "full"}})
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: True)
    assert bot.thinking_level_for(FakeChannel(333)) == "full"


def test_resolver_guild_override_opt_in(cfg, monkeypatch):
    _write(cfg, {"thinking_channels": {"333": "brief"}})
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: False)
    assert bot.thinking_level_for(FakeChannel(333)) == "brief"


def test_resolver_garbage_values_fall_back(cfg, monkeypatch):
    _write(cfg, {"thinking_default": "nope", "thinking_channels": {"333": "bogus"}})
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: True)
    assert bot.thinking_level_for(FakeChannel(333)) == "brief"


def test_set_and_clear_channel_override(cfg, monkeypatch):
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: False)
    bot.set_channel_thinking(333, "full")
    assert bot.thinking_level_for(FakeChannel(333)) == "full"
    bot.set_channel_thinking(333, None)
    assert bot.thinking_level_for(FakeChannel(333)) == "off"
    assert "thinking_channels" in json.loads(cfg.read_text())
    assert json.loads(cfg.read_text())["thinking_channels"] == {}


# --------------------------------------------------------- StreamSink


def test_sink_off_drops_thinking_entirely(cfg, monkeypatch):
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: False)
    ch = FakeChannel()
    sink = bot.StreamSink(ch, verbose=True)
    assert sink.thinking_level == "off"
    asyncio.run(
        _drive(sink, [("thinking", "secret plan"), ("text", "the answer")])
    )
    joined = "".join(ch.sent)
    assert "secret plan" not in joined
    assert "the answer" in joined
    assert "💭" not in joined


def test_sink_brief_caps_total_thinking_per_turn(cfg, monkeypatch):
    _write(cfg, {"thinking_channels": {"333": "brief"}})
    ch = FakeChannel(333)
    sink = bot.StreamSink(ch, verbose=True)
    assert sink.thinking_level == "brief"
    asyncio.run(
        _drive(
            sink,
            [
                ("thinking", "x" * 250),
                ("thinking", "y" * 250),  # only ~50 chars of budget left
                ("thinking", "z" * 100),  # over budget → dropped
                ("text", "done"),
            ],
        )
    )
    # live_content mirrors what the live Discord message displays
    # (first segment posted via send, later ones folded in via edit).
    shown = sink.live_content
    assert "x" * 250 in shown           # first 250 chars shown
    assert "y" * 50 in shown            # budget filled to the cap
    assert "z" not in shown             # nothing past the cap
    assert "…" in shown                 # truncation marker
    assert "done" in shown              # normal text unaffected
    # Only one 💭 header: the dropped z-chunk must not nudge last_kind
    # and produce a stray second header.
    assert shown.count("💭") == 1


def test_sink_full_streams_everything(cfg, monkeypatch):
    _write(cfg, {"thinking_channels": {"333": "full"}})
    ch = FakeChannel(333)
    sink = bot.StreamSink(ch, verbose=True)
    assert sink.thinking_level == "full"
    asyncio.run(_drive(sink, [("thinking", "w" * 800), ("text", "ok")]))
    shown = sink.live_content
    assert "w" * 800 in shown
    assert "ok" in shown


def test_sink_blackhole_still_wins(cfg, monkeypatch):
    # verbose=False (terse guild channel) blacks holes everything,
    # whatever the thinking level says.
    _write(cfg, {"thinking_channels": {"333": "full"}})
    ch = FakeChannel(333)
    sink = bot.StreamSink(ch, verbose=False)
    asyncio.run(_drive(sink, [("thinking", "hidden"), ("text", "also hidden")]))
    assert sink.has_sent is False
    assert ch.sent == []



def test_tool_narration_hidden_by_default(cfg, monkeypatch):
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: True)
    monkeypatch.setattr(bot, "SHOW_TOOL_USE", False)
    ch = FakeChannel()
    sink = bot.StreamSink(ch, verbose=True)
    asyncio.run(
        _drive(sink, [("tool", "using tool: `Bash`\n"), ("text", "the answer")])
    )
    shown = sink.live_content or "".join(ch.sent)
    assert "using tool" not in shown and "🔧" not in shown
    assert "the answer" in shown


def test_tool_narration_can_be_re_enabled(cfg, monkeypatch):
    monkeypatch.setattr(bot, "_is_private_channel", lambda ch: True)
    monkeypatch.setattr(bot, "SHOW_TOOL_USE", True)
    ch = FakeChannel()
    sink = bot.StreamSink(ch, verbose=True)
    asyncio.run(
        _drive(sink, [("tool", "using tool: `Bash`\n"), ("text", "the answer")])
    )
    shown = sink.live_content or "".join(ch.sent)
    assert "using tool: `Bash`" in shown
