"""Local email+password auth for chat.ald3.com (and dev.ald3.com via shared JWT).

This module is COMPLETELY INDEPENDENT of the CF Access path in ``auth.py``.
It is only used when the ``AUTH_MODE`` env var is set to ``local`` — set on the
``portfolio-chat`` container only, never on ``portfolio-chat-wizerith``. The
wizerith side keeps the CF Access flow in ``auth.py`` exactly as-is.

Architecture:
  * SQLite at /data/auth.db (per-container; chat-wizerith has its own /data/
    bind-mount so it would only see its own DB if it ever turned this on).
  * Pre-seeded users only: the ALLOWED_EMAILS env var lists everyone allowed
    in; on startup we INSERT-OR-IGNORE rows for each with password_hash=NULL.
    No public /register endpoint. New users use "Forgot password" to set a
    password the first time.
  * JWT_SECRET is shared with dev.ald3.com so one cookie covers both.
  * Token is set as a cookie on Domain=.ald3.com so chat + dev share it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

import bcrypt
import httpx
import jwt as pyjwt
from fastapi import Cookie, HTTPException, Request, Response, status

logger = logging.getLogger("chat.local_auth")

# ---------------------------------------------------------------------------
# Configuration (env-driven, evaluated on each call so tests/runtime can flip).
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("LOCAL_AUTH_DB", "/data/auth.db")
# fix #2: RS256 (asymmetric) is the issuing algorithm now — auth.ald3.com
# holds the private key and is the SOLE issuer; every other subdomain verifies
# with the public key only and therefore cannot forge a token even if popped.
# HS256 is kept as a *verify-only* legacy path so cookies minted before the
# cutover stay valid until they expire (7 days), then it can be removed.
JWT_ALG = "HS256"        # legacy (verify-only)
JWT_ALG_RS = "RS256"     # current issuing algorithm
JWT_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days
CODE_TTL_SECONDS = 60 * 15  # 15 minutes
# Cookie / issuer are env-driven so the same module can serve multiple
# tenants from separate containers. Defaults preserve the ald3.com
# deployment unchanged. wizerith sets COOKIE_NAME=wizerith_auth,
# COOKIE_DOMAIN=.wizerith.ai, JWT_ISSUER=wizerith-local-auth so an ald3
# cookie can never auth on wizerith and vice versa.
COOKIE_NAME = os.environ.get("AUTH_COOKIE_NAME", "ald3_auth")
COOKIE_DOMAIN = os.environ.get("AUTH_COOKIE_DOMAIN", ".ald3.com")
JWT_ISSUER = os.environ.get("AUTH_JWT_ISSUER", "ald3-local-auth")

# ---------------------------------------------------------------------------
# Rate-limit / lockout thresholds (fix #1). All windows in seconds. Tuned for a
# tiny pre-seeded user base — generous enough not to bother real users, tight
# enough that a 6-digit reset code (10^6 space, 15-min TTL) can never be
# brute-forced and that /request-password-reset can't be used to email-bomb.
# ---------------------------------------------------------------------------
LOGIN_MAX_FAILS = 5           # failed /login attempts per email before lockout
LOGIN_FAIL_WINDOW = 60 * 15   # 15-min lockout window
LOGIN_IP_MAX_FAILS = 20       # failed /login attempts per source IP
LOGIN_IP_WINDOW = 60 * 15
CODE_MAX_FAILS = 5            # wrong reset-code submissions per email+purpose
CODE_FAIL_WINDOW = CODE_TTL_SECONDS  # match the code's own lifetime
RESET_EMAIL_MAX = 3           # /request-password-reset sends per email
RESET_EMAIL_WINDOW = 60 * 15
RESET_IP_MAX = 10             # /request-password-reset sends per source IP
RESET_IP_WINDOW = 60 * 60

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _jwt_secret() -> str:
    secret = os.environ.get("JWT_SECRET", "").strip()
    if not secret:
        raise RuntimeError("JWT_SECRET env var is required when AUTH_MODE=local")
    return secret


def _pem_from_env(name: str) -> str | None:
    """Load a PEM key from env. Accepts either a raw PEM (with newlines) or a
    base64-encoded PEM (single line — convenient for .env / compose). Returns
    None when unset so callers can fall back to HS256 during the migration.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    if "-----BEGIN" in raw:
        return raw
    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return decoded if "-----BEGIN" in decoded else None


