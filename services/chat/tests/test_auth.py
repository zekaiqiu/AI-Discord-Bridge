"""Verification rules around the Cf-Access JWT.

Each negative test exercises one and only one verification rule so a
regression points at a specific check in ``auth.verify_jwt``.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def _alt_private_pem() -> str:
    """A second RSA key the JWKS does NOT advertise. Used for wrong-signer tests."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def test_me_requires_header(client):
    resp = client.get("/api/me")
    assert resp.status_code == 401


def test_me_rejects_token_signed_by_wrong_key(client, mint_jwt):
    # Same kid the JWKS advertises, but signed by an unrelated key.
    bad_token = mint_jwt(key_pem=_alt_private_pem())
    resp = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": bad_token})
    assert resp.status_code == 401


def test_me_rejects_wrong_audience(client, mint_jwt):
    token = mint_jwt(aud="not-our-aud")
    resp = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 401


def test_me_rejects_expired_token(client, mint_jwt):
    token = mint_jwt(exp_offset=-60)  # expired 60s ago
    resp = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 401


def test_me_rejects_wrong_issuer(client, mint_jwt):
    token = mint_jwt(iss="https://evil.cloudflareaccess.com")
    resp = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 401


def test_me_rejects_unknown_kid(client, mint_jwt):
    token = mint_jwt(kid="nonexistent-kid")
    resp = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 401


def test_me_accepts_valid_token(client, auth_headers):
    resp = client.get("/api/me", headers=auth_headers("alice@example.com"))
    assert resp.status_code == 200
    # Phase 1 (role) added a ``role`` field; alice is not felix and there
    # is no sandbox_users.json on disk, so role resolves to "user".
    assert resp.json() == {"email": "alice@example.com", "role": "user"}


def test_me_rejects_token_missing_email_claim(client, mint_jwt):
    # Email is not enforced by jose's required-claims set; ``require_user``
    # checks it explicitly. Encode without it by passing email=""... we
    # need to bypass the default. Use extra_claims to wipe.
    token = mint_jwt(extra_claims={"email": ""})
    resp = client.get("/api/me", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 401
