"""Self-describing command registry for the bridge bot.

Each handler registers itself once with the metadata it needs for `!help`
plus the runtime info the dispatcher needs to route messages. Adding a
new command in one place automatically updates `!help` — the registry is
the single source of truth.

The registry is *additive*: it lives alongside the existing if/elif
dispatch in bot.py rather than replacing it wholesale, so we can migrate
incrementally without breaking running flows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


# Section labels for `!help` grouping. The order here is the order they're
# rendered. Keep "AGENT LIFECYCLE" first since that's what most users look
# for; "HELP" last since it's self-referential.
SECTIONS: tuple[str, ...] = (
    "AGENT LIFECYCLE",
    "FLEET",
    "OBSERVABILITY",
    "ARTIFACTS",
    "QUOTAS & CONFIRMATIONS",
    "AUTOMATION",
    "EMERGENCY",
    "BRIDGE",
    "CONVERSATION",
    "HELP",
)


# A handler signature shaped to match bot.py's existing async handlers:
# they all take a discord.Message + a free-form args string.
Handler = Callable[..., Awaitable[None]]


@dataclass
class Command:
    """One registered command.

    `name` is the canonical user-facing form including the leading bang —
    e.g. "!agent task". Multi-word names are matched as a prefix (the
    dispatcher walks longest-first to disambiguate "!agent" from
    "!agent task").

    `aliases` are alternate spellings that route to the same handler.
    They appear in `!help <name>` but not as separate entries in the
    grouped listing.

    `handler(msg, args)` is invoked with the discord.Message and the rest
    of the prompt after the command's name has been stripped. Handlers
    register themselves at import time via `Registry.register`.

    `deprecated_by` is set on legacy aliases (e.g. "!task" → "!agent task").
    When the dispatcher routes through a deprecated entry, it emits a
    one-shot per-session deprecation notice naming the new form.
    """
    name: str
    section: str
    one_liner: str
    docs: str = ""
    aliases: tuple[str, ...] = ()
    handler: Optional[Handler] = None
    deprecated_by: Optional[str] = None
    hidden: bool = False  # don't show in default !help listing

    def matches(self, prompt_lower: str) -> bool:
        """True if `prompt_lower` starts with this command's name (as a word).

        We require either an exact match or a name + space prefix so that
        `!agent` does not match `!agentfoo` and `!agent task` does not
        match `!agent taskfoo`.
        """
        for token in (self.name, *self.aliases):
            t = token.lower()
            if prompt_lower == t or prompt_lower.startswith(t + " "):
                return True
        return False

    def strip_name(self, prompt: str) -> str:
        """Return the prompt with the matched name (or alias) removed."""
        prompt_lower = prompt.lower()
        for token in (self.name, *self.aliases):
            t = token.lower()
            if prompt_lower == t:
                return ""
            if prompt_lower.startswith(t + " "):
                return prompt[len(t):].lstrip()
        return prompt


class Registry:
    """Ordered collection of commands with longest-name-first lookup.

    Lookup ordering matters: "!agent task" must be tried before "!agent"
    or any "!agent <X>" prefix would also fire on "!agent task <foo>".
    """

    def __init__(self) -> None:
        self._commands: list[Command] = []

    def register(self, cmd: Command) -> Command:
        if cmd.section not in SECTIONS:
            raise ValueError(
                f"unknown section {cmd.section!r}; add it to SECTIONS first"
            )
        # Reject duplicate names — silent overrides cause confusing dispatch.
        existing_names: set[str] = set()
        for c in self._commands:
            existing_names.add(c.name.lower())
            for a in c.aliases:
                existing_names.add(a.lower())
        for token in (cmd.name, *cmd.aliases):
            if token.lower() in existing_names:
                raise ValueError(f"duplicate command/alias: {token!r}")
        self._commands.append(cmd)
        # Maintain longest-first by name length so prefix lookup is correct.
        self._commands.sort(key=lambda c: -len(c.name))
        return cmd

    def all(self) -> list[Command]:
        return list(self._commands)

    def find(self, prompt: str) -> Optional[Command]:
        """Find the command matching `prompt` by name or alias prefix."""
        p = prompt.lower()
        for cmd in self._commands:
            if cmd.matches(p):
                return cmd
        return None

    def subcommands_of(self, prefix: str) -> list[Command]:
        """Return commands whose name begins with `<prefix> ` — i.e. the
        subcommands registered under a multi-word namespace.

        Used by the dispatcher to show a usage hint when the user types a
        bare namespace like `!agent`, or an unknown subcommand like
        `!agent foobar`. Skips deprecated and hidden commands so the hint
        only advertises the canonical surface.
        """
        p = prefix.lower().strip()
        if not p:
            return []
        out: list[Command] = []
        for cmd in self._commands:
            if cmd.deprecated_by or cmd.hidden:
                continue
            if cmd.name.lower().startswith(p + " "):
                out.append(cmd)
        out.sort(key=lambda c: c.name)
        return out

    def by_section(self) -> dict[str, list[Command]]:
        """Return commands grouped by section, in SECTIONS order, with
        deprecated aliases and hidden commands omitted."""
        out: dict[str, list[Command]] = {s: [] for s in SECTIONS}
        for cmd in self._commands:
            if cmd.deprecated_by or cmd.hidden:
                continue
            out[cmd.section].append(cmd)
        # Within each section, sort alphabetically by name for stable output.
        for s in out:
            out[s].sort(key=lambda c: c.name)
        return out


# ---------- !help formatting ----------


def format_help_listing(reg: Registry) -> str:
    """Default `!help` output: grouped by section, one-liner per command."""
    lines: list[str] = []
    grouped = reg.by_section()
    for section in SECTIONS:
        cmds = grouped.get(section, [])
        if not cmds:
            continue
        lines.append(f"**{section}**")
        for c in cmds:
            lines.append(f"  `{c.name}` — {c.one_liner}")
        lines.append("")
    return "\n".join(lines).rstrip() or "_no commands registered_"


def format_help_filtered(reg: Registry, substring: str) -> str:
    """`!help <substring>`: include any command whose name or one-liner
    contains the substring (case-insensitive)."""
    s = substring.lower()
    matches: list[Command] = []
    for cmd in reg.all():
        if cmd.deprecated_by or cmd.hidden:
            continue
        if s in cmd.name.lower() or s in cmd.one_liner.lower():
            matches.append(cmd)
    if not matches:
        return f"_no commands matching `{substring}`_"
    matches.sort(key=lambda c: c.name)
    return "\n".join(
        f"**`{c.name}`** _({c.section})_ — {c.one_liner}" for c in matches
    )


def format_usage_hint(
    reg: Registry, prefix: str, *, attempted_subcommand: str = ""
) -> str | None:
    """Format the "you typed a bare namespace, here are its subcommands"
    response, or None if `prefix` is not a registered namespace.

    `attempted_subcommand` is the (invalid) word the user tried after the
    namespace — e.g. for `!agent foobar`, pass `"foobar"`. For the
    bare-namespace case (just `!agent`), pass `""`.
    """
    subs = reg.subcommands_of(prefix)
    if not subs:
        return None
    lines: list[str] = []
    if attempted_subcommand:
        lines.append(
            f"_unknown subcommand `{prefix} {attempted_subcommand}`. "
            f"Available `{prefix}` subcommands:_"
        )
    else:
        lines.append(f"_usage — `{prefix}` subcommands:_")
    for s in subs:
        lines.append(f"  `{s.name}` — {s.one_liner}")
    return "\n".join(lines)


def format_help_one(reg: Registry, name: str) -> str:
    """`!help <command>`: full docs for the given command (or alias)."""
    cmd = reg.find(name if name.startswith("!") else "!" + name)
    if cmd is None:
        return f"_no command `{name}`_"
    parts = [f"**`{cmd.name}`** _({cmd.section})_", cmd.one_liner]
    if cmd.aliases:
        parts.append("_aliases: " + ", ".join(f"`{a}`" for a in cmd.aliases) + "_")
    if cmd.deprecated_by:
        parts.append(f"_deprecated; use `{cmd.deprecated_by}` instead._")
    if cmd.docs.strip():
        parts.append("")
        parts.append(cmd.docs.strip())
    return "\n".join(parts)
