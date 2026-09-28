"""Shared test helpers (reconstructed 2026-09-28).

``test_messages.py``, ``test_tokenhub_lineup.py``, ``test_dispatch_routing.py``
and ``test_user_wiring.py`` import these; the original module was never
committed and went missing from the working tree, which left the whole chat
suite failing at collection. Shapes are derived from the call sites:

  * ``create_session(client, headers) -> session id`` (POST /api/sessions)
  * ``consume_sse(resp) -> [{"event": <type>, "data": <parsed json>}, ...]``
    — one dict per SSE frame, in order; keepalive comments are skipped.
"""
from __future__ import annotations

import json
from typing import Any


def create_session(client: Any, headers: dict[str, str]) -> str:
    resp = client.post("/api/sessions", headers=headers, json={"title": None})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def consume_sse(resp: Any) -> list[dict[str, Any]]:
    """Parse a TestClient SSE response body into ordered event dicts."""
    body = resp.text if hasattr(resp, "text") else resp.read().decode("utf-8")
    events: list[dict[str, Any]] = []
    for frame in body.split("\n\n"):
        event_type: str | None = None
        data_lines: list[str] = []
        for line in frame.split("\n"):
            if not line or line.startswith(":"):
                continue  # keepalive / comment
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        if event_type is None and not data_lines:
            continue
        raw = "\n".join(data_lines)
        try:
            data: Any = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = raw
        events.append({"event": event_type or "message", "data": data})
    return events
