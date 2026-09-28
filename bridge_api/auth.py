"""Cloudflare Access JWT verification for the bridge ops API.

Copied — not imported — from services/chat/auth.py per the brief. Keeping
this self-contained avoids a Python import dependency on the chat service
and means the bridge can ship independently.

Verification rules:
  * signature validates against a key in the cached JWKS (RS256)
  * ``aud`` equals ``CF_ACCESS_AUD``
  * ``iss`` equals ``https://<CF_ACCESS_TEAM_SUBDOMAIN>.cloudflareaccess.com``
  * ``exp`` is in the future (with 30s leeway)

Dev bypass: when ``BRIDGE_API_DEV_BYPASS=1``, the dependency skips JWT
verification entirely and reads the email from ``X-Dev-Email`` instead.
That email STILL has to be in ``ADMIN_EMAILS`` — bypass turns off CF, not
authorization.
"""

from __future__ import annotations

import json as _json
import os
import urllib.request as _urllib_request
from typing import Any, Callable, Optional

import jwt as _pyjwt
from fastapi import HTTPException, Request, status

# Module-import guard: refuse to load if dev bypass is enabled outside dev env.
# Defense in depth — there is also a request-time check in require_admin().
_DEV_BYPASS_ON = os.environ.get("BRIDGE_API_DEV_BYPASS", "").strip().lower() in ("1", "true", "yes")
_ENV = os.environ.get("BRIDGE_API_ENV", "").strip().lower()
if _DEV_BYPASS_ON and _ENV != "dev":
    raise RuntimeError(
        "BRIDGE_API_DEV_BYPASS is set but BRIDGE_API_ENV != 'dev'. "
        "Refusing to start: dev bypass disables JWT verification entirely. "
        "If this is intentional, also set BRIDGE_API_ENV=dev."
    )

# Tunables.
ADMIN_EMAILS_ENV = "ADMIN_EMAILS"
JWT_HEADER_NAME = "Cf-Access-Jwt-Assertion"
DEV_EMAIL_HEADER = "X-Dev-Email"
DEV_BYPASS_ENV = "BRIDGE_API_DEV_BYPASS"
CF_ACCESS_AUD_ENV = "CF_ACCESS_AUD"
CF_ACCESS_TEAM_ENV = "CF_ACCESS_TEAM"
CF_ACCESS_TEAM_SUBDOMAIN_ENV = "CF_ACCESS_TEAM_SUBDOMAIN"
CF_ACCESS_CERTS_URL_TEMPLATE = "https://{team}.cloudflareaccess.com/cdn-cgi/access/certs"
_JWT_LEEWAY_SECONDS = 30


class JWTVerificationError(Exception):
    """Raised when a CF Access JWT fails any verification step."""


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _admin_set_from_env() -> set[str]:
    raw = os.environ.get(ADMIN_EMAILS_ENV, "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _required_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise JWTVerificationError(f"environment variable {name} is not set")
    return val


def _default_certs_fetcher(team: str) -> dict:
    url = CF_ACCESS_CERTS_URL_TEMPLATE.format(team=team)
    try:
        with _urllib_request.urlopen(url, timeout=10) as resp:  # noqa: S310
            payload = resp.read()
    except Exception as exc:
        raise JWTVerificationError(f"failed to fetch JWKS from {url}: {exc}") from exc
    try:
        return _json.loads(payload)
    except _json.JSONDecodeError as exc:
        raise JWTVerificationError(f"JWKS at {url} was not valid JSON: {exc}") from exc


def verify_cf_access_jwt(
    token: str,
    *,
    certs_fetcher: Optional[Callable[[str], dict]] = None,
    now: Optional[Callable[[], float]] = None,
) -> dict:
    """Verify a Cloudflare Access JWT and return its claims dict.

    Reads ``CF_ACCESS_AUD`` (audience tag) and ``CF_ACCESS_TEAM`` /
    ``CF_ACCESS_TEAM_SUBDOMAIN`` (team domain) from the environment. Both
    must be set; raises ``JWTVerificationError`` otherwise.
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
            f"environment variable {CF_ACCESS_TEAM_ENV} (or "
            f"{CF_ACCESS_TEAM_SUBDOMAIN_ENV}) is not set"
        )
    expected_iss = f"https://{team}.cloudflareaccess.com"

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
        keyset = _pyjwt.PyJWKSet.from_dict(jwks)
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
        raise JWTVerificationError(f"JWT verification failed: {exc}") from exc

    if now is not None:
        now_ts = float(now())
        exp = claims.get("exp")
        if exp is not None and now_ts > float(exp) + _JWT_LEEWAY_SECONDS:
            raise JWTVerificationError("JWT is expired")

    return claims


def email_from_claims(claims: dict) -> str:
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


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------

async def require_admin(request: Request) -> str:
    """FastAPI dependency: verify CF Access JWT and confirm email is admin.

    Returns the verified, normalized email. Raises 401 (unauthenticated) or
    403 (authenticated but not admin).

    Dev bypass: when ``BRIDGE_API_DEV_BYPASS=1``, accept the email in the
    ``X-Dev-Email`` header without verifying a JWT. The email still has to be
    in ``ADMIN_EMAILS`` — bypass turns off the CF verification step, not the
    authorization gate.
    """
    admin_set = _admin_set_from_env()
    bypass = os.environ.get(DEV_BYPASS_ENV, "").strip().lower() in ("1", "true", "yes")

    if bypass:
        raw_email = request.headers.get(DEV_EMAIL_HEADER, "")
        if not raw_email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"missing {DEV_EMAIL_HEADER} (dev bypass)",
            )
        email = _normalize_email(raw_email)
        if email not in admin_set:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="email not in ADMIN_EMAILS",
            )
        request.state.user_email = email
        return email

    token = request.headers.get(JWT_HEADER_NAME)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing {JWT_HEADER_NAME}",
        )
    try:
        claims = verify_cf_access_jwt(token)
        email = email_from_claims(claims)
    except JWTVerificationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"invalid token: {exc}",
        ) from exc

    if email not in admin_set:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email not in ADMIN_EMAILS",
        )
    request.state.user_email = email
    return email


__all__ = [
    "ADMIN_EMAILS_ENV",
    "DEV_BYPASS_ENV",
    "DEV_EMAIL_HEADER",
    "JWT_HEADER_NAME",
    "JWTVerificationError",
    "email_from_claims",
    "require_admin",
    "verify_cf_access_jwt",
]
