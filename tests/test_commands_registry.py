"""Registry behavior + help formatting."""
from __future__ import annotations

import pytest

from commands_registry import (
    Command,
    Registry,
    SECTIONS,
    format_help_filtered,
    format_help_listing,
    format_help_one,
    format_usage_hint,
)


def _cmd(name: str, **kw) -> Command:
    """Helper: build a Command with sensible defaults so tests stay terse."""
    defaults = {"section": "AGENT LIFECYCLE", "one_liner": "stub"}
    defaults.update(kw)
    return Command(name=name, **defaults)


# ---------- registration ----------


def test_registry_rejects_duplicate_name():
    r = Registry()
    r.register(_cmd("!foo"))
    with pytest.raises(ValueError, match="duplicate"):
        r.register(_cmd("!foo"))


def test_registry_rejects_alias_colliding_with_name():
    r = Registry()
    r.register(_cmd("!foo"))
    with pytest.raises(ValueError, match="duplicate"):
        r.register(_cmd("!bar", aliases=("!foo",)))


def test_registry_rejects_unknown_section():
    r = Registry()
    with pytest.raises(ValueError, match="unknown section"):
        r.register(Command(name="!foo", section="MADE UP", one_liner="x"))


# ---------- prefix matching ----------


def test_find_returns_longest_match_first():
    """!agent task must beat !agent — longest-prefix wins regardless of
    registration order."""
    r = Registry()
    r.register(_cmd("!agent"))
    r.register(_cmd("!agent task"))
    found = r.find("!agent task build a thing")
    assert found is not None
    assert found.name == "!agent task"


def test_find_returns_none_when_no_match():
    r = Registry()
    r.register(_cmd("!agent"))
    assert r.find("!nope") is None


def test_find_does_not_partial_match_within_word():
    """!agent must NOT match !agentfoo."""
    r = Registry()
    r.register(_cmd("!agent"))
    assert r.find("!agentfoo bar") is None


def test_find_matches_alias():
    r = Registry()
    r.register(_cmd("!agent project", aliases=("!project",)))
    found = r.find("!project something")
    assert found is not None and found.name == "!agent project"


def test_strip_name_removes_canonical_or_alias():
    r = Registry()
    r.register(_cmd("!agent project", aliases=("!project",)))
    cmd = r.find("!project build a thing")
    assert cmd is not None
    assert cmd.strip_name("!project build a thing") == "build a thing"
    assert cmd.strip_name("!agent project build a thing") == "build a thing"


# ---------- !help formatting ----------


def test_help_listing_groups_by_section_in_section_order():
    r = Registry()
    r.register(_cmd("!agent task", section="AGENT LIFECYCLE", one_liner="spawn task"))
    r.register(_cmd("!killall", section="FLEET", one_liner="kill all"))
    r.register(_cmd("!help", section="HELP", one_liner="show help"))
    out = format_help_listing(r)
    # AGENT LIFECYCLE must appear before FLEET, FLEET before HELP.
    assert out.index("AGENT LIFECYCLE") < out.index("FLEET") < out.index("HELP")
    assert "`!agent task` — spawn task" in out
    assert "`!killall` — kill all" in out


def test_help_listing_omits_deprecated_aliases():
    r = Registry()
    r.register(_cmd("!agent project", section="AGENT LIFECYCLE", one_liner="spawn"))
    r.register(_cmd(
        "!project",
        section="AGENT LIFECYCLE",
        one_liner="LEGACY",
        deprecated_by="!agent project",
    ))
    out = format_help_listing(r)
    assert "!agent project" in out
    assert "LEGACY" not in out


def test_help_listing_omits_hidden_commands():
    r = Registry()
    r.register(_cmd("!secret", hidden=True))
    r.register(_cmd("!visible"))
    out = format_help_listing(r)
    assert "!visible" in out
    assert "!secret" not in out


