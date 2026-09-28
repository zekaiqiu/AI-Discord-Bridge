"""Artifacts with non-ASCII / spaced names must be served, and their footer
links must be percent-encoded (2026-09-28: the serve route was ASCII-only, so
every Chinese-titled PDF the model produced was listed in the footer but
404'd on click — 24 such dead links in the live store).
"""
from __future__ import annotations

import time
import urllib.parse
from pathlib import Path

import pytest

import app as app_module
from helpers import create_session

USER = "artnames@example.com"
CJK = "公司转让协议_修订版 买方版.pdf"


@pytest.fixture
def gen_root(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "generated"
    monkeypatch.setenv("CHAT_GENERATED_DIR", str(root))
    return root


def test_scan_encodes_link_and_route_serves_cjk_name(client, auth_headers, gen_root):
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    d = gen_root / sid
    d.mkdir(parents=True)
    (d / CJK).write_bytes(b"%PDF-1.4 fake")
    arts = app_module._scan_new_artifacts(session_id=sid, since_ts=time.time() - 60)
    assert [a["filename"] for a in arts] == [CJK]
    url = arts[0]["url"]
    assert url == f"/api/sessions/{sid}/generated/" + urllib.parse.quote(CJK, safe="")
    assert " " not in url and "(" not in url
    md = app_module._format_artifacts_markdown(arts)
    assert f"- [{CJK}]({url})" in md

    r = client.get(url, headers=headers)
    assert r.status_code == 200, r.text
    assert r.content == b"%PDF-1.4 fake"
    assert r.headers["content-type"].startswith("application/pdf")


def test_route_still_blocks_traversal_and_hidden(client, auth_headers, gen_root):
    headers = auth_headers(USER)
    sid = create_session(client, headers)
    d = gen_root / sid
    d.mkdir(parents=True)
    (d / ".secret").write_bytes(b"x")
    (gen_root / "outside.txt").write_bytes(b"y")
    for bad in (".secret", "..%2Foutside.txt", "%2E%2E/outside.txt", "a%00b"):
        r = client.get(f"/api/sessions/{sid}/generated/{bad}", headers=headers)
        assert r.status_code == 404, bad
    assert not app_module._GENERATED_FILENAME_RE.match("a/b")
    assert not app_module._GENERATED_FILENAME_RE.match("a\\b")
    assert not app_module._GENERATED_FILENAME_RE.match("x" * 201)
