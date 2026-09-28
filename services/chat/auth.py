"""Cloudflare Access JWT verification.

Cloudflare Access stamps every authenticated request with the
``Cf-Access-Jwt-Assertion`` header. This module verifies that header against
the team's JWKS endpoint and exposes a FastAPI dependency.

Verification rules (each enforced by ``verify_jwt``):
  * signature validates against a key in the cached JWKS (RS256)
  * ``aud`` equals ``CF_ACCESS_AUD``
  * ``iss`` equals ``https://<CF_ACCESS_TEAM>.cloudflareaccess.com``
  * ``exp`` is in the future

Design notes for downstream phases:
  * Env is read through ``_team()`` / ``_aud()`` / ``_jwks_url()`` getters that
    re-evaluate ``os.environ`` on every call. Tests can therefore set env vars
    in a fixture and the production app stays import-safe with no env set.
  * The JWKS fetcher is exposed as a module-level callable
    (``_fetch_jwks_json``) so ``conftest.py`` can monkeypatch a single seam
    instead of poking cache internals.
  * Phase 2 will reuse ``require_user`` unchanged on the SSE / message-send
    endpoints, so the dependency must always raise — never return None.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx
from fastapi import HTTPException, Request, status
from jose import jwt
from jose.exceptions import JWTError, ExpiredSignatureError, JWTClaimsError

# 5-minute TTL satisfies the brief's "≥ 5 minutes" floor while still picking up
# real-world Cloudflare key rotations within a sensible window.
_JWKS_TTL_SECONDS = 300


class AuthError(Exception):
    """Raised by ``verify_jwt`` for any verification failure.

    The FastAPI dependency converts this into a generic 401 so we never leak
    the underlying jose error to clients.
    """


# ---------------------------------------------------------------------------
# Env-driven configuration. Wrapped in functions so importing this module has
# zero side effects and so tests can flip env vars per-test.
# ---------------------------------------------------------------------------

def _team() -> str:
    # CF_ACCESS_TEAM is the brief's chosen name; CF_ACCESS_TEAM_SUBDOMAIN
    # is the existing convention shared with services/portfolio (which
    # already has it set in the live .env). Accept either; prefer the
    # brief's name when both are present so an explicit override wins.
    team = os.environ.get("CF_ACCESS_TEAM", "").strip()
    if not team:
        team = os.environ.get("CF_ACCESS_TEAM_SUBDOMAIN", "").strip()
    if not team:
        raise AuthError("CF_ACCESS_TEAM (or CF_ACCESS_TEAM_SUBDOMAIN) not configured")
    return team


def _aud() -> str:
    aud = os.environ.get("CF_ACCESS_AUD", "").strip()
    if not aud:
        raise AuthError("CF_ACCESS_AUD not configured")
    return aud


def _issuer() -> str:
    return f"https://{_team()}.cloudflareaccess.com"


def _jwks_url() -> str:
    return f"https://{_team()}.cloudflareaccess.com/cdn-cgi/access/certs"


# ---------------------------------------------------------------------------
# JWKS fetch + cache. The fetcher is a module-level reference so tests can
# monkeypatch ``auth._fetch_jwks_json`` cleanly.
# ---------------------------------------------------------------------------

def _fetch_jwks_json(url: str) -> dict[str, Any]:
    """Network fetch of a JWKS document. Replaced wholesale in tests."""
    resp = httpx.get(url, timeout=5.0)
    resp.raise_for_status()
    return resp.json()


_jwks_cache: dict[str, Any] = {
    "url": None,   # cached for the URL we last fetched against
    "data": None,  # the parsed JWKS dict
    "at": 0.0,     # monotonic timestamp
}


def _load_jwks(*, force_refresh: bool = False) -> dict[str, Any]:
    url = _jwks_url()
    now = time.monotonic()
    cache_is_valid = (
        not force_refresh
        and _jwks_cache["data"] is not None
        and _jwks_cache["url"] == url
        and (now - _jwks_cache["at"]) < _JWKS_TTL_SECONDS
    )
    if cache_is_valid:
        return _jwks_cache["data"]  # type: ignore[return-value]

    data = _fetch_jwks_json(url)
    _jwks_cache["url"] = url
    _jwks_cache["data"] = data
    _jwks_cache["at"] = now
    return data


def _reset_jwks_cache() -> None:
    """Test helper. Drops cached JWKS so the next verify call re-fetches."""
    _jwks_cache["url"] = None
    _jwks_cache["data"] = None
    _jwks_cache["at"] = 0.0


def _find_key(jwks: dict[str, Any], kid: str | None) -> dict[str, Any] | None:
    if not kid:
        return None
    for k in jwks.get("keys", []) or []:
        if k.get("kid") == kid:
            return k
    return None


# ---------------------------------------------------------------------------
# Public surface.
# ---------------------------------------------------------------------------

def verify_jwt(token: str) -> dict[str, Any]:
    """Verify a Cloudflare Access JWT and return its claims.

    Raises ``AuthError`` on any failure — bad signature, missing/wrong aud,
    expired, wrong issuer, malformed token, or unknown signing key (after one
    refresh attempt).
    """
    if not token or not isinstance(token, str):
        raise AuthError("missing token")

    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise AuthError("malformed token header") from exc

    kid = unverified_header.get("kid")

    try:
        jwks = _load_jwks()
    except AuthError:
        raise
    except Exception as exc:  # network/HTTP error reaching JWKS
        raise AuthError("jwks unavailable") from exc

    key = _find_key(jwks, kid)
    if key is None:
        # Possible key rotation race: drop the cache and try once more.
        try:
            jwks = _load_jwks(force_refresh=True)
        except Exception as exc:
            raise AuthError("jwks unavailable") from exc
        key = _find_key(jwks, kid)
    if key is None:
        raise AuthError("unknown signing key")

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=_aud(),
            issuer=_issuer(),
            options={"require": ["exp", "iss", "aud"]},
        )
    except ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except JWTClaimsError as exc:
        # Wrong aud, wrong iss, missing required claim.
        raise AuthError("invalid claims") from exc
    except JWTError as exc:
        raise AuthError("invalid token") from exc

    return claims


async def require_user(request: Request) -> dict[str, Any]:
    """FastAPI dependency: verify the inbound JWT and return its claims.

    The returned dict is guaranteed to contain a non-empty ``email``. Any
    failure — missing header, bad signature, expired, missing email — raises
    a generic 401.
    """
    token = request.headers.get("Cf-Access-Jwt-Assertion")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing Cf-Access-Jwt-Assertion",
        )
    try:
        claims = verify_jwt(token)
    except AuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid token",
        )

    email = claims.get("email")
    if not email or not isinstance(email, str):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token missing email claim",
        )
    # Stash for downstream phases (SSE handler will want it without re-parsing).
    request.state.user_email = email
    return claims


# ---------------------------------------------------------------------------
# Multi-user (per-user-container) extensions.
#
# These are NEW, additive surface added in the multi-user-containers branch.
# They live here — not in services/term-router — so the term-router service
# can import a single source of truth for CF Access JWT verification + role
# resolution (see services/term-router/app.py). The names below are:
#
#   - resolve_role(email) -> "admin" | "user"
#       Reads ADMIN_EMAILS env (comma-separated, case-insensitive). Used by
#       the new run_turn / term-router code paths. Distinct from app.py's
#       _resolve_role, which folds in DEFAULT_ADMIN_EMAILS + sandbox file
#       overrides — that lives at the app boundary because /api/me returns
#       it; this lighter helper lives at the auth boundary because it is
#       what the per-turn dispatcher and the term-router need.
#   - verify_cf_access_jwt(token, *, certs_fetcher=None, now=None) -> dict
#       Same job as verify_jwt above, implemented against PyJWT (rather than
#       python-jose) because (a) the term-router test suite was written
#       against PyJWT's JWKSet API and (b) keeps the new code on a single
#       JWT lib so we don't have two parallel JWKS caches. The original
#       verify_jwt + require_user FastAPI dependency remain unchanged so
#       every existing chat endpoint keeps working byte-for-byte.
#   - email_from_claims(claims) -> str (normalized)
#       Pulls 'email' (or identity_nonce.email fallback per CF docs) and
#       returns the normalized form. Term-router calls this immediately
#       after verify_cf_access_jwt so the WS handler has one bare string
#       to feed resolve_role / ensure_user_container.
#   - JWTVerificationError
#       Single error class for the new verifier path. Distinct from
#       AuthError above because the new code paths surface a different
#       set of failure modes (env-not-set, kid-not-in-JWKS, etc.) and
#       conflating them would muddle the term-router error handling.
#
# Email normalization rule is identical to user_container.container_name_for:
# `.strip().lower()`. Keeping that rule in lockstep across the two files is
# load-bearing — they hash the same string into the same container name.
# ---------------------------------------------------------------------------

import json as _json
import urllib.request as _urllib_request
from typing import Callable as _Callable, Optional as _Optional

import jwt as _pyjwt
from jwt import PyJWKSet as _PyJWKSet


ADMIN_EMAILS_ENV = "ADMIN_EMAILS"
JWT_HEADER_NAME = "Cf-Access-Jwt-Assertion"
CF_ACCESS_AUD_ENV = "CF_ACCESS_AUD"
CF_ACCESS_TEAM_ENV = "CF_ACCESS_TEAM"
# Legacy alias used by chat's docker-compose env passthrough; the legacy
# verify_jwt path reads either name. Mirror that here so deployments that
# only set CF_ACCESS_TEAM_SUBDOMAIN keep working with the new verifier.
CF_ACCESS_TEAM_SUBDOMAIN_ENV = "CF_ACCESS_TEAM_SUBDOMAIN"
CF_ACCESS_CERTS_URL_TEMPLATE = "https://{team}.cloudflareaccess.com/cdn-cgi/access/certs"

# Small leeway for clock skew between this host and Cloudflare's edge.
_JWT_LEEWAY_SECONDS = 30


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _admin_set_from_env() -> set[str]:
    """Parse the ADMIN_EMAILS env var into a normalized set.

    Comma-separated; case-insensitive; whitespace around each entry stripped.
    Empty / unset env -> empty set (so every email resolves to "user").

    Re-evaluated on every call (no caching) so monkeypatch'd env vars in
    tests, and live env changes in long-running processes, take effect
    without a module reload.
    """
    raw = os.environ.get(ADMIN_EMAILS_ENV, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def resolve_role(email: str) -> str:
    """Return "admin" if `email` is in ADMIN_EMAILS, else "user".

    NOTE: this is the auth-layer helper used by the per-turn dispatcher
    (services.chat.run_turn) and term-router. It is intentionally lighter
    than app._resolve_role, which also consults the sandbox_users.json
    file and DEFAULT_ADMIN_EMAILS — that fuller resolver belongs at the
    app boundary because /api/me returns its result.
    """
    return "admin" if _normalize_email(email) in _admin_set_from_env() else "user"


class JWTVerificationError(Exception):
    """Raised when a CF Access JWT fails any verification step.

    The message is intended to be safe to log; it does not include the raw
    token. Distinct from AuthError above because the new term-router /
    run_turn surfaces emit a different set of failure modes (missing env,
    kid-not-in-JWKS, identity_nonce fallback) and conflating the two would
    muddle the term-router error handling.
    """


def _default_certs_fetcher(team: str) -> dict:
    """Fetch the JWKS for `team` via stdlib urllib (no `requests` dep).

    Network errors are wrapped in JWTVerificationError so callers see a
    single error type regardless of failure mode.
    """
    url = CF_ACCESS_CERTS_URL_TEMPLATE.format(team=team)
    try:
        with _urllib_request.urlopen(url, timeout=10) as resp:  # noqa: S310 — URL built from operator-controlled CF_ACCESS_TEAM env var, not user input
            payload = resp.read()
    except Exception as exc:  # network / DNS / timeout
        raise JWTVerificationError(f"failed to fetch JWKS from {url}: {exc}") from exc
    try:
        return _json.loads(payload)
    except _json.JSONDecodeError as exc:
        raise JWTVerificationError(f"JWKS at {url} was not valid JSON: {exc}") from exc


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise JWTVerificationError(f"environment variable {name} is not set")
    return val


def verify_cf_access_jwt(
    token: str,
    *,
    certs_fetcher: _Optional[_Callable[[str], dict]] = None,
    now: _Optional[_Callable[[], float]] = None,
) -> dict:
    """Verify a Cloudflare Access JWT and return its claims dict.

    - Reads CF_ACCESS_AUD_ENV (audience tag) and CF_ACCESS_TEAM_ENV (team domain)
      from the environment. Both must be set; raises JWTVerificationError otherwise.
    - certs_fetcher: optional callable(team)->JWKS dict; injected for tests.
      If None, fetches from CF_ACCESS_CERTS_URL_TEMPLATE.format(team=<team>).
    - now: optional callable returning current epoch seconds (for test exp/nbf control).
    - Validates: signature against JWKS kid, `aud` matches env, `iss` matches team,
      `exp` not past, `nbf`/`iat` not future (with small leeway).
    - Returns the decoded claims dict on success.
    - Raises JWTVerificationError with a human-readable reason on any failure.
    """
    if not token or not isinstance(token, str):
        raise JWTVerificationError("token is empty or not a string")

    aud = _required_env(CF_ACCESS_AUD_ENV)
    team = (
        os.environ.get(CF_ACCESS_TEAM_ENV, "").strip()
        or os.environ.get(CF_ACCESS_TEAM_SUBDOMAIN_ENV, "").strip()
    )
    if not team:
        raise JWTVerificationError(
            f"environment variable {CF_ACCESS_TEAM_ENV} (or {CF_ACCESS_TEAM_SUBDOMAIN_ENV}) is not set"
        )
    expected_iss = f"https://{team}.cloudflareaccess.com"

    # Pick the right key from the JWKS using the `kid` in the JWT header.
    try:
        unverified_header = _pyjwt.get_unverified_header(token)
    except _pyjwt.InvalidTokenError as exc:
        raise JWTVerificationError(f"could not parse JWT header: {exc}") from exc
    kid = unverified_header.get("kid")
    if not kid:
        raise JWTVerificationError("JWT header is missing 'kid'")

    fetcher = certs_fetcher or _default_certs_fetcher
    jwks = fetcher(team)
    if not isinstance(jwks, dict) or "keys" not in jwks:
        raise JWTVerificationError("JWKS payload missing 'keys' array")

    try:
        keyset = _PyJWKSet.from_dict(jwks)
    except Exception as exc:
        raise JWTVerificationError(f"could not parse JWKS: {exc}") from exc

    matching = next((k for k in keyset.keys if k.key_id == kid), None)
    if matching is None:
        raise JWTVerificationError(f"no JWKS key matched kid={kid!r}")

    try:
        claims = _pyjwt.decode(
            token,
            key=matching.key,
            algorithms=["RS256"],
            audience=aud,
            issuer=expected_iss,
            leeway=_JWT_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "aud"]},
        )
    except _pyjwt.InvalidTokenError as exc:
        # Covers signature, aud, iss, exp/iat enforcement built into pyjwt.
        raise JWTVerificationError(f"JWT verification failed: {exc}") from exc

    # Manual nbf/exp re-check against an injected `now` so tests can pin the
    # clock without monkeypatching PyJWT internals.
    if now is not None:
        now_ts = float(now())
        exp = claims.get("exp")
        nbf = claims.get("nbf")
        iat = claims.get("iat")
        if exp is not None and now_ts > float(exp) + _JWT_LEEWAY_SECONDS:
            raise JWTVerificationError("JWT is expired")
        if nbf is not None and now_ts + _JWT_LEEWAY_SECONDS < float(nbf):
            raise JWTVerificationError("JWT not yet valid (nbf)")
        if iat is not None and now_ts + _JWT_LEEWAY_SECONDS < float(iat):
            raise JWTVerificationError("JWT iat is in the future")

    return claims


def email_from_claims(claims: dict) -> str:
    """Extract the verified email from a CF Access claims dict.

    Looks for 'email' first, then 'identity_nonce.email' fallback per CF docs.
    Returns the normalized (lowercased, stripped) email.
    Raises JWTVerificationError if no email claim is present.
    """
    if not isinstance(claims, dict):
        raise JWTVerificationError("claims is not a dict")

    email = claims.get("email")
    if not email:
        nonce = claims.get("identity_nonce")
        if isinstance(nonce, dict):
            email = nonce.get("email")
    if not email or not isinstance(email, str):
        raise JWTVerificationError("no 'email' claim present")
    return _normalize_email(email)


__all__ = [
    "ADMIN_EMAILS_ENV",
    "AuthError",
    "CF_ACCESS_AUD_ENV",
    "CF_ACCESS_CERTS_URL_TEMPLATE",
    "CF_ACCESS_TEAM_ENV",
    "JWT_HEADER_NAME",
    "JWTVerificationError",
    "email_from_claims",
    "require_user",
    "resolve_role",
    "verify_cf_access_jwt",
    "verify_jwt",
]
