"""FastAPI routes for the local (email+password) auth flow.

Mounted on the chat app only when ``AUTH_MODE=local``. See ``local_auth.py``
for the storage + JWT internals.

OPEN registration (auth.ald3.com is a public sign-in surface for the bet /
market products). ANY email can:

  POST /api/auth/request-password-reset (email)         -> 200; emails a code
  POST /api/auth/reset-password (email, code, new_pw)   -> 200 + JWT cookie
  POST /api/auth/login (email, password)                -> 200 + JWT cookie
  POST /api/auth/logout                                  -> 200; clears cookie
  GET  /api/auth/me                                      -> 200 user claims

The allowlist (``ALLOWED_EMAILS``) is still enforced — but only inside
``verify_jwt_token``, i.e. on chat / dev access. Non-allowlisted users get
a perfectly valid cookie (works on bet / market, which auto-provision a
user row from the JWT email on first hit) but hit 403 on chat / dev,
where the SPA renders the "Permission required" card.
"""

from __future__ import annotations

import logging
import time

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr

import local_auth
import storage

logger = logging.getLogger("chat.local_auth.routes")

router = APIRouter(prefix="/api/auth", tags=["auth"])


class EmailBody(BaseModel):
    email: EmailStr


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class ResetBody(BaseModel):
    email: EmailStr
    code: str
    new_password: str


class CodeLoginBody(BaseModel):
    email: EmailStr
    code: str


class LanguageBody(BaseModel):
    # Site-wide display language. Validated against storage._VALID_LANGUAGES
    # on write; anything else is rejected with 400.
    language: str


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    _passwordless_only()
    email = local_auth._normalize_email(body.email)
    ip = local_auth.client_ip(request)
    # fix #1: gate before doing any work, so neither password guessing per
    # account nor spray across accounts from one IP can run unbounded.
    local_auth._rate_guard(f"loginfail:{email}", local_auth.LOGIN_MAX_FAILS)
    local_auth._rate_guard(f"loginip:{ip}", local_auth.LOGIN_IP_MAX_FAILS)
    if not local_auth.user_exists(body.email) or not local_auth.verify_password(body.email, body.password):
        local_auth._rate_incr(f"loginfail:{email}", local_auth.LOGIN_FAIL_WINDOW)
        local_auth._rate_incr(f"loginip:{ip}", local_auth.LOGIN_IP_WINDOW)
        raise HTTPException(status_code=401, detail="invalid credentials")
    local_auth._rate_reset(f"loginfail:{email}")  # clear on success
    local_auth.stamp_login(body.email)
    token = local_auth.issue_jwt(body.email)
    local_auth.set_auth_cookie(response, token)
    local_auth.record_session(token, body.email, ip, request.headers.get("user-agent", ""))
    return {"email": email, "token": token}


def _passwordless_only() -> None:
    """The wizerith tenant is passwordless (email code → cookie). Its password
    endpoints were still mounted and, unlike /request-code, ungated: any
    mailbox could mint a valid SSO cookie via reset-password and pass the
    Caddy forward_auth on every gated host. Off in passwordless mode."""
    if local_auth._passwordless():
        raise HTTPException(status_code=404, detail="not found")


def _allowlist_gate(email: str) -> None:
    """Explicit ALLOWED_EMAILS membership OR domain match; pass-through when
    no domain list is configured (open-registration tenants)."""
    if email in local_auth._allowed_emails():
        return
    domains = local_auth._allowed_email_domains()
    if not domains:
        return
    domain = email.split("@", 1)[1] if "@" in email else ""
    if domain not in domains:
        raise HTTPException(
            status_code=403,
            detail=f"sign-in restricted to: {', '.join(sorted('@' + d for d in domains))}",
        )


@router.post("/request-password-reset")
async def request_password_reset(body: EmailBody, request: Request):
    _passwordless_only()
    # Open registration: create the user row on first sight so any new
    # email can complete the sign-up flow. The allowlist gate stays in
    # verify_jwt_token (chat / dev 403), it just doesn't gate sign-up.
    #
    # fix #1: cap sends per email and per source IP so this unauthenticated
    # endpoint can't be turned into a Resend-backed email bomb / cost sink.
    email = local_auth._normalize_email(body.email)
    _allowlist_gate(email)
    ip = local_auth.client_ip(request)
    local_auth._rate_limit(f"resetemail:{email}", local_auth.RESET_EMAIL_MAX, local_auth.RESET_EMAIL_WINDOW)
    local_auth._rate_limit(f"resetip:{ip}", local_auth.RESET_IP_MAX, local_auth.RESET_IP_WINDOW)
    local_auth.ensure_user(body.email)
    try:
        code = local_auth.issue_code(body.email, purpose="reset")
        await local_auth._send_code_email(body.email, code, purpose="reset")
        logger.info("verification code sent to %s", body.email)
    except Exception:
        logger.exception("failed to send verification code")
        raise HTTPException(status_code=500, detail="email send failed")
    return {"message": "a 6-digit code has been sent"}


