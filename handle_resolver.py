"""Short-handle resolver — minimal stand-in for the prior-build resolver.

Turns short handles like "p1", "t3" into canonical agent ids. The real
resolver from Phase 1 of the prior build keeps the mapping; here we expose
register_agent() / resolve() with the same shape so quotas.py can call it.
"""

from __future__ import annotations

from typing import Dict, Optional


_HANDLES: Dict[str, str] = {}


def register_agent(handle: str, canonical_id: str) -> None:
    _HANDLES[handle] = canonical_id


def resolve(handle_or_id: str) -> Optional[str]:
    if handle_or_id in _HANDLES:
        return _HANDLES[handle_or_id]
    # if it already looks canonical (registered as a value), pass through
    if handle_or_id in _HANDLES.values():
        return handle_or_id
    return None


def reset() -> None:
    _HANDLES.clear()
