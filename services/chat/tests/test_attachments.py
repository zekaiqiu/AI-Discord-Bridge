"""Attachment uploads: MIME whitelist, size cap, count cap, collision, purge-on-delete."""

from __future__ import annotations

import io
import os
from pathlib import Path
from typing import Any

import attachments


USER_A = "alice@example.com"
USER_B = "bob@example.com"


def _create_session(client: Any, headers: dict[str, str]) -> str:
    resp = client.post("/api/sessions", headers=headers, json={"title": None})
    assert resp.status_code == 201
    return resp.json()["id"]


def _upload(
    client: Any,
    sid: str,
    headers: dict[str, str],
    files_payload: list[tuple[str, tuple[str, bytes, str]]],
) -> Any:
    return client.post(
        f"/api/sessions/{sid}/attachments",
        headers=headers,
        files=files_payload,
    )


# ---------------------------------------------------------------------------
# Happy path: whitelisted MIME types accepted; result has filename/size/mime.
# ---------------------------------------------------------------------------

def test_whitelisted_mime_types_accepted(client, auth_headers, tmp_attachments_dir: Path):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)

    payload = [
        ("files", ("hello.txt", b"hello world\n", "text/plain")),
        ("files", ("note.md",   b"# heading",     "text/markdown")),
        ("files", ("doc.pdf",   b"%PDF-1.4\n",    "application/pdf")),
        ("files", ("pic.png",   b"\x89PNG\r\n\x1a\n", "image/png")),
    ]
    resp = _upload(client, sid, headers, payload)
    assert resp.status_code == 200, resp.text
    saved = resp.json()
    assert len(saved) == 4
    names = {s["filename"] for s in saved}
    assert names == {"hello.txt", "note.md", "doc.pdf", "pic.png"}
    for s in saved:
        assert s["size"] > 0
        assert s["mime"]

    # Files exist on disk inside the session-scoped attachments dir.
    sess_dir = tmp_attachments_dir / sid
    on_disk = {p.name for p in sess_dir.iterdir() if p.is_file()}
    assert on_disk == names


def test_attachment_files_have_mode_0640(client, auth_headers, tmp_attachments_dir: Path):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    _upload(
        client, sid, headers,
        [("files", ("a.txt", b"x" * 32, "text/plain"))],
    )
    f = tmp_attachments_dir / sid / "a.txt"
    mode = os.stat(f).st_mode & 0o777
    assert mode == 0o640, oct(mode)


# ---------------------------------------------------------------------------
# Rejection paths.
# ---------------------------------------------------------------------------

def test_non_whitelisted_mime_rejected_415(client, auth_headers):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    resp = _upload(
        client, sid, headers,
        [("files", ("evil.exe", b"MZ\x90\x00", "application/x-msdownload"))],
    )
    assert resp.status_code == 415, resp.text


def test_oversized_file_rejected_413(client, auth_headers):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    big = b"a" * (attachments.MAX_FILE_BYTES + 1)
    resp = _upload(
        client, sid, headers,
        [("files", ("huge.txt", big, "text/plain"))],
    )
    assert resp.status_code == 413, resp.text


def test_more_than_five_files_rejected_400(client, auth_headers):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    payload = [
        ("files", (f"f{i}.txt", b"x", "text/plain"))
        for i in range(attachments.MAX_FILES_PER_TURN + 1)
    ]
    resp = _upload(client, sid, headers, payload)
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Collision behaviour: second upload of the same name gets a millis prefix.
# ---------------------------------------------------------------------------

def test_collision_prefixes_with_unix_millis(client, auth_headers, tmp_attachments_dir: Path):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    _upload(client, sid, headers, [("files", ("dup.txt", b"first", "text/plain"))])
    resp = _upload(client, sid, headers, [("files", ("dup.txt", b"second", "text/plain"))])
    assert resp.status_code == 200
    name = resp.json()[0]["filename"]
    assert name.endswith("_dup.txt")
    assert name != "dup.txt"
    # Numeric prefix should parse as an int (millis).
    prefix = name.split("_", 1)[0]
    assert prefix.isdigit() and int(prefix) > 0

    on_disk = {p.name for p in (tmp_attachments_dir / sid).iterdir()}
    assert "dup.txt" in on_disk
    assert name in on_disk