@router.post("/reset-password")
async def reset_password(body: ResetBody, request: Request, response: Response):
    _passwordless_only()
    _allowlist_gate(local_auth._normalize_email(body.email))
    if not local_auth.user_exists(body.email):
        # Same message as a bad code: don't confirm which emails exist.
        raise HTTPException(status_code=400, detail="invalid or expired code")
    if not local_auth.consume_code(body.email, body.code, purpose="reset"):
        raise HTTPException(status_code=400, detail="invalid or expired code")
    local_auth.set_password(body.email, body.new_password)
    # fix #3: a password reset means the old credential is gone — kill every
    # token minted under it before issuing the new session, so a previously
    # stolen cookie can't survive the reset.
    local_auth.revoke_all_sessions(body.email)
    local_auth.stamp_login(body.email)
    token = local_auth.issue_jwt(body.email)
    local_auth.set_auth_cookie(response, token)
    local_auth.record_session(
        token, body.email, local_auth.client_ip(request), request.headers.get("user-agent", "")
    )
    return {"email": local_auth._normalize_email(body.email), "token": token}


# ---------------------------------------------------------------------------
# Passwordless flow (wizerith tenant): email -> 6-digit code -> session cookie.
# No password set step. ALLOWED_EMAIL_DOMAINS gates the email at the request-
# code stage so non-company addresses can't even trigger a code-send, which
# prevents Resend-cost abuse and signals "this isn't your auth surface" early.
# ald3 hosts can use the password endpoints above; both flows coexist on the
# same backend codebase, gated only by which endpoints the frontend calls.
# ---------------------------------------------------------------------------


@router.post("/request-code")
async def request_code(body: EmailBody, request: Request):
    email = local_auth._normalize_email(body.email)
    # Access gate: explicit ALLOWED_EMAILS membership OR domain match.
    # Explicit emails (e.g. a gmail collaborator) bypass the domain
    # restriction so they aren't blocked by ALLOWED_EMAIL_DOMAINS. If
    # ALLOWED_EMAIL_DOMAINS isn't set the gate passes through entirely
    # (ald3 keeps open-registration semantics); on wizerith with
    # ALLOWED_EMAIL_DOMAINS=wizerith.com a stranger trying to email-bomb
    # the endpoint gets a clean 403 with no Resend send.
    if email not in local_auth._allowed_emails():
        domains = local_auth._allowed_email_domains()
        if domains:
            domain = email.split("@", 1)[1] if "@" in email else ""
            if domain not in domains:
                raise HTTPException(
                    status_code=403,
                    detail=f"sign-in restricted to: {', '.join(sorted('@' + d for d in domains))}",
                )
    ip = local_auth.client_ip(request)
    local_auth._rate_limit(f"resetemail:{email}", local_auth.RESET_EMAIL_MAX, local_auth.RESET_EMAIL_WINDOW)
    local_auth._rate_limit(f"resetip:{ip}", local_auth.RESET_IP_MAX, local_auth.RESET_IP_WINDOW)
    # Auto-create on first sight — domain has already been validated, so
    # we don't need an admin to pre-seed every new hire.
    local_auth.ensure_user(body.email)
    try:
        code = local_auth.issue_code(body.email, purpose="login")
        await local_auth._send_code_email(body.email, code, purpose="login")
        logger.info("login code sent to %s", body.email)
    except Exception:
        logger.exception("failed to send login code")
        raise HTTPException(status_code=500, detail="email send failed")
    return {"message": "a 6-digit code has been sent"}


