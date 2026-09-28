"""Healthz is open and stable. Phase 1 acceptance: not auth-gated."""

from __future__ import annotations


def test_healthz_returns_expected_body(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "service": "chat"}


def test_healthz_does_not_require_auth(client):
    # No Cf-Access-Jwt-Assertion header at all.
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
