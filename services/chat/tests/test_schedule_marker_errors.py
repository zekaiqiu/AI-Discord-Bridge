"""Schedule markers: timing-key aliases are accepted, and a marker that
still fails is reported to the caller (which puts a visible notice in the
reply) instead of only leaving a hidden .schedule_error file.

2026-09-29: GLM wrote a marker with no in/at/every key; the reply said the
wake was set, and nothing told the user it wasn't.
"""
from __future__ import annotations

import asyncio
import json

import app


def _setup(monkeypatch, tmp_path):
    registered: list[dict] = []

    def fake_register(email, sid, prompt, **kw):
        registered.append({"prompt": prompt, **kw})
        return {"id": f"w{len(registered)}", "next_fire": 0}

    monkeypatch.setattr(app, "_session_generated_dir", lambda sid: tmp_path)
    monkeypatch.setattr(app.chat_scheduler, "register", fake_register)
    return registered


def _marker(tmp_path, name, spec):
    (tmp_path / f"{app._SCHEDULE_REQUEST_PREFIX}{name}.json").write_text(json.dumps(spec))


def test_aliases_register(monkeypatch, tmp_path):
    registered = _setup(monkeypatch, tmp_path)
    _marker(tmp_path, "a", {"prompt": "check A", "delay": "10m"})
    _marker(tmp_path, "b", {"prompt": "check B", "interval": "1h"})
    errors: list[str] = []
    n = asyncio.run(app._process_schedule_requests(session_id="s", email="e", errors=errors))
    assert n == 2 and errors == []
    assert registered[0]["delay_seconds"] == 600
    assert registered[1]["every_seconds"] == 3600


def test_bad_marker_reported(monkeypatch, tmp_path):
    registered = _setup(monkeypatch, tmp_path)
    _marker(tmp_path, "d", {"prompt": "watch", "soon": True})
    errors: list[str] = []
    n = asyncio.run(app._process_schedule_requests(session_id="s", email="e", errors=errors))
    assert n == 0 and registered == []
    assert len(errors) == 1 and "'in', 'at', or 'every'" in errors[0]
    # The old hidden trace is still written, and the marker is consumed.
    assert list(tmp_path.glob("*.schedule_error"))
    assert not list(tmp_path.glob("*.json"))


def test_errors_param_optional(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _marker(tmp_path, "d", {"prompt": "watch"})
    assert asyncio.run(app._process_schedule_requests(session_id="s", email="e")) == 0
