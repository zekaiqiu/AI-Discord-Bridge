"""SPA static serving + fallback. Covers both pre-build and built modes.

Pre-build (default in CI): the ``static/`` directory is empty/absent and
``GET /`` returns the JSON placeholder. ``GET /api/...`` and ``GET /healthz``
must remain unaffected.

Built (simulated by populating a tmp ``static/`` and monkeypatching
``app._STATIC_DIR``/``_INDEX_HTML``): asset paths serve from disk; any other
GET returns ``index.html`` with ``Content-Type: text/html``; reserved
prefixes still 404 cleanly when no API route matches.
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Pre-build mode (no static/ dir present in the repo).
# ---------------------------------------------------------------------------

def test_root_returns_placeholder_when_frontend_not_built(client, monkeypatch):
    # Force "not built" regardless of what's on disk in the workspace.
    import app as appmod
    fake = Path("/nonexistent/static-deliberately-missing")
    monkeypatch.setattr(appmod, "_STATIC_DIR", fake, raising=True)
    monkeypatch.setattr(appmod, "_INDEX_HTML", fake / "index.html", raising=True)

    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "frontend": "not built"}


def test_spa_path_returns_placeholder_when_not_built(client, monkeypatch):
    import app as appmod
    fake = Path("/nonexistent/static-deliberately-missing")
    monkeypatch.setattr(appmod, "_STATIC_DIR", fake, raising=True)
    monkeypatch.setattr(appmod, "_INDEX_HTML", fake / "index.html", raising=True)

    resp = client.get("/chat/abc-123")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "frontend": "not built"}


def test_healthz_unaffected_by_spa_fallback(client):
    # Whether built or not built, /healthz routes to its real handler.
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "chat"}


def test_api_routes_unaffected_by_spa_fallback(client, auth_headers):
    # GET /api/me requires auth; without it, must 401, NOT fall through to
    # the SPA placeholder.
    resp = client.get("/api/me")
    assert resp.status_code == 401

    # And with auth, /api/me works as before.
    resp = client.get("/api/me", headers=auth_headers("alice@example.com"))
    assert resp.status_code == 200
    # Phase 1 (role): response is now ``{email, role}``; alice is not felix
    # and there is no sandbox_users.json on disk, so role is "user".
    assert resp.json() == {"email": "alice@example.com", "role": "user"}


def test_unknown_api_path_404s_does_not_fall_through(client, auth_headers):
    # An unrouted /api/* path must 404, not return the SPA placeholder.
    resp = client.get(
        "/api/this-route-does-not-exist",
        headers=auth_headers("alice@example.com"),
    )
    assert resp.status_code == 404
    # Response body must NOT be the SPA placeholder — that would mean the
    # SPA is shadowing API 404s.
    body = resp.json()
    assert body != {"ok": True, "frontend": "not built"}


# ---------------------------------------------------------------------------
# Built mode: simulate by populating a tmp static/ and pointing the app at it.
# ---------------------------------------------------------------------------

@pytest.fixture
def built_static(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake built SPA layout and point ``app._STATIC_DIR`` at it.

    Returns the static dir for the test to inspect.
    """
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        "<!doctype html><html><head><title>Alde Chat</title></head>"
        "<body><div id='root'></div>"
        "<script type='module' src='/assets/index-abc.js'></script></body></html>"
    )
    (static / "assets" / "index-abc.js").write_text("console.log('hi');")
    (static / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")

    import app as appmod
    monkeypatch.setattr(appmod, "_STATIC_DIR", static, raising=True)
    monkeypatch.setattr(appmod, "_INDEX_HTML", static / "index.html", raising=True)
    return static


def test_root_serves_index_html_when_built(client, built_static: Path):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html"), resp.headers
    assert "<div id='root'></div>" in resp.text


def test_spa_route_falls_back_to_index_html(client, built_static: Path):
    # Any non-API, non-asset path: SPA owns it.
    for path in ("/chat", "/chat/abc-123", "/settings/profile"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.headers["content-type"].startswith("text/html"), path
        assert "<div id='root'></div>" in resp.text, path


def test_real_asset_served_directly(client, built_static: Path):
    resp = client.get("/assets/index-abc.js")
    assert resp.status_code == 200
    # Asset is served from disk; content-type is whatever FileResponse infers
    # (javascript or octet-stream depending on stdlib mimetypes), but the
    # body must be the file's bytes.
    assert "console.log" in resp.text


def test_static_prefix_path_also_resolves(client, built_static: Path):
    # Brief calls out /static/ as a reserved asset prefix; the catch-all
    # strips that prefix before resolving on-disk so /static/foo.js works
    # if foo.js exists at the static dir root.
    (built_static / "extra.txt").write_text("hello-extra")
    resp = client.get("/static/extra.txt")
    assert resp.status_code == 200
    assert "hello-extra" in resp.text


def test_traversal_attempt_does_not_escape_static_dir(
    client, built_static: Path, tmp_path: Path,
):
    # Plant a sentinel ABOVE the static dir; the SPA fallback must not
    # serve it even if the URL-encoded path tries to traverse out.
    sentinel = tmp_path / "secret.txt"
    sentinel.write_text("OWNED")
    # ``/..%2Fsecret.txt`` URL-decoded is ``/../secret.txt``; Starlette
    # normalises some encodings, but the safe-path check is the load-bearing
    # defence. We assert behavior, not the specific normalisation pathway.
    for url in ("/..%2Fsecret.txt", "/../secret.txt"):
        resp = client.get(url)
        # Either: it falls through to the SPA fallback (200 + index.html),
        # or it 404s. It must NEVER return "OWNED".
        assert "OWNED" not in resp.text, f"traversal succeeded via {url}"


def test_built_mode_does_not_break_api_routing(
    client, auth_headers, built_static: Path,
):
    # With the SPA built, /api/sessions still needs auth.
    resp = client.get("/api/sessions")
    assert resp.status_code == 401

    headers = auth_headers("alice@example.com")
    resp = client.get("/api/sessions", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
