"""Session CRUD + cross-email isolation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import storage


USER_A = "alice@example.com"
USER_B = "bob@example.com"


# ---------------------------------------------------------------------------
# Storage-level checks (file mode, slugging) — exercised via the HTTP API
# where possible to also cover the wiring.
# ---------------------------------------------------------------------------

def test_create_session_writes_file_with_mode_640(client, auth_headers, tmp_sessions_dir: Path):
    resp = client.post(
        "/api/sessions",
        headers=auth_headers(USER_A),
        json={"title": "first chat"},
    )
    assert resp.status_code == 201
    payload = resp.json()
    for field in ("id", "title", "created_at", "updated_at"):
        assert field in payload
    assert payload["title"] == "first chat"

    slug = storage.email_slug(USER_A)
    file_path = tmp_sessions_dir / slug / f"{payload['id']}.json"
    assert file_path.exists()

    mode = os.stat(file_path).st_mode & 0o777
    assert mode == 0o640, f"expected 0640, got {oct(mode)}"

    # File contents reflect the schema fields Phase 2 will mutate.
    data = json.loads(file_path.read_text())
    assert data["email"] == USER_A
    assert data["messages"] == []
    assert data["claude_session_id"]  # uuid populated at create time


def test_list_isolates_by_email(client, auth_headers):
    # User A has one session; user B has none.
    client.post("/api/sessions", headers=auth_headers(USER_A), json={"title": "A1"})

    resp_a = client.get("/api/sessions", headers=auth_headers(USER_A))
    assert resp_a.status_code == 200
    items_a = resp_a.json()
    assert len(items_a) == 1
    assert items_a[0]["title"] == "A1"

    resp_b = client.get("/api/sessions", headers=auth_headers(USER_B))
    assert resp_b.status_code == 200
    assert resp_b.json() == []


def test_get_session_cross_email_returns_404_no_existence_leak(client, auth_headers):
    created = client.post(
        "/api/sessions", headers=auth_headers(USER_A), json={"title": "A1"}
    ).json()
    sid = created["id"]

    # Owner can fetch.
    own = client.get(f"/api/sessions/{sid}", headers=auth_headers(USER_A))
    assert own.status_code == 200
    assert own.json()["id"] == sid

    # Foreign user gets 404, identical to a truly missing id.
    other = client.get(f"/api/sessions/{sid}", headers=auth_headers(USER_B))
    assert other.status_code == 404

    # Body must not reveal that the session exists. Any JSON field carrying
    # the id, the owner email, or the literal "exists" would be a leak.
    body = other.json()
    flat = json.dumps(body).lower()
    assert sid.lower() not in flat
    assert "alice" not in flat
    assert "exists" not in flat
    # Sanity: the missing-id case looks the same shape as the cross-email case.
    missing = client.get("/api/sessions/00000000-0000-0000-0000-000000000000",
                         headers=auth_headers(USER_B))
    assert missing.status_code == other.status_code
    assert set(missing.json().keys()) == set(body.keys())


def test_patch_title_owner_updates_non_owner_404(client, auth_headers):
    created = client.post(
        "/api/sessions", headers=auth_headers(USER_A), json={"title": "first"}
    ).json()
    sid = created["id"]

    # Non-owner cannot rename and gets the same 404 a missing session would.
    foreign = client.patch(
        f"/api/sessions/{sid}",
        headers=auth_headers(USER_B),
        json={"title": "hijack"},
    )
    assert foreign.status_code == 404

    # Owner rename succeeds.
    owner = client.patch(
        f"/api/sessions/{sid}",
        headers=auth_headers(USER_A),
        json={"title": "renamed"},
    )
    assert owner.status_code == 200
    assert owner.json()["title"] == "renamed"

    # And the rename actually persisted.
    refetched = client.get(f"/api/sessions/{sid}", headers=auth_headers(USER_A))
    assert refetched.json()["title"] == "renamed"


def test_delete_owner_204_nonexistent_404_other_user_404(client, auth_headers, tmp_sessions_dir: Path):
    created = client.post(
        "/api/sessions", headers=auth_headers(USER_A), json={"title": "to-keep"}
    ).json()
    sid = created["id"]

    # Non-owner delete returns 404 and leaves the file in place.
    slug_a = storage.email_slug(USER_A)
    path = tmp_sessions_dir / slug_a / f"{sid}.json"
    assert path.exists()

    other = client.delete(f"/api/sessions/{sid}", headers=auth_headers(USER_B))
    assert other.status_code == 404
    assert path.exists(), "non-owner DELETE must not touch the file"

    # Truly-missing id also 404.
    missing = client.delete(
        "/api/sessions/00000000-0000-0000-0000-000000000000",
        headers=auth_headers(USER_A),
    )
    assert missing.status_code == 404

    # Owner delete: 204 + file gone.
    owner = client.delete(f"/api/sessions/{sid}", headers=auth_headers(USER_A))
    assert owner.status_code == 204
    assert not path.exists()


def test_round_trip_create_rename_get_list_delete(client, auth_headers):
    create = client.post(
        "/api/sessions", headers=auth_headers(USER_A), json={"title": None}
    )
    assert create.status_code == 201
    sid = create.json()["id"]
    created_at = create.json()["created_at"]
    initial_updated = create.json()["updated_at"]

    # Force a clock edge so updated_at strictly advances. (Storage also has
    # a sub-second fallback, but a real-clock test best matches production.)
    time.sleep(1.1)

    rename = client.patch(
        f"/api/sessions/{sid}",
        headers=auth_headers(USER_A),
        json={"title": "named"},
    )
    assert rename.status_code == 200
    assert rename.json()["title"] == "named"
    assert rename.json()["updated_at"] > initial_updated
    assert rename.json()["created_at"] == created_at

    got = client.get(f"/api/sessions/{sid}", headers=auth_headers(USER_A))
    assert got.status_code == 200
    assert got.json()["title"] == "named"

    listed = client.get("/api/sessions", headers=auth_headers(USER_A)).json()
    assert any(item["id"] == sid and item["title"] == "named" for item in listed)

    delete = client.delete(f"/api/sessions/{sid}", headers=auth_headers(USER_A))
    assert delete.status_code == 204

    after = client.get("/api/sessions", headers=auth_headers(USER_A)).json()
    assert all(item["id"] != sid for item in after)


def test_list_sorted_most_recent_first(client, auth_headers):
    first = client.post("/api/sessions", headers=auth_headers(USER_A),
                        json={"title": "first"}).json()
    time.sleep(1.1)
    second = client.post("/api/sessions", headers=auth_headers(USER_A),
                         json={"title": "second"}).json()

    listed = client.get("/api/sessions", headers=auth_headers(USER_A)).json()
    assert [s["id"] for s in listed[:2]] == [second["id"], first["id"]]


def test_email_slug_is_deterministic_and_filesystem_safe():
    # Direct unit check on the helper Phase 2's attachment paths will reuse.
    # '@' is outside [a-z0-9._-] so it is replaced with '_'.
    assert storage.email_slug("Alice@Example.COM") == "alice_example.com"
    # Adjacent bad chars '?' and '@' each become '_' — two underscores in a row.
    assert storage.email_slug("weird/name?@x.com") == "weird_name__x.com"
    # Idempotent: a slugged value slugged again is unchanged.
    s = storage.email_slug("alice@example.com")
    assert storage.email_slug(s) == s


# ---------------------------------------------------------------------------
# Path-traversal / malformed session id rejection.
# ---------------------------------------------------------------------------

def test_storage_rejects_path_traversal_session_ids(tmp_sessions_dir: Path):
    """Direct storage-layer check: malformed ids never reach os.path.join.

    These are the inputs Starlette could plausibly hand us as a single path
    segment captured by ``{session_id}``: the literal ``..``, a hex-like
    string with embedded slashes, and a URL-decoded traversal. All must be
    rejected as not-found *without* touching the filesystem above the user
    dir.
    """
    # Plant a file at the sessions root level to prove a traversal attempt
    # cannot reach it. (Real attackers don't get to plant files, but if our
    # path scheme ever lets them out of <slug>/, we want a loud failure.)
    sentinel = tmp_sessions_dir / "sentinel.json"
    tmp_sessions_dir.mkdir(parents=True, exist_ok=True)
    sentinel.write_text('{"email":"x","id":".."}')

    for bogus in [
        "..",
        "../sentinel",
        "../../etc/passwd",
        "..%2Fsentinel",     # pre-decoded form
        "/etc/passwd",       # absolute path
        "abc",               # too short
        "ZZZZZZZZ-ZZZZ-ZZZZ-ZZZZ-ZZZZZZZZZZZZ",  # right shape, wrong charset
        "",
    ]:
        assert storage.get_session("alice@example.com", bogus) is None
        assert storage.delete_session("alice@example.com", bogus) is False
        assert storage.rename_session("alice@example.com", bogus, "x") is None

    # Sentinel must still be present and untouched.
    assert sentinel.exists()


def test_http_path_traversal_returns_404_not_500(client, auth_headers):
    """End-to-end: a traversal-shaped path param routes to a clean 404."""
    headers = auth_headers(USER_A)
    # The literal ``..`` is the only segment Starlette will pass through
    # without normalising; encoded slashes get decoded but the path-param
    # capture still sees a single segment. Either way: 404, not 500.
    resp = client.get("/api/sessions/..", headers=headers)
    assert resp.status_code == 404
    resp = client.delete("/api/sessions/not-a-uuid", headers=headers)
    assert resp.status_code == 404
    resp = client.patch(
        "/api/sessions/not-a-uuid",
        headers=headers,
        json={"title": "x"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Same-second rename produces a real ISO-8601 timestamp.
# ---------------------------------------------------------------------------

def test_rename_in_same_second_produces_valid_iso8601(client, auth_headers):
    """Regression: ``updated_at`` must remain parseable after a fast rename.

    Phase 2 will deserialise this field with ``datetime.fromisoformat`` (and
    JS ``new Date(...)``); a malformed suffix would break both.
    """
    from datetime import datetime

    created = client.post(
        "/api/sessions", headers=auth_headers(USER_A), json={"title": "t0"}
    ).json()
    sid = created["id"]
    created_at = created["created_at"]

    # Rename immediately — the same-second branch is the one we care about.
    renamed = client.patch(
        f"/api/sessions/{sid}",
        headers=auth_headers(USER_A),
        json={"title": "t1"},
    ).json()

    updated_at = renamed["updated_at"]
    # Strictly advances...
    assert updated_at > created_at, (created_at, updated_at)
    # ...and parses cleanly. fromisoformat needs '+00:00' rather than 'Z'
    # before 3.11; we normalise here just like Phase 2 will.
    normalised = updated_at.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalised)
    assert parsed.tzinfo is not None