@router.post("/login-with-code")
async def login_with_code(body: CodeLoginBody, request: Request, response: Response):
    # User row was created at request-code time. If it isn't here now,
    # something is off — could be a deleted account post-issuance. The
    # 400 is shared with the bad-code case to avoid leaking which.
    if not local_auth.user_exists(body.email):
        raise HTTPException(status_code=400, detail="invalid or expired code")
    was_unlocked = local_auth.is_unlocked(body.email)
    if not local_auth.consume_code(body.email, body.code, purpose="login"):
        raise HTTPException(status_code=400, detail="invalid or expired code")
    # Emergency unlock is single-use: the moment the fixed break-glass code is
    # redeemed for a login, revert the account to normal random codes so a
    # second sign-in with 000000 is impossible without a fresh !unlock.
    if was_unlocked:
        local_auth.lock_email(body.email)
        logger.info("emergency unlock for %s redeemed — auto-relocked", local_auth._normalize_email(body.email))
    local_auth.stamp_login(body.email)
    token = local_auth.issue_jwt(body.email)
    local_auth.set_auth_cookie(response, token)
    local_auth.record_session(
        token, body.email, local_auth.client_ip(request), request.headers.get("user-agent", "")
    )
    return {"email": local_auth._normalize_email(body.email), "token": token}


@router.post("/logout")
async def logout(request: Request, response: Response):
    # Drop the cookie AND revoke the token server-side, so a copied cookie
    # does not stay valid for the rest of its 7-day TTL after logout.
    try:
        token = local_auth._extract_token(request)
        claims = pyjwt.decode(token, options={"verify_signature": False})
        jti = claims.get("jti")
        if jti:
            local_auth.terminate_session(str(jti))
    except Exception:
        pass
    local_auth.clear_auth_cookie(response)
    return {"message": "logged out"}


@router.post("/logout-all")
async def logout_all(response: Response, claims: dict = Depends(local_auth.require_local_user_open)):
    # fix #3: server-side kill switch — bumps the user's token_version so EVERY
    # outstanding cookie for this identity (this browser and any other) is
    # rejected on next use. Use after suspected token theft.
    version = local_auth.revoke_all_sessions(claims["email"])
    local_auth.clear_auth_cookie(response)
    return {"message": "all sessions revoked", "token_version": version}


@router.get("/me")
async def auth_me(claims: dict = Depends(local_auth.require_local_user_open)):
    # OPEN endpoint: returns email for any valid cookie, no allowlist gate.
    # Powers the landing-page indicator + auth.ald3.com redirect-if-authed.
    # Chat / dev have their OWN /api/me that enforces the allowlist.
    #
    # `display_language` is the site-wide UI language. It lives in this user's
    # _settings.json (shared with chat's own /api/settings) and is surfaced
    # here because /api/auth/* is the ONLY path proxied to chat-wizerith from
    # every wizerith surface (dash, drive, landing, …). The shared topbar reads
    # it from here to localize chrome on each subdomain. Read-only + side-effect
    # free: get_settings returns defaults without creating a dir if none exists.
    return {
        "email": claims["email"],
        "exp": claims.get("exp"),
        "display_language": storage.get_settings(claims["email"]).get("ui_language", "en"),
    }


@router.put("/language")
async def set_language(
    body: LanguageBody,
    claims: dict = Depends(local_auth.require_local_user_open),
):
    # Persist the site-wide display language for this user. Written to the
    # `ui_language` field of _settings.json so chat's existing settings load
    # (/api/me) and this shared endpoint stay in sync. Validation is delegated
    # to storage._coerce_settings — an unsupported code is silently dropped,
    # so we pre-check here to return a clean 400 instead of a no-op.
    if body.language not in storage._VALID_LANGUAGES:
        raise HTTPException(status_code=400, detail="unsupported language")
    settings = storage.update_settings(claims["email"], {"ui_language": body.language})
    return {"display_language": settings.get("ui_language", "en")}


def _forgejo_username(email: str) -> str:
    """Derive a Forgejo-safe username from an email's local part."""
    local = email.split("@", 1)[0].lower()
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in local)
    return safe.strip("-._") or "user"


@router.get("/forward")
async def auth_forward(claims: dict = Depends(local_auth.require_local_user)):
    # Strict dependency: the tenant SSO gate must apply the allowlist, not
    # just signature validity.
    # Forward-auth endpoint for caddy -> Forgejo SSO (git.wizerith.ai).
    # require_local_user_open validates the .wizerith.ai cookie and raises 401
    # if it's missing/invalid (caddy turns that into a redirect to
    # auth.wizerith.ai). On success we echo a sanitized username + the email as
    # headers; caddy copies them onto the upstream request and Forgejo's
    # reverse-proxy auth auto-provisions/logs in the account. 204 = authorized.
    email = claims["email"]
    return Response(
        status_code=204,
        headers={
            "X-Wizerith-User": _forgejo_username(email),
            "X-Wizerith-Email": email,
        },
    )
