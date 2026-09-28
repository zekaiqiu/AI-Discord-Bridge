"""Prompt-injection markers and untrusted-content fence helper.

Duplicated from services/krak/app.py per Phase 2 brief; do not refactor
across services for v1. The motivation for the duplication is that krak
and chat have different deploy cadences, and a shared library would
create a release-coupling we don't want yet — if a marker false-positives
in chat we want to be able to tweak it here without touching krak.

Public surface:
  * ``fence_untrusted(content)`` — wraps an arbitrary string in the
    ⚠ BEGIN/END UNTRUSTED USER CONTENT ⚠ envelope. Used by the frontend
    to render tool output and (in a future phase) by the message handler
    to wrap any flagged chunks before they reach the model.
  * ``scan_suspicious(*texts)`` — returns sorted, deduped names of any
    matched patterns; mirrors krak's helper.

The pattern table itself, ``_SUSPICIOUS_PATTERNS``, is named with a leading
underscore to mirror krak byte-for-byte (the brief's "DUPLICATE the list...
match krak's shape exactly" mandate). It is exported via ``__all__`` so
callers that want to introspect or extend the table can do so, but the
underscore signals: the *contents* are the contract, not the binding name.
"""

from __future__ import annotations

import re
from typing import List, Pattern, Tuple

# Sourced from services/krak/app.py @ _SUSPICIOUS_PATTERNS. Keep this list
# byte-for-byte aligned with the krak version where practical; if the two
# diverge, leave a comment naming the chat-specific addition so future
# convergence work has a paper trail.
_SUSPICIOUS_PATTERNS: List[Tuple[Pattern[str], str]] = [
    # Classic prompt-injection phrasing.
    (re.compile(r"ignore\s+(?:all\s+|the\s+|previous\s+|prior\s+|earlier\s+|above\s+)*"
                r"(?:instructions|context|messages|prompts?|rules)", re.I),
     "ignore_instructions"),
    (re.compile(r"disregard\s+(?:all\s+|the\s+|previous\s+|prior\s+|earlier\s+|above\s+)*"
                r"(?:instructions|context|messages|prompts?|rules)", re.I),
     "disregard_instructions"),
    (re.compile(r"forget\s+(?:everything|all|prior|previous|the\s+above)", re.I), "forget"),
    (re.compile(r"you\s+are\s+now\b", re.I), "role_change"),
    (re.compile(r"new\s+(?:instructions|task|prompt|system|rules)", re.I), "new_instructions"),
    (re.compile(r"system\s+prompt", re.I), "system_prompt_mention"),
    (re.compile(r"override\s+(?:safety|rules|policy|guidelines|restrictions)", re.I),
     "override_safety"),
    # Role-play / jailbreak markers.
    (re.compile(r"\b(?:DAN|jailbreak|developer\s+mode|admin\s+mode)\b", re.I), "jailbreak_term"),
    (re.compile(r"pretend\s+(?:you\s+are|to\s+be)", re.I), "pretend"),
    (re.compile(r"\bact\s+as\s+(?:a|an|if)\b", re.I), "act_as"),
    # Fake tool/function-call envelopes.
    (re.compile(r"<\s*tool[_-]?(?:call|use|name)\b", re.I), "fake_tool_call"),
    (re.compile(r"<\s*function[_-]?call\b", re.I), "fake_function_call"),
    (re.compile(r"```\s*(?:bash|sh|shell|python|py)\b", re.I), "code_fence_with_runtime"),
    # Shell-shaped imperatives.
    (re.compile(r"(?:curl|wget)\s+\S+\s*\|\s*(?:sh|bash)", re.I), "curl_pipe_shell"),
    (re.compile(r"\brm\s+-rf\s+/", re.I), "rm_rf"),
    (re.compile(r"\bos\.system\s*\(", re.I), "os_system"),
    (re.compile(r"\bsubprocess\.\s*(?:call|run|Popen)", re.I), "subprocess_call"),
    # Exfiltration shapes.
    (re.compile(r"(?:read|cat|print|show|send|exfil|leak|dump)\b[^\n]{0,40}\.env", re.I),
     "env_file_read"),
    (re.compile(r"(?:read|cat)\s+/etc/(?:passwd|shadow|hosts)", re.I), "system_file_read"),
    # Unicode tricks.
    (re.compile(r"[\u202a-\u202e\u2066-\u2069]"), "bidi_override"),
    (re.compile(r"[\U000E0000-\U000E007F]"), "tag_chars"),
    (re.compile(r"[\u200b-\u200d\u2060-\u206f\ufeff]"), "zero_width"),
]


_FENCE_BEGIN = "\u26a0 BEGIN UNTRUSTED USER CONTENT \u26a0"
_FENCE_END = "\u26a0 END UNTRUSTED USER CONTENT \u26a0"


def fence_untrusted(content: str) -> str:
    """Wrap ``content`` in the standard untrusted-content envelope.

    The envelope is purely a marker for downstream consumers (the model in
    chat, the human reviewer in the frontend). It does NOT attempt to
    neutralise the content — escaping/stripping is the caller's job.
    """
    if not isinstance(content, str):
        raise TypeError("content must be a string")
    return f"{_FENCE_BEGIN}\n{content}\n{_FENCE_END}"


def scan_suspicious(*texts: str) -> List[str]:
    """Return sorted, deduped marker names that matched any of ``texts``.

    Empty list = clean. Mirrors krak's ``_scan_suspicious`` helper. Phase 4
    will call this from the message handler; Phase 2 only needs the
    patterns + fence available, but exposing the scanner now costs
    nothing and avoids a future "why isn't this importable" pass.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return []
    hits = {name for pat, name in _SUSPICIOUS_PATTERNS if pat.search(blob)}
    return sorted(hits)


__all__ = ["_SUSPICIOUS_PATTERNS", "fence_untrusted", "scan_suspicious"]