# ---------------------------------------------------------------------------
# Purge-on-delete.
# ---------------------------------------------------------------------------

def test_attachment_dir_removed_when_session_deleted(
    client, auth_headers, tmp_attachments_dir: Path,
):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    _upload(client, sid, headers, [("files", ("keep.txt", b"x", "text/plain"))])
    sess_dir = tmp_attachments_dir / sid
    assert sess_dir.is_dir()

    resp = client.delete(f"/api/sessions/{sid}", headers=headers)
    assert resp.status_code == 204
    assert not sess_dir.exists(), "attachments dir must be purged on session delete"


def test_attachment_dir_NOT_touched_on_404_delete(
    client, auth_headers, tmp_attachments_dir: Path,
):
    """If storage 404s the delete, attachments must remain — guards against
    a non-owner using DELETE to wipe another user's files."""
    headers_a = auth_headers(USER_A)
    headers_b = auth_headers(USER_B)
    sid = _create_session(client, headers_a)
    _upload(client, sid, headers_a, [("files", ("k.txt", b"x", "text/plain"))])
    sess_dir = tmp_attachments_dir / sid

    # User B attempts delete -> 404, files preserved.
    resp = client.delete(f"/api/sessions/{sid}", headers=headers_b)
    assert resp.status_code == 404
    assert sess_dir.is_dir(), "attachments must survive a foreign 404 delete"
    assert (sess_dir / "k.txt").exists()


# ---------------------------------------------------------------------------
# Cross-email upload returns 404, no dir created.
# ---------------------------------------------------------------------------

def test_cross_email_upload_404_no_dir_created(
    client, auth_headers, tmp_attachments_dir: Path,
):
    sid = _create_session(client, auth_headers(USER_A))
    resp = _upload(
        client, sid, auth_headers(USER_B),
        [("files", ("a.txt", b"x", "text/plain"))],
    )
    assert resp.status_code == 404
    # No attachments dir should have been created by the rejected request.
    assert not (tmp_attachments_dir / sid).exists()


# ---------------------------------------------------------------------------
# Attachments influence the claude argv (--add-dir present iff non-empty).
# ---------------------------------------------------------------------------

def test_run_turn_passes_add_dir_when_attachments_present(
    client, auth_headers, fake_claude, tmp_attachments_dir: Path,
):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    _upload(client, sid, headers, [("files", ("a.txt", b"x", "text/plain"))])

    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "look at the file"},
    )
    assert resp.status_code == 200
    # Drain so the call is fully recorded.
    resp.text  # noqa: B018

    # The first call (the turn) should carry an --add-dir for the
    # session's attachments dir. Prod also unconditionally `--add-dir`s
    # /home/felix to give the agent host-tree reach, so we look for the
    # session-attachments dir specifically rather than the FIRST
    # --add-dir occurrence.
    turn_call = fake_claude.calls[0]
    args = turn_call["args"]
    expected = str(tmp_attachments_dir / sid)
    assert any(a == expected for a in args), (
        f"session attachments dir {expected!r} not in args: {args}"
    )


def test_run_turn_omits_add_dir_when_no_attachments(
    client, auth_headers, fake_claude, tmp_attachments_dir,
):
    headers = auth_headers(USER_A)
    sid = _create_session(client, headers)
    resp = client.post(
        f"/api/sessions/{sid}/messages",
        headers=headers,
        json={"text": "no files"},
    )
    assert resp.status_code == 200
    resp.text  # drain

    args = fake_claude.calls[0]["args"]
    # Prod unconditionally `--add-dir`s /home/felix; the assertion this
    # test makes is the session-attachments dir specifically must be
    # absent (because no upload happened). The unconditional /home/felix
    # entry is unrelated.
    expected = str(tmp_attachments_dir / sid)
    assert not any(a == expected for a in args), (
        f"session attachments dir {expected!r} unexpectedly in args: {args}"
    )