def _private_key() -> str | None:
    """RS256 signing key — present on the issuer (auth.ald3.com / chat) only."""
    return _pem_from_env("CHAT_JWT_PRIVATE_KEY")


def _public_key() -> str | None:
    """RS256 verify key — present on every subdomain that verifies tokens."""
    return _pem_from_env("CHAT_JWT_PUBLIC_KEY")


def _allowed_emails() -> set[str]:
    raw = os.environ.get("ALLOWED_EMAILS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def _allowed_email_domains() -> set[str]:
    """Optional companion to ALLOWED_EMAILS. When set, ANY email whose
    domain matches one of these is allowed in addition to the explicit
    list. Wizerith uses ``ALLOWED_EMAIL_DOMAINS=wizerith.com`` for the
    "any company email can sign in" semantics; ald3 leaves it unset so
    only the explicit list of users it knows about is allowed.
    """
    raw = os.environ.get("ALLOWED_EMAIL_DOMAINS", "")
    return {p.strip().lower().lstrip("@") for p in raw.split(",") if p.strip()}


def _passwordless() -> bool:
    return os.environ.get("AUTH_PASSWORDLESS", "").strip().lower() in ("1", "true", "yes", "on")


def _site_brand() -> str:
    return os.environ.get("SITE_BRAND", "KRAK Services")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


# ---------------------------------------------------------------------------
# SQLite storage.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY,
    password_hash TEXT,
    created_at INTEGER NOT NULL,
    last_login_at INTEGER,
    -- fix #3: bump to invalidate every outstanding token for this user.
    token_version INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS verification_codes (
    email TEXT NOT NULL,
    code TEXT NOT NULL,
    purpose TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codes_email_purpose ON verification_codes (email, purpose);

-- fix #1: fixed-window counters for login / reset-code / reset-email throttles.
CREATE TABLE IF NOT EXISTS rate_limits (
    bucket TEXT PRIMARY KEY,
    count INTEGER NOT NULL,
    reset_at INTEGER NOT NULL
);

-- Emergency operator override: emails listed here get a fixed, known
-- verification code (EMERGENCY_UNLOCK_CODE) from issue_code instead of a
-- random one, so the operator can grant break-glass access without the user's
-- inbox. Toggled out-of-band by the Discord bridge's !unlock / !lock commands
-- (which write this same table directly on the shared host DB). An unlocked
-- account is takeover-able by anyone who knows the fixed code, so it is meant
-- to be re-locked promptly.
CREATE TABLE IF NOT EXISTS unlocked_emails (
    email TEXT PRIMARY KEY,
    unlocked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One row per issued JWT (keyed by its `jti`). Recorded at every successful
-- login so the admin console can list "who is signed in", from where, and
-- when. `revoked=1` is an out-of-band per-session kill switch: the JWT verify
-- path (`_decode_jwt_open`) rejects any token whose jti has a revoked row,
-- which is what lets an operator terminate ONE device without bumping
-- token_version (= logout everywhere). `last_seen` is refreshed (throttled) on
-- each authenticated request. Tokens minted before this table existed simply
-- have no row and are unaffected.
CREATE TABLE IF NOT EXISTS sessions (
    jti TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    login_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    last_seen INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_sessions_email ON sessions (email);
"""


def _connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db_and_seed() -> None:
    """Create tables (idempotent) and INSERT-OR-IGNORE rows for ALLOWED_EMAILS.

    Called once at app startup when AUTH_MODE=local.
    """
    with _connect() as conn:
        conn.executescript(_SCHEMA)
        # Migrate pre-existing DBs (created before fix #3) that lack the
        # token_version column. ADD COLUMN is a no-op-safe one-shot.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "token_version" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
        now = int(time.time())
        for email in _allowed_emails():
            conn.execute(
                "INSERT OR IGNORE INTO users (email, password_hash, created_at) VALUES (?, NULL, ?)",
                (email, now),
            )
        conn.commit()
    logger.info("local_auth: db initialised at %s with %d seeded users", DB_PATH, len(_allowed_emails()))


# ---------------------------------------------------------------------------
# Rate limiting / lockout (fix #1).
#
# Fixed-window counters in the `rate_limits` table. Two usage patterns:
#   * failure-based (login, reset-code): `_rate_guard` gates BEFORE the check,
#     `_rate_incr` records a failure, `_rate_reset` clears on success.
#   * volume-based (reset-email send): `_rate_limit` increments-then-gates so
#     every call counts toward the cap regardless of outcome.
# All raise HTTPException(429) with a Retry-After header when tripped.
# ---------------------------------------------------------------------------

class RateLimited(HTTPException):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts, try again later",
            headers={"Retry-After": str(max(1, retry_after))},
        )


def client_ip(request: Request) -> str:
    """Best-effort source IP. Behind Cloudflare → cloudflared → Caddy, the
    real client is in CF-Connecting-IP; fall back through XFF then the socket.
    """
    for header in ("CF-Connecting-IP", "X-Forwarded-For"):
        value = request.headers.get(header)
        if value:
            return value.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_guard(bucket: str, limit: int) -> None:
    """Raise 429 if `bucket` is already at/over `limit` within its live window."""
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT count, reset_at FROM rate_limits WHERE bucket=?", (bucket,)
        ).fetchone()
    if row and row[1] > now and row[0] >= limit:
        raise RateLimited(row[1] - now)


def _rate_incr(bucket: str, window: int) -> None:
    """Record one hit against `bucket`, (re)starting the window if expired."""
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT count, reset_at FROM rate_limits WHERE bucket=?", (bucket,)
        ).fetchone()
        if not row or row[1] <= now:
            conn.execute(
                "INSERT INTO rate_limits (bucket, count, reset_at) VALUES (?, 1, ?) "
                "ON CONFLICT(bucket) DO UPDATE SET count=1, reset_at=excluded.reset_at",
                (bucket, now + window),
            )
        else:
            conn.execute("UPDATE rate_limits SET count=count+1 WHERE bucket=?", (bucket,))
        conn.commit()


def _rate_reset(bucket: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM rate_limits WHERE bucket=?", (bucket,))
        conn.commit()


def _rate_limit(bucket: str, limit: int, window: int) -> None:
    """Volume cap: increment first, then 429 once the window total exceeds `limit`."""
    _rate_incr(bucket, window)
    now = int(time.time())
    with _connect() as conn:
        row = conn.execute(
            "SELECT count, reset_at FROM rate_limits WHERE bucket=?", (bucket,)
        ).fetchone()
    if row and row[0] > limit:
        raise RateLimited(row[1] - now)


# ---------------------------------------------------------------------------
# Email sending (Resend).
# ---------------------------------------------------------------------------

async def _send_code_email(email: str, code: str, purpose: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    from_addr = os.environ.get("FROM_EMAIL", "onboarding@resend.dev").strip()
    if not api_key:
        raise RuntimeError("RESEND_API_KEY env var is required for local auth")
    site = _site_brand()
    # Three purposes today:
    #   reset  → password-reset flow (ald3) — user already has a password.
    #   login  → passwordless flow (wizerith) — user just wants to sign in.
    #   else   → first-time setup (ald3 sign-up). Asks them to set a password
    #            after entering the code.
    if purpose == "reset":
        subject = f"{site} — password reset code"
        intro = "Use this code to reset your password."
    elif purpose == "login":
        subject = f"{site} — sign-in code"
        intro = "Use this code to sign in."
    else:
        subject = f"{site} — set up your account"
        intro = "Use this code to set your password and finish signing in."
    text = (
        f"{intro}\n\nCode: {code}\n\n"
        "This code expires in 15 minutes. If you didn't request it, ignore this email."
    )
    html = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; max-width: 480px; margin: auto; padding: 24px;">
      <p style="margin: 0 0 16px; color: #111;">{intro}</p>
      <p style="margin: 24px 0; font-size: 28px; letter-spacing: 8px; font-weight: 600; text-align: center; padding: 16px; background: #f4f4f5; border-radius: 8px;">{code}</p>
      <p style="margin: 16px 0 0; color: #71717a; font-size: 13px;">This code expires in 15 minutes. If you didn't request it, ignore this email.</p>
    </div>
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": f"{site} <{from_addr}>",
                "to": [email],
                "subject": subject,
                "text": text,
                "html": html,
            },
        )
    if r.status_code >= 400:
        logger.error("resend send failed status=%s body=%s", r.status_code, r.text)
        raise RuntimeError(f"resend send failed: {r.status_code}")


# ---------------------------------------------------------------------------
# Core operations (called from local_auth_routes.py).
# ---------------------------------------------------------------------------

def _validate_email(email: str) -> str:
    e = _normalize_email(email)
    if not _EMAIL_RE.match(e):
        raise HTTPException(status_code=400, detail="invalid email")
    return e


def is_allowed(email: str) -> bool:
    """Access gate for chat / dev (called inside verify_jwt_token).
    Two ways to qualify: explicit ALLOWED_EMAILS membership, OR domain
    match on ALLOWED_EMAIL_DOMAINS. The latter is what enables the
    "any @wizerith.com user signs in" model without enumerating every
    employee in env. Tokens for emails that satisfy neither still
    decode fine (so `/api/auth/me` works for bet/market on ald3) but
    chat / dev's strict require_local_user 403s them."""
    e = _normalize_email(email)
    if e in _allowed_emails():
        return True
    domains = _allowed_email_domains()
    if not domains:
        return False
    domain = e.split("@", 1)[1] if "@" in e else ""
    return domain in domains


def user_exists(email: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE email=?", (_normalize_email(email),)).fetchone()
        return row is not None


def ensure_user(email: str) -> None:
    """Open-registration helper: create a row for ``email`` if it doesn't
    already exist. Allowlist enforcement happens in ``verify_jwt_token``,
    not here — anyone can sign up; only allowlisted users can reach chat /
    dev. Email shape is validated; password_hash stays NULL until they
    finish the code-flow.
    """
    e = _validate_email(email)
    now = int(time.time())
    with _connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (email, password_hash, created_at) VALUES (?, NULL, ?)",
            (e, now),
        )
        conn.commit()


def get_user(email: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT email, password_hash, created_at, last_login_at FROM users WHERE email=?",
            (_normalize_email(email),),
        ).fetchone()
        if not row:
            return None
        return {
            "email": row[0],
            "password_hash": row[1],
            "created_at": row[2],
            "last_login_at": row[3],
        }


# Fixed code handed out for emails in the unlocked_emails table. Six digits so
# it satisfies the same client-side input validation as a real code.
EMERGENCY_UNLOCK_CODE = "000000"


def _new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def is_unlocked(email: str) -> bool:
    """True if ``email`` is in the emergency override table (gets a fixed code)."""
    e = _normalize_email(email)
    with _connect() as conn:
        return (
            conn.execute(
                "SELECT 1 FROM unlocked_emails WHERE email=?", (e,)
            ).fetchone()
            is not None
        )


def lock_email(email: str) -> bool:
    """Remove ``email`` from the emergency override table. Returns True if a row
    was removed. Called automatically after a successful emergency login so the
    fixed code is strictly single-use."""
    e = _normalize_email(email)
    with _connect() as conn:
        cur = conn.execute("DELETE FROM unlocked_emails WHERE email=?", (e,))
        conn.commit()
    return cur.rowcount > 0


def issue_code(email: str, purpose: str) -> str:
    """Generate, store, and return a 6-digit code for ``email`` / ``purpose``.

    Expires 15 min.

    Emergency override: an unlocked email gets the fixed EMERGENCY_UNLOCK_CODE
    instead of a random one — but ONLY for the passwordless ``login`` flow. The
    ``reset`` flow always gets a random code even while unlocked, so the
    break-glass code can never be redeemed to *set a password* and thereby
    survive a re-lock. Combined with single-use auto-relock in login-with-code,
    the override grants exactly one transient sign-in and nothing more.
    """
    e = _validate_email(email)
    code = EMERGENCY_UNLOCK_CODE if (purpose == "login" and is_unlocked(e)) else _new_code()
    now = int(time.time())
    with _connect() as conn:
        # Invalidate any previous unused codes for the same email+purpose so a
        # user can request a fresh one without juggling old ones.
        conn.execute(
            "UPDATE verification_codes SET used=1 WHERE email=? AND purpose=? AND used=0",
            (e, purpose),
        )
        conn.execute(
            "INSERT INTO verification_codes (email, code, purpose, expires_at, used, created_at) VALUES (?, ?, ?, ?, 0, ?)",
            (e, code, purpose, now + CODE_TTL_SECONDS, now),
        )
        conn.commit()
    return code


def consume_code(email: str, code: str, purpose: str) -> bool:
    """Mark a code used. Returns True if the code was valid + un-used + un-expired.

    Fix #1: hard-caps wrong guesses per email+purpose. After ``CODE_MAX_FAILS``
    bad submissions inside the window we 429 *and* invalidate every outstanding
    code for that email+purpose, so even a correct subsequent guess can't land —
    the user must request a fresh code. This is what makes the 6-digit code
    un-brute-forceable inside its 15-minute life.
    """
    e = _normalize_email(email)
    now = int(time.time())
    fail_bucket = f"codefail:{e}:{purpose}"
    _rate_guard(fail_bucket, CODE_MAX_FAILS)
    with _connect() as conn:
        row = conn.execute(
            "SELECT rowid, expires_at, used FROM verification_codes WHERE email=? AND code=? AND purpose=? ORDER BY rowid DESC LIMIT 1",
            (e, code, purpose),
        ).fetchone()
        if not row or row[2] or row[1] < now:  # missing | used | expired
            _rate_incr(fail_bucket, CODE_FAIL_WINDOW)
            # If that tipped us over the limit, burn all live codes so the
            # attacker can't keep guessing against a known-good code.
            guard_row = conn.execute(
                "SELECT count FROM rate_limits WHERE bucket=?", (fail_bucket,)
            ).fetchone()
            if guard_row and guard_row[0] >= CODE_MAX_FAILS:
                conn.execute(
                    "UPDATE verification_codes SET used=1 WHERE email=? AND purpose=? AND used=0",
                    (e, purpose),
                )
                conn.commit()
            return False
        conn.execute("UPDATE verification_codes SET used=1 WHERE rowid=?", (row[0],))
        conn.commit()
    _rate_reset(fail_bucket)  # success clears the failure counter
    return True


def set_password(email: str, new_password: str) -> None:
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="password must be at least 8 characters")
    e = _normalize_email(email)
    hashed = bcrypt.hashpw(new_password.encode("utf-8"), bcrypt.gensalt(rounds=10)).decode("utf-8")
    with _connect() as conn:
        cur = conn.execute("UPDATE users SET password_hash=? WHERE email=?", (hashed, e))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="user not found")
        conn.commit()


def verify_password(email: str, password: str) -> bool:
    user = get_user(email)
    if not user or not user.get("password_hash"):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8"))
    except Exception:
        return False


def stamp_login(email: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET last_login_at=? WHERE email=?", (int(time.time()), _normalize_email(email)))
        conn.commit()


# ---------------------------------------------------------------------------
# Token versioning / revocation (fix #3).
#
# Each user row carries a `token_version`; every issued JWT embeds it as `tv`.
# Verification rejects any token whose `tv` != the user's current version, so
# bumping the version is an instant server-side kill switch for *all* of that
# user's outstanding tokens — what plain cookie-clearing logout never gave us.
#
# Caveat: only the Python chat/dev verifier honours `tv`. The betting Node
# verifier (entertainment stack) ignores unknown claims, so a revoked token is
# still accepted by bet/market until it expires. Closing that needs the same
# check added there — tracked separately.
# ---------------------------------------------------------------------------

def get_token_version(email: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT token_version FROM users WHERE email=?", (_normalize_email(email),)
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def revoke_all_sessions(email: str) -> int:
    """Invalidate every outstanding token for ``email``. Returns the new version.

    Use for logout-everywhere and incident response (suspected token theft).
    Creates the row at version 1 if the user somehow has no row yet.
    """
    e = _normalize_email(email)
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET token_version = token_version + 1 WHERE email=?", (e,)
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT OR IGNORE INTO users (email, password_hash, created_at, token_version) VALUES (?, NULL, ?, 1)",
                (e, int(time.time())),
            )
        conn.commit()
    return get_token_version(e)


# ---------------------------------------------------------------------------
# Per-session tracking + termination.
#
# Each issued JWT carries a unique `jti`. record_session() writes one row per
# login (called from the route handlers, which have the request's IP + UA).
# The verify path then consults `_session_revoked_or_touch`, which (a) rejects
# the token if its session row is marked revoked — an operator "terminate this
# one device" without touching token_version — and (b) refreshes last_seen so
# the admin console shows live activity. Both are wrapped so a storage hiccup
# never locks out an otherwise-valid token (fail-open on the touch, the revoke
# row must be explicitly present to deny).
# ---------------------------------------------------------------------------

# Don't write last_seen on every single request — only once this many seconds
# have elapsed since the previous touch, to keep the hot path mostly read-only.
SESSION_TOUCH_INTERVAL = 60


def record_session(token: str, email: str, ip: str, user_agent: str) -> None:
    """Persist a session row for a freshly-issued ``token``. Best-effort: any
    failure is logged and swallowed so a tracking glitch never blocks login."""
    try:
        claims = pyjwt.decode(token, options={"verify_signature": False})
    except pyjwt.InvalidTokenError:
        logger.warning("record_session: could not decode freshly-issued token")
        return
    jti = claims.get("jti")
    if not jti:
        return
    e = _normalize_email(email)
    now = int(time.time())
    exp = int(claims.get("exp") or (now + JWT_TTL_SECONDS))
    ua = (user_agent or "")[:400]
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO sessions "
                "(jti, email, ip, user_agent, login_at, expires_at, last_seen, revoked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
                (str(jti), e, ip, ua, now, exp, now),
            )
            conn.commit()
    except Exception:
        logger.exception("record_session failed for %s", e)


def _session_revoked_or_touch(jti: str) -> bool:
    """Return True iff this jti has an explicitly-revoked session row. Side
    effect: refresh last_seen (throttled). Fail-open — a DB error returns False
    so storage trouble can't lock out valid sessions; only a present
    ``revoked=1`` row denies."""
    try:
        now = int(time.time())
        with _connect() as conn:
            row = conn.execute(
                "SELECT revoked, last_seen FROM sessions WHERE jti=?", (jti,)
            ).fetchone()
            if row is None:
                return False
            if row[0]:
                return True
            if row[1] is None or now - int(row[1]) >= SESSION_TOUCH_INTERVAL:
                conn.execute("UPDATE sessions SET last_seen=? WHERE jti=?", (now, jti))
                conn.commit()
        return False
    except Exception:
        logger.exception("session check failed for jti=%s", jti)
        return False


def terminate_session(jti: str) -> bool:
    """Mark a single session revoked. Returns True if a row was updated. The
    chat verify path picks this up immediately (shared DB), so the targeted
    cookie 401s on its next request while the user's other sessions live on."""
    with _connect() as conn:
        cur = conn.execute("UPDATE sessions SET revoked=1 WHERE jti=?", (jti,))
        conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# JWT issue + verify.
# ---------------------------------------------------------------------------

def issue_jwt(email: str) -> str:
    now = int(time.time())
    e = _normalize_email(email)
    payload = {
        "sub": e,
        "email": e,
        "iat": now,
        "exp": now + JWT_TTL_SECONDS,
        "iss": JWT_ISSUER,
        "tv": get_token_version(e),       # fix #3: revocation epoch
        "jti": secrets.token_hex(8),      # per-token id for log correlation
    }
    # fix #2: sign with the RS256 private key when present (the only place that
    # holds it — auth.ald3.com). Fall back to HS256 only if the key isn't wired
    # yet, so a half-finished rollout still issues working tokens.
    priv = _private_key()
    if priv:
        return pyjwt.encode(payload, priv, algorithm=JWT_ALG_RS)
    return pyjwt.encode(payload, _jwt_secret(), algorithm=JWT_ALG)


def _decode_token(token: str) -> dict[str, Any]:
    """Signature + required-claims check only. Selects RS256 (public key) or
    legacy HS256 (shared secret) by the token header's ``alg``, restricted to
    that fixed allowlist. Raises HTTPException(401) on any failure.
    """
    try:
        alg = pyjwt.get_unverified_header(token).get("alg")
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc

    if alg == JWT_ALG_RS:
        key = _public_key()
        algorithms = [JWT_ALG_RS]
    elif alg == JWT_ALG:  # legacy HS256, verify-only during migration
        key = os.environ.get("JWT_SECRET", "").strip() or None
        algorithms = [JWT_ALG]
    else:
        raise HTTPException(status_code=401, detail="invalid token")
    if not key:
        raise HTTPException(status_code=401, detail="invalid token")

    try:
        return pyjwt.decode(
            token,
            key,
            algorithms=algorithms,
            issuer=JWT_ISSUER,
            options={"require": ["exp", "iat", "iss", "email"]},
        )
    except pyjwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc


def _decode_jwt_open(token: str) -> dict[str, Any]:
    """Validate signature + required claims. NO allowlist check.

    Used by /api/auth/me (open identity endpoint) and bet / market which
    accept any valid cookie. Allowlist gating happens in
    ``verify_jwt_token`` below, which chat / dev use for their own
    /api/me.

    fix #2: verifies RS256 (current) or HS256 (legacy) tokens. The algorithm
    is chosen from the token's own header but constrained to this two-entry
    allowlist, and each branch passes a key of the matching type — so the
    RSA-public-key-as-HMAC-secret confusion attack is not possible.
    """
    claims = _decode_token(token)
    email = _normalize_email(claims.get("email", ""))
    if not email:
        raise HTTPException(status_code=401, detail="token missing email")
    # fix #3: reject tokens minted before the user's current revocation epoch.
    # Absent `tv` is treated as 0, so tokens issued before this change stay
    # valid until the first revoke bumps the version — no forced mass logout.
    if int(claims.get("tv", 0) or 0) != get_token_version(email):
        raise HTTPException(status_code=401, detail="token revoked")
    # Per-session kill switch: reject this exact token if an operator
    # terminated its session. Also refreshes last_seen for the admin console.
    # Tokens with no session row (minted before tracking existed) pass through.
    jti = claims.get("jti")
    if jti and _session_revoked_or_touch(str(jti)):
        raise HTTPException(status_code=401, detail="session terminated")
    claims["email"] = email
    return claims


def verify_jwt_token(token: str) -> dict[str, Any]:
    """Strict variant: signature + allowlist. Used by chat / dev /api/me.

    Allowlist revocation is immediate: drop user the moment their email
    leaves ALLOWED_EMAILS, even on an otherwise-valid still-fresh token.
    """
    claims = _decode_jwt_open(token)
    if not is_allowed(claims["email"]):
        raise HTTPException(status_code=403, detail="email not authorized")
    return claims


# ---------------------------------------------------------------------------
# FastAPI dependency: extract + verify token from cookie or Authorization header.
# ---------------------------------------------------------------------------

def _extract_token(request: Request) -> str:
    # Read the cookie by the env-configurable name so multi-tenant
    # deployments (ald3=ald3_auth, wizerith=wizerith_auth) don't collide.
    # Authorization: Bearer header is always accepted as a fallback so
    # API clients without cookie handling still work.
    token: str | None = request.cookies.get(COOKIE_NAME)
    if not token:
        authz = request.headers.get("Authorization", "")
        if authz.startswith("Bearer "):
            token = authz[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="not authenticated")
    return token


async def require_local_user(request: Request) -> dict[str, Any]:
    """Strict dependency: 401 if no cookie, 403 if email not allowlisted.

    Used by chat / dev backend's /api/me.
    """
    token = _extract_token(request)
    claims = verify_jwt_token(token)
    request.state.user_email = claims["email"]
    return claims


async def require_local_user_open(request: Request) -> dict[str, Any]:
    """Open dependency: 401 if no cookie, 200 for ANY valid JWT.

    Used by /api/auth/me so non-allowlisted users (who can use bet /
    market but not chat / dev) still get a valid identity response on
    the landing page and on auth.ald3.com.
    """
    token = _extract_token(request)
    claims = _decode_jwt_open(token)
    request.state.user_email = claims["email"]
    return claims


def set_auth_cookie(response: Response, token: str) -> None:
    """Set the shared .ald3.com cookie so chat + dev both see it."""
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=JWT_TTL_SECONDS,
        domain=COOKIE_DOMAIN,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, domain=COOKIE_DOMAIN, path="/")


__all__ = [
    "COOKIE_NAME",
    "COOKIE_DOMAIN",
    "client_ip",
    "clear_auth_cookie",
    "consume_code",
    "ensure_user",
    "get_token_version",
    "init_db_and_seed",
    "is_allowed",
    "issue_code",
    "issue_jwt",
    "lock_email",
    "record_session",
    "require_local_user",
    "require_local_user_open",
    "revoke_all_sessions",
    "terminate_session",
    "set_auth_cookie",
    "set_password",
    "stamp_login",
    "user_exists",
    "verify_jwt_token",
    "verify_password",
    "RateLimited",
    "LOGIN_MAX_FAILS",
    "LOGIN_FAIL_WINDOW",
    "LOGIN_IP_MAX_FAILS",
    "LOGIN_IP_WINDOW",
    "RESET_EMAIL_MAX",
    "RESET_EMAIL_WINDOW",
    "RESET_IP_MAX",
    "RESET_IP_WINDOW",
]
