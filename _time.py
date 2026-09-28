"""Shared time helpers.

Extracted to avoid byte-identical copies of `_utcnow` and `_parse_iso`
drifting across confirmations.py, quotas.py, and state_store.py.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Default clock callable: timezone-aware UTC `now`."""
    return datetime.now(timezone.utc)


def parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 timestamp; tolerate a trailing 'Z' and assume UTC
    when the string is naive."""
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