def test_help_filtered_matches_name_or_oneliner():
    r = Registry()
    r.register(_cmd("!agent task", one_liner="spawn opus worker"))
    r.register(_cmd("!agent project", one_liner="spawn pipeline"))
    r.register(_cmd("!bridge health", section="BRIDGE", one_liner="self-report"))
    # Name match
    assert "!agent task" in format_help_filtered(r, "task")
    # One-liner match
    out = format_help_filtered(r, "pipeline")
    assert "!agent project" in out and "!agent task" not in out
    # No match
    assert "no commands matching" in format_help_filtered(r, "zzz_nothing")


def test_help_one_returns_full_docs_with_aliases_and_deprecation():
    r = Registry()
    r.register(_cmd(
        "!agent project",
        one_liner="spawn pipeline",
        docs="Multi-line docs.\nWith examples.",
        aliases=("!ap",),
    ))
    r.register(_cmd(
        "!project",
        one_liner="LEGACY",
        deprecated_by="!agent project",
    ))
    one = format_help_one(r, "!agent project")
    assert "spawn pipeline" in one
    assert "Multi-line docs" in one
    assert "!ap" in one  # alias listed
    legacy = format_help_one(r, "!project")
    assert "deprecated" in legacy.lower()
    assert "!agent project" in legacy


def test_help_one_handles_missing_command_gracefully():
    r = Registry()
    assert "no command" in format_help_one(r, "!nope")


def test_help_one_accepts_name_without_bang():
    r = Registry()
    r.register(_cmd("!agent task", one_liner="spawn"))
    out = format_help_one(r, "agent task")
    assert "!agent task" in out


# ---------- subcommands_of + usage hints ----------


def test_subcommands_of_returns_namespace_children():
    r = Registry()
    r.register(_cmd("!agent task", one_liner="spawn task"))
    r.register(_cmd("!agent project", one_liner="spawn project"))
    r.register(_cmd("!agents", one_liner="list agents"))  # NOT a subcommand
    r.register(_cmd("!help", section="HELP", one_liner="show help"))
    subs = r.subcommands_of("!agent")
    names = [s.name for s in subs]
    # Both subcommands present, sibling/sigle commands excluded, sorted.
    assert names == ["!agent project", "!agent task"]


def test_subcommands_of_skips_deprecated_and_hidden():
    r = Registry()
    r.register(_cmd("!agent project", one_liner="spawn pipeline"))
    r.register(_cmd(
        "!project",
        one_liner="LEGACY",
        deprecated_by="!agent project",
    ))
    r.register(_cmd("!agent secret", one_liner="hidden", hidden=True))
    subs = r.subcommands_of("!agent")
    names = [s.name for s in subs]
    assert names == ["!agent project"]


def test_subcommands_of_returns_empty_for_unknown_namespace():
    r = Registry()
    r.register(_cmd("!agent task"))
    assert r.subcommands_of("!nope") == []
    assert r.subcommands_of("") == []


def test_subcommands_of_does_not_match_partial_first_word():
    """`!ag` is not the same prefix as `!agent`."""
    r = Registry()
    r.register(_cmd("!agent task"))
    assert r.subcommands_of("!ag") == []


def test_format_usage_hint_bare_namespace():
    r = Registry()
    r.register(_cmd("!agent task", one_liner="spawn task"))
    r.register(_cmd("!agent project", one_liner="spawn pipeline"))
    out = format_usage_hint(r, "!agent")
    assert out is not None
    # Header phrasing for bare namespace: "usage — ..."
    assert "usage" in out
    assert "`!agent task` — spawn task" in out
    assert "`!agent project` — spawn pipeline" in out
    # No "unknown subcommand" preamble in the bare case.
    assert "unknown subcommand" not in out


def test_format_usage_hint_with_attempted_subcommand():
    r = Registry()
    r.register(_cmd("!agent task", one_liner="spawn task"))
    out = format_usage_hint(r, "!agent", attempted_subcommand="foobar")
    assert out is not None
    assert "unknown subcommand" in out
    assert "!agent foobar" in out
    assert "`!agent task` — spawn task" in out


def test_format_usage_hint_returns_none_for_non_namespace():
    """`!agents` is a single command, not a namespace; should not
    accidentally produce a usage hint."""
    r = Registry()
    r.register(_cmd("!agents", one_liner="list"))
    assert format_usage_hint(r, "!agents") is None
    assert format_usage_hint(r, "!unknown") is None
