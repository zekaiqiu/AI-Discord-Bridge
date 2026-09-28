"""!help registry — minimal stand-in for the prior-build registry.

See agent_state.py header for the workspace-emptiness note. This module
exposes the same registration API the brief assumes (the one !agent / !new
already used). Phase 1 of THIS build only needs: register(section, command,
doc), help_sections(), help_for(command).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, List, Optional, Tuple


# section -> list of (command, doc) preserving insertion order
_REGISTRY: "OrderedDict[str, List[Tuple[str, str]]]" = OrderedDict()
# command -> doc, for !help <command>
_DOCS: Dict[str, str] = {}


def register(section: str, command: str, doc: str) -> None:
    if section not in _REGISTRY:
        _REGISTRY[section] = []
    # avoid duplicates if registration runs more than once
    for i, (cmd, _) in enumerate(_REGISTRY[section]):
        if cmd == command:
            _REGISTRY[section][i] = (command, doc)
            _DOCS[command] = doc
            return
    _REGISTRY[section].append((command, doc))
    _DOCS[command] = doc


def help_sections() -> "OrderedDict[str, List[Tuple[str, str]]]":
    return _REGISTRY


def help_for(command: str) -> Optional[str]:
    # accept "!confirm" or "confirm"
    key = command.lstrip("!")
    return _DOCS.get(key) or _DOCS.get("!" + key)


def _one_liner(cmd: str, doc: str) -> str:
    """Pull a short summary out of a self-identifying doc string.

    Phase 1-5 docs follow the shape `<cmd> [<args>] — <summary> [Example: ...]`.
    We strip the leading command/args prefix (legacy listing puts the
    backticked cmd on the row already) and any trailing Example clause
    (those belong in `!help <cmd>`, not the listing).
    """
    if not doc:
        return cmd
    first = doc.splitlines()[0].strip()
    sep = " — "
    if sep in first:
        first = first.split(sep, 1)[1]
    ex = first.find("Example:")
    if ex != -1:
        first = first[:ex].rstrip(" .")
    return first.strip()


def render_help() -> str:
    """Match commands_registry.format_help_listing style:
       **SECTION** header, ``  `!cmd` — one-liner`` row, blank between sections.
    Bare aliases (e.g. "confirm" without `!`) are skipped — they exist only
    so `!help confirm` resolves; including them duplicates the `!`-prefixed
    entry in the listing.
    """
    section_blocks: List[str] = []
    for section, entries in _REGISTRY.items():
        rows = [
            f"  `{cmd}` — {_one_liner(cmd, doc)}"
            for cmd, doc in entries
            if cmd.startswith("!")
        ]
        if not rows:
            continue
        section_blocks.append(f"**{section}**\n" + "\n".join(rows))
    return "\n\n".join(section_blocks)


def reset() -> None:
    """Test helper."""
    _REGISTRY.clear()
    _DOCS.clear()
